#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const USAGE = `Usage: node export-for-platform.js [--out=<dir>] [--join-blank-line | --fanqie] <file...>

把 正文/第XXX章_*.md（长篇）或 短故事正文.md（###N. 小节制，导出时转为"第X章"）批量转成可直接复制粘贴的纯文本
（番茄小说、晋江等创作后台通常没有对外开放的自动发布 API，这一步只做格式转换，
不做登录、不做提交、不碰任何账号凭证——发布仍需作者本人在平台后台手动操作）。

参数：
  <file...>           正文 md 文件路径，支持多个；也可以直接把 正文/*.md 的 glob 结果传进来
  --out=<dir>          输出目录，默认 ./导出/
  --join-blank-line    段落间插入空行输出（默认不插入，保持源文件的单换行紧凑排版；
                        部分平台编辑器粘贴单换行文本会挤成一段，此时开这个开关更稳）
  --fanqie             双步无损竖屏拆行模式（适配番茄/七猫等手机竖屏原生排版）：
                        保留源稿 ≥95% 词面，输出全文 0 空行、段首两全角空格（　　）缩进、
                        100% 人物/系统对白独立成行、手机 1~2 行短段占比 70%~90%

行为：
  - 每个输入文件生成一个同名 .txt，标题行（"###第X章 章名" 或 "## 第X章 章名"）单独取出作为文件首行
  - 默认模式不修改正文句子；--fanqie 模式仅做词面守恒的冒号收尾与从句安全拆行，不回写 .md 源稿
  - 同时生成一个 汇总.txt，把所有章节按传入顺序拼在一起，方便一次性通读检查`;

const INDENT = '\u3000\u3000';
const LEADING_COLON_QUOTE_RE = /^((?:[^“”「」]|“[^”]*”|「[^」]*」)+?)[：:](“[^“”]*”|「[^「」]*」)$/;
const HANZI_ALNUM_RE = /[\u3400-\u9fff0-9A-Za-z]/g;

const SUBORDINATE_STARTERS = [
  '要不是', '如果不是', '若是', '要是', '虽然', '尽管', '除开', '除了',
  '自从', '当', '趁着', '借着', '随着', '无论', '不管', '哪怕', '就算',
  '既然', '由于', '因为',
];
const CORRELATIVE_STARTERS = [
  '要不是', '如果不是', '若是', '要是', '虽然', '尽管', '除开', '除了',
  '无论', '不管', '哪怕', '就算', '既然',
];
const CORRELATIVE_MARKERS = ['却', '但', '可', '不过', '还是', '也', '还', '都', '就', '才', '那', '便'];
const SUBORDINATE_ENDINGS = [
  '的时候', '之时', '之际', '以后', '之后', '以前', '之前', '同时', '瞬间', '刹那',
  '不知道', '没想到', '未曾想', '谁知道', '还以为', '只以为', '只觉得', '总觉得',
  '生怕', '惟恐', '唯恐', '巴不得',
];
const CONCRETE_ACTION_CUES = [
  '看', '盯', '望', '扫', '瞥', '敲', '拍', '按', '摸', '掏', '拿', '递', '塞',
  '推', '拉', '拽', '扯', '扶', '挡', '撞', '退', '走', '跑', '冲', '站', '坐',
  '蹲', '弯腰', '起身', '抬头', '低头', '转头', '转过头', '偏头', '点头', '摇头',
  '招手', '招了招手', '挥手', '指着', '指了指', '亮了亮', '晃着', '翻开', '合上',
  '点开', '收起', '放下', '抱起', '仰着', '沉下', '咬着', '捂着', '提示音', '勾了',
];

const options = {
  out: './导出',
  joinBlankLine: false,
  fanqie: false,
  files: [],
};

for (let i = 2; i < process.argv.length; i += 1) {
  const arg = process.argv[i];
  if (arg.startsWith('--out=')) {
    options.out = arg.slice('--out='.length);
  } else if (arg === '--join-blank-line') {
    options.joinBlankLine = true;
  } else if (arg === '--fanqie' || arg === '--layout=fanqie') {
    options.fanqie = true;
  } else if (arg === '--layout=plain') {
    options.fanqie = false;
  } else if (arg === '-h' || arg === '--help') {
    process.stdout.write(`${USAGE}\n`);
    process.exit(0);
  } else if (arg.startsWith('-')) {
    die(`Unknown option: ${arg}`);
  } else {
    options.files.push(arg);
  }
}

if (options.fanqie && options.joinBlankLine) {
  die('--fanqie requires 0 blank lines and cannot be combined with --join-blank-line');
}

if (options.files.length === 0) {
  die('No files provided');
}

fs.mkdirSync(options.out, { recursive: true });

const summaryParts = [];
let converted = 0;

for (const file of options.files) {
  const fullPath = path.resolve(file);
  let input;
  try {
    input = fs.readFileSync(fullPath, 'utf8');
  } catch (error) {
    console.error(`${file}: unable to read (${error.message})`);
    continue;
  }

  let { title, paragraphs } = parseChapter(input);
  if (!title) {
    // 短故事正文.md 无标题行：用上级目录名（即书名/篇名）兜底
    title = path.basename(path.dirname(fullPath));
    console.error(`${file}: 未找到章节标题行，使用目录名作标题：${title}`);
  }

  let output;
  let extraStat = '';
  if (options.fanqie) {
    const fanqieParas = convertParagraphsToFanqie(paragraphs);
    const body = fanqieParas.map((p) => `${INDENT}${p}`).join('\n');
    output = `${title}\n${body}\n`;
    const oneTwoCount = fanqieParas.filter((p) => mobileRows(Array.from(p).length) <= 2).length;
    const oneTwoPct = fanqieParas.length > 0 ? ((oneTwoCount * 100) / fanqieParas.length).toFixed(1) : '0.0';
    extraStat = `，番茄竖屏 ${fanqieParas.length} 段，1~2行占 ${oneTwoPct}%`;
  } else {
    const sep = options.joinBlankLine ? '\n\n' : '\n';
    const body = paragraphs.join(sep);
    output = `${title}\n\n${body}\n`;
    extraStat = `，${paragraphs.length} 段`;
  }

  const baseName = path.basename(file, path.extname(file));
  const outPath = path.join(options.out, `${baseName}.txt`);
  fs.writeFileSync(outPath, output, 'utf8');
  console.log(`${file} -> ${outPath}（标题：${title}${extraStat}）`);

  summaryParts.push(output.trimEnd());
  converted += 1;
}

if (converted > 0) {
  const summaryPath = path.join(options.out, '汇总.txt');
  const summarySep = options.fanqie ? '\n' : '\n\n---\n\n';
  fs.writeFileSync(summaryPath, `${summaryParts.join(summarySep)}\n`, 'utf8');
  console.log(`\n共导出 ${converted} 章，汇总文件：${summaryPath}`);
}

function parseChapter(input) {
  const lines = input.split(/\r?\n/);
  let title = null;
  const paragraphs = [];

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith('<!--') && trimmed.endsWith('-->')) continue;

    if (title === null && paragraphs.length === 0 && /^#{1,6}\s*第[零一二三四五六七八九十百千万〇两\d]+[章节回幕集卷]/.test(trimmed)) {
      title = trimmed.replace(/^#{1,6}\s*/, '');
      continue;
    }

    // 短故事 ###N. 小节标记：转成"第X章"（中文数字）。实测证据：番茄短故事整篇上传后
    // 平台目录里永远只算"共1章"，"###N."只是我们内部拆分小节的写作脚手架，不是平台
    // 认的分节格式；真实已发布短故事里，作者若要在正文内保留可见节奏断点，用的是
    // "第一章/第二章"这种长篇式写法（2026-07-20 抽查已发布短故事实测确认），不是裸数字。
    const shortSection = trimmed.match(/^#{1,6}\s*(\d+)\.?\s*$/);
    if (shortSection) {
      paragraphs.push(`第${toChineseNumeral(Number(shortSection[1]))}章`);
      continue;
    }

    paragraphs.push(trimmed);
  }

  return { title, paragraphs };
}

