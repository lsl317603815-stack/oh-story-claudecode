#!/usr/bin/env python3
"""番茄小说原生竖屏排版无损拆行转换与质检工具 (fanqie_typeset.py)

核心设计原理（经朱雀 100% 全绿实测 + 408 章番茄真书统计标定）：
- 阶段一由 G5 底稿锁死高口语虚词/助词密度、显式真名与零黑名单套话；
- 阶段二在不破坏 G5 词句底子（字词重合度 ≥95%）的前提下，将文本转换为番茄原生排版：
  1. 全文零空行（标题与正文间、正文段落间均无空行）；
  2. 正文每段严格以两个全角空格「　　」（\\u3000\\u3000）缩进；
  3. 100% 人物/系统对白独立单起一行（「主语+动作+说道：“……”」拆为句号收尾动作段 + 独立引号段，零悬空冒号，保留叙述内短引用词组）；
  4. 手机竖屏（首行16字、续行18字）下 1~2 行短段占比达 70%~90%，叙述段零 5 行以上文字墙。
"""

from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import re
import statistics
import sys

INDENT = "\u3000\u3000"
TITLE_PREFIX_RE = re.compile(r"^\s*#{1,6}\s*")
CHAPTER_HEADING_RE = re.compile(r"^第[0-9零一二三四五六七八九十百千万〇两]+[章节回幕集卷](?:[_\s：:·.\-]|$)")
SHORT_SECTION_RE = re.compile(r"^#{1,6}\s*(\d+)\.?\s*$")
SHORT_MD_SECTION_RE = re.compile(r"^(?:###\s*\d+\.|###\s*第[一二三四五六七八九十百千万两〇零0-9]+章|\d+\.)$")
HANZI_ALNUM_RE = re.compile(r"[\u3400-\u9fff0-9A-Za-z]")
CHINESE_CHAR_RE = re.compile(r"[\u3400-\u9fff]")

ZHUQUE_FUNCTION_WORDS = (
    "的", "了", "就", "在", "是", "也", "都", "还", "又", "把", "被", "给",
    "这个", "那个", "里面", "以后", "时候", "现在", "因为", "所以", "但是",
    "不过", "然后", "已经", "还是", "起来", "出来", "下去",
)

ZHUQUE_HARD_BLACKLIST: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("em_dash", "破折号（——）高危标点", re.compile(r"——")),
    ("contrast_not_but", "对比套句「不是…而是…」", re.compile(r"不是[^。！？!?]{1,36}而是")),
    ("contrast_where_is", "强转折「这哪里是…分明…」", re.compile(r"这哪里是[^。！？!?]{1,30}分明")),
    ("contrast_not_only_reverse", "强转折「非但…反而…」", re.compile(r"非但[^。！？!?]{1,30}反而")),
    ("unconcealed_shock", "微表情套话「掩不住的惊讶/震惊」", re.compile(r"掩不住的(?:惊讶|震惊|惊色|骇然)")),
    ("shock_freeze", "微动作套话「脸上的笑…僵在」", re.compile(r"脸上的笑[^，。！？!?]{0,8}僵在")),
    ("relieved_sigh", "全知心理套话「这才(悄悄)松了一口气」", re.compile(r"这才(?:悄悄|暗暗|微微)?松了[一]?口气")),
    ("decision_cliche", "下决心套话「想通了这一点…没有犹豫」", re.compile(r"想通了这一点[^。！？!?]{0,16}(?:没有犹豫|当场)")),
    ("assert_obviously", "全知断言词「显然早就…」", re.compile(r"显然早就")),
    ("faint_before", "昏迷套词「在彻底失去意识之前」", re.compile(r"失去意识之前")),
    ("faint_eyelid", "昏迷套词「眼皮沉重地合上」", re.compile(r"眼皮沉重地合")),
    ("camera_gaze", "镜头调度套话「把目光投向」", re.compile(r"把目光投向")),
    ("camera_standout", "镜头套话「显得格外显眼/醒目」", re.compile(r"显得格外(?:显眼|醒目|突兀)")),
    ("expression_serious", "群像套话「神色一下子认真/凝重起来」", re.compile(r"神色一下子(?:认真|凝重|严肃)起来")),
    ("storyteller_zhijian", "评书公文套词「只见那…」", re.compile(r"只见(?:那|这|他|她)")),
)

ZHUQUE_ADVISORY_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("as_for_topic", "书面话题转换词「至于…，」", re.compile(r"至于[\u3400-\u9fff]{2,8}[，,]")),
)

# 匹配标准 G5 前置动作冒号引语（兼容前缀中含有「勾了“途中灵压不适”」等场内短引用词）
LEADING_COLON_QUOTE_RE = re.compile(
    r"^(?P<prefix>(?:[^“”「」]|[“][^”]*[”]|[「][^」]*[」])+?)[：:](?P<quote>[“][^“”]*[”]|[「][^「」]*[」])$"
)

