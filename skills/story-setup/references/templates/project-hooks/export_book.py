#!/usr/bin/env python3
"""export_book.py — 将书目录 正文/*.md 导出为全本 TXT、逐章 TXT 与标准 EPUB3（含 toc.ncx）。

用法：
  {PYTHON} export_book.py --project <书目录> [--out-dir <输出目录>] [--formats txt,epub] [--chapter-txt-dir <逐章TXT目录>] [--book-title <书名>] [--author <作者>]
  {PYTHON} export_book.py <正文目录> <输出.epub> <书名> [作者]

书名默认优先读取 追踪/_tracking-state.json 的 book_title，回退书目录名；作者默认读取 STORY_AUTHOR。仅依赖 Python 标准库。
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

CHAPTER_NUM_RE = re.compile(r"^第0*(\d+)章")


def chapter_sort_key(path: Path) -> tuple[int, int, str]:
    match = CHAPTER_NUM_RE.match(path.stem)
    if match:
        return (0, int(match.group(1)), path.name)
    return (1, 0, path.name)


def chapter_title_from_name(stem: str) -> str:
    return re.sub(r"^第0*(\d+)章[_ \-　]*", r"第\1章 ", stem).strip()


def parse_chapter(path: Path) -> tuple[str, list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    title: str | None = None
    paras: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"^#+\s*(.*)$", stripped)
        if heading:
            content = heading.group(1).strip()
            if title is None and not paras:
                title = content
            elif content:
                paras.append(content)
            continue
        paras.append(stripped)
    return title or chapter_title_from_name(path.stem), paras


def render_chapter_txt(path: Path) -> str:
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


def build_epub(src: Path, out: Path, book: str, author: str = "") -> int:
    files = list_chapter_files(src)
    chapters = [parse_chapter(f) for f in files]
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


def build_full_txt(src: Path, out: Path, book: str, author: str = "") -> int:
    files = list_chapter_files(src)
    header = [f"《{book}》"]
    if author:
        header.append(f"作者：{author}")
    chunks = ["\n".join(header)]
    for path in files:
        chunks.append(render_chapter_txt(path).rstrip("\n"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n\n\n".join(chunks) + "\n", encoding="utf-8")
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
    parser.add_argument("--chapter-txt-dir", default=None, help="可选：逐章导出 .txt 到指定目录")
    parser.add_argument("--book-title", default=None)
    parser.add_argument("--author", default=os.environ.get("STORY_AUTHOR", ""))
    parsed = parser.parse_args(args_list)

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
        build_full_txt(src, txt_path, book_title, parsed.author)
        outputs["txt"] = str(txt_path)
    if "epub" in formats:
        epub_path = out_dir / f"{safe_name}.epub"
        build_epub(src, epub_path, book_title, parsed.author)
        outputs["epub"] = str(epub_path)
    if parsed.chapter_txt_dir:
        chap_dir = Path(parsed.chapter_txt_dir).resolve()
        chap_dir.mkdir(parents=True, exist_ok=True)
        for md_file in list_chapter_files(src):
            (chap_dir / f"{md_file.stem}.txt").write_text(render_chapter_txt(md_file), encoding="utf-8")
        outputs["chapter_txt_dir"] = str(chap_dir)

    print(json.dumps({
        "status": "ok",
        "book_title": book_title,
        "chapters": chapter_count,
        "outputs": outputs,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
