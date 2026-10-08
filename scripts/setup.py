#!/usr/bin/env python3
"""Guided PreviewMesh installation. Configuration is data, never executable code."""
import argparse
import base64
import difflib
import fcntl
import getpass
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from setup_config import (ACCESS_GUIDE, SetupError, atomic_write, fingerprint, load_config,
                          merge_registry, read_token)
from setup_downloads import download_archive, fetch, metadata, runner_release, tool_release
from setup_language import choose_language
from setup_resume import Checkpoints, REUSABLE

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path.home() / ".config/previewmesh/setup.ini"
SYSTEM_CONFIG = Path("/etc/previewmesh/installation.json")


class Installer:
    def __init__(self, config, command, verbose=False):
        self.c, self.command, self.verbose = config, command, verbose
        self.zh = config.language == "zh-CN" or (config.language == "auto" and
                   (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower().startswith("zh"))
        self.home = Path.home() / ".local/share/previewmesh"
        self.state_path = self.home / "install-state.json"
        self.log_path = self.home / "setup.log"
        try:
            self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
            if not isinstance(self.state, dict) or any(
                    not isinstance(self.state.get(key, {}), dict)
                    for key in ("stages", "completed", "files", "downloads")):
                raise ValueError()
        except (ValueError, OSError):
            raise SetupError("Installation state is unreadable or damaged. Preserve the damaged file, inspect install-state.json.bak beside it, and restore that file if trusted, then rerun the same command. If no trusted backup exists, recover the ownership records from this machine and deployment management repository before continuing; do not discard them. / 安装状态无法读取或已损坏。请保留损坏文件，检查同目录 install-state.json.bak，确认可信后恢复并重跑同一命令；若无可信备份，请先根据本机及部署管理仓库恢复资源归属记录，不要直接丢弃。")
        owner = self.state.get("control")
        if owner and owner != self.c.control and self.state.get("control_id"):
            raise SetupError("This account already manages another deployment management repository / 本机账号已管理另一个部署管理仓库。")
        self.env = dict(os.environ)
        for key in ("GH_DEBUG", "GIT_TRACE", "GIT_TRACE_PACKET", "GIT_CURL_VERBOSE", "BASH_ENV", "ENV"):
            self.env.pop(key, None)
        self.env["PATH"] = str(self.home / "bin") + os.pathsep + self.env["PATH"]
        self.secrets = []
        self.registrations = []
        self.wsl = "microsoft" in platform.release().lower()
        self.control_id = self.state.get("control_id")
        self.pending = []
        self.source_status = {}
        self.completed_stages = []
        self.stage_results = {}
        self.checkpoints = Checkpoints(self, ROOT)

    def say(self, en, zh):
        print(zh if self.zh else en, flush=True)

    def redact(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[REDACTED]", value)
        # GitHub can return temporary tokens before we know their values.
        return re.sub(r'("(?:token|temp_download_token)"\s*:\s*)"(?:\\.|[^"\\])*"',
                      r'\1"[REDACTED]"', value, flags=re.IGNORECASE)

    def save(self):
        self.state["control"] = self.c.control
        # Keep a private last-known-good copy; never overwrite it with broken JSON.
        if self.state_path.exists():
            previous = self.state_path.read_text()
            if isinstance(json.loads(previous), dict):
                atomic_write(self.state_path.with_suffix(".json.bak"), previous)
        atomic_write(self.state_path, json.dumps(self.state, indent=2) + "\n")

    def run(self, args, *, cwd=None, input=None, env=None, check=True, timeout=300, interactive=False):
        args = [str(arg) for arg in args]
        effective = dict(self.env, **(env or {}))
        try:
            completed = subprocess.run(args, cwd=cwd, input=input, env=effective, text=True,
                                       capture_output=not interactive, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise SetupError(f"Command unavailable or timed out / 命令无法执行或超时: {Path(args[0]).name}")
        output = (completed.stdout or "") + (completed.stderr or "")
        safe = self.redact(output)
        if self.command == "install":
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(self.redact(shlex.join(args)) + "\n" + safe + "\n")
        if self.verbose and safe:
            print(safe, end="" if safe.endswith("\n") else "\n")
        if check and completed.returncode:
            raise SetupError(self.redact(
                f"Command failed / 命令失败: {shlex.join(args)}\n{safe[-1600:]}"))
        return completed

    def tool(self, name):
        return shutil.which(name, path=self.env["PATH"])

    def api(self, path, *, method="GET", data=None):
        args = ["gh", "api", "--method", method, path]
        payload = None
        if data is not None:
            args += ["--input", "-"]
            payload = json.dumps(data)
        result = self.run(args, input=payload)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def all_runners(self):
        result, page = [], 1
        while True:
            batch = self.api(f"repos/{self.c.control}/actions/runners?per_page=100&page={page}")["runners"]
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1

    def step(self, number, en, zh, action, *, detail=None):
        self.say(f"\n[{number}/8] {en}", f"\n[{number}/8] {zh}")
        if detail:
            self.say(*detail)
        inputs = self.checkpoints.inputs(number)
        reuse, reason = self.checkpoints.assess(number, inputs)
        self.stage_results[number] = ("reused" if reuse else "rerun", reason)
        self.say(f"{'Reusing' if reuse else 'Running'}: {reason}",
                 f"{'复用' if reuse else '重新执行'}：{reason}")
        if reuse:
            self.completed_stages.append((number, en, zh))
            return
        # Invalidate before side effects so an interrupted repair cannot retain success.
        self.checkpoints.record(number, "running", inputs)
        action()
        artifacts = self.checkpoints.artifacts(number) if number in REUSABLE else {}
        self.checkpoints.record(number, "complete", self.checkpoints.inputs(number), artifacts)
        self.state.setdefault("completed", {})[str(number)] = int(time.time())
        self.save()
        self.completed_stages.append((number, en, zh))
        self.say("Done.", "完成。")

    def preflight(self):
        system = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                system[key] = value.strip('"')
        if system.get("ID") != "ubuntu" or system.get("VERSION_ID") not in ("22.04", "24.04") or platform.machine() != "x86_64":
            raise SetupError("Supported: Ubuntu 22.04/24.04 x64, including WSL2 / 仅支持 Ubuntu 22.04/24.04 x64（含 WSL2）。")
        if os.getuid() == 0:
            raise SetupError("Run as your normal Linux user; the installer requests sudo when needed / 请使用普通 Linux 用户运行，需要时安装器会请求 sudo。")
        if Path("/proc/1/comm").read_text().strip() != "systemd":
            raise SetupError("systemd is required. WSL: add [boot] systemd=true to /etc/wsl.conf, then run wsl --shutdown in Windows and reopen Ubuntu.\n需要 systemd。WSL：在 /etc/wsl.conf 的 [boot] 段设置 systemd=true，然后在 Windows 执行 wsl --shutdown 并重新打开 Ubuntu。")
        if not self.tool("sudo"):
            raise SetupError("sudo is required. Ask the machine administrator to install sudo and grant your user access.\n需要 sudo，请管理员安装 sudo 并授予当前用户使用权限。")
        if self.wsl and (not self.tool("powershell.exe") or not self.tool("wslpath")):
            raise SetupError("Enable WSL Windows interoperability, reopen Ubuntu, then retry / 请启用 WSL 的 Windows 互操作功能，重新打开 Ubuntu 后重试。")
        if self.c.control_dir == ROOT:
            origin = self.run(["git", "-C", ROOT, "remote", "get-url", "origin"]).stdout.strip()
            if not self.origin_matches(origin, self.c.control):
                raise SetupError("The PreviewMesh code directory cannot be the deployment management directory / PreviewMesh 代码目录不能用作部署管理目录。")

    def dependencies(self):
        self.preflight()
        self.run(["sudo", "-v"], interactive=True)
        self.say("Installing missing tools; existing compatible versions are reused.",
                 "安装缺少的工具；已安装且兼容的版本会直接复用。")
        required = {"git": "git", "curl": "curl", "ip": "iproute2", "ss": "iproute2",
                    "tar": "tar", "iptables": "iptables", "systemd-socket-proxyd": "systemd"}
        packages = {package for tool, package in required.items() if not self.tool(tool)
                    and not (tool == "systemd-socket-proxyd" and Path("/usr/lib/systemd/systemd-socket-proxyd").exists())}
        yaml_check = [sys.executable, "-c", "import yaml"]
        needs_yaml = self.run(yaml_check, check=False).returncode != 0
        if needs_yaml:
            packages.add("python3-yaml")
        if packages:
            self.run(["sudo", "apt-get", "update"], timeout=600)
            self.run(["sudo", "apt-get", "install", "-y", "ca-certificates", *sorted(packages)], timeout=900)
        if needs_yaml and self.run(yaml_check, check=False).returncode:
            raise SetupError("PyYAML is unavailable to the installer Python. Use Ubuntu's /usr/bin/python3 with python3-yaml installed, then rerun / 安装器使用的 Python 无法导入 PyYAML。请确认已安装 python3-yaml，并使用 Ubuntu 的 /usr/bin/python3 重新运行。")
        for name in ("gh", "go", "helm"):
            if not self.tool(name):
                spec = self.state.setdefault("downloads", {}).get(name)
                if not spec:
                    spec = tool_release(name)
                    self.state["downloads"][name] = spec
                    self.save()
                destination = self.home / "tools" / name / spec["version"]
                if not (destination / spec["binary"]).exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(dir=destination.parent) as work:
                        work = Path(work)
                        download_archive(spec["url"], spec["sha256"], work)
                        if destination.exists():
                            raise SetupError(f"Unexpected tool directory / 工具目录冲突: {destination}")
                        os.replace(work, destination)
                binary = destination / spec["binary"]
                (self.home / "bin").mkdir(parents=True, exist_ok=True)
                (self.home / "bin" / name).symlink_to(binary)
            if name == "go":
                version = self.run(["go", "version"]).stdout
                match = re.search(r"\bgo(\d+)\.(\d+)", version)
                if not match or tuple(map(int, match.groups())) < (1, 25):
                    raise SetupError("Go 1.25+ required. Update the existing Go installation and rerun / 请将现有 Go 升级到 1.25 或更高版本后重试。")
            if name == "helm" and not self.run(["helm", "version", "--short"]).stdout.startswith("v3."):
                raise SetupError("Helm 3 required. Adjust PATH to Helm 3, then rerun / 需要 Helm 3，请调整 PATH 后重试；不会覆盖已有 Helm。")
        if self.run(["gh", "auth", "status", "--hostname", "github.com"], check=False).returncode:
            self.say("Sign in to GitHub in your browser. The installer needs access to create/configure repositories and workflows.",
                     "请在浏览器登录 GitHub。安装器需要创建和配置仓库及工作流的权限。")
            self.run(["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https",
                      "--web", "--scopes", "repo,workflow"], interactive=True, timeout=900)
        if self.state.get("runner_name"):
            if any(item["name"] == self.state["runner_name"] and item["busy"] for item in self.all_runners()):
                raise SetupError("Runner is busy; wait for its job to finish before installing / Runner 正在工作，请等任务结束后再安装。")

    @staticmethod
    def origin_matches(url, repository):
        return url.removesuffix(".git").lower() in (
            "https://github.com/" + repository.lower(), "git@github.com:" + repository.lower())

    def checkout(self, path, repository):
        if path.exists() and any(path.iterdir()):
            origin = self.run(["git", "-C", path, "remote", "get-url", "origin"], check=False)
            if origin.returncode and path == self.c.control_dir:
                upstream = self.run(["git", "-C", path, "remote", "get-url", "upstream"], check=False)
                if upstream.returncode == 0 and self.origin_matches(upstream.stdout.strip(), self.c.public):
                    self.run(["git", "-C", path, "remote", "add", "origin", f"https://github.com/{repository}.git"])
                    origin = self.run(["git", "-C", path, "remote", "get-url", "origin"])
            actual = origin.stdout.strip()
            if not self.origin_matches(actual, repository):
                raise SetupError(f"Wrong checkout origin / 仓库 origin 不匹配: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.run(["gh", "repo", "clone", repository, path], timeout=600)

    def control(self):
        # A 404 is the only reason to create; authentication/network errors must stop.
        lookup = self.run(["gh", "api", f"repos/{self.c.control}"], check=False)
        if lookup.returncode:
            if "HTTP 404" not in lookup.stderr:
                raise SetupError("Cannot inspect deployment management repository; check GitHub access / 无法检查部署管理仓库，请检查 GitHub 权限和网络。")
            directory = self.c.control_dir
            if directory.exists() and any(directory.iterdir()):
                upstream = self.run(["git", "-C", directory, "remote", "get-url", "upstream"], check=False)
                origins = self.run(["git", "-C", directory, "remote"]).stdout.split()
                if upstream.returncode or not self.origin_matches(upstream.stdout.strip(), self.c.public) or "origin" in origins:
                    raise SetupError("Nonempty deployment management directory is not an interrupted PreviewMesh code clone / 部署管理目录非空，且不属于可继续恢复的 PreviewMesh 代码下载。")
            else:
                directory.parent.mkdir(parents=True, exist_ok=True)
                self.run(["git", "clone", "--origin", "upstream",
                          f"https://github.com/{self.c.public}.git", directory], timeout=600)
            self.run(["gh", "repo", "create", self.c.control, "--private", "--source", directory,
                      "--remote", "origin"], timeout=300)
            self.state["created_control"] = True
            self.save()
            info = self.api(f"repos/{self.c.control}")
        else:
            info = json.loads(lookup.stdout)
        if not info["private"] or not info.get("permissions", {}).get("admin"):
            raise SetupError("Deployment management repository must be PRIVATE and your account must administer it / 部署管理仓库必须是私有仓库，当前账号需有管理权限。")
        if info.get("default_branch") != "main" and not self.state.get("created_control"):
            raise SetupError("Deployment management default branch must be main / 部署管理仓库的默认分支必须为 main。")
        self.checkout(self.c.control_dir, self.c.control)
        branch = self.run(["git", "-C", self.c.control_dir, "branch", "--show-current"]).stdout.strip()
        if branch != "main":
            raise SetupError("Switch the deployment management local directory to main and rerun / 请将部署管理本地目录切换到 main 分支后重试。")
        # Refuse old control code rather than silently deploying it with new settings.
        cli = self.c.control_dir / "cmd/previewmesh/main.go"
        if not cli.exists() or '"domain-suffix"' not in cli.read_text():
            raise SetupError("Deployment management code predates this installer. Use scripts/update-upstream.sh in its local directory, review and publish that update, then rerun.\n部署管理代码版本过旧。请在部署管理本地目录运行 scripts/update-upstream.sh，审核并发布更新后重新安装。")
        changes = self.run(["git", "-C", self.c.control_dir, "status", "--porcelain"]).stdout.splitlines()
        expected_hash = self.state.get("registry_hash")
        for change in changes:
            if change[3:] == "config/repositories.json" and expected_hash == fingerprint((self.c.control_dir / change[3:]).read_text()):
                continue
            raise SetupError("Deployment management local directory has changes; commit or stash them before installation / 部署管理本地目录有改动，请先提交或暂存后再安装。")
        self.control_id = str(info["id"])
        self.state["control_id"] = self.control_id
        self.save()
        generated = []
        for source in self.c.sources:
            info = self.api(f"repos/{source.repository}")
            if info["full_name"] != source.repository:
                raise SetupError(f"Use canonical GitHub repository name / 请使用 GitHub 的准确仓库名: {info['full_name']}")
            if not info.get("permissions", {}).get("admin"):
                raise SetupError(f"Application repository admin access required to upload Actions secrets / 上传 Actions Secrets 需要应用源码仓库的管理权限: {source.repository}")
            self.checkout(source.directory, source.repository)
            generated.append({"repository_id": str(info["id"]), "source_repository": source.repository,
                              "port": source.port, "source_secret": "SOURCE_REPO_" + str(info["id"])})
        registry_path = self.c.control_dir / "config/repositories.json"
        existing = json.loads(registry_path.read_text())
        registry = merge_registry(existing, generated)
        self.registrations = [next(item for item in registry if item["source_repository"] == source.repository)
                              for source in self.c.sources]
        content = json.dumps(registry, indent=2) + "\n"
        # Save intended content before writing so interruption can be safely recognized.
        self.state["registry_hash"] = fingerprint(content)
        self.save()
        atomic_write(registry_path, content, 0o644)
        self.run(["go", "build", "-o", self.c.control_dir / "bin/control", "./cmd/control"], cwd=self.c.control_dir, timeout=600)
        for item in self.registrations:
            self.run([self.c.control_dir / "bin/control", "resolve", "--repository-id", item["repository_id"],
                      "--source-repository", item["source_repository"], "--pr", "1"], cwd=self.c.control_dir)
        if self.run(["git", "-C", self.c.control_dir, "config", "user.email"], check=False).returncode:
            user = self.api("user")
            self.run(["git", "-C", self.c.control_dir, "config", "user.name", user["login"]])
            self.run(["git", "-C", self.c.control_dir, "config", "user.email",
                      f"{user['id']}+{user['login']}@users.noreply.github.com"])
        self.run(["git", "-C", self.c.control_dir, "add", "--", "config/repositories.json"])
        if self.run(["git", "-C", self.c.control_dir, "diff", "--cached", "--quiet"], check=False).returncode:
            self.run(["git", "-C", self.c.control_dir, "commit", "-m", "Configure PreviewMesh sources"])
        self.run(["gh", "auth", "setup-git", "--hostname", "github.com"])
        self.run(["git", "-C", self.c.control_dir, "push", "-u", "origin", "main"], timeout=600)
        if self.state.get("created_control"):
            self.api(f"repos/{self.c.control}", method="PATCH", data={"default_branch": "main"})

    def credential(self, path, en, zh):
        self.say(en, zh)
        if not path.exists():
            self.say(f"Token file: {path}", f"Token 文件：{path}")
            if not sys.stdin.isatty():
                raise SetupError(f"Create token file with mode 600, or rerun in a terminal / 请创建权限为 600 的 Token 文件，或在交互终端重试: {path}")
            prompt = "粘贴 Token（隐藏，直接回车取消）: " if self.zh else "Paste token (hidden; Enter cancels): "
            value = getpass.getpass(prompt).strip()
            if not value:
                raise SetupError("No token supplied; progress retained / 未提供 Token，已保留进度。")
            self.secrets.append(value)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(path, value + "\n")
        value = read_token(path)
        self.secrets.append(value)
        return value

    def secrets_step(self):
        fine = "https://github.com/settings/personal-access-tokens/new"
        for source, registration in zip(self.c.sources, self.registrations):
            token = self.credential(source.token_file,
                f"Application access token for {source.repository}: deployment automation uses it to read this application and update PR feedback. Create a fine-grained token at {fine}; select only this application. Contents/Metadata: read; Pull requests/Commit statuses: read and write.",
                f"{source.repository} 的应用访问 Token：部署自动化用它读取应用代码并更新 PR 反馈。在 {fine} 创建 fine-grained Token，仅选这个应用仓库。Contents/Metadata 只读；Pull requests/Commit statuses 读写。")
            self.run(["gh", "api", f"repos/{source.repository}"], env={"GH_TOKEN": token})
            self.run(["gh", "secret", "set", registration["source_secret"], "--repo", self.c.control, "--app", "actions"], input=token)
        token = self.credential(self.c.dispatch_file,
            f"Deployment notification token: application workflows use it to start workflows in {self.c.control}. Create it at {fine}; select only that deployment management repository. Actions: read/write; Metadata: read.",
            f"部署通知 Token：应用工作流用它启动 {self.c.control} 中的部署工作流。在 {fine} 创建，仅选这个部署管理仓库。Actions 读写；Metadata 只读。")
        self.run(["gh", "api", f"repos/{self.c.control}/actions/workflows"], env={"GH_TOKEN": token})
        for source in self.c.sources:
            self.run(["gh", "secret", "set", "PREVIEWMESH_DISPATCH_TOKEN", "--repo", source.repository, "--app", "actions"], input=token)
        token = self.credential(self.c.ghcr_file,
            "Image download token (GHCR): the cluster uses it to pull preview images. At https://github.com/settings/tokens/new?scopes=read:packages create a classic PAT with read:packages and access to the preview packages.",
            "镜像下载 Token（GHCR）：集群用它拉取预览镜像。在 https://github.com/settings/tokens/new?scopes=read:packages 创建 classic PAT，选择 read:packages，账号需能读取预览镜像包。")
        self.run(["gh", "secret", "set", "GHCR_READ_TOKEN", "--repo", self.c.control, "--app", "actions"], input=token)

    def managed(self, path, content, mode=0o644, privileged=False, adopt=None):
        """Only replace files previously written by us or an exact shipped template."""
        path = Path(path)
        key = str(path)
        if privileged:
            result = self.run(["sudo", "test", "-e", path], check=False)
            old = self.run(["sudo", "cat", path]).stdout if result.returncode == 0 else None
            if self.run(["sudo", "test", "-L", path], check=False).returncode == 0:
                raise SetupError(f"Refusing symlink / 拒绝符号链接: {path}")
        else:
            if path.is_symlink():
                raise SetupError(f"Refusing symlink / 拒绝符号链接: {path}")
            old = path.read_text() if path.exists() else None
        previous = self.state.setdefault("files", {}).get(key)
        if old is not None and old != content and fingerprint(old) != previous and old != adopt:
            difference = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True),
                                                     fromfile=str(path), tofile="proposed"))
            raise SetupError(f"Existing configuration differs; review before replacing / 现有配置不同，请先审核:\n{difference[:4000]}")
        # Checkpoint intent first; recovery accepts either previous or intended content.
        if old == content:
            self.state["files"][key] = fingerprint(content)
            self.save()
            return False
        if privileged:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as temporary:
                temporary.write(content)
                temporary.flush()
                self.run(["sudo", "install", "-d", "-m", "0755", path.parent])
                self.run(["sudo", "install", "-m", format(mode, "o"), "-o", "root", "-g", "root", temporary.name, path])
        else:
            atomic_write(path, content, mode)
        self.state["files"][key] = fingerprint(content)
        self.save()
        return True

    def kube(self, *args, check=True, timeout=300):
        return self.run(["sudo", self.tool("k3s"), "kubectl",
                         "--kubeconfig=/etc/rancher/k3s/k3s.yaml", *args], check=check, timeout=timeout)

    def linux_network(self):
        addresses = json.loads(self.run(["ip", "-j", "-4", "address", "show"]).stdout)
        address = next((entry for interface in addresses for entry in interface.get("addr_info", [])
                        if entry.get("local") == self.c.lan_ip), None)
        if not address:
            raise SetupError("lan_ip is not assigned to this Ubuntu machine / lan_ip 不属于这台 Ubuntu 机器。")
        subnet = self.c.subnet
        if subnet == "auto":
            subnet = str(ipaddress.ip_network(f"{self.c.lan_ip}/{address['prefixlen']}", strict=False))
        return {"address": self.c.lan_ip, "subnet": subnet}

    def cluster(self):
        if not self.tool("k3s"):
            version = self.state.get("k3s_version")
            if not version:
                # Follow the official stable channel, then retain the exact selected release.
                request = urllib.request.Request("https://update.k3s.io/v1-release/channels/stable")
                try:
                    with urllib.request.urlopen(request, timeout=120) as response:
                        version = response.url.rsplit("/", 1)[-1]
                except OSError:
                    raise SetupError("Cannot resolve K3s stable release / 无法查询 K3s 稳定版本，请检查网络。")
                if not re.fullmatch(r"v\d+\.\d+\.\d+\+k3s\d+", version):
                    raise SetupError("Unexpected K3s stable version / K3s 稳定版本信息异常。")
                self.state["k3s_version"] = version
                self.save()
            # The tagged official installer verifies the binary checksum itself.
            script = fetch(f"https://raw.githubusercontent.com/k3s-io/k3s/{version}/install.sh").decode()
            with tempfile.NamedTemporaryFile(mode="w") as temporary:
                temporary.write(script)
                temporary.flush()
                self.run(["sudo", "env", "INSTALL_K3S_VERSION=" + version,
                          "INSTALL_K3S_EXEC=server --disable servicelb", "sh", temporary.name], timeout=900)
        self.run(["sudo", "systemctl", "is-active", "--quiet", "k3s"])
        # Use K3s's matching client when no separate kubectl is installed.
        if not self.tool("kubectl"):
            (self.home / "bin").mkdir(parents=True, exist_ok=True)
            (self.home / "bin/kubectl").symlink_to(self.tool("k3s"))
        self.kube("wait", "--for=condition=Ready", "nodes", "--all", "--timeout=180s")
        self.kube("-n", "kube-system", "rollout", "status", "deployment/traefik", "--timeout=180s")
        self.kube("apply", "-f", ROOT / "ops/kubernetes/runner-rbac.yaml")
        for target in (self.c.runner_config, self.c.runner_config.parent / ".runner-check"):
            if (self.run(["git", "-C", self.c.control_dir, "ls-files", "--error-unmatch", target], check=False).returncode == 0
                    or self.run(["git", "-C", self.c.control_dir, "check-ignore", "--quiet", target], check=False).returncode != 0):
                raise SetupError("Runner credentials and temporary files must be Git-ignored; update the deployment management .gitignore / Runner 凭据及临时文件必须被 Git 忽略，请更新部署管理仓库的 .gitignore。")
        # root must never execute a k3s binary writable by the runner user.
        k3s = Path(self.tool("k3s")).resolve()
        for part in [k3s, *k3s.parents]:
            info = part.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise SetupError(f"K3s binary and ancestors must be root-owned, not group/world writable / K3s 程序及上级目录需归 root 所有且不能被组或其他用户写入: {part}")
        runtime = {"control": self.c.control, "uid": os.getuid(), "gid": os.getgid(),
                   "control_dir": str(self.c.control_dir), "kubeconfig": str(self.c.runner_config),
                   "k3s": str(k3s), "firewall": None if self.wsl else self.linux_network()}
        self.managed(SYSTEM_CONFIG, json.dumps(runtime, indent=2) + "\n", 0o600, True)
        helper = Path("/usr/local/lib/previewmesh/maintain.py")
        self.managed(helper, (ROOT / "scripts/setup_maintenance.py").read_text(), 0o755, True)
        self.run(["sudo", "/usr/bin/python3", helper, "--force"])
        self.managed("/etc/systemd/system/previewmesh-maintenance.service",
            "[Unit]\nDescription=Renew PreviewMesh access and refresh its ingress target\n"
            "After=k3s.service\nRequires=k3s.service\n\n[Service]\nType=oneshot\n"
            "ExecStart=/usr/bin/python3 /usr/local/lib/previewmesh/maintain.py\n"
            "UMask=0077\n", privileged=True)
        self.managed("/etc/systemd/system/previewmesh-maintenance.timer",
            "[Unit]\nDescription=Maintain PreviewMesh access before it expires\n\n[Timer]\n"
            "OnBootSec=30s\nOnUnitActiveSec=5min\nPersistent=true\n\n[Install]\n"
            "WantedBy=timers.target\n", privileged=True)
        self.run(["sudo", "systemctl", "daemon-reload"])
        self.run(["sudo", "systemctl", "enable", "--now", "previewmesh-maintenance.timer"])

    @staticmethod
    def systemd_value(value):
        if any(char in value for char in "\r\n\0"):
            raise SetupError("Invalid service environment / 服务环境变量无效。")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    def runner_download(self):
        catalog = self.api(f"repos/{self.c.control}/actions/runners/downloads")
        if not isinstance(catalog, list) or any(not isinstance(item, dict) for item in catalog):
            raise SetupError("Invalid Runner download list; check GitHub API and retry / Runner 下载列表格式错误，请检查 GitHub API 后重试。")
        app = next((item for item in catalog
                    if item.get("os") == "linux" and item.get("architecture") == "x64"), None)
        if app is not None:
            filename = app.get("filename")
            version = re.fullmatch(r"actions-runner-linux-x64-(\d+\.\d+\.\d+)\.tar\.gz",
                                   filename if isinstance(filename, str) else "")
            if not version:
                raise SetupError("Invalid Linux x64 Runner filename; check GitHub API and retry / Linux x64 Runner 文件名异常，请检查 GitHub API 后重试。")
            release = self.api(f"repos/actions/runner/releases/tags/v{version[1]}")
        else:
            self.say("GitHub returned no Linux x64 Runner download; using the official stable release with SHA-256 verification.",
                     "GitHub 未返回 Linux x64 Runner 下载项；将使用官方稳定发行包，并验证 SHA-256。")
            release = self.api("repos/actions/runner/releases/latest")
        return runner_release(app, release)

    def runner(self):
        directory = self.home / "runner"
        name = "previewmesh-" + self.control_id + "-" + re.sub(r"[^a-zA-Z0-9_-]", "-", socket.gethostname())[:40]
        target = f"https://github.com/{self.c.control}"
        if (directory / ".runner").exists():
            # Runner-generated JSON can carry a UTF-8 BOM after registration.
            settings = json.loads((directory / ".runner").read_text(encoding="utf-8-sig"))
            if settings.get("gitHubUrl", "").lower().rstrip("/") != target.lower():
                raise SetupError("Existing runner belongs to another repository / 现有 Runner 属于其他仓库。")
            name = settings["agentName"]
            matches = [item for item in self.all_runners() if item["id"] == settings["agentId"]]
            if not matches:
                raise SetupError("Runner registration was removed in GitHub; remove its stale local registration with config.sh remove, then rerun / GitHub 上的 Runner 登记已删除，请用 config.sh remove 清理失效的本地登记后重试。")
        else:
            if any(item["name"] == name for item in self.all_runners()):
                raise SetupError("Runner name is already registered elsewhere / 相同 Runner 名称已在其他位置登记，请先检查 GitHub Runners 页面。")
            spec = self.state.get("runner_download")
            if not spec:
                spec = self.runner_download()
                self.state["runner_download"] = spec
                self.save()
            if not (directory / "config.sh").exists():
                if directory.exists() and any(directory.iterdir()):
                    raise SetupError(f"Unexpected runner directory / Runner 目录冲突: {directory}")
                with tempfile.TemporaryDirectory(dir=self.home) as work:
                    download_archive(spec["url"], spec["sha256"], Path(work))
                    directory.mkdir(exist_ok=True)
                    for child in Path(work).iterdir():
                        shutil.move(str(child), directory / child.name)
            self.run(["sudo", directory / "bin/installdependencies.sh"], cwd=directory, timeout=900)
            token = self.api(f"repos/{self.c.control}/actions/runners/registration-token", method="POST")["token"]
            self.secrets.append(token)
            self.run([directory / "config.sh", "--unattended", "--url", target, "--name", name,
                      "--labels", "previewmesh", "--work", "_work"], cwd=directory,
                     env={"ACTIONS_RUNNER_INPUT_TOKEN": token}, timeout=300)
        if not (directory / ".service").exists():
            self.run(["sudo", directory / "svc.sh", "install", getpass.getuser()], cwd=directory)
        service = (directory / ".service").read_text().strip()
        if not re.fullmatch(r"actions\.runner\.[a-zA-Z0-9_.@-]+\.service", service):
            raise SetupError("Unexpected runner service name / Runner 服务名称异常。")
        dropin = "[Service]\nEnvironment=" + self.systemd_value("KUBECONFIG=" + str(self.c.runner_config))
        dropin += "\nEnvironment=" + self.systemd_value("PATH=" + self.env["PATH"]) + "\n"
        dropin_path = f"/etc/systemd/system/{service}.d/previewmesh.conf"
        # Remember a pending restart across a busy-job interruption.
        changed = self.managed(dropin_path, dropin, privileged=True)
        if changed:
            self.state["runner_restart_pending"] = True
            self.save()
        changed = bool(self.state.get("runner_restart_pending"))
        self.run(["sudo", "systemctl", "daemon-reload"])
        active = self.run(["systemctl", "is-active", "--quiet", service], check=False).returncode == 0
        if changed and active:
            if any(item["name"] == name and item["busy"] for item in self.all_runners()):
                raise SetupError("Runner is busy; rerun installation when its job finishes / Runner 正在执行任务，任务完成后请重试安装。")
            self.run(["sudo", "systemctl", "restart", service])
        else:
            self.run(["sudo", "systemctl", "start", service])
        self.state.update(runner_name=name, runner_service=service, runner_restart_pending=False)
        self.save()
        for _ in range(30):
            runners = self.all_runners()
            match = next((item for item in runners if item["name"] == name), None)
            if match and match["status"] == "online" and {"self-hosted", "Linux", "X64", "previewmesh"} <= {item["name"] for item in match["labels"]}:
                return
            time.sleep(2)
        raise SetupError("Runner did not come online; inspect its service log and rerun / Runner 未上线，请检查服务日志后重试。")

    def powershell(self, *args, interactive=False):
        return self.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
                        interactive=interactive, timeout=600)

    def check_traefik_values(self, current, expected):
        # Import after dependency installation so a clean Ubuntu host can bootstrap.
        try:
            import yaml
        except ImportError:
            raise SetupError("PyYAML is required; install python3-yaml or rerun install to prepare dependencies / 需要 PyYAML；请安装 python3-yaml，或重新运行 install 自动补齐依赖。")
        if not isinstance(current, str) or not isinstance(expected, str):
            raise SetupError("Traefik valuesContent must be YAML text; inspect the HelmChartConfig / Traefik 的 valuesContent 必须是 YAML 文本，请检查 HelmChartConfig。")
        try:
            observed, required = yaml.safe_load(current), yaml.safe_load(expected)
        except yaml.YAMLError:
            raise SetupError("Invalid Traefik YAML; inspect the HelmChartConfig and required template / Traefik YAML 格式无效，请检查 HelmChartConfig 和所需模板。")
        if not isinstance(required, dict):
            raise SetupError("Invalid required Traefik template / 所需 Traefik 模板格式无效。")

        def matches(value, profile):
            # YAML booleans and numbers differ even though Python considers False == 0.
            if type(value) is not type(profile):
                return False
            if isinstance(profile, dict):
                return value.keys() == profile.keys() and all(matches(value[key], item)
                                                              for key, item in profile.items())
            return value == profile

        if matches(observed, required):
            return
        # Only complete, previously shipped profiles are safe to migrate automatically.
        service = {"service": {"spec": {"type": "ClusterIP"}}}
        legacy = (service, dict(service, providers={"kubernetesIngress": {
            "ingressEndpoint": {"ip": "127.0.0.1"}}}))
        if any(matches(observed, profile) for profile in legacy):
            self.say("Existing Traefik values match a supported previous PreviewMesh template; upgrading local Ingress readiness settings.",
                     "现有 Traefik 配置与 PreviewMesh 支持的旧模板一致，将补齐本地预览所需的入口状态设置。")
            return
        raise SetupError("Existing Traefik customization differs from supported PreviewMesh profiles. Compare it with the required template before installing / 现有 Traefik 配置含有与 PreviewMesh 支持模板不同的设置，请对照所需模板审核后重试。\n"
                         "Inspect / 查看现有配置: sudo k3s kubectl -n kube-system get helmchartconfig traefik -o yaml\n"
                         f"Required template / 所需模板: {ROOT / 'ops/kubernetes/traefik-helmchartconfig.yaml'}")

    def network(self):
        # Validate DNS before changing routing. DNS rebinding protection is not bypassed.
        self.check_dns()
        current = self.kube("-n", "kube-system", "get", "helmchartconfig", "traefik",
                            "--ignore-not-found", "-o", "json").stdout.strip()
        manifest = (ROOT / "ops/kubernetes/traefik-helmchartconfig.yaml").read_text()
        expected_values = manifest.split("  valuesContent: |-\n", 1)[1]
        expected_values = "\n".join(line[4:] for line in expected_values.splitlines())
        if current:
            chart = json.loads(current)
            spec = chart.get("spec") if isinstance(chart, dict) else None
            self.check_traefik_values(spec.get("valuesContent") if isinstance(spec, dict) else None,
                                      expected_values)
        self.kube("apply", "-f", ROOT / "ops/kubernetes/traefik-helmchartconfig.yaml")
        for _ in range(60):
            service = json.loads(self.kube("-n", "kube-system", "get", "service", "traefik", "-o", "json").stdout)
            if service["spec"]["type"] == "ClusterIP" and service["spec"].get("clusterIP") not in (None, "None", ""):
                break
            time.sleep(2)
        else:
            raise SetupError("Traefik did not become ready / Traefik 未就绪，请检查 K3s 服务。")
        self.kube("-n", "kube-system", "rollout", "status", "deployment/traefik", "--timeout=180s")
        # Maintenance owns this generated value after initial installation.
        self.run(["sudo", "/usr/bin/python3", "/usr/local/lib/previewmesh/maintain.py", "--refresh-ingress"])
        socket_path = Path("/etc/systemd/system/previewmesh-ingress.socket")
        managed_before = str(socket_path) in self.state.get("files", {})
        if not managed_before and not socket_path.exists():
            probe = socket.socket()
            try:
                probe.bind(("0.0.0.0", 18080))
            except OSError:
                raise SetupError("Port 18080 is already occupied; free it and rerun / 18080 端口已被占用，请处理后重试。")
            finally:
                probe.close()
        service_content = (ROOT / "ops/wsl/previewmesh-ingress.service").read_text()
        socket_original = (ROOT / "ops/wsl/previewmesh-ingress.socket").read_text()
        listener = "0.0.0.0:18080" if self.wsl else "127.0.0.1:18080\nListenStream=" + self.c.lan_ip + ":18080"
        socket_content = socket_original.replace("127.0.0.1:18080", listener)
        self.managed("/etc/systemd/system/previewmesh-ingress.service", service_content, privileged=True)
        changed = self.managed(socket_path, socket_content, privileged=True, adopt=socket_original)
        self.run(["sudo", "systemctl", "daemon-reload"])
        if changed:
            self.run(["sudo", "systemctl", "stop", "previewmesh-ingress.service", "previewmesh-ingress.socket"])
        self.run(["sudo", "systemctl", "enable", "--now", "previewmesh-ingress.socket"])
        if self.wsl:
            distro = os.environ.get("WSL_DISTRO_NAME")
            if not distro:
                raise SetupError("WSL_DISTRO_NAME is missing; open a normal WSL terminal / 缺少 WSL 发行版信息，请从正常 WSL 终端启动。")
            windows_script = self.run(["wslpath", "-w", ROOT / "ops/wsl/setup-lan.ps1"]).stdout.strip()
            self.say("Windows will request administrator access to configure LAN forwarding.",
                     "Windows 将请求管理员权限，用来配置局域网转发。")
            self.powershell("-File", windows_script, "-Mode", "Install", "-Distro", distro,
                            "-LanIP", self.c.lan_ip, "-AllowedSubnet", self.c.subnet, interactive=True)
            self.powershell("-File", windows_script, "-Mode", "Check", "-Distro", distro,
                            "-LanIP", self.c.lan_ip, "-AllowedSubnet", self.c.subnet)
        self.http_probe("127.0.0.1")
        self.http_probe(self.c.lan_ip)

    def check_dns(self):
        name = "previewmesh-check." + self.c.suffix
        try:
            addresses = {entry[4][0] for entry in socket.getaddrinfo(name, 18080, socket.AF_INET, socket.SOCK_STREAM)}
        except OSError:
            addresses = set()
        if addresses != {self.c.lan_ip}:
            raise SetupError(f"DNS does not resolve {name} to {self.c.lan_ip}. Check DNS/private-IP filtering with your administrator. For an explicitly configured manual suffix, configure wildcard DNS or a hosts entry for this probe, then each preview hostname on its clients. No hosts fallback was applied.\n域名未解析到配置的局域网 IP。请检查 DNS/内网地址过滤；若选择手工后缀，配置通配 DNS 或探测名 hosts 记录，并为客户端配置各预览主机名。不会自动修改 hosts。\n{ACCESS_GUIDE}")

    def http_probe(self, address):
        request = urllib.request.Request(f"http://{address}:18080/",
                                         headers={"Host": "previewmesh-ingress-check.invalid"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as response:
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
        except OSError:
            raise SetupError(f"Cannot reach preview entry / 无法访问预览入口: {address}:18080")
        if code != 404:
            raise SetupError(f"Expected Traefik 404, got {code} / 入口应返回 Traefik 的 404，实际为 {code}。")

    def sources(self):
        owner, repo = self.c.control.split("/")
        template = (ROOT / "templates/source-notify.yml").read_text()
        expected = template.replace("YOUR_GITHUB_OWNER", owner).replace("YOUR_CONTROL_REPOSITORY", repo)
        for source in self.c.sources:
            self.managed(source.directory / ".github/workflows/previewmesh-notify.yml", expected)
            self.say(f"{source.repository}: notification file prepared. Use onboard-source to review and open its PR.",
                     f"{source.repository}：通知文件已配置好。使用 onboard-source 审核并创建接入 PR。")
            if not (source.directory / "Dockerfile").exists():
                self.say("Application still needs a root Dockerfile.", "应用仍需在根目录提供 Dockerfile。")
        self.say("Applications must listen on 0.0.0.0, run as UID/GID 65532 and return status=ok plus PREVIEW_COMMIT_SHA at GET /health.",
                 "应用需监听 0.0.0.0、支持 UID/GID 65532，并在 GET /health 返回 status=ok 和来自 PREVIEW_COMMIT_SHA 的 commit_sha。")

    def setup_command(self, command, source=None):
        args = ["bash", str(ROOT / "scripts/setup.sh"), command, "--config", str(self.c.path)]
        if source:
            args += ["--source", source]
        return shlex.join(args)

    def source_verification(self):
        self.say("After merging, confirm the notification on the default branch with:",
                 "合并后，用以下命令确认默认分支上的通知工作流：")
        print(self.setup_command("doctor"))
        self.say("Then open a same-repository test application PR targeting the application default branch. Check application notification Actions → deployment management Preview workflow → preview URL and matching commit SHA. Close the test PR and confirm cleanup. A second LAN machine should also open the preview URL before closing.",
                 "然后用应用源码仓库内的分支创建一个指向默认分支的测试 PR（不要使用 fork）。依次检查应用通知 Actions → 部署管理仓库的 Preview 工作流 → 预览 URL 与提交 SHA 一致。关闭测试 PR 后确认清理；关闭前还应从另一台局域网机器访问预览 URL。")

    def completion(self):
        self.say("Installation complete: all 8 infrastructure/setup stages passed.",
                 "安装完成：8 个基础环境与配置阶段均已通过。")
        self.installation_progress()
        self.say(f"Preview URL format: http://pm-r<repository_id>-pr<PR>.{self.c.suffix}:18080 (entry port, not application port).",
                 f"预览地址格式：http://pm-r<repository_id>-pr<PR>.{self.c.suffix}:18080（入口端口，并非应用端口）。")
        if self.c.domain_suffix == "auto":
            self.say("Recommended sslip.io: open the ready preview URL from the allowed network; no hosts edits are needed when DNS resolves.",
                     "推荐 sslip.io：在允许的网络中直接打开已就绪的预览 URL；DNS 正常时无需修改 hosts。")
        else:
            self.say("Manual suffix: each client needs wildcard DNS or a hosts entry for the exact preview hostname before opening its URL.",
                     "手工后缀：每台客户端需先配置通配 DNS 或该预览完整主机名的 hosts 记录，再打开 URL。")
        self.say(f"Installation → open preview: {ACCESS_GUIDE}", f"安装完成 → 打开预览：{ACCESS_GUIDE}")
        for source in self.c.sources:
            if self.source_status.get(source.repository) == "merged":
                self.say(f"{source.repository}: notification merged; test PR verification remains.",
                         f"{source.repository}：通知已合并；仍需测试 PR 验证。")
            else:
                self.say(f"{source.repository}: application onboarding is not complete. Review the diff, then create its PR:",
                         f"{source.repository}：应用尚未接入。请先查看差异，再创建接入 PR：")
                command = self.setup_command("onboard-source", source.repository)
                print(command + "\n" + command + " --create-pr")
        self.source_verification()

    def installation_progress(self):
        # Include reuse only after current-run validation has succeeded.
        for number, en, zh in self.completed_stages:
            self.say(f"Completed [{number}/8]: {en}", f"已完成 [{number}/8]：{zh}")
        for number, (result, reason) in self.stage_results.items():
            self.say(f"Recovery [{number}/8]: {result}: {reason}",
                     f"恢复结果 [{number}/8]：{'复用' if result == 'reused' else '重新执行'}：{reason}")
        if len(self.completed_stages) < 8:
            remaining = ", ".join(str(number) for number in range(len(self.completed_stages) + 1, 9))
            self.say(f"Installation incomplete. Stages still requiring completion: {remaining}. Application onboarding is not yet verified.",
                     f"安装尚未完成。仍需完成阶段：{remaining}。应用接入尚未验证。")

    def enable(self):
        self.run(["gh", "variable", "set", "PREVIEWMESH_DOMAIN_SUFFIX", "--repo", self.c.control, "--body", self.c.suffix])
        actions = self.api(f"repos/{self.c.control}/actions/permissions")
        if not actions["enabled"]:
            self.api(f"repos/{self.c.control}/actions/permissions", method="PUT", data={"enabled": True})
        for source in self.c.sources:
            actions = self.api(f"repos/{source.repository}/actions/permissions")
            if not actions["enabled"]:
                self.api(f"repos/{source.repository}/actions/permissions", method="PUT", data={"enabled": True})
        self.run(["gh", "variable", "set", "PREVIEWMESH_ENABLED", "--repo", self.c.control, "--body", "true"])
        self.doctor()

    def doctor(self):
        failures = []
        def check(label, function):
            try:
                function()
                self.say("OK: " + label, "通过：" + label)
            except (SetupError, OSError, ValueError, KeyError) as error:
                failures.append(label)
                print(self.redact(f"FAIL / 失败: {label}: {error}"))
        def require(condition, message):
            if not condition:
                raise SetupError(message)
        check("configuration / 配置", self.preflight)
        check("GitHub login / GitHub 登录", lambda: self.run(["gh", "auth", "status", "--hostname", "github.com"]))
        for service in ("k3s", "previewmesh-ingress.socket", "previewmesh-maintenance.timer"):
            check(service, lambda service=service: self.run(["systemctl", "is-active", "--quiet", service]))
        def maintenance_check():
            failed = self.run(["systemctl", "is-failed", "--quiet", "previewmesh-maintenance.service"], check=False)
            require(failed.returncode != 0, "Maintenance failed; inspect journalctl -u previewmesh-maintenance.service / 维护服务失败，请检查对应服务日志。")
        check("credential renewal / 凭据续期", maintenance_check)
        def runner_check():
            match = next((item for item in self.all_runners() if item["name"] == self.state.get("runner_name")), None)
            require(match and match["status"] == "online" and {"self-hosted", "Linux", "X64", "previewmesh"} <=
                    {item["name"] for item in match["labels"]}, "Runner offline or labels missing / Runner 离线或标签缺失。")
        check("Runner", runner_check)
        def kube_check():
            value = json.loads(self.c.runner_config.read_text())
            token = value["users"][0]["user"]["token"]
            self.secrets.append(token)
            payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
            require(payload["exp"] > time.time() + 300, "Runner access is expired or near expiry / Runner 凭据过期或即将过期。")
            self.run(["kubectl", "--kubeconfig", self.c.runner_config, "auth", "can-i", "create", "namespaces"])
            refused = self.run(["kubectl", "--kubeconfig", self.c.runner_config, "auth", "can-i", "create", "clusterroles"], check=False)
            require(refused.returncode == 1 and refused.stdout.strip() == "no", "Runner permissions exceed expected scope / Runner 权限超出预期范围。")
        check("Kubernetes access / 集群权限", kube_check)
        check("DNS", self.check_dns)
        check("HTTP entry / HTTP 入口", lambda: self.http_probe(self.c.lan_ip))
        if self.wsl:
            def windows_check():
                script = self.run(["wslpath", "-w", ROOT / "ops/wsl/setup-lan.ps1"]).stdout.strip()
                self.powershell("-File", script, "-Mode", "Check", "-Distro", os.environ.get("WSL_DISTRO_NAME", ""),
                                "-LanIP", self.c.lan_ip, "-AllowedSubnet", self.c.subnet)
            check("Windows LAN entry / Windows 局域网入口", windows_check)
        def github_check():
            info = self.api(f"repos/{self.c.control}")
            require(info["private"], "Deployment management repository must remain private / 部署管理仓库必须保持私有。")
            require(self.api(f"repos/{self.c.control}/actions/permissions")["enabled"],
                    "Deployment management Actions disabled / 部署管理仓库的 Actions 未启用。")
            variables = self.api(f"repos/{self.c.control}/actions/variables?per_page=100")["variables"]
            values = {item["name"]: item["value"] for item in variables}
            require(values.get("PREVIEWMESH_ENABLED") == "true" and values.get("PREVIEWMESH_DOMAIN_SUFFIX") == self.c.suffix,
                    "Deployment management variables differ / 部署管理仓库的配置变量不匹配。")
            registry = json.loads((self.c.control_dir / "config/repositories.json").read_text())
            secrets = self.api(f"repos/{self.c.control}/actions/secrets?per_page=100")["secrets"]
            require({"GHCR_READ_TOKEN", *(item["source_secret"] for item in registry)} <= {item["name"] for item in secrets},
                    "Deployment management secrets missing / 部署管理仓库缺少 Secrets。")
        check("Deployment management configuration / 部署管理配置", github_check)
        self.pending = []
        self.source_status = {}
        for source in self.c.sources:
            def source_check(source=source):
                values = self.api(f"repos/{source.repository}/actions/secrets?per_page=100")["secrets"]
                require("PREVIEWMESH_DISPATCH_TOKEN" in {item["name"] for item in values},
                        "Application notification secret missing / 应用源码仓库缺少通知 Secret。")
                require(self.api(f"repos/{source.repository}/actions/permissions")["enabled"],
                        "Application Actions disabled / 应用源码仓库的 Actions 未启用。")
            check(source.repository + " Actions", source_check)
            result = self.run(["gh", "api", f"repos/{source.repository}/contents/.github/workflows/previewmesh-notify.yml"], check=False)
            if result.returncode and "HTTP 404" not in result.stderr:
                self.source_status[source.repository] = "unknown"
                failures.append(source.repository + " notification access")
                self.say("Cannot inspect the published notification; check GitHub access.",
                         "无法检查已发布通知文件，请检查 GitHub 访问权限。")
                continue
            expected = (ROOT / "templates/source-notify.yml").read_text().replace("YOUR_GITHUB_OWNER", self.c.control.split("/")[0]).replace("YOUR_CONTROL_REPOSITORY", self.c.control.split("/")[1])
            try:
                remote = base64.b64decode(json.loads(result.stdout)["content"]).decode() if result.returncode == 0 else ""
            except (ValueError, KeyError):
                remote = ""
            if remote != expected:
                self.pending.append(source.repository)
                self.source_status[source.repository] = "pending"
                self.say(f"PENDING: {source.repository} notification is not merged on its default branch. Review and create an onboarding PR:",
                         f"待处理：{source.repository} 默认分支上尚未合并所需通知文件。请审核并创建接入 PR：")
                print(self.setup_command("onboard-source", source.repository) + " --create-pr")
            else:
                self.source_status[source.repository] = "merged"
                self.say(f"OK: {source.repository} notification matches on the default branch.",
                         f"通过：{source.repository} 默认分支上的通知工作流符合预期。")
        self.say("No real preview was created or verified. After publishing the notification, use a normal application PR; check the reported SHA and cleanup after closing.",
                 "尚未创建或验证真实预览。通知文件发布后，通过正常应用 PR 检查预览版本和关闭后的清理结果。")
        self.say("A colleague should also check the entry from a second LAN machine; this machine cannot prove remote reachability.",
                 "还需由同事在另一台局域网机器检查入口；本机检查不能证明其他机器能访问。")
        if failures:
            raise SetupError("Readiness checks failed / 就绪检查失败: " + ", ".join(failures))
        self.say("Infrastructure ready.", "基础环境已就绪。")

    def check(self):
        self.preflight()
        for name in ("git", "python3", "gh", "go", "helm", "k3s", "kubectl"):
            self.say(f"{name}: {self.tool(name) or 'will be installed'}",
                     f"{name}：{self.tool(name) or '安装时自动补齐'}")
        if self.tool("ip"):
            print(self.run(["ip", "-brief", "-4", "address", "show"]).stdout)
        self.say("Configuration checked. check made no changes; install performs the remaining checks and setup.",
                 "配置检查通过。check 未修改系统；install 将继续检查并安装。")

    def install(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_path.touch(mode=0o600, exist_ok=True)
        self.log_path.chmod(0o600)
        # One install per account; validate checkpoints under the installation lock.
        with (self.home / "install.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SetupError("Another installer is running / 另一个安装进程正在运行。")
            stages = [
                ("Prepare installation tools.", "准备安装工具。",
                 "Check this computer's requirements, install missing tools and complete GitHub login so later steps can create repositories and configure services.",
                 "检查本机环境、补齐所需工具并完成 GitHub 登录，让后续步骤能创建仓库和配置服务。", self.dependencies),
                ("Prepare the private deployment management repository.", "准备私有部署管理仓库。",
                 f"Use {self.c.control} to store application registrations and deployment automation. If it is new, create it from {self.c.public}; if it exists, check its PreviewMesh deployment tools. Register the configured applications and publish the management configuration.",
                 f"使用 {self.c.control} 保存应用登记和部署自动化。新仓库会以 {self.c.public} 的代码为基础创建；已有仓库会检查 PreviewMesh 部署工具。随后登记配置中的应用，并发布部署管理配置。", self.control),
                ("Configure repository and image access tokens.", "配置仓库与镜像访问 Token。",
                 "Application access tokens let automation read applications and update PR feedback. The notification token lets applications start deployment workflows. The GHCR token lets the cluster pull images. The next prompts explain each token's target and permissions.",
                 "应用访问 Token 用于读取应用和更新 PR 反馈；部署通知 Token 用于从应用触发部署工作流；GHCR Token 用于让集群拉取镜像。接下来的提示会逐个说明授权目标和权限。", self.secrets_step),
                ("Prepare the local preview cluster.", "准备本机预览集群。",
                 "Set up K3s, the Kubernetes service that runs preview containers, and limited deployment access for automation. Configure automatic renewal of that cluster access so deployments can continue.",
                 "配置 K3s（运行预览容器的 Kubernetes 服务），为部署自动化提供受限的集群访问权限，并配置这项权限的自动续期。", self.cluster),
                ("Connect the deployment runner to GitHub.", "连接 GitHub 部署执行程序。",
                 "Register and start a GitHub Actions Runner on this computer. It receives jobs from the deployment management repository and deploys previews into the local cluster.",
                 "在本机注册并启动 GitHub Actions Runner。这个程序接收部署管理仓库的任务，将预览部署到本机集群。", self.runner),
                ("Configure LAN preview access.", "配置局域网预览访问。",
                 f"Check DNS for {self.c.suffix} and configure the preview entry on {self.c.lan_ip}:18080 for the allowed client subnet. Requests are routed to each application's configured container HTTP port. WSL also needs Windows forwarding and firewall rules.",
                 f"检查 {self.c.suffix} 的 DNS，并在 {self.c.lan_ip}:18080 配置预览入口，供允许网段内的客户端访问。请求会转发到各应用配置的容器内 HTTP 端口；WSL 还需配置 Windows 转发和防火墙规则。", self.network),
                ("Prepare application notification workflows.", "准备应用通知工作流。",
                 "Generate notification workflow files in the application directories for review. Once merged into each application's default branch, they notify deployment management when PRs change. Use onboard-source to review the diff or open an onboarding PR, then review and merge it on GitHub.",
                 "在应用目录生成通知工作流文件供审核。文件合并到应用默认分支后，会在 PR 发生变化时通知部署管理仓库。使用 onboard-source 查看差异或创建接入 PR，再到 GitHub 审核并合并。", self.sources),
                ("Enable automation and check infrastructure readiness.", "启用自动化并检查基础环境。",
                 "Enable the required GitHub Actions workflows and check the cluster, runner, credentials and preview entry. After installation, onboard each application and open a test PR to verify a real preview.",
                 "启用所需的 GitHub Actions 工作流，并检查集群、执行程序、凭据和预览入口。安装完成后，还需接入各应用并创建测试 PR，验证实际预览。", self.enable),
            ]
            for number, (en, zh, detail_en, detail_zh, action) in enumerate(stages, 1):
                self.step(number, en, zh, action, detail=(detail_en, detail_zh))
            self.completion()


def main():
    parser = argparse.ArgumentParser(description="Guided PreviewMesh setup / PreviewMesh 安装")
    parser.add_argument("command", nargs="?", choices=("init", "check", "install", "doctor", "onboard-source"), default="install",
                        help="init: write configuration; check: validate it; install: set up services; doctor: inspect readiness; onboard-source: review an application notification workflow / init 写入配置；check 检查配置；install 安装服务；doctor 检查就绪状态；onboard-source 审核应用通知工作流")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Configuration INI file path / 配置 INI 文件路径")
    parser.add_argument("--language", choices=("en", "zh-CN"), help="Prompt language / 提示语言")
    parser.add_argument("--verbose", action="store_true", help="Show redacted tool output / 显示脱敏工具输出")
    parser.add_argument("--template", action="store_true", help="init: copy a template instead of the interactive wizard / 仅复制配置模板")
    parser.add_argument("--source", help="onboard-source: configured application OWNER/REPO / 配置中的应用源码仓库")
    parser.add_argument("--create-pr", action="store_true", help="onboard-source: review diff and confirm PR creation / 审阅 diff 后确认创建 PR")
    args = parser.parse_args()
    if args.template and args.command != "init":
        parser.error("--template requires init / --template 仅用于 init")
    if (args.source or args.create_pr) and args.command != "onboard-source":
        parser.error("--source and --create-pr require onboard-source")
    if args.command == "onboard-source" and not args.source:
        parser.error("onboard-source requires --source OWNER/REPO")
    installer = None
    try:
        language = args.language
        if language is None and sys.stdin.isatty():
            language = choose_language()
        if args.command == "init":
            if not args.template:
                from setup_wizard import run_wizard
                run_wizard(args.config, ROOT, language=language)
                return 0
            path = args.config.expanduser().absolute()
            if path.exists() or path.is_symlink():
                raise SetupError(f"Already exists; edit this file / 文件已存在，请直接编辑: {path}")
            template = (ROOT / "config/setup.example.ini").read_text()
            if language:
                template = template.replace("language = auto", f"language = {language}")
            atomic_write(path, template)
            label, next_step = ("请编辑", "然后执行") if language == "zh-CN" else ("Edit", "Then run")
            print(f"{label}: {path}\n{next_step}: bash {shlex.quote(str(ROOT / 'scripts/setup.sh'))} install --config {shlex.quote(str(path))}")
            return 0
        config = load_config(args.config, ROOT)
        # A session choice overrides display preferences without rewriting the INI.
        if language:
            config.language = language
        installer = Installer(config, args.command, args.verbose)
        if args.command == "onboard-source":
            from setup_onboard import onboard
            onboard(installer, ROOT, args.source, args.create_pr)
        else:
            getattr(installer, args.command)()
        return 0
    except (SetupError, OSError, ValueError, KeyError, StopIteration) as error:
        # Avoid tracebacks with local values or raw secret-bearing subprocess output.
        message = str(error) if not isinstance(error, (KeyError, StopIteration)) else "Unexpected remote response / 远端返回信息不符合预期。"
        print(installer.redact(message) if installer else message, file=sys.stderr)
        if args.command == "install":
            if installer:
                installer.installation_progress()
            print(f"Fix the issue, then rerun the SAME command / 处理问题后重新运行同一命令:\n"
                  f"bash {shlex.quote(str(ROOT / 'scripts/setup.sh'))} install --config {shlex.quote(str(args.config))}", file=sys.stderr)
            if installer:
                print(f"Redacted log / 脱敏日志: {installer.log_path}", file=sys.stderr)
        elif args.command == "onboard-source":
            print("Review the reported issue and rerun the same command. An existing onboarding branch or PR is preserved. / 请按提示处理后重跑同一命令；已有接入分支或 PR 会保留。", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        if args.command == "install" and installer:
            installer.installation_progress()
        print("\nStopped; rerun the same command to continue / 已停止，重新运行同一命令即可继续。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
