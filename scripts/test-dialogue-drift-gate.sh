#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
SCRIPT="$REPO_ROOT/skills/story-deslop/scripts/dialogue_drift_gate.js"
TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT
HISTORY="$TMP_DIR/正文"
mkdir -p "$HISTORY"

cat > "$HISTORY/第001章.md" <<'PROSE'
“你来了。”
他放下茶杯。
“是。”
她把门关上。
PROSE

cat > "$HISTORY/第002章.md" <<'PROSE'
“走吧。”张三说。
他拎起箱子。
“等等。”李四问。
她回头看了一眼门。
“没时间了。”张三说。
风吹掉了桌上的纸。
“那就走。”李四问。
两人一前一后出了门。
“灯呢？”张三说。
她又折回去。
“不用管。”李四问。
PROSE

set +e
node "$SCRIPT" --current "$HISTORY/第002章.md" --history-dir "$HISTORY" --json > "$TMP_DIR/advisory.json"
advisory_status=$?
set -e
[ "$advisory_status" -eq 0 ] || { echo "FAIL: density warnings alone must not reject" >&2; exit 1; }
node - "$TMP_DIR/advisory.json" <<'NODE'
const fs = require('fs');
const report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
if (report.status !== 'passed' || report.findings.length !== 0 || report.advisories.length === 0) throw new Error(JSON.stringify(report));
if (report.next_action !== 'continue_with_semantic_dialogue_review') throw new Error(report.next_action);
NODE

cat > "$HISTORY/第003章.md" <<'PROSE'
“你来了。”张三说。
“我来了。”李四说。
“东西呢？”张三问。
“在这里。”李四回答。
“打开。”张三说。
PROSE
set +e
node "$SCRIPT" --current "$HISTORY/第003章.md" --history-dir "$HISTORY" --json > "$TMP_DIR/rejected.json"
rejected_status=$?
set -e
[ "$rejected_status" -eq 2 ] || { echo "FAIL: five consecutive tagged turns must reject" >&2; exit 1; }
node - "$TMP_DIR/rejected.json" <<'NODE'
const fs = require('fs');
const report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const finding = report.findings.find((item) => item.code === 'consecutive-tagged-turns');
if (report.status !== 'rejected' || !finding || report.metrics.max_consecutive_tagged_turns !== 5) throw new Error(JSON.stringify(report));
if (report.next_action !== 'return_to_narrative_writer_for_contextual_revision') throw new Error(report.next_action);
if (report.examples.length < 5 || !report.examples.every((item) => item.line && item.context)) throw new Error('missing line evidence');
NODE

# 误报回归：“没有回答 / 只想问 / 有人喊”是叙事动作，不是话语标签。
cat > "$TMP_DIR/narrative-actions.md" <<'PROSE'
他没有回答，只望向窗外。“今晚别等我。”
她只想问，却又捏紧衣角。“你还回来吗？”
楼下忽然有人喊，脚步声全乱了。“快走！”
他仍旧没有回答，把门带上。“照顾好自己。”
PROSE
node "$SCRIPT" --current "$TMP_DIR/narrative-actions.md" --json > "$TMP_DIR/narrative-actions.json"
node - "$TMP_DIR/narrative-actions.json" <<'NODE'
const fs = require('fs');
const report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
if (report.status !== 'passed' || report.metrics.attribution_count !== 0) throw new Error(JSON.stringify(report));
NODE

# 真实四连话语标签仍必须阻断；中等密度的语境化对话不误拦。
cat > "$TMP_DIR/real-tags.md" <<'PROSE'
林舟说：“钥匙丢了。”
苏棠问：“丢在哪儿？”
林舟答：“车里。”
苏棠说道：“你去拿。”
PROSE
set +e
node "$SCRIPT" --current "$TMP_DIR/real-tags.md" --json > "$TMP_DIR/real-tags.json"
real_status=$?
set -e
[ "$real_status" -eq 2 ] || { echo "FAIL: four real consecutive tags must reject" >&2; exit 1; }
node - "$TMP_DIR/real-tags.json" <<'NODE'
const fs = require('fs');
const report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
if (report.metrics.max_consecutive_tagged_turns !== 4) throw new Error(JSON.stringify(report));
NODE

cat > "$TMP_DIR/moderate.md" <<'PROSE'
“账本呢？”苏棠问。
林舟没有看她，只把抽屉推回去。“烧了。”
“你撒谎。”
雨点打在铁皮棚上，一阵紧过一阵。
“昨晚十点，你还拿它去见过陈叔。”她把手机扣在桌面。
林舟盯着那只手机。“谁告诉你的？”
“重要吗？”
门外的脚步停住了。
林舟压低声音说：“后门，快走。”
PROSE
node "$SCRIPT" --current "$TMP_DIR/moderate.md" --json > "$TMP_DIR/moderate.json"
node - "$TMP_DIR/moderate.json" <<'NODE'
const fs = require('fs');
const report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
if (report.status !== 'passed') throw new Error(JSON.stringify(report));
NODE

