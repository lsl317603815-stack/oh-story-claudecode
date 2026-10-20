#!/usr/bin/env python3
"""字数门：候选稿汉字数必须落在本章细纲 `字数目标` 的 90%–110% 区间，否则阻断。

SKILL.md 步骤 8 写了“< 目标 90% 补欠账点、> 目标×1.1 压过场”，接纳流水线以前没有门禁执行它，
实测一本书的前四章因此涨到细纲目标的两倍以上。本门只读细纲里的 `字数目标`，读法与
check-outline-retention.js 相同（字段后 6 个字符内的第一个数字，可写成区间 `3000-3500 字`）；
区间按低端算下限、高端算上限。细纲缺该字段或写不出数字时按 3000 字代入并提示补纲
（SKILL.md「字数验收权威」的唯一兜底值）。

它和细纲长度检查（check-outline-retention.js：细纲 > 字数目标×0.5 提醒、×0.7 拦截）用同一个
`字数目标` 作分母，量的是不同产物：本门量正文，那边量细纲。两条只各管一端，不会互相逼迫：
1:2.5–1:3 的细纲约为目标的 33%–40%，正文落在 90%–110% 时两条同时满足。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from voice_profile import chinese_count, normalized_lines

FALLBACK_TARGET = 3000
LOWER_RATIO = 0.9
UPPER_RATIO = 1.1
FIELD = "字数目标"
# 与 check-outline-retention.js parseTarget 同一读法；这里多收两位数，便于极短的测试细纲。
TARGET_PATTERN = re.compile(r"字数目标[^0-9\n]{0,6}([0-9]{2,6})(?:\s*(?:字)?\s*[-~～—–至到]\s*([0-9]{2,6}))?")


def read_target(outline: Path) -> tuple[tuple[int, int] | None, str]:
    """Return ((low, high), source line) or (None, reason for the fallback)."""
    if not outline.is_file():
        return None, f"细纲不存在: {outline}"
    for line in outline.read_text(encoding="utf-8").splitlines():
        if FIELD not in line:
            continue
        match = TARGET_PATTERN.search(line)
        if not match:
            return None, f"细纲字数目标无法解析: {line.strip()}"
        first = int(match.group(1))
        second = int(match.group(2)) if match.group(2) else first
        low, high = min(first, second), max(first, second)
        if low <= 0:
            return None, f"细纲字数目标无法解析: {line.strip()}"
        return (low, high), line.strip()
    return None, f"细纲缺少 `{FIELD}` 字段"


def measure(candidate: Path, outline: Path) -> dict[str, Any]:
    actual = chinese_count("".join(normalized_lines(candidate.read_text(encoding="utf-8"))))
    target, source = read_target(outline)
    fallback = target is None
    low, high = target or (FALLBACK_TARGET, FALLBACK_TARGET)
    shown = str(low) if low == high else f"{low}–{high}"
    lower = int(round(low * LOWER_RATIO))
    upper = int(round(high * UPPER_RATIO))
    if actual < lower:
        result = "under"
        message = (f"候选稿 {actual} 字，低于细纲目标 {shown} 字的 90%（下限 {lower}，差 {lower - actual} 字）。"
                   "对照细纲情节点预算找欠账的密点，一次性补到配额；细纲内容不够时输出 outline_underfilled 先补纲。")
    elif actual > upper:
        result = "over"
        message = (f"候选稿 {actual} 字，超出细纲目标 {shown} 字的 110%（上限 {upper}，多 {actual - upper} 字）。"
                   "剧情推进量不变，压过场、合并疏点、拆厚段：每段一件事，对白后不追加解释段；不删主线爽点。")
    else:
        result = "pass"
        message = f"候选稿 {actual} 字，落在细纲目标 {shown} 字的 90%–110% 区间（{lower}–{upper}）。"
    findings = [{
        "file": str(candidate),
        "line": 1,
        "column": 1,
        "type": "word-target",
        "severity": "info" if result == "pass" else "blocking",
        "message": message,
    }]
    if fallback:
        findings.append({
            "file": str(outline),
            "line": 1,
            "column": 1,
            "type": "word-target-fallback",
            "severity": "advisory",
            "message": f"{source}；已按 {FALLBACK_TARGET} 字兜底验收，请在细纲补 `{FIELD}：N 字`。",
        })
    return {
        "status": "passed" if result == "pass" else "failed",
        "result": result,
        "file": str(candidate),
        "outline": str(outline),
        "target_chars": [low, high] if low != high else low,
        "target_source": None if fallback else source,
        "fallback_applied": fallback,
        "actual_chars": actual,
        "lower_bound": lower,
        "upper_bound": upper,
        "findings": findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("candidate", help="候选稿 Markdown")
    parser.add_argument("--outline", required=True, help="本章细纲 Markdown")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candidate = Path(args.candidate).expanduser().resolve()
    outline = Path(args.outline).expanduser().resolve()
    if not candidate.is_file():
        print(json.dumps({"status": "error", "message": f"候选稿不存在: {candidate}"}, ensure_ascii=False, indent=2))
        return 2
    try:
        payload = measure(candidate, outline)
    except (OSError, UnicodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
