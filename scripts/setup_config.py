"""Configuration and safe local I/O shared by the guided installer and its tests."""
import configparser
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile

REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
RFC1918 = tuple(ipaddress.ip_network(value) for value in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
ACCESS_GUIDE = "https://github.com/flashrick/PreviewMesh/blob/main/ops/install/access.md"


def validate_domain_suffix(value):
    # Reserve space for the longest supported preview identity and separating dot.
    if value != "auto" and (len(value) > 199 or any(
            len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in value.split("."))):
        raise SetupError("domain_suffix must be auto or a lowercase DNS suffix / 域名后缀需为 auto 或合法小写 DNS 后缀。")
    return value


class SetupError(Exception):
    """An actionable error whose message contains no credential values."""


def atomic_write(path, content, mode=0o600):
    """Replace only this file, never follow a destination symlink."""
    path = Path(path)
    if path.is_symlink():
        raise SetupError(f"Refusing symlink / 拒绝符号链接: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".previewmesh-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fingerprint(content):
    return hashlib.sha256(content.encode()).hexdigest()


def path_value(value, base):
    if any(char in value for char in "\n\r\0") or "$" in value:
        raise SetupError("Use a plain path, not a shell expression / 路径不能使用 Shell 表达式。")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    if path.is_symlink():
        raise SetupError(f"Refusing symlink / 拒绝符号链接: {path}")
    return path.resolve()


def validate_repo(value):
    if not REPOSITORY.fullmatch(value) or "YOUR_" in value:
        raise SetupError("Fill repository as OWNER/NAME / 请将仓库填写为实际的 OWNER/NAME。")
    return value


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


@dataclass
class Source:
    name: str
    repository: str
    directory: Path
    port: int
    token_file: Path


@dataclass
class Config:
    path: Path
    control: str
    control_dir: Path
    public: str
    language: str
    lan_ip: str
    subnet: str
    dispatch_file: Path
    ghcr_file: Path
    sources: list
    domain_suffix: str = "auto"

    @property
    def suffix(self):
        return self.lan_ip + ".sslip.io" if self.domain_suffix == "auto" else self.domain_suffix

    @property
    def runner_config(self):
        return self.control_dir / "config/previewmesh-runner.yaml"


def load_config(path, root, *, content=None):
    path = Path(path).expanduser().resolve()
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        if content is None:
            with path.open(encoding="utf-8") as stream:
                parser.read_file(stream)
        else:
            parser.read_string(content)
    except FileNotFoundError:
        raise SetupError(f"Configuration missing / 缺少配置: {path}\nRun / 执行: bash scripts/setup.sh init")
    except configparser.Error:
        raise SetupError(f"Invalid INI configuration / INI 格式错误: {path}")
    allowed = {
        "project": {"control_repository", "control_directory", "public_repository", "language"},
        "network": {"lan_ip", "allowed_subnet", "domain_suffix"},
        "credentials": {"dispatch_token_file", "ghcr_token_file"},
    }
    if parser.defaults():
        raise SetupError("[DEFAULT] is not supported / 不支持 DEFAULT 配置段。")
    for section in parser.sections():
        keys = {"repository", "directory", "port", "token_file"} if section.startswith("source:") else allowed.get(section)
        if keys is None or set(parser[section]) - keys:
            raise SetupError(f"Unknown section or option / 未知配置段或配置项: {section}")
    def value(section, key, default=None):
        answer = parser.get(section, key, fallback=default)
        if not answer:
            raise SetupError(f"Required / 必填: [{section}] {key}")
        return answer.strip()
    def location(section, key):
        return path_value(value(section, key), path.parent)
    control = validate_repo(value("project", "control_repository"))
    public = validate_repo(value("project", "public_repository", "flashrick/PreviewMesh"))
    control_dir = location("project", "control_directory")
    language = value("project", "language", "auto")
    if language not in ("auto", "zh-CN", "en"):
        raise SetupError("language must be auto, zh-CN or en / 输出语言配置无效。")
    try:
        address = ipaddress.IPv4Address(value("network", "lan_ip"))
        if not any(address in net for net in RFC1918):
            raise ValueError()
        subnet = value("network", "allowed_subnet", "auto")
        if subnet != "auto":
            network = ipaddress.IPv4Network(subnet, strict=True)
            if not any(network.subnet_of(net) for net in RFC1918) or address not in network:
                raise ValueError()
    except ValueError:
        raise SetupError("Fill a private LAN IPv4 and its subnet / 请填写真实局域网 IPv4 及其网段，不要填写回环或公网地址。")
    sources = []
    for section in parser.sections():
        if not section.startswith("source:"):
            continue
        name = section.split(":", 1)[1]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise SetupError("Invalid application section name / 应用配置段 source 的名称无效。")
        try:
            port = int(value(section, "port"))
            if not 1 <= port <= 65535:
                raise ValueError()
        except ValueError:
            raise SetupError(f"[{section}] port must be 1..65535 / 应用端口无效。")
        sources.append(Source(name, validate_repo(value(section, "repository")),
                              location(section, "directory"), port, location(section, "token_file")))
    if not sources:
        raise SetupError("Add at least one application [source:name] / 至少填写一个应用配置段 [source:name]。")
    repos = [control, public] + [source.repository for source in sources]
    if len({repo.lower() for repo in repos}) != len(repos):
        raise SetupError("Deployment management, PreviewMesh code and application repositories must differ / 部署管理仓库、PreviewMesh 代码仓库与各应用源码仓库不能重复。")
    directories = [Path(root).resolve(), control_dir] + [source.directory for source in sources]
    # Running the same installer inside control is supported after initial creation.
    if directories[0] == control_dir:
        directories = directories[1:]
    for index, directory in enumerate(directories):
        if any(overlaps(directory, other) for other in directories[index + 1:]):
                raise SetupError("Repository local directories must not overlap / 各仓库的本地目录不能相同或互相包含。")
    dispatch = location("credentials", "dispatch_token_file")
    ghcr = location("credentials", "ghcr_token_file")
    credentials = [dispatch, ghcr] + [source.token_file for source in sources]
    if len(set(credentials)) != len(credentials):
        raise SetupError("Each token needs a separate file / 每个 Token 必须使用独立文件。")
    for token_path in credentials:
        if any(token_path == directory or directory in token_path.parents for directory in directories):
            raise SetupError("Keep token files outside Git checkouts / Token 文件必须放在仓库目录之外。")
    return Config(path, control, control_dir, public, language, str(address), subnet,
                  dispatch, ghcr, sources, validate_domain_suffix(value("network", "domain_suffix", "auto")))


def read_token(path):
    """Do not print values or allow credentials from symlinks/shared files."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise SetupError(f"Cannot read token file / 无法读取 Token 文件: {path}")
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SetupError(f"Token must be owned by you with mode 600 / Token 文件需归当前用户所有、权限为 600: {path}")
        token = stream.read(16385).strip()
    if not token or len(token) > 16384 or any(char.isspace() for char in token):
        raise SetupError(f"Token file is empty or malformed / Token 文件为空或格式错误: {path}")
    # Even an untracked file in another checkout can later be committed.
    if any((parent / ".git").is_file() or (parent / ".git/HEAD").is_file() for parent in path.parents):
        raise SetupError(f"Token file is inside a Git checkout / Token 文件位于 Git 仓库中: {path}")
    return token


def merge_registry(existing, registrations):
    """Preserve unrelated registrations, but never reassign a known identity."""
    if not isinstance(existing, list):
        raise SetupError("Invalid existing registry / 现有登记文件格式错误。")
    result = [dict(item) for item in existing]
    for item in registrations:
        matches = [old for old in result if str(old.get("repository_id")) == item["repository_id"]
                   or old.get("source_repository", "").lower() == item["source_repository"].lower()]
        if len(matches) > 1:
            raise SetupError("Duplicate registry identity / 登记身份重复。")
        if matches:
            old = matches[0]
            if str(old["repository_id"]) != item["repository_id"] or old["source_repository"] != item["source_repository"]:
                raise SetupError("Repository identity changed; review registration / 仓库身份发生变化，请检查登记。")
            # Preserve established secret names for existing manual installations.
            item = dict(item, source_secret=old["source_secret"])
            result[result.index(old)] = item
        else:
            result.append(item)
    ids, names, secrets = set(), set(), set()
    for item in result:
        rid, name, secret = str(item.get("repository_id", "")), item.get("source_repository", ""), item.get("source_secret", "")
        if (not re.fullmatch(r"[1-9][0-9]*", rid) or not REPOSITORY.fullmatch(name)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]*", secret)
                or secret in {"GHCR_READ_TOKEN", "PREVIEWMESH_DISPATCH_TOKEN"}
                or secret.startswith("GITHUB_")
                or rid in ids or name.lower() in names or secret in secrets
                or type(item.get("port")) is not int or not 1 <= item["port"] <= 65535):
            raise SetupError("Invalid or conflicting registry / 登记配置无效或冲突。")
        ids.add(rid)
        names.add(name.lower())
        secrets.add(secret)
    return result