# 从句未完结标记：避免在纯时间/条件/范围状语从句后硬切句号造成残句
SUBORDINATE_STARTERS = (
    "要不是", "如果不是", "若是", "要是", "虽然", "尽管", "除开", "除了",
    "自从", "当", "趁着", "借着", "随着", "无论", "不管", "哪怕", "就算",
    "既然", "由于", "因为", "还没等", "没等", "不等", "谁知道", "哪知道", "没想到", "未曾想",
)
CORRELATIVE_STARTERS = (
    "要不是", "如果不是", "若是", "要是", "虽然", "尽管", "除开", "除了",
    "无论", "不管", "哪怕", "就算", "既然",
)
CORRELATIVE_MARKERS = ("却", "但", "可", "不过", "还是", "也", "还", "都", "就", "才", "那", "便")
SUBORDINATE_ENDINGS = (
    "的时候", "之时", "之际", "以后", "之后", "以前", "之前", "同时", "瞬间", "刹那",
    "不知道", "没想到", "未曾想", "谁知道", "还以为", "只以为", "只觉得", "总觉得",
    "生怕", "惟恐", "唯恐", "巴不得",
)
CONCRETE_ACTION_CUES = (
    "看", "盯", "望", "扫", "瞥", "敲", "拍", "按", "摸", "掏", "拿", "递", "塞",
    "推", "拉", "拽", "扯", "扶", "挡", "撞", "退", "走", "跑", "冲", "站", "坐",
    "蹲", "弯腰", "起身", "抬头", "低头", "转头", "转过头", "偏头", "点头", "摇头",
    "招手", "招了招手", "挥手", "指着", "指了指", "亮了亮", "晃着", "翻开", "合上",
    "点开", "收起", "放下", "抱起", "仰着", "沉下", "咬着", "捂着", "提示音", "勾了",
)


def to_chinese_numeral(n: int) -> str:
    digits = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
    if n < 10:
        return digits[n]
    if n == 10:
        return "十"
    if n < 20:
        return f"十{digits[n - 10]}"
    if n < 100:
        tens, ones = divmod(n, 10)
        return f"{digits[tens]}十{'' if ones == 0 else digits[ones]}"
    return str(n)


def extract_alnum_hanzi(text: str) -> str:
    lines = [
        TITLE_PREFIX_RE.sub("", l).strip()
        for l in text.splitlines()
        if l.strip() and not (l.strip().startswith("<!--") and l.strip().endswith("-->"))
    ]
    return "".join(HANZI_ALNUM_RE.findall("\n".join(lines)))


def mobile_rows(char_len: int) -> int:
    """按番茄手机端常规字号（首行两格缩进容16字，续行每行容18字）估算视觉行数。"""
    if char_len <= 16:
        return 1
    return 1 + (char_len - 16 + 17) // 18


def is_standalone_quote(s: str) -> bool:
    """判断段落是否为整行独立对白（首尾成对引号且中间未提前闭合）。"""
    return (s.startswith("“") and s.endswith("”") and "”" not in s[1:-1]) or (
        s.startswith("「") and s.endswith("」") and "」" not in s[1:-1]
    )


def is_unsplit_dialogue_line(s: str) -> bool:
    """识别未独立成行的人物/系统对白（排除叙述句内嵌的短字条/屏幕词引用）。"""
    if is_standalone_quote(s):
        return False
    if LEADING_COLON_QUOTE_RE.match(s):
        return True
    if s.startswith(("“", "「")):
        return True
    if re.search(r"[：:]\s*[“「]", s):
        return True
    if re.search(r"[”」][^“”「」]{0,16}?(?:说道|问道|答道|回道|喊道|笑道|喝道|嚷道|道)[。！？!?]?$", s):
        return True
    return False


def split_clauses_outside_quotes(text: str) -> list[str]:
    """按引号外的中文逗号切分分句，保护叙述内嵌短引号（如“同住听雪小筑，担保人：苏幼薇”）不被切碎。"""
    clauses: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in text:
        if ch in "“「":
            in_quote = True
            buf.append(ch)
        elif ch in "”」":
            in_quote = False
            buf.append(ch)
        elif not in_quote and ch == "，":
            piece = "".join(buf).strip()
            if piece:
                clauses.append(piece)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        clauses.append(tail)
    return clauses


