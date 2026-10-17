"""火山方舟 Agent Plan 的两个调用：文本模型（标注、选角）与豆包语音合成 2.0。

key 只从环境变量或 macOS 钥匙串条目 ARK_AGENT_PLAN_API_KEY 读取，绝不打印。
语音合成走 Agent Plan 专属地址，资源 ID 固定 seed-tts-2.0；账号需在方舟控制台为语音模型开启「抵扣」。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from typing import Optional, Tuple

KEY_NAME = "ARK_AGENT_PLAN_API_KEY"
CHAT_URL = "https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions"
TTS_URL = "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
TTS_RESOURCE = "seed-tts-2.0"
DEFAULT_TEXT_MODEL = "doubao-seed-2-1-pro-260915"
AFP_PER_10K_CHARS = 1350        # 方舟 Agent Plan：doubao-seed-tts-2.0 每万字符抵扣 1350 AFP


class ArkError(RuntimeError):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def load_key() -> str:
    key = os.environ.get(KEY_NAME, "")
    if not key:
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", KEY_NAME, "-w"],
                               capture_output=True, text=True, timeout=10)
            key = r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            key = ""
    if not key:
        raise ArkError(f"找不到方舟 Agent Plan key：请设置环境变量 {KEY_NAME}，或在 macOS 执行 "
                       f"security add-generic-password -U -s {KEY_NAME} -a $USER -w（由用户本人输入）", fatal=True)
    return key


def has_key() -> bool:
    try:
        load_key()
        return True
    except ArkError:
        return False


def chat(prompt: str, system: Optional[str] = None, model: str = DEFAULT_TEXT_MODEL, effort: str = "low",
         max_tokens: int = 16000, timeout: int = 600, key: Optional[str] = None) -> Tuple[str, dict]:
    """调用方舟文本模型，返回 (正文, usage)。服务端把模型替换成别的时直接报错。"""
    key = key or load_key()
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    payload = {"model": model, "messages": msgs, "max_tokens": max_tokens, "stream": False,
               "thinking": {"type": "enabled"}, "reasoning_effort": effort}
    req = urllib.request.Request(CHAT_URL, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:800]
        raise ArkError(f"文本模型 HTTP {e.code}：{body}", fatal=e.code in (401, 403))
    except (urllib.error.URLError, TimeoutError) as e:
        raise ArkError(f"文本模型网络错误：{e}")
    try:
        text = resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ArkError("文本模型返回无正文：" + json.dumps(resp, ensure_ascii=False)[:600])
    if not text:
        raise ArkError("文本模型正文为空（多半是思考耗尽 max_tokens）")
    served = str(resp.get("model") or "")
    base = lambda m: re.sub(r"-\d{6}$", "", m.strip().lower())
    if served and not (base(served).startswith(base(model)) or base(model).startswith(base(served))):
        raise ArkError(f"实际模型与请求不一致：请求 {model}，服务端返回 {served}", fatal=True)
    return text, resp.get("usage") or {}


_FATAL_TTS = ("DeductNotEnabled", "not granted", "permission denied", "Unauthorized", "invalid api key",
              "resource ID is mismatched")


def tts(text: str, speaker: str, ctx: Optional[str] = None, section: Optional[str] = None,
        params: Optional[dict] = None, fmt: str = "pcm", rate: int = 24000, timeout: int = 180,
        key: Optional[str] = None) -> Tuple[bytes, dict]:
    """合成一句，返回 (音频字节, usage)。fmt=pcm 时是 16-bit 单声道裸 PCM。"""
    key = key or load_key()
    additions = {"disable_markdown_filter": True}
    if ctx:
        additions["context_texts"] = [ctx]
    if section:
        additions["section_id"] = section
    params = dict(params or {})
    pitch = params.pop("pitch", None)
    if pitch:
        additions["post_process"] = {"pitch": int(pitch)}
    audio_params = {"format": fmt, "sample_rate": rate}
    audio_params.update({k: int(v) for k, v in params.items() if k in ("speech_rate", "loudness_rate")})
    body = {"user": {"uid": "oh-story"}, "req_params": {
        "text": text, "speaker": speaker, "audio_params": audio_params,
        "additions": json.dumps(additions, ensure_ascii=False)}}
    req = urllib.request.Request(TTS_URL, data=json.dumps(body).encode(), method="POST", headers={
        "X-Api-Key": key, "X-Api-Resource-Id": TTS_RESOURCE, "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Control-Require-Usage-Tokens-Return": "*", "Content-Type": "application/json"})
    audio, usage = bytearray(), {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                raw = raw.strip()
                if not raw:
                    continue
                d = json.loads(raw)
                code = d.get("code", 0)
                if code == 0 and d.get("data"):
                    audio.extend(base64.b64decode(d["data"]))
                elif code == 20000000:
                    usage = d.get("usage") or {}
                    break
                elif code:
                    msg = f"{code} {d.get('message', '')}"
                    raise ArkError("语音合成失败：" + msg, fatal=any(k in msg for k in _FATAL_TTS))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")[:600]
        fatal = e.code in (401, 403) or any(k in body_text for k in _FATAL_TTS)
        raise ArkError(f"语音合成 HTTP {e.code}：{body_text}", fatal=fatal)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ArkError(f"语音合成网络错误：{e}")
    if not audio:
        raise ArkError("语音合成没有返回音频")
    return bytes(audio), usage


def tts_retry(*args, attempts: int = 4, **kwargs) -> Tuple[bytes, dict]:
    """可重试错误（限流、网络、5xx）指数退避；致命错误（开关未开、未授权、音色无权限）立即抛出。"""
    last: Optional[ArkError] = None
    for i in range(attempts):
        try:
            return tts(*args, **kwargs)
        except ArkError as e:
            if e.fatal:
                raise
            last = e
            time.sleep(min(2 ** i, 8))
    raise last  # type: ignore[misc]


def explain(e: ArkError) -> str:
    msg = str(e)
    if "DeductNotEnabled" in msg:
        return (msg + "\n→ 账号还没为语音模型开启 Agent Plan「抵扣」：到方舟控制台「开通管理 → Agent Plan」的语音模型卡片上打开抵扣"
                "（只开抵扣即可，超额后付费不必开），然后重试。")
    if "not granted" in msg:
        return msg + "\n→ 当前套餐没有这个资源（例如声音复刻 seed-icl-2.0 不在 Agent Plan 内）。"
    if "permission denied" in msg or "mismatched" in msg:
        return msg + "\n→ 音色 ID 不属于豆包语音合成 2.0：检查选角表里的 voice 是否来自 references/voices-seed-tts-2.0.json。"
    return msg