set +e
node "$SCRIPT" --current "$HISTORY/不存在.md" >/dev/null 2>&1
missing_status=$?
set -e
[ "$missing_status" -eq 3 ] || { echo "FAIL: unreadable input should exit 3" >&2; exit 1; }

# 候选章文件名 candidate.md 不含章号：必须由 --chapter 显式给出，跨章基线才会读取 正文/。
PROJECT="$TMP_DIR/project"
RUN_DIR="$PROJECT/追踪/候选章/第003章/C1"
mkdir -p "$PROJECT/正文" "$RUN_DIR"
cp "$HISTORY/第001章.md" "$PROJECT/正文/第001章.md"
cat > "$PROJECT/正文/第002章.md" <<'PROSE'
“走吧。”
他拎起箱子。
“等等。”
她回头看了一眼门。
PROSE
cp "$HISTORY/第002章.md" "$RUN_DIR/candidate.md"
CANDIDATE="$RUN_DIR/candidate.md"

node "$SCRIPT" --current "$CANDIDATE" --json > "$TMP_DIR/candidate-noflag.json"
node "$SCRIPT" --current "$CANDIDATE" --chapter 3 --json > "$TMP_DIR/candidate-chapter.json"
node "$SCRIPT" --current "$CANDIDATE" --chapter 3 --project "$PROJECT" --json > "$TMP_DIR/candidate-project.json"
cp "$CANDIDATE" "$TMP_DIR/loose-candidate.md"
node "$SCRIPT" --current "$TMP_DIR/loose-candidate.md" --chapter 3 --json > "$TMP_DIR/candidate-loose.json"
node - "$TMP_DIR" <<'NODE'
const fs = require('fs');
const path = require('path');
const load = (name) => JSON.parse(fs.readFileSync(path.join(process.argv[2], name), 'utf8'));
const codes = (report) => report.advisories.map((item) => item.code);
const noflag = load('candidate-noflag.json');
if (noflag.metrics.baseline_density !== null || noflag.metrics.recent_density !== null) throw new Error(`no-flag run must not have a baseline: ${JSON.stringify(noflag.metrics)}`);
if (codes(noflag).includes('baseline-drift') || codes(noflag).includes('chapter-spike')) throw new Error(JSON.stringify(noflag.advisories));
if (!noflag.history_notice || noflag.history_notice.code !== 'history-baseline-skipped' || !noflag.history_notice.message.includes('--chapter')) {
  throw new Error(`missing history-baseline-skipped notice: ${JSON.stringify(noflag.history_notice)}`);
}
for (const name of ['candidate-chapter.json', 'candidate-project.json']) {
  const report = load(name);
  if (report.metrics.baseline_density !== 0 || report.metrics.recent_density !== 0) throw new Error(`${name}: history baseline not loaded: ${JSON.stringify(report.metrics)}`);
  if (!codes(report).includes('baseline-drift') || !codes(report).includes('chapter-spike')) throw new Error(`${name}: ${JSON.stringify(report.advisories)}`);
  if (report.history_notice) throw new Error(`${name}: unexpected notice ${JSON.stringify(report.history_notice)}`);
  if (report.status !== noflag.status || report.metrics.attribution_density !== noflag.metrics.attribution_density) throw new Error(`${name}: current-chapter metrics must not change`);
}
const loose = load('candidate-loose.json');
if (loose.metrics.baseline_density !== null || !loose.history_notice || loose.history_notice.code !== 'history-baseline-empty') {
  throw new Error(`--chapter without a findable 正文/ must say the baseline is empty: ${JSON.stringify(loose)}`);
}
NODE

# 文本模式：跳过基线时 stderr 给出一行提示，stdout 与退出码保持不变。
node "$SCRIPT" --current "$CANDIDATE" > "$TMP_DIR/candidate-text.out" 2> "$TMP_DIR/candidate-text.err"
[ "$(grep -c 'NOTICE \[history-baseline-skipped\].*--chapter' "$TMP_DIR/candidate-text.err")" -eq 1 ] || { echo "FAIL: text mode must print one history-baseline-skipped notice on stderr" >&2; cat "$TMP_DIR/candidate-text.err" >&2; exit 1; }
grep -q '^PASS:' "$TMP_DIR/candidate-text.out" || { echo "FAIL: notice must not replace the PASS line" >&2; exit 1; }
node "$SCRIPT" --current "$CANDIDATE" --chapter 3 > /dev/null 2> "$TMP_DIR/candidate-chapter.err"
[ ! -s "$TMP_DIR/candidate-chapter.err" ] || { echo "FAIL: --chapter run must not print a notice" >&2; cat "$TMP_DIR/candidate-chapter.err" >&2; exit 1; }

