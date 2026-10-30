# Clean up an installation

[Back to README](../../README.md#update-and-clean-up) · [中文](cleanup_CN.md)

## Cleanup levels

If an old installation must be cleaned, use one of the cumulative cleanup levels and inspect the plan with `--dry-run` first:

~~~bash
# Level 1: setup.ini, its draft, and installer state only; keep services,
# Runner files, repositories, and remote resources.
bash scripts/setup.sh cleanup --level 1 --config /path/to/setup.ini --dry-run

# Level 2: level 1 plus local Runner files and PreviewMesh systemd/ingress
# services; keep K3s and the remote Runner registration.
bash scripts/setup.sh cleanup --level 2 --config /path/to/setup.ini --dry-run

# Level 3: level 2 plus configured local control/source Git checkouts.
bash scripts/setup.sh cleanup --level 3 --config /path/to/setup.ini --dry-run
~~~

Remove `--dry-run` only after reviewing the plan. Level 2 does not uninstall K3s or delete the remote GitHub Runner registration; use the separate `--remote-runner` option only when that remote resource may be removed. In non-interactive mode, remote deletion additionally requires `--confirm-remote-runner`. On WSL, level 2 also removes the PreviewMesh Windows firewall rule, port forwarding, scheduled task, and `ProgramData\PreviewMesh` configuration. Level 3 removes only the configured local control/source Git checkouts. It does not delete GitHub repositories, Secrets, workflows, or other remote resources; a checkout with uncommitted, untracked, or ignored files requires explicit `--allow-dirty-repositories`. The public PreviewMesh checkout is always protected.

Token files and PreviewMesh Kubernetes namespaces/RBAC are not included by default. Select them separately with `--include-credentials` and `--cluster`; `--cluster` also leaves K3s installed. The advanced `--select` interface remains available for independent items. Removing local Runner files while keeping the remote registration leaves that Runner offline, so use it only for a migration or an explicitly retired installation.

Choose the highest level you need in one run. Do not run level 1 or 2 first and then expect level 3 to discover the old source paths after `setup.ini` has been removed.

## Cleanup during `init`

If `init` finds an existing PreviewMesh installation record or system service, it reports the detected traces and lets you choose one of three cumulative cleanup levels. The selected cleanup runs immediately, before the new configuration wizard starts. Level 1 resets only local installer settings and state. Level 2 also removes local PreviewMesh services and Runner files, while keeping K3s and the remote Runner registration. Level 3 also removes the configured local control/source checkouts, including dirty working-tree files, after path/origin safety checks and an explicit repository-delete confirmation. After that selected cleanup completes, `init` checks `~/.config/previewmesh/secrets/*.token` and asks whether those token files should also be removed. K3s, GitHub repositories, remote resources, and the new configuration are preserved. An existing Runner for another control repository is otherwise left running while the new install uses a separate local Runner directory.