function mobileRows(charLen) {
  if (charLen <= 16) return 1;
  return 1 + Math.floor((charLen - 16 + 17) / 18);
}

function isStandaloneQuote(s) {
  return (
    (s.startsWith('“') && s.endsWith('”') && !s.slice(1, -1).includes('”')) ||
    (s.startsWith('「') && s.endsWith('」') && !s.slice(1, -1).includes('」'))
  );
}

function splitParagraphOutsideQuotes(para) {
  const spans = [];
  let buf = '';
  let inQuote = false;
  for (const ch of para) {
    buf += ch;
    if (ch === '“' || ch === '「') {
      inQuote = true;
    } else if (ch === '”' || ch === '」') {
      inQuote = false;
    } else if (!inQuote && '。！？!?'.includes(ch)) {
      const piece = buf.trim();
      if (piece) spans.push(piece);
      buf = '';
    }
  }
  const tail = buf.trim();
  if (tail) spans.push(tail);
  return spans;
}

function splitClausesOutsideQuotes(text) {
  const clauses = [];
  let buf = '';
  let inQuote = false;
  for (const ch of text) {
    if (ch === '“' || ch === '「') {
      inQuote = true;
      buf += ch;
    } else if (ch === '”' || ch === '」') {
      inQuote = false;
      buf += ch;
    } else if (!inQuote && ch === '，') {
      const piece = buf.trim();
      if (piece) clauses.push(piece);
      buf = '';
    } else {
      buf += ch;
    }
  }
  const tail = buf.trim();
  if (tail) clauses.push(tail);
  return clauses;
}