def close_action_prefix(prefix: str) -> str:
    """将冒号前的引导前缀最小化收尾为自然句号陈述句，尽量不动原文字词。"""
    p = prefix.strip().rstrip("，,。；;：:")

    # 0) 心理活动/默念（绝不用「开口/开了口」）
    mental_replacements = [
        (r"下定决心想道$", "下定了决心"),
        (r"((?:在)?心里头?)(?:暗暗|默默)(?:想道|说道|道)$", r"\1暗暗想着"),
        (r"((?:在)?心里头?)([^\s，,。！？!?]{1,6}?)地想道$", r"\1\2地想着"),
        (r"((?:在)?心里头?)对自己(?:说道|道)$", r"\1暗暗对自己说了一句"),
        (r"((?:在)?心里头?[^，,。]*?)想道$", r"\1暗想"),
        (r"(?:暗暗|暗自|默默)(?:想道|说道|道)$", "在心里暗想"),
        (r"(?:心想道|暗道|心道|想道)$", "暗想"),
    ]
    for pattern, repl in mental_replacements:
        if re.search(pattern, p):
            return re.sub(pattern, repl, p) + "。"

    # 1) 特定带情绪/姿态/复合动作的说话动词收尾（长词优先，保留原情绪词根）
    specific_replacements = [
        # 笑系（必须在「笑道$」之前）
        (r"笑骂道$", "笑骂了一句"),
        (r"哭笑道$", "哭笑了一声"),
        (r"苦笑道$", "苦笑了一声"),
        (r"赔笑道$", "赔笑了一声"),
        (r"干笑道$", "干笑了一声"),
        (r"讪笑道$", "讪笑了一声"),
        (r"嗤笑道$", "嗤笑了一声"),
        (r"冷笑道$", "冷笑了一声"),
        (r"轻笑道$", "轻笑了一声"),
        (r"微笑道$", "微微一笑"),
        (r"大笑道$", "大笑了一声"),
        (r"嘲笑道$", "嘲笑了一句"),
        (r"玩笑道$", "开了句玩笑"),
        (r"笑道$", "笑了笑"),
        # 喝/喊/呼系（必须在「喝道$/喊道$」之前）
        (r"冷声喝道$", "冷喝了一声"),
        (r"急喝道$", "急喝了一声"),
        (r"厉喝道$", "厉喝了一声"),
        (r"低喝道$", "低喝了一声"),
        (r"怒喝道$", "怒喝了一声"),
        (r"娇喝道$", "娇喝了一声"),
        (r"暴喝道$", "暴喝了一声"),
        (r"喝道$", "大喝一声"),
        (r"哭骂道$", "哭骂起来"),
        (r"哭喊道$", "哭喊起来"),
        (r"挥手喊道$", "挥手大喊"),
        (r"惊呼道$", "惊呼了一声"),
        (r"欢呼道$", "欢呼了一声"),
        (r"招呼道$", "招呼了一声"),
        (r"嚷道$", "嚷了起来"),
        (r"吼道$", "吼了一声"),
        (r"叫道$", "叫了一声"),
        (r"喊道$", "喊了起来"),
        # 肢体动作+道（避免「点了点了点头」「叹口气开了口」）
        (r"(?:点了点头|点点头|点头)(?:说道|回答道|回道|答道|道)$", "点了点头"),
        (r"(?:摇了摇头|摇摇头|摇头)(?:说道|回答道|回道|答道|道)$", "摇了摇头"),
        (r"(?:摆了摆手|摆摆手|摆手)(?:说道|回答道|回道|答道|道)$", "摆了摆手"),
        (r"叹(?:了)?口气(?:说道|道)$", "叹了口气"),
        (r"(?<!叹口气)(?<!叹了口气)叹道$", "叹了口气"),
        (r"冷哼(?:一声)?(?:说道|道)$", "冷哼了一声"),
        (r"哼(?:了)?一声(?:说道|道)$", "哼了一声"),
        (r"(?:哼声|轻哼|哼)道$", "哼了一声"),
        # 开口/张口/出声防重言
        (r"开口([对冲朝向][^\s，,。！？!?]{1,10}?)(?:问道|问)$", r"开口\1问了一句"),
        (r"开口([对冲朝向][^\s，,。！？!?]{1,10}?)(?:说道|道|说)$", r"\1开了口"),
        (r"开口(?:问道|问)$", "开口问了一句"),
        (r"开口(?:说道|道)$", "开了口"),
        (r"张口就(?:说道|回答道|回道|答道|道)$", "张口就回了一句"),
        (r"出声(?:问道|问)$", "出声问了一句"),
        (r"出声(?:说道|道)$", "出了声"),
        # 双字及单字特定言说动词（必须在通用「说道/问道/答道/回道/道」之前）
        (r"发出提示音(?:说道|提示道|道)?$", "发出提示音"),
        (r"提示道$", "发出提示音"),
        (r"播报道$", "播报起来"),
        (r"回答道$", "回了一句"),
        (r"反问道$", "反问了一句"),
        (r"追问道$", "追问了一句"),
        (r"质问道$", "质问了一句"),
        (r"盘问道$", "盘问了一句"),
        (r"询问道$", "问了一句"),
        (r"确认道$", "确认了一句"),
        (r"提议道$", "开口提议"),
        (r"补充道$", "补充了一句"),
        (r"补了一句(?:说道|道)$", "补了一句"),
        (r"(?:继续|接着)(?:说道|道)$", "接着往下说"),
        (r"交代道$", "交代了一句"),
        (r"叮嘱道$", "叮嘱了一句"),
        (r"吩咐道$", "吩咐了一句"),
        (r"提醒([^\s，,。！？!?]{1,6}?)(?:说道|道)$", r"提醒了\1一句"),
        (r"提醒道$", "提醒了一句"),
        (r"解释道$", "开口解释"),
        (r"嘀咕道$", "嘀咕了一句"),
        (r"嘟囔道$", "嘟囔了一句"),
        (r"念叨道$", "念叨了一句"),
        (r"抱怨道$", "抱怨了一句"),
        (r"吐槽道$", "吐槽了一句"),
        (r"打趣道$", "打趣了一句"),
        (r"调侃道$", "调侃了一句"),
        (r"感慨道$", "感慨了一句"),
        (r"感叹道$", "感叹了一句"),
        (r"宽慰道$", "宽慰了一句"),
        (r"安慰道$", "安慰了一句"),
        (r"保证道$", "保证了一句"),
        (r"附和道$", "附和了一句"),
        (r"帮腔道$", "帮了一句腔"),
        (r"回嘴道$", "回了句嘴"),
        (r"嘴硬道$", "嘴硬了一句"),
        (r"顶回去(?:说道|道)$", "顶了回去"),
        (r"回绝道$", "回绝了一句"),
        (r"改口道$", "改了口"),
        (r"报备道$", "报备了一声"),
        (r"点破道$", "点破了一句"),
        (r"拍板道$", "拍了板"),
        (r"斥责道$", "斥责了一句"),
        (r"训斥道$", "训斥了一句"),
        (r"催道$", "催了一句"),
        (r"念道$", "念了出来"),
        (r"急道$", "急声开了口"),
        (r"斥道$", "斥了一句"),
        (r"刺道$", "刺了一句"),
        (r"劝道$", "劝了一句"),
        (r"哄道$", "哄了一句"),
        (r"呛道$", "呛了一句"),
        (r"骂道$", "骂了一句"),
        (r"嗔道$", "嗔了一句"),
        (r"应道$", "应了一声"),
        (r"接道$", "接了一句"),
        (r"夸道$", "夸了一句"),
        (r"赞道$", "赞了一句"),
        # 嗓音副词短语结尾（防止剥掉「说道」后留下悬空「冷声。/淡声。」）
        (r"(沉声|冷声|淡声|温声|轻声|低声|小声|大声|高声|朗声|厉声|急声|柔声)(?:问道|问)$", r"\1问了一句"),
        (r"(沉声|冷声|淡声|温声|轻声|低声|小声|大声|高声|朗声|厉声|急声|柔声)(?:说道|答道|回道|道)$", r"\1开了口"),
    ]
    for pattern, repl in specific_replacements:
        if re.search(pattern, p):
            p = re.sub(pattern, repl, p)
            return p + "。"

    # 2) 「对/冲/朝/向 + 某人 + 说道/问道」：按说话或提问自然收尾
    m_prep = re.search(r"([对冲朝向][^\s，,。！？!?]{1,10}?)(?:说道|问道|答道|回道|道|说|问)$", p)
    if m_prep:
        if p.endswith(("问道", "问")):
            p = re.sub(r"(?:问道|问)$", "问了一句", p)
        else:
            p = re.sub(r"(?:说道|答道|回道|道|说)$", "开了口", p)
        return p + "。"

    # 3) 「副词 + 地/随口/顺口 + 说道/问道」（如「温和地说道」「随口问道」）
    if re.search(r"(?:地|随口|顺口)(?:说道|问道|答道|回道|道)$", p):
        if p.endswith("问道"):
            p = re.sub(r"问道$", "问了一句", p)
        else:
            p = re.sub(r"(?:说道|答道|回道|道)$", "开口", p)
        return p + "。"

    # 4) 所有其余「问道」统一保留疑问语气为「问了一句」（兼容「，问道」「指着问道」「抬头问道」）
    if re.search(r"，(?:问道|问)$", p):
        return re.sub(r"，(?:问道|问)$", "，问了一句。", p)
    if p.endswith("问道"):
        return re.sub(r"问道$", "问了一句。", p)

    # 5) 常规「说道/答道/回道/道」：若前缀最后一个分句已含具体动作词，直接省去说话词落句号
    if re.search(r"(?:说道|答道|回道|道)$", p):
        stripped_verb = re.sub(r"(?:说道|答道|回道|道)$", "", p).rstrip("，,")
        last_clause = split_clauses_outside_quotes(stripped_verb)[-1]
        if any(cue in last_clause for cue in CONCRETE_ACTION_CUES) and len(last_clause) >= 4:
            return stripped_verb + "。"
        return stripped_verb + "开了口。"

    # 6) 本身不以「道」结尾（如「声音传了出来：」）直接补句号
    return p + "。"


