#!/usr/bin/env python3
"""story-setup 可选模型分工部署的回归测试：按角色开关、幂等、关闭后恢复标准写手、保留作者改过的配置。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "skills" / "story-setup" / "scripts" / "deploy-model-routing.py"
TEMPLATES = REPO / "skills" / "story-setup" / "references" / "templates"


def deploy(project: Path, roles: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(HELPER), "deploy", "--project", str(project), "--roles", roles],
        capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class DeployModelRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="model-routing-")
        self.project = Path(self.temporary.name)
        agents = self.project / ".claude" / "agents"
        agents.mkdir(parents=True)
        for template in (TEMPLATES / "agents").glob("*.md"):
            (agents / template.name).write_bytes(template.read_bytes())
        self.agents = agents
        self.models = self.project / ".story" / "models"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def config(self) -> dict[str, object]:
        return json.loads((self.models / "config.json").read_text(encoding="utf-8"))

    def test_enable_all_roles_is_idempotent(self) -> None:
        result = deploy(self.project, "writer,deslop,reader")
        self.assertEqual(result["enabled"], ["writer", "deslop", "reader"])
        writer = (self.agents / "narrative-writer.md").read_text(encoding="utf-8")
        self.assertIn("oh-story-managed", writer)
        self.assertIn("run-narrative-writer.sh", writer)
        prompt = (self.models / "narrative-writer-prompt.md").read_text(encoding="utf-8")
        self.assertTrue(prompt.startswith("# 运行环境说明"))
        self.assertNotIn("\nname: narrative-writer", prompt)  # 标准模板的 frontmatter 已剥掉
        self.assertIn("name: deslop-reviewer", (self.agents / "deslop-reviewer.md").read_text(encoding="utf-8"))
        self.assertTrue((self.models / "deslop-reviewer-rubric.md").is_file())
        cfg = self.config()
        self.assertTrue(all(cfg[role]["enabled"] for role in ("writer", "deslop", "reader")))
        self.assertEqual(cfg["deslop"]["effort"], "medium")
        self.assertEqual(cfg["reader"]["full_prior_chapters"], 3)
        for script in ("run-narrative-writer.sh", "run-deslop-review.py", "run-reader-review.py", "ark_client.py"):
            self.assertTrue((self.models / script).stat().st_mode & 0o100, script)
        before = snapshot(self.project)
        deploy(self.project, "writer,deslop,reader")
        self.assertEqual(before, snapshot(self.project))

    def test_legacy_claude_models_config_is_migrated_on_first_deploy(self) -> None:
        legacy_dir = self.project / ".claude" / "models"
        legacy_dir.mkdir(parents=True)
        legacy_cfg = {
            "deslop": {"enabled": False, "model": "doubao-custom-legacy", "effort": "low"},
            "reader": {"enabled": True, "full_prior_chapters": 2, "lanes": {"opus": {"effort": "high"}}},
        }
        (legacy_dir / "config.json").write_text(json.dumps(legacy_cfg, ensure_ascii=False), encoding="utf-8")
        deploy(self.project, "deslop")
        migrated = self.config()
        self.assertEqual(migrated["deslop"]["model"], "doubao-custom-legacy")
        self.assertEqual(migrated["deslop"]["effort"], "low")
        self.assertTrue(migrated["deslop"]["enabled"])
        self.assertFalse(migrated["reader"]["enabled"])
        self.assertEqual(migrated["reader"]["full_prior_chapters"], 2)
        self.assertEqual(migrated["reader"]["lanes"]["opus"]["effort"], "high")

    def test_antigravity_only_project_deploys_without_claude_agents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="model-routing-agy-") as tmp:
            proj = Path(tmp)
            agy_writer = proj / ".agents" / "agents" / "narrative-writer" / "agent.md"
            agy_writer.parent.mkdir(parents=True)
            agy_writer.write_text("---\nsubagent: true\n---\n原生 Antigravity 写手\n", encoding="utf-8")
            (proj / ".story-deployed").write_text("target_cli: antigravity\nagents_version: 50\n", encoding="utf-8")
            result = deploy(proj, "deslop,reader")
            self.assertEqual(result["enabled"], ["deslop", "reader"])
            self.assertTrue((proj / ".story" / "models" / "run-deslop-review.py").is_file())
            self.assertTrue((proj / ".story" / "models" / "deslop-reviewer-rubric.md").is_file())
            self.assertFalse((proj / ".claude" / "agents").exists())
            self.assertIn("原生 Antigravity 写手", agy_writer.read_text(encoding="utf-8"))

    def test_deslop_system_prompt_is_compact_injects_context_and_excludes_skill_md(self) -> None:
        import importlib.util

        deploy(self.project, "deslop")
        (self.project / "设定").mkdir(parents=True)
        (self.project / "设定" / "文风.md").write_text("# 本书文风\n- 短段推进，零破折号。\n", encoding="utf-8")
        (self.project / "设定" / "题材正文提示卡.md").write_text("# 题材卡\n- 高武学府日常与战斗并重。\n", encoding="utf-8")
        (self.project / "追踪").mkdir(parents=True)
        (self.project / "追踪" / "上下文.md").write_text(
            "# 写作连续性上下文\n## 核心角色状态\n- 苏幼薇｜已晋升二阶凝脉境玄冰凰体\n", encoding="utf-8"
        )
        skill_dir = self.project / ".agents" / "skills" / "story-deslop"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("SKILL_MD_SHOULD_NOT_BE_INJECTED\n" * 1800, encoding="utf-8")

        spec = importlib.util.spec_from_file_location("run_deslop_review", self.models / "run-deslop-review.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        system = mod.build_system(self.project)
        byte_len = len(system.encode("utf-8"))
        self.assertLessEqual(byte_len, 22_000, f"system.md too large: {byte_len} bytes")
        self.assertIn("苏幼薇｜已晋升二阶凝脉境玄冰凰体", system)
        self.assertIn("短段推进，零破折号", system)
        self.assertNotIn("SKILL_MD_SHOULD_NOT_BE_INJECTED", system)

        runs_dir = self.models / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        for idx in range(35):
            d = runs_dir / f"done-{idx:03d}"
            d.mkdir()
            marker = d / "exit_code"
            marker.write_text("0\n", encoding="utf-8")
        for idx in range(2):
            active = runs_dir / f"active-{idx:02d}"
            active.mkdir()
            (active / "task.md").write_text("running\n", encoding="utf-8")
        mod.prune_runs(runs_dir, keep=30)
        remaining = [p.name for p in runs_dir.iterdir() if p.is_dir()]
        self.assertEqual(len([n for n in remaining if n.startswith("done-")]), 30)
        self.assertEqual(len([n for n in remaining if n.startswith("active-")]), 2)

    def test_disabling_restores_the_standard_writer_and_removes_the_reviewer(self) -> None:
        deploy(self.project, "writer,deslop")
        deploy(self.project, "")
        standard = (TEMPLATES / "agents" / "narrative-writer.md").read_bytes()
        self.assertEqual((self.agents / "narrative-writer.md").read_bytes(), standard)
        self.assertFalse((self.agents / "deslop-reviewer.md").exists())
        self.assertFalse(any(self.config()[role]["enabled"] for role in ("writer", "deslop", "reader")))

    def test_author_edits_to_the_config_survive_redeploys(self) -> None:
        deploy(self.project, "reader")
        config = self.config()
        config["reader"]["lanes"]["opus"]["effort"] = "high"
        config["deslop"]["model"] = "自定义模型"
        (self.models / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        deploy(self.project, "deslop,reader")
        kept = self.config()
        self.assertEqual(kept["reader"]["lanes"]["opus"]["effort"], "high")
        self.assertEqual(kept["deslop"]["model"], "自定义模型")
        self.assertTrue(kept["deslop"]["enabled"])
        self.assertFalse(kept["writer"]["enabled"])

    def test_unmanaged_reviewer_file_is_left_alone(self) -> None:
        (self.agents / "deslop-reviewer.md").write_text("---\nname: deslop-reviewer\n---\n作者自己的版本\n", encoding="utf-8")
        deploy(self.project, "")
        self.assertTrue((self.agents / "deslop-reviewer.md").exists())

    def test_unknown_role_is_rejected(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(HELPER), "deploy", "--project", str(self.project), "--roles", "writer,editor"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("未知角色", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
