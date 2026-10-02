#!/usr/bin/env bash
# Keep the bootstrap usable on Ubuntu before Python or development tools exist.
set +x
set -euo pipefail
unset BASH_ENV ENV GH_DEBUG GIT_TRACE GIT_CURL_VERBOSE
root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
command_name=install
if [ "$#" -gt 0 ]; then command_name=$1; fi
if ! command -v python3 >/dev/null 2>&1; then
  if [ "$command_name" != install ]; then
    echo 'Python 3 is missing / 缺少 Python 3。Run: sudo apt-get update && sudo apt-get install -y python3' >&2
    exit 1
  fi
  . /etc/os-release
  if [ "$(uname -m)" != x86_64 ] || [ "$ID" != ubuntu ] || ! [[ "$VERSION_ID" =~ ^(22.04|24.04)$ ]]; then
    echo 'Supported: Ubuntu 22.04/24.04 x64 / 仅支持 Ubuntu 22.04/24.04 x64。' >&2
    exit 1
  fi
  echo 'Installing Python to read your configuration / 安装 Python，用来读取你的配置文件。'
  sudo apt-get update
  sudo apt-get install -y python3 ca-certificates
fi
exec python3 "$root/scripts/setup.py" "$@"
