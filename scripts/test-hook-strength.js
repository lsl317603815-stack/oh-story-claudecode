#!/usr/bin/env node
"use strict";

// 行为回归：skills/story-long-write/scripts/check-hook-strength.js
// （共享组 prose-hook-strength-checker 的 source）。夹具全部写进临时目录。

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const repoRoot = path.resolve(__dirname, "..");
const SOURCE = "skills/story-long-write/scripts/check-hook-strength.js";
const GROUP = "prose-hook-strength-checker";
const checker = path.join(repoRoot, SOURCE);
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hook-strength-"));

function run(args) {
  return spawnSync(process.execPath, [checker, ...args], { cwd: tmpDir, encoding: "utf8" });
}

function runJson(args) {
  const result = run(["--json", ...args]);
  let report;
  try {
    report = JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(`--json output is not JSON (exit ${result.status}): ${result.stdout}${result.stderr}`);
  }
  return { status: result.status, stderr: result.stderr, findings: report.findings };
}

function write(name, lines) {
  const file = path.join(tmpDir, name);
  fs.writeFileSync(file, Array.isArray(lines) ? `${lines.join("\n")}\n` : lines, "utf8");
  return file;
}

// "type:severity" 列表（去掉 info），便于整体断言。
function actionable(findings) {
  return findings.filter((f) => f.severity !== "info").map((f) => `${f.type}:${f.severity}`).sort();
}

function stat(findings) {
  const info = findings.filter((f) => f.type === "hook-signal-stat");
  assert.strictEqual(info.length, 1, `expected exactly one hook stat: ${JSON.stringify(findings)}`);
  assert.strictEqual(info[0].severity, "info");
  return info[0].message;
}

// 中性叙述：不含九类钩子词表、否定式悬置、时点承诺、问号、总结句与风景开头。
const NEUTRAL = [
  "林舟把账本放回柜台，翻到昨天记过的那一页，用红笔在页边写下日期。",
  "苏棠从后门进来，把雨伞靠在墙边，顺手擦干了鞋底的泥。",
  "两个人对着账本核了一遍，数目和上个月一样，谁也挑不出错。",
  "他把算盘收进抽屉，锁好柜门，又把钥匙挂回墙上的钉子。",
  "伙计搬来两筐新到的茶叶，按老规矩一筐一筐过秤，报出斤两。",
  "苏棠在本子上记下数目，又把秤砣擦了一遍，放回原处。",
];
const BODY = [...NEUTRAL, ...NEUTRAL]; // 约 290 字，超过 220 字章尾窗口
const STRONG = "就在这时，门外的铃响起。"; // interrupt + reveal = 2 个信号
const WEAK = "就在这时，苏棠把灯吹灭了。"; // 只有 interrupt = 1 个信号
const REVEAL = "柜台上的信封写着他的名字。"; // 只有 reveal = 1 个信号
const chapter = (title, ending, extra = []) => [`# ${title}`, "", ...extra, ...BODY, ...(ending ? [ending] : [])];

