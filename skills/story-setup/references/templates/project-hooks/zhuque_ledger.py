#!/usr/bin/env python3
"""朱雀 AI 检测参数化台账（零硬编码书名/主角名）。

记录每一次朱雀送检（本地 PDF 报告或作者口头告知），并按 SHA-256 核对是否对应当前正文或最新候选稿。
数据：<书目录>/追踪/朱雀检测/记录.jsonl；台账：<书目录>/追踪/朱雀检测/台账.md（自动生成）。

用法：
  {PYTHON} .story/hooks/zhuque_ledger.py [--project <书目录>] scan [--dir ~/Downloads] [--mark <特征词>]
  {PYTHON} .story/hooks/zhuque_ledger.py [--project <书目录>] add --chapter 6 --human 100 [--date YYYY-MM-DD] [--note 作者告知]
  {PYTHON} .story/hooks/zhuque_ledger.py [--project <书目录>] render

规则：
- 人工% = AIGC 值 < 0.5 的片段占比之和；疑似 0.5–0.99；AI 特征 ≥ 0.99；通过 = 人工 100%。
- 特征词默认优先取 --mark / STORY_ZHUQUE_MARK，未设置时从 追踪/_tracking-state.json 自动提取主角名与书名过滤其他书的报告。
- 收录 PDF 时，其文本与当前正文/候选稿重合 ≥ 90% 即绑定正文 sha256；正文后续修改后旧记录自动降为“旧版”。
- 只读本地 PDF，从不联网送检。
"""
from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

NAME_RE = re.compile(r"朱雀AI检测助手.*?第0*(\d+)章(?:-([^.]+))?\.pdf$")
SEG_RE = re.compile(r"片段(\d+)\s+([\d.]+)%\s+(\d+)\s+([\d.]+)")
TIME_RE = re.compile(r"检测时间：\s*(\d{4})/(\d{2})/(\d{2})\s+(\d{2}:\d{2}:\d{2})")
CHAPTER_FILE_RE = re.compile(r"^第0*(\d+)章(?:[_ \-　].*)?\.md$")
CHAPTER_DIR_RE = re.compile(r"^第0*(\d+)章$")
MATCH_MIN = 0.90


def default_project_root() -> Path:
    for key in ("STORY_BOOK_ROOT", "STORY_PROJECT_ROOT"):
        val = os.environ.get(key, "").strip()
        if val and os.path.isdir(val):
            return Path(val).resolve()
    here = Path(__file__).resolve().parent
    if here.name == "hooks" and here.parent.name in (".story", ".claude"):
        return here.parent.parent
    return Path.cwd().resolve()


def resolve_book_marks(root: Path, explicit: str | None) -> list[str]:
    raw = explicit if explicit is not None else os.environ.get("STORY_ZHUQUE_MARK", "")
    if raw.strip():
        return [item.strip() for item in re.split(r"[,，|]", raw) if item.strip()]
    state_path = root / "追踪" / "_tracking-state.json"
    marks: list[str] = []
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            active = ((state.get("context") or {}).get("active_character_names") or [])
            for name in active:
                if isinstance(name, str) and name.strip() and name.strip() not in marks:
                    marks.append(name.strip())
            chars = state.get("characters") or {}
            if isinstance(chars, dict):
                for name in chars.keys():
                    if isinstance(name, str) and name.strip() and name.strip() not in marks:
                        marks.append(name.strip())
            title = state.get("book_title")
            if isinstance(title, str) and title.strip() and title.strip() not in marks:
                marks.append(title.strip())
        except Exception:
            pass
    return marks


def norm(text: str) -> str:
    return re.sub(r"[\s　]", "", text)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def current_text(root: Path, chapter: int) -> tuple[str | None, bytes | None]:
    prose_dir = root / "正文"
    if prose_dir.is_dir():
        for entry in sorted(prose_dir.glob("第*章*.md")):
            match = CHAPTER_FILE_RE.match(entry.name)
            if match and int(match.group(1)) == chapter:
                return str(entry), entry.read_bytes()
    cand_root = root / "追踪" / "候选章"
    if cand_root.is_dir():
        for chap_dir in sorted(cand_root.glob("第*章")):
            match = CHAPTER_DIR_RE.match(chap_dir.name)
            if match and int(match.group(1)) == chapter:
                cands = sorted(chap_dir.glob("*/candidate.md"))
                if cands:
                    return str(cands[-1]), cands[-1].read_bytes()
    return None, None


