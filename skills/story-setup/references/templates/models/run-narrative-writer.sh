#!/usr/bin/env bash
# narrative-writer 转发器：把任务交给 agy（Antigravity CLI）上的模型执行。
# 由 story-setup 在启用模型分工的“写手”角色时部署到项目 .claude/models/。
#
# 用法：
#   run-narrative-writer.sh start <任务文件>   启动一次运行，最多等待 WAIT 秒
#   run-narrative-writer.sh wait  <运行ID>     继续等待尚未结束的运行
#
# 结束时输出 Gemini 的最终回复，退出码与 agy 一致；
# 超过等待时间仍在运行时输出 “RUNNING <运行ID>”，退出码 75。
#
# 环境变量：
#   NARRATIVE_WRITER_MODEL  覆盖 config.json 里 writer.model（默认 gemini-4-argon-medium）
#   NARRATIVE_WRITER_WAIT   单次等待秒数，默认 540（低于 Bash 工具 600 秒上限）
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
AGY_DIR="$ROOT/.claude/models"
RUNS="$AGY_DIR/runs"
for PYBIN in python3 python py; do "$PYBIN" -c "" 2>/dev/null && break; done
CONFIG_MODEL="$("$PYBIN" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("writer", {}).get("model", ""))' "$AGY_DIR/config.json" 2>/dev/null)"
MODEL="${NARRATIVE_WRITER_MODEL:-${CONFIG_MODEL:-gemini-4-argon-medium}}"
WAIT="${NARRATIVE_WRITER_WAIT:-540}"

die() { echo "错误：$*" >&2; exit 2; }

await_run() {
  local dir="$RUNS/$1" waited=0
  [ -d "$dir" ] || die "找不到运行 $1"
  while [ ! -f "$dir/exit_code" ]; do
    if [ "$waited" -ge "$WAIT" ]; then
      echo "RUNNING $1"
      exit 75
    fi
    sleep 3
    waited=$((waited + 3))
  done
  local code
  code="$(cat "$dir/exit_code")"
  cat "$dir/out.txt"
  if [ "$code" != "0" ]; then
    echo "--- agy 退出码 $code，错误输出末尾 ---" >&2
    tail -n 40 "$dir/err.txt" >&2
  fi
  exit "$code"
}

cmd="${1:-}"
case "$cmd" in
  start)
    task="${2:-}"
    [ -n "$task" ] && [ -s "$task" ] || die "任务文件不存在或为空：$task"
    command -v agy >/dev/null 2>&1 || die "找不到 agy 命令"
    id="$(date +%Y%m%d-%H%M%S)-$$"
    dir="$RUNS/$id"
    mkdir -p "$dir"
    cp "$task" "$dir/task.md"
    {
      cat "$AGY_DIR/narrative-writer-prompt.md"
      printf '\n\n---\n\n# 本次任务（调用方原文）\n\n'
      cat "$task"
    } > "$dir/prompt.md"
    echo "$MODEL" > "$dir/model"
    # 后台运行，避免单次 Bash 调用超时打断长任务；accept-edits 只自动批准文件修改，不放行 shell。
    nohup bash -c '
      cd "$1" || exit 2
      agy --model "$2" --mode accept-edits --print-timeout 0s -p "$(cat "$3/prompt.md")" \
        > "$3/out.txt" 2> "$3/err.txt"
      echo $? > "$3/exit_code.tmp" && mv "$3/exit_code.tmp" "$3/exit_code"
    ' _ "$ROOT" "$MODEL" "$dir" > /dev/null 2>&1 &
    echo $! > "$dir/pid"
    await_run "$id"
    ;;
  wait)
    [ -n "${2:-}" ] || die "缺少运行ID"
    await_run "$2"
    ;;
  *)
    die "用法：$0 start <任务文件> | wait <运行ID>"
    ;;
esac
