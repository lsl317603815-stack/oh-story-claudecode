---
trigger: always_on
---

<!-- oh-story-managed: antigravity-rule -->
# oh-story 网文写作项目规则（Antigravity）

本工作区部署了 oh-story 网文写作 skills，位置是 `.agents/skills/`。先读所选 skill 的 `SKILL.md`，再按它的指引加载 references 和脚本。若工作区根目录同时存在供 Codex 或通用环境使用的 `AGENTS.md`，在 Antigravity 内一律以本文件及 `.agents/` 下的 skills、agents、hooks 为准，忽略 `AGENTS.md` 中的无 hook 自锁、solo 降级与根目录 `skills/` 路径说明。

## 路由

- 长篇开书、大纲、细纲、写正文、续写、修改已写章节：`story-long-write`
- 短篇写作：`story-short-write`；短剧剧本：`story-drama-write`
- 逐项拍板的采访式定稿：`story-grill`
- 长篇 / 短篇拆文：`story-long-analyze` / `story-short-analyze`
- 长篇 / 短篇扫榜：`story-long-scan` / `story-short-scan`
- 去 AI 味：`story-deslop`；多视角审查与读者审读：`story-review`
- 查当前作品进度、角色状态、伏笔、设定：`story-explore`
- 外部资料研究：`story-research`；平台数据分析：`story-data-analyze`
- 导入已有作品：`story-import`；封面：`story-cover`；有声书：`story-audiobook`
- 发布材料整理：`story-release-package`；发到平台：`story-publish`
- 意图不明确：`story`；部署或更新本工具：`story-setup`
- 复用已登录的 Chrome：`browser-cdp`

## 写作硬约束

- 所有书稿、临时片段、追踪事务和生成的项目目录都放在当前工作区内（inside the current workspace）。不得在 `~/.gemini/`、Antigravity 的 `scratch/` 或工作区外任何目录创建或续写书稿，除非作者明确点名那个外部位置。
- 写长篇正文前必须有对应的 `大纲/细纲_第N章*.md`；短篇必须有 `小节大纲.md`。
- 长篇新章按 `story-long-write` 的候选章流程走：先写候选稿，跑完它规定的门禁、去味审查和一致性审查，作者接纳后再由脚本落盘到 `正文/`。不要直接把新章写进 `正文/`。
- 修改已接纳的正文、细纲、卷纲、总纲、设定或世界规则，先按 `story-long-write` 的修订流程用它的 revision_guard.py 建立修订清单；没有活动清单时 hook 会拒绝写入。
- `追踪/_tracking-state.json` 是结构化真源，只能通过 skill 里的追踪脚本修改；不要手改它派生的 Markdown 视图。
- 写完正文后，先处理 oh-story hooks 注入的确定性检测结果，再去写下一章或结束任务。

## 专业子代理与 Orchestrator 协调模式

- 子代理只看 `.agents/agents/agent-name/agent.md`（`agent-name` 为子代理名），用 `invoke_subagent` 加同名 `TypeName` 调用。除非作者当轮明确指定其他模型，调用时必须显式传 `Model` 锁定档位，不要省略或传 `inherit`（防止 Orchestrator 执行轮的轻量模型向下继承）：`narrative-writer`、`story-architect`、`character-designer`、`consistency-checker`、`revision-governor`、`story-researcher` 传 `Model: "pro"`；`story-explorer`、`chapter-extractor` 传 `Model: "flash"`。skill 正文里提到的 `.claude/agents/`、`.opencode/agents/`、`.codex/agents/` 是其他宿主的目录，在 Antigravity 里不要拿它们判断是否已部署。
- `invoke_subagent` 除 `TypeName`、`Model` 外带一句简短 `Role` 和完整 `Prompt`；`Prompt` 里写明要读的具体文件路径，`agy` CLI 里只读子代理没有 `find_by_name`/`grep_search` 时可以直接 `read_file`。`invoke_subagent` 在后台异步运行：派出后结束本轮，等子代理用 `send_message` 回报再做依赖它结果的步骤（提交候选、汇总审查），不要在同一轮里假定结果已到。
- 运行时不能启动子代理、返回未知 agent 或子代理失败时，按 skill 写明的 solo/direct 降级执行，并在回复里说明降级了。
- 不要在 Antigravity 里再调用 `agy` 或 `.story/models/run-narrative-writer.sh`（及旧 `.claude/models/run-narrative-writer.sh`）：那是 Claude Code 宿主把写手转给 Gemini 的转发器，在这里调用会自己调用自己。在 `narrative-writer` 子代理可用时，长篇候选章初稿、整章重写、短篇正文及大段润色一律派发 `narrative-writer`（`Model: "pro"`）执笔，禁止主会话（尤其是 Orchestrator 执行轮）直接在主会话代写正文；仅在子代理不可用触发 solo 降级，或作者明确只改零星字句时才由主会话直接改稿。
- 主会话开启 Orchestrator（规划/执行两阶段）时：纯结构讨论、分支推演、采访拍板（未要求落盘）直接留在规划轮回答；需要写章、补纲或回炉时，规划轮只读核清 `追踪/上下文.md`、细纲/卷纲与情绪节奏意图并写清写手单后交棒，执行轮严格按计划建候选、派子代理、跑门禁和提交追踪，遇到细纲欠字数（`outline_underfilled`）、设定硬冲突（S1/S2）或路线分歧时回交规划轮裁决，不得擅自编剧情硬凑。
- 项目启用了模型分工里的“去 AI 味审查”（`.story/models/config.json` 或旧 `.claude/models/config.json` 中 `deslop.enabled` 为 true）时，由主会话按 `chapter_candidate.py next` 给出的 `run-deslop --run <候选目录>` 命令运行（底层调用 `.story/models/run-deslop-review.py`）交给方舟审查模型并自动登记回执；输出 `RUNNING <运行ID>` 时加 `--wait-run <运行ID>` 继续等。不要自己替它审稿。

## 和作者沟通

- 回复用作品里的说法（书、章、大纲、设定），报错时再引用脚本名、字段名或状态名。
- skill 里标了 `<!-- author-report -->` 的模板只是格式参考：回复里写模板内的文字，不要带代码块围栏。
- 作者要求记住、忘掉或确认某个写作习惯时，用 `story-long-write` 或 `story-short-write` 的作者记忆管理，写入相应作品或工作区的 `.story/作者记忆/`，不要存进 Antigravity 自带的记忆。一次性的要求直接照做，不记录。
- 不要修改本工作区已安装的 skill 文件（`SKILL.md`、`references/`、`scripts/`），重新运行 `story-setup` 部署除外。skill 脚本报错时，停下来报告错误和命令，不要绕过它。

## 上下文恢复

新开对话时，以及上下文疑似被压缩、作品状态不确定时，先找到当前作品，读它的 `追踪/上下文.md` 再继续。Antigravity 的外部 hook 没有压缩前后事件，这条规则是压缩后恢复上下文的必经路径。
