#!/usr/bin/env node
"use strict";

// 行为回归：skills/story-long-write/scripts/check-outline-retention.js
// 留存三字段硬门 + 细纲长度（字数目标×0.5 提醒 / ×0.7 阻断）+ 章卡上限 + 重复细节。
// 夹具全部写进临时目录；用法同仓库其它 test-*.js：node scripts/test-outline-retention.js

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const repoRoot = path.resolve(__dirname, "..");
const checker = path.join(repoRoot, "skills/story-long-write/scripts/check-outline-retention.js");
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "outline-retention-"));

function han(text) {
  const m = String(text).match(/[\u4e00-\u9fff]/g);
  return m ? m.length : 0;
}

// 不含数字、引号的填充句，避免误触重复细节检查。
const FILLER = [
  "林川推门进屋看见桌上摊着旧账册",
  "管家站在廊下低头不语手指发抖",
  "族老把茶盏放回案上让众人退后",
  "雨从檐角落下院里的灯笼被风吹斜",
  "她把信折好塞进袖口转身离开祠堂",
  "少年蹲在井边数着水面上漂过的落叶",
];

function filler(chars) {
  let out = "";
  let i = 0;
  while (han(out) < chars) out += FILLER[i++ % FILLER.length] + "，";
  // 精确裁到 chars 个汉字
  let n = 0;
  let cut = "";
  for (const ch of out) {
    if (/[\u4e00-\u9fff]/.test(ch)) {
      if (n === chars) break;
      n += 1;
    }
    cut += ch;
  }
  return cut;
}

function outline({ target = 2200, coreEvent = "林川在祠堂当众拿出旧账册，逼管家认账", plotChars = 0, extraPlot = [], dropRetention = false } = {}) {
  const lines = [
    "## 细纲（第 1 章）",
    "",
    "### 第 1 章：旧账",
    `- 核心事件：${coreEvent}`,
  ];
  if (target !== null) lines.push(`- 字数目标：${target} 字`);
  lines.push(
    "- 阶段位置：开篇期第一阶段第一章，立主角处境",
    "- 目标情绪：憋屈→硬气",
    "- 本章结构公式：账册现身（起疑）+ 当众对质（施压）+ 管家松口（兑现）",
    "- 爽点：当众拆穿管家",
  );
  if (!dropRetention) {
    lines.push(
      "- 本章兑现（收一个）：账册的来历给了交代",
      "- 状态变化（变一个）：林川从被怀疑者变成追账人",
    );
  }
  lines.push(
    "- 本章禁止提前释放：账册真正的主人",
    "",
    "---",
    "> 以下为写手单，作者可不读",
    "",
    "#### 内容概括（五段式）",
    "- 起因：见情节点一",
    "- 发展：见情节点二至四",
    "- 转折：管家改口",
    "- 高潮：当众对质",
    "- 结尾：林川合上账册",
    "",
    "#### 情节细化",
    `- 情节点序列：${filler(plotChars)}`,
    ...extraPlot,
    `- 预算合计：${target || 3000}字（目标${target || 3000}，范围${target || 3000}-${Math.round((target || 3000) * 1.1)}）`,
    "",
    "#### 结尾设定和钩子",
    "- 结尾设定：林川合上账册",
  );
  if (!dropRetention) lines.push("- 章尾余势（开一个）：账册夹层里还有一页");
  return lines.join("\n") + "\n";
}

// 把总汉字数调到 total：先量骨架，再补情节点。
function outlineWithTotal(total, opts = {}) {
  const base = han(outline({ ...opts, plotChars: 0 }));
  return outline({ ...opts, plotChars: Math.max(0, total - base) });
}

function write(name, text) {
  const file = path.join(tmpDir, name);
  fs.writeFileSync(file, text, "utf8");
  return file;
}

function run(args) {
  return spawnSync(process.execPath, [checker, ...args], { cwd: tmpDir, encoding: "utf8" });
}

function runJson(args) {
  const r = run(["--json", ...args]);
  let report;
  try {
    report = JSON.parse(r.stdout);
  } catch (error) {
    throw new Error(`--json 输出不是 JSON（exit ${r.status}）：${r.stdout}${r.stderr}`);
  }
  return { status: r.status, report, result: report.results[0] };
}

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed += 1;
    process.stdout.write(`ok - ${name}\n`);
  } catch (error) {
    process.stderr.write(`FAIL - ${name}\n${error.stack || error}\n`);
    process.exitCode = 1;
  }
}

