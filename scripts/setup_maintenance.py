#!/usr/bin/env python3
"""Root-owned maintenance entry point. Never execute code from a runner checkout."""
import argparse
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

CONFIG = Path("/etc/previewmesh/installation.json")
ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def trusted(path):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError("Managed system paths must not be symlinks.")
    for item in [path, *path.parents]:
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("Managed system files and ancestors must be root-owned and not writable by others.")


def claims(token):
    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
    return int(payload["exp"]), int(payload["iat"])


def expiry(path):
    # Called only after dropping privileges. Read a bounded regular file.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("Credential file ownership/permissions are invalid.")
        value = json.loads(stream.read(1024 * 1024))
    return claims(value["users"][0]["user"]["token"])


def needs_renewal(expires, issued, now=None):
    now = time.time() if now is None else now
    return expires - now <= max(600, (expires - issued) / 3)


def drop_privileges(config):
    os.setgroups([])
    os.setgid(config["gid"])
    os.setuid(config["uid"])


def capture(args, accepted=(0,)):
    completed = subprocess.run(args, capture_output=True, text=True, env=ENVIRONMENT, timeout=60)
    if completed.returncode not in accepted:
        # Kubernetes responses can carry sensitive data; do not include raw output.
        raise RuntimeError("Kubernetes operation failed; existing credential retained.")
    return completed


