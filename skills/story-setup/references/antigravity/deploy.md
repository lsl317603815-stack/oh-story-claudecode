# Google Antigravity 部署

`target_cli` 含 `antigravity` 时，由 SKILL.md 读取并依次执行本文件。路径检查、幂等重跑、残留清理、部署标记、模板占位符和报告遵循 SKILL.md 的公共规则。本目录提供 Always-On Rule（`rules/oh-story.md`）、named-group hooks（`hooks/hooks.json`）与 I/O adapter（`hooks/story_antigravity_hook.js`）；写后 findings 经 session artifact 交给 PreInvocation/Stop。

Antigravity IDE 与交互式命令行 `agy` 共用这套项目内 `.agents/` 产物。在 Antigravity 里，正文由宿主模型（Gemini）直接写；Claude Code 侧“写手转发给 agy”的模型分工只在 Claude Code 里有意义，两者可以在同一个项目里并存。

## 部署清单（机械可检查）

| Source path | Target path | Owner class | Merge mode | Validation check |
|-------------|-------------|-------------|------------|------------------|
| current package skill root + `scripts/deploy-antigravity-skills.py` | `.agents/skills/{browser-cdp,story*}/` | story-setup managed for 21 canonical skill names | atomically replace known dirs; preserve unknown skills; never write through symlink | 21 real skill directories with valid `SKILL.md` exist |
| `scripts/generate-antigravity-agents.mjs` + `references/templates/agents/` | `.agents/agents/agent-name/agent.md`（`agent-name` 为实际名称） | story-setup managed for 8 known agent definitions | generate then atomically replace known definitions; preserve unknown user agents | 8 Markdown agents parse; exact Antigravity tool names; `mainAgent: false`, `subagent: true` |
| `references/antigravity/rules/oh-story.md` | `.agents/rules/oh-story.md` | story-setup managed | replace | `trigger: always_on`; under 12,000 characters |
| `references/antigravity/hooks/hooks.json` | `.agents/hooks.json` | user+managed | replace only top-level `oh-story` group | valid Antigravity named-group schema; user groups preserved; idempotent |
| `references/antigravity/hooks/{story_antigravity_hook.js,story_hook_core.js}` | `.agents/hooks/` same names | story-setup managed | replace | Node syntax valid; core byte-identical to shared source; hook contract tests pass |
| `scripts/merge-antigravity-hooks.py` | deployment helper only | story-setup helper | execute | atomically replaces only `oh-story`, preserves user groups, idempotent |

## Antigravity 部署算法

Antigravity 2.0 使用项目 `.agents/` customization 根。部署时创建 `.agents/skills`、`.agents/agents`、`.agents/rules`、`.agents/hooks` 并合并 `.agents/hooks.json`；不修改用户 home 下的 `~/.gemini/`。

1. 确认 `node` 在 PATH；agent 生成与项目 hooks 都依赖 Node。缺失时停止 Antigravity 这一目标的部署，不留下半成品，提示安装 Node 后重跑。
2. 部署 skills：按跨平台规则探测 Python 3，运行 `deploy-antigravity-skills.py --source "{当前 skill 包根}" --dest "{项目}/.agents/skills"`。helper 只替换 21 个 canonical 名称、保留用户其他 skills，源目标同一 realpath 时 no-op。目标必须是**真实目录**，不要新建顶层 `.agents/skills → ../skills` symlink。
   - 已有 `.agents/skills` 是 symlink（常见于 Codex / Reasonix 项目）时，helper 会停止且不沿链接写入。用 AskUserQuestion 说明：迁移会把链接当前可见的所有 skills 复制成项目内真实目录、只更新 21 个 oh-story 名称、保留链接目标原样，但 symlink 本身会被替换成目录，可能形成较大的 git diff。只有用户明确同意后才加 `--migrate-symlink` 重跑；拒绝则停止 Antigravity 部署并报告未完成。这个确认不得被“多端部署”或已有 Codex symlink 跳过。
