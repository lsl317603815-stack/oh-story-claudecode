#!/usr/bin/env python3
"""Stage fiction deslop edits, protect literals, and apply checked candidates.

`scan` reports corpus-validated surface-rule alarms with candidate positions and never edits text
(rule table: references/pattern-contracts.json; thresholds: nearest book `.deslop-thresholds.json`,
falling back to references/deslop-thresholds.default.json). `check` attaches the same alarms to the
candidate as advisory only. Edits reach the source only through `apply --confirm APPLY`.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUN_VERSION = 1
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?(?:[%％年月日时分秒章卷万亿元岁号层级]*)")
TITLE_RE = re.compile(r"《([^》\n]{1,80})》")
CODE_RE = re.compile(r"`([^`\n]{1,120})`")


RULE_TABLE_DIR = Path(__file__).resolve().parent.parent / "references"
RULE_CONTRACTS = "pattern-contracts.json"
RULE_THRESHOLDS_DEFAULT = "deslop-thresholds.default.json"
BOOK_THRESHOLDS = ".deslop-thresholds.json"
THRESHOLD_SCHEMA = "deslop-thresholds/v1"
SCAN_MAX_CANDIDATES = 20
DENOMINATOR_SCALE = {"per_1000_han": ("han", 1000), "per_100_paragraphs": ("paragraphs", 100),
                     "per_100_sentences": ("sentences", 100), "per_100_chapters": (None, 100)}


class GuardError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GuardError(f"文件不存在: {path}") from exc
    except UnicodeDecodeError as exc:
        raise GuardError(f"文件不是 UTF-8: {path}") from exc


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def within(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise GuardError(f"正文文件必须位于项目根目录内: {path}") from exc


def unique_matches(pattern: re.Pattern[str], text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        value = match.group(1) if match.lastindex else match.group(0)
        if value not in seen:
            values.append(value)
            seen.add(value)
    return values


def auto_ledger(text: str) -> dict[str, Any]:
    return {
        "version": 1,
        "auto": {
            "numbers": unique_matches(NUMBER_RE, text),
            "titled_terms": unique_matches(TITLE_RE, text),
            "inline_code": unique_matches(CODE_RE, text),
        },
        "manual": {
            "protected_literals": [], "entities": [], "timeline_facts": [],
            "knowledge_boundaries": [], "clues": [], "voice_markers": [],
            "intentional_roughness": [],
        },
        "allowed_changes": [],
    }


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise GuardError(f"JSON 无效: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GuardError(f"JSON 根节点必须是对象: {path}")
    return value


def load_run(raw: str) -> tuple[Path, dict[str, Any]]:
    run_dir = Path(raw).expanduser().resolve()
    manifest = load_json(run_dir / "manifest.json")
    if manifest.get("run_version") != RUN_VERSION:
        raise GuardError("不支持的去味运行版本")
    return run_dir, manifest


def snapshot_paths(run_dir: Path, manifest: dict[str, Any]) -> tuple[Path, Path]:
    return run_dir / manifest["snapshot_file"], run_dir / manifest["candidate_file"]


def changed_spans(source: str, candidate: str) -> list[dict[str, Any]]:
    before, after = source.splitlines(), candidate.splitlines()
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    spans = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            spans.append({
                "kind": tag, "source_start": i1 + 1, "source_end": i2,
                "candidate_start": j1 + 1, "candidate_end": j2,
                "source_preview": before[i1:i2][:3], "candidate_preview": after[j1:j2][:3],
            })
    return spans


def allowed_map(ledger: dict[str, Any]) -> dict[str, str]:
    result = {}
    for item in ledger.get("allowed_changes", []):
        if isinstance(item, dict) and isinstance(item.get("from"), str) and isinstance(item.get("to"), str):
            result[item["from"]] = item["to"]
    return result


def hard_literals(ledger: dict[str, Any]) -> list[str]:
    values: list[str] = []
    auto, manual = ledger.get("auto", {}), ledger.get("manual", {})
    for key in ("numbers", "titled_terms", "inline_code"):
        if isinstance(auto.get(key), list):
            values.extend(str(item) for item in auto[key] if str(item))
    for key in ("protected_literals", "entities"):
        if isinstance(manual.get(key), list):
            values.extend(str(item) for item in manual[key] if str(item))
    return list(dict.fromkeys(values))


def load_rule_table() -> dict[str, Any]:
    contracts = load_json(RULE_TABLE_DIR / RULE_CONTRACTS)
    table = contracts.get("rule_table") or {}
    rules = contracts.get("surface_rules") or []
    if not table.get("version") or not rules:
        raise GuardError(f"规则表缺少 rule_table.version 或 surface_rules: {RULE_TABLE_DIR / RULE_CONTRACTS}")
    return {"version": table["version"], "rules": rules}


def find_book_thresholds(path: Path) -> Path | None:
    """从正文所在目录逐级向上找最近的书目录阈值文件；不回退调用者 cwd（同 .deslop-whitelist）。"""
    for directory in [path.parent, *path.parent.parents]:
        candidate = directory / BOOK_THRESHOLDS
        if candidate.is_file():
            return candidate
    return None


def resolve_thresholds(source: Path, *, pool: str | None = None, explicit: str | None = None,
                       writer_family: str | None = None) -> dict[str, Any]:
    default_path = RULE_TABLE_DIR / RULE_THRESHOLDS_DEFAULT
    defaults = load_json(default_path)
    ctx: dict[str, Any] = {"source": "default", "path": str(default_path), "warnings": [], "disabled": [],
                           "min_han": defaults.get("min_han", 1000)}
    book: dict[str, Any] | None = None
    book_path = Path(explicit).expanduser().resolve() if explicit else find_book_thresholds(source)
    if book_path is not None:
        try:
            book = load_json(book_path)
            if book.get("schema") != THRESHOLD_SCHEMA:
                raise GuardError(f"schema 应为 {THRESHOLD_SCHEMA}，实际 {book.get('schema')!r}")
            ctx.update(source="explicit" if explicit else "book", path=str(book_path))
        except GuardError as exc:
            ctx["warnings"].append(f"书目录阈值文件无效，已回退仓内缺省表：{book_path}（{exc}）")
            book = None
    pools = defaults.get("pools", {})
    wanted = pool or (book or {}).get("pool") or defaults["default_pool"]
    if wanted not in pools:
        ctx["warnings"].append(f"题材池「{wanted}」不在缺省表里，已改用 {defaults['default_pool']}")
        wanted = defaults["default_pool"]
    ctx["pool"] = wanted
    if pools[wanted].get("warning"):
        ctx["warnings"].append(f"{wanted}：{pools[wanted]['warning']}")
    rules = {rid: dict(row) for rid, row in pools[wanted].get("rules", {}).items()}
    if book:
        for rid, override in (book.get("rules") or {}).items():
            rules[rid] = {**rules.get(rid, {}), **override, "overridden": True}
        ctx["disabled"] = list(book.get("disabled_rules") or [])
        if isinstance(book.get("min_han"), int):
            ctx["min_han"] = book["min_han"]
        ctx["baseline_source"] = (book.get("baseline") or {}).get("source")
    ctx["writer_family"] = writer_family or (book or {}).get("writer_family")
    ctx["rules"] = rules
    if ctx["source"] == "default":
        ctx["warnings"].append("未找到书目录 .deslop-thresholds.json，回退仓内缺省表")
    return ctx


def load_corpus_detectors() -> Any:
    """corpus_rules 只随 story-deslop 正本分发；其他 skill 的本脚本副本没有它时返回 None。"""
    root = Path(__file__).resolve().parent
    if not (root / "corpus_rules" / "detectors.py").is_file():
        return None
    sys.dont_write_bytecode = True
    for entry in (str(root), str(root / "corpus_rules")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import detectors  # noqa: PLC0415

    return detectors


def normalized_line_numbers(text: str, paragraphs: list[str]) -> list[int]:
    """把 voice_profile.normalized_lines 的段落映回原文行号（按序匹配去空白后的整行）。"""
    numbers: list[int] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cursor = 0
    for paragraph in paragraphs:
        while cursor < len(lines) and lines[cursor].strip() != paragraph:
            cursor += 1
        numbers.append(cursor + 1 if cursor < len(lines) else 0)
        cursor += 1
    return numbers


def surface_scan(source: Path, *, pool: str | None = None, thresholds: str | None = None,
                 writer_family: str | None = None, include_retired: bool = False) -> dict[str, Any]:
    """按规则表与阈值给出告警和候选位置；只读，不改文本。"""
    detectors = load_corpus_detectors()
    table = load_rule_table()
    ctx = resolve_thresholds(source, pool=pool, explicit=thresholds, writer_family=writer_family)
    meta = {k: ctx.get(k) for k in ("source", "path", "pool", "writer_family", "min_han", "baseline_source", "warnings", "disabled")}
    result: dict[str, Any] = {"file": str(source), "rule_table_version": table["version"], "thresholds": meta,
                              "alarms": [], "rules": []}
    if detectors is None:
        result["status"] = "unavailable"
        result["note"] = "本副本没有 corpus_rules 检测器；全量表层扫描只在 story-deslop 正本提供"
        return result
    text = read_text(source)
    paragraphs = detectors.VP.normalized_lines(text)
    line_numbers = normalized_line_numbers(text, paragraphs)
    units = detectors.unit_counts(paragraphs)
    units["chapters"] = 1
    result["units"] = units
    judged = units["han"] >= ctx["min_han"]
    if not judged:
        result["note"] = f"正文 {units['han']} 汉字 < {ctx['min_han']}，不做章级阈值判定，只列命中"
    for rule in table["rules"]:
        rid, status = rule["id"], rule["status"]
        detector = detectors.REGISTRY.get(rid)
        if detector is None:
            continue
        hits = detector(paragraphs)
        if rule.get("kind") == "metric":
            value = hits[0]["value"] if hits else None
            count = None
        else:
            field, scale = DENOMINATOR_SCALE[rule["denominator"]]
            denominator = units[field] if field else 1
            count = len(hits)
            value = count * scale / denominator if denominator else None
        bounds = ctx["rules"].get(rid, {})
        alarm: str | None = None
        bound: float | None = None
        active = judged and value is not None and rid not in ctx["disabled"]
        if status in ("conditional", "candidate") and active and bounds.get("chapter_p90") is not None:
            bound = bounds["chapter_p90"]
            alarm = "rule-threshold" if value > bound else None
        elif rule.get("action") == "family_hint" and active and bounds.get("chapter_p90") is not None:
            families = (rule.get("family_hint") or {}).get("families", [])
            if ctx["writer_family"] in families:
                bound = bounds["chapter_p90"]
                alarm = "family-hint" if value > bound else None
        if alarm is None and rule.get("deficit") and active and bounds.get("chapter_p10") is not None:
            if value < bounds["chapter_p10"]:
                bound, alarm = bounds["chapter_p10"], "deficit-floor"
        row = {"id": rid, "name": rule["name"], "status": status, "value": None if value is None else round(value, 4),
               "count": count, "bound": bound, "alarm": alarm}
        if status == "retired" and alarm is None and not include_retired:
            continue
        if status == "pending" and hits and alarm is None:
            # 待定：量不足、去留未定，维持现状但只列命中位置，不做阈值告警（决定 13）。
            row["candidates"] = [{"line": line_numbers[h["para"]], "text": h["text"], "sentence": h["sentence"][:60]}
                                 for h in hits[:5] if "para" in h]
            result.setdefault("pending_hits", []).append(row)
        result["rules"].append(row)
        if alarm:
            candidates = []
            if alarm != "deficit-floor":
                for hit in hits[:SCAN_MAX_CANDIDATES]:
                    if "para" in hit:
                        candidates.append({"line": line_numbers[hit["para"]], "text": hit["text"],
                                           "sentence": hit["sentence"][:60], "in_dialogue": hit["in_dialogue"]})
            direction = "低于真人章级 P10" if alarm == "deficit-floor" else "高于真人章级 P90"
            result["alarms"].append({**row, "severity": "advisory", "candidates": candidates,
                                     "message": f"{rule['name']} {value:.2f}，{direction}（{bound:.2f}，{ctx['pool']}）；只告警，作者决定是否改。"})
    result["status"] = "alarms" if result["alarms"] else "clean"
    return result


def build_report(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    snapshot, candidate_path = snapshot_paths(run_dir, manifest)
    source, candidate = read_text(snapshot), read_text(candidate_path)
    ledger = load_json(run_dir / "protection-ledger.json")
    source_live = Path(manifest["project_root"]) / manifest["source_path"]
    blocking, advisory = [], []

    if not source_live.is_file() or sha256(source_live) != manifest["source_sha256"]:
        blocking.append({"type": "stale-source", "message": "源正文在初始化后发生变化，拒绝覆盖。"})

    replacements = allowed_map(ledger)
    for literal in hard_literals(ledger):
        expected, actual = source.count(literal), candidate.count(literal)
        replacement = replacements.get(literal)
        if expected > actual and not (replacement and candidate.count(replacement) >= expected - actual):
            blocking.append({"type": "protected-literal-missing", "message": f"保护项减少: {literal!r}，源文 {expected} 次，候选稿 {actual} 次。"})

    retention = len(candidate) / max(len(source), 1)
    line_delta = abs(len(candidate.splitlines()) - max(len(source.splitlines()), 1)) / max(len(source.splitlines()), 1)
    if manifest["edit_scope"] == "in-place" and retention < 0.85:
        advisory.append({"type": "scope-retention", "message": f"in-place 字数保留率仅 {retention:.1%}。"})
    if manifest["edit_scope"] == "in-place" and line_delta > 0.10:
        advisory.append({"type": "scope-line-delta", "message": f"in-place 行数变化 {line_delta:.1%}。"})

    spans = changed_spans(source, candidate)
    atomic_json(run_dir / "changed-spans.json", {"version": 1, "spans": spans})
    # 表层规则只告警：候选稿的阈值告警并入 advisory，永不进入 blocking（规则表 v1，决定 9）。
    surface: dict[str, Any]
    try:
        scan = surface_scan(candidate_path)
        surface = {"status": scan["status"], "rule_table_version": scan["rule_table_version"],
                   "thresholds": {k: scan["thresholds"][k] for k in ("source", "pool")}, "alarms": len(scan["alarms"])}
        for alarm in scan["alarms"]:
            advisory.append({"type": alarm["alarm"], "rule": alarm["id"], "message": alarm["message"],
                             "candidates": alarm["candidates"][:5]})
    except GuardError as exc:
        surface = {"status": "unavailable", "note": str(exc)}
    return {
        "status": "blocked" if blocking else "pass", "run_id": manifest["run_id"],
        "edit_scope": manifest["edit_scope"], "rewrite_intensity": manifest["rewrite_intensity"],
        "retention_ratio": round(retention, 6), "changed_span_count": len(spans),
        "blocking": blocking, "advisory": advisory, "surface_scan": surface,
    }


def cmd_init(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    project_root = Path(args.project_root).expanduser().resolve()
    if not source.is_file():
        raise GuardError(f"正文文件不存在: {source}")
    if not project_root.is_dir():
        raise GuardError(f"项目根目录不存在: {project_root}")
    source_relative = within(project_root, source)
    run_id = args.run_id or datetime.now().strftime("D%Y%m%d-%H%M%S")
    if not RUN_ID_RE.fullmatch(run_id):
        raise GuardError("run id 只能包含字母、数字、点、下划线和连字符")
    run_dir = project_root / ".story-deslop" / "runs" / run_id
    if run_dir.exists():
        raise GuardError(f"运行目录已存在: {run_dir}")
    run_dir.mkdir(parents=True)
    suffix = source.suffix or ".txt"
    snapshot_name, candidate_name = f"source{suffix}", f"candidate{suffix}"
    shutil.copyfile(source, run_dir / snapshot_name)
    shutil.copyfile(source, run_dir / candidate_name)
    manifest = {
        "run_version": RUN_VERSION, "run_id": run_id, "status": "draft", "created_at": now_iso(),
        "project_root": str(project_root), "source_path": source_relative, "source_sha256": sha256(source),
        "snapshot_file": snapshot_name, "candidate_file": candidate_name,
        "edit_scope": args.scope, "rewrite_intensity": args.intensity, "issue_density": args.issue_density,
    }
    atomic_json(run_dir / "manifest.json", manifest)
    atomic_json(run_dir / "protection-ledger.json", auto_ledger(read_text(source)))
    print(run_dir)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    run_dir, manifest = load_run(args.run_dir)
    snapshot, candidate_path = snapshot_paths(run_dir, manifest)
    source, candidate = read_text(snapshot), read_text(candidate_path)
    payload = {"version": 1, "spans": changed_spans(source, candidate)}
    atomic_json(run_dir / "changed-spans.json", payload)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("".join(difflib.unified_diff(source.splitlines(True), candidate.splitlines(True), fromfile="source", tofile="candidate")), end="")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    run_dir, manifest = load_run(args.run_dir)
    report = build_report(run_dir, manifest)
    atomic_json(run_dir / "check-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 2


def cmd_apply(args: argparse.Namespace) -> int:
    if args.confirm != "APPLY":
        raise GuardError("应用候选稿必须显式传入 --confirm APPLY")
    run_dir, manifest = load_run(args.run_dir)
    report = build_report(run_dir, manifest)
    atomic_json(run_dir / "check-report.json", report)
    if report["status"] != "pass":
        raise GuardError("保护检查未通过，拒绝应用候选稿")
    _, candidate_path = snapshot_paths(run_dir, manifest)
    source_live = Path(manifest["project_root"]) / manifest["source_path"]
    atomic_text(source_live, read_text(candidate_path))
    manifest.update({"status": "applied", "applied_at": now_iso(), "applied_sha256": sha256(source_live)})
    atomic_json(run_dir / "manifest.json", manifest)
    print(source_live)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    reports = [surface_scan(Path(raw).expanduser().resolve(), pool=args.pool, thresholds=args.thresholds,
                            writer_family=args.writer_family, include_retired=args.include_retired)
               for raw in args.files]
    if args.format == "json":
        print(json.dumps({"reports": reports}, ensure_ascii=False, indent=2))
        return 0
    for report in reports:
        th = report["thresholds"]
        print(f"# {report['file']}：规则表 {report['rule_table_version']}；阈值 {th['source']}·{th['pool']}；"
              + "；".join(th["warnings"] + ([report["note"]] if report.get("note") else [])))
        for alarm in report["alarms"]:
            print(f"[advisory] {alarm['alarm']} {alarm['id']}: {alarm['message']}")
            for c in alarm["candidates"]:
                print(f"  候选 第{c['line']}行: {c['text']}｜{c['sentence']}")
        for row in report.get("pending_hits", []):
            where = "、".join(f"第{c['line']}行「{c['text']}」" for c in row["candidates"])
            print(f"[candidate] pending {row['id']} {row['name']} {row['count']} 处（待定，只列不告警）: {where}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init")
    init_parser.add_argument("source")
    init_parser.add_argument("--project-root", required=True)
    init_parser.add_argument("--scope", choices=("in-place", "bounded", "structural"), default="bounded")
    init_parser.add_argument("--intensity", choices=("minimal", "standard", "aggressive"), default="standard")
    init_parser.add_argument("--issue-density", choices=("light", "concentrated", "structural"), default="light")
    init_parser.add_argument("--run-id")
    init_parser.set_defaults(func=cmd_init)
    diff_parser = sub.add_parser("diff")
    diff_parser.add_argument("run_dir")
    diff_parser.add_argument("--format", choices=("unified", "json"), default="unified")
    diff_parser.set_defaults(func=cmd_diff)
    check_parser = sub.add_parser("check")
    check_parser.add_argument("run_dir")
    check_parser.set_defaults(func=cmd_check)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("run_dir")
    apply_parser.add_argument("--confirm", required=True)
    apply_parser.set_defaults(func=cmd_apply)
    scan_parser = sub.add_parser("scan", help="只读：按规则表与阈值报告告警和候选位置，不改文本")
    scan_parser.add_argument("files", nargs="+")
    scan_parser.add_argument("--pool")
    scan_parser.add_argument("--thresholds", help="指定书目录阈值文件；缺省逐级向上找 .deslop-thresholds.json")
    scan_parser.add_argument("--writer-family", choices=("claude", "doubao", "gpt", "unknown"))
    scan_parser.add_argument("--include-retired", action="store_true", help="同时列出 retired 规则的度量（不告警）")
    scan_parser.add_argument("--format", choices=("text", "json"), default="text")
    scan_parser.set_defaults(func=cmd_scan)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except GuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
