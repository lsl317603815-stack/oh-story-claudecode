# craft/ — 通用短剧编剧方法论库

写作层（L1–L5）的 **playbook**。这 11 篇是**题材无关**的通用短剧编剧术——子 agent 出稿前按下表加载对应文件，**运行时挑该剧题材对应的"分题材"横表行**即可，无需为某部剧改写。前 6 篇管**结构**（题材/反派/节奏/爽点/钩子/开场），3 篇管**台词 line-level**（L5 主用）：`dialogue-action.md` 管「**说什么 / 功能**」（信息前置·潜台词动作化·动作可视化），`dialogue-naturalism.md` 管「**怎么说才像真人**」（去 AI 味七宗罪·潜台词·中文口语·声音卡）——两篇是骨与肉，配对加载；**`dialogue-protocol.md` 管「怎么让前两篇真正进生成回路」**（2026-07-29 猫E019 战例定档：知识在库里≠知识在回路里——落字前成对样本+历史弹药 grep／落字中点名三吸引子／落字后机械自查[回译测试·废话检验]／样例先行交付·模型无关可喂 gpt/codex）——**L5 落对白以它为执行总纲**。第 10 篇 `scene-audit.md` 管**出稿后的诊断**（剧本医生层：因果链「因为」非「然后」·场景价值转变·删场测试·七维评分）——前 9 篇管写，它管审。第 11 篇 `adaptation-sourcing.md` 管**小说改编项目的回采纪律**（事件锚点检索+知情状态对照防后段穿帮·删线五判据·登记不等于兑现）——仅改编项目加载，构思案阶段与 L5 全稿回原著时消费。

## 层映射（哪层加载哪篇 → 管什么）

| 写作层 | 加载 | 这篇管什么 |
|---|---|---|
| **L1 世界观** | `genre-guide.md` | 13 题材定位 + 叠加公式（主+副，≤3）+ 出海映射 |
| **L2 人物** | `villain-design.md` | 反派四层递进（小/中/大/隐藏）+ 可恨/可信/递进 + 伏笔模板 + 分层台词 |
| **L3 大纲** | `rhythm-curve.md` + `satisfaction-matrix.md` | 全剧波形双模型（付费卡点剧递增四阶段 / 免费平台前置爆点·按商业模式选）+ 5 爽点分型 + 分布 |
| **L4 分集** | `hook-design.md` + `satisfaction-matrix.md` | 每集结尾钩型（5 钩）+ 本集爽点（类型+强度）+ 关键集标记 |
| **L5 单集剧本** | `opening-rules.md`（首集）+ `rhythm-curve.md` + `hook-design.md` + `dialogue-action.md` + `dialogue-naturalism.md` + **`dialogue-protocol.md`（落对白执行总纲）** | 黄金5秒开场 + 单集微型三幕（情绪弹簧·每集压/放二元）+ 结尾钩落地 + **台词信息前置/潜台词动作化/动作可视化** + **去 AI 味（七宗罪 linter·潜台词·中文口语·声音卡）** + **落字流程（成对样本+历史弹药→点名三吸引子→回译测试/废话检验机械自查→样例先行）** |
| **剧本医生**（L3/L4/L5 出稿后） | `scene-audit.md` | 逐场因果审计（「因为」非「然后」+ 问题标签）+ 价值转变/删场测试 + 单集七维评分——锁版 gate 前的诊断报告层 |
| **改编项目**（构思案 / L5 全稿回原著时） | `adaptation-sourcing.md` | 事件锚点检索（不用概念词）+ 知情状态对照（防后段穿帮）+ 召回只补细节 + 删线五判据/合并五问 + 登记不等于兑现 |

## 用法约定

- **当护栏 + 清单用**：每篇尾部都有"质量自检表"，写完即对照自查。
- **挑分题材行**：每篇都有"分题材"横表（战神/霸总/甜宠/重生/古装/悬疑/末日/校园…），按本剧题材取对应行，别套错调性。
- **不重复钩型**：hook-design 规矩——连续 3 集别用同一钩型（防免疫）。
- **强度分布先选模型**（2026-08-14 定档）：rhythm-curve / satisfaction-matrix 规矩——付费卡点剧最强爽压最后阶段（递增），红果式免费剧最强打脸前置到第一幕收官、尾段峰值改情感向。

## 来源与许可

