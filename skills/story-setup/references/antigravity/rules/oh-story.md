---
trigger: always_on
---

<!-- oh-story-managed: antigravity-rule -->
# oh-story 网文写作项目规则（Antigravity）

本工作区部署了 oh-story 网文写作 skills，位置是 `.agents/skills/`。先读所选 skill 的 `SKILL.md`，再按它的指引加载 references 和脚本。

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

## 专业子代理

- 子代理只看 `.agents/agents/agent-name/agent.md`（`agent-name` 为子代理名），用 `invoke_subagent` 加同名 `TypeName` 调用。skill 正文里提到的 `.claude/agents/`、`.opencode/agents/`、`.codex/agents/` 是其他宿主的目录，在 Antigravity 里不要拿它们判断是否已部署。
- 运行时不能启动子代理、返回未知 agent 或子代理失败时，按 skill 写明的 solo/direct 降级执行，并在回复里说明降级了。
- 不要在 Antigravity 里再调用 `agy` 或 `.claude/models/run-narrative-writer.sh`：那是 Claude Code 宿主把写手转给 Gemini 的转发器，在这里调用会自己调用自己。在 Antigravity 里，正文由主会话或 `narrative-writer` 子代理直接写。
- 项目启用了模型分工里的“去 AI 味审查”（`.claude/models/config.json` 中 `deslop.enabled` 为 true）时，`chapter_candidate.py review-packet --kind deslop` 生成的任务要交给方舟审查模型：由主会话把任务原文写进 `.claude/models/runs/inbox-<时间戳>.md`，再运行 `for PYBIN in python3 python py; do "$PYBIN" -c "" 2>/dev/null && break; done; "$PYBIN" .claude/models/run-deslop-review.py start <该文件>`；输出 `RUNNING <运行ID>` 时用 `wait <运行ID>` 继续等，结果原样交回流程。不要自己替它审稿。

## 和作者沟通

- 回复用作品里的说法（书、章、大纲、设定），报错时再引用脚本名、字段名或状态名。
- skill 里标了 `<!-- author-report -->` 的模板只是格式参考：回复里写模板内的文字，不要带代码块围栏。
- 作者要求记住、忘掉或确认某个写作习惯时，用 `story-long-write` 或 `story-short-write` 的作者记忆管理，写入相应作品或工作区的 `.story/作者记忆/`，不要存进 Antigravity 自带的记忆。一次性的要求直接照做，不记录。
- 不要修改本工作区已安装的 skill 文件（`SKILL.md`、`references/`、`scripts/`），重新运行 `story-setup` 部署除外。skill 脚本报错时，停下来报告错误和命令，不要绕过它。

## 上下文恢复

新开对话时，以及上下文疑似被压缩、作品状态不确定时，先找到当前作品，读它的 `追踪/上下文.md` 再继续。Antigravity 的外部 hook 没有压缩前后事件，这条规则是压缩后恢复上下文的必经路径。
