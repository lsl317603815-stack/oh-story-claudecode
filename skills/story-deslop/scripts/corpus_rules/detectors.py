#!/usr/bin/env python3
"""去 AI 味语料验证规则 · 网文线确定性检测器（规则表 v1-2026-10-06 的检测来源）。

每条 surface 规则一个纯函数：输入段落列表（已过 voice_profile.normalized_lines），
输出命中列表；不改文本。命中字段：rule、para（段号，0 起）、sent（段内句号，0 起）、
sentence（所在句原文）、span（段内起止偏移）、text（命中片段）、in_dialogue（是否落在引号内）。
metric 类规则（整章一个数）返回单元素列表：{rule, kind:"metric", value, num, den}。
规则去留不在这里定：见 ../../references/pattern-contracts.json 的 surface_rules（retired 的检测器照常可跑）。

实现约束：
- lieflat-less-ai-tone（MIT）只借定义、分母、阈值；本文件正则与词表按定义自写，不搬其脚本。
- 禁用词表不复制，运行时从本 skill 的 references/banned-words.md 解析（路径见 BANNED_WORDS）。
- 计数口径复用本 skill 的 voice_profile.py（chinese_count / SENTENCE_SPLIT / CLAUSE_SPLIT / MARKERS）。
- 路径默认取本文件所在的 story-deslop 目录；STORY_DESLOP_ROOT／STORY_DESLOP_SCRIPTS 可改指。
"""
from __future__ import annotations

import os
import re
import statistics
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

sys.dont_write_bytecode = True
DESLOP_ROOT = Path(os.environ.get("STORY_DESLOP_ROOT", str(Path(__file__).resolve().parents[2]))).expanduser()
DESLOP_SCRIPTS = Path(os.environ.get("STORY_DESLOP_SCRIPTS", str(DESLOP_ROOT / "scripts"))).expanduser()
BANNED_WORDS = DESLOP_ROOT / "references" / "banned-words.md"
sys.path.insert(0, str(DESLOP_SCRIPTS))
import voice_profile as VP  # noqa: E402

chinese_count = VP.chinese_count
SENTENCE_SPLIT = VP.SENTENCE_SPLIT
CLAUSE_SPLIT = VP.CLAUSE_SPLIT

Hit = dict[str, Any]
H = r"㐀-鿿"
PUNCT_STOP = "，,。！？!?；;：:、“”‘’「」『』\"（）()…—"
NOT_STOP = f"[^{PUNCT_STOP}\n]"          # 分句内字符
NOT_SENT_END = r"[^。！？!?\n]"            # 句内字符（可跨逗号）
OPEN_Q, CLOSE_Q = "“「『", "”」』"


# ───────────────────────── 基础切分 ─────────────────────────
def han(s: str) -> int:
    return chinese_count(s)


def quote_spans(p: str) -> list[tuple[int, int]]:
    """引号区间（含引号本身）。未闭合的引号视为到段尾；英文直引号按奇偶配对。"""
    spans: list[tuple[int, int]] = []
    depth, start, straight = 0, 0, None
    for i, ch in enumerate(p):
        if ch in OPEN_Q:
            if depth == 0:
                start = i
            depth += 1
        elif ch in CLOSE_Q and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append((start, i + 1))
        elif ch == '"' and depth == 0:
            if straight is None:
                straight = i
            else:
                spans.append((straight, i + 1))
                straight = None
    if depth > 0:
        spans.append((start, len(p)))
    if straight is not None:
        spans.append((straight, len(p)))
    return spans


def in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def outside_text(p: str, spans: list[tuple[int, int]]) -> str:
    out, last = [], 0
    for a, b in spans:
        out.append(p[last:a])
        last = b
    out.append(p[last:])
    return " ".join(out)


def sentence_spans(p: str) -> list[tuple[int, int]]:
    spans, pos = [], 0
    for piece in SENTENCE_SPLIT.split(p):
        if piece:
            spans.append((pos, pos + len(piece)))
            pos += len(piece)
    return spans or [(0, len(p))]


def sent_index(pos: int, sspans: list[tuple[int, int]]) -> int:
    for i, (a, b) in enumerate(sspans):
        if a <= pos < b:
            return i
    return len(sspans) - 1


class Para:
    """一段的缓存视图。"""
    __slots__ = ("idx", "text", "q", "s")

    def __init__(self, idx: int, text: str):
        self.idx, self.text = idx, text
        self.q = quote_spans(text)
        self.s = sentence_spans(text)

    def sentence(self, i: int) -> str:
        a, b = self.s[i]
        return self.text[a:b]

    @property
    def is_dialogue(self) -> bool:
        return bool(self.q) and any(han(self.text[a:b]) >= 2 for a, b in self.q)

    @property
    def is_pure_dialogue(self) -> bool:
        return self.is_dialogue and han(outside_text(self.text, self.q)) == 0


def views(paras: list[str]) -> list[Para]:
    return [Para(i, t) for i, t in enumerate(paras)]


def mk(rule: str, pv: Para, start: int, end: int, **extra: Any) -> Hit:
    si = sent_index(start, pv.s)
    h = {"rule": rule, "para": pv.idx, "sent": si, "sentence": pv.sentence(si).strip(),
         "span": [start, end], "text": pv.text[start:end], "in_dialogue": in_spans(start, pv.q)}
    h.update(extra)
    return h