# 已有章号的正式正文不受影响，也不出提示。
node "$SCRIPT" --current "$HISTORY/第002章.md" --history-dir "$HISTORY" > /dev/null 2> "$TMP_DIR/named.err"
[ ! -s "$TMP_DIR/named.err" ] || { echo "FAIL: named chapter must not print a notice" >&2; exit 1; }

# 非法 --chapter / --project 必须以非零码明确失败。
for bad in 0 -1 abc 3.5 "" 1e3 " 3"; do
  set +e
  node "$SCRIPT" --current "$CANDIDATE" --chapter "$bad" > /dev/null 2> "$TMP_DIR/bad-chapter.err"
  bad_status=$?
  set -e
  [ "$bad_status" -eq 3 ] || { echo "FAIL: --chapter '$bad' should exit 3, got $bad_status" >&2; exit 1; }
  grep -q -- '--chapter needs a positive integer' "$TMP_DIR/bad-chapter.err" || { echo "FAIL: --chapter '$bad' error message" >&2; cat "$TMP_DIR/bad-chapter.err" >&2; exit 1; }
done
set +e
node "$SCRIPT" --current "$CANDIDATE" --chapter > /dev/null 2>&1
dangling_status=$?
node "$SCRIPT" --current "$CANDIDATE" --chapter 3 --project "$TMP_DIR/没有这个项目" > /dev/null 2>&1
project_status=$?
set -e
[ "$dangling_status" -eq 3 ] || { echo "FAIL: --chapter without a value should exit 3" >&2; exit 1; }
[ "$project_status" -eq 3 ] || { echo "FAIL: missing --project root should exit 3" >&2; exit 1; }

# ── 按书上限与报幕式标签 ─────────────────────────────────────────────────
# 报幕式：主语 + 至多 4 字修饰 + 说话动词，无动作分句。连续 3 行阻断，即使带标记连续数没超 3。
cat > "$TMP_DIR/bare-run.md" <<'PROSE'
沈烬说道：“合同在这里。”
苏幼薇低声说道：“我看过了。”
沈烬又问道：“那你签不签？”
窗外的雨一直没停。
PROSE
set +e
node "$SCRIPT" --current "$TMP_DIR/bare-run.md" --json > "$TMP_DIR/bare-run.json"
bare_status=$?
set -e
[ "$bare_status" -eq 2 ] || { echo "FAIL: three consecutive bare announcements must reject, got $bare_status" >&2; exit 1; }
node - "$TMP_DIR/bare-run.json" <<'NODE'
const fs = require('fs');
const report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const codes = report.findings.map((item) => item.code);
if (!codes.includes('consecutive-bare-tagged-turns') || codes.includes('consecutive-tagged-turns')) throw new Error(JSON.stringify(report.findings));
const m = report.metrics;
if (m.max_consecutive_bare_tagged_turns !== 3 || m.max_consecutive_bare_tagged_turns_limit !== 2 || m.max_consecutive_bare_tagged_turns_limit_source !== 'default') throw new Error(JSON.stringify(m));
if (m.max_consecutive_bare_tagged_turns_run.start !== 1 || m.max_consecutive_bare_tagged_turns_run.end !== 3) throw new Error(JSON.stringify(m.max_consecutive_bare_tagged_turns_run));
NODE

