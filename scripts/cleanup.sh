#!/usr/bin/env bash
set -euo pipefail

# Keep cleanup usable from any current directory and share the installer's Python bootstrap.
root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'Python 3 is required for the cleanup menu. Install it, then rerun this command.' \
    '清理菜单需要 Python 3。请先安装 Python 3，再重新运行此命令。' >&2
  exit 1
fi
exec python3 "$root/scripts/cleanup.py" "$@"