def is_incomplete_subordinate(clauses_so_far: list[str]) -> bool:
    """判断当前累积的分句串是否尚未落出主句谓语；若是则与下一分句保持逗号连接。"""
    c = "，".join(clauses_so_far).strip("，,。！？!? ")
    last_cl = clauses_so_far[-1].strip("，,。！？!? ")
    if len(c) < 9:
        return True
    if any(last_cl.endswith(suf) for suf in SUBORDINATE_ENDINGS):
        return True
    if ("除了" in last_cl or "除开" in last_cl) and not any(
        w in last_cl for w in ("之外", "以外", "还", "也", "都", "只")
    ):
        return True
    if len(clauses_so_far) == 1:
        return any(c.startswith(st) for st in SUBORDINATE_STARTERS)
    # 已累积 ≥2 个分句：若最新分句自身新起一个从句引导词，则继续向右粘合其主句
    if any(last_cl.startswith(st) for st in SUBORDINATE_STARTERS):
        return True
    # 若首分句是关联引导词（虽然/要不是/既然等），等待后续分句出现关联呼应词（或长度已满两行半）
    if any(clauses_so_far[0].startswith(st) for st in CORRELATIVE_STARTERS):
        tail_joined = "，".join(clauses_so_far[1:])
        if len(c) < 44 and not any(m in tail_joined for m in CORRELATIVE_MARKERS):
            return True
    return False


def split_long_narrative_sentence(sent: str, max_two_row_chars: int = 34) -> list[str]:
    """将超过 2 行（>34字）的多逗号叙述复句切分为自然的 1~2 行短段，保护从句与内嵌短引号。"""
    s = sent.strip()
    if not s:
        return []
    if len(s) <= max_two_row_chars or is_standalone_quote(s):
        return [s]

    terminal = s[-1] if s[-1] in "。！？!?" else "。"
    core = s[:-1] if s[-1] in "。！？!?" else s
    raw_clauses = split_clauses_outside_quotes(core)
    if len(raw_clauses) <= 1:
        return [s]

    # 先把不完整的状语/条件从句向右粘合，保证每个切出来的单元都是完整句
    merged_units: list[str] = []
    buf: list[str] = []
    for idx, cl in enumerate(raw_clauses):
        buf.append(cl)
        is_last = idx == len(raw_clauses) - 1
        if not is_last and is_incomplete_subordinate(buf):
            continue
        merged_units.append("，".join(buf))
        buf = []
    if buf:
        tail_str = "，".join(buf)
        if merged_units:
            merged_units[-1] = f"{merged_units[-1]}，{tail_str}"
        else:
            merged_units.append(tail_str)

    if len(merged_units) == 1:
        return [merged_units[0] + terminal]

    # 再将过短的相邻单元（合并后仍 ≤28 字）适度合拢，避免切成一地 8 字碎渣
    packed: list[str] = []
    for u in merged_units:
        if packed and (len(packed[-1]) < 13 or (len(packed[-1]) + 1 + len(u) <= 28)):
            packed[-1] = f"{packed[-1]}，{u}"
        else:
            packed.append(u)
    if len(packed) >= 2 and len(packed[-1]) < 14 and (len(packed[-2]) + 1 + len(packed[-1]) <= 46):
        packed[-2] = f"{packed[-2]}，{packed[-1]}"
        packed.pop()

    out: list[str] = []
    for i, seg in enumerate(packed):
        end_mark = terminal if i == len(packed) - 1 else "。"
        out.append(seg + end_mark)
    return out


