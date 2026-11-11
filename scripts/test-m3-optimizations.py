#!/usr/bin/env python3
"""Regression tests for Milestone 3 optimizations:
- tracking_commit.py incremental `action="patch"` and O(N) UTF-8 `clip_bytes`
- revision_guard.py zero-ceiling `start-polish` / `finish-polish` + hook parity
- chapter_candidate.py gate cache, `gate-summary.json`, `aigc-safe-g5` preset, `.lineage/` pruning
- `.story/hooks/*.sh` + `.story/.env.local` across `close`, `finish-polish`, `session-end.sh`, Antigravity `stop`
- `.deslop-whitelist` normalized phrase + Unicode foreign token parity & `--skip-language-leak`
- `project-hooks` templates (`export_book.py` TXT/EPUB3, `zhuque_ledger.py` SHA-256 ledger)
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LONG_SCRIPTS = ROOT / "skills/story-long-write/scripts"
DESLOP_SCRIPTS = ROOT / "skills/story-deslop/scripts"
SETUP_TEMPLATES = ROOT / "skills/story-setup/references/templates"
ANTIGRAVITY_HOOK = ROOT / "skills/story-setup/references/antigravity/hooks/story_antigravity_hook.js"
CODEX_HOOK = ROOT / "skills/story-setup/references/codex/hooks/story_codex_hook.py"
HOOK_CORE = SETUP_TEMPLATES / "hooks/story_hook_core.js"
SESSION_END = SETUP_TEMPLATES / "hooks/session-end.sh"
PROJECT_HOOKS = SETUP_TEMPLATES / "project-hooks"

TRACKING = LONG_SCRIPTS / "tracking_commit.py"
CANDIDATE = LONG_SCRIPTS / "chapter_candidate.py"
REVISION = LONG_SCRIPTS / "revision_guard.py"
VOICE = LONG_SCRIPTS / "voice_profile.py"
DOCTOR = LONG_SCRIPTS / "story_doctor.py"


def load_py_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tracking_mod = load_py_module("m3_tracking_commit", TRACKING)
codex_mod = load_py_module("m3_codex_hook", CODEX_HOOK)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_py(script: Path, *args: str, expected: int = 0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        text=True,
        capture_output=True,
        encoding="utf-8",
        env=merged_env,
        check=False,
    )
    if proc.returncode != expected:
        raise AssertionError(
            f"expected {expected}, got {proc.returncode}: {script.name} {' '.join(args)}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


class Milestone3OptimizationsTests(unittest.TestCase):
    def test_clip_bytes_utf8_boundaries(self) -> None:
        sample = "甲乙丙丁" + "𠀀" + "戊己"  # 3-byte BMP + 4-byte SMP CJK
        for limit in range(3, 28):
            clipped = tracking_mod.clip_bytes(sample, limit)
            encoded = clipped.encode("utf-8")
            self.assertLessEqual(len(encoded), limit)
            self.assertEqual(encoded.decode("utf-8"), clipped)
            if limit < len(sample.encode("utf-8")):
                self.assertTrue(clipped.endswith("…"))

    def test_tracking_commit_patch_mode_preserves_unmodified_fields_and_continuity_guard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m3-patch-") as tmp:
            project = Path(tmp) / "book"
            project.mkdir()
            init_doc = {
                "schema_version": 1,
                "book_title": "增量补丁测试",
                "last_chapter": 1,
                "context": {
                    "position": {"volume": "第一卷", "volume_start_chapter": 1, "story_time": "清晨", "scene": "山门"},
                    "long_term_constraints": ["全程纯中文"],
                    "active_character_names": ["林川"],
                    "continuity_risks": [],
                    "recent_chapters": [{"chapter": 1, "summary": "林川背剑到山门。"}],
                    "next_chapter_commitments": ["进入外院"],
                },
                "character_snapshots": {
                    "林川": {
                        "identity": "外院杂役",
                        "location": "山门石阶",
                        "goal": "拿到外院腰牌",
                        "state": "左臂擦伤",
                        "abilities_resources": ["旧铁剑", "三枚铜钱"],
                        "relationships": ["认识周管事"],
                        "knowledge": ["今日午时封门"],
                        "open_threads": ["腰牌未领"],
                    }
                },
                "foreshadow": [
                    {
                        "id": "F001",
                        "summary": "旧铁剑剑柄藏着半截残图",
                        "planted_chapter": 1,
                        "planned_resolution_chapter": 10,
                        "status": "已埋",
                        "importance": "高",
                    }
                ],
                "timeline_events": [
                    {
                        "id": "E001",
                        "story_time": "三年前雨夜",
                        "objective_fact": "老仆把铁剑交给林川",
                        "reader_knowledge": "只知铁剑是家传旧物",
                        "reveal_status": "部分揭示",
                        "reveal_chapter": 1,
                        "characters": ["林川"],
                    }
                ],
                "facts": [
                    {
                        "id": "K001",
                        "category": "所有权",
                        "subject": "林川",
                        "predicate": "持有信物",
                        "object": "旧铁剑",
                        "cardinality": "one",
                        "canon_status": "正文已证",
                        "reader_status": "已揭示",
                        "reveal_chapter": 1,
                        "established_chapter": 1,
                        "evidence": ["正文/第001章.md｜背剑上山"],
                        "related_entities": ["林川"],
                        "negative_constraints": ["非宗门赐剑"],
                    }
                ],
            }
            init_file = Path(tmp) / "init.json"
            write_json(init_file, init_doc)
            run_py(TRACKING, "init", "--project", str(project), "--input", str(init_file))

            # Unacknowledged location change in patch mode must still be rejected by continuity guard
            bad_tx = {
                "schema_version": 1,
                "mode": "append",
                "chapter": 2,
                "chapter_title": "领牌",
                "expected_state_revision": 0,
                "delta": {
                    "result": "林川领到腰牌。",
                    "appeared_characters": ["林川"],
                    "character_changes": [{"name": "林川", "change": "挪去执事堂"}],
                    "foreshadow_changes": [],
                    "timeline_events": [],
                    "fact_changes": [],
                    "constraints": [],
                    "next_chapter_commitments": [],
                },
                "context": {
                    "position": {"volume": "第一卷", "volume_start_chapter": 1, "story_time": "上午", "scene": "执事堂"},
                    "long_term_constraints": ["全程纯中文"],
                    "active_character_names": ["林川"],
                    "continuity_risks": [],
                },
                "character_snapshots": {
                    "林川": {"action": "patch", "location": "执事堂"}
                },
            }
            bad_file = Path(tmp) / "bad.json"
            write_json(bad_file, bad_tx)
            failed = run_py(TRACKING, "commit", "--project", str(project), "--input", str(bad_file), expected=2)
            self.assertIn("continuity_changes", failed.stderr)

            # Valid patch across character_snapshots, foreshadow_changes, timeline_events, fact_changes
            good_tx = bad_tx.copy()
            good_tx["delta"] = {
                **bad_tx["delta"],
                "continuity_changes": [
                    {
                        "name": "林川",
                        "field": "location",
                        "from": "山门石阶",
                        "to": "执事堂",
                        "reason": "上山入堂领腰牌",
                    }
                ],
                "foreshadow_changes": [{"action": "patch", "id": "F001", "planned_resolution_chapter": 8}],
                "timeline_events": [{"action": "patch", "id": "E001", "reveal_status": "已揭示", "reveal_chapter": 2}],
                "fact_changes": [
                    {
                        "action": "patch",
                        "id": "K001",
                        "evidence": ["正文/第001章.md｜背剑上山", "正文/第002章.md｜执事堂登记铁剑"],
                    }
                ],
            }
            good_tx["character_snapshots"] = {
                "林川": {
                    "action": "patch",
                    "location": "执事堂",
                    "state": "领到外院木牌",
                    "open_threads": ["明日晨课点卯"],
                }
            }
            good_file = Path(tmp) / "good.json"
            write_json(good_file, good_tx)
            run_py(TRACKING, "commit", "--project", str(project), "--input", str(good_file))
            run_py(TRACKING, "check", "--project", str(project))

            state = json.loads((project / "追踪/_tracking-state.json").read_text(encoding="utf-8"))
            snap = state["characters"]["林川"]
            self.assertEqual(snap["identity"], "外院杂役")
            self.assertEqual(snap["goal"], "拿到外院腰牌")
            self.assertEqual(snap["abilities_resources"], ["旧铁剑", "三枚铜钱"])
            self.assertEqual(snap["location"], "执事堂")
            self.assertEqual(snap["state"], "领到外院木牌")
            self.assertEqual(state["foreshadow"]["F001"]["summary"], "旧铁剑剑柄藏着半截残图")
            self.assertEqual(state["foreshadow"]["F001"]["planned_resolution_chapter"], 8)
            self.assertEqual(state["timeline"]["E001"]["objective_fact"], "老仆把铁剑交给林川")
            self.assertEqual(state["timeline"]["E001"]["reveal_status"], "已揭示")
            self.assertEqual(len(state["facts"]["K001"]["evidence"]), 2)

    def test_whitelist_normalization_unicode_foreign_and_skip_language_leak(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m3-lang-") as tmp:
            book = Path(tmp)
            (book / "正文").mkdir()
            (book / ".deslop-whitelist").write_text(
                '# 行尾注释与引号归一\n"Project Zero" # 专有名词\nα # 希腊字母代号\n',
                encoding="utf-8",
            )
            prose = book / "正文" / "第001章_测试.md"
            prose.write_text("# 第一章 测试\n\n他翻开写着 Project Zero 的卷宗，页角画着一枚 α 印记。\n", encoding="utf-8")

            lg = subprocess.run(
                ["node", str(DESLOP_SCRIPTS / "language_gate.js"), str(prose)],
                text=True, capture_output=True, encoding="utf-8", check=False,
            )
            self.assertEqual(lg.returncode, 0, lg.stdout + lg.stderr)

            dg = subprocess.run(
                ["node", str(DESLOP_SCRIPTS / "check-degeneration.js"), "--check", "--language=zh", "--fail-on=blocking", str(prose)],
                text=True, capture_output=True, encoding="utf-8", check=False,
            )
            self.assertEqual(dg.returncode, 0, dg.stdout + dg.stderr)

            # Unwhitelisted Greek letter β on an ASCII-free line must be caught by default,
            # and skipped when --skip-language-leak is passed.
            prose.write_text("# 第一章 测试\n\n石碑背面刻着一枚陌生的 β 符印。\n", encoding="utf-8")
            dg_leak = subprocess.run(
                ["node", str(DESLOP_SCRIPTS / "check-degeneration.js"), "--check", "--language=zh", "--fail-on=blocking", str(prose)],
                text=True, capture_output=True, encoding="utf-8", check=False,
            )
            self.assertNotEqual(dg_leak.returncode, 0)

            dg_skip = subprocess.run(
                ["node", str(DESLOP_SCRIPTS / "check-degeneration.js"), "--check", "--language=zh", "--skip-language-leak", "--fail-on=blocking", str(prose)],
                text=True, capture_output=True, encoding="utf-8", check=False,
            )
            self.assertEqual(dg_skip.returncode, 0, dg_skip.stdout + dg_skip.stderr)

    def test_g5_preset_gate_cache_lineage_prune_polish_hooks_and_exporters(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m3-full-") as tmp:
            base = Path(tmp)
            project = base / "book"
            (project / "大纲").mkdir(parents=True)
            (project / "正文").mkdir()
            (project / "设定").mkdir()
            (project / "设定/文风.md").write_text("# 文风\n\n朴实白话。\n", encoding="utf-8")
            write_json(project / "设定/门禁配置.json", {"schema_version": 1, "preset": "aigc-safe-g5"})

            # Install a project hook + .env.local to verify event triggers
            hook_log = base / "hook-events.jsonl"
            (project / ".story/hooks").mkdir(parents=True, exist_ok=True)
            (project / ".story/.env.local").write_text(
                f'STORY_AUTHOR="云海客"\nM3_HOOK_LOG="{hook_log}"\n',
                encoding="utf-8",
            )
            hook_sh = project / ".story/hooks/record.sh"
            hook_sh.write_text(
                '#!/bin/bash\n'
                'printf \'{"event":"%s","author":"%s","chapter":"%s"}\\n\' '
                '"${STORY_HOOK_EVENT:-}" "${STORY_AUTHOR:-}" "${STORY_CHAPTER:-}" >> "$M3_HOOK_LOG"\n',
                encoding="utf-8",
            )
            hook_sh.chmod(0o755)

            # Seed chapters 1..5 as legacy chapters + tracking state at chapter 5 so we can build voice profile on Ch 6!
            chapters_seed = [
                ("第001章_春雨.md", "# 第一章 春雨\n\n林川推开木门，雨水顺着青石缝流进院里。\n他把药包放到桌边，问母亲今天是否好些？\n母亲摇头说还差一味药材。\n"),
                ("第002章_寻药.md", "# 第二章 寻药\n\n清晨雾气未散，林川背起竹篓往南山药铺走去。\n掌柜翻开账册看了一眼，说那味赤芍昨夜刚被人买空！\n林川站在柜台前皱起眉头。\n"),
                ("第003章_问路.md", "# 第三章 问路\n\n药铺学徒低声提醒他去东街老集碰碰运气。\n林川道了声谢，踩着湿滑的青砖快步赶往东街。\n巷口茶摊旁坐着一位披蓑衣的老药农。\n"),
                ("第004章_换药.md", "# 第四章 换药\n\n老药农掀开竹篓，里头恰好躺着两株带泥的赤芍。\n林川摸出仅剩的铜钱递过去，问这点钱够不够换一株？\n老药农盯着他腰间的旧铁牌看了两眼。\n"),
                ("第005章_归家.md", "# 第五章 归家\n\n老药农分文未收，只叫他把铁牌擦干净收好。\n林川揣着赤芍一路跑回小院，生火替母亲煎好汤药。\n门外巷子里却忽然响起急促的敲门声！\n"),
            ]
            for idx, (fname, body) in enumerate(chapters_seed, 1):
                (project / "正文" / fname).write_text(body, encoding="utf-8")
                (project / "大纲" / f"细纲_第{idx:03d}章.md").write_text(
                    f"# 第{idx}章细纲\n\n- 章节定位：普通推进\n- 字数目标：65 字\n\n推进第{idx}章剧情。\n",
                    encoding="utf-8",
                )

            init_doc = {
                "schema_version": 1,
                "book_title": "青石药行",
                "last_chapter": 5,
                "context": {
                    "position": {"volume": "第一卷", "volume_start_chapter": 1, "story_time": "春日傍晚", "scene": "林家小院"},
                    "long_term_constraints": [],
                    "active_character_names": ["林川"],
                    "continuity_risks": [],
                    "recent_chapters": [{"chapter": 5, "summary": "林川煎好汤药，门外响起急促敲门声。"}],
                    "next_chapter_commitments": ["开门查看来人身份"],
                },
                "character_snapshots": {
                    "林川": {
                        "identity": "林家少年",
                        "location": "林家小院",
                        "goal": "护住母亲并查清旧铁牌来历",
                        "state": "刚煎好汤药",
                        "abilities_resources": ["旧铁牌"],
                        "relationships": ["母亲"],
                        "knowledge": ["老药农认得旧铁牌"],
                        "open_threads": ["门外敲门人身份未明"],
                    }
                },
                "foreshadow": [],
                "timeline_events": [],
                "facts": [],
            }
            write_json(base / "init.json", init_doc)
            run_py(TRACKING, "init", "--project", str(project), "--input", str(base / "init.json"))

            # Candidate Chapter 6 with multiple edits to test cache + lineage pruning + G5 preset
            ch6_prose_v1 = (
                "# 第六章 夜客叩门\n\n"
                "林川吹熄灶膛余火，顺手抄起门后的木闩。\n"
                "门外站着药铺的小学徒，浑身湿透地喘着粗气。\n"
                "学徒压低嗓子问他手里是不是有一块旧铁牌？\n"
            )
            ch6_prose_v2 = (
                "# 第六章 夜客叩门\n\n"
                "林川掩上灶间木门，抄起门后的硬木顶闩。\n"
                "门外站着药铺的小学徒，蓑衣滴着冰凉的雨水。\n"
                "学徒压低嗓子问他手里是不是藏着那块旧铁牌？\n"
            )
            ch6_prose_final = (
                "# 第六章 夜客叩门\n\n"
                "林川掩紧房门，伸手握住门后的硬木顶闩。\n"
                "门外立着药铺学徒，衣角还往下淌着冰冷雨水。\n"
                "学徒喘着气问他怀里是不是揣着那块旧铁牌？\n"
            )
            target_len = 56
            (project / "大纲/细纲_第006章.md").write_text(
                f"# 第六章细纲\n\n- 章节定位：普通推进\n- 字数目标：{target_len} 字\n\n学徒冒雨敲门询问铁牌。\n",
                encoding="utf-8",
            )

            created = run_py(
                CANDIDATE, "init", "--project", str(project), "--chapter", "6",
                "--outline", "大纲/细纲_第006章.md", "--target", "正文/第006章_夜客叩门.md", "--id", "C006",
            )
            run_dir = Path(created.stdout.strip())
            cand_md = run_dir / "candidate.md"

            # Step 1: initial draft check records `draft` snapshot in .lineage/
            cand_md.write_text(ch6_prose_v1, encoding="utf-8")
            run_py(CANDIDATE, "check", "--run", str(run_dir))

            # Step 2: deslop review edits work_copy -> records `deslop` snapshot
            deslop_pkt = json.loads(run_py(CANDIDATE, "review-packet", "--run", str(run_dir), "--kind", "deslop").stdout)
            (project / str(deslop_pkt["work_copy"])).write_text(ch6_prose_v2, encoding="utf-8")
            write_json(base / "deslop1.json", {
                "kind": "deslop", "nonce": deslop_pkt["nonce"], "candidate_sha256": deslop_pkt["candidate_sha256"],
                "scope": deslop_pkt["scope"], "verdict": "CONCERNS",
                "findings": [{"severity": "S3", "category": "口语", "quote": "浑身湿透地喘着粗气", "issue": "微调画面", "action": "edited"}],
                "coverage": ["林川", "学徒", "旧铁牌"], "summary": "一轮去味",
            })
            run_py(CANDIDATE, "attest", "--run", str(run_dir), "--kind", "deslop", "--report", str(base / "deslop1.json"))

            # Step 3: intermediate micro-edit (post_edit) + check -> creates intermediate .lineage/ blob that close() should prune!
            cand_md.write_text(ch6_prose_v2.replace("冰凉", "微凉"), encoding="utf-8")
            run_py(CANDIDATE, "check", "--run", str(run_dir))

            # Step 4: final small polish (<2 char change so deslop stays valid) + check
            cand_md.write_text(ch6_prose_v2.replace("冰凉", "透凉"), encoding="utf-8")
            check_out = json.loads(run_py(CANDIDATE, "check", "--run", str(run_dir)).stdout)
            self.assertEqual(check_out["status"], "pass")
            self.assertTrue((run_dir / "gate-summary.json").is_file())
            self.assertLess((run_dir / "gate-summary.json").stat().st_size, 3072)

            lineage_before = list((run_dir / ".lineage").glob("*.md"))
            self.assertEqual(len(lineage_before), 4)

            cand_mod = load_py_module("cand_mod_test", CANDIDATE)
            loaded_cfg, _ = cand_mod.load_gate_config(project)
            self.assertEqual(loaded_cfg["dialogue"]["max_consecutive_tagged_turns"], 5)

            report_before = json.loads((run_dir / "gate-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report_before["gate_config"], "设定/门禁配置.json")
            self.assertTrue(report_before.get("gates_digest"))
            gates_by_name = {item["name"]: item for item in report_before["gates"]}
            self.assertEqual(gates_by_name["prose_metrics"]["payload"]["style_band"]["status"], "measured")
            self.assertIn("aigc-safe-g5", gates_by_name["prose_metrics"]["payload"]["style_band"]["source"])
            self.assertGreaterEqual(len(gates_by_name["prose_metrics"]["payload"]["style_band"]["checks"]), 14)
            generated_at_before = report_before["generated_at"]

            # Submit consistency review, then verify approve & promote reuse cached gate-report.json (generated_at unchanged!)
            cons_pkt = json.loads(run_py(CANDIDATE, "review-packet", "--run", str(run_dir), "--kind", "consistency").stdout)
            write_json(base / "consistency.json", {
                "kind": "consistency", "nonce": cons_pkt["nonce"], "candidate_sha256": cons_pkt["candidate_sha256"],
                "scope": cons_pkt["scope"], "verdict": "PASS", "findings": [],
                "coverage": ["林川", "学徒", "旧铁牌"], "summary": "一致性通过",
            })
            run_py(CANDIDATE, "attest", "--run", str(run_dir), "--kind", "consistency", "--report", str(base / "consistency.json"))

            time.sleep(0.05)
            run_py(CANDIDATE, "approve", "--run", str(run_dir), "--confirm", "ACCEPT", "--approval-note", "作者确认采用")
            run_py(CANDIDATE, "promote", "--run", str(run_dir), "--confirm", "PROMOTE")
            report_after_promote = json.loads((run_dir / "gate-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report_after_promote["generated_at"], generated_at_before)

            tx6 = {
                "schema_version": 1,
                "mode": "append",
                "chapter": 6,
                "chapter_title": "夜客叩门",
                "expected_state_revision": 0,
                "delta": {
                    "result": "药铺学徒冒雨敲门询问旧铁牌。",
                    "appeared_characters": ["林川"],
                    "character_changes": [],
                    "foreshadow_changes": [],
                    "timeline_events": [],
                    "fact_changes": [],
                    "constraints": [],
                    "next_chapter_commitments": ["听学徒说出追查铁牌之人"],
                },
                "context": {
                    "position": {"volume": "第一卷", "volume_start_chapter": 1, "story_time": "春日雨夜", "scene": "林家小院门前"},
                    "long_term_constraints": [],
                    "active_character_names": ["林川"],
                    "continuity_risks": [],
                },
                "character_snapshots": {},
            }
            write_json(base / "tx6.json", tx6)
            run_py(TRACKING, "commit", "--project", str(project), "--input", str(base / "tx6.json"))
            run_py(CANDIDATE, "close", "--run", str(run_dir))

            # Verify .lineage/ pruned intermediate manual edit (4 -> 3: draft + deslop + final)
            lineage_after = list((run_dir / ".lineage").glob("*.md"))
            self.assertEqual(len(lineage_after), 3)
            self.assertTrue((project / "追踪/质检回执/第006章/gate-summary.json").is_file())

            # Build accepted + golden voice profiles across chapters 1..6
            run_py(
                VOICE, "build", "--project", str(project),
                "--legacy-approved-through", "5", "--confirm", "LEGACY_APPROVED",
            )
            run_py(VOICE, "golden-build", "--project", str(project), "--chapters", "1,2,3,4,5,6", "--confirm", "GOLDEN_APPROVED")
            run_py(DOCTOR, "--project", str(project))

            # Now test zero-ceiling pure-polish fast path on Chapter 6 (100% sentence rewrite!)
            run_py(REVISION, "start-polish", "--project", str(project), "--chapters", "6")
            ch6_path = project / "正文/第006章_夜客叩门.md"

            # Hook allows editing Ch 6, blocks editing Ch 5!
            hook_check = subprocess.run(
                [
                    "node", "-e",
                    "const c = require(process.argv[1]);"
                    "console.log(JSON.stringify({"
                    "ch6: c.revisionBlockReason(process.argv[2], process.argv[3]),"
                    "ch5: c.revisionBlockReason(process.argv[2], process.argv[4]),"
                    "}));",
                    str(HOOK_CORE), str(project), str(ch6_path), str(project / "正文/第005章_归家.md"),
                ],
                text=True, capture_output=True, encoding="utf-8", check=True,
            )
            perm = json.loads(hook_check.stdout)
            self.assertIsNone(perm["ch6"])
            self.assertIn("changed_files", perm["ch5"])

            # Rewrite 100% of sentences in Ch 6; bump mtime +2s over 上下文.md to test continuityFindings parity
            polished_bad = (
                "# 第六章 夜客叩门\n\n"
                "灶膛里的最后一点火星被林川压灭，他反手握紧粗重的门闩。\n"
                "披着湿透麻衣的药铺学徒立在檐下，Hello 雨水顺着发梢往下滴落。\n"
                "那少年喘息未定，开口便问旧铁牌此刻可还在他身上？\n"
            )
            (project / "大纲/细纲_第006章.md")  # untouched
            ch6_path.write_text(polished_bad, encoding="utf-8")
            future_ts = (project / "追踪/上下文.md").stat().st_mtime + 5
            os.utime(ch6_path, (future_ts, future_ts))

            js_cont = subprocess.run(
                ["node", "-e", "const c = require(process.argv[1]); console.log(JSON.stringify(c.continuityFindings(process.argv[2])));", str(HOOK_CORE), str(project)],
                text=True, capture_output=True, encoding="utf-8", check=True,
            )
            py_cont = codex_mod.continuity_findings(project)
            self.assertEqual(json.loads(js_cont.stdout), py_cont)
            self.assertTrue(any("纯润色事务" in msg for msg in py_cont))

            # finish-polish must fail closed on English leak
            run_py(REVISION, "finish-polish", "--project", str(project), expected=2)
            self.assertFalse((project / "追踪/修改影响/active.approved.json").exists())

            # Fix leak (still 100% sentence-rewritten compared to pre-polish Ch 6, matching target word count window)
            polished_clean = (
                "# 第六章 夜客叩门\n\n"
                "灶膛余火被林川压灭，他握紧门后木栓。\n"
                "披湿麻衣的学徒立在檐下，雨水顺发梢滚落。\n"
                "少年喘息未定，追问旧铁牌可还在他身上？\n"
            )
            ch6_path.write_text(polished_clean, encoding="utf-8")
            os.utime(ch6_path, (future_ts + 2, future_ts + 2))
            finish_out = json.loads(run_py(REVISION, "finish-polish", "--project", str(project)).stdout)
            self.assertEqual(finish_out["status"], "PASS")
            self.assertTrue((project / "追踪/修改影响/active.approved.json").is_file())
            self.assertEqual(codex_mod.continuity_findings(project), [])
            run_py(DOCTOR, "--project", str(project))

            # Test Claude session-end.sh & Antigravity stop hook firing .story/hooks/*.sh
            subprocess.run(
                ["bash", str(SESSION_END)],
                cwd=str(project),
                env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
                check=True,
            )
            ag_proc = subprocess.run(
                ["node", str(ANTIGRAVITY_HOOK), "stop"],
                cwd=str(project),
                input=json.dumps({"workspacePaths": [str(project)], "fullyIdle": True, "terminationReason": "model_stop"}),
                text=True, capture_output=True, encoding="utf-8", check=True,
            )
            self.assertEqual(json.loads(ag_proc.stdout), {"decision": "stop"})

            events = [json.loads(line) for line in hook_log.read_text(encoding="utf-8").splitlines() if line.strip()]
            event_names = [item["event"] for item in events]
            self.assertIn("chapter-closed", event_names)
            self.assertIn("polish-finished", event_names)
            self.assertGreaterEqual(event_names.count("stop"), 2)
            self.assertTrue(all(item["author"] == "云海客" for item in events))

            # Test export_book.py (TXT + EPUB3) and zhuque_ledger.py
            export_res = json.loads(
                run_py(PROJECT_HOOKS / "export_book.py", "--project", str(project), "--author", "云海客").stdout
            )
            self.assertEqual(export_res["book_title"], "青石药行")
            self.assertEqual(export_res["chapters"], 6)
            epub_file = Path(export_res["outputs"]["epub"])
            txt_file = Path(export_res["outputs"]["txt"])
            self.assertTrue(epub_file.is_file())
            self.assertTrue(txt_file.is_file())
            self.assertIn("第六章 夜客叩门", txt_file.read_text(encoding="utf-8"))
            with zipfile.ZipFile(epub_file) as zf:
                infos = zf.infolist()
                self.assertEqual(infos[0].filename, "mimetype")
                self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
                self.assertIn("OEBPS/content.opf", zf.namelist())
                self.assertIn("OEBPS/chap0006.xhtml", zf.namelist())

            run_py(PROJECT_HOOKS / "zhuque_ledger.py", "--project", str(project), "add", "--chapter", "6", "--human", "100", "--note", "实测全绿")
            ledger_md = (project / "追踪/朱雀检测/台账.md").read_text(encoding="utf-8")
            self.assertIn("| 6 | 通过 | 100%", ledger_md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
