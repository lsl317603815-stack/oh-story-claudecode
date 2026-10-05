#!/usr/bin/env python3
"""Regression tests for the transactional fiction deslop guard."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "skills/story-deslop/scripts/deslop_guard.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        text=True,
        capture_output=True,
        check=False,
    )


with tempfile.TemporaryDirectory(prefix="deslop-guard-") as raw_tmp:
    tmp = Path(raw_tmp)
    project = tmp / "project"
    body = project / "正文" / "第001章.md"
    body.parent.mkdir(parents=True)
    source = "他守了 3 天。\n《药方》还在桌上。\n他记下 `A-7`。\n"
    body.write_text(source, encoding="utf-8")

    initialized = run(
        "init",
        str(body),
        "--project-root",
        str(project),
        "--run-id",
        "guard-regression",
        "--scope",
        "in-place",
        "--intensity",
        "minimal",
    )
    assert initialized.returncode == 0, initialized.stderr
    run_dir = Path(initialized.stdout.strip())
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    ledger_path = run_dir / "protection-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert manifest["source_path"] == "正文/第001章.md"
    assert manifest["edit_scope"] == "in-place"
    assert ledger["auto"]["numbers"] == ["3", "7"]
    assert ledger["auto"]["titled_terms"] == ["药方"]
    assert ledger["auto"]["inline_code"] == ["A-7"]

    candidate = run_dir / manifest["candidate_file"]
    candidate.write_text(source.replace("3", "三"), encoding="utf-8")
    blocked = run("check", str(run_dir))
    assert blocked.returncode == 2, blocked.stdout + blocked.stderr
    blocked_report = json.loads(blocked.stdout)
    assert any(item["type"] == "protected-literal-missing" for item in blocked_report["blocking"])

    ledger["allowed_changes"] = [{"from": "3", "to": "三"}]
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed = run("check", str(run_dir))
    assert passed.returncode == 0, passed.stdout + passed.stderr
    passed_report = json.loads(passed.stdout)
    assert passed_report["status"] == "pass" and passed_report["changed_span_count"] == 1
    assert (run_dir / "changed-spans.json").is_file()

    denied = run("apply", str(run_dir), "--confirm", "NO")
    assert denied.returncode == 2 and body.read_text(encoding="utf-8") == source
    applied = run("apply", str(run_dir), "--confirm", "APPLY")
    assert applied.returncode == 0, applied.stderr
    assert "守了 三 天" in body.read_text(encoding="utf-8")
    applied_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert applied_manifest["status"] == "applied" and applied_manifest["applied_sha256"]

    stale_source = project / "正文" / "第002章.md"
    stale_source.write_text("原文 8 号。\n", encoding="utf-8")
    stale_init = run(
        "init",
        str(stale_source),
        "--project-root",
        str(project),
        "--run-id",
        "stale-regression",
    )
    assert stale_init.returncode == 0, stale_init.stderr
    stale_dir = Path(stale_init.stdout.strip())
    stale_source.write_text("源文被另一进程修改。\n", encoding="utf-8")
    stale = run("check", str(stale_dir))
    assert stale.returncode == 2
    assert any(item["type"] == "stale-source" for item in json.loads(stale.stdout)["blocking"])

    outside = tmp / "outside.md"
    outside.write_text("不在项目里。\n", encoding="utf-8")
    escaped = run("init", str(outside), "--project-root", str(project), "--run-id", "escape")
    assert escaped.returncode == 2 and "必须位于项目根目录内" in escaped.stderr

    # scan：只读，按规则表 v1 与阈值给告警＋候选位置；check 把候选稿告警并入 advisory，永不 blocking。
    book = tmp / "book"
    chapter = book / "正文" / "第002章.md"
    chapter.parent.mkdir(parents=True)
    rows = ["# 第2章 扫描"] + ["他推开门，屋里很暗，地上堆着旧纸箱，墙角还靠着一把伞。"] * 50
    rows[5:5] = ["他猛地停下——门外有人。"] * 4
    chapter_text = "\n".join(rows) + "\n"
    chapter.write_text(chapter_text, encoding="utf-8")
    scanned = run("scan", str(chapter), "--format", "json")
    assert scanned.returncode == 0, scanned.stderr
    report = json.loads(scanned.stdout)["reports"][0]
    assert chapter.read_text(encoding="utf-8") == chapter_text, "scan 不得改正文"
    assert report["rule_table_version"] == "v1-2026-10-06"
    assert report["thresholds"]["source"] == "default" and report["thresholds"]["pool"] == "都市高武系统"
    alarms = {item["id"]: item for item in report["alarms"]}
    assert alarms["L04"]["alarm"] == "rule-threshold" and len(alarms["L04"]["candidates"]) == 4
    assert alarms["L04"]["candidates"][0]["line"] == 6
    assert alarms["W01"]["alarm"] == "rule-threshold"
    assert {"P12", "P11", "L21", "L16"} <= {rid for rid, item in alarms.items() if item["alarm"] == "deficit-floor"}
    assert all(item["status"] != "retired" or item["alarm"] for item in report["rules"])
    (book / ".deslop-thresholds.json").write_text(json.dumps({
        "schema": "deslop-thresholds/v1", "pool": "都市脑洞游戏制作", "disabled_rules": ["L04"],
    }, ensure_ascii=False), encoding="utf-8")
    rescanned = json.loads(run("scan", str(chapter), "--format", "json").stdout)["reports"][0]
    assert rescanned["thresholds"]["source"] == "book" and rescanned["thresholds"]["pool"] == "都市脑洞游戏制作"
    assert "L04" not in {item["id"] for item in rescanned["alarms"]}

    scan_init = run("init", str(chapter), "--project-root", str(book), "--run-id", "scan-regression")
    assert scan_init.returncode == 0, scan_init.stderr
    checked = run("check", scan_init.stdout.strip())
    assert checked.returncode == 0, checked.stdout
    check_report = json.loads(checked.stdout)
    assert check_report["status"] == "pass" and check_report["surface_scan"]["alarms"] >= 1
    assert any(item["type"] == "rule-threshold" for item in check_report["advisory"])
    assert not any(item["type"] in ("rule-threshold", "deficit-floor") for item in check_report["blocking"])

print("OK: deslop guard stages candidates, protects literals, rejects stale writes, applies explicitly, and scans alarms read-only")
