#!/usr/bin/env node

/**
 * Detect long-horizon drift toward repetitive Chinese dialogue attribution.
 *
 * This gate does not ban ordinary attribution verbs and does not rewrite prose.
 * It compares the current chapter with early accepted chapters and recent
 * chapters, then rejects only clear density, repetition, or consecutive-tag
 * degeneration.
 *
 * The chapter number normally comes from the file name (第NNN章.md). Candidate
 * drafts live at 追踪/候选章/第NNN章/<run-id>/candidate.md, whose name carries no
 * number, so callers pass --chapter N; with --chapter and no --history-dir or
 * --project, a file under <root>/追踪/候选章/ reads history from <root>/正文/.
 *
 * The consecutive-tag rule has a per-book limit. A book whose style keeps every
 * dialogue paragraph in the "主语 + 动作 + 说道：“……”" shape legitimately runs
 * several tagged turns in a row, so the limit (largest run still allowed) comes
 * from, in order: --max-consecutive-tagged-turns, then
 * <root>/设定/门禁配置.json → dialogue.max_consecutive_tagged_turns when the
 * project root is known (--project, or a candidate path), then the default 3
 * (a run of 4 blocks, the historical behaviour).
 *
 * Counting lines cannot tell "沈烬点了点头，拿起合同说道：“……”" (an action-led
 * G5 line) from "沈烬说道：“……”" (a bare announcement), so every high-confidence
 * attribution also gets a `bare` flag, decided as follows:
 *   1. The tag clause is the text between the last separator before the verb
 *      (，。！？；：、…—, a closing quote or whitespace) and the verb.
 *   2. The line's narration (the line with every quoted span cut out) splits
 *      into clauses at the same separators. When it has more clauses than the
 *      line has tag clauses, the line carries an independent action clause
 *      ("沈烬拉开椅子坐下，小声问道：", "他抬手点了一下，开口说道：",
 *      "沈烬说道：“……”说完把合同推了回去。") and no tag on it is bare.
 *   3. Otherwise strip the subject from the tag clause; the tag is bare when at
 *      most 4 Han characters of modifier remain, a trailing adverbial 地 not
 *      counted ("沈烬说道", "苏幼薇低声说道", "她又问道", "宋棠板着脸说道",
 *      "星瑕毫不犹豫地回答道"). A clause of at most 4 Han characters is bare
 *      whatever its subject; a leading pronoun (他/她/他们/大家/众人…) is the
 *      subject; otherwise the subject is the leading name found by frequency:
 *      start from its first two characters when they occur at least 3 times in
 *      the chapter, and add one more character while the longer prefix still
 *      occurs at least 70% as often (沈烬 / 苏幼薇 / 宋棠). A subject that cannot
 *      be found ("前排的宋棠低声说道", a one-off speaker) leaves the tag not bare:
 *      the rule errs toward missing an announcement, never toward flagging an
 *      action-led line.
 * A dialogue line is a bare-tagged turn when it has attributions and all of
 * them are bare. consecutive-bare-tagged-turns blocks a run longer than the
 * limit from --max-consecutive-bare-tagged-turns, then
 * 设定/门禁配置.json → dialogue.max_consecutive_bare_tagged_turns, then the
 * default 2 (a run of 3 blocks); it sits beside consecutive-tagged-turns, which
 * is unchanged. bare-tag-ratio is advisory: at least 5 bare tags making up at
 * least half of all attributions.
 * Calibration (2026-10-18, 男生也能报考魔女学院吗 第 1–4 章, all 52 high-confidence
 * attributions, every one a G5 前置冒号 line): 0 flagged bare, 0 false
 * positives. The nearest misses are single-clause leads that stay above the
 * 4-character modifier budget — "苏幼薇站在门外板着脸说道" (7), "沈烬顺着她的话说道"
 * (5), "苏幼薇几乎想都没想就直接回答道" (9) — and "他抬手…点了一下，开口说道" /
 * "沈烬瞄着…纹路，问道", whose short tag clause follows an action clause.
 */

const fs = require('fs');
const path = require('path');