def metric(rule: str, value: float, num: float, den: float, **extra: Any) -> list[Hit]:
    h = {"rule": rule, "kind": "metric", "value": round(float(value), 6), "num": num, "den": den}
    h.update(extra)
    return [h]


def regex_detector(rule: str, pattern: str | re.Pattern, *, narration_only: bool = False,
                   check: Callable[[re.Match, Para], bool] | None = None, group: int = 0) -> Callable[[list[str]], list[Hit]]:
    rx = re.compile(pattern) if isinstance(pattern, str) else pattern

    def detect(paras: list[str]) -> list[Hit]:
        out = []
        for pv in views(paras):
            for m in rx.finditer(pv.text):
                if narration_only and in_spans(m.start(group), pv.q):
                    continue
                if check and not check(m, pv):
                    continue
                out.append(mk(rule, pv, m.start(group), m.end(group)))
        return out
    detect.__name__ = f"detect_{rule}"
    return detect


def chapter_sentences(paras: list[str]) -> list[tuple[Para, int, str, bool]]:
    """整章句序列：(段视图, 段内句号, 句文本, 是否整句在引号内)。"""
    seq = []
    for pv in views(paras):
        for i, (a, b) in enumerate(pv.s):
            t = pv.text[a:b]
            if han(t) == 0:
                continue
            core = t.strip().strip("”」』")
            qa = in_spans(a + (len(t) - len(t.lstrip())), pv.q)
            seq.append((pv, i, core, qa))
    return seq


def han_lengths_sent(paras: list[str]) -> list[int]:
    return [han(s) for s in VP.split_sentences(paras) if han(s) > 0]


def han_lengths_para(paras: list[str]) -> list[int]:
    return [han(p) for p in paras if han(p) > 0]


# ───────────────────────── L：lieflat 26 项 ─────────────────────────
# L01 翻案腔：先立误解再推翻。按 lieflat 定义列举的写法自写正则。
# 「不是A，(而)是B」的 A 段最多再含一个逗号（容「不是A，不是B，是C」），防跨多分句误配。
_X1 = r"[^。！？!?\n，,；;]{1,20}(?:[，,][^。！？!?\n，,；;]{1,20})?"
_L01 = "|".join([
    rf"(?<![是要])不是(?!吗){_X1}[，,；;]\s*(?:而)?是",
    rf"并非{NOT_SENT_END}{{1,30}}?[，,；;]\s*而是",
    rf"不在于{NOT_SENT_END}{{1,30}}?[，,；;]\s*而在于",
    rf"与其说{NOT_SENT_END}{{1,30}}?[，,；;]\s*(?:倒)?不如说",
    rf"看似{NOT_SENT_END}{{1,30}}?[，,；;]\s*(?:实则|其实|实际上)",
    rf"表面(?:上)?{NOT_SENT_END}{{1,30}}?[，,；;]\s*(?:实际上|实则|其实|背地里)",
    rf"你以为{NOT_SENT_END}{{1,30}}?[，,；;]\s*(?:其实|实际上)",
    r"说到底", r"恰恰相反",
    rf"{NOT_STOP}{{1,12}}不重要[，,；;。]\s*重要的是",
    rf"(?<![是要])不是{NOT_SENT_END}{{1,30}}[。]\s*而是",
])
detect_L01 = regex_detector("L01", _L01)


def detect_L02(paras: list[str]) -> list[Hit]:
    """L02 顿号罗列过密：一个分句内 ≥2 个顿号（三项以上并列）。"""
    out = []
    for pv in views(paras):
        pos = 0
        for clause in re.split(r"([，,。！？!?；;：:\n])", pv.text):
            if clause and clause.count("、") >= 2 and han(clause) > 0:
                out.append(mk("L02", pv, pos, pos + len(clause), items=clause.count("、") + 1))
            pos += len(clause)
    return out


def _clause_profile(s: str) -> list[int]:
    return [han(c) for c in re.split(r"[，,；;：:]", s) if han(c) > 0]


def _isomorphic(a: str, b: str) -> bool:
    pa, pb = _clause_profile(a), _clause_profile(b)
    if len(pa) < 2 or len(pa) != len(pb):           # 至少一个逗号，且逗号数相同
        return False
    la, lb = sum(pa), sum(pb)
    if min(la, lb) < 6 or min(la, lb) / max(la, lb) < 0.8:
        return False
    return all(abs(x - y) <= max(2, 0.25 * max(x, y)) for x, y in zip(pa, pb))


def _iso_runs(paras: list[str]) -> list[tuple[list, int]]:
    seq = [x for x in chapter_sentences(paras) if not x[3]]   # 只看叙述句
    runs, i = [], 0
    while i < len(seq) - 1:
        j = i
        while j + 1 < len(seq) and _isomorphic(seq[j][2], seq[j + 1][2]):
            j += 1
        if j > i:
            runs.append((seq[i:j + 1], j - i + 1))
            i = j
        else:
            i += 1
    return runs


def detect_L03(paras: list[str]) -> list[Hit]:
    """L03 相邻句结构同款（连续两句）：逗号数相同、各分句长度接近、总长比 ≥0.8。跨段按章内叙述句序列判。"""
    out = []
    for run, n in _iso_runs(paras):
        for k in range(1, n):
            pv, si, core, _ = run[k]
            a, b = pv.s[si]
            out.append(mk("L03", pv, a, b, prev=run[k - 1][2]))
    return out


