#!/usr/bin/env python3
"""Build and enforce a cross-artifact revision impact checklist for a novel project."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REVISION_MODES = ("full", "prose_only")
CHAPTER_RE = re.compile(r"第0*(\d+)章")
PROSE_FILE_RE = re.compile(r"^正文/第0*(\d+)章.*\.md$")
CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{3,80}$")
SOURCE_ROOTS = ("正文", "大纲", "设定")
DERIVED_TRACKING_PREFIXES = (
    "追踪/上下文.md", "追踪/伏笔.md", "追踪/长期事实.md", "追踪/关系清单.md",
    "追踪/角色状态/", "追踪/事实档案/", "追踪/时间线/", "追踪/逐章记录/",
)
IGNORED_SOURCE_DIR_NAMES = {"归档", "archive", "archives"}


class GuardError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GuardError(message)


def as_mapping(value: object, label: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def as_list(value: object, label: str) -> list[Any]:
    require(isinstance(value, list), f"{label} must be a JSON array")
    return value


def clean_text(value: object, label: str, *, allow_empty: bool = False) -> str:
    require(isinstance(value, str), f"{label} must be a string")
    text = " ".join(value.split())
    require(allow_empty or text, f"{label} must not be empty")
    return text


def normalize_relative(project: Path, value: object, label: str, *, must_exist: bool = True) -> str:
    raw = clean_text(value, label)
    path = Path(raw)
    require(not path.is_absolute(), f"{label} must be project-relative")
    resolved = (project / path).resolve()
    try:
        relative = resolved.relative_to(project.resolve())
    except ValueError as exc:
        raise GuardError(f"{label} escapes project root") from exc
    require(not any(part in {"", ".", ".."} for part in relative.parts), f"{label} is invalid")
    if must_exist:
        require(resolved.is_file(), f"{label} does not exist: {relative.as_posix()}")
    return relative.as_posix()


def atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def is_active_source_relative(relative: str) -> bool:
    """Ignore historical copies; only active canon artifacts participate in the gate."""
    parent_parts = Path(relative).parts[:-1]
    return not any(
        "备份" in part or part in IGNORED_SOURCE_DIR_NAMES or part.startswith(".")
        for part in parent_parts
    )


def semantic_files(project: Path) -> list[Path]:
    paths: list[Path] = []
    for root_name in SOURCE_ROOTS:
        root = project / root_name
        if root.is_dir():
            paths.extend(
                path for path in root.rglob("*.md")
                if path.is_file() and is_active_source_relative(path.relative_to(project).as_posix())
            )
    return sorted(paths)


def chapter_paths(project: Path, chapter: int) -> list[Path]:
    result: list[Path] = []
    for root_name in ("正文", "大纲"):
        root = project / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            if not is_active_source_relative(path.relative_to(project).as_posix()):
                continue
            match = CHAPTER_RE.search(path.name)
            if match and int(match.group(1)) == chapter:
                result.append(path)
    return result


def is_broad_source(relative: str) -> bool:
    name = Path(relative).name
    return relative.startswith("设定/") or "总纲" in name or "卷纲" in name or name == "大纲.md"


def expands_all_outlines(relative: str) -> bool:
    name = Path(relative).name
    return relative.startswith("大纲/") and ("总纲" in name or "卷纲" in name or name == "大纲.md")


def required_checks(
    project: Path,
    changed_files: list[str],
    entities: list[str],
    chapters: list[int],
    *,
    mode: str = "full",
) -> list[str]:
    if mode == "prose_only":
        return sorted(set(changed_files))
    required = set(changed_files)
    broad = any(expands_all_outlines(path) for path in changed_files)

    for relative in changed_files:
        match = CHAPTER_RE.search(Path(relative).name)
        if match:
            chapters.append(int(match.group(1)))
    chapter_numbers = sorted(set(chapters))
    for chapter in chapter_numbers:
        for candidate in (chapter - 1, chapter, chapter + 1):
            if candidate < 1:
                continue
            required.update(path.relative_to(project).as_posix() for path in chapter_paths(project, candidate))

    outlines = project / "大纲"
    if outlines.is_dir():
        for path in outlines.rglob("*.md"):
            if broad or path.name == "大纲.md" or "总纲" in path.name or "卷纲" in path.name:
                required.add(path.relative_to(project).as_posix())

    if entities:
        for path in semantic_files(project):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if any(entity in text for entity in entities):
                required.add(path.relative_to(project).as_posix())

    for fixed in ("追踪/长期事实.md", "追踪/关系清单.md", "追踪/上下文.md"):
        if (project / fixed).is_file():
            required.add(fixed)
    for entity in entities:
        for relative in (f"追踪/事实档案/{entity}.md", f"追踪/角色状态/{entity}.md"):
            if (project / relative).is_file():
                required.add(relative)
    return sorted(required)


def normalize_plan_request(project: Path, document: object) -> dict[str, Any]:
    root = as_mapping(document, "revision plan")
    allowed = {
        "schema_version", "mode", "change_id", "summary", "semantic_change", "changed_files",
        "affected_entities", "affected_fact_ids", "affected_chapters", "polish_baselines",
    }
    require(not (set(root) - allowed), f"revision plan contains unsupported fields: {sorted(set(root) - allowed)}")
    require(root.get("schema_version") == SCHEMA_VERSION, "revision plan schema_version is unsupported")
    raw_mode = root.get("mode", "full")
    mode = clean_text(raw_mode, "mode") if raw_mode is not None else "full"
    require(mode in REVISION_MODES, f"mode must be one of {REVISION_MODES}")
    change_id = clean_text(root.get("change_id"), "change_id")
    require(CHANGE_ID_RE.fullmatch(change_id) is not None, "change_id must use 3-80 ASCII letters, digits, dot, dash or underscore")
    semantic = root.get("semantic_change")
    require(isinstance(semantic, bool), "semantic_change must be a boolean")
    if mode == "prose_only":
        require(not semantic, "prose_only mode requires semantic_change=false")
    changed = [
        normalize_relative(project, item, f"changed_files[{index}]")
        for index, item in enumerate(as_list(root.get("changed_files", []), "changed_files"))
    ]
    require(changed, "changed_files must not be empty")
    require(len(changed) == len(set(changed)), "changed_files contains duplicates")
    state_path = project / "追踪" / "_tracking-state.json"
    last_committed: int | None = None
    if state_path.is_file():
        state_obj = read_json(state_path)
        if isinstance(state_obj, dict) and isinstance(state_obj.get("last_committed_chapter"), int):
            last_committed = state_obj["last_committed_chapter"]
    for relative in changed:
        require(relative.startswith(tuple(f"{root_name}/" for root_name in SOURCE_ROOTS)), f"unsupported source artifact: {relative}")
        require(is_active_source_relative(relative), f"backup/archive artifacts are not active canon sources: {relative}")
        require(not relative.startswith(DERIVED_TRACKING_PREFIXES), f"derived tracking views must not be edited: {relative}")
        if mode == "prose_only":
            match = PROSE_FILE_RE.fullmatch(relative)
            require(
                match is not None and "_原稿_" not in Path(relative).name,
                f"prose_only mode only accepts active committed chapter prose files: {relative}",
            )
            chap = int(match.group(1))
            require(
                last_committed is None or chap <= last_committed,
                f"prose_only chapter {chap} exceeds last_committed_chapter={last_committed}",
            )
    entities = [clean_text(item, f"affected_entities[{index}]") for index, item in enumerate(as_list(root.get("affected_entities", []), "affected_entities"))]
    fact_ids = [clean_text(item, f"affected_fact_ids[{index}]") for index, item in enumerate(as_list(root.get("affected_fact_ids", []), "affected_fact_ids"))]
    require(all(re.fullmatch(r"[KR]\d{3,}", item) for item in fact_ids), "affected_fact_ids must look like K001 or R001")
    chapters = as_list(root.get("affected_chapters", []), "affected_chapters")
    require(all(isinstance(item, int) and not isinstance(item, bool) and item >= 1 for item in chapters), "affected_chapters must contain positive integers")
    if any(is_broad_source(path) for path in changed):
        require(entities or chapters, "settings/master/volume outline changes must declare affected_entities or affected_chapters")
    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "change_id": change_id,
        "summary": clean_text(root.get("summary"), "summary"),
        "semantic_change": semantic,
        "changed_files": sorted(changed),
        "affected_entities": sorted(set(entities)),
        "affected_fact_ids": sorted(set(fact_ids)),
        "affected_chapters": sorted(set(chapters)),
    }
    if mode == "prose_only":
        normalized["mode"] = "prose_only"
        raw_baselines = root.get("polish_baselines")
        if raw_baselines is not None:
            baselines_map = as_mapping(raw_baselines, "polish_baselines")
            baselines: dict[str, str] = {}
            for key, val in baselines_map.items():
                rel = normalize_relative(project, key, f"polish_baselines.{key}")
                digest = clean_text(val, f"polish_baselines.{key}")
                require(bool(re.fullmatch(r"[0-9a-f]{64}", digest)), f"polish_baselines.{key} must be a sha256 hex digest")
                baselines[rel] = digest
            normalized["polish_baselines"] = dict(sorted(baselines.items()))
    return normalized


def make_manifest(project: Path, document: object) -> dict[str, Any]:
    plan = normalize_plan_request(project, document)
    mode = plan.get("mode", "full")
    required = required_checks(
        project,
        plan["changed_files"],
        plan["affected_entities"],
        list(plan["affected_chapters"]),
        mode=mode,
    )
    return {
        **plan,
        "required_checks": required,
        "checked_files": [],
        "conflicts": [],
        "tracking_action": {
            "kind": "pending" if plan["semantic_change"] else "not_required",
            "state_revision_before": None,
            "state_revision_after": None,
            "reason": "" if plan["semantic_change"] else "仅格式/措辞修正，不改变剧情事实与未来约束",
        },
    }


def check_manifest(project: Path, document: object) -> dict[str, Any]:
    root = as_mapping(document, "revision manifest")
    plan_fields: dict[str, Any] = {
        key: root.get(key)
        for key in (
            "schema_version", "change_id", "summary", "semantic_change", "changed_files",
            "affected_entities", "affected_fact_ids", "affected_chapters",
        )
    }
    if "mode" in root:
        plan_fields["mode"] = root.get("mode")
    if "polish_baselines" in root:
        plan_fields["polish_baselines"] = root.get("polish_baselines")
    plan = normalize_plan_request(project, plan_fields)
    mode = plan.get("mode", "full")
    expected = required_checks(
        project,
        plan["changed_files"],
        plan["affected_entities"],
        list(plan["affected_chapters"]),
        mode=mode,
    )
    declared_required = [normalize_relative(project, item, f"required_checks[{index}]") for index, item in enumerate(as_list(root.get("required_checks"), "required_checks"))]
    require(declared_required == expected, "required_checks is stale or was edited; regenerate the plan")
    checked = {
        normalize_relative(project, item, f"checked_files[{index}]")
        for index, item in enumerate(as_list(root.get("checked_files"), "checked_files"))
    }
    missing = sorted(set(expected) - checked)
    require(not missing, "revision impact review is incomplete; unchecked files: " + ", ".join(missing))

    conflicts = as_list(root.get("conflicts", []), "conflicts")
    normalized_conflicts = []
    for index, raw in enumerate(conflicts):
        conflict = as_mapping(raw, f"conflicts[{index}]")
        allowed = {"severity", "source", "target", "issue", "resolution", "status"}
        require(not (set(conflict) - allowed), f"conflicts[{index}] contains unsupported fields")
        severity = clean_text(conflict.get("severity"), f"conflicts[{index}].severity")
        require(severity in {"S1", "S2", "S3", "S4"}, f"conflicts[{index}].severity is invalid")
        status = clean_text(conflict.get("status"), f"conflicts[{index}].status")
        require(status in {"resolved", "accepted", "unresolved"}, f"conflicts[{index}].status is invalid")
        require(status != "unresolved", f"conflicts[{index}] is unresolved")
        require(not (status == "accepted" and severity in {"S1", "S2"}), f"conflicts[{index}] {severity} cannot be accepted without repair")
        resolution = clean_text(conflict.get("resolution"), f"conflicts[{index}].resolution")
        normalized_conflicts.append({**conflict, "severity": severity, "status": status, "resolution": resolution})

    action = as_mapping(root.get("tracking_action"), "tracking_action")
    allowed_action = {"kind", "state_revision_before", "state_revision_after", "reason"}
    require(not (set(action) - allowed_action), "tracking_action contains unsupported fields")
    kind = clean_text(action.get("kind"), "tracking_action.kind")
    require(kind in {"commit", "migrate-v4", "not_required"}, "tracking_action.kind must close the tracking decision")
    if plan["semantic_change"]:
        require(kind in {"commit", "migrate-v4"}, "semantic revisions must update the structured tracking authority")
        before = action.get("state_revision_before")
        after = action.get("state_revision_after")
        require(isinstance(before, int) and not isinstance(before, bool), "tracking_action.state_revision_before must be an integer")
        require(isinstance(after, int) and not isinstance(after, bool) and after > before, "tracking_action.state_revision_after must be greater than before")
    else:
        require(clean_text(action.get("reason"), "tracking_action.reason"), "non-semantic revisions must explain why tracking is unchanged")
    return {
        "change_id": plan["change_id"],
        "required_check_count": len(expected),
        "checked_file_count": len(checked),
        "conflict_count": len(normalized_conflicts),
        "tracking_action": kind,
        "status": "PASS",
    }


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"unable to read JSON {path}: {exc}") from exc


def approval_stamp(manifest_path: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Bind a successful review to the exact manifest bytes.

    Hooks treat this stamp as the closure boundary. Any later edit to active.json
    changes the digest and reopens the transaction instead of silently reusing an
    old approval.
    """
    try:
        payload = manifest_path.read_bytes()
    except OSError as exc:
        raise GuardError(f"unable to hash revision manifest {manifest_path}: {exc}") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "change_id": result["change_id"],
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "approved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "tracking_action": result["tracking_action"],
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_chapter_spec(raw: str) -> list[int]:
    chapters: set[int] = set()
    for token in re.split(r"[,，\s]+", raw.strip()):
        if not token:
            continue
        if "-" in token:
            parts = token.split("-", 1)
            require(
                len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit(),
                f"invalid chapter range: {token}",
            )
            start, end = int(parts[0]), int(parts[1])
            require(1 <= start <= end, f"invalid chapter range: {token}")
            chapters.update(range(start, end + 1))
        else:
            require(token.isdigit() and int(token) >= 1, f"invalid chapter number: {token}")
            chapters.add(int(token))
    require(bool(chapters), "chapters must not be empty")
    return sorted(chapters)


