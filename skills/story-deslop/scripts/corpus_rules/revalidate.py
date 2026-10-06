#!/usr/bin/env python3
"""去 AI 味规则表重验证（网文线滚动维护）：重跑 measure 与 P2 统计，对比当前规则表，出差异报告。

设计正本 §五「重验证」：全量重算 R，出差异报告（新增、淘汰、档位变动、阈值漂移），作者拍板后规则表与阈值文件才升版本号。
本脚本默认只读规则表；`--apply --version <新版本号>` 由作者显式传入时才写回
references/pattern-contracts.json 的 surface_rules 与 references/deslop-thresholds.default.json。

用法：
    "$PYBIN" revalidate.py [--pool <语料池目录>] [--corpus-tools <含 build_corpus.py 的目录>] [--out-dir <报告目录>]
                           [--units <已有 measure_units.jsonl，跳过 measure>] [--skip-words] [--jobs 8]
    "$PYBIN" revalidate.py ... --apply --version v2-2026-11-01      # 作者拍板后才用

- 语料池缺省读环境变量 DESLOP_CORPUS_POOL，再缺省 ~/Documents/小说/_去AI味语料/网文；工具目录缺省为池旁 ../_工具。
- 报告写 <out-dir>/revalidate-report.md 与 revalidate-diff.json；out-dir 缺省 <池>/_revalidate/<UTC 时间戳>/。
- 判据、种子、bootstrap 次数与 p2_stats.py 一致（规则表 v1 的口径），所以语料未变时应报「无变动」。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REFS = HERE.parents[1] / "references"
sys.path.insert(0, str(HERE))
import p2_stats as S  # noqa: E402

DEFAULT_POOL = Path(os.environ.get("DESLOP_CORPUS_POOL", "~/Documents/小说/_去AI味语料/网文")).expanduser()
STATUS_OF_TIER = {"纳入": "included", "条件纳入": "conditional", "淘汰": "retired", "待定": "pending"}
STATUS_LABEL = {"included": "纳入", "conditional": "条件纳入", "retired": "已退役", "pending": "待定", "candidate": "候选"}
RANK = {"retired": 0, "pending": 1, "candidate": 1, "conditional": 2, "included": 3}
EXTRA_THRESHOLD_RULES = {"L23": "仅 claude／豆包写手族的族别提示", "W01": None}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def num(x: Any) -> float | None:
    if x is None:
        return None
    if x == "inf":
        return math.inf
    return float(x)


def fmt(x: Any, nd: int = 2) -> str:
    v = num(x)
    if v is None:
        return "—"
    return "∞" if math.isinf(v) else f"{v:.{nd}f}"


def rel_change(old: Any, new: Any) -> float | None:
    o, n = num(old), num(new)
    if o is None or n is None:
        return None if o == n else math.inf
    if math.isinf(o) or math.isinf(n):
        return 0.0 if o == n else math.inf
    if o == 0:
        return 0.0 if n == 0 else math.inf
    return abs(n - o) / abs(o)


# ───────────────────────── 重算 ─────────────────────────
def run_measure(manifest: Path, tools: Path, out: Path, jobs: int) -> Path:
    units = out / "measure_units.jsonl"
    cmd = [sys.executable, str(HERE / "measure.py"), str(manifest), "--corpus-tools", str(tools),
           "--units-out", str(units), "--out", str(out / "measure_stats.json"), "--jobs", str(jobs)]
    done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"measure.py 失败（退出 {done.returncode}）：{done.stderr.strip()[-500:]}")
    return units


def run_stats(units: Path, out: Path, args: argparse.Namespace, manifest: Path, tools: Path) -> None:
    argv = ["--units", str(units), "--out-dir", str(out), "--boot", str(args.boot), "--seed", str(args.seed)]
    argv += ["--skip-words"] if args.skip_words else ["--manifest", str(manifest), "--corpus-tools", str(tools)]
    S.main(argv)


def h_pool_bounds(units: list[dict[str, Any]], rid: str) -> dict[str, dict[str, float]]:
    """build_p3_tables.extra_pool_bounds 同口径：池内书等权，均值＝各书合并率平均，上限＝各书章级 P90 平均。"""
    pools: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for u in units:
        if u["sublayer"] == "H":
            pools[u["genre_pool"]][u["book_code"]].append(u)
    out = {}
    for pool, books in sorted(pools.items()):
        means = [S.acc_of(us, [rid]).rate(rid) for us in books.values()]
        p90s = [S.pctl([S.unit_rate(u, rid) for u in us], 0.9) for us in books.values()]
        out[pool] = {"human_mean": statistics.fmean(means), "chapter_p90": statistics.fmean(p90s)}
    return out


# ───────────────────────── 对比 ─────────────────────────
def compare(contracts: dict, defaults: dict, out: Path, units: list[dict[str, Any]], r_tol: float, th_tol: float,
            manifest: Path) -> dict[str, Any]:
    stats = json.loads((out / "p2_rule_stats.json").read_text(encoding="utf-8"))["rules"]
    draft = json.loads((out / "rules_v1_draft.json").read_text(encoding="utf-8"))
    thr = json.loads((out / "thresholds_draft.json").read_text(encoding="utf-8"))
    old_rules = {r["id"]: r for r in contracts["surface_rules"]}
    deficit_new = {r["id"]: r for r in draft["reverse_indicators"] if r["direction"] == "AI 偏少"}

    diff: dict[str, list] = {k: [] for k in ("added", "retired", "tier_changed", "r_drift", "deficit_changed",
                                              "threshold_drift", "threshold_added", "threshold_removed",
                                              "candidates", "known")}
    for rid, s in sorted(stats.items()):
        new_status = STATUS_OF_TIER[s["tier"]]
        row = {"id": rid, "name": s["name"], "new_tier": s["tier"], "new_R": s["R"], "new_R_ci90": s["R_ci90"],
               "new_family_R": s["family_R"], "new_holdout_R": s["holdout_R"], "notes": s["notes"]}
        old = old_rules.get(rid)
        if old is None:
            diff["added"].append({**row, "old_status": None, "old_R": None, "new_status": new_status})
            continue
        row.update(old_status=old["status"], old_R=old.get("R"), new_status=new_status)
        if old["status"] == "candidate":
            # 逐词候选：v1 的 R 取 P2 禁用词逐词表（A0g 合并率），本轮取检测器（三族等权）；转正与否由作者定，不算档位变动。
            diff["candidates"].append(row)
            if old.get("R_ci90") is None and rel_change(old.get("R"), s["R"]) not in (None, 0.0):
                diff["known"].append(f"{rid} {s['name']}：v1 的 R {fmt(old.get('R'))} 取自 P2 禁用词逐词表（A0g 三族合并率、未做区间），"
                                     f"本轮按检测器三族等权得 {fmt(s['R'])}（90% 区间 {fmt(s['R_ci90'][0])}–{fmt(s['R_ci90'][1])}）；口径差，不是语料变化")
            continue
        if new_status != old["status"]:
            key = "retired" if new_status == "retired" else "added" if RANK[new_status] > RANK[old["status"]] and new_status in ("included", "conditional") else "tier_changed"
            diff[key].append(row)
        elif (rc := rel_change(old.get("R"), s["R"])) is not None and rc > r_tol:
            diff["r_drift"].append({**row, "rel_change": rc})
        had = "deficit" in old
        has = rid in deficit_new
        if had != has or (had and has and old["deficit"].get("strength") != deficit_new[rid]["strength"]):
            diff["deficit_changed"].append({"id": rid, "name": s["name"], "old": old.get("deficit", {}).get("strength"),
                                            "new": deficit_new.get(rid, {}).get("strength"), "old_R": old.get("R"), "new_R": s["R"]})
    for rid in sorted(set(old_rules) - set(stats)):
        diff["tier_changed"].append({"id": rid, "name": old_rules[rid]["name"], "old_status": old_rules[rid]["status"],
                                     "new_status": None, "old_R": old_rules[rid].get("R"), "new_R": None,
                                     "notes": ["本轮统计没有这条规则（检测器或候选表已删）"]})

    # 阈值：新草案 ＋ L23／W01 按同口径补算，对比仓内缺省表
    new_pools: dict[str, dict[str, dict]] = {p: dict(v["rules"]) for p, v in thr["pools"].items()}
    for rid in EXTRA_THRESHOLD_RULES:
        if any(rid in v for v in new_pools.values()):
            continue
        for pool, b in h_pool_bounds(units, rid).items():
            new_pools.setdefault(pool, {})[rid] = b
    for pool in sorted(set(new_pools) | set(defaults.get("pools", {}))):
        old_r = defaults.get("pools", {}).get(pool, {}).get("rules", {})
        new_r = new_pools.get(pool, {})
        for rid in sorted(set(old_r) | set(new_r)):
            if rid not in new_r:
                diff["threshold_removed"].append({"pool": pool, "id": rid})
                continue
            if rid not in old_r:
                diff["threshold_added"].append({"pool": pool, "id": rid, **{k: new_r[rid].get(k) for k in ("human_mean", "chapter_p90", "chapter_p10")}})
                continue
            for k in ("human_mean", "chapter_p90", "chapter_p10"):
                if k in old_r[rid] or k in new_r[rid]:
                    rc = rel_change(old_r[rid].get(k), new_r[rid].get(k))
                    if rc is not None and rc > th_tol:
                        diff["threshold_drift"].append({"pool": pool, "id": rid, "field": k, "old": old_r[rid].get(k),
                                                        "new": new_r[rid].get(k), "rel_change": rc})

    # 语料口径与已知差异
    corpus = contracts.get("rule_table", {}).get("corpus", {})
    base = draft["corpus"]
    same_basis = all(f"{base[k]:,}" in corpus.get(lbl, "") for k, lbl in
                     (("A0g_train_han", "A0g"), ("A0g_holdout_han", "A0g"), ("H_train_han", "H"), ("H_holdout_han", "H")))
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    ctx = sum(1 for e in entries if e.get("sublayer") == "A0g_ctx" and not e.get("missing"))
    if ctx:
        diff["known"].append(f"A0g_ctx 旁存层 {ctx} 篇按 v1 口径排除（派工上下文污染的旧 claude 稿），不进 R")
    old_fp = corpus.get("sha256_16", {})
    new_fp = {"manifest.json": sha16(manifest), "measure_units.jsonl": sha16(out / "measure_units.jsonl")}
    if old_fp.get("manifest.json") != new_fp["manifest.json"]:
        note = ("R 基线语料（A0g 训练 {A0g_train_han:,}／留出 {A0g_holdout_han:,}；H 训练 {H_train_han:,}／留出 {H_holdout_han:,} 汉字）与 v1 相同"
                .format(**base) if same_basis else "R 基线语料（A0g 或 H）字数与 v1 不同")
        diff["known"].append(f"manifest 指纹变了（{old_fp.get('manifest.json')} → {new_fp['manifest.json']}）：接纳追加的 A1／A2／M 只进执行配对表，不进 R；{note}")
    changed = any(diff[k] for k in ("added", "retired", "tier_changed", "r_drift", "deficit_changed",
                                     "threshold_drift", "threshold_added", "threshold_removed"))
    return {"generated_at": now(), "rule_table_version": contracts.get("rule_table", {}).get("version"),
            "corpus_basis_same": same_basis, "fingerprint_old": old_fp, "fingerprint_new": new_fp,
            "r_tol": r_tol, "threshold_tol": th_tol, "changed": changed, "diff": diff, "new_pools": new_pools}


def render(rep: dict[str, Any], pool: Path, out: Path) -> str:
    d = rep["diff"]
    L = ["# 去 AI 味规则表重验证报告（网文线）", "",
         f"- 时间：{rep['generated_at']}；语料池：`{pool}`；报告目录：`{out}`",
         f"- 对比对象：规则表 `{rep['rule_table_version']}`（references/pattern-contracts.json）与缺省阈值表",
         f"- 判据同规则表 v1（p2_stats.py：R＝A0g 三族等权 ÷ H 书等权，种子与 bootstrap 次数不变）；R 漂移阈 {rep['r_tol']:.0%}，阈值漂移阈 {rep['threshold_tol']:.0%}",
         f"- 结论：**{'有变动，待作者拍板' if rep['changed'] else '无变动'}**。本报告不改规则表；作者拍板后才用 `--apply --version <新版本号>` 写回。", ""]

    def table(title: str, rows: list, head: str, fmt_row) -> None:
        L.append(f"## {title}（{len(rows)}）")
        L.append("")
        if not rows:
            L.append("无。")
        else:
            L.append(head)
            L.append("|" + "---|" * (head.count("|") - 1))
            L.extend(fmt_row(r) for r in rows)
        L.append("")

    rule_head = "| id | 名称 | 旧状态 | 新状态 | 旧 R | 新 R（90% 区间） | 新族 R | 备注 |"

    def rule_row(r: dict) -> str:
        ci = r.get("new_R_ci90") or [None, None]
        fam = "/".join(fmt(v) for v in (r.get("new_family_R") or {}).values()) or "—"
        return (f"| {r['id']} | {r['name']} | {STATUS_LABEL.get(r.get('old_status'), '—')} | {STATUS_LABEL.get(r.get('new_status'), '—')} | "
                f"{fmt(r.get('old_R'))} | {fmt(r.get('new_R'))}（{fmt(ci[0])}–{fmt(ci[1])}） | {fam} | {'；'.join(r.get('notes') or [])} |")

    table("新增（新规则，或升入纳入／条件纳入）", d["added"], rule_head, rule_row)
    table("淘汰（转为已退役）", d["retired"], rule_head, rule_row)
    table("其他档位变动", d["tier_changed"], rule_head, rule_row)
    table("R 漂移（档位不变）", d["r_drift"], rule_head, rule_row)
    table("赤字指标变动（AI 偏少的反向指标）", d["deficit_changed"], "| id | 名称 | 旧强度 | 新强度 | 旧 R | 新 R |",
          lambda r: f"| {r['id']} | {r['name']} | {r['old'] or '—'} | {r['new'] or '—'} | {fmt(r['old_R'])} | {fmt(r['new_R'])} |")
    table("阈值漂移（题材池真人章级界）", d["threshold_drift"], "| 池 | id | 字段 | 旧 | 新 | 相对变动 |",
          lambda r: f"| {r['pool']} | {r['id']} | {r['field']} | {fmt(r['old'], 4)} | {fmt(r['new'], 4)} | {r['rel_change']:.1%} |")
    table("阈值新增", d["threshold_added"], "| 池 | id | 真人均值 | 章级界 |",
          lambda r: f"| {r['pool']} | {r['id']} | {fmt(r.get('human_mean'), 4)} | {fmt(r.get('chapter_p90') if r.get('chapter_p90') is not None else r.get('chapter_p10'), 4)} |")
    table("阈值移除", d["threshold_removed"], "| 池 | id |", lambda r: f"| {r['pool']} | {r['id']} |")
    table("候选复验（不计入变动，转正由作者定）", d["candidates"], rule_head, rule_row)
    L.append(f"## 已知差异（{len(d['known'])}）")
    L.append("")
    L.extend([f"- {k}" for k in d["known"]] or ["无。"])
    L.append("")
    L.append("重算产物（p2_rule_stats.json、thresholds_draft.json、p2_tables.md 等）与本报告同目录。")
    return "\n".join(L) + "\n"


# ───────────────────────── 写回（作者显式 --apply） ─────────────────────────
def apply(contracts_path: Path, defaults_path: Path, out: Path, rep: dict[str, Any], version: str) -> None:
    contracts = json.loads(contracts_path.read_text(encoding="utf-8"))
    defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
    old_version = contracts["rule_table"]["version"]
    if version == old_version:
        raise SystemExit(f"--version 必须是新版本号（当前 {old_version}）")
    stats = json.loads((out / "p2_rule_stats.json").read_text(encoding="utf-8"))["rules"]
    draft = json.loads((out / "rules_v1_draft.json").read_text(encoding="utf-8"))
    deficit_new = {r["id"]: r for r in draft["reverse_indicators"] if r["direction"] == "AI 偏少"}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fp = f"{version}:manifest-{rep['fingerprint_new']['manifest.json']}:units-{rep['fingerprint_new']['measure_units.jsonl']}"
    rows = {r["id"]: r for r in contracts["surface_rules"]}
    for rid, s in stats.items():
        row = rows.setdefault(rid, {"id": rid, "name": s["name"], "blocking": False, "kind": s["kind"],
                                    "denominator": s["denominator"], "source": s.get("source"),
                                    "corpus_rules_detector": f"detect_{rid}"})
        row.update(version=version, R=s["R"], R_ci90=s["R_ci90"], family_R=s["family_R"], holdout_R=s["holdout_R"],
                   A0_R=s["A0_R"], tier_notes=s["notes"], corpus_fingerprint=fp)
        if row.get("status") == "candidate":
            continue
        status = STATUS_OF_TIER[s["tier"]]
        if status != row.get("status"):
            row["status"] = status
            row["counts_toward_issue_density"] = status in ("pending", "conditional", "included")
            if row.get("action") != "family_hint" or status != "retired":
                row["action"] = {"included": "threshold_hint", "conditional": "threshold_hint", "retired": "off", "pending": "advisory"}[status]
            row.pop("threshold", None)
            if status == "retired":
                row["retired"] = {"on": today, "R": s["R"], "reason": "；".join(s["notes"]) or "无区分力"}
            else:
                row.pop("retired", None)
                row["threshold"] = {"bound": "chapter_p90", "message": "超过所在题材池章级 P90 才提示；不禁用、不自动改"
                                    if status != "pending" else "量不足：维持现状但降为 advisory，只列命中位置不做阈值告警"}
        if rid in deficit_new:
            rv = deficit_new[rid]
            row["deficit"] = {**row.get("deficit", {}), "status": "preregistered_deficit", "action": "deficit_floor", "R": rv["R"],
                              "strength": rv["strength"], "bound": "chapter_p10",
                              "registered_on": row.get("deficit", {}).get("registered_on", today),
                              "use": row.get("deficit", {}).get("use", "低于所在题材池真人章级 P10 时 advisory 告警；不改稿、不自动加字")}
        else:
            row.pop("deficit", None)
    contracts["surface_rules"] = S.clean(sorted(rows.values(), key=lambda r: (r["id"] == "W01", r["id"])))
    rt = contracts["rule_table"]
    history = rt.setdefault("history", [])
    history.append({"version": old_version, "fingerprint": rt.get("corpus", {}).get("fingerprint"), "superseded_on": today})
    rt["version"] = version
    rt["decided"] = today
    rt["corpus"] = {**rt.get("corpus", {}), "fingerprint": fp,
                    "sha256_16": {**rep["fingerprint_new"], "p2_rule_stats.json": sha16(out / "p2_rule_stats.json"),
                                  "thresholds_draft.json": sha16(out / "thresholds_draft.json")}}
    rt["regenerate"] = "skills/story-deslop/scripts/corpus_rules/revalidate.py --apply --version <新版本号>；重验证与升版由作者拍板"
    rt.setdefault("status_semantics", {}).setdefault("included", "纳入：超过题材池章级 P90 提示；不自动改写（决定 9）")
    contracts_path.write_text(json.dumps(contracts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    thr = json.loads((out / "thresholds_draft.json").read_text(encoding="utf-8"))
    for pool, entry in thr["pools"].items():
        tgt = defaults["pools"].setdefault(pool, {})
        for k in ("books", "chapters", "han", "han_by_book", "warning"):
            if k in entry:
                tgt[k] = entry[k]
        rules = dict(entry["rules"])
        for rid, b in rep["new_pools"].get(pool, {}).items():
            if rid in EXTRA_THRESHOLD_RULES and rid not in rules:
                old = tgt.get("rules", {}).get(rid, {})
                rules[rid] = {**old, **b}
        tgt["rules"] = S.clean(rules)
    defaults["version"] = version
    defaults["corpus_fingerprint"] = fp
    defaults_path.write_text(json.dumps(defaults, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写回规则表 {old_version} → {version}：{contracts_path}、{defaults_path}")
    print("下一步：在仓根运行 \"$PYBIN\" scripts/sync-shared-assets.py sync 同步 long-write／review／short-write 副本，并跑 test-deslop-corpus-rules.py")


# ───────────────────────── 入口 ─────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pool", type=Path, default=DEFAULT_POOL, help="语料池目录（含 manifest.json）")
    ap.add_argument("--corpus-tools", type=Path, help="含 build_corpus.py 的目录；缺省 DESLOP_CORPUS_TOOLS 或池旁 ../_工具")
    ap.add_argument("--out-dir", type=Path, help="报告与重算产物目录；缺省 <池>/_revalidate/<UTC 时间戳>")
    ap.add_argument("--units", type=Path, help="复用已有 measure_units.jsonl，跳过 measure")
    ap.add_argument("--contracts", type=Path, default=REFS / "pattern-contracts.json")
    ap.add_argument("--thresholds", type=Path, default=REFS / "deslop-thresholds.default.json")
    ap.add_argument("--boot", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20261006)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--skip-words", action="store_true", help="不重算禁用词逐词表（较慢）")
    ap.add_argument("--r-tol", type=float, default=0.05, help="档位不变时 R 相对变动超过此值才报漂移")
    ap.add_argument("--threshold-tol", type=float, default=0.05, help="阈值相对变动超过此值才报漂移")
    ap.add_argument("--apply", action="store_true", help="作者拍板后写回规则表与缺省阈值表（须同时给 --version）")
    ap.add_argument("--version", help="--apply 时的新规则表版本号，如 v2-2026-11-01")
    args = ap.parse_args(argv)
    if args.apply and not args.version:
        ap.error("--apply 必须同时给 --version <新版本号>")

    pool = args.pool.expanduser()
    manifest = pool / "manifest.json"
    if not manifest.is_file():
        raise SystemExit(f"语料池不存在：{manifest}")
    tools = (args.corpus_tools or Path(os.environ.get("DESLOP_CORPUS_TOOLS", pool.parent / "_工具"))).expanduser()
    out = (args.out_dir or pool / "_revalidate" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    units_path = args.units.expanduser() if args.units else run_measure(manifest, tools, out, args.jobs)
    if args.units and units_path.resolve() != (out / "measure_units.jsonl").resolve():
        (out / "measure_units.jsonl").write_bytes(units_path.read_bytes())
        units_path = out / "measure_units.jsonl"
    run_stats(units_path, out, args, manifest, tools)
    units = [json.loads(l) for l in units_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    contracts = json.loads(args.contracts.read_text(encoding="utf-8"))
    defaults = json.loads(args.thresholds.read_text(encoding="utf-8"))
    rep = compare(contracts, defaults, out, units, args.r_tol, args.threshold_tol, manifest)
    (out / "revalidate-diff.json").write_text(json.dumps(S.clean({k: v for k, v in rep.items() if k != "new_pools"}),
                                                         ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    report = out / "revalidate-report.md"
    report.write_text(render(rep, pool, out), encoding="utf-8")
    d = rep["diff"]
    print(f"重验证 {'有变动' if rep['changed'] else '无变动'}：新增 {len(d['added'])}、淘汰 {len(d['retired'])}、"
          f"档位变动 {len(d['tier_changed'])}、R 漂移 {len(d['r_drift'])}、赤字变动 {len(d['deficit_changed'])}、"
          f"阈值漂移 {len(d['threshold_drift'])}（新增 {len(d['threshold_added'])}／移除 {len(d['threshold_removed'])}）；"
          f"已知差异 {len(d['known'])} → {report}")
    if args.apply:
        apply(args.contracts, args.thresholds, out, rep, args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