def detect_L03b(paras: list[str]) -> list[Hit]:
    """L03b 相邻句结构同款（连续三句）：每个长度 ≥3 的同构串记一处（三句窗口计数）。"""
    out = []
    for run, n in _iso_runs(paras):
        for k in range(2, n):
            pv, si, core, _ = run[k]
            a, b = pv.s[si]
            out.append(mk("L03b", pv, a, b, run_len=n))
    return out


detect_L04 = regex_detector("L04", r"—+|－{2,}")

_L05A = (r"(?:一句话(?:总结|概括|说)?|总结(?:一下)?|总而言之|简单(?:来)?说|核心(?:是|在于|就是)?|关键(?:是|在于|就是)"
         r"|原因(?:如下|很简单|只有一个|有[两三几]个)?|结论(?:是|就是)?|本质上(?:是)?|换句话说|说穿了|说白了"
         r"|问题(?:是|在于|就在于)|答案(?:是|很简单)|重点(?:是|在于))[：:]")
detect_L05a = regex_detector("L05a", _L05A)

_LIST_HEAD = re.compile(r"^\s*(?:\d+[\.、．)）]|[一二三四五六七八九十]+[、.．]|[（(][\d一二三四五六七八九十]+[)）]|[-•·*]\s)")


def detect_L05b(paras: list[str]) -> list[Hit]:
    """L05b 空转句引列表：叙述段以冒号收尾（≤30 汉字），下一段是列表项。"""
    out = []
    vs = views(paras)
    for pv, nxt in zip(vs, vs[1:]):
        t = pv.text.rstrip()
        if t.endswith(("：", ":")) and not in_spans(len(t) - 1, pv.q) and han(t) <= 30 and _LIST_HEAD.match(nxt.text):
            out.append(mk("L05b", pv, 0, len(t)))
    return out


_HEADING = re.compile(r"^\s*(?:#{1,6}\s*|\*\*)")
_ORD_HEAD = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:[一二三四五六七八九十]+、|第[一二三四五六七八九十]+[、，,：:\s])")


def detect_L06(paras: list[str]) -> list[Hit]:
    """L06 序数词当小标题：标题行（# 或整行加粗）以一、二、/第一、编号，全章 ≥3 个才算。"""
    vs = views(paras)
    heads = [pv for pv in vs if _ORD_HEAD.match(pv.text) and
             (_HEADING.match(pv.text) or (han(pv.text) <= 20 and not re.search(r"[。！？!?，,]", pv.text)))]
    if len(heads) < 3:
        return []
    return [mk("L06", pv, 0, len(pv.text)) for pv in heads]


_ROLE = ("导师|老师|秘书|助手|助理|顾问|管家|审查员|实习生|守护者|守门人|向导|保姆|哨兵|卫士|伙伴|朋友|医生|法官|裁判"
         "|指挥官|将军|工匠|园丁|猎人|老友|长者|智者|仆人|侍从|士兵|战士")
_PRAISE = re.compile(r"永不|不知疲倦|永远|智慧|全能|忠诚|忠实|耐心|温柔|贴心|可靠|尽职|称职|无所不知|无所不能|最好的|慈祥|睿智|沉默")
_L07 = re.compile(rf"(?:像|好像|如同|宛如|犹如|仿佛|相当于)(?:是)?(?:一个|一位|一名|个|位|名)({NOT_STOP}{{0,10}}?)(?:{_ROLE})")


def _l07_check(m: re.Match, pv: Para) -> bool:
    tail = pv.text[m.end(): m.end() + 24]
    return bool(_PRAISE.search(m.group(1))) or bool(re.match(rf"{NOT_SENT_END}{{0,20}}?(?:不仅|不只|不光)", tail))


detect_L07 = regex_detector("L07", _L07, check=_l07_check)

_UNIT = ("个|只|名|位|人|次|回|年|月|日|号|天|周|星期|小时|分钟|分|秒|点|米|公里|里|斤|公斤|元|块|毛|层|楼|岁|章|页|件|张|条"
         "|间|座|辆|台|部|本|颗|枚|根|把|道|级|倍|成|%|％|万|亿|千|百")
_L08 = re.compile(rf"\d+(?:[\.,:：]\d+)*%?|(?:[零〇二三四五六七八九十百千万亿]|[一两][零〇一二三四五六七八九十百千万亿]+)[零〇一二两三四五六七八九十百千万亿]*(?:{_UNIT})")
_L08_ADVERB = {"十分", "万分", "千万", "百分", "万万", "千百"}
detect_L08 = regex_detector("L08", _L08, check=lambda m, pv: m.group(0) not in _L08_ADVERB)
detect_L09 = regex_detector("L09", r"说白了|说穿了|先说结论")

_DET = r"(?:一个|一种|一套|一位|一名|一股|一道|这个|那个|这种|那种|这样一个|这么一个|这样一种)"
_L10A_LONG = re.compile(rf"{_DET}({NOT_STOP}{{15,40}})的([{H}]{{1,4}})")
_DE_NOT_ATTR = "目似有端别真是挺好怪"   # 的 前是这些字时多半不是定语标记（真的/是…的/目的/似的…）
_L10A_CHAIN = re.compile(rf"([{H}]{{2,6}})(?<![{_DE_NOT_ATTR}])的(?!确)([{H}]{{2,4}})(?<![{_DE_NOT_ATTR}])的(?!确)[{H}]")


