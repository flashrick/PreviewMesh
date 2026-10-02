#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/create-control-repository.sh --repository OWNER/NAME [options]

Create a private control repository from the public PreviewMesh template.

Options:
  --repository OWNER/NAME   private control repository to create (required)
  --directory PATH          local control checkout (default: ../previewmesh-control)
  --source OWNER/NAME       public PreviewMesh repository (default: PUBLIC_REPOSITORY or current origin)
  --description TEXT        optional GitHub repository description
  -h, --help                show this help

The target GitHub repository must not already exist, and the local directory must
be absent or empty. The script never removes an existing directory.
EOF
}

die() {
  printf 'create-control-repository: %s\n' "$*" >&2
  exit 1
}

repo_pattern='^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$'
control_repository="${CONTROL_REPOSITORY:-}"
control_dir="${CONTROL_DIR:-}"
source_repository="${PUBLIC_REPOSITORY:-}"
description=''

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repository)
      [[ $# -ge 2 ]] || die '--repository requires OWNER/NAME'
      control_repository=$2
      shift 2
      ;;
    --directory)
      [[ $# -ge 2 ]] || die '--directory requires a path'
      control_dir=$2
      shift 2
      ;;
    --source)
      [[ $# -ge 2 ]] || die '--source requires OWNER/NAME'
      source_repository=$2
      shift 2
      ;;
    --description)
      [[ $# -ge 2 ]] || die '--description requires text'
      description=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "$control_repository" ]] || { usage >&2; exit 2; }
[[ "$control_repository" =~ $repo_pattern ]] || die 'control repository must use OWNER/NAME format'

command -v git >/dev/null 2>&1 || die 'git is required'
command -v gh >/dev/null 2>&1 || die 'GitHub CLI (gh) is required'
gh auth status --hostname github.com >/dev/null 2>&1 || die 'run gh auth login --hostname github.com first'

source_root=$(git rev-parse --show-toplevel 2>/dev/null) || die 'run this command from a Git checkout'
source_root=$(cd "$source_root" && pwd -P)

if [[ -z "$source_repository" ]]; then
  origin_url=$(git -C "$source_root" remote get-url origin 2>/dev/null || true)
  case "$origin_url" in
    https://github.com/*/*)
      source_repository=${origin_url#https://github.com/}
      ;;
    git@github.com:*/*)
      source_repository=${origin_url#git@github.com:}
      ;;
    *)
      die 'set --source OWNER/NAME or PUBLIC_REPOSITORY when origin is not a GitHub repository'
      ;;
  esac
  source_repository=${source_repository%.git}
fi
[[ "$source_repository" =~ $repo_pattern ]] || die 'source repository must use OWNER/NAME format'
[[ "$source_repository" != "$control_repository" ]] || die 'source and control repositories must be different'

if [[ -z "$control_dir" ]]; then
  control_dir="$(dirname "$source_root")/previewmesh-control"
fi
if [[ "$control_dir" != /* ]]; then
  control_dir="$PWD/$control_dir"
fi
control_parent=$(dirname -- "$control_dir")
control_name=$(basename -- "$control_dir")
[[ "$control_name" != '.' && "$control_name" != '/' ]] || die 'invalid control checkout directory'
mkdir -p "$control_parent"
control_parent=$(cd "$control_parent" && pwd -P)
control_dir="$control_parent/$control_name"
[[ "$control_dir" != "$source_root" ]] || die 'control checkout cannot be the current template checkout'

if [[ -e "$control_dir" ]]; then
  [[ -d "$control_dir" ]] || die "target path is not a directory: $control_dir"
  [[ -z "$(find "$control_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]] || \
    die "target directory is not empty: $control_dir"
fi

if gh repo view "$control_repository" --json nameWithOwner >/dev/null 2>&1; then
  die "GitHub repository already exists: $control_repository"
fi

printf 'Cloning public template %s into %s\n' "$source_repository" "$control_dir"
gh auth setup-git --hostname github.com >/dev/null
git clone --origin upstream "https://github.com/$source_repository.git" "$control_dir"

branch=$(git -C "$control_dir" branch --show-current)
[[ "$branch" == 'main' ]] || die "public template default branch must be main; found: ${branch:-detached HEAD}"

printf 'Creating private control repository %s\n' "$control_repository"
create_args=(repo create "$control_repository" --private --source "$control_dir" --remote origin --push)
if [[ -n "$description" ]]; then
  create_args+=(--description "$description")
fi
gh "${create_args[@]}"

printf 'Created private control repository: https://github.com/%s\n' "$control_repository"
printf 'Local control checkout: %s\n' "$control_dir"
printf 'Next: cd %q and continue with setup step 2.\n' "$control_dir"
