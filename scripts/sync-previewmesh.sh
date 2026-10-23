#!/usr/bin/env bash
set -euo pipefail

# Sync the public PreviewMesh main branch into a private control repository.
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)

exec "$script_dir/update-upstream.sh" --repo-dir "$repo_root" --direct --validate --push "$@"