def detect_L10a(paras: list[str]) -> list[Hit]:
    """L10a 过长前置定语：限定词后修饰成分 ≥15 汉字再接中心语；或一个分句内「X的Y的Z」连用。"""
    out = []
    for pv in views(paras):
        for m in _L10A_LONG.finditer(pv.text):
            if han(m.group(1)) >= 15:
                out.append(mk("L10a", pv, m.start(), m.end(), form="long_premodifier"))
        for m in _L10A_CHAIN.finditer(pv.text):
            out.append(mk("L10a", pv, m.start(), m.end(), form="de_chain"))
    return out


_DANG_EXCL = "然初年天时即场面中作成真着下晚日地心兵家街众选铺值事局前今代做是"
detect_L10b = regex_detector(
    "L10b", rf"(?:^|(?<=[，,。！？!?；;：:“”「」\s]))当(?![{_DANG_EXCL}]){NOT_STOP}{{2,24}}?时[，,]")
detect_L10c = regex_detector(
    "L10c", rf"(?:^|(?<=[，,。！？!?；;“”「」]))\s*(?:对于{NOT_STOP}{{1,15}}(?:来说|而言)|对{NOT_STOP}{{1,15}}(?:来说|而言)"
    rf"|就{NOT_STOP}{{1,15}}而言|关于{NOT_STOP}{{1,15}}[，,]|在{NOT_STOP}{{1,12}}方面)")
_SENT_START = r"(?:^|(?<=[。！？!?“「]))\s*"
detect_L10d = regex_detector("L10d", _SENT_START + r"(?:然而|因此|此外|与此同时|换言之|总而言之)[，,]")
detect_L10e = regex_detector("L10e", _SENT_START + r"(?:这意味着|这表明|这说明|换句话说)")

_L11_OPEN = re.compile(r"^\s*(?:听起来|看起来|看上去|说白了|值得注意的是|值得一提的是|更重要的是|关键在于|问题在于|意味着|不难看出|显然|可见|毫无疑问|说到底)")
_ANAPHOR = re.compile(r"[这那其此]|上述|上面")


def detect_L11(paras: list[str]) -> list[Hit]:
    """L11 段首零主语评论：非首段以评论语开头，且首句无回指成分（这/那/其/此/上述/上面）。对话段不计。"""
    out, first = [], True
    for pv in views(paras):
        if han(pv.text) == 0:
            continue
        if first:
            first = False
            continue
        m = _L11_OPEN.match(pv.text)
        if not m or pv.text.lstrip().startswith(tuple(OPEN_Q + '"')):
            continue
        a, b = pv.s[0]
        if not _ANAPHOR.search(pv.text[m.end():b]):
            out.append(mk("L11", pv, m.start(), m.end()))
    return out


def detect_L12(paras: list[str]) -> list[Hit]:
    """L12 句长均匀度：章内句长（汉字）变异系数 CV。"""
    ls = han_lengths_sent(paras)
    if len(ls) < 2:
        return metric("L12", 0.0, 0, len(ls))
    mu = statistics.fmean(ls)
    return metric("L12", statistics.pstdev(ls) / mu if mu else 0.0, statistics.pstdev(ls), mu, n=len(ls))


def detect_L13(paras: list[str]) -> list[Hit]:
    """L13 相邻句长差：相邻两句汉字长度差的绝对值均值。"""
    ls = han_lengths_sent(paras)
    if len(ls) < 2:
        return metric("L13", 0.0, 0, 0)
    d = [abs(a - b) for a, b in zip(ls, ls[1:])]
    return metric("L13", statistics.fmean(d), sum(d), len(d))


def detect_L14(paras: list[str]) -> list[Hit]:
    """L14 段落长度均匀度（稳健离散度）：段长 MAD / 中位数；extra 附 CV。"""
    ls = han_lengths_para(paras)
    if len(ls) < 2:
        return metric("L14", 0.0, 0, len(ls))
    med = statistics.median(ls)
    mad = statistics.median(abs(x - med) for x in ls)
    mu = statistics.fmean(ls)
    return metric("L14", mad / med if med else 0.0, mad, med, cv=round(statistics.pstdev(ls) / mu, 6) if mu else 0.0)


detect_L15 = regex_detector("L15", r"[就很了]")
detect_L16 = regex_detector("L16", r"但是|其实|不过|就是")

_L17_SKIP = re.compile(r"^(?:[他她它我你这那]|但是|然后|不过|可是|如果|因为|所以|于是|就是|还是|已经|没有|不是|一个|只是|而且|随后|接着|同时|此时|现在|突然)")


def detect_L17(paras: list[str]) -> list[Hit]:
    """L17 反复写全称（少用代词）：相邻叙述句以同一个两字名词性开头（排除代词与虚词开头）。"""
    out = []
    seq = [x for x in chapter_sentences(paras) if not x[3]]
    for (p1, i1, s1, _), (p2, i2, s2, _) in zip(seq, seq[1:]):
        h1 = re.sub(rf"[^{H}]", "", s1)[:2]
        h2 = re.sub(rf"[^{H}]", "", s2)[:2]
        if len(h1) == 2 and h1 == h2 and not _L17_SKIP.match(h1):
            a, b = p2.s[i2]
            out.append(mk("L17", p2, a, b, head=h1))
    return out


detect_L18 = regex_detector("L18", r"被(?:认为|视为|看作|看成|称为|誉为|定义为|理解为|描述为|评为|当作|当成)")

_ANSWER_OPEN = re.compile(r"^\s*(?:答案|因为|很简单|当然|原因|其实|自然是|是的|是|不是|没有|只有|就是|显然)")