def split_paragraph_outside_quotes(para: str) -> list[str]:
    """按引号外的句末标点切分单段内的多句话。"""
    spans: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in para:
        buf.append(ch)
        if ch in "“「":
            in_quote = True
        elif ch in "”」":
            in_quote = False
        elif not in_quote and ch in "。！？!?":
            piece = "".join(buf).strip()
            if piece:
                spans.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        spans.append(tail)
    return spans


def convert_g5_to_fanqie_parts(
    text: str,
    fallback_title: str | None = None,
    *,
    keep_markdown_heading: bool = False,
) -> tuple[str | None, list[str]]:
    """将 G5 全绿源稿拆解为 (标题, 番茄竖屏自然段列表[不含缩进])。"""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title_line: str | None = None
    body_paras: list[str] = []

    for raw in raw_lines:
        s = raw.strip()
        if not s:
            continue
        if s.startswith("<!--") and s.endswith("-->"):
            continue
        if keep_markdown_heading and SHORT_MD_SECTION_RE.match(s):
            body_paras.append(s)
            continue
        short_sec = SHORT_SECTION_RE.match(s)
        if short_sec:
            body_paras.append(f"第{to_chinese_numeral(int(short_sec.group(1)))}章")
            continue
        if title_line is None and not body_paras and (
            TITLE_PREFIX_RE.match(s) or CHAPTER_HEADING_RE.match(TITLE_PREFIX_RE.sub("", s))
        ):
            title_line = TITLE_PREFIX_RE.sub("", s).strip()
            continue
        body_paras.append(TITLE_PREFIX_RE.sub("", s).strip())

    if title_line is None and fallback_title:
        title_line = TITLE_PREFIX_RE.sub("", fallback_title).strip()

    fanqie_paras: list[str] = []
    for para in body_paras:
        if keep_markdown_heading and SHORT_MD_SECTION_RE.match(para):
            fanqie_paras.append(para)
            continue
        if CHAPTER_HEADING_RE.match(para) and len(para) <= 12:
            fanqie_paras.append(para)
            continue
        for sent in split_paragraph_outside_quotes(para):
            m = LEADING_COLON_QUOTE_RE.match(sent)
            if m:
                action_sent = close_action_prefix(m.group("prefix"))
                quote_sent = m.group("quote").strip()
                fanqie_paras.extend(split_long_narrative_sentence(action_sent, max_two_row_chars=32))
                fanqie_paras.append(quote_sent)
            else:
                fanqie_paras.extend(split_long_narrative_sentence(sent, max_two_row_chars=34))

    return title_line, fanqie_paras


def convert_g5_to_fanqie(
    text: str,
    fallback_title: str | None = None,
    *,
    keep_markdown_heading: bool = False,
) -> str:
    """将 G5 全绿源稿无损转换为番茄原生竖屏排版（全文0空行 + 两全角空格缩进；支持 .txt 或 `# 第N章` .md 直出）。"""
    title_line, fanqie_paras = convert_g5_to_fanqie_parts(
        text,
        fallback_title=fallback_title,
        keep_markdown_heading=keep_markdown_heading,
    )
    out_lines: list[str] = []
    if title_line:
        out_lines.append(f"# {title_line}" if keep_markdown_heading else title_line)
    for p in fanqie_paras:
        if keep_markdown_heading and SHORT_MD_SECTION_RE.match(p):
            out_lines.append(p)
        else:
            out_lines.append(f"{INDENT}{p}")
    return "\n".join(out_lines) + "\n"


def _strip_inline_quotes(text: str) -> str:
    text = re.sub(r"“[^”]*”", "“”", text)
    text = re.sub(r"「[^」]*」", "「」", text)
    return text


