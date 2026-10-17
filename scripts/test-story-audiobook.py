#!/usr/bin/env python3
"""story-audiobook 回归测试：切分逐字还原、有声稿往返、忠实度校验、选角解析、增量复用与离线合成流程。

方舟调用全部用假函数替换，不联网、不耗额度；拼接成品需要 ffmpeg，缺失时跳过那一项。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "story-audiobook" / "scripts"))
import audiobook  # noqa: E402
import audiobook_core as core  # noqa: E402

TRICKY = [
    "他说：“第一段话，\n“第二段继续。”\n然后他走了。",
    "黄纸贴歪了，“因果”两个字斜斜冲着门口。",
    "【系统提示：绑定成功】他愣住了……真的假的？“【叮】你好。”",
    "“好。”" + "啊" * 400 + "。",
    "没有标点的一段" * 40,
    "## 第1章 开端\n\n“你放的？”李清明捏起黄纸蹭了蹭，“谁放的？”",
]

CHAPTER = """## 第1章 测试章

凌晨一点，便利店的灯管跳了一下。

甲扎着丸子头，正拿漏勺撇浮沫：“学长，问你个事。”

“嗯。”

“你当初为什么报哲学系啊？”

乙把面推进货架深处：“学费最低。”

不对。那里明明只有一卷纸。

