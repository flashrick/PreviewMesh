#!/usr/bin/env bash
set -euo pipefail

# Pull generic PreviewMesh changes into a customer control repository.
# The default mode creates a review branch; direct mode updates main locally.

usage() {
  cat <<'EOF'
Usage: scripts/update-upstream.sh [options]

Options:
  --repo-dir PATH   customer control repository (default: current Git repo)
  --remote NAME     upstream remote name (default: upstream)
  --url URL         upstream Git URL when the remote does not exist
  --ref BRANCH      upstream branch to fetch (default: main)
  --branch NAME     update branch name (default: generated)
  --direct          update the current main branch instead of creating a branch
  --validate        run control-repository checks before committing a direct update
  --push            push a direct update to origin (requires --direct)
  -h, --help        show this help
EOF
}

die() {
  printf 'update-upstream: %s\n' "$*" >&2
  exit 1
}

emit_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s\n' "$1" >> "$GITHUB_OUTPUT"
  fi
}

repo_dir=""
upstream_remote="upstream"
upstream_url=""
upstream_ref="main"
update_branch=""
direct_update=false
validate_update=false
push_update=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir)
      [[ $# -ge 2 ]] || die "--repo-dir requires a path"
      repo_dir=$2
      shift 2
      ;;
    --remote)
      [[ $# -ge 2 ]] || die "--remote requires a name"
      upstream_remote=$2
      shift 2
      ;;
    --url)
      [[ $# -ge 2 ]] || die "--url requires a Git URL"
      upstream_url=$2
      shift 2
      ;;
    --ref)
      [[ $# -ge 2 ]] || die "--ref requires a branch"
      upstream_ref=$2
      shift 2
      ;;
    --branch)
      [[ $# -ge 2 ]] || die "--branch requires a name"
      update_branch=$2
      shift 2
      ;;
    --direct)
      direct_update=true
      shift
      ;;
    --validate)
      validate_update=true
      shift
      ;;
    --push)
      push_update=true
      shift
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

if [[ -n "$repo_dir" ]]; then
  cd "$repo_dir"
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a Git repository"
cd "$repo_root"

base_branch=$(git branch --show-current)
[[ -n "$base_branch" ]] || die "the repository is in detached HEAD state"
[[ -z "$(git status --porcelain=v1 --untracked-files=all)" ]] || die "working tree must be clean"
[[ "$push_update" == false || "$direct_update" == true ]] || die "--push requires --direct"
[[ "$validate_update" == false || "$direct_update" == true ]] || die "--validate requires --direct"

if [[ "$direct_update" == true ]]; then
  [[ "$base_branch" == "main" ]] || die "--direct must run on the main branch"
  git remote get-url origin >/dev/null 2>&1 || die "origin remote is required for --direct"
  # Bring the private control branch current before applying public changes.
  git pull --ff-only origin "$base_branch"
  [[ -z "$(git status --porcelain=v1 --untracked-files=all)" ]] || die "working tree must be clean after pulling origin"
fi

if git remote get-url "$upstream_remote" >/dev/null 2>&1; then
  configured_url=$(git remote get-url "$upstream_remote")
  resolved_upstream_url=$configured_url
  printf 'Using existing remote %s: %s\n' "$upstream_remote" "$configured_url"
else
  [[ -n "$upstream_url" ]] || upstream_url="https://github.com/flashrick/PreviewMesh.git"
  git remote add "$upstream_remote" "$upstream_url"
  resolved_upstream_url=$upstream_url
  printf 'Added remote %s: %s\n' "$upstream_remote" "$upstream_url"
fi

if [[ "$push_update" == true ]]; then
  origin_push_url=$(git remote get-url --push origin)
  upstream_push_url=$(git remote get-url --push "$upstream_remote")
  [[ "$origin_push_url" != "$upstream_push_url" ]] || die "origin and upstream must use different push URLs"
fi

git fetch --no-tags "$upstream_remote" "$upstream_ref"
target_ref="$upstream_remote/$upstream_ref"
target_sha=$(git rev-parse "$target_ref")

if git merge-base --is-ancestor "$target_ref" "$base_branch"; then
  printf 'PreviewMesh is already up to date with %s (%s).\n' "$target_ref" "$target_sha"
  emit_output "updated=false"
  emit_output "branch="
  exit 0
fi

if [[ "$direct_update" == false && -z "$update_branch" ]]; then
  safe_ref=${upstream_ref//[^A-Za-z0-9._-]/-}
  update_branch="update/previewmesh-${safe_ref}-$(date -u +%Y%m%d%H%M%S)"
fi

if [[ "$direct_update" == false ]]; then
  git show-ref --verify --quiet "refs/heads/$update_branch" && die "branch already exists: $update_branch"
  git switch -c "$update_branch" >/dev/null
fi

merge_mode="normal"
if git merge-base "$base_branch" "$target_ref" >/dev/null 2>&1; then
  if ! git merge --no-edit --no-ff --no-commit "$target_ref"; then
    conflicts=$(git diff --name-only --diff-filter=U)
    unexpected_conflicts=$(printf '%s\n' "$conflicts" | sed '/^$/d; /^config\/repositories\.json$/d')
    if [[ -n "$unexpected_conflicts" ]]; then
      git merge --abort || true
      if [[ "$direct_update" == false ]]; then
        git switch "$base_branch" >/dev/null
      fi
      die "merge conflicts require manual review:\n$unexpected_conflicts"
    fi
  fi
else
  # Template-created repositories have unrelated histories. This bridge keeps
  # the customer's current tree intact while recording the public history so
  # later updates can use ordinary merges.
  merge_mode="history-bridge"
  git merge -s ours --allow-unrelated-histories --no-edit --no-commit "$target_ref"
fi

# The registry belongs to the customer and must not be replaced by upstream.
if git cat-file -e "HEAD:config/repositories.json" 2>/dev/null; then
  git restore --source=HEAD --staged --worktree -- config/repositories.json
fi

remaining_conflicts=$(git diff --name-only --diff-filter=U)
[[ -z "$remaining_conflicts" ]] || die "unresolved merge conflicts remain:\n$remaining_conflicts"

if [[ "$merge_mode" == "history-bridge" ]]; then
  [[ ! -e .previewmesh-upstream ]] || die ".previewmesh-upstream already exists; review the existing upstream link first"
  cat > .previewmesh-upstream <<EOF
repository=$resolved_upstream_url
ref=$upstream_ref
commit=$target_sha
EOF
  git add .previewmesh-upstream
fi

git add -A
if git diff --cached --quiet; then
  git merge --abort || true
  if [[ "$direct_update" == false ]]; then
    git switch "$base_branch" >/dev/null
  fi
  printf 'No customer-visible changes were found.\n'
  emit_output "updated=false"
  emit_output "branch="
  exit 0
fi

if [[ "$validate_update" == true ]]; then
  printf 'Validating the direct control-repository update before committing.\n'
  if ! go test ./... ||
    ! go vet ./... ||
    ! python3 scripts/check-workflow.py ||
    ! python3 scripts/check-github-secrets.py ||
    ! python3 scripts/check-onboard-source.py; then
    git merge --abort || true
    die "control-repository validation failed; the main branch was not updated"
  fi
fi

if [[ "$merge_mode" == "history-bridge" ]]; then
  git commit -m "chore: link PreviewMesh upstream history" >/dev/null
else
  git commit -m "chore: update PreviewMesh from upstream" >/dev/null
fi

new_sha=$(git rev-parse HEAD)
target_branch=${update_branch:-$base_branch}
printf 'Created %s at %s from %s (%s).\n' "$target_branch" "$new_sha" "$target_ref" "$target_sha"
if [[ "$direct_update" == true ]]; then
  printf 'Updated %s directly.\n' "$base_branch"
  if [[ "$push_update" == true ]]; then
    git push origin "$base_branch"
    printf 'Pushed %s to origin.\n' "$base_branch"
  else
    printf 'Review the diff and push %s when ready.\n' "$base_branch"
  fi
  emit_output "updated=true"
  emit_output "branch=$base_branch"
else
  printf 'Review the diff, run local checks, then push the branch and open a PR.\n'
  emit_output "updated=true"
  emit_output "branch=$update_branch"
fi