def analyze_zhuque_windows(indexed_prose: list[tuple[int, str]]) -> dict:
    """分析番茄竖屏短段稿的朱雀滑动窗口全绿不变量（经 16 章 100% 全绿真书语料与实测报告标定）。"""
    line_records = []
    for line_no, s in indexed_prose:
        is_q = is_standalone_quote(s) or is_unsplit_dialogue_line(s)
        cn = len(CHINESE_CHAR_RE.findall(s))
        narr_str = "" if is_standalone_quote(s) else _strip_inline_quotes(s)
        narr_cn = len(CHINESE_CHAR_RE.findall(narr_str))
        line_records.append(
            {
                "line_no": line_no,
                "text": s,
                "is_quote": is_q,
                "cn": cn,
                "narr_str": narr_str,
                "narr_cn": narr_cn,
            }
        )

    total_cn = sum(r["cn"] for r in line_records)

    # 1) 开篇首句对白前纯叙述跨度与全章最长零对白跨度
    open_quote_gap = 0
    seen_first_quote = False
    max_zero_quote_gap = 0
    max_gap_range = (0, 0)
    cur_gap = 0
    cur_start = 0

    for r in line_records:
        if r["is_quote"]:
            if not seen_first_quote:
                seen_first_quote = True
            if cur_gap > max_zero_quote_gap:
                max_zero_quote_gap = cur_gap
                max_gap_range = (cur_start, prev_line)
            cur_gap = 0
            cur_start = 0
        else:
            if not seen_first_quote:
                open_quote_gap += r["cn"]
            if cur_gap == 0:
                cur_start = r["line_no"]
            cur_gap += r["cn"]
            prev_line = r["line_no"]

    if cur_gap > max_zero_quote_gap and line_records:
        max_zero_quote_gap = cur_gap
        max_gap_range = (cur_start, line_records[-1]["line_no"])

    # 2) 500 汉字滑动窗口最少对白句数（16/16 全绿样本全部 >= 2）
    min_quotes_in_500c: int | None = None
    min_quotes_500_range = (0, 0)
    sparse_500_spans: list[tuple[int, int, int]] = []
    next_sparse_idx = 0
    if total_cn >= 520:
        suffix_cn = [0] * (len(line_records) + 1)
        for idx in range(len(line_records) - 1, -1, -1):
            suffix_cn[idx] = suffix_cn[idx + 1] + line_records[idx]["cn"]
        for i in range(len(line_records)):
            if suffix_cn[i] < 500:
                break
            win_cn = 0
            win_q = 0
            end_idx = i
            for j in range(i, len(line_records)):
                win_cn += line_records[j]["cn"]
                if line_records[j]["is_quote"]:
                    win_q += 1
                end_idx = j
                if win_cn >= 500:
                    break
            end_line = line_records[end_idx]["line_no"]
            if min_quotes_in_500c is None or win_q < min_quotes_in_500c:
                min_quotes_in_500c = win_q
                min_quotes_500_range = (line_records[i]["line_no"], end_line)
            if win_q < 2 and i >= next_sparse_idx and len(sparse_500_spans) < 4:
                sparse_500_spans.append((line_records[i]["line_no"], end_line, win_q))
                next_sparse_idx = end_idx + 1

    # 3) 260 汉字零对白纯叙述滑窗局部口语虚词密度（防局部干瘪半文言塌陷）
    min_zero_quote_fn_k: float | None = None
    min_fn_range = (0, 0)
    for i in range(len(line_records)):
        win_cn = 0
        win_q = 0
        narr_parts: list[str] = []
        narr_cn = 0
        end_line = line_records[i]["line_no"]
        for j in range(i, len(line_records)):
            win_cn += line_records[j]["cn"]
            if line_records[j]["is_quote"]:
                win_q += 1
            else:
                narr_parts.append(line_records[j]["narr_str"])
                narr_cn += line_records[j]["narr_cn"]
            end_line = line_records[j]["line_no"]
            if win_cn >= 260:
                break
        if win_cn >= 240 and win_q == 0 and narr_cn >= 210:
            joined = "".join(narr_parts)
            fn_hits = sum(joined.count(w) for w in ZHUQUE_FUNCTION_WORDS)
            fn_k = round(fn_hits * 1000.0 / narr_cn, 1)
            if min_zero_quote_fn_k is None or fn_k < min_zero_quote_fn_k:
                min_zero_quote_fn_k = fn_k
                min_fn_range = (line_records[i]["line_no"], end_line)

    blocking_issues: list[str] = []
    advisories: list[str] = []

    if total_cn >= 400 and open_quote_gap > 270:
        blocking_issues.append(
            f"朱雀首窗风险：开篇前 {open_quote_gap} 字无独立对白（上限 270 字，建议前 150 字内落出首句人物对白）"
        )
    elif total_cn >= 400 and open_quote_gap > 180:
        advisories.append(f"开篇前 {open_quote_gap} 字才出现首句对白（建议压缩到 180 字以内稳住首窗）")

    if total_cn >= 400 and max_zero_quote_gap > 290:
        blocking_issues.append(
            f"朱雀滑窗静默沙漠：行 {max_gap_range[0]}~{max_gap_range[1]} 连续 {max_zero_quote_gap} 字无独立对白"
            "（上限 290 字，单人修炼/破局戏须穿插搭话、巡查提醒或传讯打断）"
        )
    elif total_cn >= 400 and max_zero_quote_gap > 230:
        advisories.append(
            f"行 {max_gap_range[0]}~{max_gap_range[1]} 连续 {max_zero_quote_gap} 字无对白（接近 290 字滑窗红线）"
        )

    for sp_start, sp_end, sp_q in sparse_500_spans:
        blocking_issues.append(
            f"朱雀500字滑窗对白不足：行 {sp_start}~{sp_end} 约500字窗口内仅 "
            f"{sp_q} 句对白（全绿底线 ≥2 句）"
        )

    if min_zero_quote_fn_k is not None and min_zero_quote_fn_k < 115.0:
        blocking_issues.append(
            f"朱雀零对白滑窗虚词塌陷：行 {min_fn_range[0]}~{min_fn_range[1]} 局部叙述虚词仅 "
            f"{min_zero_quote_fn_k}/千字（底线 ≥115/千字）"
        )

    for r in line_records:
        for _, label, pat in ZHUQUE_HARD_BLACKLIST:
            m = pat.search(r["text"])
            if m:
                blocking_issues.append(f"朱雀高危套话（行 {r['line_no']}）：{label}「{m.group(0)}」")
        for _, label, pat in ZHUQUE_ADVISORY_PATTERNS:
            m = pat.search(r["text"])
            if m:
                advisories.append(f"朱雀疑似套话（行 {r['line_no']}）：{label}「{m.group(0)}」")

    return {
        "total_cn_chars": total_cn,
        "open_quote_gap_cn": open_quote_gap,
        "max_zero_quote_gap_cn": max_zero_quote_gap,
        "max_zero_quote_gap_lines": list(max_gap_range),
        "min_quotes_in_500c": min_quotes_in_500c,
        "min_quotes_in_500c_lines": list(min_quotes_500_range),
        "min_zero_quote_fn_per_kilo": min_zero_quote_fn_k,
        "blocking_issues": blocking_issues,
        "advisories": advisories,
    }


