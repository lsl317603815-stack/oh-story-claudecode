# 章节候选、接纳与投影自检协议

正文不是模型生成后立即成立的事实。所有会改动正文的步骤都放在作者接纳**之前**；接纳之后，正文只能走大修流程。默认链路固定为：

```text
精确一章许可 → 隔离候选稿 → 14 道确定性门禁 → 去味审查回执 → 复扫门禁 → 一致性审查回执
→ 用户接纳 → 原子写入正文 + 质检回执入库 → 逐章追踪事务 → 提交凭证闭环 → 更新声音画像 → story doctor
```

顺序由 `chapter_candidate.py` 强制。主会话不必记步骤：反复运行 `next`，只执行它打印的那一步。

```bash
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py next --project "{项目根}"
```

`next` 输出 JSON：`step`（`init` / `write` / `check` / `revise` / `review` / `attest` / `await-author` / `approve` / `promote` / `tracking`）、要运行的 `commands` 和原因 `why`。

## 授权模式

- `review`（默认）：用户说“续写/继续写/写下一章”只授权生成**下一章候选稿**。门禁与两份审查都通过后，Agent 展示标题、字数、关键变化、门禁提示、审查结论和候选路径后停止。只有用户随后明确说“接受/定稿/采用这版”，才能执行 `approve` 与 `promote`。作者看到的就是全部检查都已跑过的那一版。
- `auto`（显式选择）：只有用户在本次任务中明确说“自动定稿/无需逐章确认/连续写完并自动定稿”才可启用。初始化时必须把这句授权的含义写入 `--authorization-note`。它不是全书永久设置；本次任务结束、用户中断或出现结构性分歧即失效。auto 模式不允许豁免任何门禁。
- “继续”“续写”“再写一章”本身不等于“接受当前候选”。用户要求修改候选时，只改候选稿并重新走门禁与审查，不触碰正式正文。

## 精确章节许可

每个工作区只绑定 `last_committed_chapter + 1`、一个细纲、一个正文目标和当时的 `state_revision`。同一项目同时只能有一个未闭环候选；旧候选未接纳、放弃或完成追踪闭环前，不能创建下一章。

下列命令中的 `{PYTHON}` 先按平台探测可用的 Python 3 解释器，再用实际命令替换。

```bash
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py init \
  --project "{项目根}" \
  --chapter {N} \
  --outline "大纲/细纲_第{N}章.md" \
  --target "正文/第{N}章_{标题}.md" \
  --base "大纲/卷纲_第X卷.md"
```

脚本自动绑定 `追踪/上下文.md` 和上一章正文；`--base` 再加入会决定本章有效性的总纲、卷纲、题材契约或设定。Agent 只向命令返回目录中的候选正文文件写稿，不得直接新建正式正文。

auto 模式另有两个选项：`--review-policy lean`（去味审查只看门禁标记的段落与开头结尾，一致性审查仍是全文），`--batch-last`（本章是本批最后一章，去味必须全文）。

## 确定性门禁

```bash
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py check --run "{候选运行目录}"
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py fix   --run "{候选运行目录}"
```

`check` 先验证基础文件和追踪修订没有变化，再一次跑完 14 道门禁，把结果连同候选稿 SHA-256、每个检查脚本的 SHA-256、压力档和本书门禁配置摘要写入运行目录的 `gate-report.json`。语言门不过就立即停下，其余门禁不跑；其他门禁全部跑完后一起报告。

| 门禁 | 失败时 |
|---|---|
| `writing_method`、`language`、`style_hygiene`、`ai_patterns`、`degeneration`、`prose_metrics`、`outline_copy`、`accepted_voice_profile`、`cross_chapter_shape` | 拦截：改稿后重跑 |
| `punctuation`（`normalize-punctuation.js --check`） | 拦截：运行 `fix` 自动修正并登记前后摘要 |
| `dialogue_drift`（带 `--chapter N --project`）、`emotion_floor`、`hook_strength`（黄金三章） | 拦截；启发式，可按书降级或逐章豁免 |
| `typos` | 只记录，不拦截 |

