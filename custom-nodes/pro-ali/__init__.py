"""阿里云百炼（DashScope）节点：生图 / 改图 / 配音。

Key 与地址取自 Relay API Settings 节点的 info 输出（存在服务器 relay_config.json 的 node_settings[节点 id]，不进 git）。
只允许把 Key 发往 *.aliyuncs.com，Settings 里地址配错时宁可报错也不外发。
错误直接抛出（红色节点 + 阿里返回的原因），不重试：重试会重复计费。
"""
import base64
import io
import json
from urllib.parse import urlparse

import numpy as np
import requests
import torch
from PIL import Image
from comfy_api_nodes.util import audio_bytes_to_audio_input  # type: ignore[reportMissingImports]

DEFAULT_BASE = "https://dashscope.aliyuncs.com"
GEN_API = "/api/v1/services/aigc/multimodal-generation/generation"
IMAGE_TIMEOUT = 200   # qwen-image-3.0 实测约 66 秒，2048 大图约 40 秒
TTS_TIMEOUT = 90      # 实测 2.5~6 秒
DOWNLOAD_TIMEOUT = 60

# 只放实测过可用的：音色逐个用 1 个字合成验证过（50 个全部可用）
VOICES = ["Cherry", "Serena", "Ethan", "Chelsie", "Momo", "Vivian", "Moon", "Maia", "Kai", "Nofish", "Bella", "Jennifer",
          "Ryan", "Katerina", "Aiden", "Eldric Sage", "Mia", "Mochi", "Bellona", "Vincent", "Bunny", "Neil", "Elias",
          "Arthur", "Nini", "Ebona", "Seren", "Pip", "Stella", "Bodega", "Sonrisa", "Alek", "Dolce", "Sohee", "Ono Anna",
          "Lenn", "Emilien", "Andre", "Radio Gol", "Jada", "Dylan", "Li", "Marcus", "Roy", "Peter", "Sunny", "Eric",
          "Rocky", "Kiki"]
# 首字母大写：instruct 模型只认大写（Chinese），小写会 400；flash 两种都认
LANGS = ["Chinese", "English", "Japanese", "Korean", "German", "French", "Spanish", "Italian", "Portuguese", "Russian", "Auto"]
IMAGE_MODELS = ["qwen-image-2.0-pro", "qwen-image-3.0"]
EDIT_MODELS = ["qwen-image-edit-max"]
TTS_MODELS = ["qwen3-tts-flash", "qwen3-tts-instruct-flash"]
# 比例 × 档位 → 宽*高（1K≈1 百万像素，2K 最长边 2048）
RATIOS = {"1:1": (1, 1), "3:4": (3, 4), "4:3": (4, 3), "9:16": (9, 16), "16:9": (16, 9), "2:3": (2, 3), "3:2": (3, 2)}


def _size(ratio, level):
    w, h = RATIOS[ratio]
    if level == "2K":
        k = 2048 / max(w, h)
    else:
        k = (1024 * 1024 / (w * h)) ** 0.5
    return f"{int(round(w * k / 16) * 16)}*{int(round(h * k / 16) * 16)}"


def _creds(info):
    cfg = {}
    if info and info.strip():
        try:
            cfg = json.loads(info)
        except Exception:
            pass
    key = (cfg.get("apikey") or "").strip()
    if not key:
        raise RuntimeError("[阿里] 没有 API Key：请在 Relay API Settings 的 apikey 填阿里百炼的 Key（或在服务器 relay_config.json 里预置）")
    raw = (cfg.get("custom_api_base") or "").strip() or DEFAULT_BASE
    u = urlparse(raw)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not (host == "aliyuncs.com" or host.endswith(".aliyuncs.com")):
        raise RuntimeError(f"[阿里] 地址必须是 https 的 *.aliyuncs.com（现在是 {raw}）：Key 只发往阿里")
    return key, f"https://{host}"


def _post(base, key, body, timeout, what):
    try:
        r = requests.post(base + GEN_API, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                          json=body, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"[阿里 {what}] 连不上：{type(e).__name__}")
    try:
        d = r.json()
    except Exception:
        raise RuntimeError(f"[阿里 {what}] HTTP {r.status_code}，返回不是 JSON：{r.text[:200]}")
    if r.status_code != 200:
        raise RuntimeError(f"[阿里 {what}] HTTP {r.status_code} {d.get('code', '')}：{d.get('message', '')[:300]}")
    return d


def _download(url, what):
    try:
        r = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"[阿里 {what}] 下载结果失败：{type(e).__name__}")
    return r.content


def _to_tensor(img_bytes):
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    return torch.from_numpy(np.asarray(im).astype(np.float32) / 255.0)[None,]


