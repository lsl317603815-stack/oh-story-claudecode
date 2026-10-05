#!/usr/bin/env python3
"""按 manifest 单元跑全部检测器，出分层／子层／题材池／写手族／书级汇总。

用法：
    "$PYBIN" measure.py <manifest.json> --corpus-tools <语料池工具目录> [--layers H A0g ...] [--split all|train|holdout]
                       [--rules L01 D12 ...] [--out stats.json] [--units-out units.jsonl] [--jobs N]

- 默认排除 dup_of、missing、U 层与 A0g_ctx（A0g 旧稿改层）；--layers 显式点名才纳入（可写层名 H/A/M 或子层名）。
- manifest 条目增减、来源文件暂缺都容忍：缺文件的单元跳过并记在 skipped 里，不中断。
- count 类规则每个汇总组报：n、n_narr（引号外命中）、per_1000_han、per_100_paragraphs、per_100_sentences、
  chapters_hit_pct（≥1 处命中的章占比）、per_100_chapters；primary 指明候选表登记的主分母。
  组内是合并口径（总命中 ÷ 总分母）。metric 类规则报章均值、章中位数。
- H 层另出 H_book_equal：先算每本书的率，再对 6 本书等权平均（H01 占 H 层 55%，合并口径会被它主导）。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import importlib
import os

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import detectors as D  # noqa: E402

# 语料池的取文工具（build_corpus.py：manifest 条目 → 原文片段）在语料目录里，不入仓。
# 用 --corpus-tools 或环境变量 DESLOP_CORPUS_TOOLS 指向它所在目录。
B = None


def load_corpus_tools(tools_dir: str | os.PathLike | None) -> None:
    global B
    if B is not None:
        return
    if not tools_dir:
        raise SystemExit("需要 --corpus-tools <含 build_corpus.py 的目录> 或环境变量 DESLOP_CORPUS_TOOLS")
    sys.path.append(str(Path(tools_dir).expanduser()))
    B = importlib.import_module("build_corpus")

DEFAULT_EXCLUDE_SUBLAYERS = {"U", "A0g_ctx"}
CANDIDATES = json.loads((HERE / "rules_candidates.json").read_text(encoding="utf-8"))["candidates"]
CAND = {c["id"]: c for c in CANDIDATES}
SCALE = {"per_1000_han": ("han", 1000), "per_100_paragraphs": ("paragraphs", 100),
         "per_100_sentences": ("sentences", 100), "per_100_chapters": ("chapters", 100)}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def select(entries: list[dict[str, Any]], layers: list[str] | None, split: str) -> list[dict[str, Any]]:
    out = []
    for e in entries:
        if e.get("missing") or e.get("dup_of"):
            continue
        if layers:
            if e["layer"] not in layers and e["sublayer"] not in layers:
                continue
        elif e["sublayer"] in DEFAULT_EXCLUDE_SUBLAYERS or e["layer"] == "U":
            continue
        if split == "train" and e.get("holdout"):
            continue
        if split == "holdout" and not e.get("holdout"):
            continue
        out.append(e)
    return out


def measure_unit(args: tuple[dict[str, Any], list[str], str]) -> dict[str, Any]:
    e, rules, tools_dir = args
    load_corpus_tools(tools_dir)
    base = {k: e.get(k) for k in ("id", "layer", "sublayer", "genre_pool", "book", "book_code", "project", "family",
                                  "chapter", "holdout", "split_group")}
    try:
        paras = B.prose_lines(B.load_text(e))
    except (FileNotFoundError, OSError) as exc:
        return {**base, "skipped": f"{type(exc).__name__}: {exc}"}
    base.update(D.unit_counts(paras))
    res: dict[str, Any] = {}
    for rid in rules:
        hits = D.REGISTRY[rid](paras)
        if CAND[rid]["kind"] == "metric":
            res[rid] = {"value": hits[0]["value"]}
        else:
            res[rid] = {"n": len(hits), "n_narr": sum(1 for h in hits if not h.get("in_dialogue"))}
    base["rules"] = res
    return base


def aggregate(units: list[dict[str, Any]], rules: list[str]) -> dict[str, Any]:
    tot = {"units": len(units), "han": sum(u["han"] for u in units),
           "paragraphs": sum(u["paragraphs"] for u in units), "sentences": sum(u["sentences"] for u in units)}
    tot["chapters"] = tot["units"]
    out: dict[str, Any] = {"totals": tot, "rules": {}}
    for rid in rules:
        c = CAND[rid]
        if c["kind"] == "metric":
            vals = [u["rules"][rid]["value"] for u in units]
            out["rules"][rid] = {"kind": "metric", "mean": round(statistics.fmean(vals), 4) if vals else None,
                                 "median": round(statistics.median(vals), 4) if vals else None}
            continue
        n = sum(u["rules"][rid]["n"] for u in units)
        nn = sum(u["rules"][rid]["n_narr"] for u in units)
        r = {"kind": "count", "n": n, "n_narr": nn, "primary": c["denominator"]}
        for den, (field, k) in SCALE.items():
            r[den] = round(n * k / tot[field], 4) if tot[field] else None
        r["narr_per_1000_han"] = round(nn * 1000 / tot["han"], 4) if tot["han"] else None
        r["chapters_hit_pct"] = round(sum(1 for u in units if u["rules"][rid]["n"]) * 100 / len(units), 2) if units else None
        out["rules"][rid] = r
    return out


def book_equal(groups: dict[str, dict[str, Any]], rules: list[str]) -> dict[str, Any]:
    """对若干书级汇总等权平均：count 类各分母取书均值；metric 取书均值。"""
    books = list(groups.values())
    out: dict[str, Any] = {"books": sorted(groups), "totals": {"han": sum(b["totals"]["han"] for b in books),
                                                                "units": sum(b["totals"]["units"] for b in books)}, "rules": {}}
    for rid in rules:
        rs = [b["rules"][rid] for b in books]
        if CAND[rid]["kind"] == "metric":
            out["rules"][rid] = {"kind": "metric", "mean": round(statistics.fmean(r["mean"] for r in rs), 4),
                                 "book_min": min(r["mean"] for r in rs), "book_max": max(r["mean"] for r in rs)}
        else:
            r = {"kind": "count", "primary": CAND[rid]["denominator"]}
            for den in list(SCALE) + ["narr_per_1000_han", "chapters_hit_pct"]:
                vals = [x[den] for x in rs if x.get(den) is not None]
                r[den] = round(statistics.fmean(vals), 4) if vals else None
            prim = [x[CAND[rid]["denominator"]] for x in rs]
            r["book_min"], r["book_max"] = min(prim), max(prim)
            out["rules"][rid] = r
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--layers", nargs="*", help="层名或子层名；不给则全部可计入条目（排除 U、A0g_ctx）")
    ap.add_argument("--split", choices=("all", "train", "holdout"), default="all")
    ap.add_argument("--rules", nargs="*", help="只跑这些规则 id；默认全部 surface 检测器")
    ap.add_argument("--out", type=Path, help="汇总 JSON；不给只打印摘要")
    ap.add_argument("--units-out", type=Path, help="逐单元结果 JSONL")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--corpus-tools", default=os.environ.get("DESLOP_CORPUS_TOOLS"),
                    help="含 build_corpus.py 的语料池工具目录；缺省读环境变量 DESLOP_CORPUS_TOOLS")
    args = ap.parse_args(argv)
    load_corpus_tools(args.corpus_tools)

    entries = json.loads(args.manifest.read_text(encoding="utf-8"))
    rules = args.rules or sorted(D.REGISTRY)
    unknown = [r for r in rules if r not in D.REGISTRY or r not in CAND]
    if unknown:
        ap.error(f"未知或无检测器的规则：{unknown}")
    chosen = select(entries, args.layers, args.split)
    work = [(e, rules, str(args.corpus_tools)) for e in chosen]
    if args.jobs > 1:
        with ProcessPoolExecutor(args.jobs) as pool:
            results = list(pool.map(measure_unit, work, chunksize=8))
    else:
        results = [measure_unit(w) for w in work]
    units = [u for u in results if "skipped" not in u]
    skipped = [{"id": u["id"], "reason": u["skipped"]} for u in results if "skipped" in u]

    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for u in units:
        groups["by_layer"][u["layer"]].append(u)
        groups["by_sublayer"][u["sublayer"]].append(u)
        groups["by_pool_sublayer"][f"{u['genre_pool']}|{u['sublayer']}"].append(u)
        if u["layer"] == "H":
            groups["by_h_book"][f"{u.get('book_code')}|{u.get('book')}"].append(u)
        else:
            groups["by_project_sublayer"][f"{u.get('project')}|{u['sublayer']}"].append(u)
        if u.get("family"):
            groups["by_family"][f"{u['sublayer']}|{u['family']}"].append(u)
    summary: dict[str, Any] = {k: {g: aggregate(us, rules) for g, us in sorted(v.items())} for k, v in groups.items()}
    if summary.get("by_h_book"):
        summary["H_book_equal"] = book_equal(summary["by_h_book"], rules)
        pools: dict[str, dict[str, Any]] = defaultdict(dict)
        for u in units:
            if u["layer"] == "H":
                pools[u["genre_pool"]][f"{u.get('book_code')}|{u.get('book')}"] = summary["by_h_book"][f"{u.get('book_code')}|{u.get('book')}"]
        summary["by_pool_H_book_equal"] = {p: book_equal(bs, rules) for p, bs in sorted(pools.items())}

    doc = {"generated_at": now(), "manifest": str(args.manifest.resolve()), "layers": args.layers or "default",
           "excluded_by_default": sorted(DEFAULT_EXCLUDE_SUBLAYERS) + ["dup_of", "missing"], "split": args.split,
           "rules": rules, "units_measured": len(units), "skipped": skipped,
           "notes": ["count 类组内为合并口径（总命中÷总分母）；H_book_equal 为书级率等权平均",
                     "metric 类为章均值／章中位数，不按字加权",
                     "n_narr＝命中起点不在引号内；narr_per_1000_han 的分母仍是全部汉字"],
           **summary}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.units_out:
        args.units_out.parent.mkdir(parents=True, exist_ok=True)
        with args.units_out.open("w", encoding="utf-8") as f:
            for u in units:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")

    print(f"measured {len(units)} units, skipped {len(skipped)}; rules {len(rules)}")
    for key in ("by_sublayer", "by_pool_sublayer", "by_family"):
        print(f"== {key}")
        for g, a in summary.get(key, {}).items():
            t = a["totals"]
            print(f"  {g:28s} {t['units']:5d} 章 {t['han']:>10,d} 字 {t['paragraphs']:>7d} 段 {t['sentences']:>7d} 句")
    if "H_book_equal" in summary:
        print(f"== H_book_equal: {len(summary['H_book_equal']['books'])} 本书等权")
    return 0


if __name__ == "__main__":
    sys.exit(main())
