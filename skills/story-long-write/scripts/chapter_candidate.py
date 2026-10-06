#!/usr/bin/env python3
"""Stage, gate, review, accept, promote, and reconcile one long-form chapter candidate.

Everything that may change the prose happens before acceptance: the deterministic
gates, the punctuation ``fix``, the deslop review (the reviewer edits a copy that this
tool writes back) and the consistency review.  Both reviews are bound to the
candidate's SHA-256 through receipts, and ``approve`` refuses without them, so the
author only ever accepts text that every check has already seen.  After ``promote``
the prose only changes through the revision flow.

Receipts stop a step from being forgotten; they cannot stop a determined host from
faking one.  The per-packet nonce and the verbatim-quote check only raise that cost.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
PROTOCOL = "gated-v2"
LEGACY_PROTOCOL = "legacy-v1"
GATE_REPORT_SCHEMA_VERSION = 1
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CHAPTER_PATTERN = re.compile(r"^第0*(\d+)章(?:_|\.|$)")
OPEN_STATUSES = {"draft", "approved", "promoted"}

REVIEW_KINDS = ("deslop", "consistency")
REVIEW_LABELS = {"deslop": "去味审查", "consistency": "一致性审查"}
REVIEW_AGENTS = {"deslop": "narrative-writer", "consistency": "consistency-checker"}
SEVERITIES = ("S1", "S2", "S3", "S4")
BLOCKING_SEVERITIES = {"S1", "S2"}
VERDICTS = ("PASS", "CONCERNS", "REJECT")
REVIEW_SCOPES = ("full", "lean")
FINDING_ACTIONS = ("edited", "flagged", "none")
MIN_QUOTE_CHARS = 4
MIN_EMPTY_REVIEW_COVERAGE = 3
# 去味审查之后主会话还会为门禁或一致性问题改稿。小修不该让整轮语义审查作废，
# 但改掉超过四分之一时，作者看到的已经不是审过的那一版。
DESLOP_MAX_EDIT_RATIO = 0.25
LEAN_EDGE_PARAGRAPHS = 3

PRESSURES = ("low", "normal", "high")
PRESSURE_ORDER = {level: index for index, level in enumerate(PRESSURES)}
PRESSURE_LABELS = {"low": "低压", "normal": "常规", "high": "高压"}
POSITION_LINE = re.compile(r"章节定位\s*[：:]\s*(.*)")
LOW_POSITIONS = ("低压", "信息整理", "过场")

# 只有启发式门禁可以按书降级或逐章豁免；语言、退化、AI 句式、文风卫生这类
# 确定性硬门永远不行。
WAIVABLE_GATES = ("dialogue_drift", "emotion_floor", "hook_strength")
GATE_MODES = ("blocking", "advisory")
PAUSE_MODES = ("keep", "normalize")
QUOTE_MODES = ("keep", "ascii", "yan")
GATE_CONFIG_RELATIVE = "设定/门禁配置.json"
QUALITY_DIR = "质检回执"
PROGRESS_FILE = "质检进度.md"
PROGRESS_MARKER = "<!-- 由 chapter_candidate.py 根据 追踪/章节提交 与 追踪/质检回执 生成；不要手改，改了下一章 close 时会被覆盖 -->"
PROGRESS_LEGACY_ARCHIVE = "质检进度_旧版手工记录.md"


class CandidateError(RuntimeError):
    pass


# ── 基础工具 ────────────────────────────────────────────────────────────────


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(content)
    temp.replace(path)


def atomic_append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    existing = path.read_bytes() if path.is_file() else b""
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write_bytes(path, existing + line)


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CandidateError(f"{label}不存在: {path}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"{label}不是有效 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CandidateError(f"{label}必须是 JSON object")
    return value


def emit_json(payload: object) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def resolve_inside(project: Path, raw: str, *, must_exist: bool, label: str) -> tuple[Path, str]:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = project / candidate
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(project).as_posix()
    except ValueError as exc:
        raise CandidateError(f"{label}必须位于项目目录内: {raw}") from exc
    if must_exist and not resolved.is_file():
        raise CandidateError(f"{label}不存在: {resolved}")
    return resolved, relative


def tracking_snapshot(project: Path) -> tuple[int, int]:
    state = load_json(project / "追踪" / "_tracking-state.json", "追踪权威状态")
    last = state.get("last_committed_chapter")
    revision = state.get("state_revision")
    if not isinstance(last, int) or last < 0 or not isinstance(revision, int) or revision < 0:
        raise CandidateError("追踪权威状态缺少有效 last_committed_chapter/state_revision")
    return last, revision


def writing_method_snapshot(project: Path) -> dict[str, Any]:
    tool = Path(__file__).resolve().parent / "style_method.py"
    completed = subprocess.run(
        [sys.executable, str(tool), "check", "--project", str(project)],
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "写作方法门禁失败").strip()[-4000:]
        raise CandidateError(detail)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CandidateError("style_method.py check 未返回有效 JSON") from exc
    if not isinstance(result, dict) or result.get("status") != "ready":
        raise CandidateError("写作方法状态无效")
    return {
        "method_branch": result.get("method_branch"),
        "method_id": result.get("method_id"),
        "implicit_default": bool(result.get("implicit_default")),
    }


def writing_method_files(project: Path) -> list[Path]:
    config_path = project / "设定" / "写作方法.json"
    if not config_path.is_file():
        return []
    config = load_json(config_path, "写作方法配置")
    output = [config_path]
    if config.get("method_branch") == "B-distilled":
        for key in ("compiled_method_path", "compiled_manifest_path", "forward_test_path"):
            raw = config.get(key)
            if isinstance(raw, str) and raw.strip():
                path, _ = resolve_inside(project, raw, must_exist=True, label=f"写作方法 {key}")
                output.append(path)
    return output


def chapter_number(path: Path) -> int | None:
    match = CHAPTER_PATTERN.match(path.name)
    return int(match.group(1)) if match else None


def previous_chapter(project: Path, chapter: int) -> Path | None:
    if chapter <= 1:
        return None
    prose_dir = project / "正文"
    matches = sorted(
        path for path in prose_dir.glob("第*章*.md") if path.is_file() and chapter_number(path) == chapter - 1
    )
    if len(matches) > 1:
        raise CandidateError(f"第{chapter - 1}章存在多个正文文件，先解决章节号冲突")
    return matches[0] if matches else None


def fingerprint(project: Path, paths: list[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.resolve()
        relative = resolved.relative_to(project).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        entries.append({"path": relative, "sha256": sha256_file(resolved), "size": resolved.stat().st_size})
    return entries


def context_digest(entries: list[dict[str, Any]], last: int, revision: int, target: str) -> str:
    payload = {
        "base_files": entries,
        "expected_last_committed_chapter": last,
        "expected_state_revision": revision,
        "target": target,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def iter_manifests(project: Path) -> list[tuple[Path, dict[str, Any]]]:
    output: list[tuple[Path, dict[str, Any]]] = []
    root = project / "追踪" / "候选章"
    if not root.is_dir():
        return output
    for path in sorted(root.glob("第*章/*/manifest.json")):
        try:
            data = load_json(path, "候选章 manifest")
        except CandidateError:
            continue
        output.append((path, data))
    return output


def open_workspaces(project: Path, *, excluding: Path | None = None) -> list[Path]:
    result: list[Path] = []
    for path, data in iter_manifests(project):
        if excluding is not None and path.resolve() == excluding.resolve():
            continue
        if data.get("status") in OPEN_STATUSES:
            result.append(path.parent)
    return result


# ── manifest 版本 ───────────────────────────────────────────────────────────


def v2_defaults() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "review_policy": "full",
        "batch_last": False,
        "pressure_override": None,
        "lineage": [],
        "gate_run": None,
        "packets": {},
        "reviews": {},
        "waivers": [],
    }


def is_gated(data: dict[str, Any]) -> bool:
    return data.get("schema_version") == SCHEMA_VERSION


def load_run(raw: str) -> tuple[Path, Path, dict[str, Any]]:
    run = Path(raw).expanduser().resolve()
    manifest_path = run / "manifest.json"
    data = load_json(manifest_path, "候选章 manifest")
    version = data.get("schema_version")
    if version not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise CandidateError("不支持的候选章 manifest 版本")
    project = Path(str(data.get("project_root", ""))).resolve()
    if not project.is_dir():
        raise CandidateError("manifest 中的项目目录不存在")
    try:
        run.relative_to(project / "追踪" / "候选章")
    except ValueError as exc:
        raise CandidateError("候选运行目录不在项目的 追踪/候选章 内") from exc
    if version == LEGACY_SCHEMA_VERSION and data.get("status") == "draft":
        # 旧版 draft 还没被作者看过，原地升级后走完整的接纳前流程；已 approve 的旧候选
        # 作者已按当时规则接纳过，按旧 8 道门写入并标 legacy-v1，不追溯要求回执。
        data = {**v2_defaults(), **data, "schema_version": SCHEMA_VERSION, "upgraded_from": LEGACY_SCHEMA_VERSION}
        atomic_write_json(manifest_path, data)
    elif version == SCHEMA_VERSION:
        for key, value in v2_defaults().items():
            data.setdefault(key, value)
    return project, manifest_path, data


def require_gated_draft(data: dict[str, Any], action: str) -> None:
    if not is_gated(data):
        raise CandidateError(f"旧版（v1）候选不支持 {action}；它已按旧规则接纳，直接 promote")
    if data.get("status") != "draft":
        raise CandidateError(f"只有 draft 候选可以 {action}，当前状态: {data.get('status')}")


# ── 候选稿谱系 ─────────────────────────────────────────────────────────────


def changed_chars(before: str, after: str) -> int:
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")


def edit_ratio(before: str, after: str) -> float:
    """改动字数占原稿的比例：先按段对齐，再在改过的段里逐字比较。

    只按整段计量太粗——一段三百字里改一个字就会算成三百字改动。
    """
    old_lines = before.splitlines()
    new_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            changed += changed_chars("\n".join(old_lines[i1:i2]), "\n".join(new_lines[j1:j2]))
        elif tag != "equal":
            changed += max(sum(len(line) for line in old_lines[i1:i2]), sum(len(line) for line in new_lines[j1:j2]))
    return round(changed / max(sum(len(line) for line in old_lines), 1), 4)


def lineage_dir(run: Path) -> Path:
    return run / ".lineage"


def lineage_text(run: Path, sha: str) -> str | None:
    path = lineage_dir(run) / f"{sha}.md"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def record_lineage(run: Path, data: dict[str, Any], candidate: Path, source: str) -> str:
    """登记候选稿的当前版本；未经本工具的改动记为 edit，并算出相对上一版的改动比例。"""
    content = candidate.read_bytes()
    sha = sha256_bytes(content)
    lineage = data.setdefault("lineage", [])
    if lineage and lineage[-1]["sha256"] == sha:
        return sha
    previous = lineage[-1] if lineage else None
    ratio = None
    if previous is not None:
        before = lineage_text(run, previous["sha256"])
        ratio = edit_ratio(before, content.decode("utf-8")) if before is not None else 1.0
    snapshot = lineage_dir(run) / f"{sha}.md"
    if not snapshot.is_file():
        atomic_write_bytes(snapshot, content)
    lineage.append(
        {
            "sha256": sha,
            "source": source if previous is not None or source != "edit" else "draft",
            "recorded_at": utc_now(),
            "edit_ratio": ratio,
        }
    )
    return sha


def lineage_index(data: dict[str, Any], sha: str | None) -> int | None:
    if not sha:
        return None
    lineage = data.get("lineage", [])
    for index in range(len(lineage) - 1, -1, -1):
        if lineage[index].get("sha256") == sha:
            return index
    return None


def consistency_problem(data: dict[str, Any], current_sha: str) -> str | None:
    receipt = data.get("reviews", {}).get("consistency")
    if not receipt:
        return "缺少一致性审查回执（review-packet --kind consistency → consistency-checker → attest）"
    reviewed = receipt.get("candidate_sha256")
    if reviewed == current_sha:
        return None
    start = lineage_index(data, reviewed)
    end = lineage_index(data, current_sha)
    if start is None or end is None or end < start:
        return "一致性审查回执不对应当前候选稿；重新发审查包"
    if any(entry.get("source") != "fix" for entry in data["lineage"][start + 1 : end + 1]):
        return "候选稿在一致性审查后被改动（fix 之外的改动都要重审）；重新发一致性审查包"
    return None


def deslop_problem(data: dict[str, Any], current_sha: str, *, require_full: bool) -> str | None:
    receipt = data.get("reviews", {}).get("deslop")
    if not receipt:
        return "缺少去味审查回执（review-packet --kind deslop → narrative-writer → attest）"
    if require_full and receipt.get("scope") != "full":
        return "本章要求全文去味审查（review 模式或批次末章），当前回执只覆盖精简范围"
    start = lineage_index(data, receipt.get("output_sha256"))
    end = lineage_index(data, current_sha)
    if start is None or end is None or end < start:
        return "去味审查回执不对应当前候选稿的谱系；重新发去味审查包"
    drift = sum(
        float(entry.get("edit_ratio") or 0)
        for entry in data["lineage"][start + 1 : end + 1]
        if entry.get("source") == "edit"
    )
    if drift > DESLOP_MAX_EDIT_RATIO:
        return f"去味审查后又改动了约 {drift:.0%} 的正文（上限 {DESLOP_MAX_EDIT_RATIO:.0%}）；重新发去味审查包"
    return None


def requires_full_deslop(data: dict[str, Any]) -> bool:
    return data.get("approval_mode") != "auto" or data.get("review_policy") != "lean" or bool(data.get("batch_last"))


def review_problems(data: dict[str, Any], current_sha: str) -> list[str]:
    problems: list[str] = []
    for problem in (
        deslop_problem(data, current_sha, require_full=requires_full_deslop(data)),
        consistency_problem(data, current_sha),
    ):
        if problem:
            problems.append(problem)
    for kind in REVIEW_KINDS:
        receipt = data.get("reviews", {}).get(kind)
        if not receipt:
            continue
        if receipt.get("verdict") == "REJECT":
            problems.append(f"{REVIEW_LABELS[kind]}结论为 REJECT：先按报告改稿，再重新审查")
        if kind == "consistency" and receipt.get("open_blocking"):
            problems.append(f"一致性审查仍有 {receipt['open_blocking']} 条 S1/S2：改稿后重新发一致性审查包")
    if data.get("approval_mode") == "auto" and data.get("waivers"):
        problems.append("auto 模式不允许门禁豁免")
    return problems


# ── 门禁配置与压力档 ───────────────────────────────────────────────────────


def load_gate_config(project: Path) -> tuple[dict[str, Any], str | None]:
    config: dict[str, Any] = {
        "gates": {name: {"mode": "blocking"} for name in WAIVABLE_GATES},
        "default_pressure": "normal",
        "punctuation": {"pause_mode": "keep", "quote_mode": "keep"},
    }
    path = project / GATE_CONFIG_RELATIVE
    if not path.is_file():
        return config, None
    raw = load_json(path, "门禁配置")
    if raw.get("schema_version") != 1:
        raise CandidateError(f"{GATE_CONFIG_RELATIVE} 的 schema_version 必须是 1")
    unknown = set(raw) - {"schema_version", "gates", "default_pressure", "punctuation", "note"}
    if unknown:
        raise CandidateError(f"{GATE_CONFIG_RELATIVE} 含不支持的字段: {', '.join(sorted(unknown))}")
    gates = raw.get("gates", {})
    if not isinstance(gates, dict):
        raise CandidateError(f"{GATE_CONFIG_RELATIVE}.gates 必须是 object")
    for name, value in gates.items():
        if name not in WAIVABLE_GATES:
            raise CandidateError(f"{GATE_CONFIG_RELATIVE} 只能调整启发式门禁 {', '.join(WAIVABLE_GATES)}，不能调整 {name}")
        if not isinstance(value, dict) or value.get("mode") not in GATE_MODES or set(value) - {"mode", "reason"}:
            raise CandidateError(f"{GATE_CONFIG_RELATIVE}.gates.{name} 必须是 {{mode: blocking|advisory, reason}}")
        config["gates"][name] = {"mode": value["mode"]}
    pressure = raw.get("default_pressure", "normal")
    if pressure not in PRESSURES:
        raise CandidateError(f"{GATE_CONFIG_RELATIVE}.default_pressure 必须是 {'/'.join(PRESSURES)}")
    config["default_pressure"] = pressure
    punctuation = raw.get("punctuation", {})
    if not isinstance(punctuation, dict) or set(punctuation) - {"pause_mode", "quote_mode"}:
        raise CandidateError(f"{GATE_CONFIG_RELATIVE}.punctuation 只能含 pause_mode/quote_mode")
    if punctuation.get("pause_mode", "keep") not in PAUSE_MODES:
        raise CandidateError(f"{GATE_CONFIG_RELATIVE}.punctuation.pause_mode 必须是 {'/'.join(PAUSE_MODES)}")
    if punctuation.get("quote_mode", "keep") not in QUOTE_MODES:
        raise CandidateError(f"{GATE_CONFIG_RELATIVE}.punctuation.quote_mode 必须是 {'/'.join(QUOTE_MODES)}")
    config["punctuation"].update(punctuation)
    return config, sha256_file(path)


def outline_position(outline: Path) -> str:
    for line in outline.read_text(encoding="utf-8").splitlines():
        match = POSITION_LINE.search(line)
        if match:
            value = match.group(1).strip()
            # 模板占位（{高压/推进/...}）和 [待补充] 都按留空处理
            if value.startswith(("{", "[待补充")) or not value:
                return ""
            return value
    return ""


def resolve_pressure(data: dict[str, Any], outline: Path, config: dict[str, Any]) -> dict[str, Any]:
    override = data.get("pressure_override")
    if isinstance(override, dict) and override.get("level") in PRESSURES:
        return {"level": override["level"], "source": "override", "note": override.get("note", "")}
    position = outline_position(outline)
    if "高压" in position:
        return {"level": "high", "source": "outline", "position": position}
    if any(word in position for word in LOW_POSITIONS):
        return {"level": "low", "source": "outline", "position": position}
    if position:
        return {"level": "normal", "source": "outline", "position": position}
    return {"level": config["default_pressure"], "source": "default", "position": ""}


# ── 确定性门禁 ─────────────────────────────────────────────────────────────


def gate_specs(
    project: Path,
    candidate: Path,
    outline: Path,
    chapter: int,
    config: dict[str, Any],
    pressure: str,
) -> list[dict[str, Any]]:
    scripts = Path(__file__).resolve().parent
    style = project / "设定" / "文风.md"
    punctuation = config["punctuation"]

    def node(script: str, *args: str) -> list[str]:
        return ["node", str(scripts / script), *args]

    def python(script: str, *args: str) -> list[str]:
        return [sys.executable, str(scripts / script), *args]

    def heuristic(name: str) -> str:
        return config["gates"][name]["mode"]

    return [
        {"name": "writing_method", "kind": "blocking", "script": "style_method.py",
         "command": python("style_method.py", "check", "--project", str(project))},
        {"name": "language", "kind": "blocking", "script": "language_gate.js", "fail_fast": True,
         "command": node("language_gate.js", str(candidate))},
        {"name": "punctuation", "kind": "blocking", "script": "normalize-punctuation.js", "remedy": "fix",
         "command": node("normalize-punctuation.js", "--check", "--pause-mode", punctuation["pause_mode"],
                         "--quote-mode", punctuation["quote_mode"], str(candidate))},
        {"name": "style_hygiene", "kind": "blocking", "script": "check-style-hygiene.js",
         "command": node("check-style-hygiene.js", "--check", "--fail-on=blocking",
                         *(["--style", str(style)] if style.is_file() else []), str(candidate))},
        {"name": "ai_patterns", "kind": "blocking", "script": "check-ai-patterns.js",
         "command": node("check-ai-patterns.js", "--check", "--json", "--fail-on=blocking", str(candidate))},
        {"name": "degeneration", "kind": "blocking", "script": "check-degeneration.js",
         "command": node("check-degeneration.js", "--check", "--language=zh", "--fail-on=blocking", str(candidate))},
        {"name": "prose_metrics", "kind": "blocking", "script": "prose_metrics.py",
         "command": python("prose_metrics.py", str(candidate))},
        {"name": "outline_copy", "kind": "blocking", "script": "check-outline-copy.js",
         "command": node("check-outline-copy.js", "--outline", str(outline), "--fail-on=blocking", str(candidate))},
        {"name": "accepted_voice_profile", "kind": "blocking", "script": "voice_profile.py",
         "command": python("voice_profile.py", "check", "--project", str(project), "--candidate", str(candidate))},
        {"name": "cross_chapter_shape", "kind": "blocking", "script": "chapter_shape_gate.py",
         "command": python("chapter_shape_gate.py", "--project", str(project), "--candidate", str(candidate),
                           "--chapter", str(chapter), "--window", "6")},
        {"name": "dialogue_drift", "kind": heuristic("dialogue_drift"), "script": "dialogue_drift_gate.js",
         "advisory_codes": {2},
         "command": node("dialogue_drift_gate.js", "--current", str(candidate), "--chapter", str(chapter),
                         "--project", str(project), "--json")},
        {"name": "emotion_floor", "kind": heuristic("emotion_floor"), "script": "check-emotion-floor.js",
         "command": node("check-emotion-floor.js", "--check", "--json", "--fail-on=blocking",
                         f"--pressure={pressure}", str(candidate))},
        {"name": "hook_strength", "kind": heuristic("hook_strength"), "script": "check-hook-strength.js",
         "command": node("check-hook-strength.js", "--check", "--json", "--fail-on=blocking",
                         f"--chapter={chapter}", str(candidate))},
        {"name": "typos", "kind": "advisory", "script": "check-typos.js",
         "command": node("check-typos.js", "--check", "--json", "--fail-on=all", str(candidate))},
    ]


def gate_status(spec: dict[str, Any], returncode: int) -> str:
    if returncode == 0:
        return "pass"
    if spec["kind"] == "advisory":
        return "advisory" if returncode in spec.get("advisory_codes", {1}) else "error"
    return "fail"


def run_gates(project: Path, run: Path, data: dict[str, Any], candidate: Path, outline: Path) -> dict[str, Any]:
    if not candidate.is_file() or not candidate.read_text(encoding="utf-8").strip():
        raise CandidateError("候选稿为空")
    chapter = int(data["chapter"])
    config, config_sha = load_gate_config(project)
    pressure = resolve_pressure(data, outline, config)
    scripts = Path(__file__).resolve().parent
    waived = {item.get("gate") for item in data.get("waivers", []) if isinstance(item, dict)}
    allow_waivers = data.get("approval_mode") == "review"
    results: list[dict[str, Any]] = []
    stopped_after = None
    for spec in gate_specs(project, candidate, outline, chapter, config, pressure["level"]):
        try:
            completed = subprocess.run(
                spec["command"], text=True, capture_output=True, check=False, encoding="utf-8", errors="replace"
            )
        except FileNotFoundError as exc:
            raise CandidateError("运行正文门禁需要 node") from exc
        status = gate_status(spec, completed.returncode)
        if status == "fail" and spec["name"] in waived and allow_waivers:
            status = "waived"
        script_path = scripts / spec["script"]
        result: dict[str, Any] = {
            "name": spec["name"],
            "kind": spec["kind"],
            "status": status,
            "returncode": completed.returncode,
            "script_sha256": sha256_file(script_path) if script_path.is_file() else None,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
        if spec.get("remedy"):
            result["remedy"] = spec["remedy"]
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, (dict, list)):
            result["payload"] = payload
        results.append(result)
        if spec.get("fail_fast") and status in {"fail", "error"}:
            # 语言门不过时其他门禁的结论没有意义，先改语言
            stopped_after = spec["name"]
            break
    failures = [item["name"] for item in results if item["status"] in {"fail", "error"}]
    return {
        "schema_version": GATE_REPORT_SCHEMA_VERSION,
        "chapter": chapter,
        "candidate": data["candidate"],
        "candidate_sha256": sha256_file(candidate),
        "generated_at": utc_now(),
        "status": "fail" if failures else "pass",
        "stopped_after": stopped_after,
        "approval_mode": data.get("approval_mode"),
        "pressure": pressure,
        "gate_config": GATE_CONFIG_RELATIVE if config_sha else None,
        "gate_config_sha256": config_sha,
        "failures": failures,
        "advisories": [item["name"] for item in results if item["status"] == "advisory"],
        "waived": [item["name"] for item in results if item["status"] == "waived"],
        "gates": results,
    }


def failure_summary(report: dict[str, Any]) -> str:
    lines = [f"正文门禁未通过: {', '.join(report['failures'])}"]
    for item in report["gates"]:
        if item["status"] not in {"fail", "error"}:
            continue
        detail = (item["stdout"] or item["stderr"]).strip()
        payload = item.get("payload")
        findings = payload.get("findings") if isinstance(payload, dict) else None
        if isinstance(findings, list):
            wanted = {"blocking"} if item["kind"] == "blocking" else {"blocking", "advisory"}
            lines_out = [
                f"{finding.get('type')}: {str(finding.get('message', ''))[:160]}"
                + (f"（{str(finding.get('excerpt'))[:60]}）" if finding.get("excerpt") else "")
                for finding in findings
                if isinstance(finding, dict) and finding.get("severity") in wanted
            ]
            detail = "\n  ".join(lines_out) or (item["stderr"] or "").strip() or detail[-600:]
        hint = ""
        if item.get("remedy") == "fix":
            hint = "（运行 chapter_candidate.py fix 自动修正）"
        elif item["name"] in WAIVABLE_GATES:
            hint = "（启发式门禁：改稿；确属误报时 review 模式可经作者确认后 waive）"
        lines.append(f"- {item['name']}{hint}: {detail[-1200:]}")
    if report.get("stopped_after"):
        lines.append(f"语言门未通过，其余门禁尚未运行；先修语言再 check。")
    return "\n".join(lines)


def freshness(project: Path, data: dict[str, Any]) -> dict[str, Any]:
    changed: list[dict[str, str]] = []
    last, revision = tracking_snapshot(project)
    if last != data.get("expected_last_committed_chapter"):
        changed.append({"path": "追踪/_tracking-state.json", "reason": "last_committed_chapter_changed"})
    if revision != data.get("expected_state_revision"):
        changed.append({"path": "追踪/_tracking-state.json", "reason": "state_revision_changed"})
    stored_method = data.get("writing_method")
    if isinstance(stored_method, dict):
        current_method = writing_method_snapshot(project)
        if current_method != stored_method:
            changed.append({"path": "设定/写作方法.json", "reason": "writing_method_changed"})
    current_entries: list[dict[str, Any]] = []
    for entry in data.get("base_files", []):
        relative = str(entry.get("path", ""))
        path = project / relative
        if not path.is_file():
            changed.append({"path": relative, "reason": "missing"})
            continue
        current = {"path": relative, "sha256": sha256_file(path), "size": path.stat().st_size}
        current_entries.append(current)
        if current["sha256"] != entry.get("sha256"):
            changed.append({"path": relative, "reason": "content_changed"})
    current_digest = context_digest(current_entries, last, revision, str(data.get("target", "")))
    if current_digest != data.get("context_digest") and not changed:
        changed.append({"path": "manifest.json", "reason": "context_digest_changed"})
    target = project / str(data.get("target", ""))
    if target.exists():
        promoted = data.get("promotion") if isinstance(data.get("promotion"), dict) else {}
        expected_target_sha = promoted.get("target_sha256")
        if data.get("status") != "promoted" or not expected_target_sha or sha256_file(target) != expected_target_sha:
            changed.append({"path": str(data.get("target", "")), "reason": "target_created_or_changed"})
    return {
        "status": "stale" if changed else "fresh",
        "changed": changed,
        "current_last_committed_chapter": last,
        "current_state_revision": revision,
        "current_context_digest": current_digest,
    }


def candidate_outline(project: Path, data: dict[str, Any]) -> Path:
    raw = data.get("outline")
    if not isinstance(raw, str) or not raw.strip():
        for entry in data.get("base_files", []):
            if not isinstance(entry, dict):
                continue
            candidate = entry.get("path")
            if isinstance(candidate, str) and candidate.startswith("大纲/") and "细纲" in Path(candidate).name:
                raw = candidate
                break
    if not isinstance(raw, str) or not raw.strip():
        raise CandidateError("候选章 manifest 缺少可识别的本章细纲")
    outline, _ = resolve_inside(project, raw, must_exist=True, label="本章细纲")
    return outline


def require_fresh(project: Path, data: dict[str, Any]) -> dict[str, Any]:
    result = freshness(project, data)
    if result["status"] != "fresh":
        details = ", ".join(f"{item['path']}:{item['reason']}" for item in result["changed"])
        raise CandidateError(f"候选稿上下文已过期: {details}")
    candidate = project / str(data.get("candidate", ""))
    if not candidate.is_file():
        raise CandidateError(f"候选稿不存在: {candidate}")
    expected_chapter = data.get("chapter")
    if not isinstance(expected_chapter, int) or expected_chapter <= 0:
        raise CandidateError("manifest 章号无效")
    target = project / str(data.get("target", ""))
    if chapter_number(target) != expected_chapter:
        raise CandidateError("目标文件名章号与许可章号不一致")
    return result


def legacy_gate_names() -> set[str]:
    return {
        "writing_method", "language", "ai_patterns", "degeneration", "prose_metrics",
        "outline_copy", "accepted_voice_profile", "cross_chapter_shape",
    }


def gate_run(project: Path, run: Path, manifest_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """运行门禁、写 gate-report.json、登记谱系；不因失败抛错，由调用方决定。"""
    candidate = project / data["candidate"]
    outline = candidate_outline(project, data)
    if is_gated(data):
        record_lineage(run, data, candidate, "edit")
    report = run_gates(project, run, data, candidate, outline)
    if not is_gated(data):
        # 旧版已接纳候选按接纳时的 8 道门写入，不追溯新增门禁
        legacy = legacy_gate_names()
        report["gates"] = [item for item in report["gates"] if item["name"] in legacy]
        report["failures"] = [name for name in report["failures"] if name in legacy]
        report["advisories"] = [name for name in report["advisories"] if name in legacy]
        report["status"] = "fail" if report["failures"] else "pass"
        report["protocol"] = LEGACY_PROTOCOL
    report_path = run / "gate-report.json"
    atomic_write_json(report_path, report)
    data["gate_run"] = {
        "candidate_sha256": report["candidate_sha256"],
        "status": report["status"],
        "failures": report["failures"],
        "advisories": report["advisories"],
        "waived": report["waived"],
        "generated_at": report["generated_at"],
        "report_sha256": sha256_file(report_path),
    }
    atomic_write_json(manifest_path, data)
    return report


def validate_run(project: Path, run: Path, manifest_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    result = require_fresh(project, data)
    report = gate_run(project, run, manifest_path, data)
    if report["status"] != "pass":
        raise CandidateError(failure_summary(report))
    return {**result, "candidate_sha256": report["candidate_sha256"], "gates": report["gates"], "report": report}


# ── 命令：init / check / fix ───────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise CandidateError(f"项目目录不存在: {project}")
    if args.chapter <= 0:
        raise CandidateError("chapter 必须大于 0")
    last, revision = tracking_snapshot(project)
    if args.chapter != last + 1:
        raise CandidateError(f"精确章节许可只允许第 {last + 1} 章，收到第 {args.chapter} 章")
    existing = open_workspaces(project)
    if existing:
        raise CandidateError(f"已有未闭环候选章: {existing[0]}")
    outline, outline_relative = resolve_inside(project, args.outline, must_exist=True, label="本章细纲")
    target, target_relative = resolve_inside(project, args.target, must_exist=False, label="正文目标")
    if not target_relative.startswith("正文/") or target.suffix.lower() != ".md":
        raise CandidateError("正文目标必须是项目 正文/ 下的 Markdown 文件")
    if target.exists():
        raise CandidateError("正文目标已存在；修改旧章必须走 revision-governor/revision_guard")
    if chapter_number(target) != args.chapter:
        raise CandidateError("正文目标文件名章号与 --chapter 不一致")
    if args.approval_mode == "auto" and not (args.authorization_note or "").strip():
        raise CandidateError("自动定稿模式必须记录用户的明确授权说明")
    if args.review_policy == "lean" and args.approval_mode != "auto":
        raise CandidateError("精简审查（--review-policy lean）只用于 auto 模式；review 模式作者看到的必须是全文审过的稿")
    load_gate_config(project)  # 配置写错时在开工前报，而不是写完一章才报
    method_snapshot = writing_method_snapshot(project)
    run_id = args.id or datetime.now().strftime("C%Y%m%d-%H%M%S")
    if not ID_PATTERN.fullmatch(run_id):
        raise CandidateError("candidate id 只能包含字母、数字、点、下划线和连字符")
    run = project / "追踪" / "候选章" / f"第{args.chapter:03d}章" / run_id
    if run.exists():
        raise CandidateError(f"候选运行目录已存在: {run}")
    base_paths = [outline]
    context = project / "追踪" / "上下文.md"
    if context.is_file():
        base_paths.append(context)
    previous = previous_chapter(project, args.chapter)
    if args.chapter > 1 and previous is None:
        raise CandidateError(f"第{args.chapter - 1}章正文缺失，拒绝创建下一章候选")
    if previous is not None:
        base_paths.append(previous)
    for raw in args.base or []:
        path, _ = resolve_inside(project, raw, must_exist=True, label="基础文件")
        base_paths.append(path)
    base_paths.extend(writing_method_files(project))
    entries = fingerprint(project, base_paths)
    candidate_relative = (run / "candidate.md").relative_to(project).as_posix()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": run_id,
        "status": "draft",
        "created_at": utc_now(),
        "project_root": str(project),
        "chapter": args.chapter,
        "target": target_relative,
        "candidate": candidate_relative,
        "outline": outline_relative,
        "approval_mode": args.approval_mode,
        "authorization_note": (args.authorization_note or "").strip(),
        "expected_last_committed_chapter": last,
        "expected_state_revision": revision,
        "writing_method": method_snapshot,
        "base_files": entries,
        "context_digest": context_digest(entries, last, revision, target_relative),
        "validation": None,
        "approval": None,
        "promotion": None,
        **v2_defaults(),
        "review_policy": args.review_policy,
        "batch_last": bool(args.batch_last),
    }
    run.mkdir(parents=True)
    atomic_write_json(run / "manifest.json", payload)
    atomic_write_bytes(run / "candidate.md", b"")
    print(run)
    return 0


# 主会话需要读的证据：句段实测、声音画像漂移、近章结构五问的证据包。其余门禁的完整输出
# 只进 gate-report.json，终端只给结论和命中摘要，免得每次 check 把几十 KB 塞进上下文。
EVIDENCE_GATES = {"prose_metrics", "accepted_voice_profile", "cross_chapter_shape"}


def gate_findings_summary(item: dict[str, Any]) -> list[str]:
    payload = item.get("payload")
    findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings, list):
        text = (item.get("stdout") or item.get("stderr") or "").strip()
        return [line[:200] for line in text.splitlines()[:6]]
    return [
        f"{finding.get('severity')} {finding.get('type')}"
        + (f" 第{finding['line']}行" if isinstance(finding.get("line"), int) else "")
        + f"：{str(finding.get('message', ''))[:120]}"
        for finding in findings
        if isinstance(finding, dict) and finding.get("severity") in {"blocking", "advisory"}
    ][:12]


def check_output(result: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    gates = []
    for item in report["gates"]:
        row = {key: item.get(key) for key in ("name", "kind", "status", "returncode", "script_sha256")}
        if item["name"] in EVIDENCE_GATES and item.get("payload") is not None:
            row["payload"] = item["payload"]
        elif item["status"] != "pass":
            row["findings"] = gate_findings_summary(item)
        gates.append(row)
    return {
        **result,
        "candidate_sha256": report["candidate_sha256"],
        "status": report["status"],
        "failures": report["failures"],
        "advisories": report["advisories"],
        "waived": report["waived"],
        "pressure": report["pressure"],
        "gate_report": "gate-report.json（完整输出）",
        "gates": gates,
    }


def cmd_check(args: argparse.Namespace) -> int:
    project, manifest_path, data = load_run(args.run)
    run = manifest_path.parent
    result = require_fresh(project, data)
    if args.freshness_only:
        emit_json(result)
        return 0
    report = gate_run(project, run, manifest_path, data)
    emit_json(check_output(result, report))
    if report["status"] != "pass":
        raise CandidateError(failure_summary(report))
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    project, manifest_path, data = load_run(args.run)
    require_gated_draft(data, "fix")
    run = manifest_path.parent
    candidate = project / data["candidate"]
    if not candidate.is_file() or not candidate.read_text(encoding="utf-8").strip():
        raise CandidateError("候选稿为空")
    config, _ = load_gate_config(project)
    before = record_lineage(run, data, candidate, "edit")
    command = [
        "node",
        str(Path(__file__).resolve().parent / "normalize-punctuation.js"),
        "--pause-mode",
        config["punctuation"]["pause_mode"],
        "--quote-mode",
        config["punctuation"]["quote_mode"],
        str(candidate),
    ]
    try:
        completed = subprocess.run(command, text=True, capture_output=True, check=False, encoding="utf-8")
    except FileNotFoundError as exc:
        raise CandidateError("运行 fix 需要 node") from exc
    if completed.returncode != 0:
        raise CandidateError(f"normalize-punctuation.js 失败\n{completed.stdout}{completed.stderr}".rstrip())
    after = record_lineage(run, data, candidate, "fix")
    atomic_write_json(manifest_path, data)
    emit_json({"changed": before != after, "before_sha256": before, "after_sha256": after, "next": "check"})
    return 0


# ── 命令：审查包 / 回执 / 豁免 / 压力档 ────────────────────────────────────


def paragraph_lines(text: str) -> list[int]:
    return [index for index, line in enumerate(text.splitlines(), start=1) if line.strip() and not line.lstrip().startswith("#")]


def lean_spans(text: str, report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """精简去味只看被门禁标记的行加开头结尾；其余段落只由确定性门禁把关。"""
    lines = paragraph_lines(text)
    picked: dict[int, str] = {}
    for line in lines[:LEAN_EDGE_PARAGRAPHS]:
        picked.setdefault(line, "开头")
    for line in lines[-LEAN_EDGE_PARAGRAPHS:]:
        picked.setdefault(line, "结尾")
    if report:
        for gate in report.get("gates", []):
            payload = gate.get("payload")
            findings = payload.get("findings") if isinstance(payload, dict) else None
            for finding in findings or []:
                if not isinstance(finding, dict) or finding.get("severity") not in {"blocking", "advisory"}:
                    continue
                line = finding.get("line")
                if isinstance(line, int) and line in lines:
                    picked.setdefault(line, f"{gate['name']}:{finding.get('type', '')}")
    return [{"line": line, "reason": picked[line]} for line in sorted(picked)]


def packet_prompt(kind: str, packet: dict[str, Any]) -> str:
    fence = "`" * 3
    report_shape = (
        f'{{"kind": "{kind}", "nonce": "{packet["nonce"]}", "candidate_sha256": "{packet["candidate_sha256"]}", '
        f'"scope": "{packet["scope"]}", "verdict": "PASS|CONCERNS|REJECT", '
        '"findings": [{"severity": "S1|S2|S3|S4", "category": "…", "quote": "…", "issue": "…"'
        + (', "action": "edited|flagged"' if kind == "deslop" else ', "source": "（可选）证据所在文件的相对路径"')
        + '}], "coverage": ["…"], "summary": "…"}'
    )
    quote_rule = (
        "quote 必须逐字摘自工作副本改动前的原文（至少 4 个字），不能改写或概括。"
        if kind == "deslop"
        else "quote 必须逐字摘自候选稿（至少 4 个字）；证据在其他文件时加 source 写相对路径，quote 摘自那个文件。"
    )
    common_tail = (
        f"\n完成后在回复末尾给出一个 {fence}json 代码块作为报告，形如：\n{report_shape}\n"
        f"{quote_rule}coverage 列出实际核对过的角色、伏笔 ID、设定项或检查项；没有 findings 时至少列 3 项。"
        "S1/S2 表示接纳前必须解决的问题。verdict=REJECT 表示本章需要重写。"
    )
    if kind == "deslop":
        scope_text = (
            "全文"
            if packet["scope"] == "full"
            else "精简范围，只看以下行（其余段落已由确定性门禁把关）：" + "；".join(
                f"第{span['line']}行（{span['reason']}）" for span in packet.get("spans", [])
            )
        )
        return (
            "任务描述：候选章去味独立审查（接纳前）\n"
            f"项目目录：{packet['project']}\n"
            f"章节：第{packet['chapter']}章\n"
            f"工作副本：{packet['work_copy']}（只改这一个文件；审查结束后由主会话用 attest 写回候选稿）\n"
            f"细纲文件：{packet['outline']}\n"
            f"审查范围：{scope_text}\n"
            "禁止：写 候选稿本体、正文/、追踪/ 下任何其他文件，或新增细纲没有的剧情。\n"
            "删除优先：每条 AI 味项先判能否删除——删后不丢伏笔/钩子/角色/情节/必要信息的直接删，会丢才润色"
            "（删除受比例上限与字数下限约束，跌破下限改降AI重写）。\n"
            "必须检查：先否定再肯定的翻转句式（含跨段‘不是A/也不是B/只是C’）；对话里的‘至于X不X，怎么X’和同动词"
            "‘不V A，不V B’工整清单；正文是否把细纲多个字段里重复的同一要求逐项复述；作者解释总结/意义尾巴；"
            "成片堆叠的比喻；连续的精致戏剧反应（头皮发紧/眼皮一跳/心口一沉/胃里翻涌），跨章复读的同一生理反应词；"
            "场内载体（手机/屏幕/公告/物证）保留为角色看到的文本；任务卡点只在能卡出信息/关系/代价/选择/伏笔变化时使用。\n"
            "改了的问题 action=edited；判断应改但没动手的 action=flagged。"
            + common_tail
        )
    return (
        "任务描述：候选章一致性审查（候选审查模式，接纳前）\n"
        f"项目目录：{packet['project']}\n"
        f"候选稿：{packet['candidate']}（第{packet['chapter']}章，尚未成为正史）\n"
        f"正史截止：第{packet['chapter'] - 1}章（last_committed_chapter={packet['last_committed_chapter']}，"
        f"state_revision={packet['state_revision']}）\n"
        f"细纲文件：{packet['outline']}\n"
        "检查类型：事实冲突+伏笔断线+角色属性不一致+跨章位置/持有物漂移+长期事实/关系槽位冲突\n"
        "以 追踪/ 派生视图和第 N-1 章及以前的正文为准，只审候选稿与它们的冲突；不做文学判断，不改任何文件。"
        + common_tail
    )


def cmd_review_packet(args: argparse.Namespace) -> int:
    project, manifest_path, data = load_run(args.run)
    require_gated_draft(data, "review-packet")
    run = manifest_path.parent
    kind = args.kind
    require_fresh(project, data)
    candidate = project / data["candidate"]
    current_sha = record_lineage(run, data, candidate, "edit")
    gate = data.get("gate_run") or {}
    if gate.get("candidate_sha256") != current_sha or gate.get("status") != "pass":
        atomic_write_json(manifest_path, data)
        raise CandidateError("当前候选稿还没有通过确定性门禁；先运行 check（必要时 fix）再发审查包")
    if kind == "consistency":
        problem = deslop_problem(data, current_sha, require_full=requires_full_deslop(data))
        if problem:
            atomic_write_json(manifest_path, data)
            raise CandidateError(f"一致性审查要在去味审查之后：{problem}")
    scope = args.scope or ("full" if kind == "consistency" or requires_full_deslop(data) else "lean")
    if kind == "consistency" and scope != "full":
        raise CandidateError("一致性审查总是全文")
    if scope == "lean" and requires_full_deslop(data):
        raise CandidateError("review 模式、批次末章或未启用 lean 策略时，去味审查必须是全文")
    reviews = run / "reviews"
    text = candidate.read_text(encoding="utf-8")
    last, revision = tracking_snapshot(project)
    packet: dict[str, Any] = {
        "schema_version": 1,
        "kind": kind,
        "nonce": secrets.token_hex(8),
        "chapter": data["chapter"],
        "project": str(project),
        "candidate": data["candidate"],
        "candidate_sha256": current_sha,
        "outline": data["outline"],
        "scope": scope,
        "last_committed_chapter": last,
        "state_revision": revision,
        "agent": REVIEW_AGENTS[kind],
        "report_path": (reviews / f"{kind}.report.json").relative_to(project).as_posix(),
        "issued_at": utc_now(),
    }
    if kind == "deslop":
        work = reviews / "deslop" / "candidate.md"
        atomic_write_bytes(work, candidate.read_bytes())
        packet["work_copy"] = work.relative_to(project).as_posix()
        if scope == "lean":
            report = load_json(run / "gate-report.json", "门禁报告") if (run / "gate-report.json").is_file() else None
            packet["spans"] = lean_spans(text, report)
    packet["prompt"] = packet_prompt(kind, packet)
    atomic_write_json(reviews / f"{kind}.packet.json", packet)
    data["packets"][kind] = {
        "nonce": packet["nonce"],
        "candidate_sha256": current_sha,
        "scope": scope,
        "issued_at": packet["issued_at"],
    }
    atomic_write_json(manifest_path, data)
    emit_json(packet)
    return 0


def parse_report(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CandidateError(f"无法读取审查报告: {exc}") from exc
    text = raw.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", raw, flags=re.S)
        value = None
        for block in reversed(blocks):
            try:
                value = json.loads(block)
                break
            except json.JSONDecodeError:
                continue
        if value is None:
            raise CandidateError("审查报告里没有可解析的 JSON（整份 JSON 或 ```json 代码块）")
    if not isinstance(value, dict):
        raise CandidateError("审查报告必须是 JSON object")
    return value


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def validate_report(
    project: Path,
    kind: str,
    packet: dict[str, Any],
    report: dict[str, Any],
    reviewed_text: str,
) -> list[dict[str, Any]]:
    expected = {
        "kind": kind,
        "nonce": packet["nonce"],
        "candidate_sha256": packet["candidate_sha256"],
        "scope": packet["scope"],
    }
    for key, value in expected.items():
        if report.get(key) != value:
            raise CandidateError(f"审查报告 {key} 不匹配当前审查包（期望 {value}，收到 {report.get(key)}）")
    if report.get("verdict") not in VERDICTS:
        raise CandidateError(f"审查报告 verdict 必须是 {'/'.join(VERDICTS)}")
    findings = report.get("findings")
    coverage = report.get("coverage")
    if not isinstance(findings, list):
        raise CandidateError("审查报告 findings 必须是数组")
    if not isinstance(coverage, list) or not all(isinstance(item, str) and item.strip() for item in coverage):
        raise CandidateError("审查报告 coverage 必须是非空字符串数组")
    if not findings and len(coverage) < MIN_EMPTY_REVIEW_COVERAGE:
        raise CandidateError(f"没有 findings 的审查报告至少要在 coverage 列出 {MIN_EMPTY_REVIEW_COVERAGE} 项实际核对内容")
    squashed = squash(reviewed_text)
    normalized: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise CandidateError(f"findings[{index}] 必须是 object")
        severity = finding.get("severity")
        if severity not in SEVERITIES:
            raise CandidateError(f"findings[{index}].severity 必须是 {'/'.join(SEVERITIES)}")
        quote = finding.get("quote")
        if not isinstance(quote, str) or len(squash(quote)) < MIN_QUOTE_CHARS:
            raise CandidateError(f"findings[{index}].quote 必须是至少 {MIN_QUOTE_CHARS} 个字的原文摘录")
        action = finding.get("action", "none")
        if kind == "deslop" and action not in FINDING_ACTIONS:
            raise CandidateError(f"findings[{index}].action 必须是 {'/'.join(FINDING_ACTIONS)}")
        source = finding.get("source")
        haystack = squashed
        if kind == "consistency" and isinstance(source, str) and source.strip() and source.strip() != "candidate":
            path, _ = resolve_inside(project, source.strip(), must_exist=True, label=f"findings[{index}].source")
            haystack = squash(path.read_text(encoding="utf-8"))
        if squash(quote) not in haystack:
            missing.append(f"findings[{index}]「{quote[:30]}」")
        normalized.append(
            {
                "severity": severity,
                "category": str(finding.get("category", "")),
                "quote": quote,
                "issue": str(finding.get("issue", "")),
                "action": action if kind == "deslop" else "none",
                **({"source": source} if isinstance(source, str) and source.strip() else {}),
            }
        )
    if missing:
        raise CandidateError("以下 quote 在被审文本里找不到逐字原文（不得改写或概括）：" + "、".join(missing))
    return normalized


def cmd_attest(args: argparse.Namespace) -> int:
    project, manifest_path, data = load_run(args.run)
    require_gated_draft(data, "attest")
    run = manifest_path.parent
    kind = args.kind
    packet = data.get("packets", {}).get(kind)
    if not packet:
        raise CandidateError(f"没有未回执的{REVIEW_LABELS[kind]}审查包；先运行 review-packet --kind {kind}")
    candidate = project / data["candidate"]
    current_sha = sha256_file(candidate)
    if current_sha != packet["candidate_sha256"]:
        raise CandidateError("候选稿在发出审查包后被改动；审查结论已不对应当前稿，重新发审查包")
    report_path = Path(args.report).expanduser().resolve()
    report = parse_report(report_path)
    reviewed_text = lineage_text(run, current_sha) or candidate.read_text(encoding="utf-8")
    findings = validate_report(project, kind, packet, report, reviewed_text)
    report_bytes = report_path.read_bytes()
    stored_report = run / "reviews" / f"{kind}.report.json"
    if report_path != stored_report.resolve():
        atomic_write_bytes(stored_report, report_bytes)
    counts = {severity: sum(1 for item in findings if item["severity"] == severity) for severity in SEVERITIES}
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "kind": kind,
        "agent": REVIEW_AGENTS[kind],
        "nonce": packet["nonce"],
        "scope": packet["scope"],
        "verdict": report["verdict"],
        "severity_counts": counts,
        "coverage": report["coverage"],
        "summary": str(report.get("summary", ""))[:600],
        "report_sha256": sha256_bytes(report_bytes),
        "attested_at": utc_now(),
    }
    if kind == "deslop":
        work = run / "reviews" / "deslop" / "candidate.md"
        if not work.is_file() or not work.read_text(encoding="utf-8").strip():
            raise CandidateError("去味工作副本不存在或为空")
        output = work.read_bytes()
        if output != candidate.read_bytes():
            atomic_write_bytes(candidate, output)
        output_sha = record_lineage(run, data, candidate, "deslop")
        receipt.update(
            {
                "input_sha256": current_sha,
                "output_sha256": output_sha,
                "edit_ratio": edit_ratio(reviewed_text, output.decode("utf-8")),
                "flagged": sum(1 for item in findings if item["action"] == "flagged"),
            }
        )
    else:
        receipt.update(
            {
                "candidate_sha256": current_sha,
                "open_blocking": sum(1 for item in findings if item["severity"] in BLOCKING_SEVERITIES),
            }
        )
    receipt["findings"] = findings
    atomic_write_json(run / "reviews" / f"{kind}.receipt.json", receipt)
    data["reviews"][kind] = {key: value for key, value in receipt.items() if key != "findings"}
    data["packets"].pop(kind, None)
    atomic_write_json(manifest_path, data)
    summary = {key: value for key, value in receipt.items() if key not in {"findings", "coverage"}}
    if kind == "deslop" and receipt["output_sha256"] != receipt["input_sha256"]:
        summary["next"] = "去味改动已写回候选稿；先运行 check 复扫确定性门禁，再发一致性审查包"
    emit_json(summary)
    if report["verdict"] == "REJECT" or receipt.get("open_blocking"):
        print(f"注意：{REVIEW_LABELS[kind]}结论 {report['verdict']}，接纳前必须按报告处理", file=sys.stderr)
    return 0


def cmd_waive(args: argparse.Namespace) -> int:
    if args.confirm != "WAIVE":
        raise CandidateError("豁免门禁必须显式传入 --confirm WAIVE，且只在作者本人确认误报后使用")
    project, manifest_path, data = load_run(args.run)
    require_gated_draft(data, "waive")
    if data.get("approval_mode") != "review":
        raise CandidateError("auto 模式不允许豁免门禁：改稿，或改由作者逐章确认（review 模式）")
    if args.gate not in WAIVABLE_GATES:
        raise CandidateError(f"只有启发式门禁可以豁免：{', '.join(WAIVABLE_GATES)}")
    reason = (args.reason or "").strip()
    if not reason:
        raise CandidateError("豁免必须记录作者确认误报的理由")
    candidate = project / data["candidate"]
    current_sha = sha256_file(candidate)
    gate = data.get("gate_run") or {}
    if gate.get("candidate_sha256") != current_sha or args.gate not in gate.get("failures", []):
        raise CandidateError(f"{args.gate} 在当前候选稿的最近一次 check 里没有失败，无需豁免；先运行 check")
    waivers = [item for item in data.get("waivers", []) if item.get("gate") != args.gate]
    waivers.append({"gate": args.gate, "reason": reason, "candidate_sha256": current_sha, "waived_at": utc_now()})
    data["waivers"] = waivers
    atomic_write_json(manifest_path, data)
    emit_json({"waived": args.gate, "reason": reason, "next": "check"})
    return 0


def cmd_pressure(args: argparse.Namespace) -> int:
    project, manifest_path, data = load_run(args.run)
    require_gated_draft(data, "pressure")
    note = (args.note or "").strip()
    if not note:
        raise CandidateError("覆盖压力档必须写明理由")
    config, _ = load_gate_config(project)
    derived = resolve_pressure({**data, "pressure_override": None}, candidate_outline(project, data), config)
    if data.get("approval_mode") == "auto" and PRESSURE_ORDER[args.level] < PRESSURE_ORDER[derived["level"]]:
        raise CandidateError(f"auto 模式只能调高压力档（细纲推得 {derived['level']}）；调低需要作者在 review 模式确认")
    data["pressure_override"] = {"level": args.level, "note": note, "derived": derived, "set_at": utc_now()}
    atomic_write_json(manifest_path, data)
    emit_json({"pressure": args.level, "derived": derived, "next": "check"})
    return 0


# ── 命令：approve / promote / close / abandon / sync ───────────────────────


def cmd_approve(args: argparse.Namespace) -> int:
    if args.confirm != "ACCEPT":
        raise CandidateError("接纳候选稿必须显式传入 --confirm ACCEPT")
    project, manifest_path, data = load_run(args.run)
    if data.get("status") != "draft":
        raise CandidateError(f"只有 draft 候选可以接纳，当前状态: {data.get('status')}")
    note = (args.approval_note or data.get("authorization_note") or "").strip()
    if not note:
        raise CandidateError("必须记录用户的明确接纳说明或预先自动定稿授权")
    run = manifest_path.parent
    validation = validate_run(project, run, manifest_path, data)
    problems = review_problems(data, validation["candidate_sha256"])
    if problems:
        raise CandidateError(
            "接纳前流程未完成：\n- " + "\n- ".join(problems) + "\n运行 chapter_candidate.py next 查看下一步"
        )
    report = validation.pop("report")
    data["status"] = "approved"
    data["validation"] = {
        **{key: value for key, value in validation.items() if key != "gates"},
        "gate_report_sha256": data["gate_run"]["report_sha256"],
        "failures": report["failures"],
        "advisories": report["advisories"],
        "waived": report["waived"],
        "validated_at": utc_now(),
    }
    data["approval"] = {
        "approved_at": utc_now(),
        "mode": data.get("approval_mode"),
        "note": note,
        "candidate_sha256": validation["candidate_sha256"],
        "reviews": {kind: data["reviews"][kind].get("report_sha256") for kind in REVIEW_KINDS},
    }
    atomic_write_json(manifest_path, data)
    print(manifest_path)
    return 0


def receipt_path(project: Path, chapter: int) -> Path:
    return project / "追踪" / "章节提交" / f"第{chapter:03d}章.json"


def quality_dir(project: Path, chapter: int) -> Path:
    return project / "追踪" / QUALITY_DIR / f"第{chapter:03d}章"


def copy_quality_receipts(project: Path, run: Path, data: dict[str, Any], chapter: int) -> list[dict[str, Any]]:
    """接纳凭证随正文一起入库；候选工作区之后可以清理，质检证据不能跟着丢。"""
    destination = quality_dir(project, chapter)
    destination.mkdir(parents=True, exist_ok=True)
    copies: list[tuple[Path, str]] = [(run / "gate-report.json", "gate-report.json")]
    for kind in REVIEW_KINDS:
        copies.append((run / "reviews" / f"{kind}.receipt.json", f"{kind}.json"))
        copies.append((run / "reviews" / f"{kind}.report.json", f"{kind}.report.json"))
    entries: list[dict[str, Any]] = []
    for source, name in copies:
        if not source.is_file():
            raise CandidateError(f"质检回执缺失，拒绝写入正文: {source}")
        target = destination / name
        shutil.copyfile(source, target)
        entries.append({"path": target.relative_to(project).as_posix(), "sha256": sha256_file(target)})
    if data.get("waivers"):
        waivers = destination / "waivers.json"
        atomic_write_json(waivers, {"schema_version": 1, "waivers": data["waivers"]})
        entries.append({"path": waivers.relative_to(project).as_posix(), "sha256": sha256_file(waivers)})
    return entries


def projection_snapshot(project: Path, chapter: int) -> list[dict[str, Any]]:
    tracking = project / "追踪"
    paths = [
        tracking / "上下文.md",
        tracking / "伏笔.md",
        tracking / "长期事实.md",
        tracking / "关系清单.md",
        tracking / "时间线" / "作者真相.md",
        tracking / "时间线" / "读者已知.md",
        tracking / "逐章记录" / f"第{chapter:03d}章.md",
    ]
    paths.extend(sorted((tracking / "角色状态").glob("*.md")) if (tracking / "角色状态").is_dir() else [])
    paths.extend(sorted((tracking / "事实档案").glob("*.md")) if (tracking / "事实档案").is_dir() else [])
    entries: list[dict[str, Any]] = []
    for path in paths:
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(project).as_posix(),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
    return entries


def append_projection_event(
    project: Path,
    *,
    event: str,
    chapter: int,
    state_revision: int,
    prose_sha256: str,
    receipt: Path,
) -> None:
    atomic_append_jsonl(
        project / "追踪" / "投影日志.jsonl",
        {
            "schema_version": 1,
            "event": event,
            "recorded_at": utc_now(),
            "chapter": chapter,
            "state_revision": state_revision,
            "accepted_prose_sha256": prose_sha256,
            "receipt": receipt.relative_to(project).as_posix(),
            "projections": projection_snapshot(project, chapter),
        },
    )


def cmd_promote(args: argparse.Namespace) -> int:
    if args.confirm != "PROMOTE":
        raise CandidateError("写入正式正文必须显式传入 --confirm PROMOTE")
    project, manifest_path, data = load_run(args.run)
    if data.get("status") != "approved":
        raise CandidateError(f"只有 approved 候选可以写入正式正文，当前状态: {data.get('status')}")
    run = manifest_path.parent
    approved_sha = (data.get("approval") or {}).get("candidate_sha256")
    candidate = project / data["candidate"]
    if candidate.is_file() and sha256_file(candidate) != approved_sha:
        raise CandidateError("候选稿在接纳后被修改，必须回到 draft 重新审阅")
    validation = validate_run(project, run, manifest_path, data)
    if validation["candidate_sha256"] != approved_sha:
        raise CandidateError("候选稿在接纳后被修改，必须回到 draft 重新审阅")
    if is_gated(data):
        problems = review_problems(data, approved_sha)
        if problems:
            raise CandidateError("接纳凭证已失效：\n- " + "\n- ".join(problems))
    chapter = int(data["chapter"])
    target = project / data["target"]
    receipt = receipt_path(project, chapter)
    if receipt.exists():
        raise CandidateError(f"章节提交凭证已存在: {receipt}")
    quality = copy_quality_receipts(project, run, data, chapter) if is_gated(data) else []
    content = candidate.read_bytes()
    atomic_write_bytes(target, content)
    target_sha = sha256_bytes(content)
    promoted_at = utc_now()
    receipt_payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "awaiting_tracking",
        "chapter": data["chapter"],
        "target": data["target"],
        "accepted_prose_sha256": target_sha,
        "candidate_id": data["candidate_id"],
        "candidate_manifest": manifest_path.relative_to(project).as_posix(),
        "context_digest": data["context_digest"],
        "state_revision_before": data["expected_state_revision"],
        "state_revision_after": None,
        "accepted_at": promoted_at,
        "approval_mode": data["approval_mode"],
        "approval_note": data["approval"]["note"],
        "sync_history": [],
        "protocol": PROTOCOL if is_gated(data) else LEGACY_PROTOCOL,
    }
    if is_gated(data):
        receipt_payload.update(
            {
                "gated_prose_sha256": target_sha,
                "review_policy": data.get("review_policy"),
                "quality_receipts": quality,
                "waived_gates": [item["gate"] for item in data.get("waivers", [])],
            }
        )
    atomic_write_json(receipt, receipt_payload)
    data["status"] = "promoted"
    data["promotion"] = {"promoted_at": promoted_at, "target_sha256": target_sha, "receipt": receipt.relative_to(project).as_posix()}
    atomic_write_json(manifest_path, data)
    print(target)
    return 0


CORPUS_POOL_ENV = "STORY_DESLOP_CORPUS_POOL"
DEFAULT_CORPUS_POOL = Path("~/Documents/小说/_去AI味语料/网文")


def append_deslop_corpus(project: Path, chapter: int, pool_arg: str | None) -> None:
    """接纳闭环后把本章 M 层与谱系 A1/A2 快照幂等追加进去 AI 味语料池（仓外，规则表滚动维护用）。

    语料池目录按 --corpus-pool、环境变量 STORY_DESLOP_CORPUS_POOL、缺省 ~/Documents/小说/_去AI味语料/网文 取；
    追加器是池旁 ../_工具/build_corpus.py。池或追加器不存在只提示跳过；追加失败只告警。都不阻断接纳。
    --corpus-pool off（或环境变量为 off）关闭本步。
    """
    raw = pool_arg if pool_arg is not None else os.environ.get(CORPUS_POOL_ENV)
    if raw is not None and raw.strip().lower() in {"off", "none", "0", ""}:
        return
    pool = Path(raw).expanduser() if raw else DEFAULT_CORPUS_POOL.expanduser()
    tool = pool.parent / "_工具" / "build_corpus.py"
    if not (pool / "manifest.json").is_file() or not tool.is_file():
        print(f"语料池不存在（{pool}），跳过去 AI 味语料追加；不影响接纳", file=sys.stderr)
        return
    try:
        completed = subprocess.run(
            [sys.executable, str(tool), "append", "--project", str(project), "--chapter", str(chapter), "--pool-dir", str(pool)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"去 AI 味语料追加未完成（{exc}）；不影响接纳，可稍后在池旁运行 build_corpus.py build 补齐", file=sys.stderr)
        return
    message = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    if completed.returncode != 0:
        print(f"去 AI 味语料追加失败（退出 {completed.returncode}）：{message[-300:]}；不影响接纳", file=sys.stderr)
    elif message:
        print(message.splitlines()[-1], file=sys.stderr)


def cmd_close(args: argparse.Namespace) -> int:
    project, manifest_path, data = load_run(args.run)
    if data.get("status") != "promoted":
        raise CandidateError(f"只有 promoted 候选可以闭环，当前状态: {data.get('status')}")
    chapter = int(data["chapter"])
    receipt = receipt_path(project, chapter)
    receipt_data = load_json(receipt, "章节提交凭证")
    last, revision = tracking_snapshot(project)
    if last != chapter:
        raise CandidateError(f"追踪尚未精确提交第 {chapter} 章，当前 last_committed_chapter={last}")
    if revision <= int(data["expected_state_revision"]):
        raise CandidateError("追踪 state_revision 未推进")
    target = project / data["target"]
    target_sha = sha256_file(target)
    if target_sha != receipt_data.get("accepted_prose_sha256"):
        raise CandidateError("正式正文与接纳摘要不一致")
    closed_at = utc_now()
    receipt_data["status"] = "committed"
    receipt_data["state_revision_after"] = revision
    receipt_data["closed_at"] = closed_at
    atomic_write_json(receipt, receipt_data)
    data["status"] = "committed"
    data["closed_at"] = closed_at
    data["state_revision_after"] = revision
    atomic_write_json(manifest_path, data)
    append_projection_event(
        project,
        event="chapter_commit",
        chapter=chapter,
        state_revision=revision,
        prose_sha256=target_sha,
        receipt=receipt,
    )
    write_progress(project)
    append_deslop_corpus(project, chapter, getattr(args, "corpus_pool", None))
    print(receipt)
    return 0


def cmd_abandon(args: argparse.Namespace) -> int:
    if args.confirm != "ABANDON":
        raise CandidateError("放弃候选稿必须显式传入 --confirm ABANDON")
    _, manifest_path, data = load_run(args.run)
    if data.get("status") not in {"draft", "approved"}:
        raise CandidateError("只有尚未写入正式正文的候选可以放弃")
    data["status"] = "abandoned"
    data["abandoned_at"] = utc_now()
    data["abandon_reason"] = (args.reason or "").strip()
    atomic_write_json(manifest_path, data)
    print(manifest_path)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    if args.confirm != "SYNC":
        raise CandidateError("同步合法修订后的正文摘要必须显式传入 --confirm SYNC")
    project = Path(args.project).expanduser().resolve()
    if not (args.reason or "").strip():
        raise CandidateError("sync 必须记录修订原因")
    chapter = args.chapter
    receipt = receipt_path(project, chapter)
    data = load_json(receipt, "章节提交凭证")
    if data.get("status") != "committed":
        raise CandidateError("只能同步已闭环章节")
    revision_tool = Path(__file__).resolve().parent / "revision_guard.py"
    command = [
        sys.executable,
        str(revision_tool),
        "check",
        "--project",
        str(project),
        "--input",
        str(Path(args.revision_manifest).expanduser().resolve()),
        "--stamp",
        str(Path(args.revision_stamp).expanduser().resolve()),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise CandidateError(f"修订门禁未通过，拒绝同步摘要\n{completed.stdout}{completed.stderr}".rstrip())
    target = project / str(data.get("target", ""))
    if not target.is_file() or chapter_number(target) != chapter:
        raise CandidateError("章节提交凭证指向的正文不存在或章号不匹配")
    last, revision = tracking_snapshot(project)
    if last < chapter:
        raise CandidateError("追踪状态落后于待同步章节")
    old_sha = data.get("accepted_prose_sha256")
    new_sha = sha256_file(target)
    history = data.get("sync_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "synced_at": utc_now(),
            "reason": args.reason.strip(),
            "old_sha256": old_sha,
            "new_sha256": new_sha,
            "state_revision": revision,
            "revision_manifest": Path(args.revision_manifest).expanduser().resolve().relative_to(project).as_posix(),
        }
    )
    data["accepted_prose_sha256"] = new_sha
    data["state_revision_after"] = revision
    data["sync_history"] = history
    atomic_write_json(receipt, data)
    append_projection_event(
        project,
        event="revision_sync",
        chapter=chapter,
        state_revision=revision,
        prose_sha256=new_sha,
        receipt=receipt,
    )
    print(receipt)
    return 0


# ── 质检进度（由回执生成） ─────────────────────────────────────────────────


def gate_cell(report: dict[str, Any] | None) -> str:
    if not report:
        return "缺门禁报告"
    gates = report.get("gates", [])
    passed = sum(1 for item in gates if item.get("status") == "pass")
    parts = [f"{passed}/{len(gates)} 通过"]
    if report.get("waived"):
        parts.append("豁免 " + "、".join(report["waived"]))
    if report.get("advisories"):
        parts.append("提示 " + "、".join(report["advisories"]))
    return "；".join(parts)


def review_cell(receipt: dict[str, Any] | None) -> str:
    if not receipt:
        return "—"
    counts = receipt.get("severity_counts", {})
    serious = int(counts.get("S1", 0)) + int(counts.get("S2", 0))
    cell = f"{receipt.get('verdict')}（{'全文' if receipt.get('scope') == 'full' else '精简'}）"
    if serious:
        cell += f"，S1-S2 {serious}"
    return cell


def load_optional(path: Path) -> dict[str, Any] | None:
    try:
        return load_json(path, str(path.name))
    except CandidateError:
        return None


def build_progress(project: Path) -> str:
    rows: list[str] = []
    legacy = 0
    for path in sorted((project / "追踪" / "章节提交").glob("第*章.json")):
        receipt = load_optional(path)
        if not receipt:
            continue
        chapter = receipt.get("chapter")
        label = f"第{int(chapter):03d}章" if isinstance(chapter, int) else path.stem
        status = "已闭环" if receipt.get("status") == "committed" else "待追踪"
        mode = "自动定稿" if receipt.get("approval_mode") == "auto" else "作者接纳"
        if receipt.get("protocol") == PROTOCOL and isinstance(chapter, int):
            directory = quality_dir(project, chapter)
            gate_report = load_optional(directory / "gate-report.json")
            metrics = next(
                (item.get("payload") for item in (gate_report or {}).get("gates", []) if item.get("name") == "prose_metrics"),
                None,
            )
            words = metrics.get("character_count") if isinstance(metrics, dict) else None
            pressure = PRESSURE_LABELS.get(((gate_report or {}).get("pressure") or {}).get("level", ""), "—")
            rows.append(
                f"| {label} | {status} | {mode} | {gate_cell(gate_report)} | {pressure} | "
                f"{review_cell(load_optional(directory / 'deslop.json'))} | "
                f"{review_cell(load_optional(directory / 'consistency.json'))} | {words if words is not None else '—'} |"
            )
        else:
            legacy += 1
            rows.append(f"| {label} | {status} | {mode} | 旧协议，无回执 | — | — | — | — |")
    lines = [
        PROGRESS_MARKER,
        "# 质检进度",
        "",
        "> 本表由接纳回执自动生成：每章的确定性门禁、去味审查和一致性审查都在作者接纳**之前**完成，",
        "> 结论绑定候选稿 SHA-256，原件在 `追踪/质检回执/第NNN章/`。表里没有的就是没做过。",
        "",
        "| 章号 | 状态 | 接纳方式 | 确定性门禁 | 压力档 | 去味审查 | 一致性审查 | 字数 |",
        "|---|---|---|---|---|---|---|---:|",
        *rows,
    ]
    if legacy:
        lines.extend(["", f"> {legacy} 章按旧协议接纳，没有接纳前质检回执；需要补查时用 story-review 审已写正文。"])
    return "\n".join(lines) + "\n"


def write_progress(project: Path) -> Path:
    path = project / "追踪" / PROGRESS_FILE
    if path.is_file() and PROGRESS_MARKER not in path.read_text(encoding="utf-8"):
        archive = project / "追踪" / PROGRESS_LEGACY_ARCHIVE
        if archive.exists():
            archive = archive.with_name(f"质检进度_旧版手工记录_{datetime.now().strftime('%Y%m%d%H%M%S')}.md")
        os.replace(path, archive)
    atomic_write_bytes(path, build_progress(project).encode("utf-8"))
    return path


def cmd_progress(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise CandidateError(f"项目目录不存在: {project}")
    print(write_progress(project))
    return 0


# ── 命令：next ─────────────────────────────────────────────────────────────


def guess_outline(project: Path, chapter: int) -> str:
    matches = sorted((project / "大纲").glob(f"细纲_第{chapter:03d}章*.md")) + sorted(
        (project / "大纲").glob(f"细纲_第{chapter}章*.md")
    )
    return matches[0].relative_to(project).as_posix() if matches else f"大纲/细纲_第{chapter:03d}章.md"


def next_step(project: Path) -> dict[str, Any]:
    tool = "chapter_candidate.py"
    runs = open_workspaces(project)
    if not runs:
        last, _ = tracking_snapshot(project)
        chapter = last + 1
        return {
            "step": "init",
            "chapter": chapter,
            "commands": [
                f'{tool} init --project "{project}" --chapter {chapter} --outline "{guess_outline(project, chapter)}" '
                f'--target "正文/第{chapter:03d}章_{{标题}}.md" [--base 卷纲/题材契约] [--approval-mode auto '
                '--authorization-note "…" [--review-policy lean] [--batch-last]]'
            ],
            "why": "没有未闭环候选章；先跑 story_doctor.py 确认上一章已闭环，再为下一章创建精确许可。",
        }
    if len(runs) > 1:
        return {"step": "resolve", "runs": [str(path) for path in runs], "why": "存在多个未闭环候选章，先 abandon 多余的。"}
    run = runs[0]
    _, manifest_path, data = load_run(str(run))
    chapter = data.get("chapter")
    base = {"run": str(run), "chapter": chapter, "status": data.get("status"), "approval_mode": data.get("approval_mode")}
    status = data.get("status")
    if status == "approved":
        return {**base, "step": "promote", "commands": [f'{tool} promote --run "{run}" --confirm PROMOTE']}
    if status == "promoted":
        return {
            **base,
            "step": "tracking",
            "commands": [
                'tracking_commit.py check --project "{项目根}"（取 state_revision）',
                'tracking_commit.py commit --project "{项目根}" --input {本章事务.json}',
                f'{tool} close --run "{run}"',
                'voice_profile.py update --project "{项目根}"',
                'story_doctor.py --project "{项目根}"',
            ],
            "why": "正文已写入；提交本章唯一追踪事务后 close，再更新声音画像并跑 doctor。",
        }
    if not is_gated(data):
        return {**base, "step": "legacy", "why": "旧版候选只能按原流程 approve/promote。"}
    candidate = project / data["candidate"]
    text = candidate.read_text(encoding="utf-8") if candidate.is_file() else ""
    if not text.strip():
        return {
            **base,
            "step": "write",
            "candidate": data["candidate"],
            "why": "候选稿为空：按细纲写本章，只写入这个候选文件，不碰正文/。",
            "commands": [f'{tool} check --run "{run}"'],
        }
    current_sha = sha256_bytes(candidate.read_bytes())
    gate = data.get("gate_run") or {}
    if gate.get("candidate_sha256") != current_sha:
        return {**base, "step": "check", "commands": [f'{tool} check --run "{run}"'], "why": "候选稿有改动，先跑确定性门禁。"}
    if gate.get("status") != "pass":
        failures = gate.get("failures", [])
        commands = []
        if "punctuation" in failures:
            commands.append(f'{tool} fix --run "{run}"')
        commands.append(f'{tool} check --run "{run}"')
        why = "门禁未通过：" + "、".join(failures) + "。按 gate-report.json 改稿；标点问题用 fix 自动修正。"
        waivable = [name for name in failures if name in WAIVABLE_GATES]
        if waivable and data.get("approval_mode") == "review":
            why += f"{'、'.join(waivable)} 是启发式门禁：确属误报时先问作者，作者确认后 waive。"
        return {**base, "step": "revise", "failures": failures, "commands": commands, "why": why}
    packets = data.get("packets", {})
    for kind in REVIEW_KINDS:
        problem = (
            deslop_problem(data, current_sha, require_full=requires_full_deslop(data))
            if kind == "deslop"
            else consistency_problem(data, current_sha)
        )
        if not problem:
            continue
        packet = packets.get(kind)
        if packet and packet.get("candidate_sha256") == current_sha:
            report = run / "reviews" / f"{kind}.report.json"
            return {
                **base,
                "step": "attest",
                "kind": kind,
                "commands": [f'{tool} attest --run "{run}" --kind {kind} --report "{report}"'],
                "why": f"审查包已发出：把 {REVIEW_AGENTS[kind]} 的回复原样存到 {report.name} 后登记回执。",
            }
        return {
            **base,
            "step": "review",
            "kind": kind,
            "agent": REVIEW_AGENTS[kind],
            "commands": [f'{tool} review-packet --run "{run}" --kind {kind}'],
            "why": f"{problem}。发审查包，把输出里的 prompt 原样交给 {REVIEW_AGENTS[kind]}。",
        }
    problems = review_problems(data, current_sha)
    if problems:
        return {**base, "step": "revise", "commands": [f'{tool} check --run "{run}"'], "why": "；".join(problems)}
    if data.get("approval_mode") == "auto":
        return {
            **base,
            "step": "approve",
            "commands": [f'{tool} approve --run "{run}" --confirm ACCEPT'],
            "why": "全部门禁与审查已通过，自动定稿授权有效。",
        }
    return {
        **base,
        "step": "await-author",
        "commands": [f'{tool} approve --run "{run}" --confirm ACCEPT --approval-note "{{作者原话}}"'],
        "why": "全部门禁与审查已通过。向作者展示标题、字数、关键变化、门禁提示和候选路径后停止；"
        "只有作者明确说“接受/定稿/采用这版”才 approve。",
    }


def cmd_next(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise CandidateError(f"项目目录不存在: {project}")
    emit_json(next_step(project))
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="创建只绑定下一章的候选工作区")
    init.add_argument("--project", required=True)
    init.add_argument("--chapter", type=int, required=True)
    init.add_argument("--outline", required=True)
    init.add_argument("--target", required=True)
    init.add_argument("--base", action="append")
    init.add_argument("--approval-mode", choices=("review", "auto"), default="review")
    init.add_argument("--authorization-note")
    init.add_argument("--review-policy", choices=REVIEW_SCOPES, default="full",
                      help="auto 模式可选 lean：去味只审被标记段落与开头结尾；一致性总是全文")
    init.add_argument("--batch-last", action="store_true", help="本章是 auto 批次的最后一章：去味必须全文")
    init.add_argument("--id")
    init.set_defaults(func=cmd_init)

    check = sub.add_parser("check", help="检查陈旧状态并运行全部确定性门禁，写 gate-report.json")
    check.add_argument("--run", required=True)
    check.add_argument("--freshness-only", action="store_true")
    check.set_defaults(func=cmd_check)

    fix = sub.add_parser("fix", help="在候选稿上做确定性标点收尾并登记改动")
    fix.add_argument("--run", required=True)
    fix.set_defaults(func=cmd_fix)

    packet = sub.add_parser("review-packet", help="为去味/一致性审查发出绑定当前候选稿的审查包")
    packet.add_argument("--run", required=True)
    packet.add_argument("--kind", choices=REVIEW_KINDS, required=True)
    packet.add_argument("--scope", choices=REVIEW_SCOPES)
    packet.set_defaults(func=cmd_review_packet)

    attest = sub.add_parser("attest", help="校验审查报告并登记回执（去味改动由本命令写回候选稿）")
    attest.add_argument("--run", required=True)
    attest.add_argument("--kind", choices=REVIEW_KINDS, required=True)
    attest.add_argument("--report", required=True)
    attest.set_defaults(func=cmd_attest)

    waive = sub.add_parser("waive", help="review 模式下经作者确认豁免一道启发式门禁的误报")
    waive.add_argument("--run", required=True)
    waive.add_argument("--gate", choices=WAIVABLE_GATES, required=True)
    waive.add_argument("--reason", required=True)
    waive.add_argument("--confirm", required=True)
    waive.set_defaults(func=cmd_waive)

    pressure = sub.add_parser("pressure", help="覆盖细纲推出的情绪下限压力档（留痕）")
    pressure.add_argument("--run", required=True)
    pressure.add_argument("--level", choices=PRESSURES, required=True)
    pressure.add_argument("--note", required=True)
    pressure.set_defaults(func=cmd_pressure)

    approve = sub.add_parser("approve", help="记录用户接纳并锁定候选摘要（要求门禁通过与两份审查回执）")
    approve.add_argument("--run", required=True)
    approve.add_argument("--confirm", required=True)
    approve.add_argument("--approval-note")
    approve.set_defaults(func=cmd_approve)

    promote = sub.add_parser("promote", help="把已接纳候选原子写入正式正文，并把质检回执入库")
    promote.add_argument("--run", required=True)
    promote.add_argument("--confirm", required=True)
    promote.set_defaults(func=cmd_promote)

    close = sub.add_parser("close", help="追踪提交后闭环本章提交凭证并重建质检进度")
    close.add_argument("--run", required=True)
    close.add_argument("--corpus-pool", help="去 AI 味语料池目录；off 关闭追加（缺省读 STORY_DESLOP_CORPUS_POOL，再缺省 ~/Documents/小说/_去AI味语料/网文）")
    close.set_defaults(func=cmd_close)

    abandon = sub.add_parser("abandon", help="放弃尚未写入正式正文的候选")
    abandon.add_argument("--run", required=True)
    abandon.add_argument("--confirm", required=True)
    abandon.add_argument("--reason")
    abandon.set_defaults(func=cmd_abandon)

    sync = sub.add_parser("sync", help="仅在修订事务闭环后同步正文摘要")
    sync.add_argument("--project", required=True)
    sync.add_argument("--chapter", type=int, required=True)
    sync.add_argument("--revision-manifest", required=True)
    sync.add_argument("--revision-stamp", required=True)
    sync.add_argument("--reason", required=True)
    sync.add_argument("--confirm", required=True)
    sync.set_defaults(func=cmd_sync)

    progress = sub.add_parser("progress", help="根据接纳回执重建 追踪/质检进度.md")
    progress.add_argument("--project", required=True)
    progress.set_defaults(func=cmd_progress)

    step = sub.add_parser("next", help="只打印本书当前该做的下一步")
    step.add_argument("--project", required=True)
    step.set_defaults(func=cmd_next)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except CandidateError as exc:
        sys.stderr.flush()
        sys.stderr.buffer.write(f"error: {exc}\n".encode("utf-8"))
        sys.stderr.buffer.flush()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
