#!/usr/bin/env bash
set +x
set -euo pipefail

unset GH_DEBUG
trap 'unset token' EXIT

usage() {
  printf 'Usage: %s CONTROL_REPOSITORY [REGISTRY]\n' "$0" >&2
  printf 'Example: %s owner/previewmesh-control config/repositories.json\n' "$0" >&2
  exit 2
}

CONTROL_REPOSITORY="${1:-}"
REGISTRY="${2:-config/repositories.json}"
[[ -n "$CONTROL_REPOSITORY" ]] || usage

if [[ ! "$CONTROL_REPOSITORY" =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$ ]]; then
  printf 'Control repository must use owner/name format.\n' >&2
  exit 2
fi
if [[ ! -f "$REGISTRY" ]]; then
  printf 'Registry does not exist: %s\n' "$REGISTRY" >&2
  exit 2
fi

gh auth status >/dev/null 2>&1

# Read only secret names and repository names from trusted configuration.
registration_lines="$(python3 - "$REGISTRY" <<'PY'
import json
import re
import sys

path = sys.argv[1]
with open(path, encoding='utf-8') as stream:
    entries = json.load(stream)

if not isinstance(entries, list) or not entries:
    raise SystemExit('Registry must contain at least one source repository.')

id_pattern = re.compile(r'^[1-9][0-9]*$')
source_pattern = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$')
secret_pattern = re.compile(r'^[A-Z][A-Z0-9_]*$')
seen_ids = set()
seen_sources = set()
seen_secrets = {}

for index, entry in enumerate(entries, start=1):
    if not isinstance(entry, dict):
        raise SystemExit(f'Registry entry {index} must be an object.')
    repository_id = str(entry.get('repository_id', ''))
    source = entry.get('source_repository', '')
    secret = entry.get('source_secret', '')
    if not id_pattern.fullmatch(repository_id) or repository_id in seen_ids:
        raise SystemExit(f'Registry entry {index} has a missing or duplicate repository ID.')
    if not isinstance(source, str) or not source_pattern.fullmatch(source) or source in seen_sources:
        raise SystemExit(f'Registry entry {index} has a missing or duplicate source repository.')
    if not isinstance(secret, str) or not secret_pattern.fullmatch(secret):
        raise SystemExit(f'Registry entry {index} has an invalid source secret name.')
    if secret in seen_secrets and seen_secrets[secret] != source:
        raise SystemExit(f'Source secret {secret} must identify exactly one source repository.')
    seen_ids.add(repository_id)
    seen_sources.add(source)
    seen_secrets[secret] = source
    print(f'{secret}\t{source}')
PY
)"
mapfile -t registrations <<< "$registration_lines"

upload() {
  local secret_name="$1"
  local target_repository="$2"
  if ! printf '%s' "$token" | gh secret set "$secret_name" --repo "$target_repository" --app actions; then
    printf 'Failed to save %s in %s. Earlier uploads, if any, remain saved.\n' \
      "$secret_name" "$target_repository" >&2
    return 1
  fi
  printf 'Saved %s in %s\n' "$secret_name" "$target_repository"
}

prompt() {
  local label="$1"
  if ! read -r -s -p "$label: " token; then
    printf '\nInput ended; stopped. Earlier uploads, if any, remain saved.\n' >&2
    exit 1
  fi
  printf '\n'
  if [[ -z "$token" ]]; then
    printf 'Token is empty; stopped. Earlier uploads, if any, remain saved.\n' >&2
    exit 1
  fi
}

for registration in "${registrations[@]}"; do
  IFS=$'\t' read -r source_secret source_repository <<< "$registration"
  prompt "Paste $source_secret for $source_repository"
  upload "$source_secret" "$CONTROL_REPOSITORY"
  unset token
done

prompt 'Paste PREVIEWMESH_DISPATCH_TOKEN (control Actions write only)'
for registration in "${registrations[@]}"; do
  IFS=$'\t' read -r _ source_repository <<< "$registration"
  upload PREVIEWMESH_DISPATCH_TOKEN "$source_repository"
done
unset token

prompt 'Paste GHCR_READ_TOKEN (classic PAT, read:packages only)'
upload GHCR_READ_TOKEN "$CONTROL_REPOSITORY"
unset token

printf 'All source, dispatch and GHCR secrets were uploaded. Runtime permissions and package access still require verification.\n'