const USAGE = [
  'Usage: node dialogue_drift_gate.js --current <file> [--chapter N]',
  '         [--project <root> | --history-dir <dir>] [--baseline-count N] [--recent-window N]',
  '         [--max-consecutive-tagged-turns N] [--max-consecutive-bare-tagged-turns N] [--json]',
  '  --current <file>      chapter or candidate to check (a bare positional path also works)',
  '  --chapter N           chapter number of <file> (positive integer); overrides 第NNN章 in the file name.',
  '                        Required for candidates such as 追踪/候选章/第NNN章/<run-id>/candidate.md',
  '  --project <root>      read history chapters from <root>/正文/',
  '  --history-dir <dir>   read history chapters from <dir> (wins over --project).',
  '                        Default: the file\'s own directory, or <root>/正文/ when --chapter is given',
  '                        and the file sits under <root>/追踪/候选章/',
  '  --baseline-count N    earliest history chapters forming the style baseline (default 3)',
  '  --recent-window N     latest history chapters forming the recent window (default 3)',
  '  --max-consecutive-tagged-turns N',
  '                        largest run of consecutive tagged dialogue lines still allowed; a longer run',
  '                        blocks. Default: dialogue.max_consecutive_tagged_turns in <root>/设定/门禁配置.json',
  '                        when the project root is known, else 3 (so a run of 4 blocks)',
  '  --max-consecutive-bare-tagged-turns N',
  '                        largest run of consecutive bare-announcement lines (主语 + at most 4 modifier',
  '                        characters + 说道, no action clause) still allowed; a longer run blocks.',
  '                        Default: dialogue.max_consecutive_bare_tagged_turns in <root>/设定/门禁配置.json',
  '                        when the project root is known, else 2 (so a run of 3 blocks)',
  '  --json                print the report as JSON',
  'Exit codes: 0 passed (advisories allowed), 2 rejected, 3 usage or input error.',
].join('\n');