def overlap(text: str, pdf_text: str) -> float:
    cur = norm(text)
    chunks = [cur[i:i + 20] for i in range(0, max(0, len(cur) - 20), 20)]
    if not chunks:
        return 0.0
    return sum(1 for chunk in chunks if chunk in pdf_text) / len(chunks)


def load_records(log_path: Path) -> list[dict]:
    if not log_path.is_file():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_record(log_path: Path, rec: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec, ensure_ascii=False) + "\n")


def pdftext(path: str, layout: bool) -> str:
    args = ["pdftotext"] + (["-layout"] if layout else []) + [path, "-"]
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout


def when(rec: dict) -> str:
    tested = rec.get("tested_at") or ""
    return tested if len(tested) > 10 else tested + " 23:59:59"


def pct(val: float) -> str:
    return f"{val:g}%"


def score(segs: list[dict]) -> dict[str, float]:
    human = round(sum(seg["ratio"] for seg in segs if seg["aigc"] < 0.5), 2)
    ai = round(sum(seg["ratio"] for seg in segs if seg["aigc"] >= 0.99), 2)
    return {"human": human, "suspect": round(100 - human - ai, 2), "ai": ai}


def scan(root: Path, dirpath: str, explicit_mark: str | None) -> int:
    log_path = root / "追踪" / "朱雀检测" / "记录.jsonl"
    pdf_files = sorted(glob.glob(os.path.join(dirpath, "朱雀AI检测助手*.pdf")), key=os.path.getmtime)
    if not pdf_files:
        print("新收录 0 份。")
        return 0
    if not shutil.which("pdftotext"):
        print("跳过扫描：未安装 pdftotext（poppler），保留已有记录。", file=sys.stderr)
        return 0

    marks = resolve_book_marks(root, explicit_mark)
    known = {rec.get("pdf_sha256") for rec in load_records(log_path)}
    added = 0
    for path in pdf_files:
        match = NAME_RE.search(os.path.basename(path))
        if not match:
            continue
        psha = sha(Path(path).read_bytes())
        if psha in known:
            continue
        raw = pdftext(path, False)
        ch = int(match.group(1))
        _, body = current_text(root, ch)
        ov = overlap(body.decode("utf-8"), norm(raw)) if body else 0.0
        # 有特征词时要求命中任一特征词，或与当前章节文本高重合
        if marks and not any(mark in raw for mark in marks) and ov < MATCH_MIN:
            continue
        lay = pdftext(path, True)
        segs = [{"ratio": float(p), "chars": int(c), "aigc": float(v)} for _, p, c, v in SEG_RE.findall(lay)]
        if not segs:
            print(f"跳过（没读到片段）：{path}", file=sys.stderr)
            continue
        time_match = TIME_RE.search(lay)
        tested = (
            f"{time_match.group(1)}-{time_match.group(2)}-{time_match.group(3)} {time_match.group(4)}"
            if time_match
            else None
        )
        rec = {
            "chapter": ch,
            "tested_at": tested,
            "source": "pdf",
            "label": match.group(2),
            "file": path,
            "pdf_sha256": psha,
            "segments": segs,
            **score(segs),
            "prose_sha256": sha(body) if body and ov >= MATCH_MIN else None,
            "overlap_at_ingest": round(ov, 3),
            "ingested_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        append_record(log_path, rec)
        known.add(psha)
        added += 1
        tag = "现稿" if rec["prose_sha256"] else "旧版"
        print(f"收录 第{ch}章 {rec['label'] or ''} {tested} 人工{pct(rec['human'])} [{tag}]")
    print(f"新收录 {added} 份。")
    return added


def add(root: Path, args: argparse.Namespace) -> None:
    log_path = root / "追踪" / "朱雀检测" / "记录.jsonl"
    _, body = current_text(root, args.chapter)
    rec = {
        "chapter": args.chapter,
        "tested_at": args.date,
        "source": "author",
        "label": None,
        "human": args.human,
        "suspect": args.suspect,
        "ai": args.ai,
        "segments": None,
        "prose_sha256": None if args.old else (sha(body) if body else None),
        "note": args.note,
        "ingested_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    append_record(log_path, rec)
    print(f"已记 第{args.chapter}章 人工{args.human}%（{args.note}）")


def discover_chapters(root: Path, recs: list[dict]) -> list[int]:
    found = {int(rec["chapter"]) for rec in recs if "chapter" in rec}
    prose_dir = root / "正文"
    if prose_dir.is_dir():
        for entry in prose_dir.glob("第*章*.md"):
            match = CHAPTER_FILE_RE.match(entry.name)
            if match:
                found.add(int(match.group(1)))
    cand_root = root / "追踪" / "候选章"
    if cand_root.is_dir():
        for entry in cand_root.glob("第*章"):
            match = CHAPTER_DIR_RE.match(entry.name)
            if match:
                found.add(int(match.group(1)))
    return sorted(found)


def render(root: Path) -> Path:
    data_dir = root / "追踪" / "朱雀检测"
    log_path = data_dir / "记录.jsonl"
    ledger_path = data_dir / "台账.md"
    recs = load_records(log_path)
    chapters = discover_chapters(root, recs)
    lines = [
        "<!-- 由 .story/hooks/zhuque_ledger.py 生成；不要手改，改 记录.jsonl 后运行 render -->",
        "# 朱雀检测台账",
        "",
        f"> 生成于 {datetime.datetime.now():%Y-%m-%d %H:%M}。通过 = 人工 100%。“现稿”指测的文本和现在的正文一致；正文改过之后，之前的成绩只算旧版。",
        "",
        "| 章 | 状态 | 现稿最新 | 现稿送检 | 现稿通过 | 累计送检 | 累计通过 | 首次通过 |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    detail: list[str] = []
    for ch in chapters:
        rs = sorted([rec for rec in recs if rec["chapter"] == ch], key=when)
        _, body = current_text(root, ch)
        cur = sha(body) if body else None
        now = [rec for rec in rs if cur and rec.get("prose_sha256") == cur]
        passed = [idx for idx, rec in enumerate(rs, 1) if rec["human"] >= 100]
        if not rs:
            status, latest = ("未测" if body else "—"), "—"
        elif not now:
            status, latest = "现稿未测", "—"
        else:
            last = now[-1]
            status = "通过" if last["human"] >= 100 else "未通过"
            latest = f"{pct(last['human'])}（{(last['tested_at'] or '')[:10]}）"
        first = f"第{passed[0]}次" if passed else "—"
        lines.append(
            f"| {ch} | {status} | {latest} | {len(now)} | {sum(1 for rec in now if rec['human'] >= 100)} | {len(rs)} | {len(passed)} | {first} |"
        )
        if rs:
            detail += [
                "",
                f"## 第{ch}章",
                "",
                "| # | 时间 | 来源 | 人工 / 疑似 / AI | 片段 AIGC | 对应 |",
                "|---:|---|---|---|---|---|",
            ]
            for idx, rec in enumerate(rs, 1):
                src = os.path.basename(rec["file"]) if rec["source"] == "pdf" else f"作者告知：{rec.get('note') or ''}"
                segs = " / ".join(f"{seg['aigc']:.4f}" for seg in rec["segments"]) if rec.get("segments") else "—"
                tag = "现稿" if cur and rec.get("prose_sha256") == cur else "旧版"
                sup = "—" if rec.get("suspect") is None else rec["suspect"]
                ai = "—" if rec.get("ai") is None else rec["ai"]
                lines_h = f"{pct(rec['human'])} / {sup} / {ai}"
                detail.append(f"| {idx} | {rec['tested_at'] or '—'} | {src} | {lines_h} | {segs} | {tag} |")
    data_dir.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("\n".join(lines + detail) + "\n", encoding="utf-8")
    print(f"台账已写入 {os.path.relpath(ledger_path, root)}")
    return ledger_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="朱雀 AI 检测参数化台账")
    parser.add_argument("--project", default=None, help="书项目根目录（默认自动探测）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan_p = sub.add_parser("scan")
    scan_p.add_argument("--dir", default=os.path.expanduser("~/Downloads"))
    scan_p.add_argument("--mark", default=None, help="可选：PDF 归属特征词（逗号分隔；默认读 _tracking-state.json）")
    add_p = sub.add_parser("add")
    add_p.add_argument("--chapter", type=int, required=True)
    add_p.add_argument("--human", type=float, required=True)
    add_p.add_argument("--suspect", type=float)
    add_p.add_argument("--ai", type=float)
    add_p.add_argument("--date", default=f"{datetime.date.today():%Y-%m-%d}")
    add_p.add_argument("--note", default="作者告知，无PDF")
    add_p.add_argument("--old", action="store_true", help="测的是旧版本，不绑定现在的正文")
    sub.add_parser("render")

    args = parser.parse_args(argv)
    root = Path(args.project).resolve() if args.project else default_project_root()
    if args.cmd == "scan":
        scan(root, args.dir, args.mark)
        render(root)
    elif args.cmd == "add":
        add(root, args)
        render(root)
    else:
        render(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