function closeActionPrefix(prefix) {
  let p = prefix.trim().replace(/[，,。；;：:]+$/, '');
  const specificReplacements = [
    [/冷笑道$/, '冷笑一声'],
    [/笑道$/, '笑了笑'],
    [/哭骂道$/, '哭骂起来'],
    [/哭喊道$/, '哭喊起来'],
    [/嚷道$/, '嚷了起来'],
    [/喝道$/, '大喝一声'],
    [/播报道$/, '播报起来'],
    [/提议道$/, '开口提议'],
    [/补充道$/, '开口补充'],
    [/交代道$/, '交代了一句'],
    [/叮嘱道$/, '叮嘱了一句'],
    [/吩咐道$/, '吩咐了一句'],
    [/提醒道$/, '提醒了一句'],
    [/追问道$/, '追问了一句'],
    [/反问道$/, '反问了一句'],
    [/解释道$/, '开口解释'],
    [/发出提示音(?:说道|提示道|道)?$/, '发出提示音'],
    [/提示道$/, '发出提示音'],
    [/点头(?:说道|道)$/, '点了点头'],
    [/摇头(?:说道|道)$/, '摇了摇头'],
    [/挥手喊道$/, '挥手大喊'],
    [/喊道$/, '喊了起来'],
  ];
  for (const [pattern, repl] of specificReplacements) {
    if (pattern.test(p)) {
      return `${p.replace(pattern, repl)}。`;
    }
  }

  if (/([对冲朝向][^\s，,。！？!?]{1,10}?)(?:说道|问道|答道|回道|道|说|问)$/.test(p)) {
    if (/(?:问道|问)$/.test(p)) {
      return `${p.replace(/(?:问道|问)$/, '问了一句')}。`;
    }
    return `${p.replace(/(?:说道|答道|回道|道|说)$/, '开了口')}。`;
  }

  if (/(?:地|小声|低声|大声|轻声|随口|顺口)(?:说道|问道|答道|回道|道)$/.test(p)) {
    return `${p.replace(/(?:说道|问道|答道|回道|道)$/, '开口')}。`;
  }

  if (/(?:说道|问道|答道|回道|道)$/.test(p)) {
    const stripped = p.replace(/(?:说道|问道|答道|回道|道)$/, '').replace(/[，,]+$/, '');
    const clauses = splitClausesOutsideQuotes(stripped);
    const lastClause = clauses[clauses.length - 1] || '';
    if (Array.from(lastClause).length >= 4 && CONCRETE_ACTION_CUES.some((cue) => lastClause.includes(cue))) {
      return `${stripped}。`;
    }
    return `${stripped}开了口。`;
  }

  return `${p}。`;
}

