#!/usr/bin/env bash
set -euo pipefail

# Resolve paths relative to the checkout, even when invoked from another directory.
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ $# -eq 0 ]] || { echo "Usage: $0" >&2; exit 2; }
scripts/check-environment.sh development
go test ./...
go vet ./...
python3 scripts/check-workflow.py
python3 scripts/check-github-secrets.py
python3 scripts/check-setup.py
helm lint charts/preview
actionlint .github/workflows/preview.yml .github/workflows/update-upstream.yml templates/source-notify.yml
printf 'Local verification passed.\n'