def _is_manifest_approved(manifest_path: Path) -> bool:
    stamp_path = manifest_path.parent / "active.approved.json"
    if not manifest_path.is_file() or not stamp_path.is_file():
        return False
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(manifest, dict)
        and isinstance(stamp, dict)
        and stamp.get("schema_version") == SCHEMA_VERSION
        and stamp.get("status") == "PASS"
        and stamp.get("change_id") == manifest.get("change_id")
        and stamp.get("manifest_sha256") == hashlib.sha256(manifest_bytes).hexdigest()
    )


def _active_prose_for_chapter(project: Path, chapter: int) -> Path:
    body_dir = project / "正文"
    require(body_dir.is_dir(), f"正文 directory does not exist in {project}")
    matches: list[Path] = []
    for path in sorted(body_dir.glob("第*章*.md")):
        if "_原稿_" in path.name:
            continue
        match = CHAPTER_RE.match(path.name)
        if match and int(match.group(1)) == chapter:
            matches.append(path)
    require(bool(matches), f"chapter {chapter} active prose file not found in 正文/")
    require(len(matches) == 1, f"chapter {chapter} has multiple active prose files in 正文/")
    return matches[0]


def _outline_for_chapter(project: Path, chapter: int) -> Path:
    outline_dir = project / "大纲"
    require(outline_dir.is_dir(), f"大纲 directory does not exist in {project}")
    matches: list[Path] = []
    for path in sorted(outline_dir.glob("细纲_第*章*.md")):
        match = re.match(r"^细纲_第0*(\d+)章", path.name)
        if match and int(match.group(1)) == chapter:
            matches.append(path)
    require(bool(matches), f"chapter {chapter} outline (细纲_第{chapter}章*.md) not found in 大纲/")
    return matches[0]