接纳基线与作者精选黄金样本的声音漂移只作双向 advisory，具体语义见 `accepted-voice-profile.md`；近章结构证据必须按 `cross-chapter-shape.md` 的五问做语义复核，不因数值接近自动改文。任一已配置画像本身过期属于数据来源错误，会阻断继续使用旧范围。

**情绪下限压力档**：自动取本章细纲「章节定位」——含“高压”为 high，含“低压 / 信息整理 / 过场”为 low，其余和留空为 normal（留空时用本书配置的 `default_pressure`）。细纲写错时：

```bash
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py pressure --run "{候选运行目录}" --level high --note "{理由}"
```

覆盖会留痕；auto 模式只能调高，不能调低。

**逐章豁免**（只限 review 模式、只限三道启发式门禁、只在最近一次 `check` 里它确实失败时）：先把报告给作者看，作者确认是误报后才执行，理由写作者原话。豁免对本候选此后的版本持续有效，并随回执入库。

```bash
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py waive --run "{候选运行目录}" \
  --gate emotion_floor --reason "{作者确认误报的原话}" --confirm WAIVE
```

**按书配置** `设定/门禁配置.json`（可选，由作者决定，不由模型自行创建）：

```json
{
  "schema_version": 1,
  "gates": {"emotion_floor": {"mode": "advisory", "reason": "冷调推理，情绪默认内收"}},
  "default_pressure": "normal",
  "punctuation": {"pause_mode": "keep", "quote_mode": "keep"}
}
```

`gates` 只能调整 `dialogue_drift / emotion_floor / hook_strength`，降为 `advisory` 后失败只记录不拦截；语言、退化、AI 句式、文风卫生等确定性硬门永远不能降级。`punctuation` 决定 `check` 与 `fix` 是否清理 `……`/破折号（`pause_mode=normalize`）和引号风格；只有本书文风或发布平台明确禁用时才改。情绪下限阈值是按一部完本长篇标定的，换题材若频繁误拦，优先用这里的配置，而不是逐章豁免。

## 接纳前审查回执

去味审查和一致性审查都是**接纳前**的硬性步骤，由回执证明做过。回执能防止“忘了跑”，防不住“故意造假”；一次性校验码和逐字引文只是提高造假成本。

```bash
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py review-packet --run "{候选运行目录}" --kind deslop
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py attest --run "{候选运行目录}" --kind deslop --report "{报告文件}"
```

1. `review-packet` 要求当前候选稿已通过 `check`。它输出审查包 JSON，其中 `prompt` 字段是交给 agent 的完整任务，`report_path` 是报告存放位置。去味审查包还会把候选稿复制成运行目录下 reviews/deslop/ 里的工作副本（路径见审查包 `work_copy` 字段），agent 只改这个副本。
2. 把 `prompt` 原样交给 narrative-writer（去味）或 consistency-checker（一致性）。agent 不可用时由主线程按同一 prompt 执行，同样出报告。
3. agent 在回复末尾给出 JSON 报告：`kind`、`nonce`、`candidate_sha256`、`scope` 必须与审查包一致；`verdict` 为 `PASS / CONCERNS / REJECT`；`findings` 每条含 `severity`（S1–S4）、`category`、`quote`、`issue`，去味另含 `action`（`edited / flagged`），一致性可加 `source` 指明证据所在文件；`coverage` 列出实际核对过的角色、伏笔 ID 或检查项，没有 findings 时至少 3 项。
4. 主会话把回复原样存到 `report_path`（整份 JSON，或末尾带 json 代码块的回复原文，都可以），再运行 `attest`。`attest` 校验校验码与候选稿摘要，要求每条 `quote` 能在被审文本里逐字找到（至少 4 个字）；去味审查的工作副本在这一步写回候选稿，写回后必须重新 `check`。

顺序：去味在前，一致性在后。一致性审查要求有效的去味回执。

**回执何时失效**：
- 一致性回执绑定它审过的那一版；此后除 `fix` 以外的任何改动都会让它失效，改完重新 `check` 并复审。
- 去味回执允许之后的小修（例如为一致性问题改几句），累计改动超过正文约 25% 时失效，需要重审。
- 审查包发出后候选稿又被改动，`attest` 会拒绝，必须重新发包。

## 接纳、写入与追踪闭环

用户明确接纳（或本次任务有有效 `auto` 授权）后：

