#!/usr/bin/env python3
"""Generate or renew a restricted runner kubeconfig on the K3s machine."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import shutil
import sys


if len(sys.argv) != 1:
    print('Usage: PREVIEWMESH_RUNNER_CONFIG=/absolute/path python3 scripts/configure-runner.py')
    raise SystemExit(0 if sys.argv[1:] == ['--help'] else 2)
if not os.environ.get('PREVIEWMESH_RUNNER_CONFIG'):
    raise SystemExit('Set PREVIEWMESH_RUNNER_CONFIG to an absolute output path first.')
path = Path(os.environ['PREVIEWMESH_RUNNER_CONFIG'])
if not path.is_absolute():
    raise SystemExit('PREVIEWMESH_RUNNER_CONFIG must be an absolute path.')
for tool in ('sudo', 'git', 'kubectl'):
    if not shutil.which(tool):
        raise SystemExit(f'Missing tool: {tool}. Install it before retrying.')
root = Path(__file__).resolve().parents[1]
path = path.resolve()
if not path.is_relative_to(root):
    raise SystemExit('Runner configuration must be inside the control checkout.')
# Refuse tracked or unignored destinations before requesting a credential.
for candidate in (path, path.parent / '.runner-check'):
    tracked = subprocess.run(['git', '-C', str(root), 'ls-files', '--error-unmatch', str(candidate)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ignored = subprocess.run(['git', '-C', str(root), 'check-ignore', '--quiet', str(candidate)])
    if tracked.returncode == 0 or ignored.returncode != 0:
        raise SystemExit(f'Output and temporary files must be untracked and Git-ignored: {candidate}')
path.parent.mkdir(parents=True, exist_ok=True)

# Read only cluster connection data; never copy administrator credentials.
admin = ["sudo", "k3s", "kubectl", "--kubeconfig=/etc/rancher/k3s/k3s.yaml"]
def capture(*args):
    return subprocess.check_output([*admin, *args], text=True).strip()

cluster = json.loads(capture("config", "view", "--raw", "--minify", "-o", "json"))["clusters"][0]["cluster"]
token = capture("-n", "previewmesh-system", "create", "token", "previewmesh-runner", "--duration=24h")
if not token:
    raise SystemExit('Kubernetes returned an empty runner token.')
config = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": [{"name": "k3s", "cluster": {
        "server": cluster["server"],
        "certificate-authority-data": cluster["certificate-authority-data"],
    }}],
    "users": [{"name": "previewmesh-runner", "user": {"token": token}}],
    "contexts": [{"name": "previewmesh-runner", "context": {
        "cluster": "k3s", "user": "previewmesh-runner",
    }}],
    "current-context": "previewmesh-runner",
}
# JSON is valid YAML. Write with mode 600 and replace only after success.
fd, temporary = tempfile.mkstemp(prefix=".runner-", dir=path.parent)
try:
    with os.fdopen(fd, "w") as output:
        json.dump(config, output, indent=2)
        output.write("\n")
    # Validate the new credential before replacing a working configuration.
    for verb, resource, extra in [
        ('create', 'namespaces', []),
        ('delete', 'namespaces', []),
        ('create', 'secrets', ['--all-namespaces']),
        ('create', 'clusterroles', []),
    ]:
        check = subprocess.run(
            ['kubectl', '--kubeconfig', temporary, 'auth', 'can-i', verb, resource, *extra],
            text=True, capture_output=True,
        )
        expected = (1, 'no') if resource == 'clusterroles' else (0, 'yes')
        if (check.returncode, check.stdout.strip()) != expected:
            raise SystemExit(f'Runner permission check failed: {verb} {resource}; existing file unchanged.')
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print(f"Created runner kubeconfig: {path}")
