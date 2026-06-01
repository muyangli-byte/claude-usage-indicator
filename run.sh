#!/usr/bin/env bash
# 可重定位：无论装在哪个目录都能跑（cd 到脚本自身所在目录）
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR"
source venv/bin/activate
exec python claude_usage_indicator.py
