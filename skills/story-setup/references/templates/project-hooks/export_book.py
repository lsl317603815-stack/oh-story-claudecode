#!/usr/bin/env python3
"""export_book.py — 将书目录 正文/*.md 导出为全本 TXT、逐章 TXT 与标准 EPUB3（含 toc.ncx）。

用法：
  {PYTHON} export_book.py --project <书目录> [--out-dir <输出目录>] [--formats txt,epub] [--txt-layout plain|fanqie] [--chapter-txt-dir <逐章TXT目录>] [--book-title <书名>] [--author <作者>]
  {PYTHON} export_book.py <正文目录> <输出.epub> <书名> [作者]

书名默认优先读取 追踪/_tracking-state.json 的 book_title，回退书目录名；作者默认读取 STORY_AUTHOR；TXT 排版默认读取 STORY_TXT_LAYOUT（plain 或 fanqie）。仅依赖 Python 标准库。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

CSS = """body { margin: 0 5%; line-height: 1.8; }
h2 { text-align: center; font-size: 1.3em; margin: 1.5em 0 1.2em; }
p { text-indent: 2em; margin: 0 0 0.6em; }
"""

INDENT = "\u3000\u3000"
CHAPTER_NUM_RE = re.compile(r"^第0*(\d+)章")
TITLE_PREFIX_RE = re.compile(r"^\s*#{1,6}\s*")
CHAPTER_HEADING_RE = re.compile(r"^第[0-9零一二三四五六七八九十百千万〇两]+[章节回幕集卷](?:[_\s：:·.\-]|$)")
LEADING_COLON_QUOTE_RE = re.compile(
    r"^(?P<prefix>(?:[^“”「」]|[“][^”]*[”]|[「][^」]*[」])+?)[：:](?P<quote>[“][^“”]*[”]|[「][^「」]*[」])$"
)

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


def chapter_sort_key(path: Path) -> tuple[int, int, str]:
    match = CHAPTER_NUM_RE.match(path.stem)
    if match:
        return (0, int(match.group(1)), path.name)
    return (1, 0, path.name)


def chapter_title_from_name(stem: str) -> str:
    return re.sub(r"^第0*(\d+)章[_ \-　]*", r"第\1章 ", stem).strip()


def is_standalone_quote(s: str) -> bool:
    return (s.startswith("“") and s.endswith("”") and "”" not in s[1:-1]) or (
        s.startswith("「") and s.endswith("」") and "」" not in s[1:-1]
    )


def split_paragraph_outside_quotes(para: str) -> list[str]:
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


def split_clauses_outside_quotes(text: str) -> list[str]:
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
    p = prefix.strip().rstrip("，,。；;：:")
    mental_replacements = (
        (r"下定决心想道$", "下定了决心"),
        (r"((?:在)?心里头?)(?:暗暗|默默)(?:想道|说道|道)$", r"\1暗暗想着"),
        (r"((?:在)?心里头?)([^\s，,。！？!?]{1,6}?)地想道$", r"\1\2地想着"),
        (r"((?:在)?心里头?)对自己(?:说道|道)$", r"\1暗暗对自己说了一句"),
        (r"((?:在)?心里头?[^，,。]*?)想道$", r"\1暗想"),
        (r"(?:暗暗|暗自|默默)(?:想道|说道|道)$", "在心里暗想"),
        (r"(?:心想道|暗道|心道|想道)$", "暗想"),
    )
    for pattern, repl in mental_replacements:
        if re.search(pattern, p):
            return re.sub(pattern, repl, p) + "。"

    specific_replacements = (
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
        (r"(?:点了点头|点点头|点头)(?:说道|回答道|回道|答道|道)$", "点了点头"),
        (r"(?:摇了摇头|摇摇头|摇头)(?:说道|回答道|回道|答道|道)$", "摇了摇头"),
        (r"(?:摆了摆手|摆摆手|摆手)(?:说道|回答道|回道|答道|道)$", "摆了摆手"),
        (r"叹(?:了)?口气(?:说道|道)$", "叹了口气"),
        (r"(?<!叹口气)(?<!叹了口气)叹道$", "叹了口气"),
        (r"冷哼(?:一声)?(?:说道|道)$", "冷哼了一声"),
        (r"哼(?:了)?一声(?:说道|道)$", "哼了一声"),
        (r"(?:哼声|轻哼|哼)道$", "哼了一声"),
        (r"开口([对冲朝向][^\s，,。！？!?]{1,10}?)(?:问道|问)$", r"开口\1问了一句"),
        (r"开口([对冲朝向][^\s，,。！？!?]{1,10}?)(?:说道|道|说)$", r"\1开了口"),
        (r"开口(?:问道|问)$", "开口问了一句"),
        (r"开口(?:说道|道)$", "开了口"),
        (r"张口就(?:说道|回答道|回道|答道|道)$", "张口就回了一句"),
        (r"出声(?:问道|问)$", "出声问了一句"),
        (r"出声(?:说道|道)$", "出了声"),
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
        (r"(沉声|冷声|淡声|温声|轻声|低声|小声|大声|高声|朗声|厉声|急声|柔声)(?:问道|问)$", r"\1问了一句"),
        (r"(沉声|冷声|淡声|温声|轻声|低声|小声|大声|高声|朗声|厉声|急声|柔声)(?:说道|答道|回道|道)$", r"\1开了口"),
    )
    for pattern, repl in specific_replacements:
        if re.search(pattern, p):
            return re.sub(pattern, repl, p) + "。"

    if re.search(r"([对冲朝向][^\s，,。！？!?]{1,10}?)(?:说道|问道|答道|回道|道|说|问)$", p):
        if p.endswith(("问道", "问")):
            return re.sub(r"(?:问道|问)$", "问了一句", p) + "。"
        return re.sub(r"(?:说道|答道|回道|道|说)$", "开了口", p) + "。"

    if re.search(r"(?:地|随口|顺口)(?:说道|问道|答道|回道|道)$", p):
        if p.endswith("问道"):
            return re.sub(r"问道$", "问了一句", p) + "。"
        return re.sub(r"(?:说道|答道|回道|道)$", "开口", p) + "。"

    if re.search(r"，(?:问道|问)$", p):
        return re.sub(r"，(?:问道|问)$", "，问了一句。", p)
    if p.endswith("问道"):
        return re.sub(r"问道$", "问了一句。", p)

    if re.search(r"(?:说道|答道|回道|道)$", p):
        stripped_verb = re.sub(r"(?:说道|答道|回道|道)$", "", p).rstrip("，,")
        last_clause = split_clauses_outside_quotes(stripped_verb)[-1]
        if any(cue in last_clause for cue in CONCRETE_ACTION_CUES) and len(last_clause) >= 4:
            return stripped_verb + "。"
        return stripped_verb + "开了口。"

    return p + "。"


def is_incomplete_subordinate(clauses_so_far: list[str]) -> bool:
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
    if any(last_cl.startswith(st) for st in SUBORDINATE_STARTERS):
        return True
    if any(clauses_so_far[0].startswith(st) for st in CORRELATIVE_STARTERS):
        tail_joined = "，".join(clauses_so_far[1:])
        if len(c) < 44 and not any(m in tail_joined for m in CORRELATIVE_MARKERS):
            return True
    return False


def split_long_narrative_sentence(sent: str, max_two_row_chars: int = 34) -> list[str]:
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

    packed: list[str] = []
    for u in merged_units:
        if packed and (len(packed[-1]) < 13 or (len(packed[-1]) + 1 + len(u) <= 28)):
            packed[-1] = f"{packed[-1]}，{u}"
        else:
            packed.append(u)
    if len(packed) >= 2 and len(packed[-1]) < 14 and (len(packed[-2]) + 1 + len(packed[-1]) <= 46):
        packed[-2] = f"{packed[-2]}，{packed[-1]}"
        packed.pop()

    return [seg + (terminal if i == len(packed) - 1 else "。") for i, seg in enumerate(packed)]


def split_paragraphs_for_fanqie(paras: list[str]) -> list[str]:
    fanqie_paras: list[str] = []
    for para in paras:
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
    return fanqie_paras


def parse_chapter(path: Path) -> tuple[str, list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    title: str | None = None
    paras: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        heading = re.match(r"^#+\s*(.*)$", stripped)
        if heading:
            content = heading.group(1).strip()
            if title is None and not paras:
                title = content
            elif content:
                paras.append(content)
            continue
        if title is None and not paras and CHAPTER_HEADING_RE.match(stripped):
            title = stripped
            continue
        paras.append(stripped)
    return title or chapter_title_from_name(path.stem), paras


def render_chapter_txt(path: Path, layout: str = "plain") -> str:
    if layout == "fanqie":
        title, paras = parse_chapter(path)
        fanqie_paras = split_paragraphs_for_fanqie(paras)
        out_lines = [title] + [f"{INDENT}{p}" for p in fanqie_paras]
        return "\n".join(out_lines) + "\n"

    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    started = False
    fallback_title = chapter_title_from_name(path.stem)
    for raw in lines:
        if not started and not raw.strip():
            continue
        if not started:
            started = True
            heading = re.match(r"^#+\s*(.*)$", raw)
            if heading:
                out.append(heading.group(1))
                continue
            out.append(fallback_title)
            out.append("")
        out.append(re.sub(r"^#+\s+", "", raw))
    return "\n".join(out).rstrip() + "\n"


def xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        'lang="zh-CN" xml:lang="zh-CN">\n'
        f'<head><meta charset="utf-8"/><title>{html.escape(title)}</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def list_chapter_files(src: Path) -> list[Path]:
    return sorted((p for p in src.glob("*.md") if p.is_file()), key=chapter_sort_key)


def build_epub(src: Path, out: Path, book: str, author: str = "", layout: str = "plain") -> int:
    files = list_chapter_files(src)
    raw_chapters = [parse_chapter(f) for f in files]
    chapters = [
        (title, split_paragraphs_for_fanqie(paras) if layout == "fanqie" else paras)
        for title, paras in raw_chapters
    ]
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'oh-story-export:' + book)}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    esc = html.escape

    manifest, spine, nav_items, ncx_points = [], [], [], []
    docs: dict[str, str] = {}
    for index, (title, paras) in enumerate(chapters, 1):
        name = f"chap{index:04d}.xhtml"
        body = f"<h2>{esc(title)}</h2>\n" + "\n".join(f"<p>{esc(p)}</p>" for p in paras)
        docs[name] = xhtml(title, body)
        manifest.append(f'<item id="c{index}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{index}"/>')
        nav_items.append(f'<li><a href="{name}">{esc(title)}</a></li>')
        ncx_points.append(
            f'<navPoint id="n{index}" playOrder="{index}"><navLabel><text>{esc(title)}</text></navLabel>'
            f'<content src="{name}"/></navPoint>'
        )

    creator = f"<dc:creator>{esc(author)}</dc:creator>" if author else ""
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh-CN">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="bookid">{book_id}</dc:identifier><dc:title>{esc(book)}</dc:title>'
        f'{creator}<dc:language>zh-CN</dc:language>'
        f'<meta property="dcterms:modified">{modified}</meta></metadata>\n'
        '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        '<item id="css" href="style.css" media-type="text/css"/>'
        + "".join(manifest)
        + '</manifest>\n<spine toc="ncx">'
        + "".join(spine)
        + "</spine>\n</package>\n"
    )
    nav = xhtml(
        "目录",
        f'<nav epub:type="toc" id="toc"><h2>目录</h2><ol>{"".join(nav_items)}</ol></nav>',
    )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f'<head><meta name="dtb:uid" content="{book_id}"/></head>'
        f"<docTitle><text>{esc(book)}</text></docTitle><navMap>{''.join(ncx_points)}</navMap></ncx>\n"
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>\n"
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        deflate = dict(compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("META-INF/container.xml", container, **deflate)
        archive.writestr("OEBPS/content.opf", opf, **deflate)
        archive.writestr("OEBPS/nav.xhtml", nav, **deflate)
        archive.writestr("OEBPS/toc.ncx", ncx, **deflate)
        archive.writestr("OEBPS/style.css", CSS, **deflate)
        for name, doc in docs.items():
            archive.writestr(f"OEBPS/{name}", doc, **deflate)
    return len(chapters)


def build_full_txt(src: Path, out: Path, book: str, author: str = "", layout: str = "plain") -> int:
    files = list_chapter_files(src)
    header = [f"《{book}》"]
    if author:
        header.append(f"作者：{author}")
    chunks = ["\n".join(header)]
    for path in files:
        chunks.append(render_chapter_txt(path, layout=layout).rstrip("\n"))
    out.parent.mkdir(parents=True, exist_ok=True)
    joiner = "\n" if layout == "fanqie" else "\n\n\n"
    out.write_text(joiner.join(chunks) + "\n", encoding="utf-8")
    return len(files)


def resolve_book_title(project: Path, explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env_title = os.environ.get("STORY_BOOK_TITLE", "").strip()
    if env_title:
        return env_title
    state_file = project / "追踪" / "_tracking-state.json"
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            title = data.get("book_title")
            if isinstance(title, str) and title.strip():
                return title.strip()
        except Exception:
            pass
    return project.resolve().name


def resolve_prose_dir(project: Path) -> Path:
    direct = project / "正文"
    if direct.is_dir():
        return direct
    nested = project / project.name / "正文"
    if nested.is_dir():
        return nested
    return direct


def normalize_layout(raw: str | None, env_key: str) -> str:
    candidate = (raw if raw is not None else os.environ.get(env_key, "")).strip().lower()
    if not candidate:
        return "plain"
    if candidate not in {"plain", "fanqie"}:
        raise ValueError(f"不支持的排版模式：{candidate}（仅支持 plain 或 fanqie）")
    return candidate


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    # 兼容位置参数调用：export_book.py <正文目录> <输出.epub> <书名> [作者]
    if args_list and not args_list[0].startswith("-"):
        if len(args_list) < 3:
            sys.stderr.write(__doc__ or "")
            return 2
        count = build_epub(
            Path(args_list[0]),
            Path(args_list[1]),
            args_list[2],
            args_list[3] if len(args_list) > 3 else os.environ.get("STORY_AUTHOR", ""),
        )
        print(count)
        return 0

    parser = argparse.ArgumentParser(description="将 正文/*.md 导出为 TXT 与 EPUB3")
    parser.add_argument("--project", default=os.environ.get("STORY_BOOK_ROOT") or os.environ.get("STORY_PROJECT_ROOT") or ".")
    parser.add_argument("--out-dir", default=None, help="输出目录（默认：<书目录>/导出）")
    parser.add_argument("--formats", default="txt,epub", help="逗号分隔格式：txt,epub")
    parser.add_argument("--txt-layout", default=None, choices=["plain", "fanqie"], help="TXT 排版：plain（默认）或 fanqie（无损竖屏拆行）")
    parser.add_argument("--epub-layout", default=None, choices=["plain", "fanqie"], help="EPUB 段落排版：plain（默认）或 fanqie")
    parser.add_argument("--fanqie", action="store_true", help="--txt-layout fanqie 的快捷开关")
    parser.add_argument("--chapter-txt-dir", default=None, help="可选：逐章导出 .txt 到指定目录")
    parser.add_argument("--book-title", default=None)
    parser.add_argument("--author", default=os.environ.get("STORY_AUTHOR", ""))
    parsed = parser.parse_args(args_list)

    try:
        txt_layout = "fanqie" if parsed.fanqie else normalize_layout(parsed.txt_layout, "STORY_TXT_LAYOUT")
        epub_layout = normalize_layout(parsed.epub_layout, "STORY_EPUB_LAYOUT")
    except ValueError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2

    project = Path(parsed.project).resolve()
    src = resolve_prose_dir(project)
    if not src.is_dir():
        sys.stderr.write(f"ERROR: 未找到正文目录：{src}\n")
        return 2

    book_title = resolve_book_title(project, parsed.book_title)
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", book_title).strip() or project.name
    out_dir = Path(parsed.out_dir).resolve() if parsed.out_dir else (project / "导出")
    formats = {item.strip().lower() for item in parsed.formats.split(",") if item.strip()}

    outputs: dict[str, str] = {}
    chapter_count = len(list_chapter_files(src))
    if "txt" in formats:
        txt_path = out_dir / f"{safe_name}.txt"
        build_full_txt(src, txt_path, book_title, parsed.author, layout=txt_layout)
        outputs["txt"] = str(txt_path)
    if "epub" in formats:
        epub_path = out_dir / f"{safe_name}.epub"
        build_epub(src, epub_path, book_title, parsed.author, layout=epub_layout)
        outputs["epub"] = str(epub_path)
    if parsed.chapter_txt_dir:
        chap_dir = Path(parsed.chapter_txt_dir).resolve()
        chap_dir.mkdir(parents=True, exist_ok=True)
        for md_file in list_chapter_files(src):
            (chap_dir / f"{md_file.stem}.txt").write_text(
                render_chapter_txt(md_file, layout=txt_layout),
                encoding="utf-8",
            )
        outputs["chapter_txt_dir"] = str(chap_dir)

    print(json.dumps({
        "status": "ok",
        "book_title": book_title,
        "chapters": chapter_count,
        "txt_layout": txt_layout,
        "epub_layout": epub_layout,
        "outputs": outputs,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