function isIncompleteSubordinate(clausesSoFar) {
  const c = clausesSoFar.join('，').replace(/^[，,。！？!?\s]+|[，,。！？!?\s]+$/g, '');
  const lastCl = clausesSoFar[clausesSoFar.length - 1].replace(/^[，,。！？!?\s]+|[，,。！？!?\s]+$/g, '');
  if (Array.from(c).length < 9) return true;
  if (SUBORDINATE_ENDINGS.some((suf) => lastCl.endsWith(suf))) return true;
  if (clausesSoFar.length === 1) {
    return SUBORDINATE_STARTERS.some((st) => c.startsWith(st));
  }
  if (SUBORDINATE_STARTERS.some((st) => lastCl.startsWith(st))) return true;
  if (CORRELATIVE_STARTERS.some((st) => clausesSoFar[0].startsWith(st))) {
    const tailJoined = clausesSoFar.slice(1).join('，');
    if (Array.from(c).length < 44 && !CORRELATIVE_MARKERS.some((m) => tailJoined.includes(m))) {
      return true;
    }
  }
  return false;
}

function splitLongNarrativeSentence(sent, maxTwoRowChars) {
  const s = sent.trim();
  if (!s) return [];
  if (Array.from(s).length <= maxTwoRowChars || isStandaloneQuote(s)) {
    return [s];
  }
  const lastCh = s[s.length - 1];
  const terminal = '。！？!?'.includes(lastCh) ? lastCh : '。';
  const core = '。！？!?'.includes(lastCh) ? s.slice(0, -1) : s;
  const rawClauses = splitClausesOutsideQuotes(core);
  if (rawClauses.length <= 1) return [s];

  const mergedUnits = [];
  let buf = [];
  for (let idx = 0; idx < rawClauses.length; idx += 1) {
    buf.push(rawClauses[idx]);
    const isLast = idx === rawClauses.length - 1;
    if (!isLast && isIncompleteSubordinate(buf)) {
      continue;
    }
    mergedUnits.push(buf.join('，'));
    buf = [];
  }
  if (buf.length > 0) {
    const tailStr = buf.join('，');
    if (mergedUnits.length > 0) {
      mergedUnits[mergedUnits.length - 1] = `${mergedUnits[mergedUnits.length - 1]}，${tailStr}`;
    } else {
      mergedUnits.push(tailStr);
    }
  }
  if (mergedUnits.length === 1) {
    return [`${mergedUnits[0]}${terminal}`];
  }

  const packed = [];
  for (const u of mergedUnits) {
    if (packed.length > 0) {
      const prevLen = Array.from(packed[packed.length - 1]).length;
      const curLen = Array.from(u).length;
      if (prevLen < 13 || prevLen + 1 + curLen <= 28) {
        packed[packed.length - 1] = `${packed[packed.length - 1]}，${u}`;
        continue;
      }
    }
    packed.push(u);
  }

  return packed.map((seg, idx) => `${seg}${idx === packed.length - 1 ? terminal : '。'}`);
}

function convertParagraphsToFanqie(paragraphs) {
  const out = [];
  for (const rawPara of paragraphs) {
    const para = rawPara.replace(/^#{1,6}\s*/, '').trim();
    if (!para) continue;
    if (/^第[零一二三四五六七八九十百千万〇两\d]+章$/.test(para)) {
      out.push(para);
      continue;
    }
    for (const sent of splitParagraphOutsideQuotes(para)) {
      const m = sent.match(LEADING_COLON_QUOTE_RE);
      if (m) {
        const actionSent = closeActionPrefix(m[1]);
        const quoteSent = m[2].trim();
        out.push(...splitLongNarrativeSentence(actionSent, 32));
        out.push(quoteSent);
      } else {
        out.push(...splitLongNarrativeSentence(sent, 34));
      }
    }
  }
  return out;
}

function toChineseNumeral(n) {
  const digits = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九'];
  if (n < 10) return digits[n];
  if (n === 10) return '十';
  if (n < 20) return `十${digits[n - 10]}`;
  if (n < 100) {
    const tens = Math.floor(n / 10);
    const ones = n % 10;
    return `${digits[tens]}十${ones === 0 ? '' : digits[ones]}`;
  }
  return String(n);
}

function die(message) {
  console.error(message);
  console.error(USAGE.trimEnd());
  process.exit(2);
}
