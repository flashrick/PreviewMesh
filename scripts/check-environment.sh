#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 [local|cluster|runner|development]"
  echo 'Read-only checks. cluster uses the current operator kubeconfig and sudo for K3s.'
}
[[ $# -le 1 ]] || { usage >&2; exit 2; }
mode=${1:-local}
case "$mode" in
  local) required=(git go python3 bash gh) ;;
  cluster) required=(helm kubectl sudo) ;;
  runner) required=(go python3 helm kubectl) ;;
  development) required=(git go python3 bash helm actionlint) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
missing=0
for tool in "${required[@]}"; do
  if command -v "$tool" >/dev/null 2>&1; then
    printf 'Found %s: %s\n' "$tool" "$(command -v "$tool")"
  else
    printf 'Missing tool: %s. Install it and rerun this check.\n' "$tool" >&2
    missing=1
  fi
done
[[ "$missing" -eq 0 ]] || exit 1

if [[ "$mode" != cluster ]]; then
  python3 - <<'PY'
import re
import subprocess
version = subprocess.check_output(['go', 'version'], text=True).strip()
print(version)
match = re.search(r'\bgo(\d+)\.(\d+)', version)
if not match or tuple(map(int, match.groups())) < (1, 25):
    raise SystemExit('Go 1.25 or newer is required.')
PY
  python3 --version
fi
if [[ "$mode" != local ]]; then
  helm_version=$(helm version --short)
  printf '%s\n' "$helm_version"
  case "$helm_version" in
    v3.*) ;;
    *) echo 'Helm 3 is required.' >&2; exit 1 ;;
  esac
fi
case "$mode" in
  local)
    git --version
    gh --version
    gh auth status
    ;;
  cluster)
    sudo k3s --version
    sudo systemctl is-active k3s
    kubectl version --client
    kubectl config current-context
    kubectl cluster-info
    kubectl get nodes
    kubectl get ingressclass traefik
    kubectl -n kube-system rollout status deployment/traefik --timeout=30s
    ;;
  runner) kubectl version --client ;;
  development) actionlint -version ;;
esac
printf 'Environment check passed: %s\n' "$mode"
