#!/usr/bin/env python3
"""Safely remove PreviewMesh installation traces before a fresh setup.

The cleanup menu is intentionally narrower than a machine reset. It removes
only paths and resources that PreviewMesh creates, and leaves credentials unless
they are explicitly selected, repositories, K3s itself, and unrelated firewall
rules alone.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass


REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$")
RUNNER_SERVICE = re.compile(r"actions\.runner\.[A-Za-z0-9_.@-]+\.service$")
UNIT_NAMES = (
    "previewmesh-maintenance.service",
    "previewmesh-maintenance.timer",
    "previewmesh-ingress.service",
    "previewmesh-ingress.socket",
)
KNOWN_BINARIES = ("gh", "go", "helm", "kubectl")


def exists(path: Path) -> bool:
    """Check a path without following a final symlink."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def is_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def valid_repo(value: object) -> bool:
    return isinstance(value, str) and bool(REPOSITORY.fullmatch(value))


def valid_service(value: object) -> bool:
    return isinstance(value, str) and bool(RUNNER_SERVICE.fullmatch(value))


def json_file(path: Path) -> dict:
    """Read a local metadata file, returning an empty result on bad input."""
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def text(en: str, zh: str, language: str) -> str:
    return zh if language == "zh-CN" else en


@dataclass
class Choice:
    key: str
    en: str
    zh: str
    explanation_en: str
    explanation_zh: str
    warning_en: str = ""
    warning_zh: str = ""

    def title(self, language: str) -> str:
        return text(self.en, self.zh, language)

    def explanation(self, language: str) -> str:
        return text(self.explanation_en, self.explanation_zh, language)

    def warning(self, language: str) -> str:
        return text(self.warning_en, self.warning_zh, language)


