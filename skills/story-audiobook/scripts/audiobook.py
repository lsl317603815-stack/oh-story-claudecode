#!/usr/bin/env python3
"""story-audiobook：已定稿章节 → 多角色有声书（火山方舟 Agent Plan · 豆包语音合成 2.0）。

在作品根目录（含 正文/ 的目录）运行，或用 --book 指定。所有产物写在 <作品>/有声书/。

  audiobook.py doctor [--ping]                 环境自检：ffmpeg、方舟 key、作品目录；--ping 合成两个字验证语音开关
  audiobook.py status                          每章：定稿、有声稿、成品是否最新
  audiobook.py voices [关键词…]                 查 2.0 音色表
  audiobook.py cast propose [--roles 甲,乙]     模型按角色卡给每个角色 3 个候选音色 → 有声书/选角候选.json
  audiobook.py cast audition [--roles 甲,乙]    合成试音合集 → 有声书/试音/
  audiobook.py cast lock (--all-first | 角色=序号|voice …) [--relock]
  audiobook.py cast set 角色 [--voice V] [--fx 电话] [--语速 N] [--音量 N] [--音高 N] [--relock]
  audiobook.py cast show
  audiobook.py annotate N [--force]            正文 → 有声稿（只重标改动过的段落）
  audiobook.py check N                         有声稿校验：逐字忠实、角色已选角、是否过期
  audiobook.py synth N [--reroll 3,17] [--dry-run]   逐句合成并拼接成品（命中缓存的句子不重算）
  audiobook.py make N|N-M …                    annotate（需要时）→ check → synth

退出码：0 成功；1 校验不过或有阻断问题；2 用法或环境错误。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ark_audio as ark  # noqa: E402
import audiobook_core as core  # noqa: E402

REFS = HERE.parent / "references"
RATE = 24000
CHUNK_CHARS = 4000
DEFAULT_CONFIG = {"标注模型": ark.DEFAULT_TEXT_MODEL, "并发": 4, "场景上下文": False,
                  "响度LUFS": -18, "峰值dBTP": -3, "码率": "192k"}
GITIGNORE = "缓存/\n工作/\n成品/*.mp3\n试音/*.mp3\n"


class Fail(Exception):
    """阻断性问题：打印后以退出码 1 结束。"""


# ---------------------------------------------------------------- 路径与读写

class Book:
    def __init__(self, root: Path):
        self.root = root
        self.ab = root / "有声书"

    @staticmethod
    def find(arg: Optional[str]) -> "Book":
        base = Path(arg).expanduser().resolve() if arg else Path.cwd()
        if (base / "正文").is_dir():
            return Book(base)
        subs = [p for p in base.iterdir() if p.is_dir() and (p / "正文").is_dir()] if base.is_dir() else []
        if len(subs) == 1:
            return Book(subs[0])
        raise SystemExit(f"找不到作品目录（需要包含 正文/）：{base}。用 --book 指定。")

    def chapter_file(self, n: int) -> Path:
        hits = sorted(self.root.glob(f"正文/第{n:03d}章*.md")) or \
            [p for p in sorted((self.root / "正文").glob("第*章*.md"))
             if re.match(rf"第0*{n}章", p.name)]
        if not hits:
            raise Fail(f"找不到第 {n} 章正文（正文/第{n:03d}章*.md）")
        return hits[0]

    def chapters(self) -> List[int]:
        out = set()
        for p in (self.root / "正文").glob("第*章*.md"):
            m = re.match(r"第0*(\d+)章", p.name)
            if m:
                out.add(int(m.group(1)))
        return sorted(out)

    def title(self, n: int) -> str:
        m = re.match(r"第0*\d+章_?(.*)\.md$", self.chapter_file(n).name)
        return (m.group(1) if m else "").strip("_ ")

    def script_path(self, n: int) -> Path:
        return self.ab / "有声稿" / f"第{n:03d}章.有声稿.md"

    def product(self, n: int, ext: str) -> Path:
        t = self.title(n)
        return self.ab / "成品" / (f"第{n:03d}章" + (f"_{t}" if t else "") + ext)

    def ensure(self) -> None:
        for d in ("有声稿", "成品", "缓存", "工作", "试音"):
            (self.ab / d).mkdir(parents=True, exist_ok=True)
        gi = self.ab / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE, encoding="utf-8")

    def config(self) -> dict:
        p = self.ab / "配置.json"
        cfg = dict(DEFAULT_CONFIG)
        if p.exists():
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        return cfg

    def cast(self) -> dict:
        p = self.ab / "选角.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"角色": {}}

    def save_cast(self, cast: dict) -> None:
        self.ensure()
        cast.setdefault("说明", "story-audiobook 选角表：角色 → 豆包语音合成 2.0 音色。锁定后不换声；路人由标注自动从路人池补充（自动=true）。")
        (self.ab / "选角.json").write_text(json.dumps(cast, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def rel(book: Book, p: Path) -> str:
    try:
        return str(p.relative_to(book.root))
    except ValueError:
        return str(p)


def now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def catalog() -> List[dict]:
    return json.loads(read(REFS / "voices-seed-tts-2.0.json"))["音色"]


def voice_name(voice: str) -> str:
    return next((v["音色名"] for v in catalog() if v["voice"] == voice), voice)


# ---------------------------------------------------------------- 定稿检查

def committed_state(book: Book, n: int) -> Tuple[bool, str]:
    track = book.root / "追踪"
    if not track.is_dir():
        return True, "未建追踪（短篇或导入稿），不校验定稿回执"
    rec = track / "章节提交" / f"第{n:03d}章.json"
    if not rec.exists():
        return False, f"第 {n} 章还没有定稿回执（{rel(book, rec)}）"
    d = json.loads(read(rec))
    if d.get("status") != "committed":
        return False, f"第 {n} 章定稿状态是 {d.get('status')}，不是 committed"
    want = d.get("accepted_prose_sha256")
    if want and hashlib.sha256(book.chapter_file(n).read_bytes()).hexdigest() != want:
        return False, f"第 {n} 章正文在定稿后被改过（与定稿回执的 SHA 不一致）"
    return True, "已定稿"


def require_committed(book: Book, n: int, allow: bool) -> None:
    ok, why = committed_state(book, n)
    if not ok and not allow:
        raise Fail(why + "。有声书只做定稿章节；确需先听草稿时加 --allow-uncommitted。")


# ---------------------------------------------------------------- 角色卡

CARD_FIELDS = ("角色定位", "性别", "年龄", "身份", "外貌特征", "外貌", "性格", "声线")


def roster(book: Book) -> List[Tuple[str, str]]:
    out = []
    for f in sorted((book.root / "设定" / "角色").glob("*.md")):
        text = read(f)
        fields = []
        for line in text.splitlines():
            m = re.match(r"^\s*[-*]\s*(\S+?)[：:]\s*(.+)$", line)
            if m and m.group(1) in CARD_FIELDS:
                fields.append(f"{m.group(1)} {m.group(2).strip()[:90]}")
        if not fields:
            body = re.sub(r"(?s)^---.*?---", "", text)
            body = " ".join(l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#"))
            fields = [body[:300]]
        out.append((f.stem, "；".join(fields)))
    return out


def protagonist(cards: Sequence[Tuple[str, str]]) -> Optional[str]:
    return next((name for name, s in cards if "角色定位 主角" in s), None)


def detect_pov(book: Book) -> str:
    chs = book.chapters()
    if not chs:
        return "第三人称"
    segs = core.segment_chapter(read(book.chapter_file(chs[0])))
    narr = "".join(s.text for s in segs if s.kind == "narr")
    return "第一人称" if narr and narr.count("我") / len(narr) > 0.008 else "第三人称"


def extract_json(text: str):
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1)
    start = min([i for i in (t.find("["), t.find("{")) if i != -1] or [0])
    return json.loads(t[start:])


# ---------------------------------------------------------------- 选角

def cmd_cast_propose(book: Book, roles: Optional[List[str]], pov: str) -> int:
    cards = roster(book)
    if not cards:
        raise Fail("设定/角色/ 下没有角色卡，无法选角。先补角色卡（至少写清性别、年龄、性格、声线）。")
    if roles:
        cards = [c for c in cards if c[0] in roles] or cards
    pov = detect_pov(book) if pov == "auto" else pov
    hero = protagonist(roster(book))
    opening = ""
    chs = book.chapters()
    if chs:
        segs = core.segment_chapter(read(book.chapter_file(chs[0])))
        opening = "".join(s.raw for s in segs if s.kind == "narr")[:80]
    voices = [v for v in catalog()]
    prompt = (read(REFS / "casting-prompt.md") +
              f"\n\n# 叙事视角\n{pov}" + (f"（主角：{hero}）" if hero else "") +
              (f"\n\n# 正文开头（旁白试音用）\n{opening}" if opening else "") +
              "\n\n# 角色卡\n" + "\n".join(f"- {n}：{s}" for n, s in cards) +
              "\n\n# 可用音色（voice｜音色名｜性别｜场景｜避用）\n" +
              "\n".join(f"{v['voice']}｜{v['音色名']}｜{v['性别']}｜{v['场景']}" + ("｜避用" if v.get("避用") else "")
                        for v in voices))
    book.ensure()
    (book.ab / "工作" / "选角.prompt.md").write_text(prompt, encoding="utf-8")
    text, usage = ark.chat(prompt, model=book.config()["标注模型"], effort="medium")
    (book.ab / "工作" / "选角.raw.md").write_text(text, encoding="utf-8")
    valid = {v["voice"]: v for v in catalog() if not v.get("避用")}
    try:
        rows = extract_json(text)
    except ValueError:
        raise Fail("选角模型没有返回可解析的 JSON，原文已存到 有声书/工作/选角.raw.md，重跑一次即可。")
    path = book.ab / "选角候选.json"
    cand = json.loads(read(path)) if path.exists() else {}
    cand["视角"] = pov
    cand["主角"] = hero or cand.get("主角", "")
    cand.setdefault("角色", {})
    dropped = []
    for row in rows:
        name = str(row.get("角色", "")).strip()
        picks = []
        for c in row.get("候选", []):
            v = str(c.get("voice", "")).strip()
            if v in valid and v not in [p["voice"] for p in picks]:
                picks.append({"voice": v, "音色名": valid[v]["音色名"], "理由": str(c.get("理由", ""))})
            else:
                dropped.append(f"{name}:{v}")
        if not name or not picks:
            continue
        cand["角色"][name] = {"候选": picks[:3], "试音台词": str(row.get("试音台词", "")).strip(),
                            "试音指示": str(row.get("试音指示", "")).strip(), "变体": str(row.get("变体", "")).strip()}
    path.write_text(json.dumps(cand, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    firsts: Dict[str, List[str]] = {}
    for name, e in cand["角色"].items():
        firsts.setdefault(e["候选"][0]["voice"], []).append(name)
    print(f"视角：{pov}；候选写入 {rel(book, path)}")
    for name, e in cand["角色"].items():
        print(f"  {name}：" + " / ".join(c["音色名"] for c in e["候选"]))
    if dropped:
        print("已丢弃不在音色表或标为避用的候选：" + "、".join(dropped))
    clash = {v: ns for v, ns in firsts.items() if len(ns) > 1}
    if clash:
        print("注意：首选撞音色 " + "；".join(f"{voice_name(v)}={'、'.join(ns)}" for v, ns in clash.items()))
    print(f"用量：tokens {usage.get('prompt_tokens')}/{usage.get('completion_tokens')}")
    return 0


def _wav_bytes(pcm: bytes) -> bytes:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def cmd_cast_audition(book: Book, roles: Optional[List[str]], announcer: str) -> int:
    need_ffmpeg()
    path = book.ab / "选角候选.json"
    if not path.exists():
        raise Fail("还没有选角候选：先运行 cast propose。")
    cand = json.loads(read(path))
    todo = [(n, e) for n, e in cand["角色"].items() if not roles or n in roles]
    chars = sum(len(e["试音台词"] or "试音") * len(e["候选"]) + 12 for _, e in todo)
    print(f"试音 {len(todo)} 个角色、{sum(len(e['候选']) for _, e in todo)} 段，约 {chars} 字，"
          f"预计 {chars * ark.AFP_PER_10K_CHARS / 10000:.0f} AFP")
    key = ark.load_key()
    out = book.ab / "试音"
    out.mkdir(parents=True, exist_ok=True)
    pcm_all = bytearray()
    lines = ["# 试音清单", "", f"合集：试音合集.mp3（{now()}）", "",
             "| 时间 | 角色 | 序号 | 音色 | 理由 |", "| --- | --- | --- | --- | --- |"]
    t = 0.0
    for name, e in todo:
        role = core.NARRATOR if name == core.NARRATOR else name
        for i, c in enumerate(e["候选"], 1):
            label = f"{name}，候选{i}，{c['音色名']}"
            a, _ = ark.tts_retry(label, announcer, key=key)
            ctx = core.instruction(role, e.get("试音指示", ""))
            s, _ = ark.tts_retry(e["试音台词"] or label, c["voice"], ctx=ctx, key=key)
            sample = out / f"{name}_{i}_{c['音色名']}.mp3"
            to_mp3(_wav_bytes(s), sample)
            lines.append(f"| {int(t // 60)}:{int(t % 60):02d} | {name} | {i} | {c['音色名']} | {c['理由']} |")
            for chunk, gap in ((a, 0.35), (s, 0.9)):
                pcm_all.extend(chunk)
                pcm_all.extend(b"\x00\x00" * int(RATE * gap))
                t += len(chunk) / 2 / RATE + gap
    to_mp3(_wav_bytes(bytes(pcm_all)), out / "试音合集.mp3")
    (out / "试音清单.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"试音合集：{rel(book, out / '试音合集.mp3')}（{t / 60:.1f} 分钟）；清单：{rel(book, out / '试音清单.md')}")
    print("听完后锁定：audiobook.py cast lock 角色=序号 …（或 --all-first 全部取首选）")
    return 0


def cmd_cast_lock(book: Book, picks: List[str], all_first: bool, relock: bool) -> int:
    path = book.ab / "选角候选.json"
    cand = json.loads(read(path)) if path.exists() else {"角色": {}}
    cast = book.cast()
    roles = cast.setdefault("角色", {})
    cast.setdefault("路人池", {k: v for k, v in core.PASSERBY_POOLS.items()})
    valid = {v["voice"] for v in catalog()}
    chosen: Dict[str, str] = {}
    if all_first:
        chosen = {n: e["候选"][0]["voice"] for n, e in cand.get("角色", {}).items()}
    for p in picks:
        if "=" not in p:
            raise Fail(f"格式应为 角色=序号 或 角色=voice：{p}")
        name, val = (x.strip() for x in p.split("=", 1))
        if val.isdigit():
            opts = cand.get("角色", {}).get(name, {}).get("候选", [])
            if not 1 <= int(val) <= len(opts):
                raise Fail(f"{name} 没有第 {val} 个候选")
            chosen[name] = opts[int(val) - 1]["voice"]
        else:
            if val not in valid:
                raise Fail(f"{val} 不在豆包语音合成 2.0 音色表里")
            chosen[name] = val
    if not chosen:
        raise Fail("没有指定要锁定的角色：用 角色=序号，或 --all-first。")
    changed = []
    for name, voice in chosen.items():
        old = roles.get(name)
        if old and old.get("voice") != voice and not old.get("自动") and not relock:
            raise Fail(f"{name} 已锁定为 {old.get('音色名')}；换声会让已出的章节前后不一。确需更换请加 --relock。")
        entry = dict(old or {})
        entry.update({"voice": voice, "音色名": voice_name(voice), "锁定": dt.date.today().isoformat()})
        entry.pop("自动", None)
        var = cand.get("角色", {}).get(name, {}).get("变体")
        if var in core.VARIANTS and not entry.get("fx"):
            entry["fx"] = var
        roles[name] = entry
        changed.append(f"{name}={entry['音色名']}")
    hero = cand.get("主角")
    if cand.get("视角") == "第一人称" and hero and hero in roles:
        roles[core.NARRATOR] = dict(roles[hero], 说明="第一人称：旁白与主角同声")
        changed.append(f"旁白=主角（{roles[hero]['音色名']}）")
    cast["版本"] = int(cast.get("版本", 0)) + 1
    book.save_cast(cast)
    print("已锁定：" + "、".join(changed))
    uses: Dict[str, List[str]] = {}
    for n, e in roles.items():
        if not e.get("自动"):
            uses.setdefault(e["voice"], []).append(n)
    clash = [f"{voice_name(v)}={'、'.join(ns)}" for v, ns in uses.items() if len(ns) > 1 and core.NARRATOR not in ns]
    if clash:
        print("注意：同一音色被多个角色使用 " + "；".join(clash))
    return 0


def cmd_cast_set(book: Book, name: str, voice: Optional[str], fx: Optional[str], rate: Optional[int],
                 loud: Optional[int], pitch: Optional[int], relock: bool) -> int:
    cast = book.cast()
    roles = cast.setdefault("角色", {})
    entry = dict(roles.get(name, {}))
    if voice:
        if voice not in {v["voice"] for v in catalog()}:
            raise Fail(f"{voice} 不在豆包语音合成 2.0 音色表里")
        if entry.get("voice") and entry["voice"] != voice and not entry.get("自动") and not relock:
            raise Fail(f"{name} 已锁定为 {entry.get('音色名')}；确需更换请加 --relock。")
        entry.update({"voice": voice, "音色名": voice_name(voice), "锁定": dt.date.today().isoformat()})
        entry.pop("自动", None)
    if not entry.get("voice"):
        raise Fail(f"{name} 还没有音色：用 --voice 指定。")
    if fx is not None:
        if fx and fx not in core.VARIANTS:
            raise Fail(f"fx 只能是 {'、'.join(core.VARIANTS)} 或空")
        entry["fx"] = fx
        if not fx:
            entry.pop("fx")
    for k, v in (("语速", rate), ("音量", loud), ("音高", pitch)):
        if v is not None:
            entry[k] = v
    roles[name] = entry
    cast["版本"] = int(cast.get("版本", 0)) + 1
    book.save_cast(cast)
    print(f"{name}：{json.dumps(entry, ensure_ascii=False)}")
    return 0


def cmd_cast_show(book: Book) -> int:
    cast = book.cast()
    roles = cast.get("角色", {})
    if not roles:
        print("还没有选角：cast propose → cast audition → cast lock")
        return 0
    for name, e in roles.items():
        extra = "、".join(f"{k}={e[k]}" for k in ("fx", "语速", "音量", "音高") if e.get(k) not in (None, ""))
        print(f"{name}：{e.get('音色名')}（{e.get('voice')}）" + (f"［{extra}］" if extra else "") +
              ("［自动·路人池］" if e.get("自动") else ""))
    return 0


# ---------------------------------------------------------------- 标注

def label_prompt(book: Book, chunk: Sequence[core.Segment], context: str, cast: dict) -> str:
    cards = roster(book)
    known = list(dict.fromkeys([core.NARRATOR] + [n for n, _ in cards] + list(cast.get("角色", {}))))
    return ("# 角色表\n" + ("\n".join(f"- {n}：{s}" for n, s in cards) or "（无角色卡）") +
            "\n\n# 已有声部（沿用这些写法）\n" + "、".join(known) +
            "\n\n# 上文（只供判断，不要标注）\n" + (context or "（本章开头）") +
            "\n\n# 待标注片段\n" + core.format_segments(chunk) + "\n")


def chunk_segments(segs: Sequence[core.Segment], limit: int = CHUNK_CHARS) -> List[List[core.Segment]]:
    chunks: List[List[core.Segment]] = []
    cur: List[core.Segment] = []
    size = 0
    for para in dict.fromkeys(s.para for s in segs):
        ps = [s for s in segs if s.para == para]
        n = sum(len(s.raw) for s in ps)
        if cur and size + n > limit:
            chunks.append(cur)
            cur, size = [], 0
        cur += ps
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def annotate(book: Book, n: int, force: bool = False, allow_uncommitted: bool = False) -> core.Script:
    require_committed(book, n, allow_uncommitted)
    book.ensure()
    src_path = book.chapter_file(n)
    source = read(src_path)
    segs = core.segment_chapter(source)
    cast = book.cast()
    cfg = book.config()
    labels: Dict[int, core.Label] = {}
    scene_starts: Dict[int, Tuple[str, str]] = {}
    reused: set = set()
    sp = book.script_path(n)
    if sp.exists() and not force:
        try:
            labels, scene_starts, reused = core.reuse_labels(core.parse(read(sp)), source, segs)
        except core.ScriptError:
            labels, scene_starts, reused = {}, {}, set()
    todo = [s for s in segs if s.para not in reused]
    newbies: Dict[str, Tuple[str, str]] = {}
    usage_tokens = [0, 0]
    if todo:
        system = read(REFS / "annotate-prompt.md")
        chunks = chunk_segments(todo)

        def run(chunk):
            first = chunk[0]
            before = [s for s in segs if s.sid < first.sid]
            context = "".join(s.raw for s in before)[-600:]
            prompt = label_prompt(book, chunk, context, cast)
            text, usage = ark.chat(prompt, system=system, model=cfg["标注模型"], effort="low")
            got, scn, nb = core.parse_labels(text)
            missing = [s for s in chunk if s.sid not in got]
            if missing:
                text2, usage2 = ark.chat(label_prompt(book, missing, context, cast) +
                                         "\n（上一轮漏标了这些片段，请只补标它们）\n",
                                         system=system, model=cfg["标注模型"], effort="low")
                g2, s2, n2 = core.parse_labels(text2)
                got.update({k: v for k, v in g2.items() if k in {s.sid for s in missing}})
                scn.update(s2)
                nb.update(n2)
                text += "\n# 补标\n" + text2
                for k in ("prompt_tokens", "completion_tokens"):
                    usage[k] = (usage.get(k) or 0) + (usage2.get(k) or 0)
            (book.ab / "工作" / f"第{n:03d}章.标注.S{first.sid}.md").write_text(text, encoding="utf-8")
            return got, scn, nb, usage

        with ThreadPoolExecutor(3) as ex:
            for got, scn, nb, usage in ex.map(run, chunks):
                labels.update({k: v for k, v in got.items() if k in {s.sid for s in todo}})
                scene_starts.update(scn)
                newbies.update(nb)
                usage_tokens[0] += usage.get("prompt_tokens") or 0
                usage_tokens[1] += usage.get("completion_tokens") or 0
    names = {name for name, _ in roster(book)} | set(cast.get("角色", {})) | {core.NARRATOR}
    renamed = []
    for sid, lab in labels.items():
        base = lab.role.split("·")[0]
        if base not in names and base != core.PASSERBY:
            new = core.PASSERBY + "·" + lab.role
            renamed.append(f"{lab.role}→{new}")
            if lab.role in newbies:
                newbies[new] = newbies.pop(lab.role)
            lab.role = new
    sc = core.build_lines(segs, labels, scene_starts)
    sc.meta = {"章节": str(n), "标题": book.title(n), "正文": rel(book, src_path),
               "正文sha256": core.sha256_text(source), "标注模型": cfg["标注模型"], "生成时间": now()}
    issues = core.fidelity(source, sc)
    if issues:
        raise Fail("有声稿与正文不一致（切分逻辑缺陷，请报告）：" + json.dumps(issues[:5], ensure_ascii=False))
    added = autocast_passersby(book, sc, newbies)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(core.render(sc), encoding="utf-8")
    roles: Dict[str, int] = {}
    for ln in sc.lines:
        roles[ln.role] = roles.get(ln.role, 0) + 1
    print(f"有声稿：{rel(book, sp)}（{len(sc.lines)} 行，{len(sc.scenes)} 场；复用 {len(reused)} 段，新标 "
          f"{len({s.para for s in todo})} 段；tokens {usage_tokens[0]}/{usage_tokens[1]}）")
    print("声部：" + "、".join(f"{r}×{c}" for r, c in roles.items()))
    if renamed:
        print("角色表外的名字已按路人处理：" + "、".join(renamed))
    if added:
        print("路人已从路人池自动配音：" + "、".join(added))
    doubts = [ln for ln in sc.lines if ln.doubt]
    if doubts:
        print(f"存疑 {len(doubts)} 行（行首 ?，请作者确认说话人后去掉 ?）：")
        for ln in doubts[:20]:
            print(f"  第 {ln.no} 行 {ln.role}：{ln.text[:30]}")
    return sc


def autocast_passersby(book: Book, sc: core.Script, newbies: Dict[str, Tuple[str, str]]) -> List[str]:
    cast = book.cast()
    roles = cast.setdefault("角色", {})
    if not roles:
        return []
    added = []
    for role in core.missing_roles(cast, sc):
        if not role.startswith(core.PASSERBY + "·"):
            continue
        key = "·".join(p for p in role.split("·") if p not in core.VARIANTS)
        if key in roles:
            continue
        gender, age = newbies.get(key) or newbies.get(role) or ("男", "青年")
        cat = core.pool_category(gender, age)
        voice = core.pick_pool_voice(cast, cat)
        roles[key] = {"voice": voice, "音色名": voice_name(voice), "自动": True, "类别": cat}
        added.append(f"{key}（{cat}·{voice_name(voice)}）")
    if added:
        book.save_cast(cast)
    return added


# ---------------------------------------------------------------- 校验

def check(book: Book, n: int, quiet: bool = False) -> Tuple[core.Script, List[str], List[str]]:
    sp = book.script_path(n)
    if not sp.exists():
        raise Fail(f"第 {n} 章还没有有声稿：先运行 annotate {n}（或 make {n}）。")
    sc = core.parse(read(sp))
    source = read(book.chapter_file(n))
    errors, warns = [], []
    if sc.meta.get("正文sha256") and sc.meta["正文sha256"] != core.sha256_text(source):
        errors.append("正文在生成有声稿之后改过：运行 annotate 只重标改动段落。")
    for it in core.fidelity(source, sc):
        errors.append(f"第 {it['行']} 行附近{it['类型']}：原文「{it['原文']}」 有声稿「{it['有声稿']}」")
    cast = book.cast()
    if not cast.get("角色"):
        errors.append("还没有选角：cast propose → cast audition → cast lock。")
    else:
        for r in core.missing_roles(cast, sc):
            errors.append(f"角色「{r}」未选角：用 cast propose --roles {r.split('·')[0]} 或 cast set {r} --voice …")
    for ln in sc.lines:
        if ln.cue and not 2 <= len(ln.cue) <= 24:
            warns.append(f"第 {ln.no} 行表演指示长度 {len(ln.cue)}（建议 4–16 字）")
        if len(ln.text) > 300:
            errors.append(f"第 {ln.no} 行 {len(ln.text)} 字，超过单次合成上限 300：在句末拆成两行")
        if ln.doubt:
            warns.append(f"第 {ln.no} 行存疑（{ln.role}）：{ln.text[:24]}")
    if not quiet:
        print(f"第 {n} 章有声稿：{len(sc.lines)} 行、{len(sc.scenes)} 场；错误 {len(errors)}，提示 {len(warns)}")
        for e in errors:
            print("  ❌ " + e)
        for w in warns[:30]:
            print("  ⚠ " + w)
    return sc, errors, warns


# ---------------------------------------------------------------- 合成

def need_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise Fail("需要 ffmpeg（macOS：brew install ffmpeg）。")


def to_mp3(wav: bytes, out: Path, bitrate: str = "192k") -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "wav", "-i", "pipe:0", "-ar", "44100", "-b:a", bitrate,
                    str(out)], input=wav, check=True)


def apply_fx(wav: bytes, fx: str) -> bytes:
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "wav", "-i", "pipe:0", "-af", core.FX_FILTERS[fx],
                        "-ar", str(RATE), "-ac", "1", "-f", "wav", "pipe:1"], input=wav, capture_output=True, check=True)
    return r.stdout


def wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def loudnorm(raw: Path, out: Path, lufs: float, tp: float, bitrate: str) -> None:
    spec = f"loudnorm=I={lufs}:TP={tp}:LRA=11"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(raw), "-af", spec + ":print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if m:
        d = json.loads(m.group(0))
        try:
            finite = all(abs(float(d[k])) != float("inf") for k in ("input_i", "input_tp", "target_offset"))
        except (KeyError, ValueError):
            finite = False
        if finite:
            spec += (f":measured_I={d['input_i']}:measured_TP={d['input_tp']}:measured_LRA={d['input_lra']}"
                     f":measured_thresh={d['input_thresh']}:offset={d['target_offset']}:linear=true")
        else:
            spec = "anull"          # 几乎无声，测不出响度：原样编码
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af", spec, "-ar", "44100", "-b:a", bitrate,
                    str(out)], check=True)


def plan_lines(book: Book, sc: core.Script, use_ctx: bool) -> List[dict]:
    cast = book.cast()
    plan = []
    for ln in sc.lines:
        e = core.resolve(cast, ln.role)
        params = core.tts_params(e)
        ctx = core.instruction(ln.role, ln.cue)
        fx = e.get("fx") or None
        key = core.cache_key(e["voice"], ctx, ln.text, fx, dict(params, 场景上下文=use_ctx))
        plan.append({"line": ln, "voice": e["voice"], "ctx": ctx, "fx": fx, "params": params, "key": key,
                     "wav": book.ab / "缓存" / key[:2] / f"{key}.wav"})
    return plan


def synth(book: Book, n: int, reroll: Sequence[int] = (), dry_run: bool = False,
          allow_uncommitted: bool = False, workers: Optional[int] = None) -> int:
    require_committed(book, n, allow_uncommitted)
    sc, errors, _ = check(book, n, quiet=True)
    if errors:
        print(f"第 {n} 章有声稿校验不过，未合成：")
        for e in errors:
            print("  ❌ " + e)
        return 1
    cfg = book.config()
    use_ctx = bool(cfg.get("场景上下文"))
    plan = plan_lines(book, sc, use_ctx)
    for p in plan:
        if p["line"].no in set(reroll) and p["wav"].exists():
            p["wav"].unlink()
    todo = [p for p in plan if not p["wav"].exists()]
    chars = sum(len(p["line"].text) for p in todo)
    afp = chars * ark.AFP_PER_10K_CHARS / 10000
    if dry_run:
        print(f"第 {n} 章：{len(plan)} 行，需新合成 {len(todo)} 行、约 {chars} 字，预计 {afp:.0f} AFP；"
              f"其余 {len(plan) - len(todo)} 行命中缓存。")
        return 0
    need_ffmpeg()
    book.ensure()
    key = ark.load_key() if todo else None
    billed = [0]
    lock = threading.Lock()
    started = time.time()

    def one(p, section=None):
        ln = p["line"]
        pcm, usage = ark.tts_retry(ln.text, p["voice"], ctx=p["ctx"], section=section, params=p["params"], key=key)
        with lock:
            billed[0] += int(usage.get("text_words") or len(ln.text))
        data = _wav_bytes(pcm)
        if p["fx"] in core.FX_FILTERS:
            data = apply_fx(data, p["fx"])
        p["wav"].parent.mkdir(parents=True, exist_ok=True)
        tmp = p["wav"].with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(p["wav"])

    try:
        if use_ctx:
            scenes: Dict[int, List[dict]] = {}
            for p in todo:
                scenes.setdefault(p["line"].scene, []).append(p)

            def run_scene(ps):
                section = str(uuid.uuid4())
                for p in ps:
                    one(p, section)
            with ThreadPoolExecutor(int(workers or cfg["并发"])) as ex:
                list(ex.map(run_scene, scenes.values()))
        else:
            with ThreadPoolExecutor(int(workers or cfg["并发"])) as ex:
                list(ex.map(one, todo))
    except ark.ArkError as e:
        raise Fail(ark.explain(e))

    pcm_all = bytearray()
    t, prev, rows, srt = 0.0, None, [], []
    for p in plan:
        ln = p["line"]
        gap = core.pause_ms(prev, ln)
        pcm_all.extend(b"\x00\x00" * int(RATE * gap / 1000))
        t += gap / 1000
        data = wav_pcm(p["wav"])
        dur = len(data) / 2 / RATE
        pcm_all.extend(data)
        rows.append({"行": ln.no, "角色": ln.role, "音色": p["voice"], "指示": ln.cue, "文本": ln.text,
                     "起": round(t, 3), "时长": round(dur, 3), "缓存": p["key"]})
        srt.append((t, t + dur, ln.text))
        t += dur
        prev = ln
    pcm_all.extend(b"\x00\x00" * int(RATE * 0.8))
    raw = book.ab / "工作" / f"第{n:03d}章.raw.wav"
    raw.write_bytes(_wav_bytes(bytes(pcm_all)))
    mp3 = book.product(n, ".mp3")
    mp3.parent.mkdir(parents=True, exist_ok=True)
    loudnorm(raw, mp3, cfg["响度LUFS"], cfg["峰值dBTP"], cfg["码率"])
    raw.unlink()
    book.product(n, ".srt").write_text("\n".join(
        f"{i}\n{ts(a)} --> {ts(b)}\n{txt}\n" for i, (a, b, txt) in enumerate(srt, 1)), encoding="utf-8")
    flags = speed_flags(rows)
    manifest = {"章节": n, "标题": book.title(n), "有声稿sha256": core.sha256_text(read(book.script_path(n))),
                "生成时间": now(), "时长秒": round(t, 1),
                "本次合成行数": len(todo), "本次计费字数": billed[0], "语速异常": flags, "行": rows}
    book.product(n, ".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"第 {n} 章成品：{rel(book, mp3)}（{t / 60:.1f} 分钟，{len(plan)} 行）")
    print(f"本次合成 {len(todo)} 行、计费 {billed[0]} 字、约 {billed[0] * ark.AFP_PER_10K_CHARS / 10000:.0f} AFP；"
          f"缓存命中 {len(plan) - len(todo)} 行；用时 {time.time() - started:.0f} 秒")
    print(f"字幕：{rel(book, book.product(n, '.srt'))}；逐句清单：{rel(book, book.product(n, '.json'))}")
    if flags:
        print("语速明显偏离同角色中位的行（建议试听，必要时 --reroll）：" +
              "、".join(f"第{f['行']}行({f['偏离']:+.0%})" for f in flags[:15]))
    return 0


def speed_flags(rows: List[dict], limit: float = 0.35) -> List[dict]:
    by: Dict[str, List[float]] = {}
    for r in rows:
        if len(core.norm(r["文本"])) >= 8 and r["时长"] > 0:
            by.setdefault(r["角色"].split("·")[0], []).append(len(core.norm(r["文本"])) / r["时长"])
    med = {k: statistics.median(v) for k, v in by.items() if len(v) >= 3}
    out = []
    for r in rows:
        base = r["角色"].split("·")[0]
        n = len(core.norm(r["文本"]))
        if base in med and n >= 8 and r["时长"] > 0:
            dev = (n / r["时长"]) / med[base] - 1
            if abs(dev) > limit:
                out.append({"行": r["行"], "角色": r["角色"], "偏离": round(dev, 2)})
    return out


def ts(x: float) -> str:
    h, m, s = int(x // 3600), int(x % 3600 // 60), x % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


# ---------------------------------------------------------------- 状态与自检

def chapter_status(book: Book, n: int) -> dict:
    ok, why = committed_state(book, n)
    st = {"章": n, "定稿": "是" if ok else "否", "有声稿": "无", "成品": "无"}
    sp = book.script_path(n)
    if sp.exists():
        meta = core.parse(read(sp)).meta
        fresh = meta.get("正文sha256") == core.sha256_text(read(book.chapter_file(n)))
        st["有声稿"] = "最新" if fresh else "过期"
        man = book.product(n, ".json")
        if man.exists():
            st["成品"] = "最新" if fresh and product_fresh(book, n, json.loads(read(man))) else "过期"
    return st


def product_fresh(book: Book, n: int, manifest: dict) -> bool:
    """成品是否与当前有声稿和选角一致：逐句缓存键全部相同即最新。"""
    try:
        sc = core.parse(read(book.script_path(n)))
        keys = [p["key"] for p in plan_lines(book, sc, bool(book.config().get("场景上下文")))]
    except (KeyError, core.ScriptError):
        return False
    return keys == [r.get("缓存") for r in manifest.get("行", [])]


def cmd_status(book: Book) -> int:
    cast = book.cast()
    print(f"作品：{book.root}；选角：{len([r for r in cast.get('角色', {}).values() if not r.get('自动')])} 个角色"
          f"（版本 {cast.get('版本', '—')}）")
    for n in book.chapters():
        s = chapter_status(book, n)
        print(f"  第{n:03d}章  定稿 {s['定稿']}  有声稿 {s['有声稿']}  成品 {s['成品']}")
    return 0


def cmd_doctor(book: Book, ping: bool) -> int:
    bad = 0
    print(f"Python {sys.version.split()[0]}")
    for b in ("ffmpeg", "ffprobe"):
        ok = bool(shutil.which(b))
        bad += not ok
        print(f"{'✓' if ok else '✗'} {b}" + ("" if ok else "（brew install ffmpeg）"))
    ok = ark.has_key()
    bad += not ok
    print(f"{'✓' if ok else '✗'} 方舟 Agent Plan key（{ark.KEY_NAME}，只检查是否存在，不显示）")
    print(f"✓ 作品目录 {book.root}（正文 {len(book.chapters())} 章，角色卡 {len(roster(book))} 张）")
    cast = book.cast()
    print(f"{'✓' if cast.get('角色') else '·'} 选角表：{len(cast.get('角色', {}))} 个声部")
    if ping and ok:
        try:
            ark.tts("测试", "zh_female_xiaohe_uranus_bigtts")
            print("✓ 语音合成可用（合成 2 字）")
        except ark.ArkError as e:
            bad += 1
            print("✗ " + ark.explain(e))
    return 1 if bad else 0


def cmd_voices(words: List[str]) -> int:
    for v in catalog():
        blob = f"{v['音色名']} {v['性别']} {v['场景']} {v['voice']}"
        if all(w in blob for w in words):
            print(f"{v['音色名']:<10}{v['性别']}  {v['场景']:<6} {v['voice']}" + ("  ［避用］" if v.get("避用") else ""))
    return 0


def chapters_arg(specs: List[str]) -> List[int]:
    out: List[int] = []
    for s in specs:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", s)
        if not m:
            raise SystemExit(f"章节写法应为 N 或 N-M：{s}")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        out += list(range(a, b + 1))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="story-audiobook：定稿章节 → 多角色有声书")
    ap.add_argument("--book", help="作品根目录（包含 正文/）；默认当前目录")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--ping", action="store_true")
    sub.add_parser("status")
    v = sub.add_parser("voices")
    v.add_argument("words", nargs="*")
    c = sub.add_parser("cast")
    cs = c.add_subparsers(dest="action", required=True)
    p = cs.add_parser("propose")
    p.add_argument("--roles")
    p.add_argument("--pov", choices=["auto", "第一人称", "第三人称"], default="auto")
    a = cs.add_parser("audition")
    a.add_argument("--roles")
    a.add_argument("--announcer", default="zh_female_xiaohe_uranus_bigtts")
    lk = cs.add_parser("lock")
    lk.add_argument("picks", nargs="*")
    lk.add_argument("--all-first", action="store_true")
    lk.add_argument("--relock", action="store_true")
    st = cs.add_parser("set")
    st.add_argument("name")
    st.add_argument("--voice")
    st.add_argument("--fx")
    st.add_argument("--语速", dest="rate", type=int)
    st.add_argument("--音量", dest="loud", type=int)
    st.add_argument("--音高", dest="pitch", type=int)
    st.add_argument("--relock", action="store_true")
    cs.add_parser("show")
    for name in ("annotate", "check", "synth", "make"):
        x = sub.add_parser(name)
        x.add_argument("chapters", nargs="+")
        x.add_argument("--allow-uncommitted", action="store_true")
        if name in ("annotate", "make"):
            x.add_argument("--force", action="store_true", help="整章重新标注，不复用旧有声稿")
        if name in ("synth", "make"):
            x.add_argument("--reroll", default="", help="重新合成这些行（逗号分隔行号），换一次表演")
            x.add_argument("--dry-run", action="store_true", help="只估算要合成的字数和 AFP")
            x.add_argument("--workers", type=int)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "voices":
            return cmd_voices(args.words)
        book = Book.find(args.book)
        if args.cmd == "doctor":
            return cmd_doctor(book, args.ping)
        if args.cmd == "status":
            return cmd_status(book)
        if args.cmd == "cast":
            roles = [r.strip() for r in (getattr(args, "roles", None) or "").split(",") if r.strip()] or None
            if args.action == "propose":
                return cmd_cast_propose(book, roles, args.pov)
            if args.action == "audition":
                return cmd_cast_audition(book, roles, args.announcer)
            if args.action == "lock":
                return cmd_cast_lock(book, args.picks, args.all_first, args.relock)
            if args.action == "set":
                return cmd_cast_set(book, args.name, args.voice, args.fx, args.rate, args.loud, args.pitch, args.relock)
            return cmd_cast_show(book)
        rc = 0
        for n in chapters_arg(args.chapters):
            if args.cmd == "annotate":
                annotate(book, n, args.force, args.allow_uncommitted)
            elif args.cmd == "check":
                _, errors, _ = check(book, n)
                rc |= 1 if errors else 0
            else:
                reroll = [int(x) for x in args.reroll.split(",") if x.strip().isdigit()]
                if args.cmd == "make":
                    sp = book.script_path(n)
                    stale = not sp.exists() or core.parse(read(sp)).meta.get("正文sha256") != \
                        core.sha256_text(read(book.chapter_file(n)))
                    if stale or args.force:
                        annotate(book, n, args.force, args.allow_uncommitted)
                rc |= synth(book, n, reroll, args.dry_run, args.allow_uncommitted, args.workers)
        return rc
    except Fail as e:
        print("✗ " + str(e), file=sys.stderr)
        return 1
    except ark.ArkError as e:
        print("✗ " + ark.explain(e), file=sys.stderr)
        return 1
    except core.ScriptError as e:
        print("✗ " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
