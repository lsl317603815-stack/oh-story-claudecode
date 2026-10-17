"""有声书纯逻辑：正文切分、有声稿渲染与解析、逐字校验、选角解析、停顿规则。

这里不做网络调用，也不写文件，便于测试。网络在 ark_audio.py，命令行在 audiobook.py。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

LINE_MAX = 150          # 一行有声稿文本上限（字）
OPEN_Q, CLOSE_Q = "“", "”"
VARIANTS = ("心声", "电话", "系统", "回忆")
NARRATOR = "旁白"
PASSERBY = "路人"
TITLE_CUE = "章节标题，清晰稳重"

# 变体 → ffmpeg 滤镜（作用在 24 kHz 单声道逐句音频上）
FX_FILTERS = {
    "电话": "highpass=f=320,lowpass=f=3200,acompressor=threshold=0.1:ratio=4",
    "系统": "aecho=0.8:0.6:18|36:0.25|0.15,highpass=f=180",
    "心声": "aecho=0.8:0.5:45:0.18,volume=-1.5dB",
    "回忆": "aecho=0.8:0.6:60:0.22",
}

# 路人池：类别 → 候选音色，按顺序取第一个没被主要角色占用的
PASSERBY_POOLS = {
    "男青年": ["zh_male_yangguangqingnian_uranus_bigtts", "zh_male_qingshuangnanda_uranus_bigtts",
             "zh_male_kailangxuezhang_uranus_bigtts", "zh_male_wenrouxiaoge_uranus_bigtts",
             "zh_male_linjiananhai_uranus_bigtts", "zh_male_huolixiaoge_uranus_bigtts"],
    "女青年": ["zh_female_qinqienv_uranus_bigtts", "zh_female_tiexinnvsheng_uranus_bigtts",
             "zh_female_qingxinnvsheng_uranus_bigtts", "zh_female_wenroushunv_uranus_bigtts",
             "zh_female_kailangjiejie_uranus_bigtts"],
    "男中年": ["zh_male_wennuanahu_uranus_bigtts", "zh_male_baqiqingshu_uranus_bigtts",
             "zh_male_yuanboxiaoshu_uranus_bigtts", "zh_male_cixingjieshuonan_uranus_bigtts"],
    "女中年": ["zh_female_wenroumama_uranus_bigtts", "zh_female_zhixingnv_uranus_bigtts",
             "zh_female_tvbnv_uranus_bigtts"],
    "男老年": ["zh_male_dongfanghaoran_uranus_bigtts", "zh_male_yuanboxiaoshu_uranus_bigtts",
             "zh_male_wennuanahu_uranus_bigtts"],
    "女老年": ["zh_female_popo_uranus_bigtts", "zh_female_wenroumama_uranus_bigtts"],
    "男童": ["zh_male_naiqimengwa_uranus_bigtts", "zh_male_tiancaitongsheng_uranus_bigtts",
            "zh_male_liangsangmengzai_uranus_bigtts"],
    "女童": ["zh_female_mengyatou_uranus_bigtts"],
}
AGE_ALIASES = {"少年": "青年", "青年": "青年", "成年": "青年", "少女": "青年", "中年": "中年",
               "老年": "老年", "老人": "老年", "儿童": "童", "童": "童", "孩子": "童"}


# ---------------------------------------------------------------- 切分

@dataclass
class Segment:
    sid: int            # 全章编号，从 1 起
    para: int           # 段落编号（正文里第几个非空行，从 0 起）
    kind: str           # title | narr | quote
    text: str           # 对白不含包裹它的引号
    prefix: str = ""    # 去掉的开引号
    suffix: str = ""    # 去掉的闭引号

    @property
    def raw(self) -> str:
        return self.prefix + self.text + self.suffix


_SENT = re.compile(r".*?(?:[。！？!?]+|…{2,})[」』）)】]*", re.S)


def split_sentences(text: str) -> List[str]:
    """按句末标点切句，标点留在句尾；拼回去与原文完全相等。"""
    out, pos = [], 0
    for m in _SENT.finditer(text):
        if m.end() > pos:
            out.append(text[pos:m.end()])
            pos = m.end()
    if pos < len(text):
        out.append(text[pos:])
    return out


def chop(text: str, limit: int = LINE_MAX) -> List[str]:
    """超长文本先在句末、再在逗号处切开，仍超长才硬切；拼回去与原文相等。"""
    if len(text) <= limit:
        return [text]
    units: List[str] = []
    for s in split_sentences(text):
        if len(s) <= limit:
            units.append(s)
            continue
        for piece in re.findall(r"[^，、；,;]*[，、；,;]|[^，、；,;]+", s):
            while len(piece) > limit:
                units.append(piece[:limit])
                piece = piece[limit:]
            if piece:
                units.append(piece)
    parts, buf = [], ""
    for u in units:
        if buf and len(buf) + len(u) > limit:
            parts.append(buf)
            buf = ""
        buf += u
    if buf:
        parts.append(buf)
    return parts


def paragraph_texts(source: str) -> List[str]:
    return [l.strip() for l in source.split("\n") if l.strip()]


def segment_chapter(source: str) -> List[Segment]:
    """把一章正文切成编号片段：对白（引号内）一段一片，叙述一句一片，超长再切。

    保证：同一段落的片段按顺序拼回 raw，等于该段原文。
    """
    segs: List[Segment] = []
    in_quote = False
    for para, p in enumerate(paragraph_texts(source)):
        if re.match(r"^#{1,6}\s", p):
            segs.append(Segment(len(segs) + 1, para, "title", re.sub(r"^#{1,6}\s+", "", p)))
            in_quote = False
            continue
        for kind, text, pre, suf in _runs(p, in_quote):
            if kind == "quote":
                pieces = chop(text) if text else [""]
                for i, piece in enumerate(pieces):
                    segs.append(Segment(len(segs) + 1, para, "quote", piece,
                                        pre if i == 0 else "", suf if i == len(pieces) - 1 else ""))
            else:
                # 【系统提示】之类的方括号块单独成片，便于标成「·系统」
                for block in re.split(r"(【[^【】]*】)", text):
                    for s in ([block] if block.startswith("【") else split_sentences(block)):
                        for piece in chop(s):
                            segs.append(Segment(len(segs) + 1, para, "narr", piece))
        in_quote = _ends_open(p, in_quote)
    return segs


def _runs(p: str, in_quote: bool):
    """段落 → 交替的 (kind, text, prefix, suffix)。跨段对白在新段开头常重复开引号。"""
    out = []
    kind = "quote" if in_quote else "narr"
    buf, pre = "", ""
    for ch in p:
        if ch == OPEN_Q and kind == "narr":
            if buf:
                out.append(("narr", buf, "", ""))
            buf, pre, kind = "", OPEN_Q, "quote"
        elif ch == OPEN_Q and kind == "quote" and not buf and not pre:
            pre = OPEN_Q
        elif ch == CLOSE_Q and kind == "quote":
            out.append(("quote", buf, pre, CLOSE_Q))
            buf, pre, kind = "", "", "narr"
        else:
            buf += ch
    if buf or pre:
        out.append((kind, buf, pre, ""))
    return out


def _ends_open(p: str, in_quote: bool) -> bool:
    state = in_quote
    for ch in p:
        if ch == OPEN_Q:
            state = True
        elif ch == CLOSE_Q:
            state = False
    return state


def format_segments(segs: Iterable[Segment]) -> str:
    """给标注模型看的片段清单：段落之间空一行。"""
    out, prev = [], None
    for s in segs:
        if prev is not None and s.para != prev:
            out.append("")
        tag = {"title": "标题", "narr": "叙述", "quote": "对白"}[s.kind]
        out.append(f"S{s.sid} [{tag}] {s.text if s.kind == 'quote' else s.raw}")
        prev = s.para
    return "\n".join(out)


# ---------------------------------------------------------------- 有声稿

@dataclass
class Line:
    role: str
    cue: str
    text: str
    para: int
    doubt: bool = False
    scene: int = 0          # 0 = 章节标题，场景从 1 起
    no: int = 0             # 有声稿行号（只数台词行，从 1 起）


@dataclass
class Script:
    meta: Dict[str, str] = field(default_factory=dict)
    scenes: Dict[int, Tuple[str, str]] = field(default_factory=dict)   # 场号 → (场名, 氛围)
    lines: List[Line] = field(default_factory=list)


@dataclass
class Label:
    role: str
    cue: str = ""
    doubt: bool = False


def build_lines(segs: Sequence[Segment], labels: Dict[int, Label],
                scene_starts: Dict[int, Tuple[str, str]]) -> Script:
    """片段 + 标注 → 有声稿。

    - 同一段落里相邻、同角色、同指示的片段合并成一行（不超过 LINE_MAX）。
    - 旁白的空指示沿用上一句旁白的指示。
    - 旁白读到的引号原样保留；角色台词去掉包裹引号。
    - scene_starts：片段编号 → (场名, 氛围)，该片段所在段落开一个新场。缺标注的对白记为存疑旁白。
    """
    sc = Script()
    starts = {}
    for sid, info in scene_starts.items():
        seg = next((s for s in segs if s.sid == sid), None)
        if seg is not None and seg.kind != "title":
            starts.setdefault(seg.para, info)
    para_scene: Dict[int, int] = {}
    scene = 0
    for seg in segs:
        if seg.para in para_scene:
            continue
        if seg.kind == "title":
            para_scene[seg.para] = 0
            continue
        if seg.para in starts or scene == 0:
            scene += 1
            sc.scenes[scene] = starts.get(seg.para, ("", ""))
        para_scene[seg.para] = scene

    last_narr_cue = ""
    cur: Optional[Line] = None
    for seg in segs:
        lab = labels.get(seg.sid) or Label(NARRATOR, "", seg.kind == "quote")
        role = lab.role.strip() or NARRATOR
        cue = lab.cue.strip()
        if seg.kind == "title":
            role, cue = NARRATOR, cue or TITLE_CUE
        elif role == NARRATOR:
            cue = cue or last_narr_cue
            last_narr_cue = cue
        text = seg.raw if role == NARRATOR else seg.text
        if (cur is not None and seg.kind != "title" and cur.para == seg.para and cur.role == role
                and cur.cue == cue and cur.doubt == lab.doubt and len(cur.text) + len(text) <= LINE_MAX):
            cur.text += text
            continue
        cur = Line(role, cue, text, seg.para, lab.doubt, para_scene[seg.para])
        sc.lines.append(cur)
    renumber(sc)
    return sc


def renumber(sc: Script) -> None:
    for i, ln in enumerate(sc.lines, 1):
        ln.no = i


def format_line(ln: Line) -> str:
    return ("?" if ln.doubt else "") + ln.role + (f"〔{ln.cue}〕" if ln.cue else "") + "：" + ln.text


def render(sc: Script) -> str:
    out = ["---"] + [f"{k}: {v}" for k, v in sc.meta.items()] + ["---"]
    prev: Optional[Line] = None
    for ln in sc.lines:
        if ln.scene and (prev is None or ln.scene != prev.scene):
            name, mood = sc.scenes.get(ln.scene, ("", ""))
            out += ["", (f"# 场{ln.scene} {name}".rstrip()) + (f"〔{mood}〕" if mood else "")]
            out.append("")
        elif prev is None or ln.para != prev.para:
            out.append("")
        out.append(format_line(ln))
        prev = ln
    return "\n".join(out).rstrip() + "\n"


_LINE = re.compile(r"^([?？])?([^〔：#\s][^〔：]*?)(?:〔([^〕]*)〕)?：(.*)$")
_SCENE = re.compile(r"^#\s*场(\d+)\s*([^〔]*?)\s*(?:〔([^〕]*)〕)?\s*$")


class ScriptError(ValueError):
    pass


def parse(text: str) -> Script:
    sc = Script()
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end == -1:
            raise ScriptError("有声稿头部缺少结束的 ---")
        for row in text[3:end].strip().splitlines():
            if ":" in row:
                k, v = row.split(":", 1)
                sc.meta[k.strip()] = v.strip()
        body = text[end + 4:]
    para, scene, fresh = -1, 0, True
    for n, raw in enumerate(body.splitlines(), 1):
        s = raw.strip()
        if not s:
            fresh = True
            continue
        m = _SCENE.match(s)
        if m:
            scene = int(m.group(1))
            sc.scenes[scene] = (m.group(2).strip(), (m.group(3) or "").strip())
            fresh = True
            continue
        m = _LINE.match(s)
        if not m:
            raise ScriptError(f"有声稿正文第 {n} 行无法解析：{s[:40]}")
        if fresh:
            para += 1
            fresh = False
        sc.lines.append(Line(m.group(2).strip(), (m.group(3) or "").strip(), m.group(4),
                             para, bool(m.group(1)), scene))
    renumber(sc)
    return sc


# ---------------------------------------------------------------- 校验

def norm(s: str) -> str:
    s = re.sub(r"(?m)^\s*#{1,6}\s+", "", s)
    return re.sub(r"[\s“”]", "", s)


def fidelity(source: str, sc: Script) -> List[dict]:
    """逐字比对正文与有声稿（去掉空白与对白引号）。空列表 = 100% 忠实；每项带最近的有声稿行号。"""
    src = norm(source)
    owner: List[int] = []
    parts = []
    for ln in sc.lines:
        t = norm(ln.text)
        parts.append(t)
        owner += [ln.no] * len(t)
    out = "".join(parts)
    issues = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, src, out, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        at = owner[min(j1, len(owner) - 1)] if owner else 0
        issues.append({"类型": {"delete": "漏读", "insert": "多出", "replace": "改字"}[tag],
                       "原文": src[i1:i2][:60], "有声稿": out[j1:j2][:60], "行": at})
    return issues


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 选角

def variant_of(role: str) -> Optional[str]:
    parts = role.split("·")
    return next((p for p in reversed(parts[1:]) if p in VARIANTS), None)


def resolve(cast: dict, role: str) -> dict:
    """角色名 → 选角条目。由具体到泛查找：路人·接线员·电话 → 路人·接线员 → 路人；变体补 fx。"""
    roles = cast.get("角色", {})
    parts = role.split("·")
    for n in range(len(parts), 0, -1):
        key = "·".join(parts[:n])
        if key in roles:
            e = dict(roles[key])
            break
    else:
        raise KeyError(role)
    var = variant_of(role)
    if var and not e.get("fx"):
        e["fx"] = var
    return e


def missing_roles(cast: dict, sc: Script) -> List[str]:
    miss: List[str] = []
    for ln in sc.lines:
        try:
            resolve(cast, ln.role)
        except KeyError:
            if ln.role not in miss:
                miss.append(ln.role)
    return miss


def pool_category(gender: str, age: str) -> str:
    return ("女" if "女" in gender else "男") + AGE_ALIASES.get(age.strip(), "青年")


def pick_pool_voice(cast: dict, category: str) -> str:
    used = {e.get("voice") for e in cast.get("角色", {}).values() if not e.get("自动")}
    pool = cast.get("路人池", {}).get(category) or PASSERBY_POOLS.get(category) or PASSERBY_POOLS["男青年"]
    if isinstance(pool, str):
        pool = [pool]
    return next((v for v in pool if v not in used), pool[0])


# ---------------------------------------------------------------- 合成参数

def instruction(role: str, cue: str) -> Optional[str]:
    """表演指示 → 语音指令（context_texts 的第一条，不计费）。"""
    if not cue:
        return None
    if role.split("·")[0] == NARRATOR:
        return f"你在录有声小说的旁白，可以用「{cue}」的感觉来讲这一句吗？"
    if variant_of(role) == "心声":
        return f"这是角色心里的话，你可以用「{cue}」的语气说吗？"
    return f"你可以用「{cue}」的语气说这句话吗？"


def tts_params(entry: dict) -> dict:
    return {k: int(entry[c]) for k, c in (("speech_rate", "语速"), ("loudness_rate", "音量"), ("pitch", "音高"))
            if entry.get(c) not in (None, "", 0)}


def cache_key(voice: str, ctx: Optional[str], text: str, fx: Optional[str], params: dict) -> str:
    blob = json.dumps([voice, ctx, text, fx, params, "seed-tts-2.0"], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


def pause_ms(prev: Optional[Line], cur: Line) -> int:
    """两句之间插入的静音（毫秒）。"""
    if prev is None:
        return 0
    if prev.scene == 0 and cur.scene != 0:
        return 1000
    if cur.scene != prev.scene:
        return 1200
    if cur.para != prev.para:
        gap = 500
    else:
        gap = 160 if prev.text.rstrip().endswith(("：", "，", "、", ":", ",")) else 300
    return gap + (80 if cur.role != prev.role else 0)


# ---------------------------------------------------------------- 标注输出解析与复用

_LAB = re.compile(r"^S?(\d+)\s*[|｜]\s*([?？])?\s*([^|｜]+?)\s*(?:[|｜]\s*(.*?))?\s*$")
_SCN = re.compile(r"^场\s*[|｜]\s*S?(\d+)\s*[|｜]\s*([^|｜]*?)\s*(?:[|｜]\s*(.*?))?\s*$")
_NEW = re.compile(r"^新\s*[|｜]\s*([^|｜]+?)\s*[|｜]\s*([^|｜]+?)\s*[|｜]\s*([^|｜]+?)\s*$")


def parse_labels(text: str):
    """解析标注模型输出：
    S12|宋柚|轻快随意        片段标注（角色前加 ? 表示拿不准）
    场|S1|场名|氛围          场景从 S1 所在段落开始
    新|路人·接线员|女|青年    角色表外的路人：性别、年龄段
    """
    labels: Dict[int, Label] = {}
    scenes: Dict[int, Tuple[str, str]] = {}
    newbies: Dict[str, Tuple[str, str]] = {}
    for row in text.splitlines():
        row = row.strip().strip("`").strip()
        if not row:
            continue
        m = _SCN.match(row)
        if m:
            scenes[int(m.group(1))] = (m.group(2).strip(), (m.group(3) or "").strip())
            continue
        m = _NEW.match(row)
        if m:
            newbies[m.group(1).strip()] = (m.group(2).strip(), m.group(3).strip())
            continue
        m = _LAB.match(row)
        if m:
            labels[int(m.group(1))] = Label(m.group(3).strip(), (m.group(4) or "").strip(), bool(m.group(2)))
    return labels, scenes, newbies


def reuse_labels(old: Script, source: str, segs: Sequence[Segment]):
    """旧有声稿里与当前正文逐字相同的段落，把旧行的角色和指示映射回新片段。

    返回 (labels, scene_starts, reused_paras)。正文改了一两段时，只有改动段需要重新标注，
    其余句子文本、角色、指示都不变，合成时直接命中缓存。
    """
    groups: Dict[int, List[Line]] = {}
    for ln in old.lines:
        groups.setdefault(ln.para, []).append(ln)
    by_text: Dict[str, List[List[Line]]] = {}
    for g in groups.values():
        by_text.setdefault(norm("".join(l.text for l in g)), []).append(g)
    labels: Dict[int, Label] = {}
    scene_starts: Dict[int, Tuple[str, str]] = {}
    reused = set()
    prev_old_scene = None
    for para, p in enumerate(paragraph_texts(source)):
        cands = by_text.get(norm(p))
        psegs = [s for s in segs if s.para == para]
        if not cands or not psegs:
            prev_old_scene = None
            continue
        g = cands.pop(0)
        reused.add(para)
        spans, pos = [], 0
        for ln in g:
            n = len(norm(ln.text))
            spans.append((pos, pos + n, ln))
            pos += n
        pos = 0
        for s in psegs:
            ln = next((l for a, b, l in spans if a <= pos < b), g[-1])
            labels[s.sid] = Label(ln.role, ln.cue, ln.doubt)
            pos += len(norm(s.raw))
        if g[0].scene and g[0].scene != prev_old_scene and psegs[0].kind != "title":
            scene_starts[psegs[0].sid] = old.scenes.get(g[0].scene, ("", ""))
        prev_old_scene = g[0].scene
    return labels, scene_starts, reused
