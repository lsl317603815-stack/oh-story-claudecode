#!/usr/bin/env python3
"""去 AI 味语料验证规则：corpus_rules 检测器 + 规则表 v1 + 缺省阈值表的回归测试。

检测器样例全部自造，不取语料（语料池在仓外）。可直接运行，也可 `python3 -m pytest -q -p no:cacheprovider` 运行。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "story-deslop"
HERE = SKILL / "scripts" / "corpus_rules"
sys.path.insert(0, str(HERE))
import detectors as D  # noqa: E402

CANDS = json.loads((HERE / "rules_candidates.json").read_text(encoding="utf-8"))["candidates"]
CONTRACTS = json.loads((SKILL / "references" / "pattern-contracts.json").read_text(encoding="utf-8"))
DEFAULTS = json.loads((SKILL / "references" / "deslop-thresholds.default.json").read_text(encoding="utf-8"))
RULES = {r["id"]: r for r in CONTRACTS["surface_rules"]}

# (规则, 正样例段落, 负样例段落)
COUNT_CASES: list[tuple[str, list[str], list[str]]] = [
    ("L01", ["他要的不是钱，而是一个说法。"], ["他要钱，也要一个说法。"]),
    ("L01", ["这件事说到底还是怪他。"], ["这件事还是怪他。"]),
    ("L02", ["桌上摆着茶杯、钥匙、半包烟。"], ["桌上摆着茶杯和钥匙。"]),
    ("L03", ["老周推开铁门，屋里一股霉味。小李拉开窗帘，地上全是灰尘。"], ["老周推开铁门。屋里很暗，地上全是灰尘，墙角还堆着几只纸箱。"]),
    ("L03b", ["老周推开铁门，屋里一股霉味。小李拉开窗帘，地上全是灰尘。阿芳打开电灯，墙上全是裂缝。"],
     ["老周推开铁门，屋里一股霉味。小李拉开窗帘，地上全是灰尘。"]),
    ("L04", ["他找到了一条路——一条没人走过的路。"], ["他找到了一条没人走过的路。"]),
    ("L05a", ["核心是：先把账还上。"], ["他说：“先把账还上。”"]),
    ("L05b", ["我见过的几种情况：", "1. 断电", "2. 断网"], ["我见过断电和断网。", "后来都修好了。"]),
    ("L06", ["## 一、开门", "正文。", "## 二、进屋", "正文。", "## 三、关门", "正文。"], ["## 一、开门", "正文。", "## 二、进屋", "正文。"]),
    ("L07", ["这台机器像一位永不疲倦的管家，把屋子收拾得干干净净。"], ["他像一个刚下夜班的保安，眼皮都抬不起来。"]),
    ("L08", ["倒计时还剩四十三秒，他手里只有17块钱。"], ["他十分确定，一个人也没来。"]),
    ("L09", ["说白了，就是没钱。"], ["就是没钱。"]),
    ("L10a", ["这是一个能够让所有人在不加班的情况下按时下班的办法。"], ["这个办法能让大家按时下班。"]),
    ("L10a", ["她检查了系统的稳定性的报告。"], ["她检查了系统稳定性报告。"]),
    ("L10b", ["当所有人都离开教室时，他才站起来。"], ["当然，他最后还是站起来了。"]),
    ("L10c", ["对于新来的学生来说，食堂太远了。"], ["新来的学生嫌食堂太远。"]),
    ("L10d", ["然而，门已经锁上了。"], ["门其实已经锁上了。"]),
    ("L10e", ["这意味着他们今晚出不去。"], ["他们今晚出不去。"]),
    ("L11", ["她把信放回抽屉。", "值得一提的是，抽屉没有锁。"], ["她把信放回抽屉。", "值得一提的是，这个抽屉没有锁。"]),
    ("L15", ["他很快就走了。"], ["他快步离开。"]),
    ("L16", ["其实我不想去。"], ["我不想去。"]),
    ("L17", ["林晚放下杯子。林晚看向窗外。"], ["林晚放下杯子。她看向窗外。"]),
    ("L18", ["他被视为下一任掌门。"], ["大家都说他是下一任掌门。"]),
    ("L19", ["他为什么要回来？因为钥匙还在屋里。"], ["“他为什么要回来？”“因为钥匙还在屋里。”"]),
    ("L20", ["## 他为什么要回来？", "正文。"], ["## 他回来了", "正文。"]),
    ("L21", ["门外是谁？她没敢出声。"], ["“门外是谁？”她小声问。"]),
    ("L22", ["要么现在走，要么永远别走。"], ["现在走吧，别等天黑。"]),
    ("L23", ["他的声音仿佛从很远的地方传来。"], ["他的声音从很远的地方传来。"]),
    ("L23", ["她瘦得像根竹竿。"], ["她好像是累了。"]),
    ("L23b", ["前情。", "像一只受惊的猫，她弓起了背。"], ["前情。", "她弓起了背，又慢慢放松下来。"]),
    ("L24", ["沉默吞噬了整个房间。"], ["房间里没人说话。"]),
    ("L25", ["团队完成了对流程的优化。"], ["团队把流程改顺了。"]),
    ("L26", ["首先，把门关上。"], ["先把门关上。"]),
    ("D01", ["她眼里有一丝犹豫。"], ["她眼里有点犹豫。"]),
    ("D02", ["他深吸一口气，推开门。"], ["他推开门。"]),
    ("D03", ["他嘴角勾起，没说话。"], ["他咧了下嘴，没说话。"]),
    ("D04", ["他心中一动，停下脚步。"], ["他停下脚步。"]),
    ("D05", ["答案显而易见。"], ["答案就摆在桌上。"]),
    ("D06", ["她的眼神很坚定。"], ["她盯着他不放。"]),
    ("D07", ["他不由自主地往后退。"], ["他往后退了一步。"]),
    ("D08", ["门突然开了。"], ["门开了。"]),
    ("D09", ["她缓缓转过身。"], ["她转过身。"]),
    ("D10", ["他的防线瓦解了。"], ["他撑不住了。"]),
    ("D11", ["笑容消失，取而代之的是一脸冷漠。"], ["笑容没了，他冷着脸。"]),
    ("D11", ["他浑身散发着一股生人勿近的气息。"], ["周围的人都不敢靠近他。"]),
    ("D12", ["他不是冷漠，是累了。"], ["是不是累了，他也说不清。"]),
    ("D13", ["不是怕。", "也不是恨。", "只是不想再见。"], ["不是怕。", "他就是不想再见。"]),
    ("D14", ["他笑了一下，带着几分嘲讽。"], ["他带着人往南走。"]),
    ("D15", ["她的声音不大，却带着不容商量的意思。"], ["她压低声音说完了最后一句。"]),
    ("D16", ["他知道这一切都来不及了。"], ["“我知道来不及了。”"]),
    ("D17", ["“走吧。”他说。"], ["“走吧。”", "他转身出了门。"]),
    ("D18", ["“随你。”她淡淡道。"], ["“随你。”她把门带上了。"]),
    ("D19", ["他推开门。", "她跟上来。", "这一刻，他终于明白了一切。"], ["这一刻他还不懂。", "他推开门。", "她跟上来。", "门外下着雨。"]),
    ("D20", ["原来他早就知道。"], ["“原来是你。”"]),
    ("D21", ["之所以没人来，是因为路断了。"], ["路断了，没人来。"]),
    ("D22", ["她不知道的是，门外还站着一个人。"], ["门外还站着一个人。"]),
    ("D24", ["他走了。她来了。门开了。灯亮了。雨停了。"], ["他走了。她从走廊尽头一路小跑过来，鞋都湿透了。门开了。灯亮了。雨停了。"]),
    ("D25", ["有的人哭，有的人笑，有的人一句话也不说。"], ["有的人哭，别的人在笑。"]),
    ("D25", ["他记得那条街。他记得那家店。他记得那个人。"], ["他记得那条街。那家店早关了。他记得那个人。"]),
    ("D26", ["天色犹如泼墨。"], ["天黑透了。"]),
    ("D27", ["她瞳孔微缩，后退半步。"], ["她后退半步。"]),
    ("D28", ["“这是什么？”",
             "“这是我们镇上传了三代的老规矩，每年开春都要把井口封七天，谁也不能打水，等第八天天亮才能揭开封条，"
             "揭的时候还得由最年长的人先喝第一口。”"],
     ["“这是什么？”", "“井盖。”"]),
    ("D29", ["不吃鱼，不吃肉，只喝粥。"], ["不哭，不闹，就那么坐着。"]),
    ("D30", ["她有些紧张，手心全是汗。"], ["她手心全是汗。"]),
    ("P06", ["走吧。"], ["他从走廊那头一路走过来，手里拎着两袋东西。"]),
    ("P07", ["他从走廊那头一路走过来，手里拎着两袋刚从菜市场买回来的青菜和一条还在扑腾的鲫鱼。"], ["走吧。"]),
    ("P08", ["他从走廊那头走过来。"], ["走吧。"]),
    ("P09", ["“走吧。”他说。"], ["他说：“走吧。”"]),
    ("P10", ["他来了，又走了。"], ["他来了又走了。"]),
    ("P11", ["你去哪？"], ["你去哪。"]),
    ("P12", ["快跑！"], ["快跑。"]),
    ("P13", ["他没回头。", "雨很大。", "他没回头。"], ["他没回头。", "雨很大。"]),
    ("P14", ["他像是没听见。"], ["他没听见。"]),
    ("P15", ["这意味着他们输了。"], ["他们输了。"]),
    ("P16", ["她心口一沉。"], ["她没说话。"]),
    ("P17", ["随后他关上了门。"], ["他关上了门。"]),
    ("P18", ["他不是不想去，而是去不了。"], ["他去不了。"]),
    ("W01", ["他猛地站起来。"], ["他一下站起来。"]),
]

METRIC_CASES: list[tuple[str, list[str], list[str]]] = [
    # (规则, 值较高的样例, 值较低的样例)
    ("L12", ["走。他从走廊那头一路走过来，手里拎着两袋东西，嘴里还哼着歌。停。"], ["他走过来了。她站起来了。门关上了。"]),
    ("L13", ["走。他从走廊那头一路走过来，手里拎着两袋东西，嘴里还哼着歌。停。"], ["他走过来了。她站起来了。门关上了。"]),
    ("L14", ["走。", "他从走廊那头一路走过来，手里拎着两袋东西。", "停。", "她抬起头，看了他很久，什么也没说。"],
     ["他走过来了。", "她站起来了。", "门关上了。", "灯熄灭了。"]),
    ("D23", ["他走过来了。", "她站起来了。", "门关上了。", "灯熄灭了。"],
     ["走。", "他从走廊那头一路走过来，手里拎着两袋东西。", "停。", "她抬起头，看了他很久，什么也没说。"]),
    ("P01", ["他从走廊那头一路走过来，手里拎着两袋东西。"], ["走。"]),
    ("P02", ["他从走廊那头一路走过来，手里拎着两袋东西。"], ["走。"]),
    ("P03", ["他从走廊那头一路走过来，手里拎着两袋东西。"], ["走。停。"]),
    ("P04", ["他从走廊那头一路走过来，手里拎着两袋东西。"], ["走。停。"]),
    ("P05", ["他从走廊那头一路走过来手里拎着两袋东西。"], ["走，停，看。"]),
    ("P19", ["走。停。看。"], ["走。", "停。"]),
]


def test_every_surface_candidate_has_detector_and_cases():
    surface = {c["id"] for c in CANDS if c["tier"] == "surface" and c["kind"] != "group"}
    assert surface == set(D.REGISTRY), surface ^ set(D.REGISTRY)
    covered = {r for r, _, _ in COUNT_CASES} | {r for r, _, _ in METRIC_CASES}
    assert surface <= covered, surface - covered
    for c in CANDS:
        if c["tier"] == "surface" and c["kind"] != "group":
            assert c["detector"] == f"detect_{c['id']}"
        assert c["denominator"] in {"per_1000_han", "per_100_paragraphs", "per_100_sentences",
                                    "per_100_chapters", "per_chapter_metric"}


def test_four_conflict_topics_marked():
    topics = {c.get("conflict_topic") for c in CANDS if c.get("conflict") == "lieflat_rejected"}
    assert topics == {"段落均匀度", "句内节奏", "比喻文学化", "设问"}


def test_lieflat_26_items():
    tops = [c for c in CANDS if c["id"].startswith("L") and not c.get("parent")]
    assert len(tops) == 26
    assert sum(1 for c in tops if c["lieflat"]["status"] == "retained") == 11
    assert sum(1 for c in tops if c["lieflat"]["status"] == "rejected") == 15


def test_count_detectors():
    for rule, pos, neg in COUNT_CASES:
        hp = D.REGISTRY[rule](pos)
        hn = D.REGISTRY[rule](neg)
        assert hp, f"{rule} 正样例未命中: {pos}"
        assert not hn, f"{rule} 负样例误命中: {[h['text'] for h in hn]}"
        for h in hp:
            assert h["rule"] == rule and {"para", "sent", "sentence", "span", "text", "in_dialogue"} <= set(h)
            assert pos[h["para"]][h["span"][0]:h["span"][1]] == h["text"]


def test_metric_detectors():
    for rule, hi, lo in METRIC_CASES:
        vh = D.REGISTRY[rule](hi)[0]["value"]
        vl = D.REGISTRY[rule](lo)[0]["value"]
        assert vh > vl, f"{rule}: {vh} 不大于 {vl}"


def test_detectors_do_not_mutate_input():
    paras = ["他要的不是钱，而是一个说法。", "“走吧。”他淡淡道。"]
    snapshot = list(paras)
    D.run_all(paras)
    assert paras == snapshot


def test_in_dialogue_flag():
    hits = D.REGISTRY["D08"](["“突然下雨了。”他突然说。"])
    assert [h["in_dialogue"] for h in hits] == [True, False]


def test_banned_lexicon_parsed_from_deslop():
    assert D.BANNED_WORDS == SKILL / "references" / "banned-words.md"
    lex = D.banned_lexicon()
    for cat in D.BANNED_CATEGORY.values():
        assert lex.get(cat), cat


# ───────────────────────── 规则表 v1（2026-10-06 拍板） ─────────────────────────
def test_rule_table_counts_match_p2_ruling():
    table = CONTRACTS["rule_table"]
    assert table["version"] == "v1-2026-10-06" and CONTRACTS["version"] == 2
    statuses = {}
    for r in CONTRACTS["surface_rules"]:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    assert statuses == {"conditional": 1, "retired": 49, "pending": 32, "candidate": 1}, statuses
    assert RULES["L04"]["status"] == "conditional" and RULES["L04"]["action"] == "threshold_hint"
    assert RULES["W01"]["status"] == "candidate" and RULES["W01"]["R"] > 14
    deficits = sorted(r["id"] for r in CONTRACTS["surface_rules"] if (r.get("deficit") or {}).get("status") == "preregistered_deficit")
    assert deficits == ["D01", "D18", "D26", "L01", "L10a", "L16", "L17", "L21", "P11", "P12"], deficits
    assert RULES["L08"]["reverse_observation"]["status"] == "observe_only"


def test_every_rule_traceable_and_non_blocking():
    fingerprint = CONTRACTS["rule_table"]["corpus"]["fingerprint"]
    for r in CONTRACTS["surface_rules"]:
        assert r["blocking"] is False, r["id"]
        assert r["version"] == "v1-2026-10-06" and r["corpus_fingerprint"] == fingerprint, r["id"]
        assert "R" in r and r.get("source"), r["id"]
        assert r["corpus_rules_detector"] == f"detect_{r['id']}" and r["id"] in D.REGISTRY, r["id"]
        if r["status"] == "retired":
            assert r["retired"]["on"] == "2026-10-06" and "R" in r["retired"], r["id"]
            assert r["counts_toward_issue_density"] is False, r["id"]


def test_retired_gate_items_named_in_audit():
    retired = {"D01", "D02", "D04", "D08", "D09", "D12", "D17", "D18", "D26", "D23", "D24", "D25", "D21",
               "D27", "P13", "P14", "P17", "P18"}
    for rid in retired:
        assert RULES[rid]["status"] == "retired", rid


def test_conflict_items_never_block():
    conflict = [r for r in CONTRACTS["surface_rules"] if r.get("conflict_topic")]
    assert {r["conflict_topic"] for r in conflict} == {"段落均匀度", "句内节奏", "比喻文学化", "设问"}
    assert len(conflict) == 12  # 第 13 条 D47 属表演档，在 patterns 里
    assert any(p.get("corpus_id") == "D47" for p in CONTRACTS["patterns"])
    assert RULES["L23"]["action"] == "family_hint" and RULES["L23"]["family_hint"]["families"] == ["claude", "doubao"]


def test_check_ai_pattern_types_exist_in_detector():
    js = (SKILL / "scripts" / "check-ai-patterns.js").read_text(encoding="utf-8")
    for r in CONTRACTS["surface_rules"]:
        for t in r.get("check_ai_patterns_types", []):
            if t.startswith("candidate-term"):
                continue
            assert f"type: '{t}'" in js, t
    for t in CONTRACTS["rule_table"]["unvalidated_detectors"]:
        assert f"type: '{t}'" in js, t


def test_default_thresholds_cover_alarm_rules():
    assert DEFAULTS["schema"] == "deslop-thresholds/v1" and DEFAULTS["default_pool"] in DEFAULTS["pools"]
    assert len(DEFAULTS["pools"]) == 5
    for name, pool in DEFAULTS["pools"].items():
        for rid in ("L04", "W01", "L23"):
            assert "chapter_p90" in pool["rules"][rid], (name, rid)
        for rid in ("P11", "P12", "L16", "L21", "L17", "L10a", "L01", "D01", "D18", "D26"):
            assert "chapter_p10" in pool["rules"][rid], (name, rid)


def test_js_and_python_chapter_rates_agree():
    """check-ai-patterns.js 的章级率必须与 corpus_rules 同口径，否则阈值对不上。"""
    body = "\n".join([
        "# 第1章 测试",
        "他猛地回头——门外是谁？她没敢出声。",
        "“你来干什么？”他问，“其实不过是路过！”",
        "她像一只受惊的猫，仿佛随时会跑。但是她没跑！",
    ] * 30)
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "第001章.md"
        path.write_text(body, encoding="utf-8")
        out = subprocess.run(["node", str(SKILL / "scripts" / "check-ai-patterns.js"), "--json", str(path)],
                             capture_output=True, text=True, check=False)
        meta = json.loads(out.stdout)["rule_table"]["files"][0]["chapter"]
    paras = D.VP.normalized_lines(body)
    units = D.unit_counts(paras)
    assert meta["han"] == units["han"]
    for rid in ("L04", "W01", "L23", "P11", "P12", "L21", "L16"):
        assert meta["counts"][rid] == len(D.REGISTRY[rid](paras)), rid


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("deslop corpus rules tests:", "FAILED" if failures else "passed")
    sys.exit(1 if failures else 0)
