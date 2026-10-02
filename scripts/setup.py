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

from setup_config import (SetupError, atomic_write, fingerprint, load_config,
                          merge_registry, read_token)
from setup_downloads import download_archive, fetch, metadata, runner_release, tool_release

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
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        owner = self.state.get("control")
        if owner and owner != self.c.control and self.state.get("control_id"):
            raise SetupError("This account already manages another control repository / 本机账号已管理另一个 control 仓库。")
        self.env = dict(os.environ)
        for key in ("GH_DEBUG", "GIT_TRACE", "GIT_TRACE_PACKET", "GIT_CURL_VERBOSE", "BASH_ENV", "ENV"):
            self.env.pop(key, None)
        self.env["PATH"] = str(self.home / "bin") + os.pathsep + self.env["PATH"]
        self.secrets = []
        self.registrations = []
        self.wsl = "microsoft" in platform.release().lower()
        self.control_id = self.state.get("control_id")
        self.pending = []

    def say(self, en, zh):
        print(zh if self.zh else en, flush=True)

    def redact(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[REDACTED]", value)

    def save(self):
        self.state["control"] = self.c.control
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

    def step(self, number, en, zh, action):
        self.say(f"\n[{number}/8] {en}", f"\n[{number}/8] {zh}")
        action()
        self.state.setdefault("completed", {})[str(number)] = int(time.time())
        self.save()
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
                raise SetupError("The public checkout cannot be the control directory / 公开 checkout 不能用作 control 目录。")

    def dependencies(self):
        self.preflight()
        self.run(["sudo", "-v"], interactive=True)
        self.say("Installing missing tools; existing compatible versions are reused.",
                 "安装缺少的工具；已安装且兼容的版本会直接复用。")
        required = {"git": "git", "curl": "curl", "ip": "iproute2", "ss": "iproute2",
                    "tar": "tar", "iptables": "iptables", "systemd-socket-proxyd": "systemd"}
        packages = {package for tool, package in required.items() if not self.tool(tool)
                    and not (tool == "systemd-socket-proxyd" and Path("/usr/lib/systemd/systemd-socket-proxyd").exists())}
        if packages:
            self.run(["sudo", "apt-get", "update"], timeout=600)
            self.run(["sudo", "apt-get", "install", "-y", "ca-certificates", *sorted(packages)], timeout=900)
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
                raise SetupError("Cannot inspect control repository; check GitHub access / 无法检查 control 仓库，请检查 GitHub 权限和网络。")
            directory = self.c.control_dir
            if directory.exists() and any(directory.iterdir()):
                upstream = self.run(["git", "-C", directory, "remote", "get-url", "upstream"], check=False)
                origins = self.run(["git", "-C", directory, "remote"]).stdout.split()
                if upstream.returncode or not self.origin_matches(upstream.stdout.strip(), self.c.public) or "origin" in origins:
                    raise SetupError("Nonempty control directory is not an interrupted template clone / 非空 control 目录不属于可恢复的模板下载。")
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
            raise SetupError("Control must be PRIVATE and your account must administer it / control 必须为私有仓库，当前账号需有管理权限。")
        if info.get("default_branch") != "main" and not self.state.get("created_control"):
            raise SetupError("Control default branch must be main / control 默认分支必须为 main。")
        self.checkout(self.c.control_dir, self.c.control)
        branch = self.run(["git", "-C", self.c.control_dir, "branch", "--show-current"]).stdout.strip()
        if branch != "main":
            raise SetupError("Switch the control checkout to main and rerun / 请将 control checkout 切换到 main 后重试。")
        # Refuse old control code rather than silently deploying it with new settings.
        cli = self.c.control_dir / "cmd/previewmesh/main.go"
        if not cli.exists() or '"domain-suffix"' not in cli.read_text():
            raise SetupError("Control code predates this installer. Use scripts/update-upstream.sh in control, review and publish that update, then rerun.\ncontrol 代码版本过旧。请在 control 中运行 scripts/update-upstream.sh，审核并发布更新后重新安装。")
        changes = self.run(["git", "-C", self.c.control_dir, "status", "--porcelain"]).stdout.splitlines()
        expected_hash = self.state.get("registry_hash")
        for change in changes:
            if change[3:] == "config/repositories.json" and expected_hash == fingerprint((self.c.control_dir / change[3:]).read_text()):
                continue
            raise SetupError("Control checkout has local changes; commit or stash them before installation / control 有本地改动，请先提交或暂存后再安装。")
        self.control_id = str(info["id"])
        self.state["control_id"] = self.control_id
        self.save()
        generated = []
        for source in self.c.sources:
            info = self.api(f"repos/{source.repository}")
            if info["full_name"] != source.repository:
                raise SetupError(f"Use canonical GitHub repository name / 请使用 GitHub 的准确仓库名: {info['full_name']}")
            if not info.get("permissions", {}).get("admin"):
                raise SetupError(f"Source admin access required to upload Actions secrets / 上传 Actions Secrets 需要 source 管理权限: {source.repository}")
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
            value = getpass.getpass("Paste token (hidden; Enter cancels) / 粘贴 Token（隐藏，直接回车取消）: ").strip()
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
                f"{source.repository}: create a fine-grained token at {fine}; select only this source. Contents/Metadata: read; Pull requests/Commit statuses: read and write.",
                f"{source.repository}：在 {fine} 创建 fine-grained Token，仅选此 source。Contents/Metadata 只读；Pull requests/Commit statuses 读写。")
            self.run(["gh", "api", f"repos/{source.repository}"], env={"GH_TOKEN": token})
            self.run(["gh", "secret", "set", registration["source_secret"], "--repo", self.c.control, "--app", "actions"], input=token)
        token = self.credential(self.c.dispatch_file,
            f"Dispatch: {fine}; select ONLY {self.c.control}. Actions: read/write; Metadata: read.",
            f"通知 Token：{fine}；仅选 {self.c.control}。Actions 读写；Metadata 只读。")
        self.run(["gh", "api", f"repos/{self.c.control}/actions/workflows"], env={"GH_TOKEN": token})
        for source in self.c.sources:
            self.run(["gh", "secret", "set", "PREVIEWMESH_DISPATCH_TOKEN", "--repo", source.repository, "--app", "actions"], input=token)
        token = self.credential(self.c.ghcr_file,
            "GHCR: https://github.com/settings/tokens/new?scopes=read:packages — use a classic PAT with read:packages and access to the preview packages.",
            "镜像 Token：https://github.com/settings/tokens/new?scopes=read:packages — 使用 classic PAT，选择 read:packages，账号需能读取预览镜像包。")
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
                raise SetupError("Runner credentials and temporary files must be Git-ignored; update control .gitignore / Runner 凭据及临时文件必须被 Git 忽略，请更新 control 的 .gitignore。")
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

    def runner(self):
        directory = self.home / "runner"
        name = "previewmesh-" + self.control_id + "-" + re.sub(r"[^a-zA-Z0-9_-]", "-", socket.gethostname())[:40]
        target = f"https://github.com/{self.c.control}"
        if (directory / ".runner").exists():
            settings = json.loads((directory / ".runner").read_text())
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
                app = next(item for item in self.api(f"repos/{self.c.control}/actions/runners/downloads")
                           if item["os"] == "linux" and item["architecture"] == "x64")
                version = re.search(r"actions-runner-linux-x64-(.+)\.tar\.gz", app["filename"])[1]
                spec = runner_release(app, self.api(f"repos/actions/runner/releases/tags/v{version}"))
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

    def network(self):
        # Validate DNS before changing routing. DNS rebinding protection is not bypassed.
        self.check_dns()
        current = self.kube("-n", "kube-system", "get", "helmchartconfig", "traefik",
                            "--ignore-not-found", "-o", "json").stdout.strip()
        manifest = (ROOT / "ops/kubernetes/traefik-helmchartconfig.yaml").read_text()
        expected_values = manifest.split("  valuesContent: |-\n", 1)[1]
        expected_values = "\n".join(line[4:] for line in expected_values.splitlines())
        if current and json.loads(current)["spec"]["valuesContent"].strip() != expected_values.strip():
            raise SetupError("Existing Traefik customization differs; review it before installing / 现有 Traefik 自定义配置不同，请先审核。")
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
            raise SetupError(f"DNS does not resolve {name} to {self.c.lan_ip}. Ask your network administrator whether sslip.io/private-IP DNS is blocked; rerun after resolving it. No hosts fallback was applied.\n域名未解析到配置的局域网 IP。请网管检查 sslip.io 或内网 IP DNS 是否被拦截；解决后重试，不会偷偷改用 hosts。")

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
            self.say(f"{source.repository}: notification file prepared. Commit and publish it through your normal development process.",
                     f"{source.repository}：通知文件已配置好。请通过正常开发流程自行提交和发布。")
            if not (source.directory / "Dockerfile").exists():
                self.say("Application still needs a root Dockerfile.", "应用仍需在根目录提供 Dockerfile。")
        self.say("Applications must listen on 0.0.0.0, run as UID/GID 65532 and return status=ok plus PREVIEW_COMMIT_SHA at GET /health.",
                 "应用需监听 0.0.0.0、支持 UID/GID 65532，并在 GET /health 返回 status=ok 和来自 PREVIEW_COMMIT_SHA 的 commit_sha。")

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
            require(info["private"], "Control must remain private / control 必须保持私有。")
            require(self.api(f"repos/{self.c.control}/actions/permissions")["enabled"],
                    "Control Actions disabled / control Actions 未启用。")
            variables = self.api(f"repos/{self.c.control}/actions/variables?per_page=100")["variables"]
            values = {item["name"]: item["value"] for item in variables}
            require(values.get("PREVIEWMESH_ENABLED") == "true" and values.get("PREVIEWMESH_DOMAIN_SUFFIX") == self.c.suffix,
                    "Control variables differ / control 配置变量不匹配。")
            registry = json.loads((self.c.control_dir / "config/repositories.json").read_text())
            secrets = self.api(f"repos/{self.c.control}/actions/secrets?per_page=100")["secrets"]
            require({"GHCR_READ_TOKEN", *(item["source_secret"] for item in registry)} <= {item["name"] for item in secrets},
                    "Control secrets missing / control 缺少 Secrets。")
        check("Control configuration / Control 配置", github_check)
        self.pending = []
        for source in self.c.sources:
            def source_check(source=source):
                values = self.api(f"repos/{source.repository}/actions/secrets?per_page=100")["secrets"]
                require("PREVIEWMESH_DISPATCH_TOKEN" in {item["name"] for item in values},
                        "Source dispatch secret missing / source 缺少通知 Secret。")
                require(self.api(f"repos/{source.repository}/actions/permissions")["enabled"],
                        "Source Actions disabled / source Actions 未启用。")
            check(source.repository + " Actions", source_check)
            result = self.run(["gh", "api", f"repos/{source.repository}/contents/.github/workflows/previewmesh-notify.yml"], check=False)
            if result.returncode and "HTTP 404" not in result.stderr:
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
                self.say(f"PENDING: {source.repository} notification is not published on its default branch. Publish the generated file yourself.",
                         f"待处理：{source.repository} 默认分支上尚未发布所需通知文件，请自行提交并发布生成的文件。")
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
        # One install per account; checkpoints are hints and every step rechecks reality.
        with (self.home / "install.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SetupError("Another installer is running / 另一个安装进程正在运行。")
            stages = [
                ("Prepare tools so this computer can install PreviewMesh.", "准备工具，让这台电脑能安装 PreviewMesh。", self.dependencies),
                ("Prepare the private control repository that coordinates builds and deployments.", "准备私有 control 仓库，用来协调构建和部署。", self.control),
                ("Save access tokens so GitHub and the cluster can access the right repositories and images.", "保存访问凭据，让 GitHub 与集群能访问指定仓库和镜像。", self.secrets_step),
                ("Prepare the local cluster and renewable, restricted deployment access.", "准备本机集群和可自动续期的受限部署权限。", self.cluster),
                ("Connect the deployment assistant to GitHub.", "将本机部署助手连接到 GitHub。", self.runner),
                ("Make the preview entry reachable on your LAN.", "配置局域网入口，让同事能访问预览。", self.network),
                ("Prepare application notification files; leave source Git operations to you.", "生成应用通知文件，后续 source Git 操作由你完成。", self.sources),
                ("Enable control automation and check infrastructure readiness.", "启用 control 自动化，并检查基础环境是否就绪。", self.enable),
            ]
            for number, (en, zh, action) in enumerate(stages, 1):
                self.step(number, en, zh, action)


def main():
    parser = argparse.ArgumentParser(description="Guided PreviewMesh setup / PreviewMesh 安装")
    parser.add_argument("command", nargs="?", choices=("init", "check", "install", "doctor"), default="install")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--verbose", action="store_true", help="Show redacted tool output / 显示脱敏工具输出")
    args = parser.parse_args()
    installer = None
    try:
        if args.command == "init":
            path = args.config.expanduser().absolute()
            if path.exists() or path.is_symlink():
                raise SetupError(f"Already exists; edit this file / 文件已存在，请直接编辑: {path}")
            atomic_write(path, (ROOT / "config/setup.example.ini").read_text())
            print(f"Edit / 请编辑: {path}\nThen / 然后执行: bash {shlex.quote(str(ROOT / 'scripts/setup.sh'))} install --config {shlex.quote(str(path))}")
            return 0
        config = load_config(args.config, ROOT)
        installer = Installer(config, args.command, args.verbose)
        getattr(installer, args.command)()
        return 0
    except (SetupError, OSError, ValueError, KeyError, StopIteration) as error:
        # Avoid tracebacks with local values or raw secret-bearing subprocess output.
        message = str(error) if not isinstance(error, (KeyError, StopIteration)) else "Unexpected remote response / 远端返回信息不符合预期。"
        print(installer.redact(message) if installer else message, file=sys.stderr)
        if args.command == "install":
            print(f"Fix the issue, then rerun the SAME command / 处理问题后重新运行同一命令:\n"
                  f"bash {shlex.quote(str(ROOT / 'scripts/setup.sh'))} install --config {shlex.quote(str(args.config))}", file=sys.stderr)
            if installer:
                print(f"Redacted log / 脱敏日志: {installer.log_path}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nStopped; rerun the same command to continue / 已停止，重新运行同一命令即可继续。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
