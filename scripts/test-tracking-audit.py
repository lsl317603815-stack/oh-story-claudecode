#!/usr/bin/env python3
"""Behavior tests for the advisory tracking completeness audit."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "skills/story-long-write/scripts/tracking_audit.py"
PROSE = (
    "# 第十二章 看片会\n\n"
    "江晨把手机原版投到幕布上。周薄森看完没说话，张耀祖敲了两下桌子。\n"
    "散会后钟嘉嘉在走廊拦住江晨，说采访稿过审了，又说他只猜对了一半。\n"
)


def extraction(**overrides: object) -> dict[str, object]:
    document = {
        "chapter_number": 12,
        "title": "看片会",
        "summary": "看片会上原版胜出。",
        "key_events": [],
        "key_information_expansion": [],
        "chapter_formula": {"hook_and_foreshadowing": "钟嘉嘉说江晨只猜对了一半"},
        "characters": [
            {"name": "江晨", "importance": "major", "aliases": [], "performance": ""},
            {"name": "钟嘉嘉", "importance": "supporting", "aliases": ["钟记者"], "performance": ""},
            {"name": "张耀祖", "importance": "minor", "aliases": [], "performance": ""},
        ],
        "plot_points": [
            {"id": "P1", "type": "冲突", "event": "两版片子对比", "characters": ["江晨", "周薄森", "张耀祖"]},
            {"id": "P2", "type": "转折点", "event": "张耀祖拍板用原版", "characters": ["张耀祖", "江晨"]},
            {"id": "P3", "type": "铺垫", "event": "钟嘉嘉说只猜对一半", "characters": ["钟嘉嘉", "江晨"]},
            {"id": "P4", "type": "信息揭示", "event": "采访稿过审", "characters": ["钟嘉嘉"]},
        ],
    }
    document.update(overrides)
    return document


def transaction(**delta: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "append",
        "chapter": 12,
        "chapter_title": "看片会",
        "delta": {
            "result": "原版胜出。",
            "appeared_characters": ["江晨", "钟嘉嘉", "张耀祖", "周薄森"],
            "character_changes": [{"name": "江晨", "change": "作品获高层认可"}],
            "foreshadow_changes": [{"id": "F012"}],
            "timeline_events": [{"id": "E020"}],
            **delta,
        },
        "character_snapshots": {"江晨": {}},
    }


class TrackingAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "book"
        (self.project / "正文").mkdir(parents=True)
        (self.project / "追踪").mkdir()
        (self.project / "正文" / "第012章_看片会.md").write_text(PROSE, encoding="utf-8")
        (self.project / "追踪" / "_tracking-state.json").write_text(
            json.dumps({"characters": {"江晨": {}, "张耀祖": {}}}, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_audit(self, txn: dict[str, object], extracted: dict[str, object] | str, expect: int = 0) -> dict[str, object]:
        txn_path = self.base / "txn.json"
        txn_path.write_text(json.dumps(txn, ensure_ascii=False), encoding="utf-8")
        ext_path = self.base / "extract.txt"
        ext_path.write_text(extracted if isinstance(extracted, str) else json.dumps(extracted, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(TOOL), "--project", str(self.project), "--transaction", str(txn_path), "--extraction", str(ext_path)],
            text=True, capture_output=True, check=False, encoding="utf-8",
        )
        self.assertEqual(completed.returncode, expect, completed.stderr)
        return json.loads(completed.stdout) if expect == 0 else {"stderr": completed.stderr}

    def codes(self, result: dict[str, object]) -> list[str]:
        return [item["code"] for item in result["findings"]]

    def test_complete_transaction_is_clean(self) -> None:
        txn = transaction(character_changes=[
            {"name": "江晨", "change": "作品获高层认可"}, {"name": "张耀祖", "change": "拍板用原版"}
        ])
        result = self.run_audit(txn, extraction())
        self.assertEqual(result["status"], "clean", result)
        self.assertEqual(result["prose"], "正文/第012章_看片会.md")

    def test_unrecorded_appearance_state_change_setup_and_reveal_are_reported(self) -> None:
        txn = transaction(appeared_characters=["江晨"], foreshadow_changes=[], timeline_events=[])
        result = self.run_audit(txn, extraction())
        self.assertEqual(
            self.codes(result),
            ["appearance-unrecorded", "state-change-unrecorded", "foreshadow-unrecorded", "reveal-unrecorded"],
        )
        self.assertIn("钟嘉嘉", result["findings"][0]["message"])
        self.assertIn("张耀祖", result["findings"][0]["message"])  # minor 但出现在 2 个情节点
        self.assertIn("张耀祖", result["findings"][1]["message"])
        self.assertIn("只猜对了一半", result["findings"][2]["message"])

    def test_alias_and_containment_count_as_recorded(self) -> None:
        txn = transaction(appeared_characters=["江晨", "钟记者", "张耀祖总", "周薄森"], character_changes=[
            {"name": "江晨", "change": "x"}, {"name": "张耀祖", "change": "y"}
        ])
        self.assertNotIn("appearance-unrecorded", self.codes(self.run_audit(txn, extraction())))

    def test_names_missing_from_the_prose_are_flagged(self) -> None:
        txn = transaction(appeared_characters=["江晨", "钟嘉嘉", "张耀祖", "周薄森", "林岚"], character_changes=[
            {"name": "江晨", "change": "x"}, {"name": "张耀祖", "change": "y"}, {"name": "林岚", "change": "z"}
        ], )
        (self.project / "追踪" / "_tracking-state.json").write_text(
            json.dumps({"characters": {"江晨": {}, "张耀祖": {}, "林岚": {}}}, ensure_ascii=False), encoding="utf-8"
        )
        result = self.run_audit(txn, extraction())
        self.assertEqual(self.codes(result), ["appearance-not-in-prose", "change-without-appearance"])

    def test_fenced_agent_reply_is_accepted_and_bad_inputs_fail(self) -> None:
        reply = "抽取完成。\n\n```json\n" + json.dumps(extraction(), ensure_ascii=False) + "\n```\n"
        txn = transaction(character_changes=[{"name": "江晨", "change": "x"}, {"name": "张耀祖", "change": "y"}])
        self.assertEqual(self.run_audit(txn, reply)["status"], "clean")
        wrong = self.run_audit(txn, extraction(chapter_number=11), expect=2)
        self.assertIn("第 11 章", wrong["stderr"])
        refused = self.run_audit(txn, {"error": "章节过短"}, expect=2)
        self.assertIn("章节过短", refused["stderr"])
        self.run_audit(txn, "不是 JSON", expect=2)

    def test_missing_prose_skips_name_checks(self) -> None:
        (self.project / "正文" / "第012章_看片会.md").unlink()
        txn = transaction(character_changes=[{"name": "江晨", "change": "x"}, {"name": "张耀祖", "change": "y"}])
        self.assertEqual(self.codes(self.run_audit(txn, extraction())), ["prose-not-found"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