const VERBS = [
  '低声说道', '沉声说道', '冷声说道', '缓缓说道', '淡淡说道',
  '开口说道', '开口说', '说道', '问道', '反问道', '回答道', '答道',
  '喊道', '叫道', '嘀咕道', '回答', '反问', '嘀咕', '说', '问', '答', '喊', '叫',
];
const ATTRIBUTION_RE = new RegExp(`(${VERBS.join('|')})(?=[，。！？：；])`, 'gu');
const NON_SPEECH_PREFIX_RE = /(?:没有|没|未|未曾|不曾|不想|不愿|不肯|拒绝|避免|懒得|无意|只想|想|想要|打算|准备|正要|刚要|试图|来不及)\s*$/u;
const OPEN_QUOTE_RE = /^[，,:：\s]*[“「『"]/u;
const POST_QUOTE_TAG_RE = /^[，,\s]*[\p{Script=Han}]{1,12}$/u;

function isHighConfidenceAttribution(line, verbIndex, verb) {
  const before = line.slice(0, verbIndex);
  if (NON_SPEECH_PREFIX_RE.test(before.slice(-12))) return false;
  const afterVerb = line.slice(verbIndex + verb.length);
  if (OPEN_QUOTE_RE.test(afterVerb)) return true;
  const closeQuote = Math.max(before.lastIndexOf('”'), before.lastIndexOf('」'), before.lastIndexOf('』'), before.lastIndexOf('"'));
  if (closeQuote < 0) return false;
  return POST_QUOTE_TAG_RE.test(before.slice(closeQuote + 1));
}

const TRAILING_MODIFIER_RE = /(?:又|便|才|却|也|立刻|马上|忽然|低声|沉声|冷声|缓缓|淡淡地?)$/u;
const SUBJECT_RE = /(?:^|[“”「」。！？；，\s])([\p{Script=Han}]{1,6})$/u;

function extractSubject(line, verbIndex) {
  let before = line.slice(0, verbIndex);
  before = before.replace(TRAILING_MODIFIER_RE, '');
  const match = before.match(SUBJECT_RE);
  if (!match) return null;
  const subject = match[1];
  if (subject.length === 1 && !['我', '你', '他', '她', '它'].includes(subject)) return null;
  return subject;
}

// Bare-announcement detection; the rule is spelled out in the header comment.
const CLAUSE_SEPARATOR_RE = /[，,。！？!?；;：:、…—\s”」』]/u;
const NARRATION_SPLIT_RE = /[，,。！？!?；;：:、…—\s]+/u;
const QUOTED_SPAN_RE = /“[^”]*(?:”|$)|「[^」]*(?:」|$)|『[^』]*(?:』|$)/gu;
const HAN_RE = /\p{Script=Han}/u;
const PRONOUN_SUBJECT_RE = /^(?:他们|她们|我们|你们|它们|大家|众人|他|她|它|我|你)/u;
const BARE_MAX_MODIFIER = 4;
const NAME_MIN_COUNT = 3;
const NAME_EXTEND_RATIO = 0.7;

function hanOnly(text) {
  return [...text].filter((char) => HAN_RE.test(char)).join('');
}

function tagClause(line, verbIndex) {
  const before = line.slice(0, verbIndex);
  let start = before.length;
  while (start > 0 && !CLAUSE_SEPARATOR_RE.test(before[start - 1])) start -= 1;
  return hanOnly(before.slice(start));
}

function narrationClauseCount(line) {
  return line.replace(QUOTED_SPAN_RE, '｜').split(/[｜]/u)
    .flatMap((part) => part.split(NARRATION_SPLIT_RE))
    .filter((part) => HAN_RE.test(part)).length;
}

function occurrencesOf(text, needle) {
  return text.split(needle).length - 1;
}

// A trailing adverbial 地 is a particle, not part of the modifier word.
function modifierLength(modifier) {
  return modifier.length - (modifier.endsWith('地') ? 1 : 0);
}

// Han characters of modifier left after the subject, or null when no subject is found.
function bareModifierLength(clause, text) {
  if (modifierLength(clause) <= BARE_MAX_MODIFIER) return modifierLength(clause);
  const pronoun = clause.match(PRONOUN_SUBJECT_RE);
  if (pronoun) return modifierLength(clause.slice(pronoun[0].length));
  let size = 2;
  let count = occurrencesOf(text, clause.slice(0, size));
  if (count < NAME_MIN_COUNT) return null;
  while (size < Math.min(6, clause.length)) {
    const longer = occurrencesOf(text, clause.slice(0, size + 1));
    if (longer < count * NAME_EXTEND_RATIO) break;
    size += 1;
    count = longer;
  }
  return modifierLength(clause.slice(size));
}

function parsePositiveInteger(raw, what, noun = '') {
  const value = /^\d+$/.test(raw || '') ? Number(raw) : NaN;
  if (!Number.isSafeInteger(value) || value < 1) {
    const shown = raw === undefined ? 'nothing' : JSON.stringify(raw);
    throw new Error(`${what} needs a positive integer${noun}, got ${shown}`);
  }
  return value;
}

function parseChapterOption(raw) {
  return parsePositiveInteger(raw, '--chapter', ' chapter number');
}

const DEFAULT_MAX_CONSECUTIVE_TAGGED_TURNS = 3;
const DEFAULT_MAX_CONSECUTIVE_BARE_TAGGED_TURNS = 2;
const GATE_CONFIG_RELATIVE = path.join('设定', '门禁配置.json');

// Per-book limit from <root>/设定/门禁配置.json → dialogue.<key>. chapter_candidate.py
// validates the whole file strictly and passes the value on the command line;
// this direct read keeps a bare `--current … --project <root>` run consistent.
function configuredDialogueLimit(projectRoot, key) {
  if (!projectRoot) return null;
  const configPath = path.join(projectRoot, GATE_CONFIG_RELATIVE);
  if (!fs.existsSync(configPath)) return null;
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  } catch (error) {
    throw new Error(`${GATE_CONFIG_RELATIVE} is not valid JSON: ${error.message}`);
  }
  const dialogue = raw && typeof raw === 'object' ? raw.dialogue : undefined;
  if (dialogue === undefined) return null;
  if (!dialogue || typeof dialogue !== 'object' || Array.isArray(dialogue)) {
    throw new Error(`${GATE_CONFIG_RELATIVE}.dialogue must be an object`);
  }
  const value = dialogue[key];
  if (value === undefined) return null;
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new Error(`${GATE_CONFIG_RELATIVE}.dialogue.${key} must be a positive integer, got ${JSON.stringify(value)}`);
  }
  return value;
}