class Cleaner:
    def __init__(self, config_path: Path, language: str, dry_run: bool):
        self.language = language
        self.dry_run = dry_run
        self.config_path = absolute(config_path)
        self.home = absolute(Path.home())
        self.state_dir = self.home / ".local" / "share" / "previewmesh"
        self.state_path = self.state_dir / "install-state.json"
        self.state_backup = self.state_dir / "install-state.json.bak"
        self.runner_dir = self.state_dir / "runner"
        self.state = json_file(self.state_path)
        self.runner = json_file(self.runner_dir / ".runner")
        self.systemd_dir = Path("/etc/systemd/system")
        self.etc_dir = Path("/etc/previewmesh")
        self.system_lib_dir = Path("/usr/local/lib/previewmesh")
        self.configured_control = self._configured_control_repository()
        self.credential_paths = self._credential_paths()
        self.control = self._control_repository()
        self.control_dir = self._control_directory()
        self.runner_config = self._runner_config_path()
        self.runner_service = self._runner_service_name()
        self.agent_id = self._agent_id()
        self.failures: list[str] = []
        self.changed = False

    def _configured_control_repository(self) -> str | None:
        if not exists(self.config_path) or self.config_path.is_symlink() or not self.config_path.is_file():
            return None
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(self.config_path, encoding="utf-8")
            value = parser.get("project", "control_repository", fallback="").strip()
        except (OSError, configparser.Error, UnicodeError):
            return None
        return value if valid_repo(value) else None

    def _control_repository(self) -> str | None:
        value = self.state.get("control")
        if valid_repo(value):
            return value
        value = self.runner.get("gitHubUrl")
        match = re.fullmatch(r"https?://github\.com/([^/]+/[^/]+)/?", value or "", re.IGNORECASE)
        return match.group(1) if match and valid_repo(match.group(1)) else None

    def _control_directory(self) -> Path | None:
        candidates = []
        if exists(self.config_path) and not self.config_path.is_symlink() and self.config_path.is_file():
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(self.config_path, encoding="utf-8")
                value = parser.get("project", "control_directory", fallback="").strip()
                if value:
                    candidate = Path(value).expanduser()
                    candidates.append(candidate if candidate.is_absolute() else self.config_path.parent / candidate)
            except (OSError, configparser.Error, UnicodeError):
                pass
        value = self.state.get("control_directory")
        if isinstance(value, str) and value:
            candidates.append(Path(value).expanduser())
        for candidate in candidates:
            candidate = absolute(candidate)
            if candidate != Path("/"):
                return candidate
        return None

    def _credential_paths(self) -> list[Path]:
        candidates: list[Path] = []

        def add(value: object) -> None:
            if not isinstance(value, str) or not value or any(char in value for char in "\0\r\n"):
                return
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = self.config_path.parent / path
            path = absolute(path)
            if path not in candidates and path not in (Path("/"), self.home):
                candidates.append(path)

        if exists(self.config_path) and not self.config_path.is_symlink() and self.config_path.is_file():
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(self.config_path, encoding="utf-8")
                for key in ("dispatch_token_file", "ghcr_token_file"):
                    add(parser.get("credentials", key, fallback=""))
                for section in parser.sections():
                    if section.startswith("source:"):
                        add(parser.get(section, "token_file", fallback=""))
            except (OSError, configparser.Error, UnicodeError):
                pass

        # Keep a reset useful after setup.ini was removed in an earlier run.
        default_secrets = self.home / ".config" / "previewmesh" / "secrets"
        try:
            for path in sorted(default_secrets.glob("*.token")):
                add(str(path))
        except OSError:
            pass
        return candidates

    def _runner_config_path(self) -> Path | None:
        if self.control_dir:
            candidate = self.control_dir / "config" / "previewmesh-runner.yaml"
            if candidate.parent.name == "config" and candidate.name == "previewmesh-runner.yaml":
                return candidate
        return None

    def _runner_service_name(self) -> str | None:
        value = self.state.get("runner_service")
        if valid_service(value):
            return value
        service_file = self.runner_dir / ".service"
        try:
            value = service_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            value = ""
        if valid_service(value):
            return value
        # Recover a stale service after state.json was removed in an earlier run.
        try:
            candidates = sorted(self.systemd_dir.glob("actions.runner.*.service.d/previewmesh.conf"))
        except OSError:
            candidates = []
        for dropin in candidates:
            service = dropin.parent.name.removesuffix(".d")
            if not valid_service(service) or dropin.is_symlink():
                continue
            try:
                content = dropin.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if "KUBECONFIG=" in content and "previewmesh-runner.yaml" in content:
                return service
        return None

    def _agent_id(self) -> str | None:
        value = self.runner.get("agentId")
        if isinstance(value, int) and value > 0:
            return str(value)
        if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
            return value
        return None

    def choices(self) -> list[Choice]:
        return [
            Choice(
                "config",
                "Configuration and unfinished wizard draft",
                "活动配置和未完成的向导草稿",
                "Removes setup.ini and setup.ini.draft so the next init starts without old repository, path, network, or token-file references. Token files are handled by the separate credentials item.",
                "删除 setup.ini 和 setup.ini.draft，让下一次 init 不再沿用旧仓库、路径、网络或 Token 文件引用；Token 文件由单独的凭据项目处理。",
            ),
            Choice(
                "credentials",
                "Configured PreviewMesh token files",
                "已配置的 PreviewMesh Token 文件",
                "Removes token files referenced by the active setup.ini, plus .token files in the default PreviewMesh secrets directory. Choose this when old or expired tokens must not be reused.",
                "删除活动 setup.ini 引用的 Token 文件，以及 PreviewMesh 默认 secrets 目录中的 .token 文件。旧 Token 或 Token 过期且不应复用时选择此项。",
                "Token values cannot be recovered by PreviewMesh; create replacement files before install needs them.",
                "PreviewMesh 无法恢复 Token 内容；安装需要时请先创建新的 Token 文件。",
            ),
            Choice(
                "state",
                "Installer state, logs, downloaded tools and local runner files",
                "安装器状态、日志、下载工具和本机 Runner 文件",
                "Removes checkpoints, the resume log, installer-managed tool links/cache, the local Actions Runner registration, and the generated runner kubeconfig in the private control checkout when it can be identified.",
                "删除断点状态、恢复日志、安装器管理的工具链接/缓存、本机 Actions Runner 登记，以及能确定位置的私有 control 仓库 Runner kubeconfig。",
                "This loses resume evidence; select GitHub Runner registration too, or the remote runner name may remain occupied.",
                "这会丢失恢复证据；还应勾选 GitHub Runner 远端登记，否则远端 Runner 名称可能仍被占用。",
            ),
            Choice(
                "services",
                "PreviewMesh system services, generated files and firewall rule",
                "PreviewMesh 系统服务、生成文件和防火墙规则",
                "Stops and removes only PreviewMesh maintenance/ingress units, their generated helpers and /etc/previewmesh metadata, plus the managed PREVIEWMESH-IN IPv4 firewall chain.",
                "只停止并删除 PreviewMesh 维护/入口服务、对应生成脚本和 /etc/previewmesh 元数据，以及带有 PreviewMesh 标记的 PREVIEWMESH-IN IPv4 防火墙链。",
                "It does not uninstall K3s or flush unrelated firewall rules.",
                "不会卸载 K3s，也不会清空无关的防火墙规则。",
            ),
            Choice(
                "runner",
                "GitHub Actions Runner registration",
                "GitHub Actions Runner 远端登记",
                "Deletes the identified self-hosted Runner registration from the configured private deployment-management repository.",
                "从已识别的私有部署管理仓库删除本机 self-hosted Runner 的远端登记。",
                "This is a remote destructive action. It does not delete the repository or workflows and requires a second REMOVE confirmation.",
                "这是远端破坏性操作；不会删除仓库或工作流，并且需要再次输入 REMOVE 确认。",
            ),
            Choice(
                "cluster",
                "PreviewMesh Kubernetes resources",
                "PreviewMesh Kubernetes 资源",
                "Deletes the exact previewmesh-system namespace, PreviewMesh-managed preview namespaces, and the exact previewmesh-runner RBAC objects.",
                "删除精确名称为 previewmesh-system 的命名空间、带 PreviewMesh 管理标签的预览命名空间，以及精确名称为 previewmesh-runner 的 RBAC 对象。",
                "This removes preview workloads and their temporary data. It does not uninstall K3s or touch unlabelled namespaces.",
                "这会删除预览工作负载及其临时数据；不会卸载 K3s，也不会触碰没有 PreviewMesh 标签的命名空间。",
            ),
        ]

    def trace_lines(self, key: str) -> list[str]:
        if key == "config":
            return [str(path) for path in (self.config_path, self.config_path.with_name(self.config_path.name + ".draft")) if exists(path)]
        if key == "credentials":
            return [str(path) for path in self.credential_paths if exists(path)]
        if key == "state":
            paths = [self.state_path, self.state_backup, self.state_dir / "setup.log", self.state_dir / "install.lock"]
            paths.extend(self.state_dir / name for name in ("runner", "tools"))
            paths.extend(self.state_dir / "bin" / name for name in KNOWN_BINARIES if exists(self.state_dir / "bin" / name))
            if self.runner_config and exists(self.runner_config):
                paths.extend((self.runner_config, self.runner_config.parent / ".runner-check"))
            return [str(path) for path in paths if exists(path)]
        if key == "services":
            paths = [
                self.etc_dir / "installation.json",
                self.etc_dir / "ingress.env",
                self.system_lib_dir / "maintain.py",
                self.system_lib_dir / "ingress-route.py",
            ]
            paths.extend(self.systemd_dir / name for name in UNIT_NAMES)
            if self.runner_service:
                paths.extend((self.systemd_dir / self.runner_service,
                              self.systemd_dir / (self.runner_service + ".d") / "previewmesh.conf"))
            return [str(path) for path in paths if exists(path)]
        if key == "runner":
            if self.control and self.agent_id:
                return [f"GitHub runner {self.control} (id {self.agent_id})"]
            return []
        if key == "cluster":
            return ["K3s API: previewmesh-system and label previewmesh.local/managed-by=previewmesh"]
        return []

    def available(self, key: str) -> bool:
        if key == "cluster":
            return bool(shutil.which("k3s") or exists(Path("/usr/local/bin/k3s")))
        return bool(self.trace_lines(key))

    def describe(self, choice: Choice) -> None:
        print(f"    {choice.explanation(self.language)}")
        warning = choice.warning(self.language)
        if warning:
            print(f"    ! {warning}")
        if choice.key == "state" and self.configured_control and self.state.get("control") != self.configured_control:
            print("    ! " + text(
                "install-state.json belongs to a different deployment-management repository than setup.ini; this ownership mismatch makes install stop safely. Removing the local state starts a new local record, but does not undo old remote resources.",
                "install-state.json 所属的部署管理仓库与 setup.ini 不同；这个归属冲突会让 install 安全停止。删除本机状态只会开始新的本地记录，不会撤销旧的远端资源。",
                self.language,
            ))
        traces = self.trace_lines(choice.key)
        if traces:
            for item in traces[:8]:
                print(f"      - {item}")
            if len(traces) > 8:
                print(text("      - ...", "      - ……", self.language))
        else:
            print(text("      - No matching local trace detected.", "      - 未检测到对应的本地痕迹。", self.language))

    def _privileged_prefix(self) -> list[str] | None:
        if os.geteuid() == 0:
            return []
        if shutil.which("sudo"):
            return ["sudo"]
        return None

    def run_privileged(self, args: list[str], *, tolerate: bool = False) -> subprocess.CompletedProcess | None:
        prefix = self._privileged_prefix()
        if prefix is None:
            self.fail(text("sudo is required for this selected item.", "所选项目需要 sudo。", self.language))
            return None
        try:
            result = subprocess.run([*prefix, *args], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            self.fail(text("A privileged cleanup command could not be completed.", "特权清理命令无法完成。", self.language))
            return None
        if result.returncode and not tolerate:
            self.fail(text(f"Cleanup command failed: {args[0]}", f"清理命令失败：{args[0]}", self.language))
        return result

    def fail(self, message: str) -> None:
        self.failures.append(message)
        print(f"! {message}", file=sys.stderr)

    def remove_user_file(self, path: Path) -> None:
        if not exists(path):
            return
        if path.is_symlink():
            self.fail(text(f"Refusing symlink: {path}", f"拒绝处理符号链接：{path}", self.language))
            return
        if is_directory(path):
            self.fail(text(f"Expected a file, found a directory: {path}", f"应为文件但发现目录：{path}", self.language))
            return
        if self.dry_run:
            print(f"  {text('Would remove', '将删除', self.language)} {path}")
            return
        try:
            path.unlink()
            self.changed = True
            print(f"  {text('Removed', '已删除', self.language)} {path}")
        except OSError:
            self.fail(text(f"Could not remove {path}", f"无法删除 {path}", self.language))

    def remove_user_tree(self, path: Path) -> None:
        if not exists(path):
            return
        if path.is_symlink():
            self.fail(text(f"Refusing symlink directory: {path}", f"拒绝处理符号链接目录：{path}", self.language))
            return
        if not is_directory(path):
            self.fail(text(f"Expected a directory: {path}", f"应为目录：{path}", self.language))
            return
        if self.dry_run:
            print(f"  {text('Would remove', '将删除', self.language)} {path}")
            return
        try:
            shutil.rmtree(path)
            self.changed = True
            print(f"  {text('Removed', '已删除', self.language)} {path}")
        except OSError:
            self.fail(text(f"Could not remove {path}", f"无法删除 {path}", self.language))

    def remove_privileged_file(self, path: Path) -> None:
        if not exists(path):
            return
        if self.dry_run:
            print(f"  {text('Would remove', '将删除', self.language)} {path}")
            return
        result = self.run_privileged(["rm", "-f", "--", str(path)])
        if result and result.returncode == 0:
            self.changed = True
            print(f"  {text('Removed', '已删除', self.language)} {path}")

    def remove_empty_privileged_dir(self, path: Path) -> None:
        if not exists(path) or path.is_symlink() or not is_directory(path):
            return
        if self.dry_run:
            return
        result = self.run_privileged(["rmdir", "--", str(path)], tolerate=True)
        if result and result.returncode == 0:
            self.changed = True

    def clean_config(self) -> None:
        self.remove_user_file(self.config_path)
        self.remove_user_file(self.config_path.with_name(self.config_path.name + ".draft"))
        if not self.dry_run and self.config_path.parent != Path("/"):
            try:
                self.config_path.parent.rmdir()
            except OSError:
                pass

    def clean_credentials(self) -> None:
        for path in self.credential_paths:
            self.remove_user_file(path)

    def clean_state(self) -> None:
        for path in (self.state_path, self.state_backup, self.state_dir / "setup.log", self.state_dir / "install.lock"):
            self.remove_user_file(path)
        for name in ("runner", "tools"):
            self.remove_user_tree(self.state_dir / name)
        for name in KNOWN_BINARIES:
            self.remove_user_file(self.state_dir / "bin" / name)
        if self.runner_config:
            self.remove_user_file(self.runner_config)
            self.remove_user_file(self.runner_config.parent / ".runner-check")
        for directory in (self.state_dir / "bin", self.state_dir):
            if not self.dry_run and exists(directory) and is_directory(directory):
                try:
                    directory.rmdir()
                except OSError:
                    pass

    def clean_services(self) -> None:
        units = list(UNIT_NAMES)
        if self.runner_service:
            units.append(self.runner_service)
        changed_units = False
        for unit in units:
            unit_path = self.systemd_dir / unit
            dropin = self.systemd_dir / (unit + ".d") / "previewmesh.conf"
            if not exists(unit_path) and not exists(dropin):
                continue
            if not self.dry_run:
                stopped = self.run_privileged(["systemctl", "disable", "--now", unit], tolerate=True)
                if not stopped or stopped.returncode != 0:
                    self.fail(text(f"Could not stop systemd unit: {unit}", f"无法停止 systemd 单元：{unit}", self.language))
                    continue
            self.remove_privileged_file(unit_path)
            self.remove_privileged_file(dropin)
            if exists(dropin.parent):
                self.remove_empty_privileged_dir(dropin.parent)
            changed_units = True

        for path in (
            self.etc_dir / "installation.json",
            self.etc_dir / "ingress.env",
            self.system_lib_dir / "maintain.py",
            self.system_lib_dir / "ingress-route.py",
        ):
            if exists(path):
                self.remove_privileged_file(path)
                changed_units = True
        for directory in (self.etc_dir, self.system_lib_dir):
            self.remove_empty_privileged_dir(directory)
        if changed_units and not self.dry_run:
            self.run_privileged(["systemctl", "daemon-reload"])
        self.clean_firewall()

    def clean_firewall(self) -> None:
        binary = "/usr/sbin/iptables" if exists(Path("/usr/sbin/iptables")) else "iptables"
        if self.dry_run:
            print(f"  {text('Would inspect the managed IPv4 firewall chain PREVIEWMESH-IN.', '将检查带管理标记的 IPv4 防火墙链 PREVIEWMESH-IN。', self.language)}")
            return
        chain = self.run_privileged([binary, "-w", "10", "-S", "PREVIEWMESH-IN"], tolerate=True)
        if not chain or chain.returncode != 0 or not chain.stdout.strip():
            return
        if "previewmesh-managed" not in chain.stdout:
            self.fail(text("PREVIEWMESH-IN exists without the PreviewMesh marker; it was left untouched.",
                           "PREVIEWMESH-IN 存在但没有 PreviewMesh 管理标记，已保持不变。", self.language))
            return
        input_rules = self.run_privileged([binary, "-w", "10", "-S", "INPUT"], tolerate=True)
        if input_rules and input_rules.returncode == 0:
            for line in input_rules.stdout.splitlines():
                try:
                    tokens = shlex.split(line)
                except ValueError:
                    continue
                if len(tokens) >= 4 and tokens[:2] == ["-A", "INPUT"] and tokens[-2:] == ["-j", "PREVIEWMESH-IN"]:
                    self.run_privileged([binary, "-w", "10", "-D", "INPUT", *tokens[2:]], tolerate=True)
        self.run_privileged([binary, "-w", "10", "-F", "PREVIEWMESH-IN"])
        self.run_privileged([binary, "-w", "10", "-X", "PREVIEWMESH-IN"])
        self.changed = True
        print(f"  {text('Removed managed PREVIEWMESH-IN firewall chain.', '已删除带管理标记的 PREVIEWMESH-IN 防火墙链。', self.language)}")

    def clean_runner(self) -> None:
        if not self.control or not self.agent_id:
            self.fail(text("Could not identify a GitHub Runner and its repository from local metadata.",
                           "无法从本机元数据识别 GitHub Runner 及其所属仓库。", self.language))
            return
        if self.dry_run:
            print(f"  {text('Would delete', '将删除', self.language)} GitHub runner {self.control} (id {self.agent_id})")
            return
        if not self.args_yes and sys.stdin.isatty():
            prompt = "Type REMOVE to delete this remote Runner registration: " if self.language == "en" else "请输入 REMOVE 以删除这个远端 Runner 登记："
            if input(prompt).strip() != "REMOVE":
                self.fail(text("Remote Runner removal was cancelled.", "已取消删除远端 Runner。", self.language))
                return
        if not shutil.which("gh"):
            self.fail(text("gh CLI is required to remove the remote Runner; local files were kept.",
                           "删除远端 Runner 需要 gh CLI；本机文件已保留。", self.language))
            return
        try:
            result = subprocess.run(
                ["gh", "api", "--method", "DELETE", f"repos/{self.control}/actions/runners/{self.agent_id}"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            self.fail(text("The GitHub Runner deletion request could not be completed.", "无法完成 GitHub Runner 删除请求。", self.language))
            return
        if result.returncode:
            self.fail(text("GitHub did not remove the Runner; local registration was kept.",
                           "GitHub 未删除 Runner，本机登记已保留。", self.language))
            return
        self.changed = True
        print(f"  {text('Removed remote GitHub Runner registration.', '已删除 GitHub 远端 Runner 登记。', self.language)}")

    def kube_client(self) -> list[str] | None:
        binary = shutil.which("k3s") or ("/usr/local/bin/k3s" if exists(Path("/usr/local/bin/k3s")) else None)
        return [binary, "kubectl"] if binary else None

    def clean_cluster(self) -> None:
        client = self.kube_client()
        if not client:
            self.fail(text("k3s was not found; Kubernetes resources were not changed.",
                           "找不到 k3s，未修改 Kubernetes 资源。", self.language))
            return

        system_ns = self.run_privileged([*client, "get", "namespace", "previewmesh-system", "--ignore-not-found", "-o", "name"], tolerate=True)
        managed = self.run_privileged([*client, "get", "namespaces", "-l", "previewmesh.local/managed-by=previewmesh", "-o", "name"], tolerate=True)
        role = self.run_privileged([*client, "get", "clusterrole", "previewmesh-runner", "--ignore-not-found", "-o", "name"], tolerate=True)
        binding = self.run_privileged([*client, "get", "clusterrolebinding", "previewmesh-runner", "--ignore-not-found", "-o", "name"], tolerate=True)
        results = (system_ns, managed, role, binding)
        if any(result is None for result in results):
            return
        if any(result.returncode != 0 for result in results):
            self.fail(text("Could not inspect the Kubernetes API; no cluster resources were changed.",
                           "无法检查 Kubernetes API，未修改集群资源。", self.language))
            return
        names: list[str] = []
        if system_ns.returncode == 0 and system_ns.stdout.strip() == "namespace/previewmesh-system":
            names.append("previewmesh-system")
        if managed.returncode == 0:
            for item in managed.stdout.splitlines():
                item = item.strip()
                if item.startswith("namespace/"):
                    name = item.removeprefix("namespace/")
                    if re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name) and name not in names:
                        names.append(name)
        objects = []
        if role.returncode == 0 and role.stdout.strip() == "clusterrole/previewmesh-runner":
            objects.append("clusterrole/previewmesh-runner")
        if binding.returncode == 0 and binding.stdout.strip() == "clusterrolebinding/previewmesh-runner":
            objects.append("clusterrolebinding/previewmesh-runner")
        if not names and not objects:
            print(text("  No matching Kubernetes resources detected.", "  未检测到匹配的 Kubernetes 资源。", self.language))
            return
        if self.dry_run:
            for name in names:
                print(f"  {text('Would delete namespace', '将删除命名空间', self.language)} {name}")
            for item in objects:
                print(f"  {text('Would delete', '将删除', self.language)} {item}")
            return
        for name in names:
            self.run_privileged([*client, "delete", "namespace", name, "--ignore-not-found", "--wait=true", "--timeout=120s"])
            remaining = self.run_privileged([*client, "get", "namespace", name, "--ignore-not-found", "-o", "name"], tolerate=True)
            if remaining and remaining.returncode == 0 and remaining.stdout.strip():
                self.fail(text(f"Namespace remains after deletion: {name}", f"删除后命名空间仍存在：{name}", self.language))
        for item in objects:
            kind, name = item.split("/", 1)
            self.run_privileged([*client, "delete", kind, name, "--ignore-not-found"])
        self.changed = True
        print(text("  Removed matching PreviewMesh Kubernetes resources.", "  已删除匹配的 PreviewMesh Kubernetes 资源。", self.language))

    def run(self, selected: set[str], args_yes: bool) -> int:
        self.args_yes = args_yes
        actions = {
            "runner": self.clean_runner,
            "services": self.clean_services,
            "cluster": self.clean_cluster,
            "state": self.clean_state,
            "credentials": self.clean_credentials,
            "config": self.clean_config,
        }
        # Deregister the remote Runner before deleting the local metadata used to find it.
        for key in ("runner", "services", "cluster", "state", "credentials", "config"):
            if key in selected:
                print(f"\n{next(choice.title(self.language) for choice in self.choices() if choice.key == key)}")
                actions[key]()
        if self.failures:
            print(text("Cleanup finished with errors; review the messages above before reinstalling.",
                       "清理完成但有错误；重新安装前请先处理上面的提示。", self.language), file=sys.stderr)
            return 1
        print(text("Cleanup finished. You can now run setup.sh init, then setup.sh install.",
                   "清理完成。现在可以运行 setup.sh init，然后运行 setup.sh install。", self.language))
        return 0


def choose_language(requested: str | None) -> str:
    if requested:
        return requested
    if not sys.stdin.isatty():
        return "en"
    print("Select language / 请选择语言:\n  1. English\n  2. 中文")
    while True:
        try:
            value = input("Choice / 请选择 (1/2, q to exit / 退出): ").strip().lower()
        except EOFError:
            raise SystemExit(130)
        if value in ("1", "en", "english"):
            return "en"
        if value in ("2", "zh", "zh-cn", "中文"):
            return "zh-CN"
        if value == "q":
            raise SystemExit(130)
        print("Invalid choice; enter 1 or 2 / 选择无效，请输入 1 或 2。")


def select_choices(cleaner: Cleaner, choices: list[Choice], requested: str | None, all_items: bool) -> set[str]:
    keys = {choice.key for choice in choices}
    if all_items:
        return keys
    if requested is not None:
        selected: set[str] = set()
        for raw in requested.split(","):
            value = raw.strip().lower()
            if not value:
                continue
            if value.isdigit() and 1 <= int(value) <= len(choices):
                selected.add(choices[int(value) - 1].key)
            elif value in keys:
                selected.add(value)
            else:
                raise ValueError(f"Unknown cleanup item: {raw}")
        return selected
    if not sys.stdin.isatty():
        raise ValueError("Non-interactive cleanup requires --select or --all, and usually --yes.")

    selected: set[str] = set()
    while True:
        print("\n" + text("Select traces to clean (numbers toggle; c continues):",
                           "选择要清理的痕迹（输入数字切换，c 继续）：", cleaner.language))
        for index, choice in enumerate(choices, 1):
            marker = "x" if choice.key in selected else " "
            status = (text("detected", "已检测到", cleaner.language)
                      if cleaner.available(choice.key)
                      else text("not detected", "未检测到", cleaner.language))
            print(f"  [{marker}] {index}. {choice.title(cleaner.language)} ({status})")
            cleaner.describe(choice)
        command = input(text("Toggle number, a=all, n=none, c=continue, q=quit: ",
                             "输入数字切换，a=全选，n=清空，c=继续，q=退出：", cleaner.language)).strip().lower()
        if command == "c":
            return selected
        if command == "a":
            selected = set(keys)
            continue
        if command == "n":
            selected.clear()
            continue
        if command == "q":
            raise SystemExit(130)
        if command.isdigit() and 1 <= int(command) <= len(choices):
            key = choices[int(command) - 1].key
            if key in selected:
                selected.remove(key)
            else:
                selected.add(key)
        else:
            print(text("Enter a listed number or command.", "请输入列表中的数字或命令。", cleaner.language))


def confirm(cleaner: Cleaner, selected: set[str], choices: list[Choice], args: argparse.Namespace) -> bool:
    if not selected:
        print(text("No items selected; nothing was changed.", "没有选择项目，未做任何修改。", cleaner.language))
        return False
    print("\n" + text("Selected cleanup:", "已选择清理：", cleaner.language))
    for choice in choices:
        if choice.key in selected:
            print(f"  - {choice.title(cleaner.language)}")
            warning = choice.warning(cleaner.language)
            if warning:
                print(f"    ! {warning}")
            if choice.key == "state" and cleaner.configured_control and cleaner.state.get("control") != cleaner.configured_control:
                print("    ! " + text(
                    "install-state.json belongs to a different deployment-management repository than setup.ini; this ownership mismatch makes install stop safely. Removing the local state starts a new local record, but does not undo old remote resources.",
                    "install-state.json 所属的部署管理仓库与 setup.ini 不同；这个归属冲突会让 install 安全停止。删除本机状态只会开始新的本地记录，不会撤销旧的远端资源。",
                    cleaner.language,
                ))
    print(text("Token files are removed only when the credentials item is selected; repositories, K3s itself and unrelated resources are not selected by these items.",
               "只有选择凭据项目时才会删除 Token 文件；这些项目不会删除仓库、K3s 本体或无关资源。", cleaner.language))
    if cleaner.dry_run:
        return True
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError("Non-interactive cleanup requires --yes after --select or --all.")
    answer = input(text("Type YES to continue: ", "请输入 YES 继续：", cleaner.language)).strip()
    return answer == "YES"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Choose PreviewMesh installation traces to remove / 选择要清理的 PreviewMesh 安装痕迹")
    parser.add_argument("command", nargs="?", choices=("cleanup",), default="cleanup")
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/previewmesh/setup.ini",
                        help="Active setup.ini path / 活动 setup.ini 路径")
    parser.add_argument("--language", choices=("en", "zh-CN"), help="Prompt language / 提示语言")
    parser.add_argument("--select", metavar="ITEMS", help="Comma-separated item numbers or names: config,credentials,state,services,runner,cluster")
    parser.add_argument("--all", action="store_true", help="Select all cleanup items; use --yes for non-interactive use")
    parser.add_argument("--yes", action="store_true", help="Skip the final confirmation")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without changing files or services")
    args = parser.parse_args(argv)
    try:
        language = choose_language(args.language)
        cleaner = Cleaner(args.config, language, args.dry_run)
        choices = cleaner.choices()
        selected = select_choices(cleaner, choices, args.select, args.all)
        if not confirm(cleaner, selected, choices, args):
            return 0
        return cleaner.run(selected, args.yes)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(text("\nStopped.", "\n已停止。", locals().get("language", "en")), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