3. 部署 agents：运行 `node "{story-setup skill目录}/scripts/generate-antigravity-agents.mjs" --source "{story-setup skill目录}/references/templates/agents" --dest "{项目}/.agents/agents"`。生成器以 Claude Markdown agent 为真源，确定性转换工具名（`Read→view_file`、`Glob→find_by_name`、`Grep→grep_search`、`Write→write_to_file`、`Edit→replace_file_content/multi_replace_file_content`、`Bash→run_command`）、模型档（haiku→`flash`，其余→`pro`）、reference 根（`.agents/skills/`）和调用术语（`invoke_subagent` + `TypeName`）；先渲染全部 8 个再原子替换，保留其他用户 agent，清理旧版同名扁平 `.md`，不沿 managed agent symlink 写出项目外。不得把 Claude frontmatter 原样复制过去。
   - 始终用 `references/templates/agents/` 的原生 `narrative-writer`，**不要**用 `references/templates/models/agents/` 的转发器：转发器在 Antigravity 里会调用 `agy` 自己。
   - 去 AI 味审查转发器（`deslop-reviewer`）不生成为 Antigravity 子代理（子代理默认沙箱执行，可能连不上方舟接口）；启用模型分工时由主会话按 Always-On Rule 直接运行 `.claude/models/run-deslop-review.py`。
4. 复制 `rules/oh-story.md` 到 `.agents/rules/oh-story.md`，验证 `trigger: always_on` 且小于 12,000 字符。这条 rule 承担 skill 路由、写作硬约束、子代理目录与 compact 后恢复；Antigravity IDE 不以根 `AGENTS.md` 作为 workspace rule，不要用 AGENTS 模板代替。
5. 复制 `hooks/story_antigravity_hook.js` 与同目录 `story_hook_core.js` 到 `.agents/hooks/`，验证 `node --check`。hook 命令以 `.agents/`（`hooks.json` 所在目录）为工作目录，写作 `hooks/story_antigravity_hook.js`，不得写成 `.agents/hooks/...`。共享 core 必须与 Claude/OpenCode/ZCode/TRAE/WorkBuddy 副本字节一致。
6. 合并 hooks：运行 `merge-antigravity-hooks.py "{项目}/.agents/hooks.json" "{story-setup skill目录}/references/antigravity/hooks/hooks.json"`。helper 只替换顶层 `oh-story` named group，保留其他用户 hook groups；写后复跑并比较字节确认幂等。禁止把 Claude/Codex 的外层 `{ "hooks": ... }` schema 写入 Antigravity。
7. 事件边界：只注册 `PreToolUse`、`PostToolUse`、`PreInvocation`、`Stop`。PreToolUse 为每次调用输出 `decision`，写前先查修订门禁（已提交内容需活动修订清单）再查正文前置门；PostToolUse 只输出 `{}`，正文 findings 经 session `artifactDirectoryPath` 暂存，由下一次 PreInvocation 注入；模型准备直接结束时 Stop 最多强制继续一次。外部 hooks 没有 SessionStart/PreCompact/PostCompact，首次上下文由 `invocationNum=0` 的 PreInvocation 注入，compact 后由 Always-On Rule 要求读取 `追踪/上下文.md`。
8. `.story-deployed` 的 `target_cli` 写 `antigravity` 或多端组合（如 `claude-code,antigravity`）。只部署 Antigravity 时 `references_dir` 写 `.agents/skills/story-setup/references/agent-references`；与 Claude Code 等多端并存时保留原有值（两份 agent-references 来自同一个包，内容一致）。

## 验证

- `.agents/skills/` 下 21 个 skill 为真实目录且 `SKILL.md` 可读；`.agents/skills/story-setup/references/agent-references/` 完整
- `.agents/agents/` 下 8 个 Markdown agent 可解析，名称、模型档、工具白名单、只读边界（`chapter-extractor`、`consistency-checker`、`revision-governor`、`story-explorer` 无写文件与命令工具）与 `.agents/skills/` reference 前缀正确
- `.agents/rules/oh-story.md` 为 `trigger: always_on` 且小于 12,000 字符
- `.agents/hooks.json` 有效、顶层 `oh-story` group 恰有 PreToolUse/PostToolUse/PreInvocation/Stop，用户 hook groups 保留；`.agents/hooks/story_antigravity_hook.js` 与 `story_hook_core.js` 语法有效

## 安装报告必须提示

- 新开 Antigravity conversation（或重开 `agy`）刷新 customization；Hooks 依赖 PATH 中的 `node`。
- 外部 hook API 没有 PreCompact/PostCompact，compact 恢复靠 Always-On Rule 读取 `追踪/上下文.md`。
- 命令行写作：在项目目录进入交互式 `agy`，先确认 `/skills`、`/agents`、`/hooks` 已发现 oh-story 再发任务。print 模式 `agy -p` 必须带 `--add-dir "$PWD"`，否则不加载工作区 `.agents/` 的 skills、agents 与 hooks，并可能把产物写进 `~/.gemini/antigravity-cli/scratch/`；测试后检查 scratch 无意外小说产物。
- IDE 与交互式 `agy` 仍建议分别实机试一次。
