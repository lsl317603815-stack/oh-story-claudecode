#!/usr/bin/env python3
"""Cross-check a chapter's tracking transaction against an independent extraction.

The tracking transaction is written by the same model that wrote the chapter, so
what it forgets to record is exactly what it forgot while writing.  This tool
compares that transaction with a separate chapter-extractor pass (strict JSON
mode) over the accepted prose, plus plain name lookups in the prose itself, and
reports what looks unrecorded.  Every finding is advisory: the author or the main
session decides whether it belongs in the transaction.  Run it before
``tracking_commit.py commit``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


STATE_CHANGE_TYPES = {"状态变化", "转折点"}
SETUP_TYPES = {"铺垫"}
REVEAL_TYPES = {"信息揭示"}
NOTABLE_IMPORTANCE = {"major", "supporting"}
MIN_MINOR_PLOT_POINTS = 2
MAX_LISTED = 5


class AuditError(ValueError):
    pass


def emit(payload: object) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def load_json_document(path: Path, label: str) -> dict[str, Any]:
    """Accept a bare JSON object or an agent reply that ends with a json code block."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuditError(f"无法读取{label}: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
        for block in reversed(re.findall(r"```(?:json)?\s*\n(.*?)\n```", raw, flags=re.S)):
            try:
                value = json.loads(block)
                break
            except json.JSONDecodeError:
                continue
        if value is None:
            raise AuditError(f"{label}不是 JSON，也没有可解析的 json 代码块")
    if not isinstance(value, dict):
        raise AuditError(f"{label}必须是 JSON object")
    return value


def as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


def find_prose(project: Path, chapter: int) -> Path | None:
    pattern = re.compile(rf"^第0*{chapter}章(?:_|\.|$)")
    matches = sorted(path for path in (project / "正文").glob("第*章*.md") if pattern.match(path.name))
    return matches[0] if len(matches) == 1 else None


def load_core_characters(project: Path) -> set[str]:
    path = project / "追踪" / "_tracking-state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    characters = state.get("characters") if isinstance(state, dict) else None
    return set(characters) if isinstance(characters, dict) else set()


def extraction_people(extraction: dict[str, Any]) -> list[dict[str, Any]]:
    appearances: dict[str, int] = {}
    for point in as_list(extraction.get("plot_points")):
        for name in as_list(point.get("characters")) if isinstance(point, dict) else []:
            if isinstance(name, str) and name.strip():
                appearances[name.strip()] = appearances.get(name.strip(), 0) + 1
    people = []
    for row in as_list(extraction.get("characters")):
        if not isinstance(row, dict) or not text(row.get("name")).strip():
            continue
        name = row["name"].strip()
        aliases = [alias.strip() for alias in as_list(row.get("aliases")) if isinstance(alias, str) and alias.strip()]
        people.append(
            {
                "name": name,
                "aliases": aliases,
                "importance": text(row.get("importance")),
                "plot_points": appearances.get(name, 0) + sum(appearances.get(alias, 0) for alias in aliases),
            }
        )
    return people


def same_person(names: set[str], person: dict[str, Any]) -> bool:
    for candidate in [person["name"], *person["aliases"]]:
        for name in names:
            # 登记名和抽取名常有「林川 / 林川哥」这类包含关系；两字以下的名字只认完全相等，避免「林」撞一片
            if candidate == name or (min(len(candidate), len(name)) >= 2 and (candidate in name or name in candidate)):
                return True
    return False


