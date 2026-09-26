import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

async function write(root, relativePath, content) {
  const target = resolve(root, relativePath);
  await mkdir(resolve(target, ".."), { recursive: true });
  await writeFile(target, content, "utf8");
}

function json(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

const PROJECT = "长篇/测试长篇项目";
const OPEN_CANDIDATE_RUN = `${PROJECT}/追踪/候选章/第041章/C20260926-090000`;
const CLOSED_CANDIDATE_RUN = `${PROJECT}/追踪/候选章/第040章/C20260925-090000`;

function snapshot(identity, location, goal, openThreads = []) {
  return {
    identity,
    location,
    goal,
    state: "平稳",
    abilities_resources: [],
    relationships: [],
    knowledge: [],
    open_threads: openThreads,
  };
}

function foreshadow(id, summary, planted, planned, status, importance) {
  return {
    id,
    summary,
    planted_chapter: planted,
    planned_resolution_chapter: planned,
    status,
    importance,
    updated_chapter: status === "已埋" ? planted : planned,
  };
}

/**
 * 当前事务协议（schema_version 5）的追踪状态。数字刻意挑到能覆盖状态卡的每一块：
 * 下一章 N = 41；F001 计划第 38 章 → 逾期 3 章；F002 计划第 42 章 → 临近；
 * F003 已回收、F004 回收章远在第 60 章，都不该出现在提醒里；
 * 沈砚仍在 active_character_names 但第 20 章后未出场 → 久别 20 章；
 * 周衡不在活跃名单、有 2 条未了线程、第 8 章后未出场 → 搁置 32 章；
 * 林岚久未出场但没有未了线程 → 不提醒。
 */
export const FIXTURE_TRACKING_STATE = {
  schema_version: 5,
  book_title: "测试长篇项目",
  last_committed_chapter: 40,
  imported_through_chapter: 0,
  state_revision: 42,
  context: {
    position: {
      volume: "第一卷",
      volume_start_chapter: 1,
      story_time: "入秋第三日",
      scene: "旧城档案馆",
    },
    long_term_constraints: ["顾临不会用剑"],
    active_character_names: ["顾临", "沈砚"],
    continuity_risks: [],
    recent_chapters: [{ chapter: 40, summary: "顾临在档案馆找到旧信封。" }],
    next_chapter_commitments: ["交代旧信封的来历"],
  },
  characters: {
    顾临: snapshot("档案管理员", "旧城档案馆", "查清旧信封来历", ["旧信封的寄件人"]),
    沈砚: snapshot("顾临旧友", "城南码头", "躲开追债人"),
    周衡: snapshot("旧账房", "下落不明", "找回失踪的账本", ["欠顾临的人情", "失踪的账本"]),
    林岚: snapshot("茶馆老板", "东街茶馆", "守住茶馆"),
  },
  foreshadow: {
    F001: foreshadow("F001", "旧信封里夹着一把铜钥匙", 3, 38, "已埋", "高"),
    F002: foreshadow("F002", "档案馆地下室的第二道门", 12, 42, "已埋", "中"),
    F003: foreshadow("F003", "沈砚左手的旧伤", 5, 20, "已回收", "低"),
    F004: foreshadow("F004", "城外钟楼停摆的时间", 30, 60, "已埋", "中"),
  },
  timeline: {},
  facts: {},
  appearances: {
    周衡: { seen: [2, 5, 8], snapshot_chapter: 8 },
    林岚: { seen: [6], snapshot_chapter: 6 },
    沈砚: { seen: [14, 16, 18, 20], snapshot_chapter: 20 },
    顾临: { seen: [33, 34, 35, 36, 37, 38, 39, 40], snapshot_chapter: 40 },
  },
};

function characterCard(name, card, seen) {
  const threads = card.open_threads.length ? card.open_threads.map((item) => `- ${item}`) : ["- 无"];
  return [
    `# ${name}｜当前状态`,
    "",
    "- 状态修订：42",
    `- 快照更新：第${seen.at(-1)}章`,
    `- 最近出场：第${seen.at(-1)}章`,
    `- 身份：${card.identity}`,
    `- 位置：${card.location}`,
    `- 当前目标：${card.goal}`,
    `- 身心状态：${card.state}`,
    "",
    "## 未结事项",
    ...threads,
    "",
  ].join("\n");
}

/**
 * Build a neutral Dashboard fixture without importing any public demo novel,
 * author showcase, or benchmark corpus into this repository.
 */
export async function createDashboardFixture(root) {
  const state = FIXTURE_TRACKING_STATE;
  const files = new Map([
    ["拆文库/对标样本甲/_progress.md", "# 拆文进度\n\n状态：完成\n"],
    ["拆文库/对标样本甲/快速预览.md", "# 快速预览\n\n中性测试数据。\n"],
    ["拆文库/对标样本甲/概要.md", "# 概要\n\n用于测试目录浏览。\n"],
    ["拆文库/对标样本甲/文风.md", "# 文风\n\n叙事自然，长短句交替。\n"],
    ["拆文库/对标样本甲/拆文报告.md", "# 对标样本甲\n\n仅用于 Dashboard 回归测试。\n"],
    ["拆文库/对标样本甲/章节/第1章.md", "# 第一章\n\n测试章节。\n"],
    ["拆文库/对标样本甲/角色/顾临.md", "# 顾临\n\n中性测试角色。\n"],
    ["拆文库/对标样本乙/概要.md", "# 对标样本乙\n\n第二个中性拆文样本。\n"],
    ["拆文库/对标样本乙/文风.md", "# 文风\n\n用于验证多拆文库扫描。\n"],
    [
      `${PROJECT}/大纲/大纲.md`,
      "# 大纲\n\n中性测试项目。\n\n## 全书体量与阶段总览\n\n- 全书总章节数：300 章\n- 目标字数：30 万字\n",
    ],
    [`${PROJECT}/大纲/细纲_第001章.md`, "# 第001章细纲\n"],
    [`${PROJECT}/大纲/细纲_第002章.md`, "# 第002章细纲\n"],
    [`${PROJECT}/大纲/细纲_第003章.md`, "# 第003章细纲\n"],
    // 状态卡字数：标题行不计，全角缩进等空白不计，其余按码位计 → 6 + 7 = 13 字
    [`${PROJECT}/正文/第001章.md`, "# 第001章\n\n顾临推开门。\n"],
    [`${PROJECT}/正文/第002章.md`, "# 第002章\n\n　　沈砚没有回头。\n"],
    [`${PROJECT}/设定/文风.md`, "# 文风\n\n自然叙事。\n"],
    [`${PROJECT}/设定/世界设定.md`, "# 世界设定\n"],
    [`${PROJECT}/设定/角色/顾临.md`, "# 顾临\n\n测试主角。\n"],
    [`${PROJECT}/设定/角色/关系.md`, "# 关系\n\n顾临与配角的关系。\n"],
    // 当前追踪协议：_tracking-state.json 是唯一提交点，其余 .md 都是它的派生视图。
    [`${PROJECT}/追踪/_tracking-state.json`, json(state)],
    [
      `${PROJECT}/追踪/上下文.md`,
      [
        "# 写作连续性上下文 — 测试长篇项目",
        "",
        "> 状态修订：42。截至当前章的续写状态卡，只放下一章真正需要的连续性状态。",
        "",
        "## 当前位置",
        "- 当前章：第40章",
        "## 长期约束",
        "- 顾临不会用剑",
        "## 核心角色状态",
        "- 顾临｜档案管理员｜平稳",
        "- 沈砚｜顾临旧友｜平稳｜【久别20章，上次出场第20章】",
        "## 活跃伏笔",
        "- 【逾期3章】F001｜旧信封里夹着一把铜钥匙｜埋第3章｜第38章｜高",
        "- 【临近】F002｜档案馆地下室的第二道门｜埋第12章｜第42章｜中",
        "## 近章速记",
        "- 第40章｜顾临在档案馆找到旧信封。",
        "## 下一章承诺",
        "- 交代旧信封的来历",
        "## 连贯性风险",
        "- 无",
        "",
      ].join("\n"),
    ],
    [
      `${PROJECT}/追踪/伏笔.md`,
      [
        "# 伏笔当前状态",
        "",
        "> 状态修订：42。每个 ID 只保留一行当前状态；历史变化见 `逐章记录/`。",
        "",
        "| ID | 内容 | 埋设章 | 计划回收章 | 状态 | 重要度 | 最近变更章 |",
        "|---|---|---:|---:|---|---|---:|",
        ...Object.values(state.foreshadow).map(
          (row) =>
            `| ${row.id} | ${row.summary} | 第${row.planted_chapter}章 | 第${row.planned_resolution_chapter}章 | ` +
            `${row.status} | ${row.importance} | 第${row.updated_chapter}章 |`,
        ),
        "",
      ].join("\n"),
    ],
    [`${PROJECT}/追踪/时间线/作者真相.md`, "# 时间线｜作者真相\n\n> 状态修订：42。\n"],
    [`${PROJECT}/追踪/时间线/读者已知.md`, "# 时间线｜读者已知\n\n> 状态修订：42。\n"],
    ...Object.entries(state.characters).map(([name, card]) => [
      `${PROJECT}/追踪/角色状态/${name}.md`,
      characterCard(name, card, state.appearances[name].seen),
    ]),
    [
      `${PROJECT}/追踪/章节提交/第001章.json`,
      json({
        schema_version: 1,
        status: "committed",
        chapter: 1,
        target: "正文/第001章.md",
        accepted_at: "2026-09-01T08:00:00Z",
        approval_mode: "review",
        protocol: "gated-v2",
      }),
    ],
    [
      `${PROJECT}/追踪/章节提交/第002章.json`,
      json({
        schema_version: 1,
        status: "committed",
        chapter: 2,
        target: "正文/第002章.md",
        accepted_at: "2026-09-02T08:00:00Z",
        approval_mode: "review",
        protocol: "legacy-v1",
      }),
    ],
    // 已闭环的旧候选排在前面，状态卡必须跳过它，认出第 41 章这份未闭环的。
    [
      `${CLOSED_CANDIDATE_RUN}/manifest.json`,
      json({
        schema_version: 2,
        candidate_id: "C20260925-090000",
        status: "committed",
        chapter: 40,
        approval_mode: "review",
        protocol: "gated-v2",
        gate_run: { status: "pass" },
        reviews: { deslop: { verdict: "PASS" }, consistency: { verdict: "PASS" } },
      }),
    ],
    [
      `${OPEN_CANDIDATE_RUN}/manifest.json`,
      json({
        schema_version: 2,
        candidate_id: "C20260926-090000",
        status: "draft",
        chapter: 41,
        target: "正文/第041章.md",
        candidate: "追踪/候选章/第041章/C20260926-090000/candidate.md",
        approval_mode: "review",
        expected_last_committed_chapter: 40,
        expected_state_revision: 42,
        protocol: "gated-v2",
        review_policy: "full",
        gate_run: { status: "pass", failures: [], advisories: [], waived: [] },
        reviews: { deslop: { kind: "deslop", verdict: "PASS" } },
        waivers: [],
      }),
    ],
    [`${OPEN_CANDIDATE_RUN}/candidate.md`, "# 第041章\n\n候选稿。\n"],
  ]);

  await Promise.all(
    [...files].map(([relativePath, content]) =>
      write(root, relativePath, content),
    ),
  );
}
