#!/usr/bin/env node
"use strict";

// 行为回归：skills/story-long-write/scripts/check-emotion-floor.js
// （共享组 prose-emotion-floor-checker 的 source）。夹具全部写进临时目录。

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const repoRoot = path.resolve(__dirname, "..");
const SOURCE = "skills/story-long-write/scripts/check-emotion-floor.js";
const GROUP = "prose-emotion-floor-checker";
const checker = path.join(repoRoot, SOURCE);
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "emotion-floor-"));

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

function types(findings, severity) {
  return findings.filter((f) => !severity || f.severity === severity).map((f) => f.type).sort();
}

function stat(findings) {
  const info = findings.filter((f) => f.type === "emotion-density-stat");
  assert.strictEqual(info.length, 1, `expected exactly one density stat: ${JSON.stringify(findings)}`);
  assert.strictEqual(info[0].severity, "info");
  return info[0].message;
}

// 中性叙述：不含三通道词表里的任何词（含单字 砸/摔/撞），用来垫字数、造零体温段。
const NEUTRAL = [
  "林舟把账本放回柜台，翻到昨天记过的那一页，用红笔在页边写下日期。",
  "苏棠从后门进来，把雨伞靠在墙边，顺手擦干了鞋底的泥。",
  "两个人对着账本核了一遍，数目和上个月一样，谁也挑不出错。",
  "他把算盘收进抽屉，锁好柜门，又把钥匙挂回墙上的钉子。",
  "伙计搬来两筐新到的茶叶，按老规矩一筐一筐过秤，报出斤两。",
  "苏棠在本子上记下数目，又把秤砣擦了一遍，放回原处。",
];
function neutral(count) {
  return Array.from({ length: count }, (_, i) => NEUTRAL[i % NEUTRAL.length]);
}
// 有体温的三行：somatic 2 / impulse 4 / sense 3。
const SOMATIC = "她的指尖还带着雨水，喉咙里像塞了一团棉花。";
const IMPULSE = "他攥紧了那张收据，退了半步，话没说完就停住了。";
const SENSE = "茶水滚烫，杯沿却发凉，她舌根一阵发苦。";

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

  // ── 通过章：三通道都有落点 → 只有 info，退出 0 ──────────────────────────────
  const warm = write("第010章_warm.md", ["# 第10章 账房", "", ...neutral(6), SOMATIC, IMPULSE, SENSE, ...neutral(6)]);
  for (const pressure of ["low", "normal", "high"]) {
    const r = runJson([`--pressure=${pressure}`, warm]);
    assert.strictEqual(r.status, 0, `warm chapter must pass at ${pressure}: ${JSON.stringify(r.findings)}`);
    assert.deepStrictEqual(types(r.findings), ["emotion-density-stat"]);
    const message = stat(r.findings);
    assert.match(message, /somatic 2 \/ impulse 4 \/ sense 3/, message);
    assert.match(message, new RegExp(`pressure=${pressure}`), message);
    assert.strictEqual(r.findings[0].file, warm, "findings must carry the input path");
  }
  const warmText = run(["--check", warm]);
  assert.strictEqual(warmText.status, 0, warmText.stdout + warmText.stderr);
  assert.match(warmText.stdout, /\[info\] emotion-density-stat/);
  assert.doesNotMatch(warmText.stdout, /\[(blocking|advisory)\]/);

  // ── 零体温章：密度 0 → somatic-floor-critical blocking，三档都拦 ─────────────
  const flat = write("第011章_flat.md", ["# 第11章 对账", "", ...neutral(24)]);
  for (const pressure of ["low", "normal", "high"]) {
    const r = runJson(["--check", `--pressure=${pressure}`, flat]);
    assert.strictEqual(r.status, 1, `flat chapter must fail at ${pressure}`);
    assert(types(r.findings, "blocking").includes("somatic-floor-critical"), JSON.stringify(r.findings));
    assert.match(stat(r.findings), /情绪落地密度 0\.0\/千字（somatic 0 \/ impulse 0 \/ sense 0/);
  }
  // 高压章另有零体温段（>=500 字）与缺失控点两条 advisory；normal/low 下没有。
  assert.deepStrictEqual(
    types(runJson(["--pressure=high", flat]).findings, "advisory"),
    ["emotionless-run", "no-loss-of-control"]
  );
  assert.deepStrictEqual(types(runJson([flat]).findings, "advisory"), []);
  const flatText = run([flat]);
  assert.strictEqual(flatText.status, 1);
  assert.match(flatText.stdout, /\[blocking\] somatic-floor-critical/);
  // blocking 在 --fail-on=blocking 下同样拦截
  assert.strictEqual(run(["--fail-on=blocking", flat]).status, 1);

  // 标题行不计入正文：把词表塞进章名救不了零体温正文。
  const titled = write("第012章_title.md", ["# 第12章 心跳 冷汗 发抖 攥紧 滚烫 发麻", "", ...neutral(24)]);
  const titledReport = runJson([titled]);
  assert.strictEqual(titledReport.status, 1);
  assert(types(titledReport.findings).includes("somatic-floor-critical"), "heading hits must not count");

  // ── pressure 三档阈值：同一章 ~1.7/千字 → low 通过 / normal 建议 / high 硬拦 ───
  // 两个落点（somatic 指尖 + impulse 攥紧）落在约 1150 字里，最长零体温段 < 500 字。
  const midDensity = write("第013章_mid.md", [
    "# 第13章 盘货",
    "",
    ...neutral(14),
    "她的指尖还沾着墨。",
    ...neutral(14),
    "他攥紧了笔杆。",
    ...neutral(14),
  ]);
  const mid = {};
  for (const pressure of ["low", "normal", "high"]) mid[pressure] = runJson([`--pressure=${pressure}`, midDensity]);
  const midStat = stat(mid.normal.findings);
  const midDensityValue = Number(/情绪落地密度 ([\d.]+)\/千字/.exec(midStat)[1]);
  assert(midDensityValue >= 1.5 && midDensityValue < 2.0, `fixture must sit between low floor and high critical: ${midStat}`);
  assert.match(midStat, /somatic 1 \/ impulse 1 \/ sense 0/);
  assert.match(midStat, /下限 3/);
  assert.strictEqual(mid.low.status, 0, JSON.stringify(mid.low.findings));
  assert.deepStrictEqual(types(mid.low.findings), ["emotion-density-stat"]);
  assert.match(stat(mid.low.findings), /下限 1\.5/);
  assert.strictEqual(mid.normal.status, 1);
  assert.deepStrictEqual(types(mid.normal.findings), ["emotion-density-stat", "somatic-floor-low"]);
  assert.strictEqual(mid.high.status, 1);
  assert.deepStrictEqual(types(mid.high.findings), ["emotion-density-stat", "somatic-floor-critical"]);
  assert.match(stat(mid.high.findings), /下限 5/);
  // 默认 pressure 就是 normal
  assert.deepStrictEqual(runJson([midDensity]).findings, mid.normal.findings);

  // ── --fail-on：advisory-only 章默认退出 1，--fail-on=blocking 退出 0 ─────────
  assert.strictEqual(run([midDensity]).status, 1);
  assert.strictEqual(run(["--fail-on=all", midDensity]).status, 1);
  const advisoryOnly = run(["--check", "--fail-on=blocking", midDensity]);
  assert.strictEqual(advisoryOnly.status, 0, advisoryOnly.stdout + advisoryOnly.stderr);
  assert.match(advisoryOnly.stdout, /\[advisory\] somatic-floor-low/, "advisory must still be printed");

  // ── 零体温长段：落点全在开头，后面 ~1600 字（66 行）没有体温 ─────────────
  const longRunLines = ["# 第14章 誊账", "", SOMATIC, IMPULSE, SENSE, ...neutral(66)];
  const longRun = write("第014章_run.md", longRunLines);
  const firstNeutralLine = longRunLines.indexOf(NEUTRAL[0]) + 1;
  const runNormal = runJson([longRun]);
  assert.strictEqual(runNormal.status, 1);
  const runCritical = runNormal.findings.find((f) => f.type === "emotionless-run-critical");
  assert(runCritical, JSON.stringify(runNormal.findings));
  assert.strictEqual(runCritical.severity, "blocking");
  assert.strictEqual(runCritical.line, firstNeutralLine, "run must point at the first cold line");
  assert.match(runCritical.message, /硬上限 1500 字/);
  assert(!types(runNormal.findings).includes("somatic-floor-low"), "density itself is above the floor");
  // low 档的零体温上限更宽：同一段只是 advisory，--fail-on=blocking 放行。
  const runLow = runJson(["--pressure=low", longRun]);
  assert.deepStrictEqual(types(runLow.findings), ["emotion-density-stat", "emotionless-run"]);
  assert.match(runLow.findings.find((f) => f.type === "emotionless-run").message, /建议上限 1200 字/);
  assert.strictEqual(run(["--pressure=low", "--fail-on=blocking", longRun]).status, 0);
  const runText = run([longRun]);
  assert.match(runText.stdout, new RegExp(`第014章_run\\.md:${firstNeutralLine}: \\[blocking\\] emotionless-run-critical`));

  // ── 单通道刷量：8 个 somatic、其他通道为 0 ─────────────────────────────────
  const spam = write("第015章_spam.md", [
    "# 第15章 夜查",
    "",
    ...neutral(5),
    "她心跳很快，指尖全是汗，喉咙发干。",
    "他的呼吸很乱，额头全是冷汗，嘴唇抿得很紧。",
    "她后背贴着门板，掌心里全是水。",
    ...neutral(5),
  ]);
  const spamNormal = runJson([spam]);
  assert.strictEqual(spamNormal.status, 1);
  assert.match(stat(spamNormal.findings), /somatic 8 \/ impulse 0 \/ sense 0/);
  assert.deepStrictEqual(types(spamNormal.findings, "blocking"), ["single-channel-spam"]);
  assert.match(spamNormal.findings.find((f) => f.type === "single-channel-spam").message, /100% 的情绪落点/);
  assert.deepStrictEqual(types(spamNormal.findings, "advisory"), ["channel-narrow"]);
  // low 档只要求 1 个通道 → 没有 channel-narrow，但刷量仍是 blocking
  const spamLow = runJson(["--pressure=low", spam]);
  assert.deepStrictEqual(types(spamLow.findings), ["emotion-density-stat", "single-channel-spam"]);
  assert.strictEqual(run(["--pressure=low", "--fail-on=blocking", spam]).status, 1);

  // ── 精致反应复读：「心口一沉」4 次 blocking，3 次放行 ────────────────────────
  const clicheLines = (n) => [
    "# 第16章 翻供",
    "",
    ...neutral(4),
    ...Array.from({ length: n }, (_, i) => `${NEUTRAL[i]}她心口一沉。`),
    IMPULSE,
    SENSE,
    ...neutral(4),
  ];
  const cliche4 = runJson([write("第016章_cliche4.md", clicheLines(4))]);
  assert.strictEqual(cliche4.status, 1);
  const clicheHit = cliche4.findings.find((f) => f.type === "single-channel-spam");
  assert(clicheHit, JSON.stringify(cliche4.findings));
  assert.strictEqual(clicheHit.severity, "blocking");
  assert.match(clicheHit.message, /「心口一沉」在本章出现 4 次/);
  assert.match(stat(cliche4.findings), /somatic 4 \/ impulse 4 \/ sense 3/, "ratio alone must not trigger spam");
  const cliche3 = runJson([write("第016章_cliche3.md", clicheLines(3))]);
  assert.strictEqual(cliche3.status, 0, JSON.stringify(cliche3.findings));

  // ── 高压章缺失控点：somatic + sense 足量但 impulse 为 0 ─────────────────────
  const composed = write("第017章_composed.md", ["# 第17章 对峙", "", ...neutral(6), SOMATIC, SENSE, SOMATIC, SENSE, ...neutral(6)]);
  assert.strictEqual(run([composed]).status, 0, "normal pressure does not demand loss of control");
  const composedHigh = runJson(["--pressure=high", composed]);
  assert.deepStrictEqual(types(composedHigh.findings), ["emotion-density-stat", "no-loss-of-control"]);
  assert.strictEqual(composedHigh.status, 1);
  assert.strictEqual(run(["--pressure=high", "--fail-on=blocking", composed]).status, 0);

  // ── 多文件：pressure 作用于整批；任一文件不达标即退出 1 ──────────────────────
  const batch = runJson([warm, midDensity]);
  assert.strictEqual(batch.status, 1);
  assert.deepStrictEqual([...new Set(batch.findings.map((f) => f.file))], [warm, midDensity]);
  assert.deepStrictEqual(
    batch.findings.filter((f) => f.severity !== "info").map((f) => [path.basename(f.file), f.type]),
    [["第013章_mid.md", "somatic-floor-low"]]
  );

  // ── CRLF 与 LF 结果一致 ──────────────────────────────────────────────────
  const crlf = write("第013章_crlf.md", fs.readFileSync(midDensity, "utf8").replace(/\n/g, "\r\n"));
  assert.deepStrictEqual(
    runJson([crlf]).findings.map(({ file, ...rest }) => rest),
    mid.normal.findings.map(({ file, ...rest }) => rest)
  );

  // ── 边界：空文件 / 过短文件（<200 字）不判定，直接通过 ─────────────────────
  const empty = write("empty.md", "");
  const emptyText = run(["--check", empty]);
  assert.strictEqual(emptyText.status, 0, emptyText.stderr);
  assert.match(emptyText.stdout, /情绪落地下限检查通过。/);
  assert.deepStrictEqual(runJson([empty]).findings, []);
  const short = write("short.md", ["# 短章", ...neutral(3)]);
  assert.deepStrictEqual(runJson(["--pressure=high", short]).findings, []);

  // ── 读取失败：退出 2，优先于 findings；--json 下不写 stderr、仍输出合法 JSON ─
  const missingPath = path.join(tmpDir, "missing.md");
  const missing = run([missingPath]);
  assert.strictEqual(missing.status, 2);
  assert.match(missing.stderr, /missing\.md: unable to read/);
  const missingWithFlat = runJson([flat, missingPath]);
  assert.strictEqual(missingWithFlat.status, 2, "read failure must dominate finding exit code");
  assert.strictEqual(missingWithFlat.stderr, "");
  assert(types(missingWithFlat.findings).includes("somatic-floor-critical"), "readable files are still reported");
  assert.strictEqual(run(["--fail-on=blocking", warm, missingPath]).status, 2);

  // ── CLI 契约 ──────────────────────────────────────────────────────────────
  const help = run(["--help"]);
  assert.strictEqual(help.status, 0);
  assert.match(help.stdout, /^Usage: node check-emotion-floor\.js/);
  assert.strictEqual(run(["-h"]).status, 0);
  for (const bad of [[], ["--check"], ["--pressure=extreme", warm], ["--fail-on=advisory", warm], ["--strict", warm]]) {
    const r = run(bad);
    assert.strictEqual(r.status, 2, `${JSON.stringify(bad)} must exit 2`);
    assert.match(r.stderr, /Usage:/, `${JSON.stringify(bad)} must print usage`);
  }
  assert.match(run(["--pressure=extreme", warm]).stderr, /unknown --pressure value: extreme/);
  assert.match(run([]).stderr, /no input file given/);

  console.log("PASS: check-emotion-floor pressure tiers, density/run/channel/cliche floors, fail-on, json, errors, shared copies");
} finally {
  fs.rmSync(tmpDir, { recursive: true, force: true });
}
