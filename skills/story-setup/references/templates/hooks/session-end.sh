#!/bin/bash
# session-end.sh — 会话结束时按需记录最后状态
# 设计原则：默认静默且不写文件；显式启用时也不创建短篇项目的 追踪/ 目录
set -euo pipefail

# 加载公共函数库
source "$(dirname "$0")/lib/common.sh"

# 字节稳定区域：经 discover_active_book 处理中文书名/路径，GBK 区域下才不会乱（issue #164 同类）。
export LC_ALL=C

ROOT=$(project_root)
BOOK_DIR=$(discover_active_book)

# 用户级项目钩子扩展（.story/hooks/*.sh）：存在时自动加载 .story/.env.local 并以非阻断方式执行。
run_story_project_hooks() {
  local base="$1"
  [ -n "$base" ] && [ -d "$base/.story/hooks" ] || return 0
  local script
  for script in "$base/.story/hooks"/*.sh; do
    [ -f "$script" ] || continue
    (
      set +e
      if [ -f "$ROOT/.story/.env.local" ]; then
        set -a; source "$ROOT/.story/.env.local" >/dev/null 2>&1 || true; set +a
      fi
      if [ -n "$BOOK_DIR" ] && [ "$BOOK_DIR" != "$ROOT" ] && [ -f "$BOOK_DIR/.story/.env.local" ]; then
        set -a; source "$BOOK_DIR/.story/.env.local" >/dev/null 2>&1 || true; set +a
      fi
      export STORY_HOOK_EVENT="stop"
      export STORY_PROJECT_ROOT="$ROOT"
      export STORY_BOOK_ROOT="${BOOK_DIR:-$ROOT}"
      bash "$script" >/dev/null || true
    ) || true
  done
  return 0
}

run_story_project_hooks "$ROOT"
if [ -n "$BOOK_DIR" ] && [ "$BOOK_DIR" != "$ROOT" ]; then
  run_story_project_hooks "$BOOK_DIR"
fi

# 默认禁用 session-log.txt 写入（避免每次会话结束都污染工作树）。
# 显式 STORY_SESSION_LOG=1 才启用；即使启用，也只写入已存在的长篇追踪目录。
if [ "${STORY_SESSION_LOG:-0}" != "1" ]; then
  exit 0
fi

# 只写入已存在的追踪目录；不要 mkdir，避免把短篇项目误升级成长篇结构。
if [ -n "$BOOK_DIR" ] && [ -d "$BOOK_DIR/追踪" ]; then
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] session ended" >> "$BOOK_DIR/追踪/session-log.txt"
fi
