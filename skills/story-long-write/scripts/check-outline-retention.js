#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const USAGE = `Usage: node check-outline-retention.js [--json] [--fail-on=blocking|all] <细纲文件...>

把"章章推进 / 收一个、变一个、开一个"的留存节奏从文档自觉变成大纲层确定性硬门。
逐个检查细纲文件是否填齐三个留存字段（只查"字段在不在、填没填"，不判断内容语义质量）：

  - 本章兑现（收一个）   本章偿还哪笔期待债 / 付了什么利息（进展·收益·关系反馈·情绪释放）
  - 状态变化（变一个）   目标/风险/信息/关系/资源/身份/情绪立场 至少一项 章前 → 章后
  - 章尾余势（开一个）   留下的下一步（问题/决定/行动/关系变化/阶段目标/情绪余势）

只处理"能确定性判定"的部分。跨章语义问题（假钩子=下一章是否撤回、刺激通胀=跨章强度曲线、
本章兑现是否真的兑现）不在这里硬做——那些留给 story-review 审查 agent，正则会误杀。

一个字段判为缺失（blocking）当且仅当：整章找不到该字段行，或该字段冒号后仍是 {…} 模板占位、
或冒号后为空。填了真实内容（哪怕简短）即视为通过——不评判好坏，只保证"没被跳过"。

另查三类「细纲是否长过了头」（plot-core-methods「小纲四步法」：细纲:正文 = 1:2.5～1:3，
细纲只写目的和效果、不展开情节）：

  - 长度   汉字（按 [\u4e00-\u9fff] 计）> 字数目标×0.5 为 advisory，> ×0.7 为 blocking；
           缺「字数目标」时按 3000 代入并提示补纲（与 story-long-write SKILL.md「字数验收权威」同一兜底）
  - 章卡   第一个 #### 之前、作者要读的顶部字段区：字段值合计 > 350 汉字为 advisory
           （不计标题与字段名）；「核心事件」> 80 汉字为 advisory
  - 重复   同一引号台词（“…”/「…」，≥4 字）或同一「数字+量词」片段（如 4800元、三十星币、百分之二十）
           在一章细纲里出现 ≥3 次为 advisory，列前 5 个及次数。具体数字只该落在情节点序列那一处。

