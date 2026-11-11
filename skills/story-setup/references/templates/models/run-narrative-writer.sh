#!/usr/bin/env bash
# narrative-writer 转发器：把任务交给 agy（Antigravity CLI）上的模型执行。
# 由 story-setup 在启用模型分工的“写手”角色时部署到项目 .story/models/（兼容 .claude/models/）。
#
# 用法：
#   run-narrative-writer.sh start <任务文件>   启动一次运行，最多等待 WAIT 秒
#   run-narrative-writer.sh wait  <运行ID>     继续等待尚未结束的运行
#
# 结束时输出 Gemini 的最终回复，退出码与 agy 一致；
# 超过等待时间仍在运行时输出 “RUNNING <运行ID>”，退出码 75。
#
# 环境变量：
#   NARRATIVE_WRITER_MODEL    覆盖 config.json 里 writer.model（默认 gemini-4-argon-medium）
#   NARRATIVE_WRITER_WAIT     单次等待秒数，默认 540（低于 Bash 工具 600 秒上限）
#   NARRATIVE_WRITER_TIMEOUT  agy 单次打印模式超时秒数，默认 360（防止 0s 无限挂起）
#   STORY_MODELS_KEEP_RUNS    已完成运行目录保留上限，默认 30
set -uo pipefail

MODELS_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$MODELS_DIR/../.." && pwd)"
RUNS="$MODELS_DIR/runs"
for PYBIN in python3 python py; do "$PYBIN" -c "" 2>/dev/null && break; done
CONFIG_FILE="$MODELS_DIR/config.json"
[ -f "$CONFIG_FILE" ] || CONFIG_FILE="$ROOT/.story/models/config.json"
[ -f "$CONFIG_FILE" ] || CONFIG_FILE="$ROOT/.claude/models/config.json"
CONFIG_MODEL="$("$PYBIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("writer", {}).get("model", ""))' "$CONFIG_FILE" 2>/dev/null)"
MODEL="${NARRATIVE_WRITER_MODEL:-${CONFIG_MODEL:-gemini-4-argon-medium}}"
WAIT="${NARRATIVE_WRITER_WAIT:-540}"
TIMEOUT="${NARRATIVE_WRITER_TIMEOUT:-360}"
KEEP_RUNS="${STORY_MODELS_KEEP_RUNS:-30}"

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
    PROMPT_TMPL="$MODELS_DIR/narrative-writer-prompt.md"
    [ -f "$PROMPT_TMPL" ] || PROMPT_TMPL="$ROOT/.claude/models/narrative-writer-prompt.md"
    [ -f "$PROMPT_TMPL" ] || die "找不到写手提示词模板：$PROMPT_TMPL"
    id="$(date +%Y%m%d-%H%M%S)-$$"
    dir="$RUNS/$id"
    mkdir -p "$dir"
    cp "$task" "$dir/task.md"
    {
      cat "$PROMPT_TMPL"
      printf '\n\n---\n\n# 本次任务（调用方原文）\n\n'
      cat "$task"
    } > "$dir/prompt.md"
    echo "$MODEL" > "$dir/model"
    # 后台运行：通过标准输入传入完整 prompt（避免 50KB 命令行参数膨胀），显式挂接 --add-dir 与有限超时防死锁。
    nohup bash -c '
      cd "$1" || exit 2
      agy --add-dir "$1" --model "$2" --mode accept-edits --disable-slash-commands \
        --input-format text --print-timeout "${4}s" \
        -p "请严格阅读标准输入中的完整写作规范与本次任务说明，只修改任务指定的候选文件并按要求格式输出报告；严禁执行任何 shell 命令。" \
        < "$3/prompt.md" > "$3/out.txt" 2> "$3/err.txt"
      code=$?
      echo "$code" > "$3/exit_code.tmp" && mv "$3/exit_code.tmp" "$3/exit_code"
      "$5" - "$6" "$7" <<'"'"'PY'"'"' 2>/dev/null || true
import shutil, sys
from pathlib import Path
runs, keep = Path(sys.argv[1]), int(sys.argv[2])
if keep >= 1 and runs.is_dir():
    done = sorted(
        ((p / "exit_code").stat().st_mtime, p)
        for p in runs.iterdir()
        if p.is_dir() and (p / "exit_code").is_file()
    )
    for _, old in done[:-keep] if len(done) > keep else []:
        shutil.rmtree(old, ignore_errors=True)
PY
    ' _ "$ROOT" "$MODEL" "$dir" "$TIMEOUT" "$PYBIN" "$RUNS" "$KEEP_RUNS" > /dev/null 2>&1 &
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
