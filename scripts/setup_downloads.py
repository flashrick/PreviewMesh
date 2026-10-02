"""Official release downloads with checksums and bounded, safe extraction."""
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import urllib.request

from setup_config import SetupError


def fetch(url):
    if not url.startswith("https://"):
        raise SetupError("Downloads require HTTPS / 下载必须使用 HTTPS。")
    request = urllib.request.Request(url, headers={"User-Agent": "PreviewMesh-setup",
                                                   "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if not response.url.startswith("https://"):
                raise SetupError("Insecure download redirect / 下载重定向不安全。")
            return response.read()
    except (OSError, ValueError):
        raise SetupError(f"Download failed; check network and retry / 下载失败，请检查网络后重试: {url}")


def metadata(url):
    try:
        return json.loads(fetch(url))
    except ValueError:
        raise SetupError(f"Invalid release metadata / 发行信息格式错误: {url}")


def checksum_line(content, filename):
    for line in content.splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(.+)", line.strip())
        if match and match[2].removeprefix("./") == filename:
            return match[1].lower()
    raise SetupError(f"Official checksum missing / 官方校验和缺失: {filename}")


def download_archive(url, checksum, destination):
    if not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise SetupError("Invalid download checksum / 下载校验和无效。")
    data = fetch(url)
    if hashlib.sha256(data).hexdigest() != checksum:
        raise SetupError("Download checksum mismatch; nothing installed / 下载校验失败，未安装。")
    archive = destination / "download.tar.gz"
    archive.write_bytes(data)
    try:
        with tarfile.open(archive, "r:gz") as stream:
            # Official releases are trusted only after checksum verification; reject
            # traversal, links and devices anyway before writing any member.
            members = stream.getmembers()
            names = set()
            for member in members:
                target = destination / member.name
                if (Path(member.name).is_absolute() or ".." in Path(member.name).parts
                        or not (member.isfile() or member.isdir() or member.issym() or member.islnk())
                        or not target.resolve().is_relative_to(destination.resolve())):
                    raise SetupError("Unsafe archive entry / 压缩包包含不安全路径。")
                normalized = str(Path(member.name))
                if normalized in names:
                    raise SetupError("Duplicate archive entry / 压缩包路径重复。")
                names.add(normalized)
                if member.issym() or member.islnk():
                    linked = (target.parent if member.issym() else destination) / member.linkname
                    if Path(member.linkname).is_absolute() or not linked.resolve().is_relative_to(destination.resolve()):
                        raise SetupError("Unsafe archive link / 压缩包链接不安全。")
            stream.extractall(destination, members=[item for item in members if item.isfile() or item.isdir()])
            for member in members:
                target = destination / member.name
                if member.issym() or member.islnk():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    linked = (target.parent if member.issym() else destination) / member.linkname
                    if not linked.resolve().is_relative_to(destination.resolve()):
                        raise SetupError("Unsafe archive link / 压缩包链接不安全。")
                    if member.issym():
                        target.symlink_to(member.linkname)
                    else:
                        os.link(linked, target)
    finally:
        archive.unlink(missing_ok=True)


def tool_release(name):
    """Resolve once; the installer persists this selection before downloading."""
    if name == "go":
        releases = metadata("https://go.dev/dl/?mode=json")
        release = next(item for item in releases if item["stable"])
        file = next(item for item in release["files"] if item["os"] == "linux"
                    and item["arch"] == "amd64" and item["kind"] == "archive")
        return {"version": release["version"], "url": "https://go.dev/dl/" + file["filename"],
                "sha256": file["sha256"], "binary": "go/bin/go"}
    if name == "helm":
        releases = metadata("https://api.github.com/repos/helm/helm/releases?per_page=100")
        release = next(item for item in releases if item["tag_name"].startswith("v3.")
                       and not item["prerelease"] and not item["draft"])
        version = release["tag_name"]
        url = f"https://get.helm.sh/helm-{version}-linux-amd64.tar.gz"
        checksum = fetch(url + ".sha256sum").decode()
        return {"version": version, "url": url,
                "sha256": checksum_line(checksum, url.rsplit("/", 1)[1]), "binary": "linux-amd64/helm"}
    release = metadata("https://api.github.com/repos/cli/cli/releases/latest")
    assets = release["assets"]
    archive = next(item for item in assets if item["name"].endswith("_linux_amd64.tar.gz"))
    checksums = next(item for item in assets if item["name"].endswith("_checksums.txt"))
    checksum = checksum_line(fetch(checksums["browser_download_url"]).decode(), archive["name"])
    folder = archive["name"].removesuffix(".tar.gz")
    return {"version": release["tag_name"], "url": archive["browser_download_url"],
            "sha256": checksum, "binary": folder + "/bin/gh"}


def runner_release(app, release):
    filename = app["filename"]
    asset = next((item for item in release["assets"] if item["name"] == filename), {})
    checksum = app.get("sha256_checksum") or (asset.get("digest") or "").removeprefix("sha256:")
    if not re.fullmatch(r"[a-f0-9]{64}", checksum or ""):
        # Older GitHub releases publish the checksum beside the filename in the body.
        match = re.search(r"([a-f0-9]{64})\s+\*?" + re.escape(filename), release.get("body") or "")
        if not match:
            raise SetupError("Runner checksum missing; check GitHub release / Runner 官方校验和缺失，请检查发行页面。")
        checksum = match[1]
    return {"url": app["download_url"], "sha256": checksum, "version": release["tag_name"]}
