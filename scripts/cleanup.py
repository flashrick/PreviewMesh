#!/usr/bin/env python3
"""Safely remove PreviewMesh installation traces before a fresh setup.

The cleanup levels are cumulative but keep remote resources and K3s opt-in.
They remove only paths and resources that PreviewMesh creates; credentials,
remote Runner registrations, Kubernetes resources and repositories are kept
unless their separate choices are explicitly selected.
"""
from __future__ import annotations

import argparse
import configparser
import fcntl
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
PUBLIC_ROOT = Path(__file__).resolve().parents[1]
CLEANUP_LEVELS = {
    "1": ("config", "state"),
    "2": ("config", "state", "services", "runner-files"),
    "3": ("config", "state", "services", "runner-files", "repositories"),
}
CLEANUP_LEVEL_ALIASES = {
    "1": "1",
    "settings": "1",
    "setting": "1",
    "local-settings": "1",
    "2": "2",
    "runtime": "2",
    "services": "2",
    "3": "3",
    "repositories": "3",
    "repos": "3",
}


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
        self.state = json_file(self.state_path)
        self.runner_dir = self._runner_directory()
        self.runner = json_file(self.runner_dir / ".runner")
        self.runner_dirs = self._runner_directories()
        self.runner_entries = self._runner_entries()
        self.systemd_dir = Path("/etc/systemd/system")
        self.etc_dir = Path("/etc/previewmesh")
        self.system_lib_dir = Path("/usr/local/lib/previewmesh")
        self.configured_control = self._configured_control_repository()
        self.credential_paths = self._credential_paths()
        self.control = self._control_repository()
        self.control_dir = self._control_directory()
        self.repository_repositories: dict[Path, str] = {}
        self.repository_dirs = self._repository_directories()
        self.runner_config = self._runner_config_path()
        self.runner_services = self._runner_service_names()
        self.runner_service = self.runner_services[0] if self.runner_services else None
        self.agent_id = self._agent_id()
        self.failures: list[str] = []
        self.changed = False
        self.selected: set[str] = set()
        self.args_yes = False
        self.confirm_remote_runner = False
        self.allow_dirty_repositories = False
        self.runner_cleanup_complete = True
        self.services_cleanup_complete = True
        self.cleanup_lock = None
        self.cleanup_lock_created = False

    def _runner_directory(self) -> Path:
        """Limit state-selected Runner paths to direct children of PreviewMesh state."""
        value = self.state.get("runner_directory")
        if isinstance(value, str):
            candidate = absolute(Path(value))
            if (candidate.parent == self.state_dir
                    and (candidate.name == "runner" or candidate.name.startswith("runner-"))
                    and not candidate.is_symlink()):
                return candidate
        return self.state_dir / "runner"

    def _runner_directories(self) -> list[Path]:
        """Find only direct, user-owned Runner directories under the state directory."""
        candidates = [self.runner_dir]
        try:
            children = sorted(self.state_dir.iterdir()) if is_directory(self.state_dir) else []
        except OSError:
            children = []
        for candidate in children:
            if (candidate.name == "runner" or candidate.name.startswith("runner-")) and candidate not in candidates:
                if not candidate.is_symlink() and is_directory(candidate):
                    candidates.append(candidate)
        return candidates

    def _runner_entries(self) -> list[dict[str, object]]:
        """Read every local Runner registration so a previous control is not orphaned."""
        entries = []
        for directory in self.runner_dirs:
            marker = json_file(directory / ".runner")
            value = marker.get("gitHubUrl")
            match = re.fullmatch(r"https?://github\.com/([^/]+/[^/]+)/?", value or "", re.IGNORECASE)
            agent = marker.get("agentId")
            if isinstance(agent, int) and agent > 0:
                agent = str(agent)
            if not isinstance(agent, str) or not re.fullmatch(r"[1-9][0-9]*", agent):
                continue
            if not match or not valid_repo(match.group(1)):
                continue
            entries.append({
                "directory": directory,
                "control": match.group(1),
                "agent_id": agent,
                "name": marker.get("agentName") if isinstance(marker.get("agentName"), str) else None,
                "service": self._service_file_value(directory),
            })
        return entries

    @staticmethod
    def _service_file_value(directory: Path) -> str | None:
        try:
            value = (directory / ".service").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        return value if valid_service(value) else None

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

    def _repository_directories(self) -> list[Path]:
        """Resolve configured control/source checkouts before config cleanup removes their paths."""
        candidates: list[Path] = []

        def add(value: object, repository: object = None) -> None:
            if isinstance(value, Path):
                path = value
            elif isinstance(value, str) and value and not any(char in value for char in "\0\r\n"):
                path = Path(value).expanduser()
            else:
                return
            if not path.is_absolute():
                path = self.config_path.parent / path
            path = absolute(path)
            if path not in candidates:
                candidates.append(path)
            if valid_repo(repository):
                self.repository_repositories[path] = repository

        add(self.control_dir, self.configured_control)
        if exists(self.config_path) and not self.config_path.is_symlink() and self.config_path.is_file():
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(self.config_path, encoding="utf-8")
                for section in parser.sections():
                    if section.startswith("source:"):
                        add(parser.get(section, "directory", fallback=""),
                            parser.get(section, "repository", fallback=""))
            except (OSError, configparser.Error, UnicodeError):
                pass
        return candidates

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

    def _runner_service_names(self) -> list[str]:
        """Recover all Runner units, including registrations left by an older control."""
        names: set[str] = set()
        value = self.state.get("runner_service")
        if valid_service(value):
            names.add(value)
        for entry in self.runner_entries:
            value = entry.get("service")
            if valid_service(value):
                names.add(value)
        try:
            unit_files = sorted(self.systemd_dir.glob("actions.runner.*.service"))
        except OSError:
            unit_files = []
        for unit_path in unit_files:
            service = unit_path.name
            if not valid_service(service) or unit_path.is_symlink() or not unit_path.is_file():
                continue
            try:
                content = unit_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if any(str(directory) in content for directory in self.runner_dirs):
                names.add(service)
        # Recover stale services after state.json was removed in an earlier run.
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
                names.add(service)
        return sorted(names)

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
                "Installer ownership and checkpoint state",
                "安装器归属和断点状态",
                "Removes install-state.json, its backup, the resume log and completed install lock. Runner files, tool caches and services are kept so a running installation is not broken.",
                "删除 install-state.json、备份、恢复日志和已完成的安装锁。保留 Runner 文件、工具缓存和服务，避免正在运行的安装被破坏。",
                "This removes resume evidence but does not deregister a remote Runner or delete runtime files.",
                "这会删除恢复证据，但不会注销远端 Runner 或删除运行时文件。",
            ),
            Choice(
                "runner-files",
                "Local Runner files, tool cache and generated Runner kubeconfig",
                "本机 Runner 文件、工具缓存和生成的 Runner kubeconfig",
                "Removes local Runner registrations, Runner directories, installer-managed tools, links and generated Runner kubeconfig files. The Runner service must be removed or stopped first.",
                "删除本机 Runner 登记、Runner 目录、安装器管理的工具、链接和生成的 Runner kubeconfig。必须先删除或停止 Runner 服务。",
                "This does not remove a GitHub Runner registration; use the separate remote Runner item only when the remote resource may be deleted.",
                "这不会删除 GitHub 远端 Runner 登记；只有允许删除远端资源时才选择独立的远端 Runner 项目。",
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
                "repositories",
                "Local control and source Git checkouts",
                "本地 control 和 source Git checkout",
                "Removes only the configured local deployment-management and application Git checkouts. The public PreviewMesh checkout, GitHub repositories, remote resources and unconfigured directories are protected.",
                "只删除配置中指定的本地部署管理仓库和应用源码 Git checkout。公开 PreviewMesh checkout、GitHub 仓库、远端资源和未配置目录都会受到保护。",
                "This is irreversible for local files. Repositories with uncommitted, untracked or ignored files require --allow-dirty-repositories and a separate DELETE-LOCAL-REPOSITORIES confirmation.",
                "本地文件无法恢复。含未提交、未跟踪或被忽略文件的仓库必须显式使用 --allow-dirty-repositories，并额外输入 DELETE-LOCAL-REPOSITORIES 确认。",
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
            return [str(path) for path in paths if exists(path)]
        if key == "runner-files":
            paths = list(self.runner_dirs)
            paths.append(self.state_dir / "tools")
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
            for service in self.runner_services:
                paths.extend((self.systemd_dir / service,
                              self.systemd_dir / (service + ".d") / "previewmesh.conf"))
            return [str(path) for path in paths if exists(path)]
        if key == "runner":
            return [f"GitHub runner {entry['control']} (id {entry['agent_id']})" for entry in self.runner_entries]
        if key == "repositories":
            return [str(path) for path in self.repository_dirs if exists(path)]
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

    def acquire_cleanup_lock(self) -> bool:
        """Serialize cleanup with install without leaving a lock in dry-run mode."""
        path = self.state_dir / "install.lock"
        try:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                self.fail(text(f"Refusing unsafe install lock: {path}",
                               f"拒绝处理不安全的安装锁：{path}", self.language))
                return False
            if self.dry_run and not path.exists():
                return True
            was_present = path.exists()
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = path.open("a+")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                self.fail(text(
                    "An installer or cleanup is already running; no changes were made.",
                    "安装器或清理程序正在运行，本次未做任何修改。",
                    self.language,
                ))
                return False
            self.cleanup_lock = lock
            self.cleanup_lock_created = not was_present
            return True
        except OSError:
            self.fail(text(
                f"Could not acquire the PreviewMesh cleanup lock: {path}",
                f"无法获取 PreviewMesh 清理锁：{path}",
                self.language,
            ))
            return False

    def release_cleanup_lock(self) -> None:
        if self.cleanup_lock is None:
            return
        path = self.state_dir / "install.lock"
        try:
            fcntl.flock(self.cleanup_lock, fcntl.LOCK_UN)
        finally:
            self.cleanup_lock.close()
            self.cleanup_lock = None
        if self.cleanup_lock_created and exists(path):
            try:
                path.unlink()
            except OSError:
                self.fail(text(
                    f"Could not remove the temporary cleanup lock: {path}",
                    f"无法删除临时清理锁：{path}",
                    self.language,
                ))

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

    def remove_install_lock(self) -> bool:
        """Never remove a lock held by another installer process."""
        path = self.state_dir / "install.lock"
        if not exists(path):
            return True
        if self.cleanup_lock is not None:
            self.remove_user_file(path)
            return not self.failures
        if path.is_symlink() or not path.is_file():
            self.remove_user_file(path)
            return not self.failures
        try:
            with path.open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.fail(text(
                        "An installer is running; install.lock was kept.",
                        "安装器正在运行，已保留 install.lock。",
                        self.language,
                    ))
                    return False
                fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            self.fail(text(
                f"Could not inspect install.lock: {path}",
                f"无法检查 install.lock：{path}",
                self.language,
            ))
            return False
        self.remove_user_file(path)
        return not self.failures

    def clean_state(self) -> None:
        if not self.remove_install_lock():
            return
        for path in (self.state_path, self.state_backup, self.state_dir / "setup.log"):
            self.remove_user_file(path)

    def clean_runner_files(self) -> None:
        """Remove local runtime files only after their systemd units are gone."""
        if self.runner_services and "services" not in self.selected:
            self.fail(text(
                "Runner services are still registered; select services before removing Runner files.",
                "Runner 服务仍已登记；删除 Runner 文件前请先选择 services。",
                self.language,
            ))
            return
        if not self.services_cleanup_complete:
            self.fail(text(
                "A PreviewMesh service could not be stopped; Runner files were kept.",
                "PreviewMesh 服务未能停止，已保留 Runner 文件。",
                self.language,
            ))
            return
        if "runner" in self.selected and not self.runner_cleanup_complete:
            self.fail(text(
                "Remote Runner cleanup did not complete; local Runner files were kept.",
                "远端 Runner 清理未完成，已保留本机 Runner 文件。",
                self.language,
            ))
            return
        for path in self.runner_dirs:
            self.remove_user_tree(path)
        self.remove_user_tree(self.state_dir / "tools")
        for name in KNOWN_BINARIES:
            self.remove_user_file(self.state_dir / "bin" / name)
        if self.runner_config:
            self.remove_user_file(self.runner_config)
            self.remove_user_file(self.runner_config.parent / ".runner-check")
        for directory in (self.state_dir / "bin", self.state_dir / "tools"):
            if not self.dry_run and exists(directory) and is_directory(directory):
                try:
                    directory.rmdir()
                except OSError:
                    pass

    def clean_services(self) -> None:
        units = list(UNIT_NAMES)
        units.extend(self.runner_services)
        changed_units = False
        self.services_cleanup_complete = True
        for unit in units:
            unit_path = self.systemd_dir / unit
            dropin = self.systemd_dir / (unit + ".d") / "previewmesh.conf"
            if not exists(unit_path) and not exists(dropin):
                continue
            if not self.dry_run:
                stopped = self.run_privileged(["systemctl", "disable", "--now", unit], tolerate=True)
                if not stopped or stopped.returncode != 0:
                    self.fail(text(f"Could not stop systemd unit: {unit}", f"无法停止 systemd 单元：{unit}", self.language))
                    self.services_cleanup_complete = False
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
        self.clean_windows_lan()

    def clean_windows_lan(self) -> None:
        """Remove only PreviewMesh's WSL forwarding resources when running in WSL."""
        distro = os.environ.get("WSL_DISTRO_NAME", "").strip()
        if not distro:
            return
        powershell = shutil.which("powershell.exe")
        wslpath = shutil.which("wslpath")
        script = PUBLIC_ROOT / "ops" / "wsl" / "setup-lan.ps1"
        if not powershell or not wslpath or not exists(script):
            self.fail(text(
                "WSL cleanup requires powershell.exe, wslpath and the PreviewMesh LAN helper.",
                "WSL 清理需要 powershell.exe、wslpath 和 PreviewMesh 局域网辅助脚本。",
                self.language,
            ))
            self.services_cleanup_complete = False
            return
        try:
            converted = subprocess.run([wslpath, "-w", str(script)], capture_output=True,
                                       text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            converted = None
        if not converted or converted.returncode != 0 or not converted.stdout.strip():
            self.fail(text("Could not resolve the Windows PreviewMesh cleanup script.",
                           "无法解析 Windows 侧的 PreviewMesh 清理脚本。", self.language))
            self.services_cleanup_complete = False
            return
        windows_script = converted.stdout.strip()
        if self.dry_run:
            print(text(
                f"  Would remove PreviewMesh Windows LAN forwarding for WSL distro {distro}.",
                f"  将删除 WSL 发行版 {distro} 对应的 PreviewMesh Windows 局域网转发。",
                self.language,
            ))
            return
        try:
            result = subprocess.run(
                [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", windows_script,
                 "-Mode", "Remove", "-Distro", distro],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if not result or result.returncode != 0:
            self.fail(text(
                "Windows PreviewMesh LAN resources were not removed; review the administrator prompt.",
                "Windows 侧 PreviewMesh 局域网资源未删除，请检查管理员权限提示。",
                self.language,
            ))
            self.services_cleanup_complete = False

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
        if not self.runner_entries:
            print(text("  No local GitHub Runner registration detected.", "  未检测到本机 GitHub Runner 登记。", self.language))
            return
        if self.dry_run:
            for entry in self.runner_entries:
                print(f"  {text('Would delete', '将删除', self.language)} GitHub runner {entry['control']} (id {entry['agent_id']})")
            return
        if self.args_yes and not self.confirm_remote_runner:
            self.fail(text(
                "Remote Runner deletion with --yes also requires --confirm-remote-runner.",
                "使用 --yes 删除远端 Runner 时还必须指定 --confirm-remote-runner。",
                self.language,
            ))
            self.runner_cleanup_complete = False
            return
        if not self.args_yes and sys.stdin.isatty():
            prompt = "Type REMOVE to delete these remote Runner registrations: " if self.language == "en" else "请输入 REMOVE 以删除这些远端 Runner 登记："
            if input(prompt).strip() != "REMOVE":
                self.fail(text("Remote Runner removal was cancelled.", "已取消删除远端 Runner。", self.language))
                self.runner_cleanup_complete = False
                return
        if not shutil.which("gh"):
            self.fail(text("gh CLI is required to remove the remote Runner; local files were kept.",
                           "删除远端 Runner 需要 gh CLI；本机文件已保留。", self.language))
            self.runner_cleanup_complete = False
            return
        for entry in self.runner_entries:
            try:
                details = subprocess.run(
                    ["gh", "api", f"repos/{entry['control']}/actions/runners/{entry['agent_id']}"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired):
                self.fail(text("The GitHub Runner status request could not be completed.", "无法获取 GitHub Runner 状态。", self.language))
                self.runner_cleanup_complete = False
                continue
            if details.returncode:
                self.fail(text(
                    f"Could not verify Runner {entry['control']} (id {entry['agent_id']}); local registration was kept.",
                    f"无法验证 Runner {entry['control']}（id {entry['agent_id']}），本机登记已保留。",
                    self.language,
                ))
                self.runner_cleanup_complete = False
                continue
            try:
                remote = json.loads(details.stdout)
            except (TypeError, ValueError):
                remote = None
            if not isinstance(remote, dict):
                self.fail(text(
                    f"GitHub returned invalid details for Runner {entry['control']} (id {entry['agent_id']}).",
                    f"GitHub 返回的 Runner {entry['control']}（id {entry['agent_id']}）信息无效。",
                    self.language,
                ))
                self.runner_cleanup_complete = False
                continue
            if remote.get("busy") is True:
                self.fail(text(
                    f"Runner {entry['control']} (id {entry['agent_id']}) is busy; remote registration was kept.",
                    f"Runner {entry['control']}（id {entry['agent_id']}）正在执行任务，已保留远端登记。",
                    self.language,
                ))
                self.runner_cleanup_complete = False
                continue
            if entry.get("name") and remote.get("name") != entry["name"]:
                self.fail(text(
                    f"Runner identity changed for {entry['control']} (id {entry['agent_id']}); remote registration was kept.",
                    f"Runner {entry['control']}（id {entry['agent_id']}）的身份已变化，已保留远端登记。",
                    self.language,
                ))
                self.runner_cleanup_complete = False
                continue
            try:
                result = subprocess.run(
                    ["gh", "api", "--method", "DELETE",
                     f"repos/{entry['control']}/actions/runners/{entry['agent_id']}"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired):
                self.fail(text("The GitHub Runner deletion request could not be completed.", "无法完成 GitHub Runner 删除请求。", self.language))
                self.runner_cleanup_complete = False
                continue
            if result.returncode:
                self.fail(text(
                    f"GitHub did not remove Runner {entry['control']} (id {entry['agent_id']}); local registration was kept.",
                    f"GitHub 未删除 Runner {entry['control']}（id {entry['agent_id']}），本机登记已保留。",
                    self.language,
                ))
                self.runner_cleanup_complete = False
                continue
            self.changed = True
            print(f"  {text('Removed remote GitHub Runner registration.', '已删除 GitHub 远端 Runner 登记。')} "
                  f"{entry['control']} (id {entry['agent_id']})")

    def _repository_check(self, path: Path) -> bool:
        """Validate a configured checkout before any recursive deletion."""
        if path.is_symlink() or not is_directory(path):
            self.fail(text(f"Refusing non-directory repository path: {path}",
                           f"拒绝处理非目录仓库路径：{path}", self.language))
            return False
        if path == Path("/") or path == self.home or path == self.state_dir:
            self.fail(text(f"Refusing broad repository path: {path}",
                           f"拒绝处理范围过大的仓库路径：{path}", self.language))
            return False
        if path == PUBLIC_ROOT or path in PUBLIC_ROOT.parents or PUBLIC_ROOT in path.parents:
            self.fail(text(f"Refusing to delete the public PreviewMesh checkout: {path}",
                           f"拒绝删除公开 PreviewMesh checkout：{path}", self.language))
            return False
        git_metadata = path / ".git"
        if not git_metadata.is_dir():
            self.fail(text(f"Refusing path without a standalone Git checkout: {path}",
                           f"拒绝处理没有独立 Git checkout 的路径：{path}", self.language))
            return False
        try:
            root = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if root.returncode != 0 or absolute(Path(root.stdout.strip())) != path:
                self.fail(text(f"Refusing path that is not a Git checkout root: {path}",
                               f"拒绝处理不是 Git checkout 根目录的路径：{path}", self.language))
                return False
            expected = self.repository_repositories.get(path)
            origin = subprocess.run(
                ["git", "-C", str(path), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            remote = origin.stdout.strip().rstrip("/")
            match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$", remote, re.IGNORECASE)
            if origin.returncode != 0 or not match or not expected or match.group(1).lower() != expected.lower():
                self.fail(text(
                    f"Refusing checkout whose origin does not match the configured repository: {path}",
                    f"拒绝处理 origin 与配置仓库不匹配的 checkout：{path}",
                    self.language,
                ))
                return False
            status = subprocess.run(
                ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all", "--ignored=matching"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            self.fail(text(f"Could not inspect Git checkout: {path}",
                           f"无法检查 Git checkout：{path}", self.language))
            return False
        if status.returncode != 0:
            self.fail(text(f"Could not inspect Git checkout: {path}",
                           f"无法检查 Git checkout：{path}", self.language))
            return False
        if status.stdout.strip() and not self.allow_dirty_repositories:
            self.fail(text(
                f"Git checkout has uncommitted, untracked or ignored files; use --allow-dirty-repositories only after review: {path}",
                f"Git checkout 含有未提交、未跟踪或被忽略文件；确认后才可使用 --allow-dirty-repositories：{path}",
                self.language,
            ))
            return False
        return True

    def clean_repositories(self) -> None:
        """Delete only explicitly configured local control/source checkout roots."""
        if self.trace_lines("services") and "services" not in self.selected:
            self.fail(text(
                "PreviewMesh services must be removed before deleting repositories.",
                "删除本地仓库前必须先清除 PreviewMesh 服务。",
                self.language,
            ))
            return
        if not self.services_cleanup_complete:
            self.fail(text(
                "A PreviewMesh service could not be stopped; local repositories were kept.",
                "PreviewMesh 服务未能停止，已保留本地仓库。",
                self.language,
            ))
            return
        if "runner" in self.selected and not self.runner_cleanup_complete:
            self.fail(text(
                "Remote Runner cleanup did not complete; local repositories were kept.",
                "远端 Runner 清理未完成，已保留本地仓库。",
                self.language,
            ))
            return
        paths = [path for path in self.repository_dirs if exists(path)]
        if not paths:
            print(text("  No configured local control/source checkout detected.",
                       "  未检测到配置中的本地 control/source checkout。", self.language))
            return
        unique = []
        for path in paths:
            if path not in unique:
                unique.append(path)
        for index, path in enumerate(unique):
            for other in unique[index + 1:]:
                if path == other or path in other.parents or other in path.parents:
                    self.fail(text(
                        f"Configured repository paths overlap; none were removed: {path} and {other}",
                        f"配置的仓库路径互相重叠，未删除任何仓库：{path} 和 {other}",
                        self.language,
                    ))
                    return
        valid = [path for path in unique if self._repository_check(path)]
        if len(valid) != len(unique):
            return
        if self.runner_entries and "runner" not in self.selected:
            print(text(
                "  ! Remote Runner registrations were kept; the old control may still show an offline Runner.",
                "  ！已保留远端 Runner 登记；旧 control 可能仍显示离线 Runner。",
                self.language,
            ))
        if self.dry_run:
            for path in valid:
                print(f"  {text('Would remove local repository', '将删除本地仓库', self.language)} {path}")
            return
        for path in valid:
            self.remove_user_tree(path)

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
        self.selected = set(selected)
        self.args_yes = args_yes
        if not self.acquire_cleanup_lock():
            return 1
        actions = {
            "runner": self.clean_runner,
            "services": self.clean_services,
            "cluster": self.clean_cluster,
            "runner-files": self.clean_runner_files,
            "state": self.clean_state,
            "credentials": self.clean_credentials,
            "repositories": self.clean_repositories,
            "config": self.clean_config,
        }
        try:
            # Stop local services before removing their files; remote deletion remains opt-in.
            for key in ("runner", "services", "cluster", "runner-files", "credentials", "state", "repositories", "config"):
                if key in selected:
                    print(f"\n{next(choice.title(self.language) for choice in self.choices() if choice.key == key)}")
                    actions[key]()
        finally:
            self.release_cleanup_lock()
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


def level_items(value: str) -> tuple[str, ...]:
    key = CLEANUP_LEVEL_ALIASES.get(value.strip().lower())
    if key is None:
        raise ValueError("Unknown cleanup level; choose 1, 2 or 3 / 清理级别无效，请选择 1、2 或 3。")
    return CLEANUP_LEVELS[key]


def print_level_menu(language: str) -> None:
    print("\n" + text("Choose cleanup level:", "选择清理级别：", language))
    print("  1. " + text(
        "Local settings and installer state only (keeps services, Runner files and repositories)",
        "仅清理本地设置和安装器状态（保留服务、Runner 文件和仓库）",
        language,
    ))
    print("  2. " + text(
        "Level 1 plus local Runner files and PreviewMesh system services (keeps remote Runner and K3s)",
        "级别 1 + 本机 Runner 文件和 PreviewMesh 系统服务（保留远端 Runner 和 K3s）",
        language,
    ))
    print("  3. " + text(
        "Level 2 plus configured local control/source Git checkouts",
        "级别 2 + 配置中的本地 control/source Git checkout",
        language,
    ))
    print("  c. " + text("Custom item selection", "自定义选择项目", language))


def select_choices(cleaner: Cleaner, choices: list[Choice], requested: str | None,
                   all_items: bool, requested_level: str | None,
                   include_credentials: bool, remote_runner: bool, cluster: bool) -> set[str]:
    keys = {choice.key for choice in choices}
    if requested_level is not None:
        selected = set(level_items(requested_level))
    elif all_items:
        # Keep the historical --all useful without silently deleting remote or
        # cluster resources; those actions require their named opt-in flags.
        selected = keys - {"runner", "cluster", "repositories"}
    elif requested is not None:
        selected = set()
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
    else:
        if not sys.stdin.isatty():
            raise ValueError("Non-interactive cleanup requires --level, --select or --all, and usually --yes.")
        while True:
            print_level_menu(cleaner.language)
            command = input(text("Choose 1/2/3, c=custom, q=quit: ", "请选择 1/2/3，c=自定义，q=退出：", cleaner.language)).strip().lower()
            if command == "q":
                raise SystemExit(130)
            if command in ("1", "2", "3"):
                selected = set(CLEANUP_LEVELS[command])
                break
            if command == "c":
                break
            print(text("Enter 1, 2, 3 or c.", "请输入 1、2、3 或 c。", cleaner.language))
        if command != "c":
            if include_credentials:
                selected.add("credentials")
            if remote_runner:
                selected.add("runner")
            if cluster:
                selected.add("cluster")
            return selected

        selected = set()
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
                break
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
    if include_credentials:
        selected.add("credentials")
    if remote_runner:
        selected.add("runner")
    if cluster:
        selected.add("cluster")
    return selected


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
    print(text(
        "The three levels keep K3s, GitHub repositories and remote resources. Token files, remote Runner registrations and Kubernetes resources are separate opt-in items.",
        "三档清理都会保留 K3s、GitHub 仓库和远端资源。Token 文件、远端 Runner 登记和 Kubernetes 资源都是独立的可选项目。",
        cleaner.language,
    ))
    if "runner-files" in selected and cleaner.runner_entries and "runner" not in selected:
        print(text(
            "! Local Runner files will be removed while remote Runner registrations are kept; those remote Runners will become offline.",
            "！将删除本机 Runner 文件但保留远端 Runner 登记；这些远端 Runner 会变为离线。",
            cleaner.language,
        ))
    if "repositories" in selected and cleaner.runner_entries and "runner" not in selected:
        print(text(
            "! Local repositories will be removed while remote Runner registrations are kept.",
            "！将删除本地仓库但保留远端 Runner 登记。",
            cleaner.language,
        ))
    if cleaner.dry_run:
        return True
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError("Non-interactive cleanup requires --yes after --select or --all.")
    answer = input(text("Type YES to continue: ", "请输入 YES 继续：", cleaner.language)).strip()
    if answer != "YES":
        return False
    if "repositories" in selected:
        # A clean checkout still needs an explicit confirmation because the entire
        # directory, not only PreviewMesh-generated files, will be removed.
        answer = input(text(
            "Type DELETE-LOCAL-REPOSITORIES to remove the local checkouts: ",
            "请输入 DELETE-LOCAL-REPOSITORIES 以删除本地 checkout：",
            cleaner.language,
        )).strip()
        if answer != "DELETE-LOCAL-REPOSITORIES":
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Choose PreviewMesh installation traces to remove / 选择要清理的 PreviewMesh 安装痕迹")
    parser.add_argument("command", nargs="?", choices=("cleanup",), default="cleanup")
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/previewmesh/setup.ini",
                        help="Active setup.ini path / 活动 setup.ini 路径")
    parser.add_argument("--language", choices=("en", "zh-CN"), help="Prompt language / 提示语言")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--level", "--tier", dest="level", metavar="LEVEL",
                           help="Cumulative cleanup level 1/2/3 (or settings/runtime/repositories)")
    selection.add_argument("--select", metavar="ITEMS",
                           help="Comma-separated item numbers or names; use for advanced independent cleanup")
    selection.add_argument("--all", action="store_true", help="Select all local cleanup items; add named opt-ins for remote/cluster/repository deletion")
    parser.add_argument("--include-credentials", action="store_true",
                        help="Add local token files to a selected level")
    parser.add_argument("--remote-runner", action="store_true",
                        help="Add remote GitHub Runner deregistration to a selected level")
    parser.add_argument("--confirm-remote-runner", action="store_true",
                        help="Allow --yes to delete remote Runner registrations")
    parser.add_argument("--cluster", action="store_true",
                        help="Add PreviewMesh Kubernetes resource deletion to a selected level; K3s itself stays installed")
    parser.add_argument("--allow-dirty-repositories", action="store_true",
                        help="Allow level 3 to remove uncommitted/untracked/ignored local Git checkout files")
    parser.add_argument("--yes", action="store_true", help="Skip the final confirmation")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without changing files or services")
    args = parser.parse_args(argv)
    try:
        language = choose_language(args.language)
        cleaner = Cleaner(args.config, language, args.dry_run)
        cleaner.allow_dirty_repositories = args.allow_dirty_repositories
        cleaner.confirm_remote_runner = args.confirm_remote_runner
        choices = cleaner.choices()
        selected = select_choices(cleaner, choices, args.select, args.all, args.level,
                                  args.include_credentials, args.remote_runner, args.cluster)
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
