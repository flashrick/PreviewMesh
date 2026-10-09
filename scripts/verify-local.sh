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
python3 scripts/check-setup-ui.py
python3 scripts/check-setup-wizard.py
python3 scripts/check-installer.py
python3 scripts/check-cleanup.py
python3 scripts/check-onboard-source.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/experiment_runner_test.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/evaluation_freeze_test.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/evaluation_matrix_test.py
bash -n scripts/setup.sh
helm lint charts/preview
actionlint .github/workflows/preview.yml .github/workflows/update-upstream.yml templates/source-notify.yml
printf 'Local verification passed.\n'