try {
  test("100% 长度样例 → blocking（exit 1）", () => {
    const file = write("full.md", outlineWithTotal(2200));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 1);
    assert.ok(result.ratio >= 0.99 && result.ratio <= 1.01, `ratio=${result.ratio}`);
    const length = result.findings.find((f) => f.kind === "length" && f.field === "细纲长度");
    assert.ok(length, "应报细纲长度");
    assert.strictEqual(length.severity, "blocking");
    const text = run([file]);
    assert.strictEqual(text.status, 1);
    assert.match(text.stdout, /超过硬上限 70%/);
  });

  test("48% 样例 → 全绿（无任何 finding，exit 0）", () => {
    const file = write("lean.md", outlineWithTotal(1056));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    assert.ok(Math.abs(result.ratio - 0.48) < 0.005, `ratio=${result.ratio}`);
    assert.deepStrictEqual(result.findings, []);
    assert.deepStrictEqual(result.dups, []);
    assert.ok(result.card_chars > 0 && result.card_chars <= 350, `card_chars=${result.card_chars}`);
    assert.strictEqual(run(["--fail-on=all", file]).status, 0);
    assert.match(run([file]).stdout, /\[PASS\]/);
  });

  test("同一数字/台词重复 ≥3 次 → advisory（exit 0；--fail-on=all → 1）", () => {
    const file = write("dup.md", outline({
      plotChars: 200,
      coreEvent: "林川查出账上转走4800元",
      extraPlot: [
        "- 情节点二：管家辩称4800元是修祠堂的钱【信息揭示·密250】",
        "- 情节点三：族老问那4800元去了哪里，管家喊“我没拿过一文钱”",
        "- 情节点四：管家又喊“我没拿过一文钱”，三十星币的旧债被翻出",
        "- 情节点五：林川复述“我没拿过一文钱”，摊开第一页",
      ],
    }));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    const dup = result.findings.find((f) => f.kind === "dup");
    assert.ok(dup, "应报重复细节");
    assert.strictEqual(dup.severity, "advisory");
    const texts = Object.fromEntries(result.dups.map((d) => [d.text, d.count]));
    assert.strictEqual(texts["4800元"], 3);
    assert.strictEqual(texts["“我没拿过一文钱”"], 3);
    assert.ok(!("三十星币" in texts), "只出现一次不报");
    assert.ok(!("250" in texts) && !Object.keys(texts).some((k) => k.startsWith("2200")), "字数目标/预算不算细节");
    assert.strictEqual(run(["--fail-on=all", file]).status, 1);
  });

  test("50%-70% 之间 → 只 advisory", () => {
    const file = write("mid.md", outlineWithTotal(1320));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    const length = result.findings.find((f) => f.field === "细纲长度");
    assert.ok(length && length.severity === "advisory", JSON.stringify(result.findings));
  });

  test("缺字数目标 → 按 3000 代入并提示补纲", () => {
    const file = write("notarget.md", outlineWithTotal(2200, { target: null }));
    const { status, result } = runJson([file]);
    const m = result.metrics[0];
    assert.strictEqual(m.target, 3000);
    assert.strictEqual(m.target_defaulted, true);
    assert.ok(result.findings.some((f) => f.field === "字数目标" && f.severity === "advisory"));
    // 2200/3000 ≈ 73% → 仍按 3000 代入判硬上限
    assert.strictEqual(status, 1);
  });

  test("核心事件 >80 汉字、章卡 >350 → advisory", () => {
    const longCore = filler(90);
    const file = write("card.md", outline({ coreEvent: longCore, plotChars: 100 }).replace(
      "- 爽点：当众拆穿管家",
      `- 爽点：${filler(300)}`,
    ));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    assert.ok(result.findings.some((f) => f.field === "核心事件" && f.severity === "advisory"));
    assert.ok(result.findings.some((f) => f.field === "章卡" && f.severity === "advisory"));
    assert.ok(result.card_chars > 350);
  });

  test("留存字段缺失仍是 blocking（旧行为不变）", () => {
    const file = write("noretention.md", outlineWithTotal(900, { dropRetention: true }));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 1);
    assert.strictEqual(result.findings.filter((f) => f.kind === "retention").length, 3);
    assert.match(run([file]).stdout, /共 3 处留存字段缺失/);
  });
} finally {
  fs.rmSync(tmpDir, { recursive: true, force: true });
}

if (process.exitCode) process.exit(process.exitCode);
process.stdout.write(`check-outline-retention: ${passed} passed\n`);
