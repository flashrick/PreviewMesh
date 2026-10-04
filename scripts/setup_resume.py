"""Versioned checkpoints; only independently verifiable stages may be reused."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import re
import stat

from setup_config import SetupError, fingerprint

SCHEMA = 1
REUSABLE = {2, 7}


def digest(value):
    return fingerprint(json.dumps(value, sort_keys=True, default=str))


def file_record(path):
    path = Path(path)
    if path.is_symlink():
        raise SetupError("Artifact is a symlink; inspect and restore a regular file / 产物为符号链接，请检查并恢复为普通文件。")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not info.st_size:
        raise SetupError("Artifact is empty or not a regular file / 产物为空或不是普通文件。")
    with path.open("rb") as stream:
        hasher = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
        checksum = hasher.hexdigest()
    return {"sha256": checksum, "mode": stat.S_IMODE(info.st_mode), "size": info.st_size}


class Checkpoints:
    def __init__(self, installer, root):
        self.i, self.root = installer, root

    def inputs(self, number):
        config = asdict(self.i.c)
        # Presentation and config-file location do not affect installation output.
        for key in ("path", "language"):
            config.pop(key, None)
        if number in (2, 7):
            for key in ("lan_ip", "subnet", "domain_suffix", "dispatch_file", "ghcr_file"):
                config.pop(key, None)
            for source in config["sources"]:
                source.pop("token_file", None)
        scripts = {str(p.relative_to(self.root)): file_record(p)["sha256"]
                   for pattern in ("scripts/setup*.py", "scripts/setup.sh", "templates/*",
                                   "ops/kubernetes/*", "ops/wsl/*")
                   for p in self.root.glob(pattern) if p.is_file()}
        versions = {"python": platform.python_version(), "platform": platform.platform()}
        for name, args in (("git", ["--version"]), ("go", ["version"]),
                           ("gh", ["--version"]), ("helm", ["version", "--short"])):
            if self.i.tool(name):
                result = self.i.run([name, *args])
                # Persist hashes, never arbitrary command output or credential material.
                version = re.search(r"\d+\.\d+(?:\.\d+)?", result.stdout)
                versions[name] = {"version": version[0] if version else "unknown",
                                  "output": digest(result.stdout.strip()),
                                  "binary": file_record(Path(self.i.tool(name)).resolve())}
        return {"configuration": digest(config), "scripts": digest(scripts),
                "versions": versions}

    def artifacts(self, number):
        i = self.i
        paths = []
        if number == 2:
            paths = [i.c.control_dir / "bin/control", i.c.control_dir / "config/repositories.json"]
            tracked = i.run(["git", "-C", i.c.control_dir, "ls-files", "-z"]).stdout
            paths += [i.c.control_dir / name for name in tracked.split("\0") if name]
        elif number == 7:
            paths = [s.directory / ".github/workflows/previewmesh-notify.yml" for s in i.c.sources]
        # Empty tracked files are valid inputs; required stage outputs must be nonempty.
        result = {}
        for path in dict.fromkeys(paths):
            if (not path.is_symlink() and path.is_file() and path.stat().st_size == 0
                    and number == 2 and path not in paths[:2]):
                result[str(path)] = {"sha256": hashlib.sha256(b"").hexdigest(),
                                     "mode": stat.S_IMODE(path.stat().st_mode), "size": 0}
            else:
                result[str(path)] = file_record(path)
        return result

    def control_valid(self):
        i = self.i
        info = i.api(f"repos/{i.c.control}")
        if (not info["private"] or not info.get("permissions", {}).get("admin")
                or str(info["id"]) != i.control_id or info["default_branch"] != "main"):
            return False
        origin = i.run(["git", "-C", i.c.control_dir, "remote", "get-url", "origin"]).stdout.strip()
        branch = i.run(["git", "-C", i.c.control_dir, "branch", "--show-current"]).stdout.strip()
        head = i.run(["git", "-C", i.c.control_dir, "rev-parse", "HEAD"]).stdout.strip()
        remote = i.run(["git", "-C", i.c.control_dir, "ls-remote", "origin", "refs/heads/main"]).stdout.split()
        if not i.origin_matches(origin, i.c.control) or branch != "main" or not remote or remote[0] != head:
            return False
        # Untracked build inputs and index changes are not covered by file hashes.
        if i.run(["git", "-C", i.c.control_dir, "status", "--porcelain"]).stdout.strip():
            return False
        registry = json.loads((i.c.control_dir / "config/repositories.json").read_text())
        registrations = []
        for source in i.c.sources:
            info = i.api(f"repos/{source.repository}")
            origin = i.run(["git", "-C", source.directory, "remote", "get-url", "origin"]).stdout.strip()
            expected = {"repository_id": str(info["id"]), "source_repository": source.repository,
                        "port": source.port, "source_secret": "SOURCE_REPO_" + str(info["id"])}
            if (not info.get("permissions", {}).get("admin") or info["full_name"] != source.repository
                    or not i.origin_matches(origin, source.repository) or expected not in registry):
                return False
            registrations.append(expected)
        i.registrations = registrations
        return True

    def assess(self, number, inputs):
        record = self.i.state.get("stages", {}).get(str(number))
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            return False, "no verifiable checkpoint / 无可验证的检查点"
        if record.get("status") != "complete":
            return False, "previous run interrupted or failed / 上次执行中断或失败"
        if not isinstance(record.get("inputs"), dict):
            return False, "checkpoint is incomplete; rebuilding evidence / 检查点不完整，重新建立校验记录"
        for key, label in (("configuration", "configuration changed / 配置变化"),
                           ("scripts", "installer or templates changed / 安装脚本或模板变化"),
                           ("versions", "dependency versions changed / 依赖版本变化")):
            if record.get("inputs", {}).get(key) != inputs[key]:
                return False, label
        if number not in REUSABLE:
            return False, "live credentials or runtime state require revalidation / 凭据或运行状态需要现场重新校验"
        try:
            current = self.artifacts(number)
            if not current or current != record.get("artifacts"):
                return False, "artifact missing, incomplete or changed / 产物缺失、不完整或内容变化"
            if number == 2 and not self.control_valid():
                return False, "repository identity, access or published revision changed / 仓库身份、权限或已发布版本变化"
        except (OSError, ValueError, KeyError, TypeError, SetupError):
            # Raw API/errors may include secrets; reasons are deliberately fixed strings.
            return False, "artifact or remote validation failed; check files and access / 产物或远端校验失败，请检查文件及访问权限"
        return True, "artifacts, inputs and versions verified / 产物、输入和版本校验通过"

    def record(self, number, status, inputs, artifacts=None):
        self.i.state.setdefault("stages", {})[str(number)] = {
            "schema": SCHEMA, "status": status, "inputs": inputs,
            "artifacts": artifacts or {},
        }
        self.i.save()