def analyze_fanqie_layout(
    path: pathlib.Path,
    g5_ref: pathlib.Path | None = None,
    *,
    require_markdown_h1: bool = False,
    check_zhuque_windows: bool = False,
) -> dict:
    raw = path.read_text(encoding="utf-8")
    all_lines = raw.splitlines()
    blank_line_count = sum(1 for l in all_lines if not l.strip())

    indexed_nonempty = [(idx + 1, l) for idx, l in enumerate(all_lines) if l.strip()]
    has_title = bool(
        indexed_nonempty
        and not indexed_nonempty[0][1].startswith(INDENT)
        and not SHORT_MD_SECTION_RE.match(indexed_nonempty[0][1].strip())
        and (
            TITLE_PREFIX_RE.match(indexed_nonempty[0][1].strip())
            or CHAPTER_HEADING_RE.match(TITLE_PREFIX_RE.sub("", indexed_nonempty[0][1].strip()))
        )
    )
    raw_body_indexed = indexed_nonempty[1:] if has_title else indexed_nonempty
    bad_indent_lines = [
        line_no
        for line_no, l in raw_body_indexed
        if not l.startswith(INDENT) and not SHORT_MD_SECTION_RE.match(l.strip())
    ]

    indexed_prose = [
        (line_no, l.strip())
        for line_no, l in raw_body_indexed
        if not SHORT_MD_SECTION_RE.match(l.strip())
    ]
    stripped = [s for _, s in indexed_prose]
    lens = [len(s) for s in stripped]
    standalone_quotes = [s for s in stripped if is_standalone_quote(s)]
    unsplit_dialogues = [i + 1 for i, s in enumerate(stripped) if is_unsplit_dialogue_line(s)]
    quotes_total = len(standalone_quotes) + len(unsplit_dialogues)
    dangling_colons = [i + 1 for i, s in enumerate(stripped) if s.endswith(("：", ":"))]

    row_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    narr_5plus = 0
    quote_6plus = 0
    for s in stripped:
        r = mobile_rows(len(s))
        row_counts[r if r < 5 else 5] += 1
        if r >= 5 and not is_standalone_quote(s):
            narr_5plus += 1
        if r >= 6 and is_standalone_quote(s):
            quote_6plus += 1

    total_p = max(1, len(lens))
    row_pcts = {k: round(v * 100.0 / total_p, 1) for k, v in row_counts.items()}
    one_two_pct = round((row_counts[1] + row_counts[2]) * 100.0 / total_p, 1)

    sim_pct: float | None = None
    if g5_ref is not None:
        seq_a = extract_alnum_hanzi(g5_ref.read_text(encoding="utf-8"))
        seq_b = extract_alnum_hanzi(raw)
        sim_pct = round(difflib.SequenceMatcher(None, seq_a, seq_b).ratio() * 100.0, 2)

    issues: list[str] = []
    advisories: list[str] = []
    if require_markdown_h1:
        first_line = all_lines[0] if all_lines else ""
        if not re.match(r"^#\s+第[0-9零一二三四五六七八九十百千万〇两]+章(?:\s+|$)", first_line):
            issues.append("首行必须是 `# 第N章 标题` 一级标题格式（兼容平台直传剥标题协议）")
    if blank_line_count != 0:
        issues.append(f"存在 {blank_line_count} 行空行（番茄原生标准要求全文 0 空行）")
    if bad_indent_lines:
        issues.append(f"有 {len(bad_indent_lines)} 段未以两全角空格缩进（首处行号 {bad_indent_lines[0]}）")
    if unsplit_dialogues:
        issues.append(f"存在未独立成行的对白段落（段落序号 {unsplit_dialogues[:5]}）")
    if dangling_colons:
        issues.append(f"存在悬空冒号结尾段落（段落序号 {dangling_colons[:5]}）")
    if one_two_pct < 68.0:
        issues.append(f"手机1~2行短段占比偏低（{one_two_pct}% < 68%）")
    if narr_5plus > 0:
        issues.append(f"存在 {narr_5plus} 个 5行及以上叙述文字墙长段")
    if quote_6plus > 0 or row_counts[5] > max(2, int(total_p * 0.03)):
        issues.append(f"长台词 5行+ 段落偏多或超长（5行+共 {row_counts[5]} 段，6行+台词 {quote_6plus} 段）")
    if sim_pct is not None and sim_pct < 95.0:
        issues.append(f"与 G5 底稿词面重合度偏低（{sim_pct}% < 95%，警惕拆行时篡改原词）")

    zhuque_windows: dict | None = None
    if check_zhuque_windows:
        zhuque_windows = analyze_zhuque_windows(indexed_prose)
        issues.extend(zhuque_windows["blocking_issues"])
        advisories.extend(zhuque_windows["advisories"])

    findings = [
        {"severity": "blocking", "type": "fanqie-layout", "message": msg}
        for msg in issues
    ] + [
        {"severity": "advisory", "type": "zhuque-window", "message": msg}
        for msg in advisories
    ]

    result = {
        "file": str(path),
        "passed": len(issues) == 0,
        "paragraphs": len(lens),
        "blank_lines": blank_line_count,
        "mean_chars": round(statistics.fmean(lens), 1) if lens else 0.0,
        "median_chars": round(statistics.median(lens), 1) if lens else 0.0,
        "std_chars": round(statistics.pstdev(lens), 1) if len(lens) > 1 else 0.0,
        "quotes_total": quotes_total,
        "quotes_standalone": len(standalone_quotes),
        "row_pcts": row_pcts,
        "one_two_row_pct": one_two_pct,
        "lexical_similarity_pct": sim_pct,
        "issues": issues,
        "advisories": advisories,
        "findings": findings,
    }
    if zhuque_windows is not None:
        result["zhuque_windows"] = zhuque_windows
    return result