def detect_L19(paras: list[str]) -> list[Hit]:
    """L19 设问自答（叙述层）：引号外以问号收尾的句子，紧跟一个以答语开头的叙述句。"""
    out = []
    seq = chapter_sentences(paras)
    for (p1, i1, s1, q1), (p2, i2, s2, q2) in zip(seq, seq[1:]):
        if q1 or q2 or not s1.rstrip().endswith(("？", "?")):
            continue
        if _ANSWER_OPEN.match(s2):
            a, b = p1.s[i1]
            out.append(mk("L19", p1, a, b, answer=s2))
    return out


def detect_L20(paras: list[str]) -> list[Hit]:
    """L20 问句小标题：# 标题或整行加粗标题以问号收尾。"""
    return [mk("L20", pv, 0, len(pv.text)) for pv in views(paras)
            if _HEADING.match(pv.text) and re.search(r"[？?]\**\s*$", pv.text)]


detect_L21 = regex_detector("L21", r"[？?]", narration_only=True)


def detect_L22(paras: list[str]) -> list[Hit]:
    """L22 句内同构排比：同一句内相邻两个分句（3–13 汉字）以同一汉字开头。"""
    out = []
    for pv in views(paras):
        for si, (a, b) in enumerate(pv.s):
            pos, prev = a, None
            for part in re.split(r"([，,、；;])", pv.text[a:b]):
                if part and part not in "，,、；;":
                    c = part.strip("“”「」 ")
                    if prev and 3 <= han(c) <= 13 and 3 <= han(prev[0]) <= 13 and c[:1] == prev[0][:1] and re.match(rf"[{H}]", c):
                        out.append(mk("L22", pv, prev[1], pos + len(part)))
                    prev = (c, pos)
                pos += len(part)
    return out


_SIMILE = r"仿佛|如同|宛如|宛若|犹如|好似|恍若|(?<!类)似的|(?<!好)像是|像(?:一|个|只|条|头|块|座|把|根|片|团|颗|道|朵|张|股|阵|被)"
detect_L23 = regex_detector("L23", _SIMILE)


def detect_L23b(paras: list[str]) -> list[Hit]:
    """L23b 比喻起段：叙述段的第一个分句里有比喻标记。"""
    out = []
    rx = re.compile(_SIMILE)
    for pv in views(paras):
        if han(pv.text) == 0 or pv.text.lstrip().startswith(tuple(OPEN_Q + '"')):
            continue
        first = re.split(r"[，,。！？!?；;]", pv.text, maxsplit=1)[0]
        m = rx.search(first)
        if m:
            out.append(mk("L23b", pv, m.start(), m.end()))
    return out


detect_L24 = regex_detector(
    "L24", r"(?:^|(?<=[，,。！？!?；;“”\s]))(?:时间|岁月|沉默|恐惧|焦虑|孤独|悲伤|愤怒|绝望|希望|记忆|回忆|命运|疲惫|不安|寂静|夜色|黑暗)"
           r"(?:像|在|正|也|已经|终于|慢慢|悄悄)?(?:保管|收藏|收拾|攥住|攥|咬住|咬|吞没|吞噬|吞|啃噬|啃|爬上|爬|钻进|钻|压住|压|缠住|缠"
           r"|抓住|抓|掐住|掐|敲|推|拽|踩|舔|漫上|漫|淹没|显出|长出|勒住|勒)")
detect_L25 = regex_detector(
    "L25", rf"(?:完成|实现|进行|开展|作出|做出|加以|予以)了?(?:对)?{NOT_STOP}{{0,10}}?的?"
           r"(?:优化|提升|调整|分析|改造|升级|处理|检查|研究|评估|改进|确认|部署|整顿|清理|梳理|讨论|审查|测试)")
detect_L26 = regex_detector("L26", _SENT_START + r"(?:首先|其次|再次|最后|第一|第二|第三|一方面|另一方面)[，,、]")


# ───────────────────────── D：story-deslop 门禁 A–G ─────────────────────────
@lru_cache(maxsize=1)
def banned_lexicon() -> dict[str, list[str]]:
    """运行时解析 banned-words.md：一级各类、二级语境敏感／弱化副词、书面腔表左列。不在本仓留副本。"""
    text = BANNED_WORDS.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    level = None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            level = line[3:].strip()
            continue
        if not line.startswith("### ") or level is None:
            continue
        name = line[4:].strip()
        body = []
        for nxt in lines[i + 1:]:
            if nxt.startswith("#"):
                break
            if nxt.strip():
                body.append(nxt.strip())
        if level.startswith("一级"):
            words = re.sub(r"[（(].*", "", body[0]) if body else ""
            out["一级/" + name] = [w.strip() for w in words.split("、") if w.strip()]
        elif level.startswith("二级"):
            if name.startswith("语境敏感词") or name.startswith("弱化副词"):
                words = re.sub(r"[（(].*", "", body[0]) if body else ""
                out["二级/" + name.split("（")[0]] = [w.strip() for w in words.split("、") if w.strip()]
            elif name.startswith("书面腔"):
                rows = [r for r in body if r.startswith("|") and not set(r) <= set("|-: ")]
                out["二级/书面腔"] = [r.split("|")[1].strip() for r in rows[1:]]
    return out


BANNED_CATEGORY = {"D01": "一级/情态类", "D02": "一级/动作类", "D03": "一级/表情类", "D04": "一级/心理类",
                   "D05": "一级/判断类", "D06": "一级/形容类", "D07": "一级/过渡类",
                   "D08": "二级/语境敏感词", "D09": "二级/弱化副词", "D10": "二级/书面腔"}


