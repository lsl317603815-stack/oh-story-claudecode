#!/usr/bin/env python3
"""Behavior tests for the 细纲 字数目标 gate (word_target_gate.py)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "skills/story-long-write/scripts/word_target_gate.py"
SENTENCE = "林川推开院门，把药包放到桌边。"  # 13 个汉字


def run(candidate: Path, outline: Path, expected: int) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(TOOL), "--outline", str(outline), str(candidate)],
        text=True, capture_output=True, check=False, encoding="utf-8",
    )
    if completed.returncode != expected:
        raise AssertionError(
            f"expected {expected}, got {completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return json.loads(completed.stdout)


def prose(path: Path, sentences: int) -> Path:
    # 标题行不计字数，与 prose_metrics / voice_profile 同一口径。
    path.write_text("# 第一章 春雨入院\n\n" + "\n".join([SENTENCE] * sentences) + "\n", encoding="utf-8")
    return path


def outline(path: Path, target_line: str | None) -> Path:
    body = "# 第一章细纲\n\n- 章节定位：普通推进\n"
    if target_line is not None:
        body += f"- {target_line}\n"
    path.write_text(body + "\n林川冒雨回家。\n", encoding="utf-8")
    return path


with tempfile.TemporaryDirectory(prefix="word-target-") as temporary:
    root = Path(temporary)
    candidate = prose(root / "candidate.md", 10)  # 130 字
    target = outline(root / "细纲.md", "字数目标：130 字")

    passed = run(candidate, target, 0)
    assert passed["status"] == "passed" and passed["result"] == "pass", passed
    assert (passed["actual_chars"], passed["target_chars"]) == (130, 130), passed
    assert (passed["lower_bound"], passed["upper_bound"]) == (117, 143), passed
    assert passed["fallback_applied"] is False and passed["target_source"] == "- 字数目标：130 字", passed

    short = run(prose(root / "short.md", 8), target, 1)  # 104 < 117
    assert short["result"] == "under", short
    assert short["findings"][0]["severity"] == "blocking" and "差 13 字" in short["findings"][0]["message"], short

    long = run(prose(root / "long.md", 12), target, 1)  # 156 > 143
    assert long["result"] == "over" and "多 13 字" in long["findings"][0]["message"], long

    # 区间目标：低端算下限，高端算上限；“约”等前缀与 check-outline-retention.js 同一读法。
    ranged = outline(root / "区间.md", "字数目标：约 100-130 字")
    assert run(prose(root / "r1.md", 7), ranged, 0)["lower_bound"] == 90  # 91 字
    assert run(prose(root / "r2.md", 11), ranged, 0)["upper_bound"] == 143  # 143 字
    assert run(prose(root / "r3.md", 6), ranged, 1)["result"] == "under"  # 78 字

    # 缺字段或写不出数字：按 3000 兜底照样阻断，并提示补纲。
    for name, line in (("缺字段.md", None), ("无数字.md", "字数目标：三千字")):
        fallback = run(candidate, outline(root / name, line), 1)
        assert fallback["fallback_applied"] is True and fallback["target_chars"] == 3000, fallback
        assert fallback["result"] == "under", fallback
        notice = [item for item in fallback["findings"] if item["type"] == "word-target-fallback"]
        assert len(notice) == 1 and notice[0]["severity"] == "advisory", fallback
    thousands = run(prose(root / "full.md", 231), outline(root / "缺.md", None), 0)  # 3003 字
    assert thousands["fallback_applied"] is True, thousands

    missing = subprocess.run(
        [sys.executable, str(TOOL), "--outline", str(target), str(root / "没有.md")],
        text=True, capture_output=True, check=False, encoding="utf-8",
    )
    assert missing.returncode == 2 and json.loads(missing.stdout)["status"] == "error", missing.stdout

print("OK: word target gate passes 90%–110% of 字数目标, blocks under/over, reads ranges, and falls back to 3000")