try {
  // ── 共享副本：manifest 里登记的每个 target 必须与 source 字节一致 ──────────
  const manifest = JSON.parse(fs.readFileSync(path.join(repoRoot, "scripts/shared-assets.json"), "utf8"));
  const group = manifest.groups.find((g) => g.name === GROUP);
  assert(group, `shared-assets.json has no group ${GROUP}`);
  assert.strictEqual(group.source, SOURCE, "group source drifted from the path this test exercises");
  assert(group.targets.length > 0, "group must list at least one target");
  const sourceBytes = fs.readFileSync(checker);
  for (const target of group.targets) {
    assert(
      sourceBytes.equals(fs.readFileSync(path.join(repoRoot, target))),
      `${target} is not byte-identical to ${SOURCE}; run scripts/sync-shared-assets.py`
    );
  }

  // ── 黄金三章（按文件名 第NNN章 解析）：强钩通过、弱钩/无钩为 blocking ────────
  const g1 = write("第001章_开账.md", chapter("第1章 开账", STRONG));
  const g1Report = runJson([g1]);
  assert.strictEqual(g1Report.status, 0, JSON.stringify(g1Report.findings));
  assert.deepStrictEqual(actionable(g1Report.findings), []);
  assert.match(stat(g1Report.findings), /章尾钩子信号 2 个，类型 \[interrupt, reveal\]（第 1 章，定位 golden/);
  assert.strictEqual(g1Report.findings[0].file, g1, "findings must carry the input path");
  const g1Text = run(["--check", g1]);
  assert.strictEqual(g1Text.status, 0);
  assert.match(g1Text.stdout, /\[info\] hook-signal-stat/);

  const g2 = write("第002章_弱钩.md", chapter("第2章 弱钩", WEAK));
  const g2Report = runJson([g2]);
  assert.strictEqual(g2Report.status, 1);
  assert.deepStrictEqual(actionable(g2Report.findings), ["golden-three-weak-hook:blocking"]);
  assert.match(g2Report.findings.find((f) => f.type === "golden-three-weak-hook").message, /第 2 章属于黄金三章，章尾只有 1 个钩子信号/);
  assert.strictEqual(run(["--fail-on=blocking", g2]).status, 1, "golden weak hook must block under --fail-on=blocking");

  const g3 = write("第003章_无钩.md", chapter("第3章 无钩", null));
  const g3Report = runJson([g3]);
  assert.deepStrictEqual(actionable(g3Report.findings), ["ending-no-hook:blocking"]);
  const noHook = g3Report.findings.find((f) => f.type === "ending-no-hook");
  assert(noHook.excerpt && noHook.excerpt.length <= 70, "ending-no-hook must quote the tail");
  assert.strictEqual(run(["--fail-on=blocking", g3]).status, 1);
  const g3Text = run([g3]);
  assert.strictEqual(g3Text.status, 1);
  assert.match(g3Text.stdout, /第003章_无钩\.md: \[blocking\] ending-no-hook: /);

  // ── 正文章（第 4 章起）：无钩只是 advisory，弱钩不判 ────────────────────────
  const b4 = write("第004章_无钩.md", chapter("第4章 无钩", null));
  const b4Report = runJson([b4]);
  assert.deepStrictEqual(actionable(b4Report.findings), ["ending-no-hook:advisory"]);
  assert.match(stat(b4Report.findings), /章尾钩子信号 0 个，类型 \[无\]（第 4 章，定位 body/);
  assert.strictEqual(b4Report.status, 1, "default --fail-on=all fails on advisory");
  assert.strictEqual(run(["--fail-on=all", b4]).status, 1);
  const b4Blocking = run(["--check", "--fail-on=blocking", b4]);
  assert.strictEqual(b4Blocking.status, 0, "--fail-on=blocking ignores advisory");
  assert.match(b4Blocking.stdout, /\[advisory\] ending-no-hook/, "advisory must still be printed");
  const b5 = write("第005章_弱钩.md", chapter("第5章 弱钩", WEAK));
  assert.strictEqual(run([b5]).status, 0, "one signal is enough outside the golden three");

  // ── 只看章尾窗口：钩子词在章首、章尾 220 字内没有 → 仍判无钩 ──────────────
  const early = runJson([write("第006章_钩在前.md", chapter("第6章 钩在前", null, [STRONG]))]);
  assert.deepStrictEqual(actionable(early.findings), ["ending-no-hook:advisory"]);

  // ── 结构判定：末行是台词 → dialogue 钩；末尾问号 → question 钩 ─────────────
  const dialogue = runJson([write("第007章_台词.md", chapter("第7章 台词", "“明天把账本送过来。”"))]);
  assert.strictEqual(dialogue.status, 0, JSON.stringify(dialogue.findings));
  assert.match(stat(dialogue.findings), /章尾钩子信号 1 个，类型 \[dialogue\]/);
  const cornerQuote = runJson([write("第007章_直角引号.md", chapter("第7章 台词", "「明天把账本送过来。」"))]);
  assert.match(stat(cornerQuote.findings), /类型 \[dialogue\]/);
  const question = runJson([write("第008章_问号.md", chapter("第8章 问号", "那本旧账后来落在谁手里？"))]);
  assert.match(stat(question.findings), /章尾钩子信号 1 个，类型 \[question\]/);
  // 正则补的两类：时点承诺（九点以前）与否定式悬置（还没松开）
  const appointment = runJson([write("第009章_时点.md", chapter("第9章 时点", "货单要在九点以前交到码头。"))]);
  assert.match(stat(appointment.findings), /章尾钩子信号 1 个，类型 \[appointment\]/);
  const negated = runJson([write("第009章_否定.md", chapter("第9章 否定", "她攥着那把钥匙，一直还没松开。"))]);
  assert.match(stat(negated.findings), /章尾钩子信号 1 个，类型 \[suspend\]/);

  // ── 总结体收尾 & 风景开场：advisory ─────────────────────────────────────
  const summary = runJson([write("第020章_总结.md", chapter("第20章 总结", `他终于明白，这笔账从一开始就对不上。${STRONG}`))]);
  assert.deepStrictEqual(actionable(summary.findings), ["ending-summary-tell:advisory"]);
  assert.match(summary.findings.find((f) => f.type === "ending-summary-tell").message, /「终于明白」/);
  const scenery = runJson([write("第021章_风景.md", chapter("第21章 风景", STRONG, ["夜色落在河面上，码头的灯一盏一盏熄了。"]))]);
  assert.deepStrictEqual(actionable(scenery.findings), ["opening-scenery:advisory"]);
  assert.match(scenery.findings.find((f) => f.type === "opening-scenery").message, /「夜色」/);
  const sceneryWithDialogue = runJson([
    write("第022章_对话开场.md", chapter("第22章 对话开场", STRONG, ["“开门。”夜色落在河面上，码头的灯一盏一盏熄了。"])),
  ]);
  assert.deepStrictEqual(actionable(sceneryWithDialogue.findings), [], "dialogue in the first 60 chars exempts scenery");

  // ── 章号来源：文件名 > 首行标题（阿拉伯数字）> --chapter；都没有 → 正文章 ─────
  const titled = runJson([write("draft-title.md", chapter("第2章 标题定章", WEAK))]);
  assert.deepStrictEqual(actionable(titled.findings), ["golden-three-weak-hook:blocking"]);
  assert.match(stat(titled.findings), /第 2 章，定位 golden/);

  const anonymous = write("draft.md", chapter("无题", WEAK));
  const anonymousReport = runJson([anonymous]);
  assert.strictEqual(anonymousReport.status, 0);
  assert.match(stat(anonymousReport.findings), /第 \? 章，定位 body/);
  const asChapter2 = runJson(["--chapter=2", anonymous]);
  assert.deepStrictEqual(actionable(asChapter2.findings), ["golden-three-weak-hook:blocking"]);
  assert.match(stat(asChapter2.findings), /第 2 章，定位 golden/);
  assert.match(stat(runJson(["--chapter=4", anonymous]).findings), /第 4 章，定位 body/);

  const namedBody = write("第010章_文件名优先.md", chapter("第1章 标题与文件名冲突", WEAK));
  const namedReport = runJson(["--chapter=1", namedBody]);
  assert.strictEqual(namedReport.status, 0, "file name chapter number must win over title and --chapter");
  assert.match(stat(namedReport.findings), /第 10 章，定位 body/);

  // --position 覆盖一切自动判定
  const forcedGolden = runJson(["--position=golden", namedBody]);
  assert.deepStrictEqual(actionable(forcedGolden.findings), ["golden-three-weak-hook:blocking"]);
  assert.match(stat(forcedGolden.findings), /定位 golden/);
  for (const position of ["body", "key", "climax"]) {
    const forced = runJson([`--position=${position}`, g3]);
    assert.deepStrictEqual(actionable(forced.findings), ["ending-no-hook:advisory"], position);
    assert.match(stat(forced.findings), new RegExp(`定位 ${position}`));
    assert.strictEqual(run([`--position=${position}`, "--fail-on=blocking", g3]).status, 0);
  }

  // 首行中文数字章号（文档写明支持）。候选稿文件名 candidate.md 不带章号，章号只能从这里来；
  // CN_DIGITS 曾是主流程之后才初始化的模块级 const，这条路径会抛 ReferenceError。
  const cnTitle = write("draft-cn.md", chapter("第二章 中文章号", WEAK));
  const cnResult = run(["--json", cnTitle]);
  assert.doesNotMatch(cnResult.stderr, /ReferenceError/);
  const cnReport = JSON.parse(cnResult.stdout);
  assert.deepStrictEqual(actionable(cnReport.findings), ["golden-three-weak-hook:blocking"]);
  assert.match(stat(cnReport.findings), /第 2 章，定位 golden/);
  const cnBig = JSON.parse(run(["--json", write("draft-cn-big.md", chapter("第一百零五章 中文章号", WEAK))]).stdout);
  assert.match(stat(cnBig.findings), /第 105 章，定位 body/);

  // ── 相邻两章同一类钩子 → hook-type-repeat（只在章号真的相邻时判）───────────
  const r10 = write("第030章_信封.md", chapter("第30章 信封", REVEAL));
  const r11 = write("第031章_信封.md", chapter("第31章 信封", REVEAL));
  const r12 = write("第032章_信封.md", chapter("第32章 信封", REVEAL));
  const i11 = write("第031章_打断.md", chapter("第31章 打断", WEAK));
  const repeat = runJson([r10, r11]);
  assert.strictEqual(repeat.status, 1);
  const repeatHits = repeat.findings.filter((f) => f.type === "hook-type-repeat");
  assert.strictEqual(repeatHits.length, 1, JSON.stringify(repeat.findings));
  assert.strictEqual(repeatHits[0].file, r11, "repeat is reported on the later chapter");
  assert.strictEqual(repeatHits[0].severity, "advisory");
  assert.match(repeatHits[0].message, /第030章_信封\.md.*都只用了 reveal 一类钩子/);
  assert.strictEqual(run(["--fail-on=blocking", r10, r11]).status, 0);
  for (const [label, files] of [
    ["non-adjacent", [r10, r12]],
    ["reversed order", [r11, r10]],
    ["different kinds", [r10, i11]],
    ["multi-kind chapters", [g1, write("第002章_双钩.md", chapter("第2章 双钩", STRONG))]],
  ]) {
    const report = runJson(files);
    assert(!report.findings.some((f) => f.type === "hook-type-repeat"), `${label}: ${JSON.stringify(report.findings)}`);
  }

  // ── CRLF 与 LF 结果一致 ──────────────────────────────────────────────────
  const crlf = write("第003章_crlf.md", fs.readFileSync(g3, "utf8").replace(/\n/g, "\r\n"));
  assert.deepStrictEqual(actionable(runJson([crlf]).findings), ["ending-no-hook:blocking"]);

  // ── 边界：空文件 / 过短文件（去标点 <200 字）不判定，直接通过 ─────────────
  const empty = write("第001章_空.md", "");
  const emptyText = run(["--check", empty]);
  assert.strictEqual(emptyText.status, 0, emptyText.stderr);
  assert.match(emptyText.stdout, /钩子强度下限检查通过。/);
  assert.deepStrictEqual(runJson([empty]).findings, []);
  const short = write("第001章_短.md", ["# 第1章", ...NEUTRAL.slice(0, 3)]);
  assert.deepStrictEqual(runJson([short]).findings, []);

  // ── 读取失败：退出 2，优先于 findings；--json 下不写 stderr、仍输出合法 JSON ─
  const missingPath = path.join(tmpDir, "第099章_missing.md");
  const missing = run([missingPath]);
  assert.strictEqual(missing.status, 2);
  assert.match(missing.stderr, /第099章_missing\.md: unable to read/);
  const missingWithBlocking = runJson([g3, missingPath]);
  assert.strictEqual(missingWithBlocking.status, 2, "read failure must dominate finding exit code");
  assert.strictEqual(missingWithBlocking.stderr, "");
  assert.deepStrictEqual(actionable(missingWithBlocking.findings), ["ending-no-hook:blocking"]);
  assert.strictEqual(run([g1, missingPath]).status, 2);

  // ── CLI 契约 ──────────────────────────────────────────────────────────────
  const help = run(["--help"]);
  assert.strictEqual(help.status, 0);
  assert.match(help.stdout, /^Usage: node check-hook-strength\.js/);
  assert.strictEqual(run(["-h"]).status, 0);
  for (const bad of [
    [],
    ["--check"],
    ["--chapter=0", g1],
    ["--chapter=abc", g1],
    ["--chapter=1.5", g1],
    ["--position=middle", g1],
    ["--fail-on=advisory", g1],
    ["--strict", g1],
  ]) {
    const r = run(bad);
    assert.strictEqual(r.status, 2, `${JSON.stringify(bad)} must exit 2`);
    assert.match(r.stderr, /Usage:/, `${JSON.stringify(bad)} must print usage`);
  }
  assert.match(run(["--chapter=0", g1]).stderr, /--chapter must be a positive integer/);
  assert.match(run([]).stderr, /no input file given/);

  console.log("PASS: check-hook-strength golden-three gate, body advisories, chapter/position resolution, repeat, fail-on, json, errors, shared copies");
} finally {
  fs.rmSync(tmpDir, { recursive: true, force: true });
}