def _banned_detector(rule: str) -> Callable[[list[str]], list[Hit]]:
    def detect(paras: list[str]) -> list[Hit]:
        words = banned_lexicon().get(BANNED_CATEGORY[rule], [])
        if not words:
            raise RuntimeError(f"{rule}: banned-words.md 里没解析到 {BANNED_CATEGORY[rule]}")
        rx = re.compile("|".join(sorted(map(re.escape, words), key=len, reverse=True)))
        out = []
        for pv in views(paras):
            for m in rx.finditer(pv.text):
                out.append(mk(rule, pv, m.start(), m.end(), word=m.group(0)))
        return out
    detect.__name__ = f"detect_{rule}"
    return detect


detect_D01, detect_D02, detect_D03, detect_D04, detect_D05, detect_D06, detect_D07, detect_D08, detect_D09, detect_D10 = (
    _banned_detector(r) for r in ("D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10"))

detect_D11 = regex_detector(
    "D11", rf"取而代之的是|淬(?:了|着){NOT_STOP}{{0,4}}?(?:毒|冰|火|寒|光|霜)|显得(?:有些|有点|格外|十分|很|非常|更加|愈发)[{H}]{{1,4}}"
           rf"|心(?:里|底|中|头)(?:某个|某处|的某个)(?:地方|角落)?{NOT_STOP}{{0,6}}软|散发(?:着|出)(?:一股|一种|一阵){NOT_STOP}{{0,12}}?(?:气息|气场|气势)")
detect_D12 = regex_detector("D12", rf"(?<![是要])不是(?!吗){_X1}[，,]\s*(?:而)?是")


def detect_D13(paras: list[str]) -> list[Hit]:
    """D13 跨句/跨段否定排列：「不是A。」后接「也不是B。」（可多句），再接「只是/而是/是/就是C」。"""
    out = []
    seq = chapter_sentences(paras)
    starts = [re.sub(r"^[“「\"\s]+", "", s) for _, _, s, _ in seq]
    for i in range(len(seq) - 2):
        if not starts[i].startswith("不是") or not starts[i + 1].startswith("也不是"):
            continue
        j = i + 1
        while j + 1 < len(seq) and starts[j + 1].startswith("也不是"):
            j += 1
        if j + 1 < len(seq) and re.match(r"(?:只是|而是|就是|仅仅是|是)", starts[j + 1]):
            pv, si, _, _ = seq[j + 1]
            a, b = pv.s[si]
            out.append(mk("D13", pv, a, b, run_len=j + 2 - i))
    return out


detect_D14 = regex_detector("D14", r"[，,]\s*带着")
detect_D15 = regex_detector(
    "D15", rf"声音(?:不大|很轻|很低|不高|压得很低){NOT_SENT_END}{{0,6}}?[，,]\s*(?:却|但)(?:带着|透着|有着)|语气(?:毫无|没有|不带)(?:波澜|起伏)"
           r"|平静无波|(?:声音|语气|声线)(?:平直|平平|平板)|听不出(?:任何|什么|半点|一丝)?(?:情绪|喜怒|起伏|波澜)"
           r"|(?:声音|语气)(?:里|中)?(?:带着|透着)(?:一丝|一股|几分|不容)")
detect_D16 = regex_detector("D16", r"(?:他|她)(?:们)?(?:心里|心中)?(?:知道|感到|感觉到|觉得|意识到|明白|察觉到|清楚地知道)")

_TAG = re.compile(r"说|道|问|答|喊|叫|嚷|吼|骂|叹|开口|补充|解释|嘀咕|低语|喃喃|回应")


def detect_D17(paras: list[str]) -> list[Hit]:
    """D17 对话标签密度：含台词的段落里，引号外出现言说动词（说/道/问/答/喊…）。命中＝带标签的对话段。"""
    out = []
    for pv in views(paras):
        if not pv.is_dialogue:
            continue
        m = _TAG.search(outside_text(pv.text, pv.q))
        if m:
            out.append({"rule": "D17", "para": pv.idx, "sent": 0, "sentence": pv.sentence(0).strip(),
                        "span": [0, len(pv.text)], "text": pv.text, "tag": m.group(0), "in_dialogue": False})
    return out


detect_D18 = regex_detector(
    "D18", r"(?:冷冷|淡淡|缓缓|沉声|轻声|低声|柔声|厉声|冷声|淡声|温声|笑|怒|急|喃喃|幽幽|悠悠|朗声)(?:地)?(?:道|说道)(?![理路具歉别谢喜贺德])|说道",
    narration_only=True)

_ELEVATE = re.compile(r"这一刻|那一刻|终于明白|终于知道|这才意识到|才刚刚开始|从这一刻(?:起|开始)|(?:他|她)(?:不)?知道|(?:他|她)明白|这就是|命运|宿命"
                      r"|不知道的是|一切(?:都|才)|未来")


def detect_D19(paras: list[str]) -> list[Hit]:
    """D19 结尾升华句：章末最后 3 个叙述段里的升华／点题标志。命中逐处记，章级命中率另算。"""
    vs = [pv for pv in views(paras) if han(pv.text) > 0 and not pv.is_pure_dialogue]
    out = []
    for pv in vs[-3:]:
        for m in _ELEVATE.finditer(pv.text):
            if not in_spans(m.start(), pv.q):
                out.append(mk("D19", pv, m.start(), m.end()))
    return out


