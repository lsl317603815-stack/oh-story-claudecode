#!/usr/bin/env python3
"""模型分工的方舟客户端：经火山方舟 Agent Plan / Coding Plan（OpenAI 兼容）调用豆包、GLM、DeepSeek、Kimi 等模型出文本。

由 story-setup 在启用模型分工时部署到项目 .claude/models/，供去AI味审查与读者审读调用。

套餐由 --plan 或环境变量 ARK_PLAN 选择，默认 agent：
  agent   https://ark.cn-beijing.volces.com/api/plan/v3    key 条目 ARK_AGENT_PLAN_API_KEY
  coding  https://ark.cn-beijing.volces.com/api/coding/v3  key 条目 ARK_API_KEY
key 只从同名环境变量或 macOS 钥匙串（service=条目名）读取，绝不打印。

用法：
  ark_client.py [--plan agent|coding] models          # 列出可见模型（Agent Plan 数据面可能不提供列表）
  ark_client.py run -p prompt.txt -o out.md          # 默认 doubao-seed-2-1-pro-260915，思考 medium
  ark_client.py run -p prompt.txt -o out.md -m 模型名 [-s system.txt] [-e low|medium|high] [--no-thinking]
  cat prompt.txt | ark_client.py run -o out.md

服务端返回的实际模型与请求不一致时（套餐不支持而被自动替换）直接报错退出，不写输出。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

PLANS = {
    "agent": ("https://ark.cn-beijing.volces.com/api/plan/v3", "ARK_AGENT_PLAN_API_KEY"),
    "coding": ("https://ark.cn-beijing.volces.com/api/coding/v3", "ARK_API_KEY"),
}
DEFAULT_MODEL = "doubao-seed-2-1-pro-260915"


def load_key(key_name):
    key = os.environ.get(key_name, "")
    if not key:
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", key_name, "-w"],
                               capture_output=True, text=True, timeout=10)
            key = r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            key = ""
    if not key:
        sys.exit(f"{key_name} 不在环境变量也不在钥匙串：请用户自行执行 "
                 f"security add-generic-password -U -s {key_name} -a $USER -w（agent 不得读取或写入 key）")
    return key


def call(method, url, key, payload=None, timeout=600):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}：{e.read().decode(errors='replace')[:1500]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", choices=sorted(PLANS), default=os.environ.get("ARK_PLAN", "agent"),
                    help="方舟套餐，默认 agent（也可用环境变量 ARK_PLAN）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models")
    r = sub.add_parser("run")
    r.add_argument("-p", "--prompt", help="用户 prompt 文件；省略则读 stdin")
    r.add_argument("-s", "--system", help="system prompt 文件")
    r.add_argument("-o", "--out", required=True, help="正文输出文件")
    r.add_argument("-m", "--model", default=DEFAULT_MODEL)
    r.add_argument("-e", "--effort", choices=["low", "medium", "high"], default="medium",
                   help="思考档位（thinking 开启时生效）")
    r.add_argument("--no-thinking", action="store_true", help="关闭思考")
    r.add_argument("-t", "--temperature", type=float)
    r.add_argument("--max-tokens", type=int, default=16000)
    r.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()

    BASE_URL, key_name = PLANS[a.plan]
    key = load_key(key_name)
    if a.cmd == "models":
        for m in sorted(x["id"] for x in call("GET", f"{BASE_URL}/models", key).get("data", [])):
            print(m)
        return

    prompt = open(a.prompt, encoding="utf-8").read() if a.prompt else sys.stdin.read()
    msgs = []
    if a.system:
        msgs.append({"role": "system", "content": open(a.system, encoding="utf-8").read()})
    msgs.append({"role": "user", "content": prompt})
    payload = {"model": a.model, "messages": msgs, "max_tokens": a.max_tokens, "stream": False}
    if a.no_thinking:
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = a.effort
    if a.temperature is not None:
        payload["temperature"] = a.temperature
    resp = call("POST", f"{BASE_URL}/chat/completions", key, payload, timeout=a.timeout)
    try:
        msg = resp["choices"][0]["message"]
        text = msg["content"]
    except (KeyError, IndexError, TypeError):
        sys.exit("返回无正文：" + json.dumps(resp, ensure_ascii=False)[:1500])
    if not text:
        sys.exit(f"正文为空（finish_reason={resp['choices'][0].get('finish_reason')}）："
                 "多半是思考耗尽 max_tokens，调大 --max-tokens 或降 -e")
    served = str(resp.get("model") or "")
    base = lambda m: re.sub(r"-\d{6}$", "", m.strip().lower())
    if a.model != "auto" and served and not (base(served).startswith(base(a.model))
                                              or base(a.model).startswith(base(served))):
        sys.exit(f"实际模型与请求不一致：请求 {a.model}，服务端返回 {served}（套餐可能不支持该模型而被自动替换）；未写入输出")
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    u = resp.get("usage") or {}
    reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    print(f"已写入 {a.out}（{len(text)} 字符；model={resp.get('model', a.model)}；"
          f"tokens in/out={u.get('prompt_tokens')}/{u.get('completion_tokens')}"
          + (f"，其中思考 {reasoning}" if reasoning else "") +
          f"；finish={resp['choices'][0].get('finish_reason')}）")


if __name__ == "__main__":
    main()