电话那头的接线员顿了顿：“先生？患者说什么？”
"""


def reconstruct_ok(text: str) -> bool:
    segs = core.segment_chapter(text)
    for i, p in enumerate(core.paragraph_texts(text)):
        want = re.sub(r"^#{1,6}\s+", "", p)
        if "".join(s.raw for s in segs if s.para == i) != want:
            return False
    return True


class CoreTests(unittest.TestCase):
    def test_segmentation_round_trips(self):
        for text in TRICKY + [CHAPTER]:
            self.assertTrue(reconstruct_ok(text), text[:20])

    def test_chop_respects_limit_and_text(self):
        for text in ("啊" * 400 + "。", "一句话，" * 80, "无标点" * 120):
            parts = core.chop(text)
            self.assertEqual("".join(parts), text)
            self.assertTrue(all(len(p) <= core.LINE_MAX for p in parts))

    def test_brackets_are_own_segments(self):
        segs = core.segment_chapter("【系统提示：绑定成功】他愣住了。")
        self.assertEqual(segs[0].raw, "【系统提示：绑定成功】")

    def _labels(self, segs):
        labels = {}
        for s in segs:
            if s.kind == "quote":
                labels[s.sid] = core.Label("甲" if s.sid % 2 else "乙", "轻快")
            elif s.kind == "narr":
                labels[s.sid] = core.Label("旁白", "" if s.sid % 3 and s.sid > 2 else "平稳")
        return labels

    def test_build_render_parse_keeps_fidelity(self):
        segs = core.segment_chapter(CHAPTER)
        sc = core.build_lines(segs, self._labels(segs), {2: ("便利店", "深夜")})
        self.assertEqual(core.fidelity(CHAPTER, sc), [])
        again = core.parse(core.render(sc))
        self.assertEqual(core.fidelity(CHAPTER, again), [])
        self.assertEqual([l.role for l in again.lines], [l.role for l in sc.lines])
        self.assertEqual(again.lines[0].scene, 0)
        self.assertEqual(again.scenes[1], ("便利店", "深夜"))
        narr = [l for l in sc.lines if l.role == "旁白" and l.scene]
        self.assertTrue(all(l.cue for l in narr), "旁白空指示应沿用上一句")

    def test_quoted_term_read_by_narrator_keeps_quotes(self):
        text = "黄纸贴歪了，“因果”两个字斜斜冲着门口。"
        segs = core.segment_chapter(text)
        sc = core.build_lines(segs, {s.sid: core.Label("旁白", "平稳") for s in segs}, {})
        self.assertEqual(len(sc.lines), 1)
        self.assertEqual(sc.lines[0].text, text)

    def test_missing_quote_label_is_doubtful(self):
        segs = core.segment_chapter("“你好。”")
        sc = core.build_lines(segs, {}, {})
        self.assertTrue(sc.lines[0].doubt)

    def test_fidelity_reports_drops_and_reorders(self):
        segs = core.segment_chapter(CHAPTER)
        sc = core.build_lines(segs, self._labels(segs), {})
        sc.lines[3].text = sc.lines[3].text[2:]
        sc.lines[5], sc.lines[6] = sc.lines[6], sc.lines[5]
        core.renumber(sc)
        issues = core.fidelity(CHAPTER, sc)
        self.assertTrue(issues)
        self.assertTrue(all(i["行"] >= 1 for i in issues))

    def test_parse_labels_is_tolerant(self):
        text = "```\n场｜S2｜便利店｜深夜\nS3|甲|轻快\nS4｜?乙｜愣住\nS5|旁白|\n新|路人·接线员|女|青年\n```"
        labels, scenes, new = core.parse_labels(text)
        self.assertEqual(labels[3].role, "甲")
        self.assertTrue(labels[4].doubt)
        self.assertEqual(labels[4].role, "乙")
        self.assertEqual(labels[5].cue, "")
        self.assertEqual(scenes[2], ("便利店", "深夜"))
        self.assertEqual(new["路人·接线员"], ("女", "青年"))

    def test_resolve_variants_and_pools(self):
        cast = {"角色": {"旁白": {"voice": "v0"}, "甲": {"voice": "zh_female_qinqienv_uranus_bigtts"},
                         "路人·接线员": {"voice": "v2", "自动": True}}}
        self.assertEqual(core.resolve(cast, "路人·接线员·电话")["fx"], "电话")
        self.assertEqual(core.resolve(cast, "甲·心声")["fx"], "心声")
        with self.assertRaises(KeyError):
            core.resolve(cast, "丙")
        self.assertEqual(core.pick_pool_voice(cast, "女青年"), "zh_female_tiexinnvsheng_uranus_bigtts")
        self.assertEqual(core.pool_category("女", "老人"), "女老年")

    def test_pause_rules(self):
        a = core.Line("旁白", "", "他说：", 1, scene=1)
        b = core.Line("甲", "", "好。", 1, scene=1)
        c = core.Line("乙", "", "嗯。", 2, scene=1)
        d = core.Line("旁白", "", "第二天。", 3, scene=2)
        t = core.Line("旁白", "", "第1章", 0, scene=0)
        self.assertEqual(core.pause_ms(a, b), 240)
        self.assertEqual(core.pause_ms(b, c), 580)
        self.assertEqual(core.pause_ms(c, d), 1200)
        self.assertEqual(core.pause_ms(t, a), 1000)

    def test_instruction_and_cache_key(self):
        self.assertIn("旁白", core.instruction("旁白", "压低"))
        self.assertIn("心里", core.instruction("甲·心声", "疑惑"))
        self.assertIsNone(core.instruction("甲", ""))
        k1 = core.cache_key("v", "x", "文本", None, {})
        self.assertNotEqual(k1, core.cache_key("v", "y", "文本", None, {}))
        self.assertNotEqual(k1, core.cache_key("v", "x", "文本", "电话", {}))

    def test_reuse_labels_keeps_unchanged_paragraphs(self):
        segs = core.segment_chapter(CHAPTER)
        old = core.build_lines(segs, self._labels(segs), {2: ("便利店", "深夜")})
        old.lines[2].role = "甲·心声"            # 作者手改过的声部要被保留
        new_src = CHAPTER.replace("不对。那里明明只有一卷纸。", "不对。那里明明只有两卷纸。")
        new_segs = core.segment_chapter(new_src)
        labels, scenes, reused = core.reuse_labels(core.parse(core.render(old)), new_src, new_segs)
        changed = next(i for i, p in enumerate(core.paragraph_texts(new_src)) if "两卷" in p)
        self.assertNotIn(changed, reused)
        self.assertEqual(len(reused), len(core.paragraph_texts(new_src)) - 1)
        self.assertIn("甲·心声", {l.role for l in labels.values()})
        self.assertTrue(scenes)


def fake_chat_factory(calls):
    def fake_chat(prompt, system=None, **_):
        calls.append(prompt)
        out = []
        for sid, kind, text in re.findall(r"^S(\d+) \[(标题|叙述|对白)\] (.*)$", prompt, re.M):
            sid = int(sid)
            if kind == "对白":
                role = "路人·接线员" if "先生" in text else ("甲" if "学长" in text or "哲学" in text else "乙")
                out.append(f"S{sid}|{role}|自然")
            elif kind == "叙述" and text.startswith("不对"):
                out.append(f"S{sid}|乙·心声|疑惑")
            else:
                out.append(f"S{sid}|旁白|平稳")
        first = re.search(r"^S(\d+) \[(?:叙述|对白)\]", prompt, re.M)
        if first:
            out.insert(0, f"场|S{first.group(1)}|便利店|深夜")
        out.append("新|路人·接线员|女|青年")
        return "\n".join(out), {"prompt_tokens": 100, "completion_tokens": 50}
    return fake_chat


def fake_tts_factory(calls):
    def fake_tts(text, speaker, ctx=None, section=None, params=None, **_):
        calls.append((text, speaker, ctx))
        n = int(audiobook.RATE * 0.05 * max(1, len(text)))
        tone = b"".join(int(8000 * math.sin(i / 8)).to_bytes(2, "little", signed=True) for i in range(64))
        return (tone * (n // 64 + 1))[:2 * n], {"text_words": len(text)}
    return fake_tts


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="audiobook-")
        self.root = Path(self.tmp.name) / "书"
        (self.root / "正文").mkdir(parents=True)
        (self.root / "设定" / "角色").mkdir(parents=True)
        for name, voice in (("甲", "轻快自然"), ("乙", "话少短句")):
            (self.root / "设定" / "角色" / f"{name}.md").write_text(
                f"# {name}\n\n- 角色定位：配角\n- 声线：{voice}\n", encoding="utf-8")
        self.chapter = self.root / "正文" / "第001章_测试章.md"
        self.write_chapter(CHAPTER)
        self.chat_calls, self.tts_calls = [], []
        self.saved = (audiobook.ark.chat, audiobook.ark.tts_retry, audiobook.ark.load_key)
        audiobook.ark.chat = fake_chat_factory(self.chat_calls)
        audiobook.ark.tts_retry = fake_tts_factory(self.tts_calls)
        audiobook.ark.load_key = lambda: "fake-key"

    def tearDown(self):
        audiobook.ark.chat, audiobook.ark.tts_retry, audiobook.ark.load_key = self.saved
        self.tmp.cleanup()

    def write_chapter(self, text):
        self.chapter.write_text(text, encoding="utf-8")
        rec = self.root / "追踪" / "章节提交" / "第001章.json"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(json.dumps({"status": "committed", "chapter": 1,
                                   "accepted_prose_sha256": hashlib.sha256(self.chapter.read_bytes()).hexdigest()}),
                       encoding="utf-8")

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = audiobook.main(["--book", str(self.root), *argv])
        return rc, out.getvalue()

    def lock_cast(self):
        rc, out = self.run_cli("cast", "lock", "旁白=zh_male_xuanyijieshuo_uranus_bigtts",
                               "甲=zh_female_linjianvhai_uranus_bigtts", "乙=zh_male_m191_uranus_bigtts")
        self.assertEqual(rc, 0, out)

    def test_uncommitted_chapter_is_refused(self):
        (self.root / "追踪" / "章节提交" / "第001章.json").unlink()
        self.lock_cast()
        rc, out = self.run_cli("annotate", "1")
        self.assertEqual(rc, 1)
        self.assertIn("定稿", out)
        rc, out = self.run_cli("annotate", "1", "--allow-uncommitted")
        self.assertEqual(rc, 0, out)

    def test_legacy_chapters_count_as_committed(self):
        (self.root / "追踪" / "章节提交" / "第001章.json").unlink()
        state = self.root / "追踪" / "_tracking-state.json"
        state.write_text(json.dumps({"schema_version": 5, "last_committed_chapter": 1}), encoding="utf-8")
        self.assertTrue(audiobook.committed_state(audiobook.Book(self.root), 1)[0])
        state.write_text(json.dumps({"schema_version": 5, "last_committed_chapter": 0}), encoding="utf-8")
        self.assertFalse(audiobook.committed_state(audiobook.Book(self.root), 1)[0])

    def test_relock_needs_flag(self):
        self.lock_cast()
        rc, out = self.run_cli("cast", "lock", "甲=zh_female_qinqienv_uranus_bigtts")
        self.assertEqual(rc, 1)
        self.assertIn("--relock", out)

    def test_annotate_check_and_incremental_reuse(self):
        self.lock_cast()
        rc, out = self.run_cli("annotate", "1")
        self.assertEqual(rc, 0, out)
        sc = core.parse((self.root / "有声书" / "有声稿" / "第001章.有声稿.md").read_text(encoding="utf-8"))
        roles = {l.role for l in sc.lines}
        self.assertTrue({"旁白", "甲", "乙", "乙·心声", "路人·接线员"} <= roles, roles)
        cast = json.loads((self.root / "有声书" / "选角.json").read_text(encoding="utf-8"))
        self.assertTrue(cast["角色"]["路人·接线员"]["自动"])
        rc, out = self.run_cli("check", "1")
        self.assertEqual(rc, 0, out)
        self.write_chapter(CHAPTER.replace("只有一卷纸", "只有两卷纸"))
        rc, out = self.run_cli("check", "1")
        self.assertEqual(rc, 1)
        self.chat_calls.clear()
        rc, out = self.run_cli("annotate", "1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.chat_calls), 1)
        self.assertIn("两卷纸", self.chat_calls[0])
        self.assertNotIn("学长", self.chat_calls[0].split("# 待标注片段")[1])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_synth_uses_cache_and_rerolls(self):
        self.lock_cast()
        rc, out = self.run_cli("make", "1")
        self.assertEqual(rc, 0, out)
        base = self.root / "有声书" / "成品" / "第001章_测试章"
        for ext in (".mp3", ".srt", ".json"):
            self.assertTrue(Path(str(base) + ext).exists(), ext)
        manifest = json.loads(Path(str(base) + ".json").read_text(encoding="utf-8"))
        n_lines = len(manifest["行"])
        self.assertEqual(len(self.tts_calls), n_lines)
        self.assertIn("成品 最新", self.run_cli("status")[1])
        self.tts_calls.clear()
        rc, out = self.run_cli("synth", "1", "--reroll", "2")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.tts_calls), 1)
        rc, out = self.run_cli("synth", "1", "--dry-run")
        self.assertIn("需新合成 0 行", out)
        self.run_cli("cast", "set", "甲", "--voice", "zh_female_qinqienv_uranus_bigtts", "--relock")
        self.assertIn("成品 过期", self.run_cli("status")[1])


if __name__ == "__main__":
    unittest.main(verbosity=1)
