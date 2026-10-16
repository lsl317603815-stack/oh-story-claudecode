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
        self.models = self.project / ".claude" / "models"

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
        self.assertTrue(all(self.config()[role]["enabled"] for role in ("writer", "deslop", "reader")))
        for script in ("run-narrative-writer.sh", "run-deslop-review.py", "run-reader-review.py", "ark_client.py"):
            self.assertTrue((self.models / script).stat().st_mode & 0o100, script)
        before = snapshot(self.project)
        deploy(self.project, "writer,deslop,reader")
        self.assertEqual(before, snapshot(self.project))

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
