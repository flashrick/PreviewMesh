#!/usr/bin/env bash
# Keep the bootstrap usable on Ubuntu before Python or development tools exist.
set +x
set -euo pipefail
unset BASH_ENV ENV GH_DEBUG GIT_TRACE GIT_CURL_VERBOSE
root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
command_name=install
language=
next_value=
template_mode=0
help_mode=0
for argument in "$@"; do
  if [ -n "$next_value" ]; then
    if [ "$next_value" = --language ]; then language=$argument; fi
    next_value=
    continue
  fi
  case "$argument" in
    init|check|install|doctor|onboard-source|cleanup) command_name=$argument ;;
    --template) template_mode=1 ;;
    -h|--help) help_mode=1 ;;
    --language) next_value=--language ;;
    --language=*) language=${argument#--language=} ;;
    --config|--source) next_value=$argument ;;
  esac
done
# Cleanup has its own dependency-light menu and must not load a setup.ini first.
if [ "$command_name" = cleanup ]; then
  exec bash "$root/scripts/cleanup.sh" "$@"
fi
# Keep this shell layer limited to argument parsing and Python bootstrapping.
# The Python UI owns language selection so it can use the same terminal fallback
# and interactive controls as the configuration wizard.
say() {
  case "$language" in
    zh-CN) printf '%s\n' "$2" ;;
    en) printf '%s\n' "$1" ;;
    *) printf '%s / %s\n' "$1" "$2" ;;
  esac
}
if ! command -v python3 >/dev/null 2>&1; then
  if [ "$command_name" != install ]; then
    say 'Python 3 is missing. Run: sudo apt-get update && sudo apt-get install -y python3' \
      '缺少 Python 3。请执行：sudo apt-get update && sudo apt-get install -y python3' >&2
    exit 1
  fi
  . /etc/os-release
  if [ "$(uname -m)" != x86_64 ] || [ "$ID" != ubuntu ] || ! [[ "$VERSION_ID" =~ ^(22.04|24.04)$ ]]; then
    say 'Supported: Ubuntu 22.04/24.04 x64.' '仅支持 Ubuntu 22.04/24.04 x64。' >&2
    exit 1
  fi
  say 'Installing Python to read your configuration.' '安装 Python，用来读取你的配置文件。'
  sudo apt-get update
  sudo apt-get install -y python3 python3-yaml ca-certificates
fi

# Prepare the optional UI in a user-owned virtual environment only when the
# caller can actually use interactive terminal controls. This keeps CI and
# redirected commands offline and leaves the system Python installation alone.
setup_python=python3
ui_mode=${PREVIEWMESH_UI:-auto}
ui_disabled=0
case "$ui_mode" in
  plain|off|false|0) ui_disabled=1 ;;
esac
ci_interactive=0
if [ "${PREVIEWMESH_INTERACTIVE:-}" = 1 ]; then ci_interactive=1; fi
if [ "$template_mode" -eq 0 ] && [ "$help_mode" -eq 0 ] \
  && [ "$ui_disabled" -eq 0 ] && [ -t 0 ] && [ -t 1 ] \
  && [ "${TERM:-}" != dumb ] \
  && { [ -z "${CI:-}" ] || [ "$ci_interactive" -eq 1 ]; }; then
  if ! python3 -c 'import rich, questionary' >/dev/null 2>&1; then
    if [ -n "${PREVIEWMESH_UI_VENV:-}" ]; then
      ui_venv=$PREVIEWMESH_UI_VENV
    elif [ -n "${XDG_CACHE_HOME:-}" ]; then
      ui_venv="$XDG_CACHE_HOME/previewmesh/ui-venv"
    else
      ui_venv="${HOME:-$root}/.cache/previewmesh/ui-venv"
    fi
    say 'Preparing the enhanced terminal UI (one-time setup).' '正在准备增强终端界面（首次运行一次）。'
    if ui_python=$(python3 "$root/scripts/setup_ui_bootstrap.py" \
      --root "$root" --venv "$ui_venv" 2>/dev/null); then
      setup_python=$ui_python
      say 'Enhanced terminal UI is ready.' '增强终端界面已准备好。'
    else
      say 'UI dependencies unavailable; continuing with plain text. Retry after checking network or python3-venv.' \
        '界面依赖不可用，将使用纯文本继续。请检查网络或 python3-venv 后重试。' >&2
    fi
  fi
fi
exec "$setup_python" "$root/scripts/setup.py" "$@"
