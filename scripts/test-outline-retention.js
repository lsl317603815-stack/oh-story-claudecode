#!/usr/bin/env node
"use strict";

// 行为回归：skills/story-long-write/scripts/check-outline-retention.js
// 留存三字段硬门 + 细纲长度（字数目标×0.6 提醒 / ×0.8 阻断）+ 章卡（上限与「不是人话」）+ 重复细节。
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

// 按现行模板排版：章卡 7 个字段 → 这章怎么演 → 人物与信息差 → 结尾 → --- 写手单。
function outline({ target = 2200, coreEvent = "林川在祠堂当众拿出旧账册，逼管家认下这笔账。", plotChars = 0, extraPlot = [], dropRetention = false, card = null } = {}) {
  const lines = [
    "## 细纲（第 1 章）",
    "",
    "### 第 1 章：旧账",
  ];
  if (card) {
    lines.push(...card);
  } else {
    lines.push(
      `- 核心事件：${coreEvent}`,
      "- 本章结构公式：先让账册在祠堂露面，再让林川当众对质，最后逼得管家松口。",
      "- 爽点：林川一句话拆穿管家，族老当场变了脸色。",
    );
    if (!dropRetention) {
      lines.push(
        "- 本章兑现（收一个）：读者惦记的账册来历在这一章有了交代。",
        "- 状态变化（变一个）：林川从被怀疑的人变成了追账的人。",
        "- 章尾余势（开一个）：账册夹层里还压着一页，读者想知道上面写了什么。",
      );
    }
    lines.push("- 本章禁止提前释放：账册真正的主人是谁，这一章不说破。");
  }
  lines.push(
    "",
    "#### 这章怎么演（情节点序列）",
    `1. 林川走进祠堂，${filler(plotChars)}（铺垫，约150字）`,
    ...extraPlot,
    `预算合计：${target || 3000}字（目标${target || 3000}，范围${target || 3000}-${Math.round((target || 3000) * 1.1)}）`,
    "",
    "#### 人物与信息差",
    "先出场的是林川和族老，管家最后才进门。管家以为没人见过那本账册，读者知道林川早就看过。",
    "",
    "#### 结尾",
    "本章收在林川合上账册。下一章从夹层那一页开头。",
    "",
    "---",
    "写手单（作者可不读）",
  );
  if (target !== null) lines.push(`- 字数目标：${target} 字`);
  lines.push(
    "- 阶段位置：开篇期第一阶段第一章，立主角处境",
    "- 单元ID/位置：L1-1第1拍",
    "- 目标情绪：憋屈→硬气",
    "- 契约风险：契约安全",
  );
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
    assert.match(text.stdout, /超过硬上限 80%/);
  });

  test("58% 样例 → 全绿（无任何 finding，exit 0）", () => {
    const file = write("lean.md", outlineWithTotal(1276));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    assert.ok(Math.abs(result.ratio - 0.58) < 0.005, `ratio=${result.ratio}`);
    assert.deepStrictEqual(result.findings, []);
    assert.deepStrictEqual(result.dups, []);
    assert.ok(result.card_chars > 0 && result.card_chars <= 400, `card_chars=${result.card_chars}`);
    assert.strictEqual(run(["--fail-on=all", file]).status, 0);
    assert.match(run([file]).stdout, /\[PASS\]/);
  });

  test("同一数字/台词重复 ≥3 次 → advisory（exit 0；--fail-on=all → 1）", () => {
    const file = write("dup.md", outline({
      plotChars: 200,
      coreEvent: "林川查出账上转走4800元",
      extraPlot: [
        "2. 管家辩称4800元是修祠堂的钱。（信息揭示，重点写，约250字）",
        "3. 族老问那4800元去了哪里，管家喊“我没拿过一文钱”。（对质，约200字）",
        "4. 管家又喊“我没拿过一文钱”，三十星币的旧债被翻了出来。（反转，约250字）",
        "5. 林川复述“我没拿过一文钱”，摊开第一页。（打脸，约260字）",
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

  test("60%-80% 之间 → 只 advisory", () => {
    const file = write("mid.md", outlineWithTotal(1540));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    const length = result.findings.find((f) => f.field === "细纲长度");
    assert.ok(length && length.severity === "advisory", JSON.stringify(result.findings));
  });

  test("缺字数目标 → 按 3000 代入并提示补纲", () => {
    const file = write("notarget.md", outlineWithTotal(2600, { target: null }));
    const { status, result } = runJson([file]);
    const m = result.metrics[0];
    assert.strictEqual(m.target, 3000);
    assert.strictEqual(m.target_defaulted, true);
    assert.ok(result.findings.some((f) => f.field === "字数目标" && f.severity === "advisory"));
    // 2600/3000 ≈ 87% → 仍按 3000 代入判硬上限
    assert.strictEqual(status, 1);
  });

  test("单项 >70 汉字、章卡 >400 → advisory", () => {
    const file = write("card.md", outline({ coreEvent: filler(90), plotChars: 100 }).replace(
      "- 爽点：林川一句话拆穿管家，族老当场变了脸色。",
      `- 爽点：${filler(60)}。\n- 补充说明：${filler(300)}`,
    ));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    const single = result.findings.find((f) => f.field === "章卡单项");
    assert.ok(single && single.severity === "advisory" && /核心事件 90 字/.test(single.reason), JSON.stringify(result.findings));
    assert.ok(result.findings.some((f) => f.field === "章卡" && f.severity === "advisory"));
    assert.ok(result.card_chars > 400);
  });

  test("箭头串/代号/名词堆章卡 → advisory「不是人话」", () => {
    const file = write("arrows.md", outline({
      plotChars: 200,
      card: [
        "- 核心事件：入册与星币＋腕表黑屏＋搭肩吸煞＋护短转场",
        "- 本章结构公式：院门照面＋藏拙＋醋意＋握腕写字",
        "- 爽点：好笑→火药味→担心→醋意",
        "- 本章兑现（收一个）：L1-2第4拍兑现",
        "- 状态变化（变一个）：搭档→心防松动",
        "- 章尾余势（开一个）：排期、霜桶、腕表、星网（如适用：强度中）",
        "- 本章禁止提前释放：无",
      ],
    }));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    const finding = result.findings.find((f) => f.field === "不是人话");
    assert.ok(finding && finding.severity === "advisory", JSON.stringify(result.findings));
    const labels = new Set(result.metrics[0].card_not_plain.map((x) => x.label));
    for (const label of ["箭头/加号串", "「L1-2」式代号", "「第N拍」代号", "两字词组堆", "标记语"]) {
      assert.ok(labels.has(label), `missing ${label}: ${[...labels]}`);
    }
    assert.strictEqual(run(["--fail-on=all", file]).status, 1);
  });

  test("情节点里的【·密250】旧记号和「/」串也算 → advisory", () => {
    const file = write("oldmarks.md", outline({
      plotChars: 200,
      extraPlot: ["2. 管家改口【信息揭示·密250】，铺垫/对质/兑现一起写"],
    }));
    const { result } = runJson([file]);
    const hits = result.metrics[0].card_not_plain;
    assert.ok(hits.some((x) => x.label === "【·密】旧记号" && x.field === "这章怎么演"), JSON.stringify(hits));
    assert.ok(hits.some((x) => x.label === "「/」串联"), JSON.stringify(hits));
  });

  test("句子化细纲 → 静默；写手单里的代号和箭头不算", () => {
    const file = write("plain.md", outlineWithTotal(1000));
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    assert.deepStrictEqual(result.findings, []);
    // 写手单里的「L1-1第1拍」「憋屈→硬气」在 --- 之后，不触发
    assert.deepStrictEqual(result.metrics[0].card_not_plain, []);
    assert.ok(result.card_chars > 0 && result.card_chars <= 400, `card_chars=${result.card_chars}`);
  });

  test("旧版细纲（五段式在前、没有 ---）：章卡退回第一个 ####，全文都扫", () => {
    const text = [
      "### 第 3 章：旧格式",
      "- 核心事件：林川查账",
      "- 目标情绪：憋屈→硬气",
      "- 本章兑现（收一个）：账册来历",
      "- 状态变化（变一个）：被怀疑→追账",
      "- 字数目标：2200 字",
      "#### 内容概括（五段式）",
      "- 起因：账册现身【铺垫·疏40】",
      "#### 结尾设定和钩子",
      "- 章尾余势（开一个）：夹层一页",
      "",
    ].join("\n");
    const file = write("legacy.md", text);
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    const hits = result.metrics[0].card_not_plain;
    assert.ok(hits.some((x) => x.field === "目标情绪" && x.label === "箭头/加号串"), JSON.stringify(hits));
    assert.ok(hits.some((x) => x.field === "内容概括" && x.label === "【·密】旧记号"), JSON.stringify(hits));
    assert.ok(result.findings.filter((f) => f.kind === "retention").length === 0);
  });

  test("模板（workflow-setup Phase 3）里的完整示例 → 零提醒", () => {
    const setup = fs.readFileSync(path.join(repoRoot, "skills/story-long-write/references/workflow-setup.md"), "utf8");
    const start = setup.indexOf("**完整示例**");
    assert.ok(start > 0, "workflow-setup.md 缺少「完整示例」");
    const block = setup.slice(start).split("```\n")[1];
    assert.ok(/### 第 7 章/.test(block) && /写手单/.test(block), "示例不完整");
    const file = write("example.md", block);
    const { status, result } = runJson([file]);
    assert.strictEqual(status, 0);
    assert.deepStrictEqual(result.findings, [], JSON.stringify(result.findings));
    assert.strictEqual(result.metrics[0].target, 2200);
    assert.ok(result.ratio <= 0.6, `ratio=${result.ratio}`);
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
