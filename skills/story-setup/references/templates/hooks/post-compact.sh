#!/bin/bash
# post-compact.sh — compact 后提醒恢复上下文
# 送达边界（code.claude.com/docs/en/hooks）：PostCompact 没有 decision control，也不收
# additionalContext，exit 0 的 stdout 只进 debug log——这句提示在 Claude Code 上到不了模型，
# 改 JSON 也没用。真正的压缩后回灌由 SessionStart（source=compact，settings 里未设 matcher 即覆盖）
# 的 session-start.sh 注入「当前位置」摘要承担；本脚本的输出只是 debug log 记录。
set -euo pipefail

# 加载公共函数库
source "$(dirname "$0")/lib/common.sh"

# 字节稳定区域：经 discover_active_book 处理中文书名/路径，GBK 区域下才不会乱（issue #164 同类）。
export LC_ALL=C

ROOT=$(project_root)
BOOK_DIR=$(discover_active_book)

if [ -n "$BOOK_DIR" ] && [ -f "$BOOK_DIR/追踪/上下文.md" ]; then
  LINE_COUNT=$(wc -l < "$BOOK_DIR/追踪/上下文.md" | tr -d ' ')
  echo "Context was compacted. Read ${BOOK_DIR#$ROOT/}/追踪/上下文.md ($LINE_COUNT lines) to restore writing context."
else
  echo "Context was compacted. Check 追踪/上下文.md to restore context."
fi