detect_D20 = regex_detector(
    "D20", _SENT_START + r"原来|(?:终于|这才)(?:明白|意识到|知道|懂了|看清)|从这一刻(?:起|开始)|此刻[，,]|一切(?:都|皆)", narration_only=True)
detect_D21 = regex_detector(
    "D21", rf"之所以{NOT_SENT_END}{{0,30}}?是因为|这意味着|也就是说|换句话说|正是因为|由此可见|不难看出|事实上|综上所述|换言之", narration_only=True)
detect_D22 = regex_detector(
    "D22", rf"不知道的是|殊不知|多年(?:以|之)后|冥冥之中|(?:仿佛|似乎)预示着|谁也没(?:有)?想到|谁也不会想到|这一切{NOT_STOP}{{0,8}}?(?:才刚刚|只是)开始",
    narration_only=True)


def detect_D23(paras: list[str]) -> list[Hit]:
    """D23 段落均匀带占比：段长落在章内中位数 ±25% 区间的段占比（%）。"""
    ls = han_lengths_para(paras)
    if not ls:
        return metric("D23", 0.0, 0, 0)
    med = statistics.median(ls)
    k = sum(1 for x in ls if 0.75 * med <= x <= 1.25 * med)
    return metric("D23", k * 100 / len(ls), k, len(ls))


def _bucket(n: int) -> str:
    return "s" if n <= 8 else ("m" if n <= 34 else "l")


def detect_D24(paras: list[str]) -> list[Hit]:
    """D24 句长同档长串：连续 ≥5 句落在同一长度档（短 ≤8／中 9–34／长 ≥35），每串记一处（标在串尾句）。"""
    seq = chapter_sentences(paras)
    out, i = [], 0
    while i < len(seq):
        j = i
        while j + 1 < len(seq) and _bucket(han(seq[j + 1][2])) == _bucket(han(seq[i][2])):
            j += 1
        if j - i + 1 >= 5:
            pv, si, _, _ = seq[j]
            a, b = pv.s[si]
            out.append(mk("D24", pv, a, b, run_len=j - i + 1, bucket=_bucket(han(seq[i][2]))))
        i = j + 1
    return out


def detect_D25(paras: list[str]) -> list[Hit]:
    """D25 连续排比：连续 ≥3 句（或同句 ≥3 分句）以同样两个汉字开头；或「有的…有的…有的」「一边…一边…一边」。"""
    out = []
    seq = chapter_sentences(paras)
    heads = [re.sub(rf"[^{H}]", "", s)[:2] for _, _, s, _ in seq]
    i = 0
    while i < len(seq):
        j = i
        while j + 1 < len(seq) and len(heads[i]) == 2 and heads[j + 1] == heads[i]:
            j += 1
        if j - i + 1 >= 3:
            pv, si, _, _ = seq[j]
            a, b = pv.s[si]
            out.append(mk("D25", pv, a, b, form="sentences", run_len=j - i + 1, head=heads[i]))
        i = j + 1
    for pv in views(paras):
        for si, (a, b) in enumerate(pv.s):
            clauses = [c for c in re.split(r"[，,、；;]", pv.text[a:b]) if han(c) >= 2]
            hs = [re.sub(rf"[^{H}]", "", c)[:2] for c in clauses]
            k = 0
            while k < len(hs):
                m = k
                while m + 1 < len(hs) and len(hs[k]) == 2 and hs[m + 1] == hs[k]:
                    m += 1
                if m - k + 1 >= 3:
                    out.append(mk("D25", pv, a, b, form="clauses", run_len=m - k + 1, head=hs[k]))
                k = m + 1
        for m in re.finditer(rf"有的{NOT_SENT_END}*?有的{NOT_SENT_END}*?有的|一边{NOT_SENT_END}*?一边{NOT_SENT_END}*?一边", pv.text):
            out.append(mk("D25", pv, m.start(), m.end(), form="marker"))
    return out


detect_D26 = regex_detector("D26", r"仿佛|犹如|宛若|宛如|如同|恍若|好似")
detect_D27 = regex_detector(
    "D27", r"眼中闪过|眼底闪过|嘴角勾起|嘴角微扬|心中涌起|心头涌起|瞳孔(?:微缩|骤缩|一缩|猛地一缩|收缩)|指节泛白|呼吸一滞|心口一沉|心头一紧|心头一震"
           r"|后背发凉|头皮发麻|眉头紧锁|倒吸一口凉气|脸色一变|喉结(?:滚动|上下滚动|滚了滚)|眼眶(?:一热|泛红)|心脏漏跳")


def detect_D28(paras: list[str]) -> list[Hit]:
    """D28 问答教学对：短问句台词段（≤30 汉字、问号收尾）后紧接一段 ≥60 汉字的台词段。"""
    out = []
    vs = views(paras)
    for pv, nxt in zip(vs, vs[1:]):
        if not pv.is_dialogue or not nxt.is_dialogue:
            continue
        q_in = "".join(pv.text[a:b] for a, b in pv.q)
        n_in = "".join(nxt.text[a:b] for a, b in nxt.q)
        if 0 < han(q_in) <= 30 and re.search(r"[？?][”」』\"]?\s*$", q_in) and han(n_in) >= 60:
            out.append(mk("D28", nxt, 0, len(nxt.text), question=q_in))
    return out


detect_D29 = regex_detector(
    "D29", rf"至于{NOT_STOP}{{1,8}}?(?:不|没){NOT_STOP}{{0,8}}[，,]{NOT_STOP}{{0,6}}怎么|不([{H}])[{H}]{{1,6}}[，,]\s*不\1")
