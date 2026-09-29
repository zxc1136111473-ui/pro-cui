"""Gemini 音乐（Lyria）节点：经 geminiweb 的 OpenAI 兼容 chat 端点出音乐，不需要 Suno 的 key。

geminiweb 的 gemini-music 模型把生成的 MP3 以 data:audio/mpeg;base64 内嵌在回复正文里。
地址和 Key 取自 Relay API Settings 节点的 info 输出（与 04 文生视频共用同一个 geminiweb Key）。
"""
import base64
import json
import re

import requests
from comfy_api_nodes.util import audio_bytes_to_audio_input  # type: ignore[reportMissingImports]

AUDIO_RE = re.compile(r"data:audio/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=]+)")
DEFAULT_BASE = "http://airelay-geminiweb:8083"


class ProGeminiMusic:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "创作一段30秒的轻快电子流行纯音乐，适合短视频配乐"}),
                "model": ("STRING", {"default": "gemini-music"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF, "control_after_generate": True}),
            },
            "optional": {"info": ("STRING", {"default": "", "forceInput": True})},
        }

    RETURN_TYPES = ("AUDIO", "STRING")
    RETURN_NAMES = ("audio", "response")
    FUNCTION = "generate"
    CATEGORY = "pro/audio"

    def generate(self, prompt, model, seed, info=""):
        cfg = {}
        if info and info.strip():
            try:
                cfg = json.loads(info)
            except Exception:
                pass
        key = (cfg.get("apikey") or "").strip()
        base = (cfg.get("custom_api_base") or cfg.get("api_base") or "").strip().rstrip("/")
        if "runninghub" in base or not base:  # 模板里 api_base 是占位的公网地址，别拿来当 geminiweb 地址
            base = DEFAULT_BASE
        if not key:
            raise RuntimeError("[Gemini 音乐] 没有 API Key：请在 Relay API Settings 的 apikey 填 geminiweb 的 API Key（与 04 文生视频相同）")
        model = model.strip() or "gemini-music"
        try:
            resp = requests.post(
                base + "/v1/chat/completions",
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                timeout=300,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"[Gemini 音乐] 连不上 {base}：{e}")
        if resp.status_code != 200:
            raise RuntimeError(f"[Gemini 音乐] HTTP {resp.status_code}：{resp.text[:400]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except Exception:
            raise RuntimeError(f"[Gemini 音乐] 返回格式不对：{resp.text[:400]}")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        m = AUDIO_RE.search(content)
        if not m:
            raise RuntimeError("[Gemini 音乐] 回复里没有音频（可能被拒绝或额度用尽）：" + AUDIO_RE.sub("<音频>", content)[:400])
        audio = audio_bytes_to_audio_input(base64.b64decode(m.group(1)))
        text = AUDIO_RE.sub("<音频>", content).strip()
        return (audio, json.dumps({"code": "success", "text": text[:1000]}, ensure_ascii=False))


NODE_CLASS_MAPPINGS = {"ProGeminiMusic": ProGeminiMusic}
NODE_DISPLAY_NAME_MAPPINGS = {"ProGeminiMusic": "Gemini 音乐（geminiweb）"}
