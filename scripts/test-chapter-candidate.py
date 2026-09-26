#!/usr/bin/env python3
"""Behavior tests for the pre-acceptance chapter pipeline, review receipts, and story doctor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/story-long-write/scripts"
CANDIDATE = SCRIPTS / "chapter_candidate.py"
TRACKING = SCRIPTS / "tracking_commit.py"
DOCTOR = SCRIPTS / "story_doctor.py"

TITLE = "# 第一章 春雨入院\n\n"
PROSE = TITLE + "林川推开木门，雨水顺着青石缝流进院里。\n他把药包放到桌边，问母亲今天是否好些。\n母亲把药包往灯下挪了挪，说里头少了一味药。\n"
# 超过 200 字、没有任何情绪落点、章尾也不留钩子：情绪下限与黄金三章钩子都会拦
FLAT = TITLE + (
    "林川把账本摊在桌上，从第一页开始对数。米行上月进了四十担糙米，卖出三十二担，剩下的堆在后仓。\n"
    "伙计阿福搬来算盘，把零头又拨了一遍。门外的车夫在卸新到的麻袋，麻绳一圈圈绕在木桩上。\n"
    "账房先生端着茶碗进来，把三张欠条压在砚台底下，说东街铺子下午来结账。\n"
    "林川点头，把欠条上的日子抄进册子，又在页边写了一个数。阿福收起算盘，去后仓清点剩下的糙米。\n"
    "窗外的日头挪到檐角，街上卖豆腐的吆喝了两声。林川合上账本，把铅笔插回笔筒，起身去后仓看米。\n"
    "后仓的门板新刷过桐油，阿福把米袋按月份码成三排，每排贴一张纸条写明斤两。\n"
    "林川一排排数过去，数到第二排时停下来，把纸条上的斤两和册子上的数对了一遍，又往下数。\n"
    "数完三排，他把册子夹在腋下，锁上后仓的门，沿着廊下回到前堂，把钥匙挂回原来的钉子上。\n"
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(tool: Path, *args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, str(tool), *args], text=True, capture_output=True, check=False, encoding="utf-8"
    )
    if completed.returncode != expected:
        raise AssertionError(
            f"expected {expected}, got {completed.returncode}: {tool.name} {' '.join(args)}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def initial_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "book_title": "候选门禁测试",
        "last_chapter": 0,
        "context": {
            "position": {"volume": "第一卷", "volume_start_chapter": 1, "story_time": "春日清晨", "scene": "林家旧院"},
            "long_term_constraints": [],
            "active_character_names": [],
            "continuity_risks": [],
            "recent_chapters": [],
            "next_chapter_commitments": [],
        },
        "character_snapshots": {},
        "foreshadow": [],
        "timeline_events": [],
        "facts": [],
    }


def chapter_transaction(revision: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "append",
        "chapter": 1,
        "chapter_title": "春雨入院",
        "expected_state_revision": revision,
        "delta": {
            "result": "林川送回药包，发现药包里少了一味药。",
            "appeared_characters": ["林川", "母亲"],
            "character_changes": [],
            "foreshadow_changes": [],
            "timeline_events": [],
            "fact_changes": [],
            "constraints": [],
            "next_chapter_commitments": ["查清药包里缺失的一味药。"],
        },
        "context": {
            "position": {"volume": "第一卷", "volume_start_chapter": 1, "story_time": "春日清晨", "scene": "林家旧院"},
            "long_term_constraints": [],
            "active_character_names": [],
            "continuity_risks": [],
        },
        "character_snapshots": {},
    }


class ChapterCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="chapter-candidate-")
        self.base = Path(self.temporary.name)
        self.project = self.base / "book"
        (self.project / "大纲").mkdir(parents=True)
        (self.project / "正文").mkdir()
        self.outline = self.project / "大纲" / "细纲_第001章.md"
        self.write_outline("普通推进")
        write_json(self.base / "init.json", initial_document())
        run(TRACKING, "init", "--project", str(self.project), "--input", str(self.base / "init.json"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────
    def write_outline(self, position: str) -> None:
        self.outline.write_text(
            f"# 第一章细纲\n\n- 章节定位：{position}\n\n林川冒雨回家，把药包交给母亲。\n", encoding="utf-8"
        )

    def init(self, *extra: str, run_id: str = "C001") -> Path:
        created = run(
            CANDIDATE, "init", "--project", str(self.project), "--chapter", "1",
            "--outline", "大纲/细纲_第001章.md", "--target", "正文/第001章_春雨入院.md", "--id", run_id, *extra,
        )
        self.run_dir = Path(created.stdout.strip())
        self.candidate = self.run_dir / "candidate.md"
        return self.run_dir

    def manifest(self) -> dict[str, object]:
        return json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))

    def check(self, expected: int = 0) -> dict[str, object]:
        completed = run(CANDIDATE, "check", "--run", str(self.run_dir), expected=expected)
        return json.loads(completed.stdout)

    def next(self) -> dict[str, object]:
        return json.loads(run(CANDIDATE, "next", "--project", str(self.project)).stdout)

    def packet(self, kind: str, *extra: str, expected: int = 0) -> dict[str, object]:
        completed = run(CANDIDATE, "review-packet", "--run", str(self.run_dir), "--kind", kind, *extra, expected=expected)
        return json.loads(completed.stdout) if expected == 0 else {"stderr": completed.stderr}

    def report(self, packet: dict[str, object], *, verdict: str = "PASS", findings: list | None = None,
               fenced: bool = False, **overrides: object) -> Path:
        body = {
            "kind": packet["kind"],
            "nonce": packet["nonce"],
            "candidate_sha256": packet["candidate_sha256"],
            "scope": packet["scope"],
            "verdict": verdict,
            "findings": findings or [],
            "coverage": ["林川", "母亲", "药包"],
            "summary": "审查完成",
            **overrides,
        }
        path = self.base / f"{packet['kind']}-{os.urandom(3).hex()}.txt"
        text = json.dumps(body, ensure_ascii=False, indent=2)
        path.write_text(f"VERDICT: {verdict}\n\n```json\n{text}\n```\n" if fenced else text, encoding="utf-8")
        return path

    def attest(self, kind: str, report: Path, expected: int = 0) -> subprocess.CompletedProcess[str]:
        return run(CANDIDATE, "attest", "--run", str(self.run_dir), "--kind", kind, "--report", str(report), expected=expected)

    def deslop(self, *, edit: tuple[str, str] | None = None, **report_args: object) -> None:
        packet = self.packet("deslop")
        work = self.project / str(packet["work_copy"])
        if edit:
            work.write_text(work.read_text(encoding="utf-8").replace(*edit), encoding="utf-8")
        self.attest("deslop", self.report(packet, **report_args))

    def consistency(self, **report_args: object) -> dict[str, object]:
        packet = self.packet("consistency")
        self.attest("consistency", self.report(packet, **report_args))
        return packet

    def reviewed(self, prose: str = PROSE) -> None:
        self.candidate.write_text(prose, encoding="utf-8")
        self.check()
        self.deslop()
        self.check()
        self.consistency()

    def finish_chapter(self) -> None:
        run(CANDIDATE, "promote", "--run", str(self.run_dir), "--confirm", "PROMOTE")
        revision = json.loads((self.project / "追踪/_tracking-state.json").read_text(encoding="utf-8"))["state_revision"]
        write_json(self.base / "chapter.json", chapter_transaction(revision))
        run(TRACKING, "commit", "--project", str(self.project), "--input", str(self.base / "chapter.json"))
        run(CANDIDATE, "close", "--run", str(self.run_dir))

    # ── 主流程 ───────────────────────────────────────────────────────────
    def test_full_review_flow_gates_reviews_receipts_and_doctor(self) -> None:
        self.init()
        self.assertEqual(self.next()["step"], "write")
        self.candidate.write_text(PROSE.replace("母亲把药包", "\n---\n\n母亲把药包"), encoding="utf-8")

        failed = self.check(expected=2)
        self.assertEqual(failed["failures"], ["punctuation"])
        self.assertEqual(len(failed["gates"]), 14)
        self.assertTrue(all(item["script_sha256"] for item in failed["gates"]))
        step = self.next()
        self.assertEqual(step["step"], "revise")
        self.assertIn("fix", step["commands"][0])

        fixed = json.loads(run(CANDIDATE, "fix", "--run", str(self.run_dir)).stdout)
        self.assertTrue(fixed["changed"])
        self.assertNotIn("---", self.candidate.read_text(encoding="utf-8"))
        passed = self.check()
        self.assertEqual(passed["status"], "pass")
        self.assertEqual(passed["pressure"]["level"], "normal")
        report = json.loads((self.run_dir / "gate-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["candidate_sha256"], passed["candidate_sha256"])

        blocked = run(CANDIDATE, "approve", "--run", str(self.run_dir), "--confirm", "ACCEPT",
                      "--approval-note", "用户明确采用这一版", expected=2)
        self.assertIn("缺少去味审查回执", blocked.stderr)
        self.assertIn("缺少一致性审查回执", blocked.stderr)
        self.assertEqual(self.next()["step"], "review")
        self.packet("consistency", expected=2)  # 一致性审查必须排在去味之后

        packet = self.packet("deslop")
        self.assertEqual(packet["scope"], "full")
        self.assertIn(packet["nonce"], packet["prompt"])
        self.assertEqual(self.next()["step"], "attest")
        work = self.project / str(packet["work_copy"])
        work.write_text(work.read_text(encoding="utf-8").replace("问母亲今天是否好些", "问母亲今天好些没有"), encoding="utf-8")
        before = self.candidate.read_text(encoding="utf-8")
        self.attest("deslop", self.report(packet, verdict="CONCERNS", fenced=True, findings=[
            {"severity": "S3", "category": "书面腔", "quote": "问母亲今天是否好些", "issue": "口语化", "action": "edited"}
        ]))
        self.assertNotEqual(self.candidate.read_text(encoding="utf-8"), before)
        self.assertIn("今天好些没有", self.candidate.read_text(encoding="utf-8"))
        self.assertEqual(self.next()["step"], "check")

        self.check()
        self.consistency()
        step = self.next()
        self.assertEqual(step["step"], "await-author")
        run(CANDIDATE, "approve", "--run", str(self.run_dir), "--confirm", "ACCEPT", "--approval-note", "用户明确采用这一版")
        self.assertEqual(self.next()["step"], "promote")

        self.finish_chapter()
        chapter_receipt = json.loads((self.project / "追踪/章节提交/第001章.json").read_text(encoding="utf-8"))
        self.assertEqual(chapter_receipt["protocol"], "gated-v2")
        quality = self.project / "追踪/质检回执/第001章"
        for name in ("gate-report.json", "deslop.json", "deslop.report.json", "consistency.json", "consistency.report.json"):
            self.assertTrue((quality / name).is_file(), name)
        progress = (self.project / "追踪/质检进度.md").read_text(encoding="utf-8")
        self.assertIn("| 第001章 | 已闭环 | 作者接纳 |", progress)
        self.assertIn("CONCERNS（全文）", progress)

        doctor = json.loads(run(DOCTOR, "--project", str(self.project)).stdout)
        self.assertEqual(doctor["status"], "pass", doctor)
        receipts_check = next(item for item in doctor["checks"] if item["name"] == "quality_receipts")
        self.assertEqual(receipts_check["gated"], 1)
        self.assertEqual(self.next()["step"], "init")

        (quality / "consistency.json").write_text("{}\n", encoding="utf-8")
        broken = json.loads(run(DOCTOR, "--project", str(self.project), expected=2).stdout)
        self.assertTrue(any(item["code"] == "quality-receipt-digest-mismatch" for item in broken["errors"]), broken)

        final_prose = self.project / "正文" / "第001章_春雨入院.md"
        final_prose.write_text(final_prose.read_text(encoding="utf-8") + "静默手改。\n", encoding="utf-8")
        broken = json.loads(run(DOCTOR, "--project", str(self.project), expected=2).stdout)
        self.assertTrue(any(item["code"] == "accepted-prose-digest-mismatch" for item in broken["errors"]), broken)

    def test_exact_permission_staleness_and_post_approval_edits(self) -> None:
        self.init()
        blocked_parallel = run(
            CANDIDATE, "init", "--project", str(self.project), "--chapter", "1", "--outline", "大纲/细纲_第001章.md",
            "--target", "正文/第001章_另一版.md", expected=2,
        )
        self.assertIn("未闭环候选章", blocked_parallel.stderr)
        self.reviewed()
        original = self.outline.read_text(encoding="utf-8")
        self.outline.write_text(original + "\n临时改动。\n", encoding="utf-8")
        stale = run(CANDIDATE, "check", "--run", str(self.run_dir), "--freshness-only", expected=2)
        self.assertIn("已过期", stale.stderr)
        self.outline.write_text(original, encoding="utf-8")

        run(CANDIDATE, "approve", "--run", str(self.run_dir), "--confirm", "ACCEPT", expected=2)  # 没有接纳说明
        run(CANDIDATE, "approve", "--run", str(self.run_dir), "--confirm", "ACCEPT", "--approval-note", "采用")
        self.candidate.write_text(self.candidate.read_text(encoding="utf-8") + "又添一句。\n", encoding="utf-8")
        changed = run(CANDIDATE, "promote", "--run", str(self.run_dir), "--confirm", "PROMOTE", expected=2)
        self.assertIn("接纳后被修改", changed.stderr)

    # ── 审查回执 ─────────────────────────────────────────────────────────
    def test_consistency_s1_blocks_until_an_edit_is_re_reviewed(self) -> None:
        self.init()
        self.candidate.write_text(PROSE, encoding="utf-8")
        self.check()
        self.deslop()
        self.check()
        self.consistency(verdict="CONCERNS", findings=[
            {"severity": "S1", "category": "伏笔", "quote": "里头少了一味药", "issue": "与设定冲突"}
        ])
        blocked = run(CANDIDATE, "approve", "--run", str(self.run_dir), "--confirm", "ACCEPT",
                      "--approval-note", "采用", expected=2)
        self.assertIn("仍有 1 条 S1/S2", blocked.stderr)

        self.candidate.write_text(PROSE.replace("里头少了一味药", "里头缺了一味药"), encoding="utf-8")
        self.assertEqual(self.next()["step"], "check")
        self.check()
        step = self.next()
        self.assertEqual((step["step"], step.get("kind")), ("review", "consistency"))  # 小修不作废去味回执
        self.consistency()
        run(CANDIDATE, "approve", "--run", str(self.run_dir), "--confirm", "ACCEPT", "--approval-note", "采用")

    def test_report_must_quote_verbatim_and_match_the_packet(self) -> None:
        self.init()
        self.candidate.write_text(PROSE, encoding="utf-8")
        self.check()
        packet = self.packet("deslop")
        invented = self.attest("deslop", self.report(packet, findings=[
            {"severity": "S3", "category": "编造", "quote": "候选稿里根本没有这句话", "issue": "", "action": "flagged"}
        ]), expected=2)
        self.assertIn("找不到逐字原文", invented.stderr)
        wrong_nonce = self.attest("deslop", self.report(packet, nonce="0" * 16), expected=2)
        self.assertIn("nonce 不匹配", wrong_nonce.stderr)
        thin = self.attest("deslop", self.report(packet, coverage=["全文"]), expected=2)
        self.assertIn("至少要在 coverage 列出 3 项", thin.stderr)

        self.candidate.write_text(PROSE + "补一句。\n", encoding="utf-8")
        moved = self.attest("deslop", self.report(packet), expected=2)
        self.assertIn("发出审查包后被改动", moved.stderr)

    def test_consistency_quote_may_cite_another_project_file(self) -> None:
        (self.project / "设定").mkdir()
        (self.project / "设定" / "药铺.md").write_text("林家药铺只卖晒干的当归。\n", encoding="utf-8")
        self.init()
        self.candidate.write_text(PROSE, encoding="utf-8")
        self.check()
        self.deslop()
        self.check()
        packet = self.packet("consistency")
        self.attest("consistency", self.report(packet, verdict="CONCERNS", findings=[
            {"severity": "S3", "category": "设定", "quote": "只卖晒干的当归", "source": "设定/药铺.md", "issue": "核对"}
        ]))

    def test_large_edit_after_deslop_requires_a_new_deslop_review(self) -> None:
        self.init()
        self.reviewed()
        rewritten = TITLE + "林川在雨里站了很久，才推门进院。\n桌上的油灯已经点起来，母亲靠在床头等他。\n他把药包递过去，母亲一样样摸过，摇了摇头。\n"
        self.candidate.write_text(rewritten, encoding="utf-8")
        self.check()
        step = self.next()
        self.assertEqual((step["step"], step.get("kind")), ("review", "deslop"))
        self.assertIn("重新发去味审查包", step["why"])

    # ── 启发式门禁：豁免、按书降级、压力档 ───────────────────────────────
    def test_waiver_needs_review_mode_and_a_real_failure(self) -> None:
        self.init()
        self.candidate.write_text(FLAT, encoding="utf-8")
        failed = self.check(expected=2)
        self.assertIn("hook_strength", failed["failures"])
        self.assertIn("emotion_floor", failed["failures"])
        run(CANDIDATE, "waive", "--run", str(self.run_dir), "--gate", "typos", "--reason", "x", "--confirm", "WAIVE", expected=2)
        run(CANDIDATE, "waive", "--run", str(self.run_dir), "--gate", "dialogue_drift", "--reason", "x",
            "--confirm", "WAIVE", expected=2)  # 没失败的门不能豁免
        for gate in ("hook_strength", "emotion_floor"):
            run(CANDIDATE, "waive", "--run", str(self.run_dir), "--gate", gate, "--reason", "作者确认：账房日常章的误报",
                "--confirm", "WAIVE")
        passed = self.check()
        self.assertEqual(sorted(passed["waived"]), ["emotion_floor", "hook_strength"])

    def test_auto_mode_refuses_waivers_and_lowering_pressure(self) -> None:
        self.write_outline("高压")
        self.init("--approval-mode", "auto", "--authorization-note", "用户说连续写完并自动定稿")
        self.candidate.write_text(FLAT, encoding="utf-8")
        failed = self.check(expected=2)
        self.assertEqual(failed["pressure"], {"level": "high", "source": "outline", "position": "高压"})
        refused = run(CANDIDATE, "waive", "--run", str(self.run_dir), "--gate", "emotion_floor", "--reason", "x",
                      "--confirm", "WAIVE", expected=2)
        self.assertIn("auto 模式不允许豁免", refused.stderr)
        lowered = run(CANDIDATE, "pressure", "--run", str(self.run_dir), "--level", "low", "--note", "想省事", expected=2)
        self.assertIn("只能调高", lowered.stderr)

    def test_book_config_can_demote_heuristic_gates_but_not_hard_gates(self) -> None:
        write_json(self.project / "设定" / "门禁配置.json", {"schema_version": 1, "gates": {"language": {"mode": "advisory"}}})
        refused = run(CANDIDATE, "init", "--project", str(self.project), "--chapter", "1", "--outline", "大纲/细纲_第001章.md",
                      "--target", "正文/第001章_春雨入院.md", expected=2)
        self.assertIn("只能调整启发式门禁", refused.stderr)
        write_json(self.project / "设定" / "门禁配置.json", {"schema_version": 1, "gates": {
            "emotion_floor": {"mode": "advisory", "reason": "本书是冷调推理"},
            "hook_strength": {"mode": "advisory"},
        }})
        self.init()
        self.candidate.write_text(FLAT, encoding="utf-8")
        passed = self.check()
        self.assertIn("emotion_floor", passed["advisories"])
        self.assertTrue(passed["gate_report"])
        report = json.loads((self.run_dir / "gate-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["gate_config"], "设定/门禁配置.json")
        self.assertTrue(report["gate_config_sha256"])

    def test_language_failure_stops_the_other_gates(self) -> None:
        self.init()
        self.candidate.write_text(TITLE + "林川推开门。This sentence should never appear in the chapter at all.\n", encoding="utf-8")
        failed = self.check(expected=2)
        self.assertEqual([item["name"] for item in failed["gates"]], ["writing_method", "language"])
        self.assertEqual(failed["failures"], ["language"])

    # ── auto 模式精简审查 ────────────────────────────────────────────────
    def test_lean_review_policy_is_auto_only_and_full_on_batch_end(self) -> None:
        refused = run(CANDIDATE, "init", "--project", str(self.project), "--chapter", "1", "--outline", "大纲/细纲_第001章.md",
                      "--target", "正文/第001章_春雨入院.md", "--review-policy", "lean", expected=2)
        self.assertIn("只用于 auto 模式", refused.stderr)
        self.init("--approval-mode", "auto", "--authorization-note", "用户授权自动定稿", "--review-policy", "lean")
        self.candidate.write_text(PROSE, encoding="utf-8")
        self.check()
        packet = self.packet("deslop")
        self.assertEqual(packet["scope"], "lean")
        self.assertTrue(packet["spans"])
        self.assertIn("精简范围", packet["prompt"])
        self.attest("deslop", self.report(packet))
        self.consistency()
        self.assertEqual(self.next()["step"], "approve")
        run(CANDIDATE, "approve", "--run", str(self.run_dir), "--confirm", "ACCEPT")

    def test_batch_last_chapter_needs_a_full_deslop(self) -> None:
        self.init("--approval-mode", "auto", "--authorization-note", "用户授权自动定稿", "--review-policy", "lean", "--batch-last")
        self.candidate.write_text(PROSE, encoding="utf-8")
        self.check()
        self.assertEqual(self.packet("deslop")["scope"], "full")

    # ── 兼容旧版 manifest 与手工质检进度 ─────────────────────────────────
    def legacy_manifest(self, status: str) -> None:
        manifest = self.manifest()
        for key in ("protocol", "review_policy", "batch_last", "pressure_override", "lineage", "gate_run",
                    "packets", "reviews", "waivers"):
            manifest.pop(key, None)
        manifest["schema_version"] = 1
        manifest["status"] = status
        if status == "approved":
            manifest["approval"] = {"approved_at": "2026-01-01T00:00:00Z", "mode": "review", "note": "旧版接纳",
                                    "candidate_sha256": __import__("hashlib").sha256(self.candidate.read_bytes()).hexdigest()}
        write_json(self.run_dir / "manifest.json", manifest)

    def test_legacy_draft_is_upgraded_and_must_complete_reviews(self) -> None:
        self.init()
        self.candidate.write_text(PROSE, encoding="utf-8")
        self.legacy_manifest("draft")
        self.check()
        self.assertEqual(self.manifest()["schema_version"], 2)
        blocked = run(CANDIDATE, "approve", "--run", str(self.run_dir), "--confirm", "ACCEPT",
                      "--approval-note", "采用", expected=2)
        self.assertIn("缺少去味审查回执", blocked.stderr)

    def test_legacy_approved_candidate_promotes_under_the_old_gates(self) -> None:
        (self.project / "追踪" / "质检进度.md").write_text("# 质检进度 — 手工版\n\n| 第001章 | ✓ |\n", encoding="utf-8")
        self.init()
        self.candidate.write_text(FLAT, encoding="utf-8")  # 新门禁会拦，旧 8 道门不拦
        self.legacy_manifest("approved")
        self.finish_chapter()
        chapter_receipt = json.loads((self.project / "追踪/章节提交/第001章.json").read_text(encoding="utf-8"))
        self.assertEqual(chapter_receipt["protocol"], "legacy-v1")
        self.assertFalse((self.project / "追踪/质检回执").exists())
        archived = self.project / "追踪" / "质检进度_旧版手工记录.md"
        self.assertIn("手工版", archived.read_text(encoding="utf-8"))
        self.assertIn("旧协议，无回执", (self.project / "追踪/质检进度.md").read_text(encoding="utf-8"))
        doctor = json.loads(run(DOCTOR, "--project", str(self.project)).stdout)
        self.assertEqual(doctor["status"], "pass", doctor)
        self.assertTrue(any(item["code"] == "quality-receipts-legacy" for item in doctor["warnings"]), doctor)


if __name__ == "__main__":
    unittest.main(verbosity=2)