退出码：0 = 全部通过或只有 advisory；1 = 有 blocking（缺字段/占位/空，或超 0.7 硬上限）；
--fail-on=all 时 advisory 也返回 1；2 = 用法或读取错误。
--json 每个 result 带 ratio、card_chars、dups（多章文件取各章最大值/合并），逐章明细在 metrics。
`;

const FIELDS = [
  { key: '本章兑现', label: '本章兑现（收一个）' },
  { key: '状态变化', label: '状态变化（变一个）' },
  { key: '章尾余势', label: '章尾余势（开一个）' },
];

// 章节分节：模板用 "### 第 N 章：" 作为单章起点；无此标记时整文件按一章处理。
const CHAPTER_HEADER = /^#{2,4}\s*第\s*[0-9一二三四五六七八九十百千两]+\s*章/;

function isPlaceholder(value) {
  const v = value.trim();
  if (v === '') return true;
  // 冒号后仍是整段 {…} 模板占位（允许前后有少量修饰），视为未填。
  if (/^\{[^}]*\}$/.test(v)) return true;
  if (/^[（(]\s*待补充\s*[)）]$/.test(v) || v === '待补充' || v === 'TODO' || v === 'TBD') return true;
  return false;
}

function fieldValue(sectionText, label) {
  // 容忍 label 中括号/冒号的全半角；核心以中文字段名锚定，冒号用 [：:]。
  const core = label.replace(/[（(].*$/, ''); // 本章兑现 / 状态变化 / 章尾余势
  const bracket = label.match(/[（(](.+?)[)）]/); // 收一个 / 变一个 / 开一个
  const tag = bracket ? bracket[1] : '';
  const re = new RegExp(
    `^\\s*[-*]?\\s*${core}\\s*[（(]\\s*${tag}\\s*[)）]\\s*[：:]\\s*(.*)$`,
    'm'
  );
  const m = sectionText.match(re);
  if (m) return { present: true, value: m[1] };
  // 退一步：只按核心字段名 + 冒号匹配（容忍作者漏写括注）。
  const re2 = new RegExp(`^\\s*[-*]?\\s*${core}\\s*[（(][^)）]*[)）]?\\s*[：:]\\s*(.*)$`, 'm');
  const m2 = sectionText.match(re2);
  if (m2) return { present: true, value: m2[1] };
  return { present: false, value: '' };
}

function splitChapters(text) {
  const lines = text.split('\n');
  const idx = [];
  lines.forEach((line, i) => {
    if (CHAPTER_HEADER.test(line)) idx.push(i);
  });
  if (idx.length <= 1) return [{ title: null, text }];
  const sections = [];
  for (let k = 0; k < idx.length; k++) {
    const start = idx[k];
    const end = k + 1 < idx.length ? idx[k + 1] : lines.length;
    sections.push({ title: lines[start].replace(/^#+\s*/, '').trim(), text: lines.slice(start, end).join('\n') });
  }
  return sections;
}

// ---- 长度 / 章卡 / 重复细节（advisory 为主，超 0.7 硬上限才 blocking）----
const HAN_RE = /[\u4e00-\u9fff]/g;
const RATIO_TARGET = 0.5; // 目标：细纲 ≤ 正文目标的一半
const RATIO_HARD = 0.7; // 硬上限
const DEFAULT_TARGET = 3000; // 缺字数目标时的唯一兜底，与 SKILL.md「字数验收权威」一致
const CARD_MAX = 350;
const CORE_EVENT_MAX = 80;
const DUP_MIN = 3;
const DUP_SHOWN = 5;
const CARD_NOTE = '以下为写手单';
const CN_DIGITS = '零〇一二三四五六七八九十百千万两';
const QUANTIFIERS = [
  '星币', '金币', '灵石', '公里', '公斤', '分钟', '小时',
  '字', '块', '元', '米', '人', '条', '次', '个', '张', '件', '枚', '颗', '瓶', '份', '名', '位',
  '道', '招', '层', '级', '天', '年', '岁', '斤', '里', '秒', '倍', '万', '亿',
];
// 百分之… 放在最前，免得「百」+量词先把它吃掉。
const NUM_QTY_RE = new RegExp(
  `百分之[${CN_DIGITS}]+|\\d+(?:\\.\\d+)?\\s*[%％]|(?:\\d+(?:\\.\\d+)?|[${CN_DIGITS}]+)\\s*(?:${QUANTIFIERS.join('|')})`,
  'g'
);
// 副词/泛指，不是具体数量。
const NUM_QTY_STOP = new Set(['千万', '万万', '一一', '万一']);
// 「密250字」「≈40 字」这类字数预算标注不算细节。
const BUDGET_PREFIX_RE = /(?:密|疏|≈|约|预算|计|目标|·)\s*$/;
const QUOTE_RES = [/“([^”\n]{4,80})”/g, /「([^」\n]{4,80})」/g];

function hanCount(text) {
  const m = String(text).match(HAN_RE);
  return m ? m.length : 0;
}

function parseTarget(text) {
  const m = text.match(/字数目标[^0-9\n]{0,6}(\d{3,6})(?:\s*(?:字)?\s*[-~～—–至到]\s*(\d{3,6}))?/);
  if (!m) return null;
  const a = Number(m[1]);
  const b = m[2] ? Number(m[2]) : 0;
  return Math.max(a, b);
}

// 章卡 = 第一个 #### 之前的部分；只计字段值与非字段正文，不计 # 标题、字段名和分隔说明行。
function cardStats(sectionText) {
  const lines = sectionText.split('\n');
  let chars = 0;
  let coreEvent = null;
  for (const line of lines) {
    if (/^\s*####/.test(line)) break;
    if (/^\s*#/.test(line)) continue;
    if (line.includes(CARD_NOTE)) continue;
    const field = line.match(/^\s*[-*]\s*([^：:\n]{1,24})[：:](.*)$/);
    if (field) {
      const value = field[2];
      chars += hanCount(value);
      if (coreEvent === null && /^核心事件/.test(field[1].trim())) coreEvent = hanCount(value);
    } else {
      chars += hanCount(line);
    }
  }
  return { cardChars: chars, coreEventChars: coreEvent };
}

// 重复细节：字数目标 / 预算合计行与【…】预算标签不算（它们本来就是数字）。
function dupStats(sectionText) {
  const counts = new Map();
  const bump = (key) => counts.set(key, (counts.get(key) || 0) + 1);
  for (const raw of sectionText.split('\n')) {
    if (/字数目标|预算合计/.test(raw)) continue;
    const line = raw.replace(/【[^】\n]*】/g, '');
    for (const re of QUOTE_RES) {
      for (const m of line.matchAll(re)) bump(`“${m[1]}”`);
    }
    for (const m of line.matchAll(NUM_QTY_RE)) {
      const frag = m[0].replace(/\s+/g, '');
      // 「一个/一次/两人」这类泛用量词短语不算具体细节。
      if (/^[一两几][^\d]$/.test(frag) || NUM_QTY_STOP.has(frag)) continue;
      if (BUDGET_PREFIX_RE.test(line.slice(Math.max(0, m.index - 3), m.index))) continue;
      bump(frag);
    }
  }
  return [...counts.entries()]
    .filter(([, n]) => n >= DUP_MIN)
    .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map(([text, count]) => ({ text, count }));
}

function lengthMetrics(sectionText) {
  const parsed = parseTarget(sectionText);
  const target = parsed || DEFAULT_TARGET;
  const hanChars = hanCount(sectionText);
  const ratio = Math.round((hanChars / target) * 1000) / 1000;
  const { cardChars, coreEventChars } = cardStats(sectionText);
  return {
    han_chars: hanChars,
    target,
    target_defaulted: !parsed,
    ratio,
    card_chars: cardChars,
    core_event_chars: coreEventChars,
    dups: dupStats(sectionText),
  };
}

function lengthFindings(chapter, m) {
  const out = [];
  const pct = `${Math.round(m.ratio * 100)}%`;
  const basis = m.target_defaulted ? `（缺「字数目标」，按 ${DEFAULT_TARGET} 代入，请补纲）` : '';
  if (m.target_defaulted) {
    out.push({ chapter, field: '字数目标', reason: `缺字数目标，长度按 ${DEFAULT_TARGET} 字代入，请补纲`, severity: 'advisory', kind: 'length' });
  }
  if (m.ratio > RATIO_HARD) {
    out.push({ chapter, field: '细纲长度', reason: `汉字 ${m.han_chars} / 字数目标 ${m.target} = ${pct}，超过硬上限 ${RATIO_HARD * 100}%${basis}；按 1:2.5～1:3 压回 ${RATIO_TARGET * 100}% 以内：五段式/多线只写功能与转折，具体数字和台词只留在情节点序列`, severity: 'blocking', kind: 'length' });
  } else if (m.ratio > RATIO_TARGET) {
    out.push({ chapter, field: '细纲长度', reason: `汉字 ${m.han_chars} / 字数目标 ${m.target} = ${pct}，超过目标 ${RATIO_TARGET * 100}%${basis}；建议删去五段式/多线里重抄情节点的内容`, severity: 'advisory', kind: 'length' });
  }
  if (m.card_chars > CARD_MAX) {
    out.push({ chapter, field: '章卡', reason: `顶部章卡字段值 ${m.card_chars} 汉字 > ${CARD_MAX}；章卡是作者要读的部分，细节挪进情节点序列`, severity: 'advisory', kind: 'card' });
  }
  if (m.core_event_chars !== null && m.core_event_chars > CORE_EVENT_MAX) {
    out.push({ chapter, field: '核心事件', reason: `${m.core_event_chars} 汉字 > ${CORE_EVENT_MAX}；只说一件事，不装数字和台词`, severity: 'advisory', kind: 'card' });
  }
  if (m.dups.length) {
    const shown = m.dups.slice(0, DUP_SHOWN).map((d) => `${d.text}×${d.count}`).join('、');
    out.push({ chapter, field: '重复细节', reason: `同一细节出现 ≥${DUP_MIN} 次：${shown}；只在情节点序列留一处，其他字段改成「见情节点 N」——一处改六处漏`, severity: 'advisory', kind: 'dup' });
  }
  return out;
}

function checkFile(file) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (err) {
    return { file, error: `无法读取: ${err.message}`, findings: [] };
  }
  const findings = [];
  const metrics = [];
  for (const chap of splitChapters(text)) {
    for (const field of FIELDS) {
      const { present, value } = fieldValue(chap.text, field.label);
      if (!present) {
        findings.push({ chapter: chap.title, field: field.label, reason: '缺失字段', severity: 'blocking', kind: 'retention' });
      } else if (isPlaceholder(value)) {
        findings.push({ chapter: chap.title, field: field.label, reason: '仍是占位符/空', severity: 'blocking', kind: 'retention' });
      }
    }
    const m = lengthMetrics(chap.text);
    metrics.push({ chapter: chap.title, ...m });
    findings.push(...lengthFindings(chap.title, m));
  }
  const ratio = metrics.reduce((x, m) => Math.max(x, m.ratio), 0);
  const cardChars = metrics.reduce((x, m) => Math.max(x, m.card_chars), 0);
  const dups = metrics.flatMap((m) => m.dups);
  return { file, error: null, ratio, card_chars: cardChars, dups, metrics, findings };
}

function main(argv) {
  const args = argv.slice(2);
  let json = false;
  let failOn = 'blocking';
  const files = [];
  for (const a of args) {
    if (a === '--json') json = true;
    else if (a === '-h' || a === '--help') { process.stdout.write(USAGE); return 0; }
    else if (a.startsWith('--fail-on=')) failOn = a.slice('--fail-on='.length);
    else files.push(a);
  }
  if (files.length === 0) { process.stderr.write(USAGE); return 2; }

  const results = files.map(checkFile);
  const hadReadError = results.some((r) => r.error);
  const totalFindings = results.reduce((n, r) => n + r.findings.length, 0);
  const all = results.flatMap((r) => r.findings);
  const retentionCount = all.filter((f) => f.kind === 'retention').length;
  const blockingCount = all.filter((f) => f.severity === 'blocking').length;
  const advisoryCount = all.filter((f) => f.severity === 'advisory').length;

  if (json) {
    process.stdout.write(JSON.stringify({ results, totalFindings, blocking: blockingCount, advisory: advisoryCount }, null, 2) + '\n');
  } else {
    for (const r of results) {
      const name = path.basename(r.file);
      if (r.error) { process.stderr.write(`[ERROR] ${name}: ${r.error}\n`); continue; }
      if (r.findings.length === 0) { process.stdout.write(`[PASS] ${name}: 三个留存字段齐备；长度 ${Math.round(r.ratio * 100)}%，章卡 ${r.card_chars} 字\n`); continue; }
      for (const f of r.findings) {
        const where = f.chapter ? `（${f.chapter}）` : '';
        const tag = f.kind === 'retention' ? '缺' : f.severity === 'blocking' ? '超·阻断' : '提醒';
        process.stdout.write(`[${tag}] ${name}${where}: ${f.field} —— ${f.reason}\n`);
      }
    }
    if (retentionCount > 0) {
      process.stdout.write(`\n共 ${retentionCount} 处留存字段缺失/未填。补齐"收一个（本章兑现）/变一个（状态变化）/开一个（章尾余势）"后再落正文。\n`);
    }
    if (blockingCount - retentionCount > 0) {
      process.stdout.write(`\n${blockingCount - retentionCount} 章细纲超过字数目标×${RATIO_HARD} 硬上限，压缩后再交付本批。\n`);
    }
  }

  if (hadReadError) return 2;
  if (failOn === 'all') return totalFindings > 0 ? 1 : 0;
  return blockingCount > 0 ? 1 : 0;
}

// 设 exitCode 而不是立即 process.exit()：macOS 上 stdout 写管道是异步的，大 --json 报告会被截断。
process.exitCode = main(process.argv);
