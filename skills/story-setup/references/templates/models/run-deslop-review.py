#!/usr/bin/env python3
"""deslop-reviewer 转发：用方舟上的模型（默认豆包）对候选章做去AI味独立审查。

由 story-setup 在启用模型分工的“去AI味审查”角色时部署到项目 .claude/models/；
模型与思考档位读 .claude/models/config.json 的 deslop 段。

用法：
  run-deslop-review.py start <审查任务文件>   启动一次审查，最多等待 WAIT 秒
  run-deslop-review.py audit <审查任务文件>   /审查（story-review）的文字质量审查：只审不改
  run-deslop-review.py wait  <运行ID>         继续等待尚未结束的审查

start 的审查任务文件就是 `chapter_candidate.py review-packet --kind deslop` 输出的 prompt 原文；
audit 的任务文件是 story-review 发给文字审查员的 prompt 原文，脚本从中取出“项目路径”和正文文件路径，
把正文内联后交给豆包，回复原样交回（VERDICT / FINDINGS / RECOMMENDATIONS）。

流程：解析任务里的项目目录与工作副本 → 找到同目录的 deslop.packet.json，核对校验码与原稿指纹
→ 内联去味规范、细纲与工作副本调用豆包 → 逐条套用替换清单到工作副本 → 校验报告引文
→ 标准输出末尾给出 json 报告块，由主会话原样存为报告后 attest。

结束时退出码 0 表示审查完成；超过等待时间仍在运行时输出 “RUNNING <运行ID>”，退出码 75。

环境变量（覆盖 config.json）：
  DESLOP_REVIEWER_MODEL   默认 doubao-seed-2-1-pro-260915
  DESLOP_REVIEWER_EFFORT  思考档位 low|medium|high，默认 high
  DESLOP_REVIEWER_WAIT    单次等待秒数，默认 540（低于 Bash 工具 600 秒上限）
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARK_DIR = ROOT / ".claude" / "models"
RUNS = ARK_DIR / "runs"
ARK = ARK_DIR / "ark_client.py"
_CFG = json.loads((ARK_DIR / "config.json").read_text(encoding="utf-8")).get("deslop", {}) \
    if (ARK_DIR / "config.json").exists() else {}
MODEL = os.environ.get("DESLOP_REVIEWER_MODEL", _CFG.get("model", "doubao-seed-2-1-pro-260915"))
EFFORT = os.environ.get("DESLOP_REVIEWER_EFFORT", _CFG.get("effort", "high"))
WAIT = int(os.environ.get("DESLOP_REVIEWER_WAIT", "540"))

# 去味规范按顺序找：项目内的 story-deslop 副本（generic/openclaw 部署才有），
# 否则用 story-setup 部署给每个 Claude 项目的 agent-references 副本；找不到的跳过。
REFERENCE_NAMES = ["SKILL.md", "anti-ai-writing.md", "fiction-protection-ledger.md", "pattern-governance.md",
                   "structural-audit.md", "banned-words.md", "dialogue-craft-moderate.md"]


def reference_files(project):
    deslop = project / "skills" / "story-deslop"
    shared = project / ".claude" / "skills" / "story-setup" / "references" / "agent-references"
    found = []
    for name in REFERENCE_NAMES:
        for candidate in (deslop / name, deslop / "references" / name, shared / name):
            if name != "SKILL.md" and candidate == deslop / name:
                continue
            if candidate.is_file():
                found.append(candidate)
                break
    card = project / "设定" / "题材正文提示卡.md"
    if card.is_file():
        found.append(card)
    return found
MIN_QUOTE = 4
LATIN = re.compile(r"[A-Za-z]")


class ReviewError(Exception):
    pass


def squash(text):
    return re.sub(r"\s+", "", text)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def field(task, name):
    match = re.search(rf"^{name}：(.+)$", task, re.M)
    if not match:
        raise ReviewError(f"审查任务里找不到“{name}”")
    return match.group(1).split("（")[0].strip()


def build_system(project, intro="deslop-reviewer.system.md"):
    parts = [(ARK_DIR / intro).read_text(encoding="utf-8"), "\n\n# 内联规范\n"]
    for ref in reference_files(project):
        rel = ref.relative_to(project).as_posix() if ref.is_relative_to(project) else ref.name
        parts.append(f"\n\n---\n\n## 文件：{rel}\n\n{ref.read_text(encoding='utf-8')}")
    return "".join(parts)


def build_prompt(task, outline, original, feedback=""):
    prompt = (
        f"# 审查任务（调用方原文）\n\n{task}\n\n"
        "说明：上面任务里“完成后在回复末尾给出 json 报告”的格式，以系统提示里的输出格式为准"
        "（edits + report），调用脚本会据此生成最终报告。\n\n"
        f"# 细纲\n\n{outline}\n\n"
        f"# 工作副本（改动前原文）\n\n{original}\n"
    )
    if feedback:
        prompt += f"\n\n# 上一次输出的问题（请修正后重新完整输出）\n\n{feedback}\n"
    return prompt


def call_model(run, prompt, attempt):
    (run / f"prompt{attempt}.md").write_text(prompt, encoding="utf-8")
    out = run / f"reply{attempt}.md"
    proc = subprocess.run(
        [sys.executable, str(ARK), "run", "-s", str(run / "system.md"), "-p", str(run / f"prompt{attempt}.md"),
         "-o", str(out), "-m", MODEL, "-e", EFFORT, "--max-tokens", "32000", "--timeout", "900"],
        capture_output=True, text=True,
    )
    log = (proc.stdout + proc.stderr).strip()
    with open(run / "log.txt", "a", encoding="utf-8") as f:
        f.write(f"[第{attempt}次调用] {log}\n")
    if proc.returncode != 0:
        raise ReviewError(f"模型调用失败：{log}")
    return out.read_text(encoding="utf-8")


def parse_reply(reply):
    blocks = re.findall(r"```json\s*(.*?)```", reply, re.S)
    raw = blocks[-1] if blocks else reply.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"输出不是合法 json：{exc}")
    if not isinstance(data, dict) or not isinstance(data.get("report"), dict):
        raise ReviewError("输出缺少 report 对象")
    if not isinstance(data.get("edits", []), list):
        raise ReviewError("edits 必须是数组")
    return data


def check_quotes(report, original):
    haystack = squash(original)
    bad = []
    for i, item in enumerate(report.get("findings") or []):
        quote = item.get("quote") if isinstance(item, dict) else None
        if not isinstance(quote, str) or len(squash(quote)) < MIN_QUOTE or squash(quote) not in haystack:
            bad.append(f"findings[{i}].quote 不是工作副本原文的逐字摘录：{quote!r}")
    return bad


def apply_edits(edits, text):
    applied, notes = [], []
    for i, edit in enumerate(edits):
        old = edit.get("old") if isinstance(edit, dict) else None
        new = edit.get("new", "") if isinstance(edit, dict) else None
        if not isinstance(old, str) or not old or not isinstance(new, str):
            notes.append(f"第{i + 1}条替换格式无效，未套用")
            continue
        if LATIN.search(new) and not LATIN.search(old):
            notes.append(f"第{i + 1}条替换引入了拉丁字母，未套用：{new[:30]}")
            continue
        count = text.count(old)
        if count != 1:
            notes.append(f"第{i + 1}条替换的原文在工作副本中出现 {count} 次，未套用：{old[:30]}")
            continue
        text = text.replace(old, new, 1)
        applied.append(old)
    return text, applied, notes


def review(run):
    task = (run / "task.md").read_text(encoding="utf-8")
    project = Path(field(task, "项目目录"))
    work = project / field(task, "工作副本")
    packet_path = work.parent.parent / "deslop.packet.json"
    if not packet_path.exists():
        raise ReviewError(f"找不到审查包 {packet_path}")
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    if packet.get("kind") != "deslop" or packet.get("nonce") not in task:
        raise ReviewError("审查任务与当前审查包的校验码不一致：请重新发审查包")
    if sha256(work) != packet["candidate_sha256"]:
        raise ReviewError("工作副本已不是审查包发出时的原稿（可能已被改过）：请重新发审查包后再审")

    original = work.read_text(encoding="utf-8")
    (run / "work.orig.md").write_text(original, encoding="utf-8")
    outline_path = project / packet["outline"]
    outline = outline_path.read_text(encoding="utf-8") if outline_path.exists() else "（细纲文件缺失）"
    (run / "system.md").write_text(build_system(project), encoding="utf-8")

    data, feedback = None, ""
    for attempt in (1, 2):
        reply = call_model(run, build_prompt(task, outline, original, feedback), attempt)
        try:
            data = parse_reply(reply)
        except ReviewError as exc:
            feedback = str(exc)
            data = None
            continue
        bad = check_quotes(data["report"], original)
        if not bad:
            break
        feedback = "\n".join(bad)
    if data is None:
        raise ReviewError(f"两次输出都无法解析：{feedback}")

    report_in = data["report"]
    notes = []
    findings = []
    for item in report_in.get("findings") or []:
        if check_quotes({"findings": [item]}, original):
            notes.append(f"剔除一条引文不合规的发现：{str(item.get('quote'))[:30]}")
            continue
        findings.append(item)

    text, applied, edit_notes = apply_edits(data.get("edits") or [], original)
    notes += edit_notes
    if edit_notes:
        # 没套上的替换对应的发现，降为 flagged，避免报告声称改了实际没改的地方
        applied_squashed = [squash(a) for a in applied]
        for item in findings:
            if item.get("action") == "edited" and not any(squash(item["quote"]) in a or a in squash(item["quote"])
                                                         for a in applied_squashed):
                item["action"] = "flagged"
    if text != original:
        work.write_text(text, encoding="utf-8")

    summary = str(report_in.get("summary", ""))
    if notes:
        summary += "（转发脚本备注：" + "；".join(notes) + "）"
    report = {
        "kind": "deslop",
        "nonce": packet["nonce"],
        "candidate_sha256": packet["candidate_sha256"],
        "scope": packet["scope"],
        "verdict": report_in.get("verdict"),
        "findings": findings,
        "coverage": report_in.get("coverage") or [],
        "summary": summary,
    }
    lines = [
        f"去AI味审查完成（模型 {MODEL}，思考 {EFFORT}）。",
        f"套用替换 {len(applied)} 条，未套用 {len(edit_notes)} 条；工作副本{'已更新' if text != original else '未改动'}。",
    ]
    lines += [f"- {n}" for n in notes]
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(report, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines)


AUDIT_MAX_CHARS = 300_000


def audit(run):
    task = (run / "task.md").read_text(encoding="utf-8")
    project = Path(field(task, "项目路径"))
    if not project.is_dir():
        raise ReviewError(f"项目路径不存在：{project}")
    files, total = [], 0
    for token in dict.fromkeys(re.findall(r"[^\s`'\"“”‘’：:，,；;（）()【】\[\]{}<>]+\.md", task)):
        path = (project / token).resolve() if not Path(token).is_absolute() else Path(token).resolve()
        if not path.is_file() or not path.is_relative_to(project):
            continue
        rel = path.relative_to(project).as_posix()
        if rel.startswith(("skills/", ".claude/", ".codex/")):
            continue  # 参考文件已由系统提示内联，不当作审查对象
        text = path.read_text(encoding="utf-8")
        total += len(text)
        if total > AUDIT_MAX_CHARS:
            raise ReviewError(f"审查范围超过 {AUDIT_MAX_CHARS} 字符，请主会话分批审查")
        files.append((rel, text))
    if not files:
        raise ReviewError("任务里没有找到项目内可读的正文文件路径（审查范围需写明相对路径）")
    (run / "system.md").write_text(build_system(project, "deslop-auditor.system.md"), encoding="utf-8")
    body = "\n\n".join(f"## 文件：{rel}\n\n" + "\n".join(f"{i:>4}  {line}" for i, line in enumerate(text.splitlines(), 1))
                       for rel, text in files)
    prompt = f"# 审查任务（调用方原文）\n\n{task}\n\n# 审查对象全文（行首数字是原始行号）\n\n{body}\n"
    reply = call_model(run, prompt, 1)
    header = f"文字质量审查完成（模型 {MODEL}，思考 {EFFORT}；审查文件：{'、'.join(rel for rel, _ in files)}）。"
    return header + "\n\n" + reply.strip()


def worker(run):
    mode = (run / "mode").read_text().strip() if (run / "mode").exists() else "start"
    try:
        output, code = (audit(run) if mode == "audit" else review(run)), 0
    except ReviewError as exc:
        output, code = f"去AI味审查失败：{exc}", 1
    except Exception as exc:  # noqa: BLE001 - 失败原因必须交回调用方
        output, code = f"去AI味审查异常：{type(exc).__name__}: {exc}", 1
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


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "_work":
        worker(Path(sys.argv[2]))
        return
    if len(sys.argv) == 3 and sys.argv[1] in ("start", "audit"):
        task = Path(sys.argv[2])
        if not task.is_file() or not task.read_text(encoding="utf-8").strip():
            sys.exit(f"错误：任务文件不存在或为空：{task}")
        run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        run = RUNS / run_id
        run.mkdir(parents=True)
        (run / "task.md").write_text(task.read_text(encoding="utf-8"), encoding="utf-8")
        # 任务里没有“工作副本：”行就不是接纳前去味审查包，自动按只审不改的 audit 处理，防止误改文件
        mode = sys.argv[1] if re.search(r"^工作副本：", task.read_text(encoding="utf-8"), re.M) else "audit"
        (run / "mode").write_text(mode)
        subprocess.Popen([sys.executable, __file__, "_work", str(run)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await_run(run_id)
    if len(sys.argv) == 3 and sys.argv[1] == "wait":
        await_run(sys.argv[2])
    sys.exit("用法：run-deslop-review.py start|audit <审查任务文件> | wait <运行ID>")


if __name__ == "__main__":
    main()