def format_report(rep: dict) -> str:
    status = "PASS (符合番茄真书原生排版)" if rep["passed"] else "NEEDS_ADJUST"
    rp = rep["row_pcts"]
    sim_str = f" | G5词面保留率 {rep['lexical_similarity_pct']}%" if rep["lexical_similarity_pct"] is not None else ""
    lines = [
        f"=== 番茄原生竖屏排版体检: {rep['file']} ===",
        f"状态: {status}",
        (
            f"段落: {rep['paragraphs']} 段 | 空行: {rep['blank_lines']} | "
            f"段均 {rep['mean_chars']}字 (中位 {rep['median_chars']}字, σ={rep['std_chars']}) | "
            f"独立对白: {rep['quotes_standalone']}/{rep['quotes_total']}{sim_str}"
        ),
        (
            f"手机视觉行数分布: 1~2行合计 {rep['one_two_row_pct']}% "
            f"[1行:{rp[1]}% | 2行:{rp[2]}% | 3行:{rp[3]}% | 4行:{rp[4]}% | 5行+:{rp[5]}%]"
        ),
    ]
    zw = rep.get("zhuque_windows")
    if isinstance(zw, dict):
        lines.append(
            f"朱雀滑窗: 开篇无对白 {zw['open_quote_gap_cn']}字 | "
            f"最大连续无对白 {zw['max_zero_quote_gap_cn']}字 | "
            f"500字窗最少对白 {zw['min_quotes_in_500c']}"
        )
    for issue in rep["issues"]:
        lines.append(f"  - [拦截] {issue}")
    for adv in rep.get("advisories", []):
        lines.append(f"  - [提示] {adv}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="G5 全绿源稿转番茄原生竖屏排版（TXT / 直传 .md）与行数分布质检工具")
    parser.add_argument("files", nargs="*", help="待体检的章节文件（.txt 或 .md）")
    parser.add_argument("--convert", metavar="G5_FILE", help="输入已过检的 G5 源稿（.md 或 .txt），转换为番茄原生排版")
    parser.add_argument("-o", "--output", metavar="OUT_FILE", help="转换输出路径（配合 --convert 使用；.md 默认保留 `# 第N章` 标题）")
    parser.add_argument("--md", action="store_true", help="输出各章直传 Markdown 格式（首行 `# 第N章 标题` + 全文0空行 + 两全角空格缩进）")
    parser.add_argument("--txt", action="store_true", help="强制输出无 `#` 前缀的纯文本标题格式")
    parser.add_argument("--require-md-h1", action="store_true", help="体检时强制要求首行为 `# 第N章 标题` 一级标题")
    parser.add_argument("--check-zhuque-windows", action="store_true", help="同步检查朱雀 500 字滑窗对白密度、最大零对白跨度与高危套话")
    parser.add_argument("--compare-g5", metavar="G5_REF", help="对比原 G5 底稿核验字词保留率（≥95%%）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON 报告")
    args = parser.parse_args()

    if args.md and args.txt:
        parser.error("--md 与 --txt 不能同时使用")

    if args.convert:
        src = pathlib.Path(args.convert).expanduser().resolve()
        dst = pathlib.Path(args.output).expanduser().resolve() if args.output else None
        keep_md = bool(args.md or (dst is not None and dst.suffix.lower() == ".md" and not args.txt))
        converted = convert_g5_to_fanqie(src.read_text(encoding="utf-8"), keep_markdown_heading=keep_md)
        if dst is not None:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(converted, encoding="utf-8")
            rep = analyze_fanqie_layout(
                dst,
                g5_ref=src,
                require_markdown_h1=keep_md or args.require_md_h1,
                check_zhuque_windows=args.check_zhuque_windows,
            )
            if args.json:
                print(json.dumps(rep, ensure_ascii=False, indent=2))
            else:
                print(format_report(rep))
            return 0 if rep["passed"] else 1
        sys.stdout.write(converted)
        return 0

    if not args.files:
        parser.print_help()
        return 2

    g5_ref = pathlib.Path(args.compare_g5).expanduser().resolve() if args.compare_g5 else None
    all_ok = True
    raw_reports = []
    for f in args.files:
        rep = analyze_fanqie_layout(
            pathlib.Path(f).expanduser().resolve(),
            g5_ref=g5_ref,
            require_markdown_h1=args.require_md_h1,
            check_zhuque_windows=args.check_zhuque_windows,
        )
        if not rep["passed"]:
            all_ok = False
        raw_reports.append(rep)
    if args.json:
        payload = raw_reports[0] if len(raw_reports) == 1 else {"passed": all_ok, "results": raw_reports}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(format_report(r) for r in raw_reports))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