# 前置动作冒号式（G5）：每行都有独立动作分句，不算报幕；连续 4 行只撞带标记上限。
cat > "$TMP_DIR/action-led.md" <<'PROSE'
沈烬拉开椅子坐下，小声问道：“你早就知道了？”
苏幼薇把茶杯推到他面前，轻声回答道：“猜到一点。”
沈烬盯着杯里的茶叶，又问道：“那为什么不说？”
苏幼薇站起来走到窗边，背对着他说道：“说了你会信吗？”
PROSE
set +e
node "$SCRIPT" --current "$TMP_DIR/action-led.md" --json > "$TMP_DIR/action-led-default.json"
action_default=$?
set -e
[ "$action_default" -eq 2 ] || { echo "FAIL: four tagged turns must still reject at the default limit" >&2; exit 1; }
node "$SCRIPT" --current "$TMP_DIR/action-led.md" --max-consecutive-tagged-turns 4 --json > "$TMP_DIR/action-led-cli.json"
node - "$TMP_DIR" <<'NODE'
const fs = require('fs');
const path = require('path');
const load = (name) => JSON.parse(fs.readFileSync(path.join(process.argv[2], name), 'utf8'));
const strict = load('action-led-default.json');
if (!strict.findings.every((item) => item.code === 'consecutive-tagged-turns')) throw new Error(JSON.stringify(strict.findings));
if (strict.metrics.bare_tagged_count !== 0) throw new Error(`action-led lines must not be bare: ${JSON.stringify(strict.examples)}`);
const cli = load('action-led-cli.json');
if (cli.status !== 'passed' || cli.metrics.max_consecutive_tagged_turns_limit !== 4 || cli.metrics.max_consecutive_tagged_turns_limit_source !== 'cli') throw new Error(JSON.stringify(cli.metrics));
NODE

# 门禁配置覆盖：--project 时读 设定/门禁配置.json → dialogue；命令行优先于配置。
BOOK="$TMP_DIR/book"
mkdir -p "$BOOK/设定" "$BOOK/正文"
cp "$TMP_DIR/action-led.md" "$BOOK/第004章.md"
cat > "$BOOK/设定/门禁配置.json" <<'JSON'
{"schema_version": 1, "dialogue": {"max_consecutive_tagged_turns": 5, "reason": "全书前置动作冒号式对白"}}
JSON
node "$SCRIPT" --current "$BOOK/第004章.md" --project "$BOOK" --json > "$TMP_DIR/config-limit.json"
set +e
node "$SCRIPT" --current "$BOOK/第004章.md" --project "$BOOK" --max-consecutive-tagged-turns 3 --json > "$TMP_DIR/cli-over-config.json"
cli_over_status=$?
set -e
[ "$cli_over_status" -eq 2 ] || { echo "FAIL: a CLI limit must override the book config" >&2; exit 1; }
node - "$TMP_DIR/config-limit.json" <<'NODE'
const fs = require('fs');
const report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const m = report.metrics;
if (report.status !== 'passed' || m.max_consecutive_tagged_turns_limit !== 5 || m.max_consecutive_tagged_turns_limit_source !== '设定/门禁配置.json') throw new Error(JSON.stringify(m));
if (m.max_consecutive_bare_tagged_turns_limit !== 2 || m.max_consecutive_bare_tagged_turns_limit_source !== 'default') throw new Error(JSON.stringify(m));
NODE
for bad in '{"schema_version": 1, "dialogue": {"max_consecutive_tagged_turns": 0}}' '{"schema_version": 1, "dialogue": []}' '{not json'; do
  printf '%s\n' "$bad" > "$BOOK/设定/门禁配置.json"
  set +e
  node "$SCRIPT" --current "$BOOK/第004章.md" --project "$BOOK" --json > /dev/null 2>&1
  bad_config_status=$?
  set -e
  [ "$bad_config_status" -eq 3 ] || { echo "FAIL: bad dialogue config '$bad' should exit 3, got $bad_config_status" >&2; exit 1; }
done

# 报幕式占比 advisory：5 处报幕、不连续，只提示不阻断。
cat > "$TMP_DIR/bare-ratio.md" <<'PROSE'
沈烬说道：“走吧。”
雨下得更大了。
苏幼薇说道：“去哪？”
两人站在廊下没动。
沈烬说道：“回学院。”
远处有车灯扫过来。
苏幼薇问道：“现在？”
沈烬把伞撑开。
沈烬说道：“现在。”
PROSE
node "$SCRIPT" --current "$TMP_DIR/bare-ratio.md" --json > "$TMP_DIR/bare-ratio.json"
node - "$TMP_DIR/bare-ratio.json" <<'NODE'
const fs = require('fs');
const report = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
if (report.status !== 'passed' || report.findings.length !== 0) throw new Error(JSON.stringify(report.findings));
if (!report.advisories.some((item) => item.code === 'bare-tag-ratio') || report.metrics.bare_tagged_count !== 5) throw new Error(JSON.stringify(report));
NODE

set +e
node "$SCRIPT" --current "$TMP_DIR/bare-run.md" --max-consecutive-bare-tagged-turns 0 > /dev/null 2>&1
zero_status=$?
set -e
[ "$zero_status" -eq 3 ] || { echo "FAIL: a non-positive CLI limit should exit 3" >&2; exit 1; }

echo "OK: dialogue drift gate keeps regressions, rejects real tag runs, avoids narrative-action false positives, baselines candidates via --chapter, flags bare-announcement runs, and honors per-book limits"
