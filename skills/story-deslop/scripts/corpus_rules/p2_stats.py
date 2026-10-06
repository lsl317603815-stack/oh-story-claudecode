#!/usr/bin/env python3
"""网文线规则统计（P2 口径）：对候选规则算频率比 R（A0g ÷ H 书等权）、按组 bootstrap 区间、三族／题材池／留出集／留一本书，
出去留档位、执行与过度矫正配对表、禁用词分类与逐词命中、阈值草案与规则表草案。

规则表 v1（references/pattern-contracts.json）就是按本口径定的；revalidate.py 复用本模块重算并对比。
本文件由仓外语料池 _工具/p2_stats.py 搬入（2026-10-06 P4），判据、种子与输出口径不变，只改了路径：
检测器与候选表取本目录，语料取文工具（build_corpus.py）与 manifest 由参数给。

用法：
    "$PYBIN" p2_stats.py --units <measure_units.jsonl> --out-dir <目录> [--boot 1000] [--seed 20261006]
                         [--skip-words | --manifest <manifest.json> --corpus-tools <含 build_corpus.py 的目录>]

输入是 measure.py 的逐单元结果（--units-out）。不调外模、不改检测器。
产物（out-dir 下）：p2_rule_stats.json、p2_chain.json、p2_banned_words.json、thresholds_draft.json、rules_v1_draft.json、
p2_tables.md、p2_h_outlier.json。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
DET = HERE
sys.path.insert(0, str(DET))

CAND_DOC = json.loads((DET / "rules_candidates.json").read_text(encoding="utf-8"))
CAND = {c["id"]: c for c in CAND_DOC["candidates"]}
SCALE = {"per_1000_han": ("han", 1000), "per_100_paragraphs": ("paragraphs", 100),
         "per_100_sentences": ("sentences", 100), "per_100_chapters": ("chapters", 100)}

# 极性：+1＝假设 AI 更高；-1＝假设 AI 更低（材料密度、均匀度类离散指标）；0＝形状指标，无预设方向，按数据方向取。
POLARITY = {r: 1 for r in CAND}
POLARITY.update({"L08": -1, "L12": -1, "L13": -1, "L14": -1})
for r in ("P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08", "P09", "P10", "P11", "P12", "P19"):
    POLARITY[r] = 0

GATES = {
    "A 禁用词替换": ["D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10"],
    "B 句式去套路": ["D11", "D12", "D13", "D14", "D15", "D16", "D17", "D18", "D26", "D29"],
    "C 心理描写外化": ["D16", "D30"],
    "D 节奏调整": ["D23", "D24", "D25"],
    "E 对话去腔调": [],
    "F 结尾去升华": ["D19", "D20"],
    "G 去解释腔／上帝感": ["D21", "D22"],
    "（脚本／标记，非门禁正文）": ["D27", "D28", "P13", "P14", "P15", "P16", "P17", "P18"],
}

TIER_NAME = {3: "纳入", 2: "条件纳入", 1: "淘汰", 0: "待定"}
MATCHED_POOLS = {"都市脑洞游戏制作": ["H03"], "都市高武系统": ["H02", "H04"]}
MIN_AI_HITS = 10      # A0g 训练侧命中 <10 → 样本不足
MIN_H_HITS = 50       # H 侧命中 <50 且 AI 也少 → 近零


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ───────────────────────── 基础取数 ─────────────────────────
class Acc:
    """一组单元的累计量：每条规则的命中和、各分母和、metric 值和与章数。"""
    __slots__ = ("hits", "den", "msum", "n")

    def __init__(self) -> None:
        self.hits: dict[str, float] = defaultdict(float)
        self.den = {"han": 0, "paragraphs": 0, "sentences": 0, "chapters": 0}
        self.msum: dict[str, float] = defaultdict(float)
        self.n = 0

    def add_unit(self, u: dict[str, Any], rules: list[str]) -> "Acc":
        self.n += 1
        for k in ("han", "paragraphs", "sentences"):
            self.den[k] += u[k]
        self.den["chapters"] += 1
        for r in rules:
            v = u["rules"][r]
            if "value" in v:
                self.msum[r] += v["value"]
            else:
                self.hits[r] += v["n"]
        return self

    def add(self, o: "Acc") -> "Acc":
        self.n += o.n
        for k in self.den:
            self.den[k] += o.den[k]
        for r, v in o.hits.items():
            self.hits[r] += v
        for r, v in o.msum.items():
            self.msum[r] += v
        return self

    def rate(self, r: str) -> float | None:
        c = CAND[r]
        if c["kind"] == "metric":
            return self.msum[r] / self.n if self.n else None
        field, k = SCALE[c["denominator"]]
        d = self.den[field]
        return self.hits[r] * k / d if d else None


def acc_of(units: list[dict[str, Any]], rules: list[str]) -> Acc:
    a = Acc()
    for u in units:
        a.add_unit(u, rules)
    return a


def mean_rates(accs: list[Acc], r: str) -> float | None:
    vals = [a.rate(r) for a in accs if a.n]
    vals = [v for v in vals if v is not None]
    return statistics.fmean(vals) if vals else None


def ratio(a: float | None, h: float | None) -> float | None:
    if a is None or h is None:
        return None
    if h == 0:
        return math.inf if a > 0 else None
    return a / h


def eff(r: float | None, pol: int) -> float | None:
    """方向校正后的区分度：+1 取 R，-1 取 1/R，0 取 max(R,1/R)。"""
    if r is None:
        return None
    if r == 0:
        return math.inf if pol == -1 or pol == 0 else 0.0
    if math.isinf(r):
        return 0.0 if pol == -1 else math.inf
    if pol == 1:
        return r
    if pol == -1:
        return 1 / r
    return max(r, 1 / r)


def fmt(x: float | None, nd: int = 2) -> str:
    if x is None:
        return "—"
    if isinstance(x, str):
        return "∞" if x == "inf" else x
    if isinstance(x, float) and math.isinf(x):
        return "∞"
    return f"{x:.{nd}f}"


def poisson_cdf(k: int, mu: float) -> float:
    term = math.exp(-mu)
    tot = term
    for i in range(1, k + 1):
        term *= mu / i
        tot += term
    return tot


def poisson_hi(k: int, alpha: float = 0.05) -> float:
    """观测 k 次时泊松均值的单侧 (1-alpha) 上界（精确法，二分）。"""
    lo, hi = 0.0, k + 50.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if poisson_cdf(k, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def pctl(vals: list[float], q: float) -> float | None:
    v = sorted(x for x in vals if x is not None and not math.isnan(x))
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    pos = (len(v) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


# ───────────────────────── 主统计 ─────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--units", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, help="禁用词逐词命中要取原文：语料池 manifest.json")
    ap.add_argument("--corpus-tools", type=Path, help="禁用词逐词命中要取原文：含 build_corpus.py 的目录")
    ap.add_argument("--boot", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20261006)
    ap.add_argument("--skip-words", action="store_true", help="不重跑禁用词逐词命中（较慢）")
    args = ap.parse_args(argv)
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    units = [json.loads(l) for l in args.units.read_text(encoding="utf-8").splitlines() if l.strip()]
    rules = sorted(r for r in units[0]["rules"] if r in CAND)
    by_sub: dict[str, list] = defaultdict(list)
    for u in units:
        by_sub[u["sublayer"]].append(u)
    H = by_sub["H"]
    A0g = by_sub["A0g"]
    books = sorted({u["book_code"] for u in H})
    pool_of_book = {u["book_code"]: u["genre_pool"] for u in H}

    def h_accs(us: list, only_books: list[str] | None = None) -> list[Acc]:
        g: dict[str, list] = defaultdict(list)
        for u in us:
            if only_books is None or u["book_code"] in only_books:
                g[u["book_code"]].append(u)
        return [acc_of(v, rules) for _, v in sorted(g.items())]

    def ai_accs(us: list) -> list[Acc]:
        g: dict[str, list] = defaultdict(list)
        for u in us:
            g[u["family"]].append(u)
        return [acc_of(v, rules) for _, v in sorted(g.items())]

    H_tr = [u for u in H if not u["holdout"]]
    H_ho = [u for u in H if u["holdout"]]
    A_tr = [u for u in A0g if not u["holdout"]]
    A_ho = [u for u in A0g if u["holdout"]]
    fams = sorted({u["family"] for u in A0g})

    hb_tr = h_accs(H_tr)
    hb_ho = h_accs(H_ho)
    hb_all = h_accs(H)
    ab_tr = ai_accs(A_tr)
    ab_ho = ai_accs(A_ho)
    fam_tr = {f: acc_of([u for u in A_tr if u["family"] == f], rules) for f in fams}
    a_pool_tr = {p: acc_of([u for u in A_tr if u["genre_pool"] == p], rules) for p in MATCHED_POOLS}
    h_pool_tr = {p: h_accs(H_tr, [b for b in books if pool_of_book[b] == p]) for p in sorted(set(pool_of_book.values()))}
    hbook_tr = {b: acc_of([u for u in H_tr if u["book_code"] == b], rules) for b in books}
    A0 = acc_of(by_sub["A0"], rules)
    M_acc = {s: acc_of(by_sub[s], rules) for s in ("M", "M_auto", "M_legacy")}
    A0g_all_fam = ai_accs(A0g)
    sub_all = {s: acc_of(by_sub[s], rules) for s in ("A1", "A2", "A2g")}

    # ── bootstrap 块：AI 按章组（每组含三族），H 按书内 10 章块 ──
    ai_groups: dict[str, dict[str, Acc]] = defaultdict(dict)
    for u in A_tr:
        ai_groups[u["split_group"]].setdefault(u["family"], Acc()).add_unit(u, rules)
    ai_gkeys = sorted(ai_groups)
    h_blocks: dict[str, dict[str, Acc]] = defaultdict(dict)
    for u in H_tr:
        h_blocks[u["book_code"]].setdefault(u["split_group"], Acc()).add_unit(u, rules)
    rng = random.Random(args.seed)
    boot_ai: list[dict[str, float | None]] = []
    boot_h: list[dict[str, float | None]] = []
    for _ in range(args.boot):
        pick = [rng.choice(ai_gkeys) for _ in ai_gkeys]
        fa = {f: Acc() for f in fams}
        for g in pick:
            for f, a in ai_groups[g].items():
                fa[f].add(a)
        boot_ai.append({r: mean_rates(list(fa.values()), r) for r in rules})
        bacc = []
        for b in books:
            ks = sorted(h_blocks[b])
            a = Acc()
            for _k in ks:
                a.add(h_blocks[b][rng.choice(ks)])
            bacc.append(a)
        boot_h.append({r: mean_rates(bacc, r) for r in rules})

    stats: dict[str, Any] = {}
    for r in rules:
        c = CAND[r]
        pol = POLARITY[r]
        a_tr = mean_rates(ab_tr, r)
        h_tr = mean_rates(hb_tr, r)
        R = ratio(a_tr, h_tr)
        Rs = [ratio(ba[r], bh[r]) for ba, bh in zip(boot_ai, boot_h)]
        Rs = [x for x in Rs if x is not None]
        lo, hi = pctl([x if not math.isinf(x) else 1e9 for x in Rs], 0.05), pctl([x if not math.isinf(x) else 1e9 for x in Rs], 0.95)
        fam_R = {f: ratio(fam_tr[f].rate(r), h_tr) for f in fams}
        pool_R_matched = {p: ratio(a_pool_tr[p].rate(r), mean_rates(h_pool_tr[p], r)) for p in MATCHED_POOLS}
        pool_R_all = {p: ratio(a_tr, mean_rates(h_pool_tr[p], r)) for p in h_pool_tr}
        lobo = {b: ratio(a_tr, mean_rates([hbook_tr[x] for x in books if x != b], r)) for b in books}
        R_ho = ratio(mean_rates(ab_ho, r), mean_rates(hb_ho, r))
        R_A0 = ratio(A0.rate(r), mean_rates(hb_all, r))
        h_all = mean_rates(hb_all, r)
        a_all = mean_rates(A0g_all_fam, r)
        m_rate = M_acc["M"].rate(r)
        ai_hits = sum(a.hits[r] for a in ab_tr) if c["kind"] == "count" else None
        h_hits = sum(a.hits[r] for a in hb_tr) if c["kind"] == "count" else None

        # 方向：主判按假设极性（形状指标 pol=0 一律按「AI 偏多」判，偏少归反向指标）
        d = pol if pol != 0 else 1
        ai_den = None
        if c["kind"] == "count":
            field, k = SCALE[c["denominator"]]
            ai_den = sum(a.den[field] for a in ab_tr) / k
        expected = (h_tr * ai_den) if (ai_den is not None and h_tr is not None) else None
        low_power = ai_hits is not None and ai_hits < MIN_AI_HITS and (expected is None or expected < MIN_AI_HITS)

        def evaluate(dd: int) -> tuple[int, list[str], float | None]:
            Re = eff(R, dd)
            nts: list[str] = []
            if Re is None:
                return 0, ["无法计算（两侧皆零）"], None
            if low_power:
                if dd > 0 and expected and expected > 0:
                    r_hi = poisson_hi(int(ai_hits)) / expected
                    if r_hi < 1.25:
                        return 1, [f"量少但可排除：A0g {int(ai_hits)} 处／期望 {expected:.1f}，泊松 95% 上界 R={r_hi:.2f}<1.25"], Re
                    return 0, [f"量不足：A0g {int(ai_hits)} 处／期望 {expected:.1f}，泊松上界 R={r_hi:.2f}"], Re
                return 0, [f"量不足：A0g 训练侧 {int(ai_hits)} 处，按真人率期望 {(expected or 0):.1f} 处"], Re
            t = 3 if Re >= 2.0 else 2 if Re >= 1.25 else 1
            if t == 1:
                return 1, [], Re
            fam_e = [eff(v, dd) for v in fam_R.values()]
            pool_e = [eff(v, dd) for v in pool_R_matched.values()]
            ci = [eff(x if not (x and x >= 1e8) else math.inf, dd) for x in (lo, hi)]
            ci_lo = min(x for x in ci if x is not None) if any(x is not None for x in ci) else None
            ho_e = eff(R_ho, dd)
            if t == 3 and not all(v is not None and v >= 1 for v in fam_e):
                t = 2
                nts.append("有族方向相反，降一档")
            elif t == 2:
                if not all(v is not None and v >= 1.25 for v in fam_e):
                    return 1, ["条件档未过：三族未全≥1.25"], Re
                if not all(v is not None and v > 1 for v in pool_e):
                    return 1, ["条件档未过：两对位题材池不同向"], Re
            if ci_lo is not None and ci_lo <= 1:
                t -= 1
                nts.append("90%区间跨1，降一档")
            if t >= 2 and (ho_e is None or ho_e < 1):
                t -= 1
                nts.append("留出集反向或无数据（unstable），降一档")
            return t, nts, Re

        tier, notes, Re = evaluate(d)
        base_tier = 3 if (Re or 0) >= 2 else 2 if (Re or 0) >= 1.25 else 1
        # 原始方向描述
        sig = lo is not None and hi is not None and not (lo <= 1 <= hi)
        if R is not None and not low_power:
            if R < 0.8:
                notes.insert(0, "真人更高" + ("（区间不含1）" if sig else "（区间含1）"))
            elif R > 1.25:
                notes.insert(0, "AI 更高" + ("（区间不含1）" if sig else "（区间含1）"))
            else:
                notes.insert(0, "无区分力")
            if pol == -1 and R > 1.25:
                notes.append("与假设方向相反")
        # 反向指标：按相反方向（形状指标取 AI 偏少）用同一判据
        rev_tier, rev_notes, _ = evaluate(-d)
        reverse = None
        if rev_tier >= 2:
            reverse = {"tier": TIER_NAME[rev_tier], "direction": "AI 偏少" if -d < 0 else "AI 偏多", "notes": rev_notes}
            notes.append(f"反向指标·{reverse['direction']}（{reverse['tier']}档强度）")
        crosses = lo is not None and hi is not None and lo <= 1 <= hi
        unstable = R_ho is None or R is None or ((R_ho >= 1) != (R >= 1))
        if c.get("conflict"):
            notes.append(f"冲突组·{c.get('conflict_topic')}")
        # M 位置
        lo_ref, hi_ref = sorted([h_all or 0, a_all or 0])
        if m_rate is None or h_all is None or a_all is None:
            mpos = "—"
        elif m_rate < lo_ref:
            mpos = "低于两端"
        elif m_rate > hi_ref:
            mpos = "高于两端"
        else:
            mpos = "介于H与A0g"
        stats[r] = {
            "id": r, "name": c["name"], "kind": c["kind"], "denominator": c["denominator"], "polarity": pol,
            "direction_used": d, "conflict": c.get("conflict"), "conflict_topic": c.get("conflict_topic"),
            "lieflat": c.get("lieflat"), "source": c.get("source"),
            "A0g_train_rate": a_tr, "H_train_rate_book_equal": h_tr, "R": R, "R_ci90": [lo, hi], "R_eff": Re, "A0g_train_hits": ai_hits, "H_train_hits": h_hits,
            "family_R": fam_R, "pool_R_matched": pool_R_matched, "pool_R_vs_H_pool": pool_R_all,
            "lobo_R": lobo, "lobo_range": [min(v for v in lobo.values() if v is not None), max(v for v in lobo.values() if v is not None)] if any(v is not None for v in lobo.values()) else None,
            "holdout_R": R_ho, "holdout_unstable": unstable, "A0_R": R_A0,
            "H_all_book_equal": h_all, "A0g_all": a_all, "M_rate": m_rate, "M_auto_rate": M_acc["M_auto"].rate(r),
            "M_legacy_rate": M_acc["M_legacy"].rate(r), "M_position": mpos,
            "A1_rate": sub_all["A1"].rate(r), "A2_rate": sub_all["A2"].rate(r), "A2g_rate": sub_all["A2g"].rate(r),
            "H_book_rates_train": {b: hbook_tr[b].rate(r) for b in books},
            "base_tier": TIER_NAME[base_tier], "tier": TIER_NAME[tier], "notes": notes, "reverse": reverse,
            "expected_ai_hits_at_H_rate": expected, "low_power": low_power, "ci_crosses_1": crosses,
            "verifiable": "计数型·可逐处定位复测" if c["kind"] == "count" else "度量型·整章值复测，不能逐处定位",
        }

    # ── H 书离群：每本书在「AI 信号」规则上更像 A0g 还是更像其他书 ──
    sig_rules = [r for r, st in stats.items() if st.get("reverse") or st["tier"] in ("纳入", "条件纳入")]
    outlier: dict[str, Any] = {"rules": sig_rules, "books": {}}
    for b in books:
        closer = []
        for r in sig_rules:
            vb = hbook_tr[b].rate(r)
            others = [hbook_tr[x].rate(r) for x in books if x != b]
            med = statistics.median(others)
            va = stats[r]["A0g_train_rate"]
            if None in (vb, med, va):
                continue
            lg = lambda x: math.log(x + 1e-3)
            if abs(lg(vb) - lg(va)) < abs(lg(vb) - lg(med)):
                closer.append(r)
        outlier["books"][b] = {"closer_to_A0g": closer, "n": len(closer), "of": len(sig_rules)}
    (out / "p2_h_outlier.json").write_text(json.dumps(outlier, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── 执行与过度矫正：配对链 ──
    chain = chain_tables(units, rules, by_sub)

    # ── 阈值草案（按 H 题材池，书等权，全 H） ──
    thresholds = build_thresholds(H, rules, stats, pool_of_book)

    # ── 规则表草案 ──
    rules_draft = {
        "version": "rules-v1-draft-p2", "generated_at": now(),
        "basis": "A0g（三族×11章，训练侧 8 章组）÷ H 书等权（6 本，训练侧）；单基线加严判据见审计报告开头",
        "corpus": {"A0g_train_han": sum(a.den["han"] for a in ab_tr), "A0g_holdout_han": sum(a.den["han"] for a in ab_ho),
                   "H_train_han": sum(a.den["han"] for a in hb_tr), "H_holdout_han": sum(a.den["han"] for a in hb_ho)},
        "groups": {t: [] for t in ("纳入", "条件纳入", "淘汰", "待定")},
        "reverse_indicators": [],
        "performance_tier": [{"id": k, "name": v["name"], "status": "候选·不算R·只出候选表"} for k, v in CAND.items() if v.get("kind") == "probe"],
    }
    for r, s in stats.items():
        if s.get("reverse"):
            rules_draft["reverse_indicators"].append({"id": r, "name": s["name"], "R": rnd(s["R"]), "direction": s["reverse"]["direction"],
                                                      "strength": s["reverse"]["tier"],
                                                      "use": ("只报不补丁：低于真人下限时提示，不自动加字" if s["reverse"]["direction"] == "AI 偏少"
                                                              else "只报：事后反向发现，混源包内容（细纲数字），下轮独立复验前不进规则表")})
        rules_draft["groups"][s["tier"]].append({
            "id": r, "name": s["name"], "denominator": s["denominator"], "R": rnd(s["R"]), "R_ci90": [rnd(x) for x in s["R_ci90"]],
            "R_eff": rnd(s["R_eff"]), "direction": s["direction_used"], "family_R": {k: rnd(v) for k, v in s["family_R"].items()},
            "holdout_R": rnd(s["holdout_R"]), "notes": s["notes"], "source": s["source"], "kind": s["kind"]})

    doc = {"generated_at": now(), "units_file": str(args.units), "boot": args.boot, "seed": args.seed,
           "criteria": {"include": "R_eff≥2.0", "conditional": "1.25≤R_eff<2.0 且三族各≥1.25 且两对位题材池同向",
                        "reject": "R_eff<1.25（0.8–1.25 无区分力；<0.8 真人更高）",
                        "downgrades": ["纳入档任一族方向相反降一档（不再叠加条件档入门要求）", "90% bootstrap 区间跨 1 降一档", "留出集方向不同或无数据（unstable）降一档"],
                        "reverse": "同一判据按相反方向再判一次；达条件档以上记为反向指标（只报不补丁）",
                        "polarity": "L08/L12/L13/L14 假设 AI 偏少（取 1/R）；P01–P12、P19 形状指标无预设，主判按 AI 偏多，偏少归反向指标",
                        "pending": f"A0g 训练侧命中 <{MIN_AI_HITS} 且按真人率期望命中也 <{MIN_AI_HITS}（量不足以区分）"},
           "corpus": rules_draft["corpus"], "rules": {r: clean(s) for r, s in stats.items()}}
    (out / "p2_rule_stats.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "p2_chain.json").write_text(json.dumps(clean(chain), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "thresholds_draft.json").write_text(json.dumps(clean(thresholds), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "rules_v1_draft.json").write_text(json.dumps(clean(rules_draft), ensure_ascii=False, indent=1), encoding="utf-8")

    words = None
    if not args.skip_words:
        if not (args.manifest and args.corpus_tools):
            ap.error("禁用词逐词命中需要 --manifest 与 --corpus-tools；不需要时传 --skip-words")
        words = banned_words(units, stats, args.manifest, args.corpus_tools)
        (out / "p2_banned_words.json").write_text(json.dumps(clean(words), ensure_ascii=False, indent=1), encoding="utf-8")
    elif (out / "p2_banned_words.json").exists():
        words = json.loads((out / "p2_banned_words.json").read_text(encoding="utf-8"))

    (out / "p2_tables.md").write_text(render_tables(stats, chain, words, fams, books), encoding="utf-8")
    cnt = Counter(s["tier"] for s in stats.values())
    print(f"rules {len(stats)}: {dict(cnt)}")
    return 0


def rnd(x: Any, nd: int = 4) -> Any:
    if isinstance(x, float):
        if math.isinf(x):
            return "inf"
        if math.isnan(x):
            return None
        return round(x, nd)
    return x


def clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return rnd(o)


# ───────────────────────── 执行与过度矫正 ─────────────────────────
def chain_tables(units: list, rules: list[str], by_sub: dict) -> dict[str, Any]:
    """A0g→A1→A2→M 同章配对。链一：真香 6 章（有 A0g）；链二：真香第 14–40 章 A1→A2→M系（无 A0g，量大）。"""
    zxz = [u for u in units if (u.get("split_group") or "").startswith("ZXZ|")]
    grp_with_a0g = {u["split_group"] for u in zxz if u["sublayer"] == "A0g"}
    chain1 = {s: acc_of([u for u in zxz if u["split_group"] in grp_with_a0g and u["sublayer"] == s], rules)
              for s in ("A0g", "A1", "A2", "M", "M_auto")}
    chain1["M系"] = acc_of([u for u in zxz if u["split_group"] in grp_with_a0g and u["sublayer"] in ("M", "M_auto")], rules)
    g2 = {u["split_group"] for u in zxz if u["sublayer"] in ("M", "M_auto")}
    g2 = {g for g in g2 if any(u["split_group"] == g and u["sublayer"] == "A1" for u in zxz)
          and any(u["split_group"] == g and u["sublayer"] == "A2" for u in zxz)}
    chain2 = {s: acc_of([u for u in zxz if u["split_group"] in g2 and u["sublayer"] == s], rules) for s in ("A1", "A2")}
    chain2["M系"] = acc_of([u for u in zxz if u["split_group"] in g2 and u["sublayer"] in ("M", "M_auto")], rules)
    h03 = acc_of([u for u in by_sub["H"] if u["book_code"] == "H03"], rules)
    res: dict[str, Any] = {"chain1_groups": sorted(grp_with_a0g, key=lambda g: int(g.split("|")[1])),
                           "chain2_groups": len(g2), "baseline": "H03（同题材池真人，全章）", "rules": {}}
    res["chain1_han"] = {k: v.den["han"] for k, v in chain1.items()}
    res["chain2_han"] = {k: v.den["han"] for k, v in chain2.items()}
    for r in rules:
        res["rules"][r] = {"chain1": {k: v.rate(r) for k, v in chain1.items()},
                           "chain2": {k: v.rate(r) for k, v in chain2.items()}, "H03": h03.rate(r)}
    return res


# ───────────────────────── 阈值草案 ─────────────────────────
def unit_rate(u: dict[str, Any], r: str) -> float | None:
    c = CAND[r]
    v = u["rules"][r]
    if "value" in v:
        return v["value"]
    field, k = SCALE[c["denominator"]]
    d = 1 if field == "chapters" else u[field]
    return v["n"] * k / d if d else None


def build_thresholds(H: list, rules: list[str], stats: dict, pool_of_book: dict) -> dict[str, Any]:
    keep = [r for r in rules if stats[r]["tier"] in ("纳入", "条件纳入", "待定") or stats[r].get("reverse")]
    pools: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for u in H:
        pools[u["genre_pool"]][u["book_code"]].append(u)
    out: dict[str, Any] = {"version": "thresholds-draft-p2", "generated_at": now(),
                           "basis": "H 层全章（训练+留出），题材池内书等权：均值＝各书合并率的平均；放过上限＝各书章级 P90 的平均（方向为 AI 偏低的规则给 P10 下限）",
                           "rules_included": "规则表草案中 纳入／条件纳入／待定 三档", "pools": {}}
    for p, bk in sorted(pools.items()):
        entry: dict[str, Any] = {"books": sorted(bk), "chapters": sum(len(v) for v in bk.values()),
                                 "han": sum(sum(u["han"] for u in v) for v in bk.values()),
                                 "han_by_book": {b: sum(u["han"] for u in v) for b, v in sorted(bk.items())}, "rules": {}}
        if len(bk) == 1:
            entry["warning"] = "单书池：阈值＝该书文风，不是题材均值"
        for r in keep:
            accs = [acc_of(v, [r]) for v in bk.values()]
            mean = statistics.fmean(a.rate(r) for a in accs)
            d = stats[r]["direction_used"]
            if stats[r].get("reverse") and stats[r]["tier"] not in ("纳入", "条件纳入"):
                d = -1 if stats[r]["reverse"]["direction"] == "AI 偏少" else 1
            q = 0.9 if d >= 0 else 0.1
            bound = statistics.fmean(pctl([unit_rate(u, r) for u in v], q) for v in bk.values())
            entry["rules"][r] = {"name": CAND[r]["name"], "tier": stats[r]["tier"], "denominator": CAND[r]["denominator"],
                                 "direction": "AI偏高→上限" if d >= 0 else "AI偏低→下限",
                                 "reverse_indicator": bool(stats[r].get("reverse")),
                                 "human_mean": mean, "chapter_p90" if d >= 0 else "chapter_p10": bound}
        out["pools"][p] = entry
    return out


# ───────────────────────── 禁用词逐词 ─────────────────────────
def banned_words(units: list, stats: dict, manifest_path: Path, tools_dir: Path) -> dict[str, Any]:
    if str(tools_dir) not in sys.path:
        sys.path.append(str(Path(tools_dir).expanduser()))
    import build_corpus as B  # noqa: E402
    import detectors as D  # noqa: E402
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    entries = {e["id"]: e for e in manifest}
    lex = D.banned_lexicon()
    cats = D.BANNED_CATEGORY
    word_hits: dict[str, dict[str, Counter]] = {"A0g": defaultdict(Counter)}  # cat -> word -> n
    hbook: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    han_a = 0
    han_b: Counter = Counter()
    fam_hits: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    han_f: Counter = Counter()
    for u in units:
        if u["sublayer"] not in ("A0g", "H") or u.get("holdout"):
            continue
        e = entries[u["id"]]
        paras = B.prose_lines(B.load_text(e))
        for rid in cats:
            for h in D.REGISTRY[rid](paras):
                w = h.get("word") or h["text"]
                if u["sublayer"] == "A0g":
                    word_hits["A0g"][rid][w] += 1
                    fam_hits[u["family"]][rid][w] += 1
                else:
                    hbook[u["book_code"]][rid][w] += 1
        if u["sublayer"] == "A0g":
            han_a += u["han"]
            han_f[u["family"]] += u["han"]
        else:
            han_b[u["book_code"]] += u["han"]
    res: dict[str, Any] = {"split": "train", "A0g_han": han_a, "H_han_by_book": dict(han_b), "categories": {}}
    for rid, cat in cats.items():
        words = lex.get(cat, [])
        rows = []
        for w in words:
            a = word_hits["A0g"][rid][w] * 1000 / han_a
            hr = statistics.fmean(hbook[b][rid][w] * 1000 / han_b[b] for b in han_b)
            fr = {f: fam_hits[f][rid][w] * 1000 / han_f[f] for f in han_f}
            rows.append({"word": w, "A0g_n": word_hits["A0g"][rid][w], "H_n": sum(hbook[b][rid][w] for b in han_b),
                         "A0g_per_1000": a, "H_per_1000_book_equal": hr, "R": ratio(a, hr), "family_per_1000": fr})
        hit_words = [x for x in rows if x["A0g_n"] or x["H_n"]]
        res["categories"][rid] = {
            "category": cat, "n_words": len(words), "n_words_hit_A0g": sum(1 for x in rows if x["A0g_n"]),
            "n_words_hit_H": sum(1 for x in rows if x["H_n"]),
            "n_words_R_ge_2_and_A0g_ge_5": sum(1 for x in rows if x["A0g_n"] >= 5 and x["R"] is not None and x["R"] >= 2),
            "n_words_R_lt_0_8_and_H_ge_50": sum(1 for x in rows if x["H_n"] >= 50 and x["R"] is not None and x["R"] < 0.8),
            "category_R": stats[rid]["R"], "tier": stats[rid]["tier"],
            "top_by_A0g": sorted(hit_words, key=lambda x: -x["A0g_n"])[:6],
            "top_by_H": sorted(hit_words, key=lambda x: -x["H_per_1000_book_equal"])[:6],
        }
    cands = []
    for rid in cats:
        words = lex.get(cats[rid], [])
        for w in words:
            n = word_hits["A0g"][rid][w]
            hr = statistics.fmean(hbook[b][rid][w] * 1000 / han_b[b] for b in han_b)
            fr = {f: fam_hits[f][rid][w] * 1000 / han_f[f] for f in han_f}
            if n >= 10 and hr > 0 and all(v >= 2 * hr for v in fr.values()):
                cands.append({"category": rid, "word": w, "A0g_n": n, "A0g_per_1000": n * 1000 / han_a, "H_per_1000_book_equal": hr,
                              "R": n * 1000 / han_a / hr, "family_R": {f: v / hr for f, v in fr.items()}})
    res["word_candidates"] = sorted(cands, key=lambda x: -x["R"])
    res["word_candidate_rule"] = "A0g 训练侧 ≥10 次，且三族各自 ≥2×真人书等权率；逐词未做 bootstrap，只作提名"
    return res


# ───────────────────────── 自动表 ─────────────────────────
def render_tables(stats: dict, chain: dict, words: dict | None, fams: list[str], books: list[str]) -> str:
    L: list[str] = []
    order = {"纳入": 0, "条件纳入": 1, "待定": 2, "淘汰": 3}
    L.append("## 附表一 · 表层候选逐条（训练侧；R＝A0g 三族等权 ÷ H 六书等权）\n")
    L.append("分母缩写：千字＝每千汉字；百段；百句；百章；章值＝整章度量取章均值。R_eff 为按极性校正后的区分度（极性 −1 的规则取 1/R；形状指标按数据方向取 max(R,1/R)）。"
             "题材池 R：高武＝A0g(魔法高武+因果)÷H02/H04；脑洞＝A0g(真香)÷H03。留一本书列为去掉任一本后 R 的最小–最大。"
             "M 位置用全量：M（逐章过审 14 章）相对 H 书等权与 A0g。\n")
    L.append("| id | 名称 | 分母 | A0g 率 | H 率 | R（90%区间） | 族 R（claude/doubao/gpt） | 题材池 R（高武/脑洞） | 留一本书 | 留出 R | A0 R | M 率·位置 | 档位 | 备注 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    den_abbr = {"per_1000_han": "千字", "per_100_paragraphs": "百段", "per_100_sentences": "百句",
                "per_100_chapters": "百章", "per_chapter_metric": "章值"}
    for r, s in sorted(stats.items(), key=lambda kv: (order[kv[1]["tier"]], -(kv[1]["R_eff"] or 0) if not (kv[1]["R_eff"] and math.isinf(kv[1]["R_eff"])) else -1e9, kv[0])):
        lo, hi = s["R_ci90"]
        fam = "/".join(fmt(s["family_R"].get(f)) for f in fams)
        pool = "/".join(fmt(s["pool_R_matched"].get(p)) for p in ("都市高武系统", "都市脑洞游戏制作"))
        lobo = f"{fmt(s['lobo_range'][0])}–{fmt(s['lobo_range'][1])}" if s["lobo_range"] else "—"
        L.append(f"| {r} | {s['name']} | {den_abbr[s['denominator']]} | {fmt(s['A0g_train_rate'], 3)} | {fmt(s['H_train_rate_book_equal'], 3)} | "
                 f"{fmt(s['R'])}（{fmt(lo)}–{fmt(hi if hi is None or hi < 1e8 else math.inf)}） | {fam} | {pool} | {lobo} | {fmt(s['holdout_R'])}{'⚠' if s['holdout_unstable'] else ''} | "
                 f"{fmt(s['A0_R'])} | {fmt(s['M_rate'], 3)}·{s['M_position']} | **{s['tier']}** | {'；'.join(s['notes'])} |")
    L.append("")
    L.append("## 附表二 · H 各书训练侧率（看是否一本书独大／离群）\n")
    L.append("| id | " + " | ".join(books) + " | A0g |")
    L.append("|---|" + "---|" * (len(books) + 1))
    for r, s in sorted(stats.items()):
        L.append(f"| {r} | " + " | ".join(fmt(s["H_book_rates_train"][b], 3) for b in books) + f" | {fmt(s['A0g_train_rate'], 3)} |")
    L.append("")
    L.append("## 附表三 · 执行与过度矫正配对\n")
    L.append(f"链一：真香制作人同章 6 组（{', '.join(chain['chain1_groups'])}），A0g→A1→A2→M系；字数 {chain['chain1_han']}。"
             f"链二：真香 {chain['chain2_groups']} 章 A1→A2→M系（M＋M_auto）；字数 {chain['chain2_han']}。基线 H03（同池真人）。"
             "「M/H03」<0.5 且 H03 率非零标「洗过头」。\n")
    L.append("| id | 名称 | 档位 | 链一 A0g | A1 | A2 | M系 | 链二 A1 | A2 | M系 | H03 | M系/H03（链二） | 判读 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r, s in sorted(stats.items(), key=lambda kv: (order[kv[1]["tier"]], kv[0])):
        c1, c2, h = chain["rules"][r]["chain1"], chain["rules"][r]["chain2"], chain["rules"][r]["H03"]
        mh = ratio(c2["M系"], h)
        verdict = judge_chain(s, c1, c2, h)
        L.append(f"| {r} | {s['name']} | {s['tier']} | {fmt(c1['A0g'], 3)} | {fmt(c1['A1'], 3)} | {fmt(c1['A2'], 3)} | {fmt(c1['M系'], 3)} | "
                 f"{fmt(c2['A1'], 3)} | {fmt(c2['A2'], 3)} | {fmt(c2['M系'], 3)} | {fmt(h, 3)} | {fmt(mh)} | {verdict} |")
    L.append("")
    if words:
        L.append("## 附表四 · 禁用词按类别（训练侧，每千字；不复制整表，只列命中前几的词）\n")
        L.append(f"A0g 训练侧 {words['A0g_han']:,} 字；H 训练侧按书等权。\n")
        L.append("| 类 | 类名 | 词数 | A0g 命中词数 | H 命中词数 | 类 R | 档位 | 逐词 R≥2 且 A0g≥5 | 逐词 R<0.8 且 H≥50 | A0g 高频词（A0g 次／R） | 真人高频词（H 每千字／R） |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for rid, c in words["categories"].items():
            ta = "、".join(f"{x['word']}({x['A0g_n']}/{fmt(x['R'])})" for x in c["top_by_A0g"][:5] if x["A0g_n"])
            th = "、".join(f"{x['word']}({fmt(x['H_per_1000_book_equal'], 3)}/{fmt(x['R'])})" for x in c["top_by_H"][:4] if x["H_n"])
            L.append(f"| {rid} | {c['category']} | {c['n_words']} | {c['n_words_hit_A0g']} | {c['n_words_hit_H']} | {fmt(c['category_R'])} | {c['tier']} | "
                     f"{c['n_words_R_ge_2_and_A0g_ge_5']} | {c['n_words_R_lt_0_8_and_H_ge_50']} | {ta or '—'} | {th or '—'} |")
        L.append("")
    return "\n".join(L)


def judge_chain(s: dict, c1: dict, c2: dict, h: float | None) -> str:
    d = s["direction_used"]
    m = c2["M系"]
    if h is None or m is None:
        return "—"
    if h == 0:
        return "H03 为零" if m == 0 else "H03 为零，M 有"
    rel = m / h
    tag = []
    if d >= 0:
        if rel < 0.5:
            tag.append("洗过头（M<½H）")
        elif rel > 1.5:
            tag.append("M 仍高于真人")
    else:
        if rel > 2:
            tag.append("洗过头（反向）")
        elif rel < 0.67:
            tag.append("M 仍偏 AI")
    a0g, a1 = c1["A0g"], c1["A1"]
    if a0g and a1 is not None and a0g > 0 and d >= 0:
        if a1 / a0g < 0.5:
            tag.append("A1 已压下")
    return "；".join(tag) or "≈真人"


if __name__ == "__main__":
    sys.exit(main())