function resolveDialogueLimit(cliValue, projectRoot, key, fallback) {
  if (cliValue !== undefined) return { limit: cliValue, source: 'cli' };
  const configured = configuredDialogueLimit(projectRoot, key);
  if (configured !== null) return { limit: configured, source: GATE_CONFIG_RELATIVE };
  return { limit: fallback, source: 'default' };
}

function parseArgs(argv) {
  const options = { json: false, help: false, baselineCount: 3, recentWindow: 3 };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--json') options.json = true;
    else if (arg === '--help' || arg === '-h') options.help = true;
    else if (arg === '--current') options.current = argv[++index];
    else if (arg === '--chapter') options.chapter = parseChapterOption(argv[++index]);
    else if (arg === '--project') {
      options.project = argv[++index];
      if (!options.project) throw new Error('--project needs a project root directory');
    }
    else if (arg === '--history-dir') options.historyDir = argv[++index];
    else if (arg === '--baseline-count') options.baselineCount = Number(argv[++index]);
    else if (arg === '--recent-window') options.recentWindow = Number(argv[++index]);
    else if (arg === '--max-consecutive-tagged-turns') {
      options.maxConsecutiveTaggedTurns = parsePositiveInteger(argv[++index], '--max-consecutive-tagged-turns');
    }
    else if (arg === '--max-consecutive-bare-tagged-turns') {
      options.maxConsecutiveBareTaggedTurns = parsePositiveInteger(argv[++index], '--max-consecutive-bare-tagged-turns');
    }
    else if (!arg.startsWith('--') && !options.current) options.current = arg;
    else throw new Error(`unknown argument: ${arg}`);
  }
  if (options.help) return options;
  if (!options.current) throw new Error('missing --current <chapter-file>');
  return options;
}

function chapterNumber(filePath) {
  const match = path.basename(filePath).match(/第0*(\d+)章/);
  return match ? Number(match[1]) : null;
}

// <root>/追踪/候选章/第NNN章/<run-id>/candidate.md -> <root>
function candidateProjectRoot(filePath) {
  let dir = path.dirname(filePath);
  for (;;) {
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    if (path.basename(dir) === '候选章' && path.basename(parent) === '追踪') return path.dirname(parent);
    dir = parent;
  }
}

// `optional` history dirs (derived from a project root) may not exist yet before
// the first accepted chapter; an explicit or default directory must exist.
// The project root, when the caller gave one (--project) or the candidate path
// reveals it; null otherwise.
function resolveProjectRoot(options, currentPath) {
  if (options.project) {
    const root = path.resolve(options.project);
    if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) {
      throw new Error(`--project is not a directory: ${root}`);
    }
    return root;
  }
  if (options.chapter !== undefined) return candidateProjectRoot(currentPath);
  return null;
}

function resolveHistoryDir(options, currentPath, projectRoot) {
  if (options.historyDir) return { dir: path.resolve(options.historyDir), optional: false };
  if (projectRoot) return { dir: path.join(projectRoot, '正文'), optional: true };
  return { dir: path.dirname(currentPath), optional: false };
}

function familyOf(verb) {
  if (verb.includes('问')) return '问';
  if (verb.includes('说') || verb === '嘀咕' || verb === '嘀咕道') return '说';
  if (verb.includes('答')) return '答';
  if (verb.includes('喊') || verb.includes('叫')) return '喊叫';
  return verb;
}

function countMatches(text, regex) {
  return [...text.matchAll(regex)].length;
}