```bash
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py approve \
  --run "{候选运行目录}" --confirm ACCEPT \
  --approval-note "{用户本次接纳或自动定稿授权摘要}"

{PYTHON} skills/story-long-write/scripts/chapter_candidate.py promote \
  --run "{候选运行目录}" --confirm PROMOTE
```

`approve` 重跑全部门禁，并在以下任一情况拒绝：门禁未过且未豁免；去味或一致性回执缺失或失效；review 模式下去味回执不是全文；任一审查结论为 REJECT；一致性审查仍有 S1/S2；auto 模式存在豁免。

`promote` 原子写入正式正文，并生成 `追踪/章节提交/第NNN章.json`；其中保存已接纳正文的 SHA-256、上下文指纹、追踪修订前值和 `protocol: gated-v2`。门禁报告、两份审查回执与原始报告、豁免记录同时复制到 `追踪/质检回执/第NNN章/`，各文件摘要登记在提交凭证里。随后按 `tracking-transaction.md` 提交本章唯一追踪事务，再闭环：

```bash
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py close --run "{候选运行目录}"
{PYTHON} skills/story-long-write/scripts/voice_profile.py update --project "{项目根}"
{PYTHON} skills/story-long-write/scripts/story_doctor.py --project "{项目根}"
```

`close` 要求 `last_committed_chapter` 精确等于本章且 `state_revision` 已推进，把正文摘要、状态修订和派生视图摘要追加到 `追踪/投影日志.jsonl`，并按全部提交凭证与质检回执重建 `追踪/质检进度.md`。该表不要手改；已有的手工旧表第一次重建时会原样改名为 `追踪/质检进度_旧版手工记录.md`。需要单独重建时运行 `chapter_candidate.py progress --project "{项目根}"`。

声音画像尚未配置时 `update` 安全返回 `not_configured`；已经配置时必须把新回执纳入。`doctor` 复核：

- `_tracking-state.json` 与全部派生视图一致；到期伏笔、久别角色等追踪提醒列为 warning；
- 没有 draft / approved / promoted 的悬空候选；
- 已接纳正文未被静默手改；
- gated-v2 章节的质检回执齐全、摘要未变，门禁报告绑定的正是接纳正文；旧协议章节只合并提示一次；
- 已配置的接纳声音画像，以及作者精选的黄金声音样本，仍绑定当前已接纳正文；
- 修订事务已闭环；
- 最新同修订号投影未漂移；卷末要求时还要有闭环冷读账本。

任何 error 都阻止下一章。旧项目没有历史提交凭证时只从下一章起建立，不伪造旧章凭证；如要让既有旧章成为声音样本，必须按 `accepted-voice-profile.md` 由作者显式批准连续范围。

## 旧版候选的兼容

- 旧版（v1）manifest 仍是 draft 时，第一次被任何命令读取就原地升级为 v2，必须补完门禁与两份审查才能接纳。
- 旧版已 approve 的候选按接纳时的 8 道门禁写入正文，提交凭证标 `protocol: legacy-v1`，不追溯要求回执。

## 候选退回与合法修订

未写入正式正文的候选可显式放弃：

```bash
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py abandon \
  --run "{候选运行目录}" --confirm ABANDON --reason "{原因}"
```

正式正文一旦写入，不得用候选命令覆盖。任何手工回改先走 `revision-governor` + `revision_guard.py` + 追踪修订事务；全部闭环后，用 `sync` 更新已接纳正文摘要：

```bash
{PYTHON} skills/story-long-write/scripts/chapter_candidate.py sync \
  --project "{项目根}" --chapter {N} \
  --revision-manifest "追踪/修改影响/active.json" \
  --revision-stamp "追踪/修改影响/active.approved.json" \
  --reason "{修订原因}" --confirm SYNC
```

`sync` 会再次运行修订门禁；它不是绕过摘要失效检查的快捷开关。质检回执记录的是接纳时那一版，修订后的正文由修订流程自己的复核负责。

合法修订导致接纳正文摘要变化后，声音画像会刻意进入 `stale`。修订事务和 `sync` 全部通过后再运行 `voice_profile.py update`；由旧章显式批准、但没有回执的样本发生变化时，不得静默更新，必须重新审阅并再次确认旧章范围。
