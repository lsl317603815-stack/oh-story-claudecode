#!/usr/bin/env python3
"""Behavior tests for deterministic prose metrics."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "skills/story-deslop/scripts/prose_metrics.py"
MIRROR = ROOT / "skills/story-long-write/scripts/prose_metrics.py"


def run(path: Path, expected: int = 0, *extra: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(TOOL), *extra, str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != expected:
        raise AssertionError(
            f"expected {expected}, got {completed.returncode}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return json.loads(completed.stdout)


assert TOOL.read_bytes() == MIRROR.read_bytes(), "story-deslop/story-long-write prose metrics must stay identical"

with tempfile.TemporaryDirectory(prefix="prose-metrics-") as temporary:
    root = Path(temporary)
    prose = root / "prose.md"
    prose.write_text(
        "## 第一章 雨夜\n\n"
        "雨停了。\n"
        "林川推开院门，鞋底带进一层泥水，母亲背对着他坐在窗下，正把桌上的旧药包拆开又重新扎紧。\n"
        "他没问。只把灯芯拨亮，又去灶边添了半瓢水，等锅里慢慢冒出热气。\n",
        encoding="utf-8",
    )
    result = run(prose)
    assert result["status"] == "measured", result
    assert result["sentence_count"] == 4, result
    distribution = result["sentence_distribution"]
    assert distribution["short"]["count"] == 2, result
    assert distribution["medium"]["count"] == 1, result
    assert distribution["long"]["count"] == 1, result
    total_rate = sum(distribution[key]["rate_pct"] for key in ("short", "medium", "long"))
    assert abs(total_rate - 100.0) < 0.001, result

    # 不配 prose_bands 时只报实测值，不做文风带对照（没有内置带）。
    assert result["style_band"] == {"status": "not_configured"}, result["style_band"]
    values = result["band_values"]
    assert set(values) >= {"function_word_per_kilo", "bare_quote_rate_pct", "sentence_cv"}, values
    assert result["dialogue_shape"]["quoted_paragraph_count"] == 0, result["dialogue_shape"]

    dialogue = root / "dialogue.md"
    dialogue.write_text(
        "## 第二章 夜谈\n\n"
        "林川把灯拨亮，在母亲对面坐下来，问道：“药是谁送来的？”\n"
        "“隔壁的陈婶。”\n"
        "他把药包翻过来看了看封口，又放回桌上，屋里一时间没人说话了。\n",
        encoding="utf-8",
    )
    shape = run(dialogue)["dialogue_shape"]
    assert shape == {"quoted_paragraph_count": 2, "bare_quote_count": 1, "leading_colon_tag_count": 1,
                     "short_paragraph_count_le12": 1}, shape

    config = root / "门禁配置.json"
    config.write_text(json.dumps({"schema_version": 1, "prose_bands": {
        "note": "本书第1–4章实测",
        "paragraph_mean_chars": {"low": 1, "high": 500, "source": "测试带"},
        "bare_quote_rate_pct": {"low": 0, "high": 10, "label": "裸引语%"},
    }}, ensure_ascii=False), encoding="utf-8")
    banded = run(dialogue, 0, "--gate-config", str(config))["style_band"]
    assert banded["status"] == "measured" and banded["severity"] == "advisory", banded
    assert banded["source"] == "本书第1–4章实测", banded
    assert banded["checks"]["paragraph_mean_chars"]["verdict"] == "within", banded
    assert banded["checks"]["paragraph_mean_chars"]["source"] == "测试带", banded
    assert banded["checks"]["bare_quote_rate_pct"]["verdict"] == "above", banded
    assert banded["off_band"] == ["裸引语%"], banded

    for bad in (
        {"prose_bands": {"made_up_metric": {"low": 0, "high": 1}}},
        {"prose_bands": {"sentence_cv": {"low": 0.6, "high": 0.2}}},
        {"prose_bands": {"sentence_cv": {"low": 0.1}}},
        {"prose_bands": []},
    ):
        config.write_text(json.dumps({"schema_version": 1, **bad}), encoding="utf-8")
        refused = run(dialogue, 2, "--gate-config", str(config))
        assert refused["status"] == "error", refused
    config.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    assert run(dialogue, 0, "--gate-config", str(config))["style_band"] == {"status": "not_configured"}

    empty = root / "empty.md"
    empty.write_text("## 只有标题\n", encoding="utf-8")
    error = run(empty, expected=2)
    assert error["status"] == "error", error

print("OK: prose metrics report deterministic sentence buckets, compare per-book prose_bands as advisory, and reject empty prose or bad bands")
