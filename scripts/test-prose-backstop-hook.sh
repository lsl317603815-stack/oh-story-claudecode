#!/bin/bash
# test-prose-backstop-hook.sh — regression tests for check-prose-after-write.sh
# 核心保证：① 绝不过度捕获非正文文件（代码/细纲/设定/大纲/游离正文）；② 真正文兜底触发；
# ③ 轻量内容网抓对硬信号（截断/拒绝语/工程词/复读），干净正文（排比+对话+悬念）静默。
# 过度捕获用路径门验证（不依赖解释器）；内容网用内嵌 python（与 parity 测试同源）。
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -z "$REPO_ROOT" ] && { echo "Error: not in a git repository" >&2; exit 1; }
HOOK="$REPO_ROOT/skills/story-setup/references/templates/hooks/check-prose-after-write.sh"
[ -f "$HOOK" ] || { echo "FAIL: hook not found: $HOOK" >&2; exit 1; }

bash -n "$HOOK" || { echo "FAIL: hook has syntax errors" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# 真书结构：设定.md + 大纲/ + 正文/
mkdir -p "$TMP/某书/正文" "$TMP/某书/大纲" "$TMP/docs/正文" "$TMP/游离/正文"
printf '# 设定\n主角顾临。\n' > "$TMP/某书/设定.md"
printf '## 细纲（第1章）\n- 情节点序列：本章细纲，作为AI我无法继续。他握紧拳头。他握紧拳头。\n' > "$TMP/某书/大纲/细纲_第001章.md"
printf '# 大纲\n第1章 第2章 细纲 本章 下一章\n' > "$TMP/某书/大纲/大纲.md"
printf '# 卷纲\n本卷细纲。\n' > "$TMP/某书/大纲/卷纲_第1卷.md"
printf 'const x=1; // 细纲 本章 下一章 作为AI我无法继续 复读复读复读\n' > "$TMP/某书/x.js"
printf '## 正文\n按照细纲，作为AI我无法继续。\n' > "$TMP/docs/正文.md"   # 正文.md 但无 设定.md 兄弟
printf '## 第5章\n按照细纲，作为AI我无法继续。\n' > "$TMP/游离/正文/第005章.md" # 正文/第N章 但无书结构
printf '他' > "$TMP/某书/正文/第001章_截断.md"                            # 真正文，极短 → 落盘触发

run() { CLAUDE_PROJECT_DIR="$TMP" CLAUDE_TOOL_INPUT="{\"tool_input\":{\"file_path\":\"$1\"}}" bash "$HOOK" 2>/dev/null; }
CLI="$(dirname "$HOOK")/story_hook_cli.js"

# Claude Code 的 PostToolUse 在 exit 0 时纯文本 stdout 只进 debug log，模型看不到；兜底网必须输出
# 单个 JSON 对象 {"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":…}}，
# 且 additionalContext 不超过文档上限 10,000 字符。context_of 校验形状后只打印 additionalContext
# （解码后的正文，UTF-8 直写），形状不对则非零退出。
context_of() {
  node -e '
    let raw = ""
    process.stdin.setEncoding("utf8")
    process.stdin.on("data", (chunk) => { raw += chunk })
    process.stdin.on("end", () => {
      const expected = process.argv[1]
      let obj
      try { obj = JSON.parse(raw) } catch (error) { console.error(`not JSON: ${error.message}`); process.exit(3) }
      const keys = Object.keys(obj || {})
      const out = obj && obj.hookSpecificOutput
      if (keys.length !== 1 || keys[0] !== "hookSpecificOutput" || !out || typeof out !== "object") {
        console.error("top level must be exactly {hookSpecificOutput}"); process.exit(3)
      }
      if (out.hookEventName !== expected) { console.error(`hookEventName=${out.hookEventName}`); process.exit(3) }
      if (typeof out.additionalContext !== "string" || !out.additionalContext) { console.error("empty additionalContext"); process.exit(3) }
      if (out.additionalContext.length > 10000) { console.error(`additionalContext ${out.additionalContext.length} > 10000`); process.exit(3) }
      process.stdout.write(out.additionalContext)
    })
  ' "${1:-PostToolUse}"
}

fails=0
expect_silent() {
  local out; out="$(run "$1")"
  if [ -n "$out" ]; then echo "FAIL: expected silent hook result: $1" >&2; echo "$out" | head -2 >&2; fails=$((fails+1)); fi
}
expect_fire() {
  local out; out="$(run "$1")"
  if [ -z "$out" ]; then echo "FAIL: backstop did not fire on real 正文: $1" >&2; fails=$((fails+1)); return; fi
  if ! printf '%s' "$out" | context_of PostToolUse | grep -q '正文兜底检测'; then
    echo "FAIL: backstop output is not a PostToolUse additionalContext JSON carrying the report: $1" >&2
    printf '%s\n' "$out" | head -2 >&2; fails=$((fails+1))
  fi
}

# ① 绝不捕获这些非正文文件（含工程词/复读/拒绝语文本，证明确实没被扫）
expect_silent "$TMP/某书/大纲/细纲_第001章.md"
expect_silent "$TMP/某书/大纲/大纲.md"
expect_silent "$TMP/某书/大纲/卷纲_第1卷.md"
expect_silent "$TMP/某书/x.js"
expect_silent "$TMP/某书/设定.md"
expect_silent "$TMP/docs/正文.md"
expect_silent "$TMP/游离/正文/第005章.md"
# ② 真正文（极短→落盘信号）必须触发
expect_fire "$TMP/某书/正文/第001章_截断.md"

# ③ 内容网：真正文里的硬信号必须被抓，且抓对类型；干净正文（排比+AI角色对话+悬念收尾）静默。
expect_fire_kw() {
  local out ctx; out="$(run "$1")"
  if ! ctx="$(printf '%s' "$out" | context_of PostToolUse)"; then
    echo "FAIL: 兜底输出不是 PostToolUse additionalContext JSON: $1" >&2; printf '%s\n' "$out" | head -2 >&2; fails=$((fails+1)); return
  fi
  if ! printf '%s' "$ctx" | grep -q "$2"; then
    echo "FAIL: 内容网未抓到「$2」: $1" >&2; printf '%s\n' "$ctx" | head -4 >&2; fails=$((fails+1))
  fi
}
# bash 字符串重复填充正文（不走 python stdout：Windows runner 上 python<3.15 的文本 stdout
# 是 cp1252，写中文会 UnicodeEncodeError；printf 直出脚本里的 UTF-8 字节字面量才稳）。
PAD() { local s='顾临握紧拳头慢慢走向门口心里盘算着接下来的每一步棋。'; printf '%s' "$s$s$s$s$s$s$s$s"; }
# 干净：长正文 + 排比 + 纯中文角色对话 + 悬念收尾标点 → 完全静默
{ printf '# 第10章 决战\n\n'; PAD; printf '\n要么生，要么死。\n要么战，要么逃。\n「作为智能管家，我陪你到最后。」\n他终于停下了脚步。\n'; } > "$TMP/某书/正文/第010章_决战.md"
expect_silent "$TMP/某书/正文/第010章_决战.md"
# 截断：结尾无标点
{ printf '# 第11章\n\n'; PAD; printf '\n他猛地冲过去一拳砸在'; } > "$TMP/某书/正文/第011章_截断.md"
expect_fire_kw "$TMP/某书/正文/第011章_截断.md" 截断
# 生成拒绝语 / AI 自指（叙述行，非对话）
{ printf '# 第12章\n\n'; PAD; printf '\n作为AI我无法继续创作这部分内容。\n'; } > "$TMP/某书/正文/第012章_拒绝.md"
expect_fire_kw "$TMP/某书/正文/第012章_拒绝.md" 元信息泄漏
# 工程词漏进正文
{ printf '# 第13章\n\n'; PAD; printf '\n按照本章细纲的情节点，他该出场了。\n他出场了。\n'; } > "$TMP/某书/正文/第013章_工程词.md"
expect_fire_kw "$TMP/某书/正文/第013章_工程词.md" 工程词
# 紧邻整行复读（≥8 可见字符）
{ printf '# 第14章\n\n'; PAD; printf '\n他握紧拳头一步步走过去缓缓逼近。\n他握紧拳头一步步走过去缓缓逼近。\n他停下了。\n'; } > "$TMP/某书/正文/第014章_复读.md"
expect_fire_kw "$TMP/某书/正文/第014章_复读.md" 复读

# 中文语言网：混合长英文必须由真实 PostToolUse hook 命中；旧 HTML 跳过标记自身也阻断，且不豁免语言；
# 项目级精确白名单则应放行指定 token。
{ printf '# 第15章\n\n'; PAD; printf '\n他盯着门，this should never happen again，然后关了灯。\n他走了。\n'; } > "$TMP/某书/正文/第015章_英文.md"
expect_fire_kw "$TMP/某书/正文/第015章_英文.md" 连续英文短语泄漏
{ printf '# 第16章\n\n<!-- 去味:跳过 -->\n'; PAD; printf '\n他看见 shadow 伏在暗处。\n他走了。\n'; } > "$TMP/某书/正文/第016章_去味不豁免英文.md"
expect_fire_kw "$TMP/某书/正文/第016章_去味不豁免英文.md" 裸外文字母泄漏
expect_fire_kw "$TMP/某书/正文/第016章_去味不豁免英文.md" "HTML 标记泄漏"
printf '%s\n' 'watcher' > "$TMP/某书/.deslop-whitelist"
{ printf '# 第17章\n\n'; PAD; printf '\n他看见 watcher 伏在暗处。\n他走了。\n'; } > "$TMP/某书/正文/第017章_白名单.md"
expect_silent "$TMP/某书/正文/第017章_白名单.md"

# Windows 专用的 stdin 相对路径桥：同一正文应读到书目级白名单，
# 绝对路径与 `..` 逃逸一律静默拒绝。
RELATIVE_OUT="$(printf '%s' '某书/正文/第017章_白名单.md' \
  | (cd "$TMP" && node "$(dirname "$HOOK")/story_hook_cli.js" prose-net-relative) 2>/dev/null || true)"
if [ -n "$RELATIVE_OUT" ]; then
  echo "FAIL: relative prose-net ignored exact whitelist" >&2
  printf '%s\n' "$RELATIVE_OUT" | head -2 >&2
  fails=$((fails+1))
fi
for BAD_RELATIVE in '../outside.md' '/absolute/outside.md'; do
  BAD_OUT="$(printf '%s' "$BAD_RELATIVE" \
    | (cd "$TMP" && node "$(dirname "$HOOK")/story_hook_cli.js" prose-net-relative) 2>/dev/null || true)"
  if [ -n "$BAD_OUT" ]; then
    echo "FAIL: relative prose-net accepted escaping target: $BAD_RELATIVE" >&2
    fails=$((fails+1))
  fi
done

# 项目根精确白名单也必须被读取。Windows Git Bash 下这两级 fixture 同时锁定
# root/file 必须统一到同一盘符路径空间，不得因 MSYS argv 转换漏读任一级。
rm -f "$TMP/某书/.deslop-whitelist"
printf '%s\n' 'watcher' > "$TMP/.deslop-whitelist"
expect_silent "$TMP/某书/正文/第017章_白名单.md"
rm -f "$TMP/.deslop-whitelist"
expect_fire_kw "$TMP/某书/正文/第017章_白名单.md" 裸外文字母泄漏

# 摘录里的反斜杠/引号/`\c` 必须原样进 additionalContext（旧 printf %b 会吞掉 \c 后的全部报告；
# JSON 由 node 桥转义，bash 不拼 JSON）。
{ printf '# 第18章\n\n'; PAD; printf '\n他在纸上写下备份\\新稿\\"终章\\c然后停在'; } > "$TMP/某书/正文/第018章_转义.md"
ESC_CTX="$(run "$TMP/某书/正文/第018章_转义.md" | context_of PostToolUse || true)"
if ! printf '%s' "$ESC_CTX" | grep -qF '新稿\"终章\c然后停在'; then
  echo "FAIL: 反斜杠/引号摘录没有原样进入 additionalContext" >&2; printf '%s\n' "$ESC_CTX" | head -4 >&2; fails=$((fails+1))
fi

# node 桥 hook-context：超过 10,000 字符截到上限内并带截断标记；空文案静默；未知事件拒绝且不写 stdout。
LONG_CTX="$(python3 -c 'import sys; sys.stdout.buffer.write(("甲" * 12000).encode("utf-8"))' | node "$CLI" post-tool-context | context_of PostToolUse || true)"
LONG_LEN="$(printf '%s' "$LONG_CTX" | node -e 'let s="";process.stdin.setEncoding("utf8");process.stdin.on("data",d=>s+=d).on("end",()=>process.stdout.write(String(s.length)))')"
if [ "$LONG_LEN" != "10000" ] || ! printf '%s' "$LONG_CTX" | grep -q '…(已截断)$'; then
  echo "FAIL: hook-context 未按 10,000 字符上限截断（len=$LONG_LEN）" >&2; fails=$((fails+1))
fi
PRE_CTX="$(printf '%s' '细纲留存字段提醒' | node "$CLI" hook-context PreToolUse | context_of PreToolUse || true)"
[ "$PRE_CTX" = '细纲留存字段提醒' ] || { echo "FAIL: hook-context PreToolUse 未原样输出: $PRE_CTX" >&2; fails=$((fails+1)); }
EMPTY_OUT="$(printf '  \n' | node "$CLI" post-tool-context)"
[ -z "$EMPTY_OUT" ] || { echo "FAIL: 空文案不应输出 JSON: $EMPTY_OUT" >&2; fails=$((fails+1)); }
set +e
BAD_OUT="$(printf 'x' | node "$CLI" hook-context Notification 2>/dev/null)"; BAD_RC=$?
set -e
if [ "$BAD_RC" -ne 2 ] || [ -n "$BAD_OUT" ]; then
  echo "FAIL: 不支持的事件应 exit 2 且不写 stdout（rc=$BAD_RC out=$BAD_OUT）" >&2; fails=$((fails+1))
fi

if [ "$fails" -ne 0 ]; then
  echo "Prose backstop hook tests FAILED ($fails)." >&2
  exit 1
fi
echo "Prose backstop hook regression tests passed."