**结构 6 篇**（genre / villain / rhythm / satisfaction / hook / opening）来自开源项目 **[0xsline/short-drama](https://github.com/0xsline/short-drama)**，原样吸收，遵循其 **MIT License**：

```
MIT License · Copyright (c) 2025 0xsline

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction... The above copyright notice and this
permission notice shall be included in all copies or substantial portions of
the Software.
```

完整协议见 https://github.com/0xsline/short-drama/blob/main/LICENSE 。

**line-level 第 1 篇** `dialogue-action.md`（台词与动作可视化）方法论提炼自开源项目 **[GongLingRui/screen-creative-skills](https://github.com/GongLingRui/screen-creative-skills)** 的 `drama-creator`（情绪弹簧理论·金牌剧作官），遵循其 **MIT License · Copyright (c) 2026 宫凡**；`rhythm-curve.md` 的「情绪弹簧·二元定律」段同源。本仓按自身体例重写并补 9:16 竖屏/下游出图联动，非逐字照搬。

**line-level 第 2 篇** `dialogue-naturalism.md`（对白自然化·去 AI 味）为**本仓自撰**：方法论骨架取自影视编剧通论（潜台词 / 各怀目的 / 答非所问 / 具体代抽象，对应 Mamet·McKee·Sorkin 的对白观）+ 中文口语化技法；全部 before→after 实例取自本仓真实短剧（《天才足球少年》E001–E003）的对白诊断与对抗重写，无第三方版权。生成自 `dialogue-craft-mastery` 工作流（诊断 32 句 AI 味台词 → 对抗重写 18 句 → codify），2026-06-08。**2026-06-09 经 `shortdrama-dialogue-mastery` 工作流大幅扩充**——逐本全文读完 **122 部真实竖屏爆款短剧**（GitHub `jubenjuben` 仓·同源 GongLingRui），出 124 张拉片卡 → 6 维综合，新增：七宗罪升「带豁免位软 linter」+ 3 宗新变体（ECHO_CONFIRM/MOUTHPIECE_NPC/REGISTER_SPLIT）、中文口语肌理 T1–T10 linter、声音卡四引擎（自称/双声道/演变弧/男女差）、功能位法则、题材声腔对照表 +《天才足球少年》声腔配方。引证均挂真实爆款台词（`card_NNN` 可回溯，语料未随本包分发，在源库 `craft/_research-shortdrama-corpus/`）。

**前 6 篇结构方法论 · 2026-06-09 实证补编**：原 6 篇取自 `0xsline/short-drama`，是**纯理论、无真实爆款背书**。经 `shortdrama-structure-mastery` 工作流逐本结构重读 **122 部真实竖屏爆款短剧**（同 `jubenjuben` 仓）→ 124 张结构卡 → 每篇文末追加一节 `## 实证补编 · 124 部真实爆款回采`，给原理论配真实爆款实证（新增/校准/印证，引证 `scard_NNN` 可回溯，语料未随本包分发，在源库 `craft/_research-structure-corpus/`）。核心增量：剧情引擎=隐藏信息差×养肥分层打脸（可证伪+见证者升级）×麦格芬三级延宕；in medias res 开局判读约 96%；集尾停"将揭未揭"；爽点必须可证伪（落进球/比分非台词）。原理论一字未动，实证作补编挂其后。

**第 9 篇** `scene-audit.md`（逐场审计·剧本医生层）吸收自 **DeepWhite screenwriting v1**（用户资料包，2026-06-12）的 screenwriter 审计引擎——其内核为剧作通论公有方法（麦基 Controlling Idea / Scene Value 价值转变、亚里士多德行动统一/删场测试、因果链审计），按本仓体例重写＋竖屏短剧适配（价值对补「知晓/无知＝信息差刻度」「身份/强弱/债」爽点对、过渡场零容忍、七维评分对接本仓 gate 流），非逐字照搬。

**第 11 篇** `adaptation-sourcing.md`（改编回采与兑现纪律）提炼自 **[zenstory-ai/drama-skills](https://github.com/zenstory-ai/drama-skills)** 的 `adaptation-craft` 一篇（MIT · Copyright (c) 2026 drama-skills contributors），2026-08-28 外采全批后按本仓管线术语重写（判决账《参考-drama-skills外采-20260828》在源库，不随本包分发），非逐字照搬。

本 README 与各层的 `## 加载方法论` 接线段为本仓库新增。