function analyze(text) {
  const lines = text.split(/\r?\n/);
  const occurrences = [];
  const verbs = new Map();
  const families = new Map();
  let dialogueTurns = 0;
  let consecutive = 0;
  let maxConsecutive = 0;
  let bareCount = 0;
  let consecutiveBare = 0;
  let maxConsecutiveBare = 0;
  let bareRunStart = 0;
  let maxBareRun = null;

  lines.forEach((line, lineIndex) => {
    const turnsOnLine = countMatches(line, /[“「]/g);
    dialogueTurns += turnsOnLine;
    ATTRIBUTION_RE.lastIndex = 0;
    const lineOccurrences = [];
    for (const match of line.matchAll(ATTRIBUTION_RE)) {
      const verb = match[1];
      if (!isHighConfidenceAttribution(line, match.index, verb)) continue;
      const subject = extractSubject(line, match.index);
      const family = familyOf(verb);
      const item = {
        line: lineIndex + 1,
        column: match.index + 1,
        text: subject ? `${subject}${verb}` : verb,
        subject,
        verb,
        family,
        tag_clause: tagClause(line, match.index),
        bare: false,
        context: line.trim(),
      };
      occurrences.push(item);
      lineOccurrences.push(item);
      verbs.set(verb, (verbs.get(verb) || 0) + 1);
      families.set(family, (families.get(family) || 0) + 1);
    }
    if (lineOccurrences.length > 0 && narrationClauseCount(line) <= lineOccurrences.length) {
      lineOccurrences.forEach((item) => {
        const modifier = bareModifierLength(item.tag_clause, text);
        item.bare = modifier !== null && modifier <= BARE_MAX_MODIFIER;
      });
    }
    bareCount += lineOccurrences.filter((item) => item.bare).length;

    if (turnsOnLine > 0 && lineOccurrences.length > 0) {
      consecutive += 1;
      maxConsecutive = Math.max(maxConsecutive, consecutive);
    } else if (line.trim()) {
      consecutive = 0;
    }
    if (turnsOnLine > 0 && lineOccurrences.length > 0 && lineOccurrences.every((item) => item.bare)) {
      if (consecutiveBare === 0) bareRunStart = lineIndex + 1;
      consecutiveBare += 1;
      if (consecutiveBare > maxConsecutiveBare) {
        maxConsecutiveBare = consecutiveBare;
        maxBareRun = { start: bareRunStart, end: lineIndex + 1 };
      }
    } else if (line.trim()) {
      consecutiveBare = 0;
    }
  });

  const attributionCount = occurrences.length;
  const density = dialogueTurns > 0 ? attributionCount / dialogueTurns : 0;
  const topVerb = [...verbs.entries()].sort((left, right) => right[1] - left[1])[0] || [null, 0];
  const topFamily = [...families.entries()].sort((left, right) => right[1] - left[1])[0] || [null, 0];

  return {
    dialogueTurns,
    attributionCount,
    density,
    maxConsecutive,
    bareCount,
    bareRatio: attributionCount ? bareCount / attributionCount : 0,
    maxConsecutiveBare,
    maxBareRun,
    topVerb: { value: topVerb[0], count: topVerb[1], ratio: attributionCount ? topVerb[1] / attributionCount : 0 },
    topFamily: { value: topFamily[0], count: topFamily[1], ratio: attributionCount ? topFamily[1] / attributionCount : 0 },
    occurrences,
  };
}

function mean(items, selector) {
  if (!items.length) return null;
  return items.reduce((sum, item) => sum + selector(item), 0) / items.length;
}

function loadHistory(currentPath, currentNumber, history) {
  if (currentNumber === null) return [];
  if (history.optional && !fs.existsSync(history.dir)) return [];
  const historyDir = history.dir;
  return fs.readdirSync(historyDir)
    .filter((name) => name.endsWith('.md'))
    .map((name) => path.join(historyDir, name))
    .filter((filePath) => filePath !== currentPath)
    .map((filePath) => ({ filePath, number: chapterNumber(filePath) }))
    .filter((item) => item.number !== null && item.number < currentNumber)
    .sort((left, right) => left.number - right.number)
    .map((item) => ({
      file: item.filePath,
      number: item.number,
      metrics: analyze(fs.readFileSync(item.filePath, 'utf8')),
    }));
}

