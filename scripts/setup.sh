#!/usr/bin/env bash
# Keep the bootstrap usable on Ubuntu before Python or development tools exist.
set +x
set -euo pipefail
unset BASH_ENV ENV GH_DEBUG GIT_TRACE GIT_CURL_VERBOSE
root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
command_name=install
language=
language_option=false
help_requested=false
next_value=
for argument in "$@"; do
  if [ -n "$next_value" ]; then
    if [ "$next_value" = --language ]; then language=$argument; fi
    next_value=
    continue
  fi
  case "$argument" in
    init|check|install|doctor|onboard-source) command_name=$argument ;;
    --language) language_option=true; next_value=--language ;;
    --language=*) language_option=true; language=${argument#--language=} ;;
    --config|--source) next_value=$argument ;;
    -h|--help) help_requested=true ;;
  esac
done
# Select before bootstrapping Python, and pass the choice on without a second prompt.
if [ -t 0 ] && [ "$language_option" = false ] && [ "$help_requested" = false ]; then
  printf 'Select language / 请选择语言:\n  1. English\n  2. 中文\n'
  while :; do
    printf 'Choice / 请选择 (1/2, q to exit / 退出): '
    if ! IFS= read -r answer; then
      printf '\nStopped / 已停止。\n' >&2
      exit 130
    fi
    case "${answer,,}" in
      1|en|english) language=en; break ;;
      2|zh|zh-cn|中文) language=zh-CN; break ;;
      q) exit 130 ;;
      *) printf 'Invalid choice; enter 1 or 2 / 选择无效，请输入 1 或 2。\n' ;;
    esac
  done
  set -- --language "$language" "$@"
fi
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
  sudo apt-get install -y python3 ca-certificates
fi
exec python3 "$root/scripts/setup.py" "$@"