def replace_credential(path, value, client):
    """Runs as the runner user; a writable checkout cannot redirect root writes."""
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError("Credential destination must not be a symlink.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".runner-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2)
            stream.write("\n")
        for verb, resource, extra in (
            ("create", "namespaces", []), ("delete", "namespaces", []),
            ("create", "secrets", ["--all-namespaces"]), ("create", "clusterroles", []),
        ):
            checked = capture([*client, "--kubeconfig", temporary, "auth", "can-i", verb, resource, *extra], (0, 1))
            expected = (1, "no") if resource == "clusterroles" else (0, "yes")
            if (checked.returncode, checked.stdout.strip()) != expected:
                raise RuntimeError("Runner permission check failed; existing credential retained.")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def firewall_rules(address, subnet):
    address = ipaddress.IPv4Address(address)
    network = ipaddress.IPv4Network(subnet, strict=True)
    private = [ipaddress.ip_network(item) for item in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    if address not in network or not any(network.subnet_of(item) for item in private):
        raise RuntimeError("Invalid private LAN firewall configuration.")
    return ("*filter\n:PREVIEWMESH-IN - [0:0]\n-F PREVIEWMESH-IN\n"
            "-A PREVIEWMESH-IN -i lo -j ACCEPT\n"
            f"-A PREVIEWMESH-IN -s {network} -d {address} -j ACCEPT\n"
            "-A PREVIEWMESH-IN -m comment --comment previewmesh-managed -j DROP\nCOMMIT\n")


def configure_firewall(policy):
    if not policy:
        return
    binary, restore = "/usr/sbin/iptables", "/usr/sbin/iptables-restore"
    trusted(Path(binary).resolve())
    trusted(Path(restore).resolve())
    current = capture([binary, "-w", "10", "-S", "PREVIEWMESH-IN"], (0, 1))
    rules = firewall_rules(policy["address"], policy["subnet"])
    if current.returncode == 0 and "previewmesh-managed" not in current.stdout:
        raise RuntimeError("An unmanaged PREVIEWMESH-IN firewall chain already exists.")
    # --noflush preserves other rules; the transaction only replaces our named chain.
    result = subprocess.run([restore, "-w", "10", "--noflush"], input=rules, text=True,
                            env=ENVIRONMENT, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Could not configure the PreviewMesh firewall chain.")
    hook = ["INPUT", "-p", "tcp", "--dport", "18080", "-j", "PREVIEWMESH-IN"]
    if capture([binary, "-w", "10", "-C", *hook], (0, 1)).returncode:
        capture([binary, "-w", "10", "-I", "INPUT", "1", *hook[1:]])


def refresh_ingress(admin):
    response = capture([*admin, "-n", "kube-system", "get", "service", "traefik", "-o", "json"])
    spec = json.loads(response.stdout)["spec"]
    if spec["type"] != "ClusterIP":
        raise RuntimeError("Traefik is not an internal ClusterIP service.")
    address = str(ipaddress.IPv4Address(spec["clusterIP"]))
    path = Path("/etc/previewmesh/ingress.env")
    trusted(path.parent)
    if path.exists():
        trusted(path)
    content = f"PREVIEWMESH_TRAEFIK_ENDPOINT={address}:80\n"
    if path.exists() and path.read_text() == content:
        return
    fd, temporary = tempfile.mkstemp(prefix=".ingress-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # The socket starts this service on demand; no service may exist on first setup.
    active = subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", "previewmesh-ingress.service"],
                            env=ENVIRONMENT, capture_output=True, timeout=30)
    if active.returncode == 0:
        subprocess.run(["/usr/bin/systemctl", "restart", "previewmesh-ingress.service"],
                       env=ENVIRONMENT, capture_output=True, check=True, timeout=30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--refresh-ingress", action="store_true")
    parser.add_argument("--inspect", type=Path)
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(expiry(args.inspect)))
        return
    if os.getuid() != 0:
        raise RuntimeError("Maintenance must run from its root-owned service.")
    trusted(CONFIG)
    config = json.loads(CONFIG.read_text())
    if config["uid"] <= 0 or config["gid"] <= 0:
        raise RuntimeError("Runner must be an unprivileged user.")
    binary = Path(config["k3s"])
    trusted(binary)
    control = Path(config["control_dir"])
    output = Path(config["kubeconfig"])
    if not output.is_absolute() or output != control / "config/previewmesh-runner.yaml":
        raise RuntimeError("Credential output must be the fixed private control path.")
    os.chdir("/")
    with open("/run/previewmesh-maintenance.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        admin = [str(binary), "kubectl", "--kubeconfig=/etc/rancher/k3s/k3s.yaml"]
        # Network failure must not prevent renewing the deployment credential.
        network_error = None
        try:
            configure_firewall(config.get("firewall"))
        except Exception as error:
            network_error = error
        if args.refresh_ingress or Path("/etc/previewmesh/ingress.env").exists():
            try:
                refresh_ingress(admin)
            except Exception as error:
                network_error = error
        if args.refresh_ingress:
            if network_error:
                raise network_error
            return
        # Read runner-controlled files as the runner, never as root.
        read = subprocess.run(["/usr/bin/python3", str(Path(__file__).resolve()), "--inspect", str(output)],
                              preexec_fn=lambda: drop_privileges(config), env=ENVIRONMENT,
                              capture_output=True, text=True, timeout=30)
        renew = True
        if read.returncode == 0 and not args.force:
            renew = needs_renewal(*json.loads(read.stdout))
        if not renew:
            if network_error:
                raise network_error
            print("Runner access remains valid; renewal is not yet due.")
            return
        cluster = json.loads(capture([*admin, "config", "view", "--raw", "--minify", "-o", "json"]).stdout)["clusters"][0]["cluster"]
        token = capture([*admin, "-n", "previewmesh-system", "create", "token", "previewmesh-runner", "--duration=24h"]).stdout.strip()
        expires, _ = claims(token)
        if expires - time.time() < 900:
            raise RuntimeError("API server granted less than 15 minutes; adjust its token policy.")
        value = {
            "apiVersion": "v1", "kind": "Config",
            "clusters": [{"name": "k3s", "cluster": {"server": cluster["server"],
                                                    "certificate-authority-data": cluster["certificate-authority-data"]}}],
            "users": [{"name": "previewmesh-runner", "user": {"token": token}}],
            "contexts": [{"name": "previewmesh-runner", "context": {"cluster": "k3s", "user": "previewmesh-runner"}}],
            "current-context": "previewmesh-runner",
        }
        # Permanently give up root before writing under the private checkout.
        drop_privileges(config)
        for candidate in (output, output.parent / ".runner-check"):
            ignored = subprocess.run(["/usr/bin/git", "-C", str(control), "check-ignore", "--quiet", str(candidate)],
                                     env=ENVIRONMENT, capture_output=True)
            tracked = subprocess.run(["/usr/bin/git", "-C", str(control), "ls-files", "--error-unmatch", str(candidate)],
                                     env=ENVIRONMENT, capture_output=True)
            if ignored.returncode or tracked.returncode == 0:
                raise RuntimeError("Credential paths must remain untracked and Git-ignored.")
        replace_credential(output, value, [str(binary), "kubectl"])
        print("Renewed restricted runner access; expires at " + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(expires)))
        if network_error:
            raise network_error


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        if isinstance(error, RuntimeError):
            print(str(error), file=sys.stderr)
        print("PreviewMesh maintenance failed. Run setup.sh doctor; check K3s, file ownership, Git ignore rules and token lifetime. Failed renewal does not replace an existing credential.", file=sys.stderr)
        sys.exit(1)
