#!/usr/bin/env python3
"""Measure Chinese prose sentence and paragraph shape without model estimates.

Besides sentence buckets, it measures dialogue shape (bare quotes, leading-colon
tags, short paragraphs) and narration texture (function words, particles, modal
endings, first/second person, ！/？ endings, degree adverbs, sentence-length CV).
These are plain counts. A book may compare them with its own calibrated bands
through ``--gate-config <设定/门禁配置.json>`` → ``prose_bands``; the result is
advisory only, and there are no built-in bands because a band measured on one
book is not a quality quota for another.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any

from voice_profile import chinese_count, normalized_lines, prose_metrics, split_sentences


def rounded(value: float) -> float:
    return round(float(value), 4)


def percentage(count: int, total: int) -> float:
    return rounded(count * 100 / total) if total else 0.0


FUNCTION_WORDS = ("的", "了", "就", "在", "是", "也", "都", "还", "又", "把", "被", "给", "这个", "那个", "里面", "以后",
                  "时候", "现在", "因为", "所以", "但是", "不过", "然后", "已经", "还是", "起来", "出来", "下去")
PARTICLE_PATTERN = re.compile(r"[的了着过呢吧啊呀嘛么]")
MODAL_END = re.compile(r"[啊呢吧嘛呀哦][！？。…]*$")
SECOND_PERSON = re.compile(r"[我你您咱]")
DEGREE_ADVERBS = ("完全", "绝对", "根本", "相当", "彻底", "特别", "真的", "简直", "居然", "竟然", "果然", "明明", "反正",
                  "到底", "压根", "干脆", "好歹", "差点", "恐怕", "估计", "大概", "多半", "似乎", "好像", "应该")
QUOTE_ONLY = re.compile(r"^[“「][^”」]*[”」]$")
LEADING_COLON_TAG = re.compile(r"^[^“「]{2,}[:：][“「]")

# prose_bands 可比较的实测项及默认标签；口径见各自的计算处。
BAND_METRICS: dict[str, str] = {
    "paragraph_mean_chars": "段均字数",
    "sentence_mean_chars": "句均字数",
    "short_sentence_rate_pct": "短句(<15字)占比%",
    "bare_quote_rate_pct": "裸引语占含引号段%",
    "leading_colon_tag_count": "前置冒号标签段数",
    "short_paragraph_rate_pct": "≤12字段落占比%",
    "function_word_per_kilo": "叙述口语虚词/千字",
    "particle_per_kilo": "叙述语气助词/千字",
    "narr_modal_end_pct": "叙述句尾语气词%",
    "narr_second_person_pct": "叙述句含你我咱%",
    "narr_exclaim_pct": "叙述句以！收%",
    "narr_question_pct": "叙述句以？收%",
    "degree_adverb_per_kilo": "程度与口语副词/千字",
    "sentence_cv": "句长离散度",
}
BAND_KEYS = {"low", "high", "label", "source"}


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load_bands(config_path: Path) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Read ``prose_bands`` from a book's gate config; an absent key means no bands."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"门禁配置不是合法 JSON: {config_path}: {exc}") from exc
    bands = raw.get("prose_bands") if isinstance(raw, dict) else None
    if bands is None:
        return {}, None
    if not isinstance(bands, dict):
        raise ValueError("prose_bands 必须是 object")
    note = bands.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("prose_bands.note 必须是字符串")
    result: dict[str, dict[str, Any]] = {}
    for key, band in bands.items():
        if key == "note":
            continue
        if key not in BAND_METRICS:
            raise ValueError(f"prose_bands 含未知实测项 {key}；可用: {', '.join(BAND_METRICS)}")
        if not isinstance(band, dict) or set(band) - BAND_KEYS or not {"low", "high"} <= set(band):
            raise ValueError(f"prose_bands.{key} 必须是 {{low, high, label?, source?}}")
        if not is_number(band["low"]) or not is_number(band["high"]) or band["low"] > band["high"]:
            raise ValueError(f"prose_bands.{key} 的 low/high 必须是数字且 low ≤ high")
        for text_key in ("label", "source"):
            if text_key in band and not isinstance(band[text_key], str):
                raise ValueError(f"prose_bands.{key}.{text_key} 必须是字符串")
        result[key] = band
    return result, note


def band_check(value: float, band: dict[str, Any], label: str) -> dict[str, Any]:
    if value < band["low"]:
        verdict = "below"
    elif value > band["high"]:
        verdict = "above"
    else:
        verdict = "within"
    return {
        "label": band.get("label", label),
        "value": value,
        "band": [band["low"], band["high"]],
        "source": band.get("source"),
        "verdict": verdict,
    }


def style_band(values: dict[str, float], bands: dict[str, dict[str, Any]], note: str | None) -> dict[str, Any]:
    if not bands:
        return {"status": "not_configured"}
    checks = {key: band_check(values[key], band, BAND_METRICS[key]) for key, band in bands.items()}
    return {
        "status": "measured",
        "severity": "advisory",
        "source": note or "设定/门禁配置.json → prose_bands",
        "off_band": [item["label"] for item in checks.values() if item["verdict"] != "within"],
        "checks": checks,
    }


def measure(path: Path, bands: dict[str, dict[str, Any]] | None = None, band_note: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"正文文件不存在: {path}")
    text = path.read_text(encoding="utf-8")
    paragraphs = normalized_lines(text)
    sentences = split_sentences(paragraphs)
    sentence_lengths = [chinese_count(sentence) for sentence in sentences if chinese_count(sentence) > 0]
    paragraph_lengths = [chinese_count(paragraph) for paragraph in paragraphs if chinese_count(paragraph) > 0]
    if not sentence_lengths or not paragraph_lengths:
        raise ValueError(f"正文没有可测量的中文句段: {path}")

    metrics, _, character_count = prose_metrics(path)
    short = sum(1 for length in sentence_lengths if length <= 8)
    medium = sum(1 for length in sentence_lengths if 9 <= length <= 34)
    long = sum(1 for length in sentence_lengths if length >= 35)
    sentence_count = len(sentence_lengths)
    paragraph_count = len(paragraph_lengths)

    # 对白形状：裸引语 = 整段只有引号；前置冒号 = 引导语 + 冒号 + 引号起段；短句 <15 字；短段 ≤12 字。
    quoted_paragraphs = [item for item in paragraphs if "“" in item or "「" in item]
    bare_quotes = sum(1 for item in quoted_paragraphs if QUOTE_ONLY.match(item))
    leading_colon_tags = sum(1 for item in quoted_paragraphs if LEADING_COLON_TAG.match(item))
    short_paragraphs = sum(1 for length in paragraph_lengths if length <= 12)
    short_sentences_lt15 = sum(1 for length in sentence_lengths if length < 15)
    # 叙述质地只看不含引号的段落；split_sentences 保留句末标点。
    narrative_paragraphs = [item for item in paragraphs if "“" not in item and "「" not in item]
    narrative_text = "".join(narrative_paragraphs)
    narrative_chars = max(1, chinese_count(narrative_text))
    narrative_sentences = split_sentences(narrative_paragraphs)
    narrative_sentence_total = max(1, len(narrative_sentences))
    modal_end = sum(1 for item in narrative_sentences if MODAL_END.search(item))
    second_person = sum(1 for item in narrative_sentences if SECOND_PERSON.search(item))
    narr_exclaim = sum(1 for item in narrative_sentences if item.endswith("！"))
    narr_question = sum(1 for item in narrative_sentences if item.endswith("？"))
    function_words = sum(narrative_text.count(word) for word in FUNCTION_WORDS)
    particles = len(PARTICLE_PATTERN.findall(narrative_text))
    degree_adverbs = sum("".join(paragraphs).count(word) for word in DEGREE_ADVERBS)
    sentence_cv = (
        rounded(statistics.pstdev(sentence_lengths) / statistics.fmean(sentence_lengths))
        if len(sentence_lengths) > 1 else 0.0
    )
    band_values = {
        "paragraph_mean_chars": metrics["paragraph_mean_chars"],
        "sentence_mean_chars": metrics["sentence_mean_chars"],
        "short_sentence_rate_pct": percentage(short_sentences_lt15, sentence_count),
        "bare_quote_rate_pct": percentage(bare_quotes, len(quoted_paragraphs)),
        "leading_colon_tag_count": leading_colon_tags,
        "short_paragraph_rate_pct": percentage(short_paragraphs, paragraph_count),
        "function_word_per_kilo": rounded(function_words * 1000 / narrative_chars),
        "particle_per_kilo": rounded(particles * 1000 / narrative_chars),
        "narr_modal_end_pct": percentage(modal_end, narrative_sentence_total),
        "narr_second_person_pct": percentage(second_person, narrative_sentence_total),
        "narr_exclaim_pct": percentage(narr_exclaim, narrative_sentence_total),
        "narr_question_pct": percentage(narr_question, narrative_sentence_total),
        "degree_adverb_per_kilo": rounded(degree_adverbs * 1000 / character_count),
        "sentence_cv": sentence_cv,
    }
    return {
        "status": "measured",
        "file": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "character_count": character_count,
        "sentence_count": sentence_count,
        "paragraph_count": paragraph_count,
        "bucket_contract": {"short": "1-8字", "medium": "9-34字", "long": "35字及以上"},
        "sentence_distribution": {
            "short": {"count": short, "rate_pct": percentage(short, sentence_count)},
            "medium": {"count": medium, "rate_pct": percentage(medium, sentence_count)},
            "long": {"count": long, "rate_pct": percentage(long, sentence_count)},
        },
        "sentence_mean_chars": metrics["sentence_mean_chars"],
        "sentence_median_chars": metrics["sentence_median_chars"],
        "paragraph_mean_chars": metrics["paragraph_mean_chars"],
        "paragraph_median_chars": metrics["paragraph_median_chars"],
        "sentences_per_paragraph": rounded(sentence_count / paragraph_count),
        "dialogue_shape": {
            "quoted_paragraph_count": len(quoted_paragraphs),
            "bare_quote_count": bare_quotes,
            "leading_colon_tag_count": leading_colon_tags,
            "short_paragraph_count_le12": short_paragraphs,
        },
        "texture": {
            "narrative_sentence_count": len(narrative_sentences),
            "narrative_chars": narrative_chars,
            "function_word_count": function_words,
            "particle_count": particles,
            "degree_adverb_count": degree_adverbs,
        },
        "band_values": band_values,
        "style_band": style_band(band_values, bands or {}, band_note),
        "instruction": (
            "只报告实测值；这些统计用于定位复核，不是跨题材质量配额。"
            "style_band 是本书自己标定的文风带 advisory 对照：超带写进候选报告并做语义复核，"
            "不阻断接纳，也不授权机械改文。"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="Chinese prose files")
    parser.add_argument("--gate-config", help="书的 设定/门禁配置.json；读其中 prose_bands 做 advisory 文风带对照")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results: list[dict[str, Any]] = []
    try:
        bands: dict[str, dict[str, Any]] = {}
        band_note = None
        if args.gate_config:
            config_path = Path(args.gate_config).expanduser().resolve()
            if not config_path.is_file():
                raise ValueError(f"门禁配置不存在: {config_path}")
            bands, band_note = load_bands(config_path)
        for raw in args.files:
            results.append(measure(Path(raw).expanduser().resolve(), bands, band_note))
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    payload: dict[str, Any] = results[0] if len(results) == 1 else {"status": "measured", "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