def start_polish(
    project: Path,
    chapters_raw: str,
    *,
    summary: str | None = None,
    change_id: str | None = None,
) -> dict[str, Any]:
    import chapter_candidate as cc

    project = cc.resolve_book_dir(project).resolve()
    require(project.is_dir(), "project root does not exist")
    manifest_path = project / "追踪" / "修改影响" / "active.json"
    if manifest_path.is_file() and not _is_manifest_approved(manifest_path):
        raise GuardError(f"an unclosed revision transaction already exists: {manifest_path}")
    open_runs = cc.open_workspaces(project)
    require(not open_runs, f"cannot start polish while chapter candidate is open: {open_runs[0] if open_runs else ''}")

    chapters = parse_chapter_spec(chapters_raw)
    last, _ = cc.tracking_snapshot(project)
    for chap in chapters:
        require(chap <= last, f"chapter {chap} exceeds last_committed_chapter={last}")

    changed_files: list[str] = []
    baselines: dict[str, str] = {}
    for chap in chapters:
        prose_path = _active_prose_for_chapter(project, chap)
        _outline_for_chapter(project, chap)
        rel = prose_path.relative_to(project).as_posix()
        changed_files.append(rel)
        baselines[rel] = _sha256_file(prose_path)

    cid = change_id or f"P{datetime.now().strftime('%Y%m%d-%H%M%S')}-ch{'-'.join(str(c) for c in chapters[:4])}"[:80]
    label = "、".join(f"第{c}章" for c in chapters)
    plan_doc = {
        "schema_version": SCHEMA_VERSION,
        "mode": "prose_only",
        "change_id": cid,
        "summary": (summary or f"{label}纯文风润色（不改剧情事实、伏笔与人物状态，无改动字数比例上限）").strip(),
        "semantic_change": False,
        "changed_files": changed_files,
        "affected_entities": [],
        "affected_fact_ids": [],
        "affected_chapters": chapters,
        "polish_baselines": baselines,
    }
    manifest = make_manifest(project, plan_doc)
    atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    stamp_path = manifest_path.parent / "active.approved.json"
    stamp_path.unlink(missing_ok=True)
    return {
        "status": "OPEN",
        "mode": "prose_only",
        "change_id": cid,
        "chapters": chapters,
        "changed_files": manifest["changed_files"],
        "manifest": str(manifest_path),
    }