def audit(project: Path, transaction: dict[str, Any], extraction: dict[str, Any]) -> dict[str, Any]:
    chapter = transaction.get("chapter")
    if not isinstance(chapter, int) or chapter <= 0:
        raise AuditError("追踪事务缺少有效 chapter")
    if extraction.get("error"):
        raise AuditError(f"chapter-extractor 返回错误: {extraction['error']}")
    extracted_chapter = extraction.get("chapter_number")
    if extracted_chapter != chapter:
        raise AuditError(f"抽取结果是第 {extracted_chapter} 章，事务是第 {chapter} 章")
    delta = transaction.get("delta") if isinstance(transaction.get("delta"), dict) else {}
    snapshots = transaction.get("character_snapshots") if isinstance(transaction.get("character_snapshots"), dict) else {}
    appeared = {name for name in as_list(delta.get("appeared_characters")) if isinstance(name, str)}
    changed = {
        text(item.get("name")) for item in as_list(delta.get("character_changes")) if isinstance(item, dict)
    } - {""}
    recorded = appeared | changed | set(snapshots)
    core = load_core_characters(project) | set(snapshots)
    prose_path = find_prose(project, chapter)
    prose = prose_path.read_text(encoding="utf-8") if prose_path else ""
    people = extraction_people(extraction)
    findings: list[dict[str, str]] = []

    def add(code: str, message: str) -> None:
        findings.append({"code": code, "severity": "advisory", "message": message})

    missing = [
        person["name"]
        for person in people
        if (person["importance"] in NOTABLE_IMPORTANCE or person["plot_points"] >= MIN_MINOR_PLOT_POINTS)
        and not same_person(recorded, person)
    ]
    if missing:
        add(
            "appearance-unrecorded",
            f"抽取到 {len(missing)} 个出场角色不在 appeared_characters 里：{'、'.join(missing[:MAX_LISTED])}"
            f"{'…' if len(missing) > MAX_LISTED else ''}；确认后补进出场名单（核心角色的「最近出场」靠它）",
        )
    if prose:
        phantom = sorted(name for name in appeared if name not in prose)
        if phantom:
            add(
                "appearance-not-in-prose",
                f"登记出场但正文里找不到这个名字：{'、'.join(phantom[:MAX_LISTED])}；核对是不是别名、代称，或登记错了章",
            )
        silent = sorted(name for name in changed & core if name not in prose)
        if silent:
            add(
                "change-without-appearance",
                f"核心角色有状态变化登记，但正文里没出现名字：{'、'.join(silent[:MAX_LISTED])}；确认变化是否真在本章发生",
            )
    else:
        add("prose-not-found", f"没找到唯一的 正文/第{chapter}章*.md，跳过名字核对")

    points = [point for point in as_list(extraction.get("plot_points")) if isinstance(point, dict)]
    core_people = [person for person in people if person["name"] in core or any(alias in core for alias in person["aliases"])]
    unrecorded_changes = []
    for person in core_people:
        names = {person["name"], *person["aliases"]}
        if same_person(changed, person):
            continue
        if any(point.get("type") in STATE_CHANGE_TYPES and names & set(as_list(point.get("characters"))) for point in points):
            unrecorded_changes.append(person["name"])
    if unrecorded_changes:
        add(
            "state-change-unrecorded",
            f"抽取到核心角色的转折/状态变化情节点，但事务没有他们的 character_changes：{'、'.join(unrecorded_changes[:MAX_LISTED])}",
        )

    setups = [text(point.get("event")) for point in points if point.get("type") in SETUP_TYPES]
    hook_note = text((extraction.get("chapter_formula") or {}).get("hook_and_foreshadowing")) if isinstance(
        extraction.get("chapter_formula"), dict
    ) else ""
    if setups and not as_list(delta.get("foreshadow_changes")):
        listed = "；".join(item[:40] for item in setups[:3])
        add(
            "foreshadow-unrecorded",
            f"抽取到 {len(setups)} 个铺垫情节点（{listed}），事务没有任何 foreshadow_changes；"
            f"会跨章回收的要登记伏笔 ID{f'。抽取的章尾卡点：{hook_note[:60]}' if hook_note else ''}",
        )
    reveals = [text(point.get("event")) for point in points if point.get("type") in REVEAL_TYPES]
    if reveals and not as_list(delta.get("timeline_events")) and not as_list(delta.get("fact_changes")):
        add(
            "reveal-unrecorded",
            f"抽取到 {len(reveals)} 个信息揭示情节点（{'；'.join(item[:40] for item in reveals[:3])}），"
            "事务没有 timeline_events 或 fact_changes；读者已知范围或长期事实可能漏更新",
        )
    return {
        "chapter": chapter,
        "prose": prose_path.relative_to(project).as_posix() if prose_path else None,
        "status": "advisory" if findings else "clean",
        "findings": findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=Path, required=True, help="book project root")
    parser.add_argument("--transaction", type=Path, required=True, help="the chapter's tracking transaction JSON")
    parser.add_argument(
        "--extraction", type=Path, required=True, help="chapter-extractor reply in OUTPUT_MODE: json for the same chapter"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        project = args.project.expanduser().resolve()
        if not project.is_dir():
            raise AuditError(f"项目目录不存在: {project}")
        result = audit(
            project,
            load_json_document(args.transaction, "追踪事务"),
            load_json_document(args.extraction, "抽取结果"),
        )
    except AuditError as exc:
        sys.stderr.flush()
        sys.stderr.buffer.write(f"error: {exc}\n".encode("utf-8"))
        sys.stderr.buffer.flush()
        return 2
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
