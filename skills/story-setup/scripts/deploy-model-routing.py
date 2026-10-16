#!/usr/bin/env python3
"""部署或调整项目的模型分工（可选，仅 Claude Code）。

三个角色可以分别开关：
  writer  narrative-writer 改成转发器，正文写作交给 agy（Antigravity CLI）上的模型
  deslop  接纳前去AI味审查与 /审查 的文字审查交给方舟上的模型（新增 deslop-reviewer agent）
  reader  逐章读者审读（单车道或多读者团），只作建议，不卡定稿

用法：
  deploy-model-routing.py check                                  只读检查本机依赖，输出 JSON
  deploy-model-routing.py deploy --project 项目根 --roles writer,deslop,reader
  deploy-model-routing.py deploy --project 项目根 --roles ""      全部关闭，恢复标准写手与审查

deploy 必须在 story-setup 的 Step 5（部署标准 agents）之后执行：它会用转发器覆盖 narrative-writer，
关闭 writer 时再把标准版写回。.claude/models/config.json 只在不存在时创建，之后只改各角色的 enabled，
作者手动改过的模型、车道等字段保留。重复执行结果一致。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROLES = ("writer", "deslop", "reader")
TEMPLATES = Path(__file__).resolve().parent.parent / "references" / "templates"
MODELS_SRC = TEMPLATES / "models"
MANAGED_MARK = "oh-story-managed"
# 部署到 .claude/models/ 的受管文件；config.json 与 runs/ 属于项目状态，不在此列
MANAGED_FILES = (
    "ark_client.py", "run-narrative-writer.sh", "run-deslop-review.py", "run-reader-review.py",
    "narrative-writer.preamble.md", "deslop-reviewer.system.md", "deslop-auditor.system.md",
    "reader-review.system.md", "reader-persona.example.md",
)


def keychain_has(name):
    if os.environ.get(name):
        return True
    if sys.platform != "darwin" or not shutil.which("security"):
        return False
    # 只查条目是否存在（不带 -w），不读取 key 内容
    return subprocess.run(["security", "find-generic-password", "-s", name],
                          capture_output=True).returncode == 0


def check():
    report = {
        "python3": bool(shutil.which("python3")),
        "agy": bool(shutil.which("agy")),
        "codex": bool(shutil.which("codex")),
        "claude": bool(shutil.which("claude")),
        "ark_agent_plan_key": keychain_has("ARK_AGENT_PLAN_API_KEY"),
        "ark_coding_plan_key": keychain_has("ARK_API_KEY"),
    }
    report["roles_ready"] = {
        "writer": report["agy"],
        "deslop": report["python3"] and (report["ark_agent_plan_key"] or report["ark_coding_plan_key"]),
        "reader": report["python3"] and report["claude"],
    }
    report["notes"] = [
        "writer 需要已登录的 agy；deslop 需要方舟 Agent Plan（或 Coding Plan）key 存在钥匙串或环境变量；",
        "reader 的单车道与总控默认走 claude 命令行，读者团里 gpt 车道需要 codex，豆包/kimi/glm/deepseek 车道需要方舟 key。",
    ]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def strip_frontmatter(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def write_if_changed(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)


def deploy(project, roles):
    project = project.resolve()
    claude = project / ".claude"
    if not (claude / "agents").is_dir():
        sys.exit("错误：项目还没有部署 .claude/agents（先完成 story-setup Step 5）")
    models = claude / "models"
    models.mkdir(parents=True, exist_ok=True)
    for name in MANAGED_FILES:
        target = models / name
        write_if_changed(target, (MODELS_SRC / name).read_bytes())
        if name.endswith((".py", ".sh")):
            target.chmod(0o755)

    config_path = models / "config.json"
    config = json.loads((config_path if config_path.exists() else MODELS_SRC / "config.default.json")
                        .read_text(encoding="utf-8"))
    for role in ROLES:
        config.setdefault(role, json.loads((MODELS_SRC / "config.default.json").read_text(encoding="utf-8"))[role])
        config[role]["enabled"] = role in roles
    write_if_changed(config_path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    agents = claude / "agents"
    standard_writer = (TEMPLATES / "agents" / "narrative-writer.md").read_bytes()
    if "writer" in roles:
        prompt = (MODELS_SRC / "narrative-writer.preamble.md").read_text(encoding="utf-8") + \
            strip_frontmatter(standard_writer.decode("utf-8"))
        write_if_changed(models / "narrative-writer-prompt.md", prompt.encode("utf-8"))
        write_if_changed(agents / "narrative-writer.md", (MODELS_SRC / "agents" / "narrative-writer-forwarder.md").read_bytes())
    else:
        write_if_changed(agents / "narrative-writer.md", standard_writer)

    reviewer = agents / "deslop-reviewer.md"
    if "deslop" in roles:
        write_if_changed(reviewer, (MODELS_SRC / "agents" / "deslop-reviewer.md").read_bytes())
    elif reviewer.exists() and MANAGED_MARK in reviewer.read_text(encoding="utf-8"):
        reviewer.unlink()

    print(json.dumps({"project": str(project), "enabled": [r for r in ROLES if r in roles],
                      "config": str(config_path.relative_to(project))}, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(description="部署或调整项目的模型分工")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    d = sub.add_parser("deploy")
    d.add_argument("--project", required=True, type=Path)
    d.add_argument("--roles", required=True, help="逗号分隔：writer,deslop,reader；空字符串表示全部关闭")
    a = ap.parse_args()
    if a.cmd == "check":
        return check()
    roles = [r.strip() for r in a.roles.split(",") if r.strip()]
    unknown = [r for r in roles if r not in ROLES]
    if unknown:
        sys.exit(f"错误：未知角色 {unknown}；可选 {', '.join(ROLES)}")
    return deploy(a.project, set(roles))


if __name__ == "__main__":
    sys.exit(main())
