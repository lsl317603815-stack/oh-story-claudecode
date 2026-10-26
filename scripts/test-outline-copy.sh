#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL="$ROOT/skills/story-deslop/scripts/check-outline-copy.js"
MIRROR="$ROOT/skills/story-long-write/scripts/check-outline-copy.js"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/outline-copy.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

cmp "$TOOL" "$MIRROR"

cat > "$TMP/outline.md" <<'EOF'
## 细纲（第一章）
- 复沓锚句：“今日所言字字为证来日若有半句虚言任凭族规处置”
- 核心事件：林川把染血的旧账册放到祠堂长桌正中当众说出管家的名字
- 本章禁止提前释放：账册真正的主人
EOF

cat > "$TMP/copied.md" <<'EOF'
## 第一章 祠堂

林川把染血的旧账册放到祠堂长桌正中，当众说出管家的名字。
EOF

if node "$TOOL" --outline "$TMP/outline.md" --json "$TMP/copied.md" > "$TMP/copied.json"; then
  echo "FAIL: unanchored outline copy should block" >&2
  exit 1
fi
grep -q 'outline-verbatim-overlap' "$TMP/copied.json"

cat > "$TMP/anchored.md" <<'EOF'
## 第一章 祠堂

“今日所言，字字为证。来日若有半句虚言，任凭族规处置。”
EOF
node "$TOOL" --outline "$TMP/outline.md" --json "$TMP/anchored.md" > "$TMP/anchored.json"
grep -q '"status": "pass"' "$TMP/anchored.json"

cat > "$TMP/partial-anchor.md" <<'EOF'
## 第一章 祠堂

今日所言，字字为证，来日若有半句虚言。
EOF
if node "$TOOL" --outline "$TMP/outline.md" --json "$TMP/partial-anchor.md" > "$TMP/partial-anchor.json"; then
  echo "FAIL: copying only part of a registered refrain should still block" >&2
  exit 1
fi
grep -q 'outline-verbatim-overlap' "$TMP/partial-anchor.json"

cat > "$TMP/paraphrased.md" <<'EOF'
## 第一章 祠堂

林川进门后没急着开口。他把那本带血的账册搁到众人都看得见的地方，目光越过长桌，停在管家脸上。
EOF
node "$TOOL" --outline "$TMP/outline.md" --json "$TMP/paraphrased.md" > "$TMP/paraphrased.json"
grep -q '"status": "pass"' "$TMP/paraphrased.json"

cat > "$TMP/fifteen-outline.md" <<'EOF'
- 核心事件：一二三四五六七八九十甲乙丙丁戊
EOF
cat > "$TMP/fifteen-prose.md" <<'EOF'
## 测试
一二三四五六七八九十甲乙丙丁戊。
EOF
node "$TOOL" --outline "$TMP/fifteen-outline.md" --json "$TMP/fifteen-prose.md" > "$TMP/fifteen.json"
grep -q '"status": "pass"' "$TMP/fifteen.json"

node "$TOOL" --outline "$TMP/outline.md" --fail-on=never "$TMP/copied.md" "$TMP/paraphrased.md" > "$TMP/multiple.txt"
grep -q '\[BLOCK\]' "$TMP/multiple.txt"
grep -q '\[PASS\]' "$TMP/multiple.txt"

# --- #404 写后回填的「实际完成情况」不是誊抄来源；支持普通/加粗字段与标题子节 ---
COPIED="他握紧那柄旧刀在雨里站了整整一夜没有离开半步"
printf '%s。\n' "$COPIED" > "$TMP/completed-prose.md"
expect_exit() {
  local want="$1" label="$2"
  set +e
  node "$TOOL" --outline "$TMP/completion.md" "$TMP/completed-prose.md" > "$TMP/completion.out" 2>&1
  local got=$?
  set -e
  if [ "$got" -ne "$want" ]; then
    echo "FAIL [$label]: expected exit $want, got $got" >&2
    cat "$TMP/completion.out" >&2
    exit 1
  fi
}
printf '#### 实际完成情况\n%s。\n' "$COPIED" > "$TMP/completion.md"
expect_exit 0 completion-heading
printf '%s\n' "- 实际完成情况：${COPIED}。" "- 结尾设定：离开。" > "$TMP/completion.md"
expect_exit 0 completion-field
printf '%s\n' "- **实际完成情况**：" "  ${COPIED}。" "- 结尾设定：离开。" > "$TMP/completion.md"
expect_exit 0 completion-bold-field
printf '#### 实际完成情况\n已完成。\n##### 实际落地\n%s。\n' "$COPIED" > "$TMP/completion.md"
expect_exit 0 completion-nested-heading

# 排除完成记录不能吞掉记录前、同级/上级标题后或下一字段的真实照搬
printf '#### 情节细化\n%s。\n#### 实际完成情况\n已完成。\n' "$COPIED" > "$TMP/completion.md"
expect_exit 1 copy-before-completion
printf '#### 实际完成情况\n##### 实际落地\n已完成。\n#### 后续情节\n%s。\n' "$COPIED" > "$TMP/completion.md"
expect_exit 1 copy-after-completion-peer-heading
printf '#### 实际完成情况\n##### 实际落地\n已完成。\n## 下一章\n%s。\n' "$COPIED" > "$TMP/completion.md"
expect_exit 1 copy-after-completion-parent-heading
printf '%s\n' "- **实际完成情况**：已完成。" "- 结尾设定：${COPIED}。" > "$TMP/completion.md"
expect_exit 1 copy-after-completion-field

# --- #398 显式细纲或正文读不到、参数缺失：显式失败（退 2），不当成「无照搬」放行 ---
set +e
node "$TOOL" --outline "$TMP/不存在的细纲.md" "$TMP/completed-prose.md" > /dev/null 2>&1; missing_outline=$?
node "$TOOL" --outline "$TMP/outline.md" "$TMP/不存在的正文.md" > /dev/null 2>&1; missing_prose=$?
node "$TOOL" --outline > /dev/null 2>&1; missing_value=$?
set -e
[ "$missing_outline" -eq 2 ] || { echo "FAIL: missing explicit outline should exit 2, got $missing_outline" >&2; exit 1; }
[ "$missing_prose" -eq 2 ] || { echo "FAIL: missing prose should exit 2, got $missing_prose" >&2; exit 1; }
[ "$missing_value" -eq 2 ] || { echo "FAIL: --outline without a value should exit 2, got $missing_value" >&2; exit 1; }

echo "OK: outline-copy detector blocks 16+ characters and honors exact refrain anchors"
