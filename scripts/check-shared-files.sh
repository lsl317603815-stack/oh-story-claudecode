#!/bin/bash
# check-shared-files.sh — 检查跨 skill 同名脚本与参考文档一致性
# 单一真源：scripts/shared-assets.json + scripts/sync-shared-assets.py
# 兼容 bash 3+（macOS）与 Windows Python 解释器回退探测
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || (cd "$SCRIPT_DIR/.." && pwd))"

echo "Shared File Consistency Check"
echo "=============================="

PYTHON_BIN=""
for candidate in python3 python py; do
  if "$candidate" -c "" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "FAIL: Python 3 is required (tried python3, python, and py)" >&2
  exit 1
fi

"$PYTHON_BIN" "$REPO_ROOT/scripts/sync-shared-assets.py" check
echo "All shared files are consistent."
