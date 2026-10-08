#!/usr/bin/env python3
"""Prepare one internal Traefik route when mirrored WSL ingress starts."""
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import time

ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def trusted(path):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError("Ingress tools must not resolve to symlinks.")
    for item in [path, *path.parents]:
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("Ingress tools and parents must be root-owned and not writable by others.")


def capture(args, accepted=(0,)):
    result = subprocess.run(args, capture_output=True, text=True, env=ENVIRONMENT, timeout=10)
    if result.returncode not in accepted:
        raise RuntimeError("Could not prepare the mirrored WSL ingress route.")
    return result


def prepare_ingress_route(endpoint):
    if "microsoft" not in Path("/proc/sys/kernel/osrelease").read_text().lower():
        return
    # Keep the wslinfo symlink as argv[0]; /init selects its command from it.
    trusted(Path("/usr/bin/wslinfo").resolve())
    if capture(["/usr/bin/wslinfo", "--networking-mode"]).stdout.strip() != "mirrored":
        return
    host, separator, port = (endpoint or "").rpartition(":")
    if not separator or port != "80":
        raise RuntimeError("A private Traefik IPv4 endpoint on port 80 is required.")
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        raise RuntimeError("A private Traefik IPv4 endpoint on port 80 is required.") from None
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise RuntimeError("A private Traefik IPv4 endpoint on port 80 is required.")
    binary = "/usr/sbin/ip"
    trusted(Path(binary).resolve())
    deadline = time.monotonic() + 60
    source = None
    # K3s becoming active does not mean its Pod bridge has been created yet.
    while source is None:
        response = capture([binary, "-j", "-4", "address", "show", "dev", "cni0"], (0, 1))
        if response.returncode == 0:
            for interface in json.loads(response.stdout):
                for item in interface.get("addr_info", []):
                    if item.get("family") == "inet" and item.get("scope") == "global":
                        candidate = ipaddress.IPv4Address(item["local"])
                        if (candidate.is_private and not candidate.is_loopback and
                                not candidate.is_link_local and not candidate.is_unspecified):
                            source = str(candidate)
                            break
                if source is not None:
                    break
        if source is not None:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("K3s Pod bridge cni0 did not become ready for the preview entry.")
        time.sleep(1)
    destination = str(address) + "/32"
    current = capture([binary, "-j", "-4", "route", "show", "table", "main", "exact", destination])
    routes = json.loads(current.stdout)
    if routes:
        if len(routes) == 1 and routes[0].get("dev") == "cni0" and routes[0].get("prefsrc") == source:
            return
        raise RuntimeError("Traefik already has a different host route; review it before changing ingress routing.")
    # A LAN source sends Service replies to Windows in mirrored mode. Select a
    # local source only for this Service IP, preserving all unrelated routes.
    capture([binary, "-4", "route", "add", destination, "dev", "cni0", "src", source])


def main():
    if os.getuid() != 0:
        raise RuntimeError("Ingress routing must run from its root-owned startup helper.")
    prepare_ingress_route(os.environ.get("PREVIEWMESH_TRAEFIK_ENDPOINT"))


if __name__ == "__main__":
    main()