function evaluate(current, baseline, recent, limits) {
  const findings = [];
  const advisories = [];
  const block = (code, message) => findings.push({ severity: 'blocking', code, message });
  const advise = (code, message) => advisories.push({ severity: 'advisory', code, message });

  if (current.dialogueTurns >= 6 && current.attributionCount >= 6 && current.density >= 0.72) {
    advise('attribution-density', `对白归属标记密度为 ${current.density.toFixed(2)}，需要结合场景清晰度复核`);
  }
  if (current.attributionCount >= 5 && current.topFamily.ratio >= 0.60) {
    advise('verb-family-repeat', `“${current.topFamily.value}”类标记占全部归属标记的 ${(current.topFamily.ratio * 100).toFixed(0)}%`);
  }
  if (current.attributionCount >= 4 && current.topVerb.count >= 4 && current.topVerb.ratio >= 0.50) {
    advise('same-verb-repeat', `“${current.topVerb.value}”重复 ${current.topVerb.count} 次`);
  }
  if (current.maxConsecutive > limits.maxConsecutiveTaggedTurns) {
    block('consecutive-tagged-turns', `连续 ${current.maxConsecutive} 个对白行使用显式归属标记（本书上限 ${limits.maxConsecutiveTaggedTurns}），需退回检查是否逐句报幕`);
  }
  if (current.maxConsecutiveBare > limits.maxConsecutiveBareTaggedTurns) {
    const run = current.maxBareRun;
    block('consecutive-bare-tagged-turns', `第 ${run.start}–${run.end} 行连续 ${current.maxConsecutiveBare} 个对白行是报幕式标签（主语加至多 4 字修饰直接接说话动词、无动作分句，本书上限 ${limits.maxConsecutiveBareTaggedTurns}），需退回补人物动作或改写`);
  }
  if (current.bareCount >= 5 && current.bareRatio >= 0.50) {
    advise('bare-tag-ratio', `报幕式标签 ${current.bareCount} 处，占全部归属标记的 ${(current.bareRatio * 100).toFixed(0)}%`);
  }

  const baselineDensity = mean(baseline, (item) => item.metrics.density);
  if (
    baselineDensity !== null
    && current.dialogueTurns >= 6
    && current.attributionCount >= 5
    && current.density >= 0.50
    && current.density >= baselineDensity + 0.25
    && current.density >= baselineDensity * 1.5
  ) {
    advise('baseline-drift', `本章密度 ${current.density.toFixed(2)}，明显高于前期基线 ${baselineDensity.toFixed(2)}`);
  }

  const recentDensity = mean(recent, (item) => item.metrics.density);
  if (
    recentDensity !== null
    && current.dialogueTurns >= 6
    && current.attributionCount >= 5
    && current.density >= 0.60
    && current.density >= recentDensity + 0.25
  ) {
    advise('chapter-spike', `本章密度 ${current.density.toFixed(2)}，较近期均值 ${recentDensity.toFixed(2)} 突升`);
  }

  return { findings, advisories, baselineDensity, recentDensity };
}

