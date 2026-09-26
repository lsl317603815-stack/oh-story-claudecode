#!/usr/bin/env node
"use strict";

// 行为回归：skills/story-deslop/scripts/check-typos.js
// （共享组 prose-typo-checker 的 source）。夹具全部写进临时目录。

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const repoRoot = path.resolve(__dirname, "..");
const SOURCE = "skills/story-deslop/scripts/check-typos.js";
const GROUP = "prose-typo-checker";
const checker = path.join(repoRoot, SOURCE);
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "check-typos-"));

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

// 把 finding 压成 "行:列 错→对"，便于整体断言位置与建议。
function brief(findings) {
  return findings.map((f) => {
    const m = /疑似错别字"([^"]+)"，常见正确写法是"([^"]+)"/.exec(f.message);
    assert(m, `unexpected message shape: ${f.message}`);
    return `${f.line}:${f.column} ${m[1]}→${m[2]}`;
  });
}

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

  // ── 干净章：正确写法与「张声」跨词边界的近邻都不报 ─────────────────────────
  const clean = write("第002章_clean.md", [
    "# 第2章 对账",
    "",
    "林舟迫不及待地翻开账本，苏棠却没有声张，只把伞靠在墙边。",
    "她紧张声音都变了，伙计夸张声调地报数，掌柜主张声明作废。",
    "既然账对不上，即使是度假回来的二掌柜也得留下来核一遍。",
  ]);
  const cleanText = run(["--check", clean]);
  assert.strictEqual(cleanText.status, 0, cleanText.stdout + cleanText.stderr);
  assert.match(cleanText.stdout, /未发现词典收录的常见错别字/);
  assert.deepStrictEqual(runJson([clean]).findings, []);

  // ── 错字章：行号、列号（1 起）、同一行多处、排序与 note ────────────────────
  const dirty = write("第003章_dirty.md", [
    "# 第3章 旧账",
    "",
    "林舟迫不急待地翻开账本，苏棠却没张声。",
    "她说要去渡假，渡假之前先把账对清。",
    "即然如此，他也只能再接再励。",
  ]);
  const dirtyReport = runJson([dirty]);
  assert.strictEqual(dirtyReport.status, 1);
  assert.strictEqual(dirtyReport.stderr, "");
  assert.deepStrictEqual(brief(dirtyReport.findings), [
    "3:3 迫不急待→迫不及待",
    "3:16 没张声→没声张",
    "4:5 渡假→度假",
    "4:8 渡假→度假",
    "5:1 即然→既然",
    "5:10 再接再励→再接再厉",
  ]);
  for (const f of dirtyReport.findings) {
    assert.strictEqual(f.file, dirty);
    assert.strictEqual(f.type, "known-typo");
    assert.strictEqual(f.severity, "advisory", "the typo checker has no blocking tier");
    assert(f.excerpt && f.excerpt.length <= 80);
  }
  assert.strictEqual(dirtyReport.findings[0].excerpt, "林舟迫不急待地翻开账本，苏棠却没"); // 前后各 10 字的窗口
  assert.match(dirtyReport.findings[2].message, /（"度假"才对；"渡"只用于渡河\/渡人等跨越义）/, "optional note must be appended");
  assert.doesNotMatch(dirtyReport.findings[0].message, /（/, "entries without a note must not get an empty note");

  const dirtyText = run(["--check", dirty]);
  assert.strictEqual(dirtyText.status, 1);
  assert.match(dirtyText.stdout, /第003章_dirty\.md:3:3: \[advisory\] known-typo: 疑似错别字"迫不急待"/);
  assert.strictEqual(dirtyText.stdout.trim().split("\n").length, 6);

  // ── --fail-on：没有 blocking 分级，所以 blocking 永不失败；all（默认）有即失败 ─
  assert.strictEqual(run(["--fail-on=all", dirty]).status, 1);
  const blockingOnly = run(["--fail-on=blocking", dirty]);
  assert.strictEqual(blockingOnly.status, 0, blockingOnly.stderr);
  assert.match(blockingOnly.stdout, /\[advisory\] known-typo/, "findings must still be printed");
  assert.strictEqual(runJson(["--fail-on=blocking", dirty]).status, 0);

  // ── 「张声」只按否定搭配收，长搭配不重复计数 ──────────────────────────────
  const zhang = runJson([write("zhang.md", ["他不敢张声。", "她没有张声。", "你别张声。"])]);
  assert.deepStrictEqual(brief(zhang.findings), ["1:2 不敢张声→不敢声张", "2:2 没有张声→没有声张", "3:2 别张声→别声张"]);

  // ── 词典自洽：每条错法都能报出对应正确写法；正确写法全文零命中 ────────────
  const source = sourceBytes.toString("utf8");
  const dictBlock = /const TYPO_DICTIONARY = \[([\s\S]*?)\n\];/.exec(source);
  assert(dictBlock, "cannot locate TYPO_DICTIONARY in the checker");
  const pairs = [...dictBlock[1].matchAll(/^\s*\['([^']+)', '([^']+)'/gm)].map((m) => [m[1], m[2]]);
  assert(pairs.length >= 30, `dictionary unexpectedly small: ${pairs.length}`);
  const allWrong = runJson([write("all-wrong.md", pairs.map(([wrong]) => `他写下「${wrong}」两个字。`))]);
  assert.deepStrictEqual(
    brief(allWrong.findings),
    pairs.map(([wrong, correct], i) => `${i + 1}:5 ${wrong}→${correct}`),
    "every dictionary entry must fire exactly once on its own line"
  );
  const allCorrect = runJson([write("all-correct.md", pairs.map(([, correct]) => `他写下「${correct}」两个字。`))]);
  assert.deepStrictEqual(allCorrect.findings, [], "a correct form must never contain another entry's wrong form");

  // ── CRLF、相对路径、多文件 ──────────────────────────────────────────────
  const crlf = write("第003章_crlf.md", fs.readFileSync(dirty, "utf8").replace(/\n/g, "\r\n"));
  assert.deepStrictEqual(brief(runJson([crlf]).findings), brief(dirtyReport.findings));
  const relative = runJson(["第003章_dirty.md"]);
  assert(relative.findings.every((f) => f.file === "第003章_dirty.md"), "file field echoes the path as given");
  const batch = runJson([clean, dirty, crlf]);
  assert.strictEqual(batch.status, 1);
  assert.deepStrictEqual([...new Set(batch.findings.map((f) => f.file))], [dirty, crlf]);

  // ── 边界：空文件通过 ─────────────────────────────────────────────────────
  const empty = write("empty.md", "");
  const emptyText = run([empty]);
  assert.strictEqual(emptyText.status, 0, emptyText.stderr);
  assert.match(emptyText.stdout, /未发现词典收录的常见错别字/);
  assert.deepStrictEqual(runJson([empty]).findings, []);

  // ── 读取失败：退出 2，优先于 findings；--json 下不写 stderr、仍输出合法 JSON ─
  const missingPath = path.join(tmpDir, "missing.md");
  const missing = run([missingPath]);
  assert.strictEqual(missing.status, 2);
  assert.match(missing.stderr, /missing\.md: unable to read/);
  const missingWithDirty = runJson([dirty, missingPath]);
  assert.strictEqual(missingWithDirty.status, 2, "read failure must dominate finding exit code");
  assert.strictEqual(missingWithDirty.stderr, "");
  assert.strictEqual(missingWithDirty.findings.length, 6, "readable files are still reported");
  assert.strictEqual(run(["--fail-on=blocking", clean, missingPath]).status, 2);

  // ── CLI 契约 ──────────────────────────────────────────────────────────────
  const help = run(["--help"]);
  assert.strictEqual(help.status, 0);
  assert.match(help.stdout, /^Usage: node check-typos\.js/);
  assert.strictEqual(run(["-h"]).status, 0);
  for (const bad of [[], ["--check"], ["--json"], ["--fail-on=advisory", clean], ["--fix", clean]]) {
    const r = run(bad);
    assert.strictEqual(r.status, 2, `${JSON.stringify(bad)} must exit 2`);
    assert.match(r.stderr, /Usage:/, `${JSON.stringify(bad)} must print usage`);
  }
  assert.match(run([]).stderr, /No files provided/);
  assert.match(run(["--fix", clean]).stderr, /Unknown option: --fix/);
  // 只读：检查不得改动输入
  assert.strictEqual(fs.readFileSync(dirty, "utf8").includes("迫不急待"), true, "checker must never rewrite the file");

  console.log("PASS: check-typos dictionary hits, positions, notes, 张声 guard, fail-on, json, errors, shared copies");
} finally {
  fs.rmSync(tmpDir, { recursive: true, force: true });
}