def _to_data_url(tensor):
    arr = (tensor[0].cpu().numpy().clip(0, 1) * 255).astype(np.uint8)
    im = Image.fromarray(arr)
    im.thumbnail((2048, 2048))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _image_from(d, what):
    try:
        for c in d["output"]["choices"][0]["message"]["content"]:
            if c.get("image"):
                return c["image"]
    except Exception:
        pass
    raise RuntimeError(f"[阿里 {what}] 返回里没有图片：{json.dumps(d, ensure_ascii=False)[:300]}")


class ProAliImage:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "prompt": ("STRING", {"multiline": True, "default": "一杯冰美式咖啡放在木桌上，自然光，产品摄影"}),
            "model": (IMAGE_MODELS, {"default": "qwen-image-2.0-pro"}),
            "ratio": (list(RATIOS), {"default": "3:4"}),
            "level": (["1K", "2K"], {"default": "1K"}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 2147483646, "control_after_generate": True})},
            "optional": {"info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "response")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, prompt, model, ratio, level, seed, info=""):
        key, base = _creds(info)
        if model == "qwen-image-3.0" and level == "2K":
            # 实测 3.0 出 2K 会超过 200 秒超时（1K 约 66 秒），超时也照样计费，所以直接拦住
            raise RuntimeError("[阿里 生图] qwen-image-3.0 目前只用 1K（2K 实测超时）；要 2K 请选 qwen-image-2.0-pro")
        size = _size(ratio, level)
        d = _post(base, key, {"model": model, "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
                              "parameters": {"size": size, "watermark": False, "seed": seed}}, IMAGE_TIMEOUT, "生图")
        data = _download(_image_from(d, "生图"), "生图")
        return (_to_tensor(data), json.dumps({"code": "success", "model": model, "size": size, "usage": d.get("usage")}, ensure_ascii=False))


class ProAliImageEdit:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image1": ("IMAGE",),
            "prompt": ("STRING", {"multiline": True, "default": "请编辑这张商品照片：把原来的背景完全去掉，换成干净、无缝的纯白色（#FFFFFF）影棚背景；商品本身保持原样（形状、细节、颜色、角度都不要改），在商品下方加柔和自然的接触阴影；专业电商产品图。"}),
            "model": (EDIT_MODELS, {"default": "qwen-image-edit-max"}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 2147483646, "control_after_generate": True})},
            "optional": {"image2": ("IMAGE",), "image3": ("IMAGE",), "info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "response")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, image1, prompt, model, seed, image2=None, image3=None, info=""):
        key, base = _creds(info)
        content = [{"image": _to_data_url(t)} for t in (image1, image2, image3) if t is not None] + [{"text": prompt}]
        d = _post(base, key, {"model": model, "input": {"messages": [{"role": "user", "content": content}]},
                              "parameters": {"watermark": False, "seed": seed}}, IMAGE_TIMEOUT, "改图")
        data = _download(_image_from(d, "改图"), "改图")
        return (_to_tensor(data), json.dumps({"code": "success", "model": model, "usage": d.get("usage")}, ensure_ascii=False))


class ProAliTTS:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "text": ("STRING", {"multiline": True, "default": "夏日清凉节，全场满一百九十九减五十，限时三天，欢迎选购。"}),
            "voice": (VOICES, {"default": "Cherry"}),
            "model": (TTS_MODELS, {"default": "qwen3-tts-flash"}),
            "language": (LANGS, {"default": "Chinese"})},
            "optional": {"instructions": ("STRING", {"multiline": True, "default": "", "placeholder": "仅 instruct 模型有效，例如：语气热情洪亮，语速偏快，像直播间主播带货"}),
                         "info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("AUDIO", "STRING")
    RETURN_NAMES = ("audio", "response")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, text, voice, model, language, instructions="", info=""):
        key, base = _creds(info)
        if not text.strip():
            raise RuntimeError("[阿里 配音] 文字是空的")
        inp = {"text": text, "voice": voice, "language_type": language}
        if model == "qwen3-tts-instruct-flash" and instructions.strip():
            inp["instructions"] = instructions.strip()
            inp["optimize_instructions"] = True
        d = _post(base, key, {"model": model, "input": inp}, TTS_TIMEOUT, "配音")
        try:
            url = d["output"]["audio"]["url"]
        except Exception:
            raise RuntimeError("[阿里 配音] 返回里没有音频：" + json.dumps(d, ensure_ascii=False)[:300])
        audio = audio_bytes_to_audio_input(_download(url, "配音"))
        return (audio, json.dumps({"code": "success", "model": model, "voice": voice, "usage": d.get("usage")}, ensure_ascii=False))


NODE_CLASS_MAPPINGS = {"ProAliImage": ProAliImage, "ProAliImageEdit": ProAliImageEdit, "ProAliTTS": ProAliTTS}
NODE_DISPLAY_NAME_MAPPINGS = {"ProAliImage": "阿里 文生图（qwen-image，支持 2K）", "ProAliImageEdit": "阿里 改图（qwen-image-edit）",
                              "ProAliTTS": "阿里 配音（qwen3-tts）"}