function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(`dialogue_drift_gate: ${error.message}`);
    console.error(USAGE);
    process.exit(3);
  }
  if (options.help) {
    console.log(USAGE);
    process.exit(0);
  }

  const currentPath = path.resolve(options.current);
  try {
    const current = analyze(fs.readFileSync(currentPath, 'utf8'));
    const projectRoot = resolveProjectRoot(options, currentPath);
    const maxConsecutive = resolveDialogueLimit(options.maxConsecutiveTaggedTurns, projectRoot,
      'max_consecutive_tagged_turns', DEFAULT_MAX_CONSECUTIVE_TAGGED_TURNS);
    const maxConsecutiveBare = resolveDialogueLimit(options.maxConsecutiveBareTaggedTurns, projectRoot,
      'max_consecutive_bare_tagged_turns', DEFAULT_MAX_CONSECUTIVE_BARE_TAGGED_TURNS);
    const historySource = resolveHistoryDir(options, currentPath, projectRoot);
    const currentNumber = options.chapter !== undefined ? options.chapter : chapterNumber(currentPath);
    const history = loadHistory(currentPath, currentNumber, historySource);
    let historyNotice = null;
    if (currentNumber === null) {
      historyNotice = {
        code: 'history-baseline-skipped',
        message: `cross-chapter baseline skipped: no chapter number in "${path.basename(currentPath)}"; `
          + 'pass --chapter N (candidates: 追踪/候选章/第NNN章/<run-id>/candidate.md)',
      };
    } else if (options.chapter !== undefined && currentNumber > 1 && history.length === 0) {
      historyNotice = {
        code: 'history-baseline-empty',
        message: `cross-chapter baseline empty: no chapter before 第${String(currentNumber).padStart(3, '0')}章 in ${historySource.dir}; `
          + 'pass --project <root> or --history-dir <正文 dir>',
      };
    }
    const baseline = history.slice(0, options.baselineCount);
    const recent = history.slice(-options.recentWindow);
    const evaluation = evaluate(current, baseline, recent, {
      maxConsecutiveTaggedTurns: maxConsecutive.limit,
      maxConsecutiveBareTaggedTurns: maxConsecutiveBare.limit,
    });
    const rejected = evaluation.findings.length > 0;
    const result = {
      status: rejected ? 'rejected' : 'passed',
      file: currentPath,
      metrics: {
        dialogue_turns: current.dialogueTurns,
        attribution_count: current.attributionCount,
        attribution_density: Number(current.density.toFixed(3)),
        max_consecutive_tagged_turns: current.maxConsecutive,
        max_consecutive_tagged_turns_limit: maxConsecutive.limit,
        max_consecutive_tagged_turns_limit_source: maxConsecutive.source,
        bare_tagged_count: current.bareCount,
        bare_tagged_ratio: Number(current.bareRatio.toFixed(3)),
        max_consecutive_bare_tagged_turns: current.maxConsecutiveBare,
        max_consecutive_bare_tagged_turns_run: current.maxBareRun,
        max_consecutive_bare_tagged_turns_limit: maxConsecutiveBare.limit,
        max_consecutive_bare_tagged_turns_limit_source: maxConsecutiveBare.source,
        top_verb: current.topVerb,
        top_family: current.topFamily,
        baseline_density: evaluation.baselineDensity === null ? null : Number(evaluation.baselineDensity.toFixed(3)),
        recent_density: evaluation.recentDensity === null ? null : Number(evaluation.recentDensity.toFixed(3)),
      },
      findings: evaluation.findings,
      advisories: evaluation.advisories,
      examples: current.occurrences.slice(0, 12),
      next_action: rejected
        ? 'return_to_narrative_writer_for_contextual_revision'
        : evaluation.advisories.length
          ? 'continue_with_semantic_dialogue_review'
          : 'continue_workflow',
    };
    if (historyNotice) result.history_notice = historyNotice;

    if (options.json) {
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    } else if (!rejected) {
      console.log(`PASS: dialogue attribution is stable in ${currentPath}`);
      evaluation.advisories.forEach((finding) => console.log(`  ADVISORY [${finding.code}] ${finding.message}`));
    } else {
      console.error(`REJECTED: dialogue attribution drift in ${currentPath}`);
      evaluation.findings.forEach((finding) => console.error(`  [${finding.code}] ${finding.message}`));
      evaluation.advisories.forEach((finding) => console.error(`  ADVISORY [${finding.code}] ${finding.message}`));
      current.occurrences.slice(0, 12).forEach((item) => console.error(`  ${item.line}:${item.column} [${item.text}] ${item.context}`));
      console.error('Return this chapter to the narrative writer, revise in context, then rerun the gate.');
    }
    if (!options.json && historyNotice) console.error(`NOTICE [${historyNotice.code}] ${historyNotice.message}`);
    // exitCode 而不是 process.exit()：macOS 上写管道是异步的，立刻退出会截断 --json 输出
    process.exitCode = rejected ? 2 : 0;
  } catch (error) {
    console.error(`dialogue_drift_gate: ${error.message}`);
    process.exit(3);
  }
}

main();
