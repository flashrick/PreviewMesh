# Docker bridge egress in WSL mirrored networking

Some WSL mirrored-network configurations need an explicit source-port range for
TCP connections leaving Docker's default bridge. PreviewMesh does not require
this helper on every host; use it only after reproducing a Docker-only network
failure.

## Install after validation

Run from the PreviewMesh control checkout in WSL:

```bash
sudo install -m 0755 ops/wsl/docker-egress-ports.sh /usr/local/sbin/previewmesh-docker-egress
sudo install -m 0644 ops/wsl/previewmesh-docker-egress.service /etc/systemd/system/previewmesh-docker-egress.service
sudo systemctl daemon-reload
sudo systemctl enable --now previewmesh-docker-egress.service
```

The helper reads the current WSL ephemeral-port range, Docker's default bridge
subnet, and the interface selected for an external route. It adds a
commented, idempotent TCP MASQUERADE rule only when that rule is absent.

It covers TCP from the default bridge on the selected interface. It does not
cover custom Docker networks, UDP, IPv6, or general firewall policy. Rerun the
service after a routing or interface change. New connections are required when
testing a changed NAT rule; do not flush the machine's connection-tracking
table.

## Remove

Disable the startup helper, inspect the exact rule, and remove only the rule
with the PreviewMesh comment:

```bash
sudo systemctl disable --now previewmesh-docker-egress.service
sudo iptables -t nat -S POSTROUTING | rg previewmesh-wsl-ports
```

Use the printed rule arguments with `-D` instead of `-A` if the rule must be
removed manually.