detect_D30 = regex_detector(
    "D30", r"(?:他|她)(?:们)?(?:感到|觉得|显得)?(?:很|十分|非常|有些|有点|格外|无比|特别|一阵)"
           r"(?:紧张|害怕|愤怒|伤心|失落|难过|开心|高兴|激动|不安|尴尬|委屈|无奈|焦虑|恐惧|兴奋|感动|震惊|惊讶|疑惑|困惑|欣慰|沮丧|绝望)")


# ───────────────────────── P：prose_metrics 形状指标 ─────────────────────────
def _sent_bucket_counts(paras: list[str]) -> tuple[int, int, int, int]:
    ls = han_lengths_sent(paras)
    return sum(1 for x in ls if x <= 8), sum(1 for x in ls if 9 <= x <= 34), sum(1 for x in ls if x >= 35), len(ls)


def _metric_mean(rule: str, values: list[int], median: bool = False) -> list[Hit]:
    if not values:
        return metric(rule, 0.0, 0, 0)
    v = statistics.median(values) if median else statistics.fmean(values)
    return metric(rule, v, sum(values), len(values))


def detect_P01(paras): return _metric_mean("P01", han_lengths_para(paras))
def detect_P02(paras): return _metric_mean("P02", han_lengths_para(paras), median=True)
def detect_P03(paras): return _metric_mean("P03", han_lengths_sent(paras))
def detect_P04(paras): return _metric_mean("P04", han_lengths_sent(paras), median=True)


def detect_P05(paras):
    return _metric_mean("P05", [han(c) for p in paras for c in CLAUSE_SPLIT.split(p) if han(c) > 0])


def _sentence_hits(rule: str, paras: list[str], pred: Callable[[int], bool]) -> list[Hit]:
    out = []
    for pv, si, s, _ in chapter_sentences(paras):
        if pred(han(s)):
            a, b = pv.s[si]
            out.append(mk(rule, pv, a, b))
    return out


def detect_P06(paras): return _sentence_hits("P06", paras, lambda n: n <= 8)
def detect_P07(paras): return _sentence_hits("P07", paras, lambda n: n >= 35)
def detect_P08(paras): return _sentence_hits("P08", paras, lambda n: 9 <= n <= 34)


def detect_P09(paras):
    return [mk("P09", pv, 0, 1) for pv in views(paras)
            if han(pv.text) > 0 and pv.text.lstrip().startswith(("“", "「", "『", '"'))]


detect_P10 = regex_detector("P10", r"[，,]")
detect_P11 = regex_detector("P11", r"[？?]")
detect_P12 = regex_detector("P12", r"[！!]")


def detect_P13(paras):
    """P13 章内重复句：去标点后 ≥4 字、在本章出现 ≥2 次的句子，每次出现记一处（同 voice_profile.internal_repeat）。"""
    seq = chapter_sentences(paras)
    norm = [VP.normalized_sentence(s) for _, _, s, _ in seq]
    from collections import Counter
    c = Counter(n for n in norm if len(n) >= 4)
    out = []
    for (pv, si, s, _), n in zip(seq, norm):
        if len(n) >= 4 and c[n] >= 2:
            a, b = pv.s[si]
            out.append(mk("P13", pv, a, b))
    return out


detect_P14 = regex_detector("P14", VP.MARKERS["simile_marker_per_1k"])
detect_P15 = regex_detector("P15", VP.MARKERS["explanation_marker_per_1k"])
detect_P16 = regex_detector("P16", VP.MARKERS["body_reaction_marker_per_1k"])
detect_P17 = regex_detector("P17", VP.MARKERS["transition_marker_per_1k"])
detect_P18 = regex_detector("P18", VP.MARKERS["contrast_template_per_1k"])


def detect_P19(paras):
    ls_s, ls_p = han_lengths_sent(paras), han_lengths_para(paras)
    return metric("P19", len(ls_s) / len(ls_p) if ls_p else 0.0, len(ls_s), len(ls_p))


# ───────────────────────── W：逐词候选（P2 拍板后单列） ─────────────────────────
# W01「猛地」：D08 语境敏感词整类淘汰后逐词单列（R 14.9，三族各 9–24 倍，逐词未做区间，下轮复验）。
# 口径同 D08 逐词统计：全文子串计数，含引号内。
detect_W01 = regex_detector("W01", r"猛地")


# ───────────────────────── 注册表 ─────────────────────────
REGISTRY: dict[str, Callable[[list[str]], list[Hit]]] = {
    name[len("detect_"):]: fn for name, fn in sorted(globals().items())
    if name.startswith("detect_") and callable(fn)
}


def run_all(paras: list[str], rules: list[str] | None = None) -> dict[str, list[Hit]]:
    return {rid: REGISTRY[rid](paras) for rid in (rules or REGISTRY)}


def unit_counts(paras: list[str]) -> dict[str, int]:
    return {"han": sum(han(p) for p in paras),
            "paragraphs": sum(1 for p in paras if han(p) > 0),
            "sentences": len(han_lengths_sent(paras))}


if __name__ == "__main__":
    import json
    text = Path(sys.argv[1]).read_text(encoding="utf-8")
    paras = VP.normalized_lines(text)
    res = run_all(paras, sys.argv[2:] or None)
    print(json.dumps({k: v for k, v in res.items() if v}, ensure_ascii=False, indent=1))