def finish_polish(project: Path, *, reason: str | None = None) -> dict[str, Any]:
    import chapter_candidate as cc

    project = cc.resolve_book_dir(project).resolve()
    require(project.is_dir(), "project root does not exist")
    manifest_path = project / "追踪" / "修改影响" / "active.json"
    require(manifest_path.is_file(), f"no active revision manifest found: {manifest_path}")
    require(not _is_manifest_approved(manifest_path), f"polish transaction is already closed: {manifest_path}")
    manifest_raw = read_json(manifest_path)
    manifest = as_mapping(manifest_raw, "revision manifest")
    require(manifest.get("mode") == "prose_only", "active.json is not a prose_only polish transaction; use check --stamp")

    changed_files = [normalize_relative(project, item, f"changed_files[{idx}]") for idx, item in enumerate(as_list(manifest.get("changed_files"), "changed_files"))]
    baselines = manifest.get("polish_baselines") if isinstance(manifest.get("polish_baselines"), dict) else {}
    chapter_items: list[tuple[int, str, Path, Path]] = []
    for rel in changed_files:
        match = PROSE_FILE_RE.fullmatch(rel)
        require(match is not None, f"invalid prose_only target: {rel}")
        chap = int(match.group(1))
        prose_path = project / rel
        outline_path = _outline_for_chapter(project, chap)
        chapter_items.append((chap, rel, prose_path, outline_path))
    chapter_items.sort(key=lambda item: item[0])
    polished_set = {item[0] for item in chapter_items}

    # 1) Run full 15 deterministic gates on every polished chapter (zero edit-ratio ceiling)
    reports: dict[int, dict[str, Any]] = {}
    summaries: dict[int, dict[str, Any]] = {}
    for chap, rel, prose_path, outline_path in chapter_items:
        receipt_file = cc.receipt_path(project, chap)
        approval_mode = "review"
        waivers: list[dict[str, Any]] = []
        if receipt_file.is_file():
            receipt_obj = read_json(receipt_file)
            if isinstance(receipt_obj, dict) and receipt_obj.get("approval_mode") in {"review", "auto"}:
                approval_mode = receipt_obj["approval_mode"]
        waivers_file = cc.quality_dir(project, chap) / "waivers.json"
        if waivers_file.is_file():
            waivers_obj = read_json(waivers_file)
            if isinstance(waivers_obj, dict) and isinstance(waivers_obj.get("waivers"), list):
                current_sha = _sha256_file(prose_path)
                waivers = [
                    w for w in waivers_obj["waivers"]
                    if isinstance(w, dict) and w.get("candidate_sha256") == current_sha
                ]
        pseudo_data = {
            "chapter": chap,
            "candidate": rel,
            "approval_mode": approval_mode,
            "pressure_override": None,
            "waivers": waivers,
        }
        try:
            report = cc.run_gates(
                project,
                None,
                pseudo_data,
                prose_path,
                outline_path,
                polish_chapters=polished_set,
            )
        except cc.CandidateError as exc:
            raise GuardError(f"第{chap}章门禁运行异常: {exc}") from exc
        if report["status"] != "pass":
            raise GuardError(f"第{chap}章未通过确定性门禁，纯润色事务保持打开：\n{cc.failure_summary(report)}")
        reports[chap] = report
        summaries[chap] = cc.build_gate_summary(report)

    # 2) Verify tracking views are intact before syncing receipts
    tracking_tool = Path(__file__).resolve().parent / "tracking_commit.py"
    tracking_completed = subprocess.run(
        [sys.executable, str(tracking_tool), "check", "--project", str(project)],
        text=True,
        capture_output=True,
        check=False,
    )
    require(
        tracking_completed.returncode == 0,
        f"tracking_commit.py check failed:\n{tracking_completed.stdout}{tracking_completed.stderr}".rstrip(),
    )
    _, state_revision = cc.tracking_snapshot(project)
    polish_reason = (
        (reason or "").strip()
        or str(manifest.get("summary") or "").strip()
        or "纯文风润色（不改剧情事实、伏笔与人物状态）"
    )

    # 3) Update chapter receipts, gate-report/gate-summary, and projection log
    for chap, rel, prose_path, _ in chapter_items:
        new_sha = reports[chap]["candidate_sha256"]
        old_sha = baselines.get(rel)
        receipt_file = cc.receipt_path(project, chap)
        if not receipt_file.is_file():
            continue
        receipt_data = as_mapping(read_json(receipt_file), f"receipt ch{chap}")
        if receipt_data.get("status") != "committed":
            continue
        if old_sha is None:
            old_sha = receipt_data.get("accepted_prose_sha256")

        qdir = cc.quality_dir(project, chap)
        if receipt_data.get("protocol") == cc.PROTOCOL or qdir.is_dir():
            qdir.mkdir(parents=True, exist_ok=True)
            report_dest = qdir / "gate-report.json"
            summary_dest = qdir / "gate-summary.json"
            cc.atomic_write_json(report_dest, reports[chap])
            cc.atomic_write_json(summary_dest, summaries[chap])
            report_rel = report_dest.relative_to(project).as_posix()
            summary_rel = summary_dest.relative_to(project).as_posix()
            report_sha = _sha256_file(report_dest)
            summary_sha = _sha256_file(summary_dest)
            q_entries = [
                dict(item) for item in (receipt_data.get("quality_receipts") or [])
                if isinstance(item, dict) and item.get("path") not in {report_rel, summary_rel}
            ]
            q_entries.insert(0, {"path": summary_rel, "sha256": summary_sha})
            q_entries.insert(0, {"path": report_rel, "sha256": report_sha})
            receipt_data["quality_receipts"] = q_entries
            receipt_data["gated_prose_sha256"] = new_sha

        history = receipt_data.get("sync_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "synced_at": cc.utc_now(),
                "mode": "prose_only",
                "reason": polish_reason,
                "old_sha256": old_sha,
                "new_sha256": new_sha,
                "state_revision": state_revision,
                "revision_manifest": manifest_path.relative_to(project).as_posix(),
            }
        )
        receipt_data["accepted_prose_sha256"] = new_sha
        receipt_data["state_revision_after"] = state_revision
        receipt_data["sync_history"] = history
        cc.atomic_write_json(receipt_file, receipt_data)
        cc.append_projection_event(
            project,
            event="polish_sync",
            chapter=chap,
            state_revision=state_revision,
            prose_sha256=new_sha,
            receipt=receipt_file,
        )

    if (project / "追踪" / "章节提交").is_dir() and any((project / "追踪" / "章节提交").glob("第*章.json")):
        cc.write_progress(project)

    # 4) Refresh accepted & golden voice profiles if configured
    voice_tool = Path(__file__).resolve().parent / "voice_profile.py"
    csv_chapters = ",".join(str(c) for c in sorted(polished_set))
    voice_completed = subprocess.run(
        [sys.executable, str(voice_tool), "update", "--project", str(project), "--allow-legacy-chapters", csv_chapters],
        text=True,
        capture_output=True,
        check=False,
    )
    require(
        voice_completed.returncode == 0,
        f"voice_profile.py update failed:\n{voice_completed.stdout}{voice_completed.stderr}".rstrip(),
    )
    golden_path = project / "追踪" / "文风" / "golden-voice-profile.json"
    if golden_path.is_file():
        golden_obj = read_json(golden_path)
        golden_chaps = golden_obj.get("chapters") if isinstance(golden_obj, dict) else None
        if isinstance(golden_chaps, list) and (set(golden_chaps) & polished_set):
            golden_csv = ",".join(str(c) for c in golden_chaps)
            golden_completed = subprocess.run(
                [
                    sys.executable,
                    str(voice_tool),
                    "golden-build",
                    "--project",
                    str(project),
                    "--chapters",
                    golden_csv,
                    "--confirm",
                    "GOLDEN_APPROVED",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            require(
                golden_completed.returncode == 0,
                f"voice_profile.py golden-build failed:\n{golden_completed.stdout}{golden_completed.stderr}".rstrip(),
            )

    # 5) Touch 上下文.md mtime (without modifying bytes) so hook staleness heuristic sees fresh context
    context_path = project / "追踪" / "上下文.md"
    if context_path.is_file():
        newest_prose = max((p.stat().st_mtime for _, _, p, _ in chapter_items if p.is_file()), default=0.0)
        target_ts = max(time.time(), newest_prose)
        os.utime(context_path, (target_ts, target_ts))

    # 6) Close active.json and write active.approved.json
    closed_manifest = {
        **manifest,
        "checked_files": list(manifest["required_checks"]),
        "conflicts": manifest.get("conflicts") or [],
        "tracking_action": {
            "kind": "not_required",
            "state_revision_before": state_revision,
            "state_revision_after": state_revision,
            "reason": polish_reason,
        },
    }
    atomic_write(manifest_path, json.dumps(closed_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    checked = check_manifest(project, read_json(manifest_path))
    stamp_path = manifest_path.parent / "active.approved.json"
    stamp = approval_stamp(manifest_path, checked)
    atomic_write(stamp_path, json.dumps(stamp, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    # 7) Run project hooks (.story/hooks/*.sh)
    cc.run_project_hooks(project, "polish-finished", {"STORY_CHAPTERS": csv_chapters})

    return {
        **checked,
        "mode": "prose_only",
        "chapters": sorted(polished_set),
        "approval_stamp": str(stamp_path),
        "gate_summaries": {str(chap): summaries[chap] for chap in sorted(polished_set)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--project", type=Path, required=True)
    plan_parser.add_argument("--input", type=Path, required=True)
    plan_parser.add_argument("--output", type=Path, required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--project", type=Path, required=True)
    check_parser.add_argument("--input", type=Path, required=True)
    check_parser.add_argument(
        "--stamp",
        type=Path,
        help="write an approval stamp bound to the exact checked manifest bytes",
    )
    start_polish_parser = subparsers.add_parser("start-polish", help="开启不设字数改动比例上限的纯文风润色事务")
    start_polish_parser.add_argument("--project", type=Path, required=True)
    start_polish_parser.add_argument("--chapters", required=True, help="章号或范围，例如 3 或 1,3,5 或 2-4")
    start_polish_parser.add_argument("--summary", help="润色说明")
    start_polish_parser.add_argument("--change-id", help="可选自定义事务 ID")
    finish_polish_parser = subparsers.add_parser("finish-polish", help="跑全套确定性门禁并一键闭环纯文风润色事务")
    finish_polish_parser.add_argument("--project", type=Path, required=True)
    finish_polish_parser.add_argument("--reason", help="闭环记录原因")
    args = parser.parse_args()
    try:
        if args.command == "start-polish":
            result = start_polish(
                args.project.expanduser(),
                args.chapters,
                summary=args.summary,
                change_id=args.change_id,
            )
        elif args.command == "finish-polish":
            result = finish_polish(args.project.expanduser(), reason=args.reason)
        else:
            project = args.project.resolve()
            require(project.is_dir(), "project root does not exist")
            if args.command == "plan":
                result = make_manifest(project, read_json(args.input))
                atomic_write(args.output.resolve(), json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            else:
                manifest_path = args.input.resolve()
                result = check_manifest(project, read_json(manifest_path))
                if args.stamp:
                    stamp_path = args.stamp.resolve()
                    stamp = approval_stamp(manifest_path, result)
                    atomic_write(stamp_path, json.dumps(stamp, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
                    result = {**result, "approval_stamp": str(stamp_path)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (GuardError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
