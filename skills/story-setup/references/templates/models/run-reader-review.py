#!/usr/bin/env python3
"""逐章读者审读：让模型扮演目标平台读者顺读到本章，给出读感。只作建议，不卡定稿。

由 story-setup 在启用模型分工的“读者审读”角色时部署到项目 .claude/models/；车道、总控与书城信息读
.claude/models/config.json 的 reader 段。书名、标签、平台留空时从 设定/题材定位.md 读取。

读者只看得到：书名、标签、读者画像、已接纳的前文正文、本章正文，以及这位读者上一章留下的读书笔记。
不给细纲、设定、追踪文件；外部命令行都在项目外的临时空目录里运行，不加载项目说明和钩子。

用法：
  run-reader-review.py lanes                                   列出可选读者车道
  run-reader-review.py start <章号> [--lane 车道] [--text 正文]  单车道（默认 opus），核心读者画像
  run-reader-review.py panel <章号> --readers gpt,kimi,glm [--text 正文] [--reassign]
                                                               多读者并行：总控分配读者身份，读完由总控汇总
  run-reader-review.py wait <运行ID>                           继续等待尚未结束的审读

不传 --text 时自动选择：本章已有正式正文就读正文，否则读最新的候选稿。
单车道结果：读者审读/第NNN章/{车道}.md|json，并复制为 读者审读/第NNN章.md|json。
多读者结果：每位读者 读者审读/第NNN章/团-{车道}.md|json，汇总写入 读者审读/第NNN章.md|json。
多读者的身份与单车道的核心读者画像都登记在 读者审读/读者团.json（核心读者键为 _core，首次运行由总控
按书城信息生成），以后各章沿用同一身份，读书笔记才接得上；--reassign 重新分配读者团身份。
超过等待时间仍在运行时输出 “RUNNING <运行ID>”，退出码 75。

provider 三种：
  ark     方舟（ark_client.py，Agent Plan；服务端替换模型时会报错）
  codex   Codex 命令行（codex exec，只读沙箱、临时会话）
  claude  Claude Code 命令行（claude -p，关闭全部工具、不保存会话）
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARK_DIR = ROOT / ".claude" / "models"
RUNS = ARK_DIR / "runs"
ARK = ARK_DIR / "ark_client.py"
CONFIG = json.loads((ARK_DIR / "config.json").read_text(encoding="utf-8"))["reader"]
EXAMPLE_PERSONA = ARK_DIR / "reader-persona.example.md"
LANES = CONFIG["lanes"]
CONTROLLER = CONFIG["controller"]
WAIT = int(os.environ.get("READER_REVIEW_WAIT", "540"))
FULL_PRIOR = int(CONFIG.get("full_prior_chapters", 10))
MIN_QUOTE = 6
LIST_KEYS = ("enjoyed", "skimmed", "confused_or_pulled_out", "drop_risk")
PANEL_FILE = "读者团.json"


class ReviewError(Exception):
    pass


def book_root():
    marker = ROOT / ".active-book"
    name = marker.read_text(encoding="utf-8").strip() if marker.exists() else "."
    return (ROOT / (name or ".")).resolve()


def squash(text):
    return re.sub(r"\s+", "", text)


def chapter_file(book, n):
    hits = sorted((book / "正文").glob(f"第{n:03d}章*.md")) if (book / "正文").is_dir() else []
    return hits[0] if hits else None


def latest_candidate(book, n):
    runs = sorted((book / "追踪" / "候选章" / f"第{n:03d}章").glob("*/manifest.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for manifest in runs:
        status = json.loads(manifest.read_text(encoding="utf-8")).get("status")
        if status not in ("abandoned", "promoted") and (manifest.parent / "candidate.md").exists():
            return manifest.parent / "candidate.md"
    return None


def pick_text(book, n, given):
    if given:
        path = Path(given).resolve()
        if not path.is_file():
            raise ReviewError(f"正文文件不存在：{given}")
        return path
    path = chapter_file(book, n) or latest_candidate(book, n)
    if path is None:
        raise ReviewError(f"第{n}章既没有正式正文，也没有可读的候选稿")
    return path


def store_info():
    """书城页面信息：config 里填了就用，留空的从 设定/题材定位.md 的基本信息读。"""
    title, tags, platform = CONFIG.get("book_title", ""), list(CONFIG.get("tags") or []), CONFIG.get("platform", "")
    card = book_root() / "设定" / "题材定位.md"
    text = card.read_text(encoding="utf-8") if card.is_file() else ""
    def field(*names):
        for name in names:
            match = re.search(rf"{name}[^：:\n]*[：:]\s*\**\s*(.+)", text)
            if match:
                return match.group(1).strip().strip("*").strip()
        return ""
    if not title:
        raw = field("书名")
        named = re.search(r"《([^》]+)》", raw)
        title = named.group(1) if named else raw.split("（")[0].strip()
    if not tags:
        raw = re.sub(r"[（(][^）)]*[）)]", "", field("题材类型", "题材"))
        tags = [t.strip() for t in re.split(r"[+＋、/，,]", raw) if t.strip()][:6]
    if not platform:
        platform = field("目标平台", "平台").split("（")[0].strip()
    return title or "（未命名）", tags, platform or "未注明"


def store_page():
    title, tags, platform = store_info()
    return f"书名：{title}\n标签：{'、'.join(tags) if tags else '（无）'}\n平台：{platform}"


# ---------- 模型调用 ----------

def call(cfg, system, prompt, workdir, name):
    """按车道配置调用一次模型，返回回复全文。提示词与回复存在 workdir 里留档。"""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / f"{name}.system.md").write_text(system, encoding="utf-8")
    (workdir / f"{name}.prompt.md").write_text(prompt, encoding="utf-8")
    out = workdir / f"{name}.reply.md"
    provider, model, effort = cfg["provider"], cfg["model"], cfg.get("effort", "medium")
    isolated = Path(tempfile.mkdtemp(prefix="reader-review-"))
    if provider == "ark":
        cmd, stdin = [sys.executable, str(ARK), "run", "-s", str(workdir / f"{name}.system.md"),
                      "-p", str(workdir / f"{name}.prompt.md"), "-o", str(out), "-m", model, "-e", effort,
                      "--max-tokens", "16000", "--timeout", "900"], None
    elif provider == "codex":
        cmd, stdin = ["codex", "exec", "-m", model, "-c", f"model_reasoning_effort={effort}", "-s", "read-only",
                      "--skip-git-repo-check", "--ephemeral", "-C", str(isolated), "-o", str(out), "-"], \
            system + "\n\n---\n\n" + prompt
    elif provider == "claude":
        cmd, stdin = ["claude", "-p", "--model", model, "--effort", effort, "--tools", "",
                      "--no-session-persistence", "--system-prompt", system], prompt
    else:
        raise ReviewError(f"未知 provider：{provider}")
    started = time.time()
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, cwd=isolated, timeout=1200)
    except subprocess.TimeoutExpired:
        raise ReviewError(f"{cfg.get('label', model)} 调用超时")
    finally:
        shutil.rmtree(isolated, ignore_errors=True)
    if provider == "claude" and proc.returncode == 0:
        out.write_text(proc.stdout, encoding="utf-8")
    log = (proc.stdout + proc.stderr if provider == "ark" else proc.stderr).strip()[-1500:] or "（无日志输出）"
    with open(workdir / "log.txt", "a", encoding="utf-8") as f:
        f.write(f"[{name}，{round(time.time() - started)} 秒] {log}\n")
    if proc.returncode != 0 or not out.exists():
        raise ReviewError(f"{cfg.get('label', model)} 调用失败：{log}")
    return out.read_text(encoding="utf-8")


def parse_json(reply):
    blocks = re.findall(r"```json\s*(.*?)```", reply, re.S)
    try:
        return json.loads(blocks[-1] if blocks else reply.strip())
    except json.JSONDecodeError as exc:
        raise ReviewError(f"输出不是合法 json：{exc}")


# ---------- 单个读者 ----------

def reader_problems(data, text):
    if not isinstance(data, dict) or not isinstance(data.get("continue_reading"), dict):
        return ["输出缺少 continue_reading"]
    problems = []
    score = data["continue_reading"].get("score")
    if not isinstance(score, (int, float)) or not 1 <= score <= 10:
        problems.append(f"continue_reading.score 必须是 1 到 10 的数字，当前是 {score!r}")
    haystack = squash(text)
    items = [(key, item) for key in LIST_KEYS for item in (data.get(key) or [])]
    if isinstance(data.get("chapter_end"), dict):
        items.append(("chapter_end", data["chapter_end"]))
    for key, item in items:
        quote = item.get("quote") if isinstance(item, dict) else None
        if not isinstance(quote, str) or len(squash(quote)) < MIN_QUOTE or squash(quote) not in haystack:
            problems.append(f"{key} 的 quote 不是本章原文的逐字摘录：{quote!r}")
    return problems


def reader_prompt(book, n, text, reader_id):
    parts = [f"# 书城页面\n\n{store_page()}\n"]
    notes_path = book / "读者审读" / f"第{n - 1:03d}章" / f"{reader_id}.json"
    if n > 1 and notes_path.exists():
        memory = json.loads(notes_path.read_text(encoding="utf-8")).get("reader_memory", {})
        parts.append("# 你之前的读书笔记（读完上一章时写的）\n\n" + json.dumps(memory, ensure_ascii=False, indent=2) + "\n")
    prior = []
    for k in range(max(1, n - FULL_PRIOR), n):
        path = chapter_file(book, k)
        if path is None:
            raise ReviewError(f"第{k}章还没有正式正文，无法顺读到第{n}章")
        prior.append(path.read_text(encoding="utf-8").strip())
    if n - FULL_PRIOR > 1:
        parts.append(f"（更早的第1至{n - FULL_PRIOR - 1}章你已经读过，印象见上面的读书笔记。）\n")
    if prior:
        parts.append("# 你已经读过的章节\n\n" + "\n\n".join(prior) + "\n")
    parts.append("# 本章（刚读完的这一章）\n\n" + text.strip() + "\n")
    return "\n".join(parts)


def run_reader(book, n, text, lane, reader_id, persona_name, persona, workdir):
    cfg = LANES[lane]
    system = (ARK_DIR / "reader-review.system.md").read_text(encoding="utf-8") + "\n\n---\n\n" + persona
    prompt = reader_prompt(book, n, text, reader_id)
    data, feedback = None, ""
    for attempt in (1, 2):
        extra = f"\n\n# 上一次输出的问题（请修正后重新完整输出）\n\n{feedback}\n" if feedback else ""
        try:
            data = parse_json(call(cfg, system, prompt + extra, workdir, f"{reader_id}-{attempt}"))
        except ReviewError as exc:
            if "调用失败" in str(exc) or "超时" in str(exc):
                raise
            feedback, data = str(exc), None
            continue
        problems = reader_problems(data, text)
        if not problems:
            break
        feedback = "\n".join(problems)
    if data is None or not isinstance(data.get("continue_reading"), dict):
        raise ReviewError(f"{cfg['label']} 两次输出都不合格：{feedback}")
    score = data["continue_reading"].get("score")
    if not isinstance(score, (int, float)) or not 1 <= score <= 10:
        raise ReviewError(f"{cfg['label']} 两次给出的分数都不在 1 到 10 之间")
    notes, haystack = [], squash(text)
    for key in LIST_KEYS:
        kept = []
        for item in data.get(key) or []:
            quote = item.get("quote", "") if isinstance(item, dict) else ""
            if len(squash(quote)) >= MIN_QUOTE and squash(quote) in haystack:
                kept.append(item)
            else:
                notes.append(f"剔除一条引文不是原文的“{key}”：{str(quote)[:30]}")
        data[key] = kept
    data["_reader"] = {"id": reader_id, "lane": lane, "name": persona_name, "label": cfg["label"],
                       "provider": cfg["provider"], "model": cfg["model"], "effort": cfg.get("effort", "medium"),
                       "notes": notes}
    return data


def render_reader(n, data, source, sha):
    r = data["_reader"]
    cont = data.get("continue_reading") or {}
    end = data.get("chapter_end") or {}
    lines = [
        f"# 第{n}章 读者审读 · {r['name']}",
        "",
        f"> 读者：{r['name']}；模型 {r['label']}（{r['provider']} · {r['model']}，思考 {r['effort']}）。只作建议，不卡定稿。",
        f"> 读的是 `{source}`（SHA-256 {sha[:12]}）。",
        "",
        f"**一句话读后感**：{data.get('first_impression', '')}",
        "",
        f"**追读意愿**：{cont.get('score', '?')} / 10，{'会' if cont.get('will_click_next') else '不会'}点下一章。{cont.get('why', '')}",
        "",
    ]
    titles = {"enjoyed": "读得爽的地方", "skimmed": "想跳过的地方", "confused_or_pulled_out": "没看懂或出戏的地方",
              "drop_risk": "想划走的瞬间"}
    for key, title in titles.items():
        lines += [f"## {title}", ""]
        items = data.get(key) or []
        lines += [f"- “{i.get('quote', '')}”：{i.get('feeling') or i.get('why') or ''}" for i in items] or ["无"]
        lines.append("")
    lines += ["## 章尾", "",
              f"- “{end.get('quote', '')}”：{'想' if end.get('want_next') else '不想'}翻页。{end.get('why', '')}",
              "", "## 读者的期待与猜测", ""]
    memory = data.get("reader_memory") or {}
    for label, key in (("期待", "expect"), ("猜测", "guess")):
        lines += [f"- {label}：{item}" for item in memory.get(key) or []]
    lines += ["", "## 给作者的话", ""]
    lines += [f"- {s}" for s in data.get("suggestions") or []] or ["无"]
    if r["notes"]:
        lines += ["", "## 脚本备注", ""] + [f"- {x}" for x in r["notes"]]
    return "\n".join(lines) + "\n"


def save_reader(book, n, data, source, sha):
    folder = book / "读者审读" / f"第{n:03d}章"
    folder.mkdir(parents=True, exist_ok=True)
    data["_meta"] = {"chapter": n, "source": source, "source_sha256": sha,
                     "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    rid = data["_reader"]["id"]
    (folder / f"{rid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_reader(n, data, source, sha)
    (folder / f"{rid}.md").write_text(md, encoding="utf-8")
    return md


# ---------- 总控：分配读者身份、汇总 ----------

def load_panel(book):
    panel_path = book / "读者审读" / PANEL_FILE
    return json.loads(panel_path.read_text(encoding="utf-8")) if panel_path.exists() else {}


def save_panel(book, panel):
    panel_path = book / "读者审读" / PANEL_FILE
    panel_path.parent.mkdir(parents=True, exist_ok=True)
    panel_path.write_text(json.dumps(panel, ensure_ascii=False, indent=2), encoding="utf-8")


def core_persona(book, workdir):
    """单车道用的核心读者画像：首次由总控按书城信息生成，登记为 _core 后各章沿用。"""
    panel = load_panel(book)
    if "_core" in panel:
        return panel["_core"]
    prompt = (
        "你是一本网文读者测试的总控。请写出这本书在该平台上最核心的那类读者的画像。\n\n"
        f"# 书城页面\n\n{store_page()}\n\n"
        f"# 读者画像写法示例（另一本书的核心读者，只参考结构与口吻）\n\n{EXAMPLE_PERSONA.read_text(encoding='utf-8')}\n\n"
        "# 要求\n\n你只知道书名、标签和平台，不要编造剧情或设定。按示例的结构写（你是谁、为什么点开这本书、"
        "想要什么、讨厌什么、怎么读），用第二人称“你”，三百字左右。\n\n只输出一个 ```json 代码块：\n"
        '{"name": "简短身份名，例如二十出头男大学生", "persona": "画像全文"}'
    )
    data = parse_json(call(CONTROLLER, "你是严谨的读者研究员，只按要求输出 json。", prompt, workdir, "总控-核心读者"))
    if not isinstance(data, dict) or not data.get("name") or not data.get("persona"):
        raise ReviewError("总控没有给出核心读者画像")
    panel["_core"] = {"name": data["name"], "persona": data["persona"]}
    save_panel(book, panel)
    return panel["_core"]


def assign_personas(book, lanes, reassign, workdir):
    panel = load_panel(book)
    if reassign:
        panel = {key: value for key, value in panel.items() if key.startswith("_")}
    missing = [lane for lane in lanes if lane not in panel]
    if not missing:
        return panel
    example = EXAMPLE_PERSONA.read_text(encoding="utf-8")
    existing = {lane: panel[lane]["name"] for lane in panel if not lane.startswith("_")}
    prompt = (
        "你是一本网文读者测试的总控。请为下面这些读者车道各分配一个该平台读者身份，组成多视角读者团。\n\n"
        f"# 书城页面\n\n{store_page()}\n\n"
        f"# 需要分配身份的车道\n\n{json.dumps(missing, ensure_ascii=False)}\n\n"
        f"# 已有读者身份（沿用，不要重复）\n\n{json.dumps(existing, ensure_ascii=False) if existing else '无'}\n\n"
        f"# 读者画像写法示例（另一本书的核心读者，只参考结构与口吻）\n\n{example}\n\n"
        "# 要求\n\n"
        "1. 每个身份都是会在这个平台的书城点开这本书的真实读者，在年龄、性别、阅读经验、阅读场景和口味上彼此拉开差异"
        "（例如年轻男性核心读者、中年男性、年轻女性、中年女性、看过大量同类书的老读者、很少看网文的新读者）。"
        "读者团里如果还没有这本书最核心的那类读者，先分配一个。\n"
        "2. 你只知道书名和标签，不要编造剧情或设定，也不要预设他们会喜欢或讨厌。\n"
        "3. 画像按示例的结构写（你是谁、为什么点开这本书、想要什么、讨厌什么、怎么读），用第二人称“你”，三百字左右。\n\n"
        "只输出一个 ```json 代码块：\n"
        '{"personas": [{"lane": "车道名", "name": "简短身份名，例如二十出头男大学生", "persona": "画像全文"}]}'
    )
    data = parse_json(call(CONTROLLER, "你是严谨的读者研究员，只按要求输出 json。", prompt, workdir, "总控-分配身份"))
    if not isinstance(data, dict):
        raise ReviewError("总控分配身份的输出不是对象")
    for item in data.get("personas", []):
        if item.get("lane") in missing and item.get("name") and item.get("persona"):
            panel[item["lane"]] = {"name": item["name"], "persona": item["persona"]}
    still = [lane for lane in missing if lane not in panel]
    if still:
        raise ReviewError(f"总控没有给这些车道分配身份：{still}")
    save_panel(book, panel)
    return panel


def summarize(n, results, failures, workdir):
    scores = [r["continue_reading"]["score"] for r in results]
    average = round(sum(scores) / len(scores), 1)
    clicks = sum(1 for r in results if r["continue_reading"].get("will_click_next"))
    digest = [{"读者": r["_reader"]["name"], "模型": r["_reader"]["label"],
               **{k: r.get(k) for k in ("first_impression", "continue_reading", *LIST_KEYS, "chapter_end", "suggestions")}}
              for r in results]
    prompt = (
        f"你是读者测试的总控。下面是 {len(results)} 位不同身份的读者读完第{n}章后的反馈。"
        f"平均追读分 {average} / 10（脚本已算好，不要重算），{clicks}/{len(results)} 人会点下一章。\n\n"
        f"# 读者反馈\n\n{json.dumps(digest, ensure_ascii=False, indent=2)}\n\n"
        "请站在作者的角度汇总：哪些感受是多数读者共有的，哪些只属于某类读者；哪类读者最可能流失、为什么；"
        "作者最值得先处理的意见。不要另外编造读者没有提到的问题。引用原文时照抄读者给的 quote。\n\n"
        "只输出一个 ```json 代码块：\n"
        '{"overall": "两三句综合读后感", "consensus": ["多数读者共有的感受"], '
        '"divergence": [{"point": "分歧点", "detail": "谁怎么看"}], '
        '"churn_risk": "哪类读者最可能流失及原因", "chapter_end": "章尾综合评价", '
        '"priority_suggestions": ["按优先级排列的修改意见，最多 5 条"]}'
    )
    summary = parse_json(call(CONTROLLER, "你是严谨的读者研究员，只按要求输出 json。", prompt, workdir, "总控-汇总"))
    summary["average_score"] = average
    summary["will_click_next"] = f"{clicks}/{len(results)}"
    summary["failures"] = failures
    return summary


def render_summary(n, summary, results, source, sha):
    lines = [
        f"# 第{n}章 读者审读 · 多读者汇总",
        "",
        f"> 读的是 `{source}`（SHA-256 {sha[:12]}）。总控 {CONTROLLER['label']} 分配身份并汇总；只作建议，不卡定稿。",
        "",
        f"**平均追读分**：{summary['average_score']} / 10　　**会点下一章**：{summary['will_click_next']}",
        "",
        f"**综合读后感**：{summary.get('overall', '')}",
        "",
        "| 读者 | 模型 | 追读分 | 点下一章 | 一句话读后感 |",
        "|---|---|---:|:---:|---|",
    ]
    for r in results:
        c = r["continue_reading"]
        lines.append(f"| [{r['_reader']['name']}](第{n:03d}章/{r['_reader']['id']}.md) | {r['_reader']['label']} | "
                     f"{c.get('score')} | {'会' if c.get('will_click_next') else '不会'} | {r.get('first_impression', '')} |")
    lines += ["", "## 共识", ""]
    lines += [f"- {x}" for x in summary.get("consensus") or []] or ["无"]
    lines += ["", "## 分歧", ""]
    lines += [f"- **{d.get('point', '')}**：{d.get('detail', '')}" for d in summary.get("divergence") or []] or ["无"]
    lines += ["", "## 最可能流失的读者", "", summary.get("churn_risk", "") or "无",
              "", "## 章尾", "", summary.get("chapter_end", "") or "无",
              "", "## 优先处理的意见", ""]
    lines += [f"{i}. {s}" for i, s in enumerate(summary.get("priority_suggestions") or [], 1)] or ["无"]
    if summary.get("failures"):
        lines += ["", "## 没跑成的读者", ""] + [f"- {f}" for f in summary["failures"]]
    return "\n".join(lines) + "\n"


# ---------- 任务入口 ----------

def execute(run):
    job = json.loads((run / "job.json").read_text(encoding="utf-8"))
    book, n = book_root(), job["chapter"]
    path = pick_text(book, n, job.get("text"))
    text = path.read_text(encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    source = path.relative_to(book).as_posix() if path.is_relative_to(book) else str(path)
    out_dir = book / "读者审读"

    if job["mode"] == "single":
        lane = job["lane"]
        core = core_persona(book, run)
        data = run_reader(book, n, text, lane, lane, core["name"], core["persona"], run / lane)
        md = save_reader(book, n, data, source, sha)
        data["_meta"]["mode"] = "single"
        (out_dir / f"第{n:03d}章.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / f"第{n:03d}章.md").write_text(md, encoding="utf-8")
        return md

    lanes = job["readers"]
    panel = assign_personas(book, lanes, job.get("reassign"), run)
    results, failures = [], []

    def one(lane):
        rid = f"团-{lane}"
        return lane, run_reader(book, n, text, lane, rid, panel[lane]["name"], panel[lane]["persona"], run / rid)

    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        futures = [pool.submit(one, lane) for lane in lanes]
        for future in futures:
            try:
                lane, data = future.result()
                save_reader(book, n, data, source, sha)
                results.append(data)
            except ReviewError as exc:
                failures.append(str(exc))
    if not results:
        raise ReviewError("所有读者都没跑成：" + "；".join(failures))
    results.sort(key=lambda r: lanes.index(r["_reader"]["lane"]))
    summary = summarize(n, results, failures, run)
    summary["_meta"] = {"chapter": n, "mode": "panel", "source": source, "source_sha256": sha,
                        "readers": [r["_reader"]["id"] for r in results],
                        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    md = render_summary(n, summary, results, source, sha)
    (out_dir / f"第{n:03d}章.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"第{n:03d}章.md").write_text(md, encoding="utf-8")
    return md


def worker(run):
    try:
        output, code = execute(run), 0
    except ReviewError as exc:
        output, code = f"读者审读失败：{exc}", 1
    except Exception as exc:  # noqa: BLE001 - 失败原因必须交回调用方
        output, code = f"读者审读异常：{type(exc).__name__}: {exc}", 1
    (run / "out.txt").write_text(output, encoding="utf-8")
    tmp = run / "exit_code.tmp"
    tmp.write_text(str(code))
    tmp.rename(run / "exit_code")


def await_run(run_id):
    run = RUNS / run_id
    if not run.is_dir():
        sys.exit(f"错误：找不到运行 {run_id}")
    waited = 0
    while not (run / "exit_code").exists():
        if waited >= WAIT:
            print(f"RUNNING {run_id}")
            sys.exit(75)
        time.sleep(3)
        waited += 3
    print((run / "out.txt").read_text(encoding="utf-8"))
    sys.exit(int((run / "exit_code").read_text()))


def launch(job):
    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-reader-{os.getpid()}"
    run = RUNS / run_id
    run.mkdir(parents=True)
    (run / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    subprocess.Popen([sys.executable, __file__, "_work", str(run)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    await_run(run_id)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "_work":
        worker(Path(sys.argv[2]))
        return
    ap = argparse.ArgumentParser(description="逐章读者审读")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lanes", help="列出可选读者车道")
    s = sub.add_parser("start", help="单车道审读")
    s.add_argument("chapter", type=int)
    s.add_argument("--lane", default=CONFIG.get("default_lane", "opus"))
    s.add_argument("--text")
    p = sub.add_parser("panel", help="多读者并行审读")
    p.add_argument("chapter", type=int)
    p.add_argument("--readers", required=True, help="逗号分隔的车道名")
    p.add_argument("--text")
    p.add_argument("--reassign", action="store_true", help="重新分配所有读者身份")
    w = sub.add_parser("wait", help="继续等待")
    w.add_argument("run_id")
    a = ap.parse_args()

    if a.cmd == "lanes":
        panel = load_panel(book_root())
        print(f"书城信息：{store_page().replace(chr(10), '；')}")
        if "_core" in panel:
            print(f"单车道核心读者：{panel['_core']['name']}")
        for name, cfg in LANES.items():
            default = "（单车道默认）" if name == CONFIG.get("default_lane") else ""
            persona = f"  读者团身份：{panel[name]['name']}" if name in panel else ""
            print(f"{name}\t{cfg['label']}（{cfg['provider']} · {cfg['model']} · {cfg.get('effort', 'medium')}）{default}{persona}")
        return
    if a.cmd == "wait":
        await_run(a.run_id)
    if a.chapter < 1:
        sys.exit("错误：章号必须是正整数")
    text = str(Path(a.text).resolve()) if a.text else None
    if a.cmd == "start":
        if a.lane not in LANES:
            sys.exit(f"错误：没有车道 {a.lane}；可选 {', '.join(LANES)}")
        launch({"mode": "single", "chapter": a.chapter, "lane": a.lane, "text": text})
    readers = [x.strip() for x in a.readers.split(",") if x.strip()]
    unknown = [x for x in readers if x not in LANES]
    if unknown or len(readers) < 2 or len(set(readers)) != len(readers):
        sys.exit(f"错误：--readers 需要至少两个不重复的车道；未知车道 {unknown}；可选 {', '.join(LANES)}")
    launch({"mode": "panel", "chapter": a.chapter, "readers": readers, "text": text, "reassign": a.reassign})


if __name__ == "__main__":
    main()
