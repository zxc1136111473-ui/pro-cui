"""阿里云百炼（DashScope）节点：生图 / 改图 / 配音（含多角色对白）/ 听写 / 写提示词 / 改视频 / 视频换人。

Key 与地址取自 Relay API Settings 节点的 info 输出（存在服务器 relay_config.json 的 node_settings[节点 id]，不进 git）。
只允许把 Key 发往 *.aliyuncs.com，Settings 里地址配错时宁可报错也不外发。
「写提示词」选谷歌（gemini）模型时走网关：Key 和地址取自「Key：网关」节点（gateway_info 输入），只发往那个节点配置的网关地址，阿里的 Key 不会发给网关。
错误直接抛出（红色节点 + 阿里返回的原因）。计费请求（生图 / 改图 / 配音 / 听写 / 音色）只发一次、不重试：重试会重复计费。
视频类（改视频 / 视频换人）是异步任务：先把视频 / 人像传到阿里的免费临时存储（oss:// 地址），再提交任务（只发一次）、隔几秒查一次、下载结果；失败的任务阿里不收费。
连接阶段单独处理：发请求前先用一个免费的 HEAD 把 TCP + TLS 连接建好（建不好就重试，这时什么都没发出去，不会计费），请求走这条已经连好的连接；
结果下载是 GET，失败重试。写提示词的文字请求几乎不花钱，连不上（含超时）整个请求重试一次。
"""
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from urllib.parse import urlparse

import av
import numpy as np
import requests
import torch
import torchaudio.functional as taf
from PIL import Image
from comfy_api.latest import InputImpl  # type: ignore[reportMissingImports]
from comfy_api_nodes.util import audio_bytes_to_audio_input  # type: ignore[reportMissingImports]

DEFAULT_BASE = "https://dashscope.aliyuncs.com"
GEN_API = "/api/v1/services/aigc/multimodal-generation/generation"
VOICE_API = "/api/v1/services/audio/tts/customization"  # 音色设计 / 声音克隆（创建自定义音色）
CUSTOM_VOICE_MODELS = {"qwen-tts-vd-": "qwen3-tts-vd-2026-01-26", "qwen-tts-vc-": "qwen3-tts-vc-2026-01-22"}  # 音色 id 前缀 → 合成模型
IMAGE_TIMEOUT = 200   # qwen-image-3.0 实测约 66 秒，2048 大图约 40 秒
TTS_TIMEOUT = 90      # 实测 2.5~6 秒
ASR_TIMEOUT = 90      # 实测 6 秒音频约 4.5 秒
WRITER_TIMEOUT = 60   # 关掉思考后实测约 3 秒（开着约 12~20 秒）
WRITER_MODEL = "qwen3.8-flash"
# 「写提示词」能选的文字模型：2026-10-10 逐个用本节点的请求体在服务器上试过（一句话需求 → 扩写），都能用，2~4 秒；glm-5.3 只能开思考（本节点关着思考），用不了。
# qwen3.8-omni-flash 能看图（「看图写文案」/ 图生视频要用它或同类），其余只能读文字
# 谷歌（gemini）的三个走网关（Key：网关，gateway_info 输入）：2026-10-11 用本节点的请求体在服务器上试过，文字和看图都行，但比阿里慢（文字 4~45 秒，看图 10~170 秒，偶尔更久），超时放宽到 120 秒。
# 没放进来的：gemini-3.1-pro-thinking（两分钟不回）、gemini-3.6-flash（十次里卡住了两次）、gemini-3.5-flash-lite-thinking（比 flash-lite 慢得多）、gemini-canvas（是做网页的，回 502）
GATEWAY_WRITER_MODELS = ["gemini-3.5-flash-lite", "gemini-3.6-flash-thinking", "gemini-3.1-pro"]
WRITER_MODELS = [WRITER_MODEL, "qwen3.8-max", "qwen3.7-plus", "qwen3.8-omni-flash", "deepseek-v4-pro", "kimi-k3"] + GATEWAY_WRITER_MODELS
GATEWAY_WRITER_TIMEOUT = 120      # 文字 5~45 秒；带图片的更慢也更不稳（实测 10 秒到 170 秒不等：第一次 90 秒没回、重试那次又花了近 80 秒），所以放宽到 120 秒
CHAT_API = "/compatible-mode/v1/chat/completions"
GATEWAY_CHAT_API = "/v1/chat/completions"             # 网关（New API）的 OpenAI 兼容地址，没有 /compatible-mode
_CITE = re.compile(r"\s*\[cite:[^\]]*\]")               # 谷歌网页版的回复里夹着「[cite: 1]」这样的引用标记，不是正文，要去掉（会混进提示词里）
ASR_MAX_SECONDS = 300  # 阿里 qwen3-asr-flash 单次上限约 5 分钟
DOWNLOAD_TIMEOUT = 60
CONNECT_TIMEOUT = 3        # 连接 / 握手阶段的超时（正常不到 1 秒）：德国 → 北京的线路偶尔握手卡 10~25 秒后被重置，干等没用，换条新连接多半马上就好
WARMUP_TRIES = 4           # 预热连接最多试几次
POST_CONNECT_TIMEOUT = 10  # 请求自己（预热过的连接断了要重连时）的连接超时：线路彻底不通时别干等整个超时

# 只放实测过可用的：音色逐个用 1 个字合成验证过（49 个全部可用）
VOICES = ["Cherry", "Serena", "Ethan", "Chelsie", "Momo", "Vivian", "Moon", "Maia", "Kai", "Nofish", "Bella", "Jennifer",
          "Ryan", "Katerina", "Aiden", "Eldric Sage", "Mia", "Mochi", "Bellona", "Vincent", "Bunny", "Neil", "Elias",
          "Arthur", "Nini", "Ebona", "Seren", "Pip", "Stella", "Bodega", "Sonrisa", "Alek", "Dolce", "Sohee", "Ono Anna",
          "Lenn", "Emilien", "Andre", "Radio Gol", "Jada", "Dylan", "Li", "Marcus", "Roy", "Peter", "Sunny", "Eric",
          "Rocky", "Kiki"]
# 每个音色的 (性别, 特点)，根据官方《Qwen-TTS 音色列表》（help.aliyun.com/zh/model-studio/qwen-tts-voice-list）概括；方言音色选「中文」语种时说的就是那种方言。
# Ebona 官方表里没有（实测能合成），不知道性别和风格。多角色对白的音色下拉里把这些写在音色名后面，小白和 AI 助手都能照着挑。
VOICE_INFO = {
    "Cherry": ("女", "阳光亲切"), "Serena": ("女", "温柔"), "Ethan": ("男", "朝气温暖，带北方口音"), "Chelsie": ("女", "二次元虚拟女友"),
    "Momo": ("女", "撒娇搞怪"), "Vivian": ("女", "拽拽的小暴躁"), "Moon": ("男", "率性帅气"), "Maia": ("女", "知性温柔"),
    "Kai": ("男", "舒缓放松"), "Nofish": ("男", "不卷舌的设计师"), "Bella": ("女", "萌萌的小萝莉"), "Jennifer": ("女", "电影感美式女声"),
    "Ryan": ("男", "节奏感强、戏感足"), "Katerina": ("女", "御姐，韵律感强"), "Aiden": ("男", "美式大男孩"), "Eldric Sage": ("男", "沉稳睿智的老者"),
    "Mia": ("女", "温顺乖巧"), "Mochi": ("男", "聪明的童真小大人"), "Bellona": ("女", "洪亮清晰、江湖气"), "Vincent": ("男", "沙哑烟嗓、豪情"),
    "Bunny": ("女", "萌系小萝莉"), "Neil": ("男", "新闻主播式播报"), "Elias": ("女", "讲解知识、条理清晰"), "Arthur": ("男", "质朴的乡村老人"),
    "Nini": ("女", "软糯甜美"), "Ebona": ("未知", "官方表没收录，不知道风格"), "Seren": ("女", "温和舒缓、助眠"), "Pip": ("男", "调皮的小男孩"),
    "Stella": ("女", "甜美迷糊的少女"), "Bodega": ("男", "热情的西班牙大叔"), "Sonrisa": ("女", "开朗热情的拉美大姐"), "Alek": ("男", "冷峻中带暖意"),
    "Dolce": ("男", "慵懒的意大利大叔"), "Sohee": ("女", "情绪丰富的韩国姐姐"), "Ono Anna": ("女", "鬼灵精怪的青梅竹马"), "Lenn": ("男", "理性中带叛逆"),
    "Emilien": ("男", "浪漫的法国大哥哥"), "Andre": ("男", "磁性沉稳"), "Radio Gol": ("男", "足球解说风格"),
    "Jada": ("女", "上海话，风风火火的阿姐"), "Dylan": ("男", "北京话，胡同长大的少年"), "Li": ("男", "南京话，耐心的瑜伽老师"),
    "Marcus": ("男", "陕西话，朴实厚重"), "Roy": ("男", "闽南语，诙谐直爽"), "Peter": ("男", "天津话，相声捧哏"),
    "Sunny": ("女", "四川话，甜美川妹子"), "Eric": ("男", "四川话，市井的成都男子"), "Rocky": ("男", "粤语，幽默风趣"), "Kiki": ("女", "粤语，甜美港妹"),
}
VOICE_LABELS = [f"{v} · {VOICE_INFO[v][0]} · {VOICE_INFO[v][1]}" for v in VOICES]
# 首字母大写：instruct 模型只认大写（Chinese），小写会 400；flash 两种都认
LANGS = ["Chinese", "English", "Japanese", "Korean", "German", "French", "Spanish", "Italian", "Portuguese", "Russian", "Auto"]
# 阿里出图模型：2026-10-10~11 在服务器上逐个试过，纯文字出图和「带商品图当素材」都能用，出图尺寸听话。
# 1K 约要：2.0 4~26 秒、2.1-pro 15~30 秒、2.0-pro 约 47 秒、3.0 约 35~66 秒、3.0-pro 约 46~83 秒；2K 只有 2.0-pro / 2.1-pro 试过（约 40 秒）。
IMAGE_MODELS = ["qwen-image-2.0-pro", "qwen-image-2.1-pro", "qwen-image-2.0", "qwen-image-3.0", "qwen-image-3.0-pro"]
# 改图 / 以图为素材出图：同一批模型，请求里多带图。原来用的 qwen-image-edit-max 阿里已经下线（返回 Model not exist）
EDIT_MODELS = IMAGE_MODELS
MODELS_2K = {"qwen-image-2.0-pro", "qwen-image-2.1-pro"}      # 2K 试过能出的；3.x 的 2K 实测会超过 200 秒超时（超时也照样计费），2.0 的 2K 没试过
SAME_AS_INPUT = "跟原图一样"
TTS_MODELS = ["qwen3-tts-flash", "qwen3-tts-instruct-flash"]
ASR_LANGS = ["auto", "zh", "en", "ja", "ko"]
# 比例 × 档位 → 宽*高（1K≈1 百万像素，2K 最长边 2048）
RATIOS = {"1:1": (1, 1), "3:4": (3, 4), "4:3": (4, 3), "9:16": (9, 16), "16:9": (16, 9), "2:3": (2, 3), "3:2": (3, 2)}


def _size(ratio, level):
    w, h = RATIOS[ratio]
    if level == "2K":
        k = 2048 / max(w, h)
    else:
        k = (1024 * 1024 / (w * h)) ** 0.5
    return f"{int(round(w * k / 16) * 16)}*{int(round(h * k / 16) * 16)}"


def _check_level(model, level, what):
    """不能出 2K 的模型选了 2K：直接拦住，不发请求（超时也照样计费）。"""
    if level == "2K" and model not in MODELS_2K:
        raise RuntimeError(f"[阿里 {what}] {model} 目前只用 1K（3.x 的 2K 实测超时，2.0 的 2K 没试过）；要 2K 请选 qwen-image-2.0-pro 或 qwen-image-2.1-pro")


def _closest_ratio(width, height):
    """宽高 → 最接近的常用比例（RATIOS 里的一个）。"""
    r = width / max(height, 1)
    return min(RATIOS, key=lambda k: abs(math.log(r / (RATIOS[k][0] / RATIOS[k][1]))))


def _creds(info):
    cfg = {}
    if info and info.strip():
        try:
            cfg = json.loads(info)
        except Exception:
            pass
    key = (cfg.get("apikey") or "").strip()
    if not key:
        raise RuntimeError("[阿里] 没有 API Key：请点开画布上标题为「Key：阿里」的节点（折叠着的，点标题左边的圆点展开；类型 Relay API Settings），在 apikey 填阿里百炼的 Key（或在服务器 relay_config.json 里预置）")
    raw = (cfg.get("custom_api_base") or "").strip() or DEFAULT_BASE
    u = urlparse(raw)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not (host == "aliyuncs.com" or host.endswith(".aliyuncs.com")):
        raise RuntimeError(f"[阿里] 地址必须是 https 的 *.aliyuncs.com（现在是 {raw}）：Key 只发往阿里")
    return key, f"https://{host}"


def _gateway_creds(info, model):
    """网关（New API）的 Key 和地址：取自「Key：网关」节点的 info（apikey / api_base）。地址由那个节点配置（一般是同一台服务器上的 http://airelay-newapi:3000），只要求是 http(s)。"""
    cfg = {}
    if info and info.strip():
        try:
            cfg = json.loads(info)
        except Exception:
            pass
    key = str(cfg.get("apikey") or "").strip()
    if not key:
        raise RuntimeError(f"[网关 写提示词] 选了谷歌模型「{model}」，要走网关，但没有拿到网关的 Key：请点开标题为「Key：网关」的节点（折叠着的，点标题左边的圆点展开；类型 Relay API Settings），"
                           "在 apikey 填网关的 Key（或在服务器 relay_config.json 里预置）；工作流里没有这个节点的话，换成阿里的模型")
    raw = str(cfg.get("api_base") or "").strip().rstrip("/")
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise RuntimeError(f"[网关 写提示词] 网关地址不对（现在是 {raw or '空的'}）：「Key：网关」节点里的 custom_api_base 要填 http(s):// 开头的地址")
    return key, raw


def _open(base, what, who="阿里"):
    """开一个会话，并先对同一个主机发一个免费的 HEAD 把 TCP + TLS 连接建好（建不好就重试）。
    这时还没有任何计费请求发出去，所以连接阶段的失败可以放心重试；之后的请求走这条连好的连接（keep-alive），不会再卡在握手上。
    HEAD 不带 Key，返回什么状态码都行（阿里网关的 / 是 404）：只要连上、握手成功。"""
    s = requests.Session()
    last = None
    for _ in range(WARMUP_TRIES):
        try:
            s.head(base + "/", timeout=(CONNECT_TIMEOUT, 6))
            return s
        except requests.RequestException as e:
            last = e
    s.close()
    raise RuntimeError(f"[{who} {what}] 连不上：{type(last).__name__}（连接 / 握手试了 {WARMUP_TRIES} 次都没成功，请求没有发出，不会计费）")


def _send(base, key, path, body, timeout, what, who="阿里", headers=None):
    """（预热连接后）把 JSON 发给阿里（who="网关" 时发给网关），只发一次，返回 Response；连不上 / 等回复超时抛 RuntimeError。headers = 要多带的请求头。"""
    s = _open(base, what, who)
    try:
        return s.post(base + path, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", **(headers or {})},
                      json=body, timeout=(POST_CONNECT_TIMEOUT, timeout))
    except requests.ReadTimeout:
        raise RuntimeError(f"[{who} {what}] 等了 {timeout} 秒没有回应：请求已经发出，{who}那边可能还在处理（可能已计费），先别连着重跑")
    except requests.RequestException as e:
        raise RuntimeError(f"[{who} {what}] 连不上：{type(e).__name__}")
    finally:
        s.close()


def _post(base, key, body, timeout, what, path=GEN_API):
    r = _send(base, key, path, body, timeout, what)
    try:
        d = r.json()
    except Exception:
        raise RuntimeError(f"[阿里 {what}] HTTP {r.status_code}，返回不是 JSON：{r.text[:200]}")
    if r.status_code != 200:
        raise RuntimeError(f"[阿里 {what}] HTTP {r.status_code} {d.get('code', '')}：{d.get('message', '')[:300]}")
    return d


def _download(url, what):
    """下载生成结果（GET，可以放心重试：结果已经生成并计费了，下载失败不重试就白花钱）。4xx（链接过期之类）重试没用，直接报。"""
    last = None
    for _ in range(3):
        try:
            r = requests.get(url, timeout=(CONNECT_TIMEOUT * 2, DOWNLOAD_TIMEOUT))
            r.raise_for_status()
            return r.content
        except requests.HTTPError as e:
            last = e
            if e.response is not None and e.response.status_code < 500:
                break
        except requests.RequestException as e:
            last = e
    raise RuntimeError(f"[阿里 {what}] 下载结果失败：{type(last).__name__}")


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
        _check_level(model, level, "生图")
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
            "model": (EDIT_MODELS, {"default": "qwen-image-2.0-pro"}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 2147483646, "control_after_generate": True})},
            "optional": {"image2": ("IMAGE",), "image3": ("IMAGE",), "info": ("STRING", {"default": "", "forceInput": True}),
                         "ratio": ([SAME_AS_INPUT] + list(RATIOS), {"default": SAME_AS_INPUT, "tooltip": "出图的比例；默认跟第一张图一样（取最接近的常用比例）"}),
                         "level": (["1K", "2K"], {"default": "1K", "tooltip": "1K 约 100 万像素；2K 只有 qwen-image-2.0-pro / 2.1-pro 能用"}),
                         # 「出图引擎」模式节点把选中的模型名送进来（一个下拉管整个应用里所有的阿里出图节点）：接了就以它为准，没接就用上面的「模型」
                         "use_model": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "response")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, image1, prompt, model, seed, image2=None, image3=None, info="", ratio=SAME_AS_INPUT, level="1K", use_model=""):
        key, base = _creds(info)
        model = (use_model or "").strip() or model
        if model not in EDIT_MODELS:
            raise RuntimeError(f"[阿里 改图] 不认识的模型「{model}」：可选 {'、'.join(EDIT_MODELS)}")
        _check_level(model, level, "改图")
        if ratio not in RATIOS:                                        # 「跟原图一样」：按第一张图的宽高取最接近的常用比例
            try:
                ratio = _closest_ratio(int(image1.shape[2]), int(image1.shape[1]))
            except Exception:
                ratio = "1:1"
        size = _size(ratio, level)
        content = [{"image": _to_data_url(t)} for t in (image1, image2, image3) if t is not None] + [{"text": prompt}]
        d = _post(base, key, {"model": model, "input": {"messages": [{"role": "user", "content": content}]},
                              "parameters": {"watermark": False, "seed": seed, "size": size}}, IMAGE_TIMEOUT, "改图")
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
                         "speed": ("FLOAT", {"default": 1.0, "min": 0.5, "max": 2.0, "step": 0.05, "tooltip": "语速倍数，本地变速不变调（阿里接口本身没有语速参数）"}),
                         "volume_db": ("FLOAT", {"default": 0.0, "min": -20.0, "max": 12.0, "step": 0.5, "tooltip": "音量增减 dB，本地处理"}),
                         "custom_voice": ("STRING", {"default": "", "tooltip": "自定义音色 id（音色设计 / 声音克隆节点输出）；填了就忽略上面的音色和模型"}),
                         "info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("AUDIO", "STRING")
    RETURN_NAMES = ("audio", "response")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, text, voice, model, language, instructions="", speed=1.0, volume_db=0.0, custom_voice="", info=""):
        key, base = _creds(info)
        if not text.strip():
            raise RuntimeError("[阿里 配音] 文字是空的")
        custom = custom_voice.strip()
        if custom:
            model = next((m for pre, m in CUSTOM_VOICE_MODELS.items() if custom.startswith(pre)), None)
            if not model:
                raise RuntimeError("[阿里 配音] 自定义音色 id 应以 qwen-tts-vd- 或 qwen-tts-vc- 开头")
            voice = custom
        raw, d = _tts_request(base, key, text, voice, model, language, instructions)
        audio = _adjust(audio_bytes_to_audio_input(raw), speed, volume_db)
        return (audio, json.dumps({"code": "success", "model": model, "voice": voice, "usage": d.get("usage")}, ensure_ascii=False))


def _tts_request(base, key, text, voice, model, language, instructions=""):
    """合成一段文字：只发一次（计费），返回 (音频原始字节, 阿里的返回)。语气指令只有 instruct 模型才带。"""
    inp = {"text": text, "voice": voice, "language_type": language}
    if model == "qwen3-tts-instruct-flash" and instructions.strip():
        inp["instructions"] = instructions.strip()
        inp["optimize_instructions"] = True
    d = _post(base, key, {"model": model, "input": inp}, TTS_TIMEOUT, "配音")
    try:
        url = d["output"]["audio"]["url"]
    except Exception:
        raise RuntimeError("[阿里 配音] 返回里没有音频：" + json.dumps(d, ensure_ascii=False)[:300])
    try:
        return _download(url, "配音"), d
    except RuntimeError as e:
        raise RuntimeError(f"{e}（这一句已经合成成功、可能已经计费；重新运行会再合成一次）") from e


def _adjust(audio, speed, volume_db):
    """本地调语速（PyAV atempo，变速不变调）和音量；两项都是默认值时原样返回。"""
    w = audio["waveform"]
    sr = int(audio["sample_rate"])
    if abs(speed - 1.0) > 1e-3:
        import av
        c = w.shape[1]
        layout = "mono" if c == 1 else "stereo"
        g = av.filter.Graph()
        src = g.add_abuffer(format="fltp", sample_rate=sr, layout=layout, time_base=f"1/{sr}")
        sink = g.add("abuffersink")
        tempo = g.add("atempo", f"{speed}")
        src.link_to(tempo)
        tempo.link_to(sink)
        g.configure()
        frame = av.AudioFrame.from_ndarray(w[0].cpu().numpy().astype(np.float32), format="fltp", layout=layout)
        frame.sample_rate, frame.pts = sr, 0
        src.push(frame)
        src.push(None)
        parts = []
        while True:
            try:
                parts.append(sink.pull().to_ndarray())
            except (av.error.EOFError, av.error.BlockingIOError):
                break
        w = torch.from_numpy(np.concatenate(parts, axis=1))[None]
    if abs(volume_db) > 1e-3:
        w = (w * 10 ** (volume_db / 20)).clamp(-1, 1)
    return {"waveform": w, "sample_rate": sr}


def _audio_to_wav16k(audio, sr_out=16000):
    """ComfyUI AUDIO → 16kHz 单声道 16bit wav 字节（比原始音频小得多，传到阿里更快）"""
    w = audio["waveform"]
    w = (w[0] if w.dim() == 3 else w).float().mean(dim=0, keepdim=True)  # [1,T]
    sr = int(audio["sample_rate"])
    if sr != sr_out:
        w = taf.resample(w, sr, sr_out)
    pcm = (w[0].cpu().numpy().clip(-1, 1) * 32767).astype(np.int16)
    if len(pcm) > ASR_MAX_SECONDS * sr_out:
        raise RuntimeError(f"[阿里 语音识别] 音频超过 {ASR_MAX_SECONDS} 秒，请先截短")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr_out)
        f.writeframes(pcm.tobytes())
    return buf.getvalue()


class ProAliASR:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",), "language": (ASR_LANGS, {"default": "auto"})},
                "optional": {"info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "response")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, audio, language, info=""):
        key, base = _creds(info)
        wav = _audio_to_wav16k(audio)
        body = {"model": "qwen3-asr-flash", "stream": False,
                "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "data:audio/wav;base64," + base64.b64encode(wav).decode()}}]}]}
        if language != "auto":
            body["asr_options"] = {"language": language}
        r = _send(base, key, CHAT_API, body, ASR_TIMEOUT, "语音识别")
        try:
            d = r.json()
        except Exception:
            raise RuntimeError(f"[阿里 语音识别] HTTP {r.status_code}，返回不是 JSON：{r.text[:200]}")
        if r.status_code != 200:
            err = d.get("error", d)
            raise RuntimeError(f"[阿里 语音识别] HTTP {r.status_code} {err.get('code', '')}：{str(err.get('message', ''))[:300]}")
        try:
            text = d["choices"][0]["message"]["content"].strip()
        except Exception:
            raise RuntimeError("[阿里 语音识别] 返回里没有文字：" + json.dumps(d, ensure_ascii=False)[:300])
        return (text, json.dumps({"code": "success", "seconds": (d.get("usage") or {}).get("seconds")}, ensure_ascii=False))


def _chat(base, key, body, timeout, what, path=CHAT_API, who="阿里"):
    """OpenAI 兼容的 chat/completions（默认发给阿里；谷歌模型传 path=GATEWAY_CHAT_API、who="网关"）。连不上（含超时）整个请求重试一次（文字请求几乎不花钱），HTTP 错误不重试；返回解析后的 JSON。"""
    for attempt in (1, 2):
        try:
            r = _send(base, key, path, body, timeout, what, who)
            break
        except RuntimeError:
            if attempt == 2:
                raise
    try:
        d = r.json()
    except Exception:
        raise RuntimeError(f"[{who} {what}] HTTP {r.status_code}，返回不是 JSON：{r.text[:200]}")
    if r.status_code != 200:
        err = d.get("error", d)
        if not isinstance(err, dict):
            err = {"message": err}
        raise RuntimeError(f"[{who} {what}] HTTP {r.status_code} {err.get('code') or ''}：{str(err.get('message', ''))[:300]}")
    return d


class ProAliPromptWriter:
    """一句话需求 → 文字模型按「扩写指令」写成完整提示词（生图 / 视频 / 改图 / 配乐 / 音色描述 / 写文案 / 翻译…，指令不同而已）。
    和 relayapi 的文字节点不同：出错直接抛出（红色节点 + 原因），不会让下游拿着空提示词白白出图。
    接了图片就是看图说话（要用 omni 模型；图先缩到最长边 768，大图传阿里很慢）。"""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            # display_name：前端用它当控件的显示名
            "idea": ("STRING", {"multiline": True, "default": "", "display_name": "一句话需求", "tooltip": "想要什么，一句话说清楚；模型会按下面的「扩写指令」写成完整提示词"}),
            "instruction": ("STRING", {"multiline": True, "default": "", "display_name": "扩写指令（一般不用改）", "tooltip": "告诉模型怎么写、写成什么样；各工作流已经按用途写好"}),
            "model": (WRITER_MODELS, {"default": WRITER_MODEL, "display_name": "模型",
                                      "tooltip": "写提示词用哪个文字模型。默认 qwen3.8-flash（快、省）；想写得更好选 qwen3.8-max；gemini-* 是谷歌的，走网关（要接「Key：网关」），比阿里慢（几秒到 40 秒）。"
                                                 "要看图的（看图写文案、图生视频）必须选 qwen3.8-omni-flash 或 gemini-*，其余模型只能读文字"}),
            # seed 不参与请求：只是让每次运行都重新扩写（ComfyUI 输入没变就直接用上次的结果）
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "control_after_generate": True, "display_name": "随机数（变一下就重新扩写）"})},
            "optional": {"info": ("STRING", {"default": "", "forceInput": True}), "image": ("IMAGE",), "gateway_info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "response")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, idea, instruction, model, seed, info="", image=None, gateway_info=""):
        model = model.strip() or WRITER_MODEL
        google = model in GATEWAY_WRITER_MODELS                  # 谷歌的模型走网关（用「Key：网关」）；其余都发给阿里
        if google:
            key, base = _gateway_creds(gateway_info, model)
        else:
            key, base = _creds(info)
        who = "网关" if google else "阿里"
        if not idea.strip():
            raise RuntimeError(f"[{who} 写提示词] 一句话需求是空的")
        if not instruction.strip():
            raise RuntimeError(f"[{who} 写提示词] 扩写指令是空的")
        user = idea.strip() if image is None else [{"type": "image_url", "image_url": {"url": _to_data_url(image)}}, {"type": "text", "text": idea.strip()}]
        body = {"model": model, "stream": False,
                "messages": [{"role": "system", "content": instruction.strip()}, {"role": "user", "content": user}]}
        if not google:
            body["enable_thinking"] = False                      # 阿里的参数（关思考，快）；网关那边不需要
        d = _chat(base, key, body, GATEWAY_WRITER_TIMEOUT if google else WRITER_TIMEOUT, "写提示词", GATEWAY_CHAT_API if google else CHAT_API, who)
        try:
            text = _CITE.sub("", d["choices"][0]["message"]["content"]).strip()
        except Exception:
            text = ""
        if not text:
            raise RuntimeError(f"[{who} 写提示词] 返回里没有文字：" + json.dumps(d, ensure_ascii=False)[:300])
        return (text, json.dumps({"code": "success", "model": model, "usage": d.get("usage")}, ensure_ascii=False))


class ProAliVoiceDesign:
    """用文字描述设计一个新音色（qwen3-tts-vd），输出音色 id 和试听音频；id 存在阿里账号里，可反复用。"""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "voice_prompt": ("STRING", {"multiline": True, "default": "沉稳的中年男性，语速适中，声音温暖有磁性，适合产品介绍"}),
            "preview_text": ("STRING", {"multiline": True, "default": "欢迎选购我们的新品，限时三天，满一百九十九减五十。"}),
            "name": ("STRING", {"default": "myvoice", "tooltip": "只能用字母/数字/下划线"}),
            "language": (["zh", "en", "ja", "ko", "de", "fr", "es", "it", "pt", "ru"], {"default": "zh"})},
            "optional": {"info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("STRING", "AUDIO")
    RETURN_NAMES = ("voice", "preview")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, voice_prompt, preview_text, name, language, info=""):
        key, base = _creds(info)
        if not voice_prompt.strip() or not preview_text.strip():
            raise RuntimeError("[阿里 音色设计] 音色描述和试听文字都不能为空")
        d = _post(base, key, {"model": "qwen-voice-design", "input": {
            "action": "create", "target_model": "qwen3-tts-vd-2026-01-26", "voice_prompt": voice_prompt.strip(),
            "preview_text": preview_text.strip(), "preferred_name": name.strip() or "myvoice", "language": language},
            "parameters": {"sample_rate": 24000, "response_format": "wav"}}, TTS_TIMEOUT, "音色设计", VOICE_API)
        try:
            o = d["output"]
            return (o["voice"], audio_bytes_to_audio_input(base64.b64decode(o["preview_audio"]["data"])))
        except Exception:
            raise RuntimeError("[阿里 音色设计] 返回里没有音色：" + json.dumps(d, ensure_ascii=False)[:300])


class ProAliVoiceClone:
    """用一段样音克隆声音（qwen3-tts-vc），输出音色 id。只克隆你有权使用的声音（本人或已获授权）。"""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",), "name": ("STRING", {"default": "myclone", "tooltip": "只能用字母/数字/下划线"})},
                "optional": {"info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("voice",)
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, audio, name, info=""):
        key, base = _creds(info)
        wav = base64.b64encode(_audio_to_wav16k(audio, 24000)).decode()
        d = _post(base, key, {"model": "qwen-voice-enrollment", "input": {
            "action": "create", "target_model": "qwen3-tts-vc-2026-01-22", "preferred_name": name.strip() or "myclone",
            "audio": {"data": "data:audio/wav;base64," + wav}}}, TTS_TIMEOUT, "声音克隆", VOICE_API)
        try:
            return (d["output"]["voice"],)
        except Exception:
            raise RuntimeError("[阿里 声音克隆] 返回里没有音色：" + json.dumps(d, ensure_ascii=False)[:300])


class ProAliVoiceAdmin:
    """列出 / 删除账号里的自定义音色（音色设计 vd 和声音克隆 vc）；输出 JSON 文本。"""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"action": (["列出", "删除"], {"default": "列出"}),
                             "voice": ("STRING", {"default": "", "tooltip": "删除时填要删的音色 id"})},
                "optional": {"info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("result",)
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"
    OUTPUT_NODE = True

    def run(self, action, voice="", info=""):
        key, base = _creds(info)
        ask = lambda model, inp: _post(base, key, {"model": model, "input": inp}, TTS_TIMEOUT, "音色管理", VOICE_API)["output"]
        kinds = {"design": "qwen-voice-design", "clone": "qwen-voice-enrollment"}
        if action == "删除":
            vid = voice.strip()
            kind = "design" if vid.startswith("qwen-tts-vd-") else "clone" if vid.startswith("qwen-tts-vc-") else None
            if not kind:
                raise RuntimeError("[阿里 音色管理] 要删的音色 id 应以 qwen-tts-vd- 或 qwen-tts-vc- 开头")
            ask(kinds[kind], {"action": "delete", "voice": vid})
            res = json.dumps({"deleted": vid}, ensure_ascii=False)
            return {"ui": {"text": [res]}, "result": (res,)}
        out = {}
        for kind, model in kinds.items():
            voices, page = [], 0
            while True:
                o = ask(model, {"action": "list", "page_size": 50, "page_index": page})
                voices += o.get("voice_list") or []
                page += 1
                if len(voices) >= o.get("total_count", 0) or not o.get("voice_list"):
                    break
            out[kind] = voices
        res = json.dumps(out, ensure_ascii=False)
        return {"ui": {"text": [res]}, "result": (res,)}


# ── 多角色对白 ───────────────────────────────────────────────────────────────
MAX_DIALOGUE_LINES = 40     # 一次最多这么多句（每句调用一次阿里配音，按字数计费，防止误粘一大段文章）
MAX_DIALOGUE_CHARS = 1500
CACHE_KEEP = 400            # 已合成的句子最多留这么多条
NARRATOR = "旁白"
NARRATOR_NAMES = {"旁白", "画外音", "解说"}
_PAREN = re.compile(r"[（(][^）)]*[）)]")
_ROLE_LINE = re.compile(r"^([^：:\n]{0,20}?)\s*[：:]\s*(.*)$")
_LIST_MARK = re.compile(r"^\s*(?:[-*•·](?=\s)|\d+[.、)）](?!\d))\s*")                  # 「1. 」「- 」这样的序号 / 项目符号；「9.9元」「-5℃」开头的数字是台词本身，不能剥


def voice_id(label):
    """音色下拉里的「Cherry · 女 · 阳光亲切」→ 音色 id「Cherry」；直接写 id 也认（大小写不计）。"""
    head = str(label or "").split(" · ")[0].strip()
    for v in VOICES:
        if v.lower() == head.lower():
            return v
    raise RuntimeError(f"[阿里 对白] 音色「{label}」不在可选列表里")


def parse_script(script, role_names, max_lines=MAX_DIALOGUE_LINES, max_chars=MAX_DIALOGUE_CHARS):
    """对白脚本 → [(角色序号 0~3 / None=旁白, 角色显示名, 台词)]。每行「角色：台词」（中英文冒号都行；括号里的动作提示不念）；
    没有「角色：」的行、「旁白：」的行归旁白。台词里出现角色表里没有的名字就报错、不猜：猜错了会用错声音，还照样计费。
    角色名比较时括号里的部分不算（角色表里写「小美（店员）」、台词里写「小美：」或「小美（笑）：」都是同一个人）。"""
    names = [(n or "").strip() for n in role_names]
    keys = [_PAREN.sub("", n).strip().lower() for n in names]
    used = [k for k in keys if k]
    if len(set(used)) != len(used):
        raise RuntimeError("[阿里 对白] 角色表里有两个角色同名（括号里的说明不算），请改成不同的名字")
    out = []
    for raw in str(script or "").replace("\r\n", "\n").split("\n"):
        line = _LIST_MARK.sub("", raw).strip()
        if not line:
            continue
        m = _ROLE_LINE.match(line)
        who, text = None, line
        if m:
            label = _PAREN.sub("", m.group(1)).strip()
            hit = next((i for i, k in enumerate(keys) if k and k == label.lower()), None) if label else None
            if hit is not None:                                                  # 角色表里的名字（纯数字的名字也算）
                who, text = hit, m.group(2)
            elif not label or label in NARRATOR_NAMES:                           # 「：台词」「旁白：台词」
                text = m.group(2)
            elif not re.fullmatch(r"[\d\s.\-/]+", label):                       # 「10:30 开始」这种时间不是角色名：整行当旁白
                have = "、".join(n for n in names if n) or "（角色表是空的）"
                raise RuntimeError(f"[阿里 对白] 台词里的角色「{label}」不在角色表里（现有角色：{have}）。请在「角色 N 名字」里填上它，或改成已有的名字；如果这句是旁白，前面写「旁白：」")
        text = _PAREN.sub("", text).strip()
        if text:
            out.append((who, names[who] if who is not None else NARRATOR, text))
    if not out:
        raise RuntimeError("[阿里 对白] 对白脚本是空的（每行写「角色：台词」）")
    chars = sum(len(t) for _, _, t in out)
    if len(out) > max_lines or chars > max_chars:
        raise RuntimeError(f"[阿里 对白] 对白太长（{len(out)} 句 / {chars} 个字），一次最多 {max_lines} 句、{max_chars} 个字：请分成几次")
    return out


def split_text(text, max_chars):
    """台词切成字幕条：以标点为界（标点不显示），太长的再按 max_chars 硬切。"""
    cues = []
    for p in re.split(r"[，。！？；、：,.!?;:\n]+", text):
        p = p.strip()
        while len(p) > max_chars:
            cues.append(p[:max_chars])
            p = p[max_chars:]
        if p:
            cues.append(p)
    return cues


def build_dialogue(clips, gap_s):
    """clips = 每句的单声道音频（float32 数组，同一采样率 sr）→ (整段音频, 每句的 (开始, 结束) 秒)；句与句之间留 gap_s 秒停顿。"""
    sr = clips[0][1]
    gap = np.zeros(int(round(gap_s * sr)), np.float32)
    parts, spans, t = [], [], 0
    for k, (x, _) in enumerate(clips):
        if k:
            parts.append(gap)
            t += len(gap)
        spans.append((t / sr, (t + len(x)) / sr))
        parts.append(x)
        t += len(x)
    return np.concatenate(parts), spans


def dialogue_cues(lines, spans, max_chars, show_names):
    """每句台词按真实的起止时间（不用猜）排成字幕：长句切成几条、按字数分配这一句的时间；show_names 时每条前面写「角色：」（旁白不写）。"""
    cues = []
    for (who, name, text), (a, b) in zip(lines, spans):
        subs = split_text(text, max_chars) or [text]
        w = [max(1, len(x)) for x in subs]
        t = a
        for x, wi in zip(subs, w):
            e = t + (b - a) * wi / sum(w)
            cues.append([t, e, f"{name}：{x}" if show_names and who is not None else x])
            t = e
    for k in range(len(cues) - 1):          # 停顿里字幕不消失（保持到下一句开始前一点），最后一条多留一会儿
        cues[k][1] = max(cues[k][1], cues[k + 1][0] - 0.04)
    cues[-1][1] += 0.3
    return cues


def _srt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(cues):
    return "\n".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1))


# 已合成的句子存在临时目录里：改了其中几句再运行，只重新合成改过的；中途失败（网络断了）重跑也不会把前面已经付过费的句子再付一遍
def _cache_dir():
    import folder_paths
    d = os.path.join(folder_paths.get_temp_directory(), "pro_dialogue")
    os.makedirs(d, exist_ok=True)
    return d


def _line_key(model, voice, language, text, style):
    return hashlib.sha1(json.dumps([model, voice, language, text, style], ensure_ascii=False).encode("utf-8")).hexdigest()


def _cache_get(key):
    p = os.path.join(_cache_dir(), key + ".audio")
    try:
        with open(p, "rb") as f:
            data = f.read()
        os.utime(p)
        return data or None
    except OSError:
        return None


def _cache_put(key, data):
    d = _cache_dir()
    tmp = os.path.join(d, key + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, os.path.join(d, key + ".audio"))
    old = sorted((os.path.getmtime(os.path.join(d, n)), n) for n in os.listdir(d) if n.endswith(".audio"))
    for _, n in old[:-CACHE_KEEP]:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass


def _save_srt(prefix, srt):
    """字幕存成 output/<prefix>_00001_.srt（和「字幕生成」节点一样），返回所在的子目录。"""
    import folder_paths
    folder, name, counter, subfolder, _ = folder_paths.get_save_image_path(prefix, folder_paths.get_output_directory())
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"{name}_{counter:05}_.srt"), "w", encoding="utf-8") as f:
        f.write(srt)
    return subfolder


def _cache_drop(key):
    try:
        os.remove(os.path.join(_cache_dir(), key + ".audio"))
    except OSError:
        pass


def _check_interrupt():
    """用户点了「停止」就别再合成后面的句子：ComfyUI 只在节点和节点之间检查中断，一个节点里的长循环要自己查。"""
    try:
        import comfy.model_management as mm
    except ImportError:
        return
    mm.throw_exception_if_processing_interrupted()


def _decode_line(raw, speed, volume_db):
    """合成好的一句（音频字节）→ (单声道 float32 数组, 采样率)；变速 / 音量按句处理。"""
    a = _adjust(audio_bytes_to_audio_input(raw), speed, volume_db)
    return a["waveform"][0].float().mean(dim=0).cpu().numpy().astype(np.float32), int(a["sample_rate"])


def _to_rate(x, sr_from, sr_to):
    if sr_from == sr_to:
        return x
    return taf.resample(torch.from_numpy(x)[None], sr_from, sr_to)[0].numpy().astype(np.float32)


class ProAliDialogue:
    """多角色对白配音：脚本里每行「角色：台词」，每个角色选一个音色（最多 4 个，另有「旁白」）；每句合成一次、按真实时长拼起来，
    同时给出带角色名的字幕（SRT，接到「合成成片」就能烧进画面）。每句只发一次请求（计费），不重试；已合成的句子会存下来，改几句只重做几句。"""
    @classmethod
    def INPUT_TYPES(cls):
        demo = ("小美：老板，这个苹果怎么卖呀？\n阿强：新到的红富士，脆甜多汁，今天只要九块九一斤。\n"
                "小美：那我先来五斤，再帮我挑几个大的。\n阿强：好嘞，马上给你装好！")
        return {"required": {
            "script": ("STRING", {"multiline": True, "default": demo, "tooltip": "每行写「角色：台词」，角色名要和下面的「角色 N 名字」一致；「旁白：」或没写角色的行用旁白音色；括号里的动作提示（笑）不会念出来"}),
            "role1_name": ("STRING", {"default": "小美", "multiline": False}),
            "role1_voice": (VOICE_LABELS, {"default": VOICE_LABELS[VOICES.index("Cherry")]}),
            "role2_name": ("STRING", {"default": "阿强", "multiline": False}),
            "role2_voice": (VOICE_LABELS, {"default": VOICE_LABELS[VOICES.index("Ethan")]}),
            "role3_name": ("STRING", {"default": "", "multiline": False, "tooltip": "没有第 3 个角色就留空"}),
            "role3_voice": (VOICE_LABELS, {"default": VOICE_LABELS[VOICES.index("Serena")]}),
            "role4_name": ("STRING", {"default": "", "multiline": False, "tooltip": "没有第 4 个角色就留空"}),
            "role4_voice": (VOICE_LABELS, {"default": VOICE_LABELS[VOICES.index("Ryan")]}),
            "narrator_voice": (VOICE_LABELS, {"default": VOICE_LABELS[VOICES.index("Neil")], "tooltip": "「旁白：」和没写角色的行用这个音色"}),
            "model": (TTS_MODELS, {"default": "qwen3-tts-flash"}),
            "language": (LANGS, {"default": "Chinese"}),
            "speed": ("FLOAT", {"default": 1.0, "min": 0.5, "max": 2.0, "step": 0.05, "tooltip": "所有角色的语速倍数，本地变速不变调"}),
            "gap_s": ("FLOAT", {"default": 0.35, "min": 0.0, "max": 3.0, "step": 0.05, "tooltip": "句与句之间停顿多久（秒）"}),
            "show_names": ("BOOLEAN", {"default": True, "label_on": "字幕里写角色名", "label_off": "字幕里不写角色名"}),
            "max_chars": ("INT", {"default": 16, "min": 4, "max": 60, "tooltip": "字幕每行最多几个字"}),
            "reuse": ("BOOLEAN", {"default": True, "label_on": "没改的句子用上次的", "label_off": "全部重新合成"}),
            "volume_db": ("FLOAT", {"default": 0.0, "min": -20.0, "max": 12.0, "step": 0.5}),
            "filename_prefix": ("STRING", {"default": "subtitles/对白字幕", "tooltip": "字幕文件（.srt）存到 output 里的位置"})},
            "optional": {f"role{i}_style": ("STRING", {"multiline": True, "default": "", "placeholder": "仅 instruct 模型有效，例如：语气兴奋，语速偏快"}) for i in (1, 2, 3, 4)}
            | {"info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("AUDIO", "STRING", "STRING")
    RETURN_NAMES = ("audio", "srt", "report")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, script, role1_name, role1_voice, role2_name, role2_voice, role3_name, role3_voice, role4_name, role4_voice, narrator_voice,
            model, language, speed, gap_s, show_names, max_chars, reuse, volume_db, filename_prefix,
            role1_style="", role2_style="", role3_style="", role4_style="", info=""):
        key, base = _creds(info)
        names = [role1_name, role2_name, role3_name, role4_name]
        voices = [voice_id(v) for v in (role1_voice, role2_voice, role3_voice, role4_voice)]
        styles = [role1_style, role2_style, role3_style, role4_style]
        lines = parse_script(script, names)
        narrator = voice_id(narrator_voice)
        instruct = model == "qwen3-tts-instruct-flash"
        clips, reused = [], 0
        for k, (who, name, text) in enumerate(lines):
            _check_interrupt()
            voice = narrator if who is None else voices[who]
            style = styles[who].strip() if (instruct and who is not None) else ""
            ck = _line_key(model, voice, language, text, style)
            clip = None
            raw = _cache_get(ck) if reuse else None
            if raw is not None:
                try:
                    clip = _decode_line(raw, speed, volume_db)
                    reused += 1
                except Exception:                                    # 存下来的这一条解不开（坏了）：丢掉，重新合成这一句
                    _cache_drop(ck)
            if clip is None:
                try:
                    raw, _ = _tts_request(base, key, text, voice, model, language, style)
                except RuntimeError as e:
                    if k == 0:
                        done = ""
                    elif reuse:
                        done = f"前面 {k} 句已经合成好并存下来了，重新运行不会重复计费；"
                    else:
                        done = f"前面 {k} 句已经合成好并存下来了（要接着用它们，请打开「没改的句子用上次的」再运行；关着会全部重新合成、重新计费）；"
                    raise RuntimeError(f"[阿里 对白] 第 {k + 1}/{len(lines)} 句（{name}：{text[:12]}…）合成失败，{done}原因：{e}")
                try:
                    clip = _decode_line(raw, speed, volume_db)       # 先解码：返回了解不开的音频就别存进缓存，不然以后每次都命中它
                except Exception as e:
                    raise RuntimeError(f"[阿里 对白] 第 {k + 1}/{len(lines)} 句（{name}：{text[:12]}…）阿里返回了音频但解不开（{type(e).__name__}），这一句可能已经计费")
                _cache_put(ck, raw)
            clips.append(clip)
        sr = clips[0][1]
        clips = [(_to_rate(x, r, sr), sr) for x, r in clips]
        audio, spans = build_dialogue(clips, gap_s)
        srt = to_srt(dialogue_cues(lines, spans, max_chars, show_names))
        who_used = {}
        for who, name, _ in lines:
            who_used.setdefault(name, narrator if who is None else voices[who])
        report = (f"共 {len(lines)} 句、{len(who_used)} 个声音：" + "，".join(f"{n} → {v}" for n, v in who_used.items())
                  + f"；对白时长 {len(audio) / sr:.1f} 秒" + (f"；其中 {reused} 句直接用了上次合成的（没有重新计费）" if reused else "")
                  + ("；语气指令只有 instruct 模型才有效，这次没用上" if any(s.strip() for s in styles) and not instruct else ""))
        where = _save_srt(filename_prefix, srt)
        return {"ui": {"text": [f"{report}\n（字幕文件已存到 output/{where}/）\n\n{srt}"]},
                "result": ({"waveform": torch.from_numpy(audio)[None, None, :], "sample_rate": sr}, srt, report)}


# ════════════════════════════════════════════════════════════════════════════
# 视频类：改视频（万相 2.7 视频编辑 / HappyHorse 视频编辑）和视频换人（万相 2.2 animate-mix）——阿里的异步任务
# 流程：视频 / 人像先传到阿里的免费临时存储（oss:// 地址，48 小时有效，只能给同一个模型用，上传不计费）→ 提交任务（只发一次、不重试：重试会重复计费）
#      → 隔几秒查一次 → 下载结果。失败的任务阿里不收费；提交之后点了「停止」，只能取消还在排队的，已经在做的会照常计费（报错里写任务编号）。
# 输入限制（2026-10-12 查官方文档）都写在 VIDEO_LIMITS 里：不合格的先在本地处理（太长截前几秒、尺寸太大缩小、格式不认转成 mp4），没法自动处理的在上传之前就报错。
# ════════════════════════════════════════════════════════════════════════════
VIDEO_API = {"swap": "/api/v1/services/aigc/image2video/video-synthesis", "edit": "/api/v1/services/aigc/video-generation/video-synthesis"}
TASK_API = "/api/v1/tasks/"
UPLOAD_API = "/api/v1/uploads"
SUBMIT_TIMEOUT = 60
UPLOAD_TIMEOUT = (10, 300)        # 视频最大 200MB，德国 → 北京传一次可能要一两分钟
VIDEO_POLL_S = 10                 # 官方建议约 15 秒查一次（查询限流默认 20 次 / 秒），10 秒够用
VIDEO_TASK_TIMEOUT = 25 * 60      # 一般 1~5 分钟，排队时会更久
POLL_FAILS = 8                    # 连着几次都查不到才放弃（任务还在阿里那边做）
FFMPEG_TIMEOUT = 600
# 德国 → 阿里临时存储的上传速度很不稳（2026-10-12 实测 0.02~0.3 MB/s，同一个文件有时 8 秒、有时 130 秒），所以超过 HEAVY_MB 的视频先压一压再传：
# 画质对这几个模型够用（出片本来就是 720P / 1080P），文件小了上传快好几倍
HEAVY_MB = 2
HEAVY_SIDE = 1280                 # 压的时候长边最多这么多像素
HEAVY_CRF = "24"                  # 压的力度（越大越小越糊；24 肉眼基本看不出）
PLACEHOLDER_VIDEO = "请上传视频.mp4"   # 随套件带的占位视频：工作流里「上传视频」节点没给文件时用它，不能当真的视频传出去
SWAP_MODEL = "wan2.2-animate-mix"
EDIT_VIDEO_MODELS = ["wan2.7-videoedit", "happyhorse-1.0-video-edit"]
# seconds：输入视频要几秒（上限同时是「最多只用前几秒」：计费按秒，多传的白花钱）；side：(短边至少, 长边最多) 像素；ratio：长边 / 短边最大；mb：文件大小上限；
# formats：认的容器；refs：最多几张参考图；ref_side / ref_ratio：参考图短边至少 / 长边 / 短边最大
VIDEO_LIMITS = {
    SWAP_MODEL: {"api": "swap", "seconds": (2, 30), "side": (200, 2048), "ratio": 3.0, "mb": 200, "formats": ("mp4", "mov", "avi")},
    "wan2.7-videoedit": {"api": "edit", "seconds": (2, 10), "side": (240, 4096), "ratio": 8.0, "mb": 100, "formats": ("mp4", "mov"),
                         "refs": 4, "ref_side": 240, "ref_ratio": 8.0},
    "happyhorse-1.0-video-edit": {"api": "edit", "seconds": (3, 15), "side": (360, 4096), "ratio": 2.5, "mb": 100, "formats": ("mp4", "mov"),
                                  "refs": 5, "ref_side": 300, "ref_ratio": 2.5},
}
SWAP_QUALITY = {"标准（wan-std，快一点、便宜）": "wan-std", "专业（wan-pro，更流畅、更贵）": "wan-pro"}
EDIT_RATIOS = ["跟原视频一样", "16:9", "9:16", "1:1", "4:3", "3:4"]
EDIT_AUDIO = {"保留原视频的声音": "origin", "让 AI 决定（指令里写了声音才会重新生成）": "auto"}
# 阿里返回的错误码 → 人话（带点的错误码先按整个码找，再按前缀找；2026-10-12 真跑见过 InvalidImage.NoHuman、InvalidVideo.Duration）
VIDEO_ERROR_HINTS = {
    "DataInspectionFailed": "内容没通过阿里的审核（上传的图片 / 视频，或者生成出来的画面被判为不合规），换一个素材再试",
    "IPInfringementSuspect": "被判疑似侵权，没通过阿里的审核，换一个素材再试",
    "InvalidImage.NoHuman": "人物照片里没有检测到人（要清晰的单人照，脸和上半身都在画面里）",
    "InvalidImage": "人物照片没通过检查（要清晰的单人照，脸和上半身都在画面里、别太小）",
    "InvalidVideo": "视频没通过检查（时长、尺寸、格式或里面的人不合要求）",
}


def _ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise RuntimeError("[视频] 服务器里没有找到 ffmpeg（镜像里应该有 imageio-ffmpeg）")


def _run_ffmpeg(args, what, timeout=FFMPEG_TIMEOUT):
    """跑一条 ffmpeg（日志写进临时文件，不占管道）；点了「停止」就杀掉它。失败抛错并带上日志最后几行。"""
    exe = _ffmpeg_exe()
    with tempfile.TemporaryFile() as log:
        p = subprocess.Popen([exe, "-hide_banner", "-loglevel", "error", "-y", *args], stdout=subprocess.DEVNULL, stderr=log)
        t0 = time.monotonic()
        try:
            while p.poll() is None:
                _check_interrupt()
                if time.monotonic() - t0 > timeout:
                    raise RuntimeError(f"[阿里 {what}] 处理视频花了超过 {timeout // 60} 分钟，已放弃（视频太大？）")
                time.sleep(0.3)
        except BaseException:
            p.kill()
            p.wait()
            raise
        if p.returncode != 0:
            log.seek(0)
            tail = log.read().decode("utf-8", "replace").strip().splitlines()[-4:]
            raise RuntimeError(f"[阿里 {what}] 处理视频失败（ffmpeg）：{' | '.join(tail)[:300]}")


def _rotated(path):
    """手机竖拍的视频：编码里是横的，带「旋转 90 / 270 度」的标记。问 ffmpeg 本人（它打印的流信息里有 displaymatrix: rotation of -90.00 degrees）。"""
    try:
        r = subprocess.run([_ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, text=True, timeout=30)
    except (OSError, RuntimeError, subprocess.SubprocessError):          # 没有 ffmpeg 等：查不出旋转就当没有（后面真要用 ffmpeg 时会报清楚的错）
        return False
    m = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", r.stderr)
    return bool(m) and round(abs(float(m.group(1)))) % 180 == 90


def _probe_video(src, what):
    """用 PyAV 只读文件头（不解码画面）→ {seconds, width, height, vcodec, container, has_audio}；打不开 / 里面没有画面就报错。"""
    try:
        if hasattr(src, "seek"):
            src.seek(0)
        with av.open(src) as c:
            vs = c.streams.video[0] if c.streams.video else None
            if vs is None:
                raise ValueError("里面没有画面")
            if vs.duration and vs.time_base:
                sec = float(vs.duration * vs.time_base)
            elif c.duration:
                sec = float(c.duration / av.time_base)
            else:
                sec = 0.0
            return {"seconds": sec, "width": int(vs.width), "height": int(vs.height), "vcodec": vs.codec_context.name,
                    "container": c.format.name, "has_audio": bool(c.streams.audio)}
    except Exception as e:
        raise RuntimeError(f"[阿里 {what}] 这个视频打不开（{type(e).__name__}：{str(e)[:80]}）：换一个 mp4 / mov 再试")


def _prepare_video(src, model, what):
    """按 model 的输入限制整理视频 → (字节, 文件名, 实际用的秒数, 说明列表)。
    太短 / 太小 / 比例不对：没法自动处理，报错；太长 / 边长太大 / 不是 H.264 / 容器不认 / 带旋转标记 / 文件太大：重新编码成 mp4（只取前几秒、缩小、压一压；几秒钟）；都合格就原样用。"""
    lim = VIDEO_LIMITS[model]
    info = _probe_video(src, what)
    sec, w, h = info["seconds"], info["width"], info["height"]
    lo, hi = lim["seconds"]
    if sec and sec < lo - 0.05:
        raise RuntimeError(f"[阿里 {what}] 视频太短：只有 {sec:.1f} 秒，这个模型要 {lo}~{hi} 秒的视频")
    if min(w, h) < lim["side"][0]:
        raise RuntimeError(f"[阿里 {what}] 视频画面太小：{w}×{h}，短边至少要 {lim['side'][0]} 像素")
    if max(w, h) / max(min(w, h), 1) > lim["ratio"]:
        raise RuntimeError(f"[阿里 {what}] 视频画面太窄长：{w}×{h}，长边不能超过短边的 {lim['ratio']:g} 倍")
    path = src if isinstance(src, str) else None
    size = os.path.getsize(path) if path else len(src.getvalue())
    notes, work = [], None
    if path is None:                                   # 内存里的视频：落到临时文件再处理（ffmpeg 要读文件）
        fd, work = tempfile.mkstemp(suffix=".mp4")
        with os.fdopen(fd, "wb") as f:
            f.write(src.getvalue())
        path = work
    try:
        too_long = sec > hi - 0.02                     # 上限是「不超过」：10.0 秒的视频容器时长可能是 10.008，贴着线不保险，截到比上限短 0.1 秒（复制截取只能停在帧的边界，会多出几十毫秒）
        too_big = max(w, h) > lim["side"][1]
        rotated = _rotated(path)                       # 带旋转标记的竖拍视频：阿里认不认标记没把握，一律转码（ffmpeg 自己转正、清掉标记）
        plain = (info["vcodec"] == "h264" and any(f in info["container"] for f in lim["formats"]) and not rotated)
        over_size = size > lim["mb"] * 1024 * 1024
        heavy = size > HEAVY_MB * 1024 * 1024          # 没超限但不小（Veo 出的 10 秒约 5MB、手机拍的原片更大）：上传太慢，压一压再传
        use_s = (hi - 0.1) if too_long else (sec or hi)
        if too_long or too_big or over_size or heavy or not plain:
            fd, out = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)
            try:
                args = ["-i", path]
                if too_long:
                    args += ["-t", f"{hi - 0.1:g}"]
                    notes.append(f"视频有 {sec:.0f} 秒，超过这个模型的上限，只用了前 {hi} 秒")
                # 太长也重新编码，不用复制截取：复制只能停在帧的边界，实测 10 秒上限的截出来 10.08 秒，贴着线阿里可能判超；重新编码按时间精确停
                # 长边缩到 side 以内、保持比例、宽高取偶数。用 scale 表达式（iw / ih 是转正之后的宽高）而不是写死「宽:高」：手机竖拍的视频存成横的 + 旋转标记，
                # ffmpeg 先转正再套滤镜，按编码里的宽高写死会把画面压扁
                side = min(lim["side"][1], HEAVY_SIDE) if heavy else lim["side"][1]
                args += ["-vf", f"scale=w='min({side},iw)':h='min({side},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", HEAVY_CRF if heavy else "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
                shrunk = max(w, h) > side
                notes.append("视频重新编码成了 mp4" + (f"（长边缩小到 {side} 像素以内）" if shrunk else "（压小了文件，上传更快）" if heavy else ""))
                _run_ffmpeg(args + ["-movflags", "+faststart", out], what)
                with open(out, "rb") as f:
                    data = f.read()
            finally:
                if os.path.exists(out):
                    os.remove(out)
        else:
            with open(path, "rb") as f:
                data = f.read()
    finally:
        if work and os.path.exists(work):
            os.remove(work)
    if len(data) > lim["mb"] * 1024 * 1024:
        raise RuntimeError(f"[阿里 {what}] 视频文件太大：{len(data) / 1048576:.0f} MB，这个模型最多 {lim['mb']} MB：剪短一点或降低清晰度再试")
    return data, hashlib.sha1(data).hexdigest()[:16] + ".mp4", use_s, notes


def _image_bytes(tensor, max_side=2048, max_mb=5):
    """IMAGE（取第一张）→ JPEG 字节（最长边缩到 max_side 以内，超过 max_mb 就压一压）。"""
    arr = (tensor[0].cpu().numpy().clip(0, 1) * 255).astype(np.uint8)
    im = Image.fromarray(arr).convert("RGB")
    im.thumbnail((max_side, max_side))
    for q in (95, 88, 80, 70):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q)
        if buf.tell() <= max_mb * 1048576:
            return buf.getvalue()
    raise RuntimeError(f"图片压到最低质量还有 {buf.tell() / 1048576:.1f} MB，超过 {max_mb} MB")


def _check_image_size(tensor, lo, max_ratio, label, what):
    h, w = int(tensor.shape[1]), int(tensor.shape[2])
    if min(w, h) < lo:
        raise RuntimeError(f"[阿里 {what}] {label}太小：{w}×{h}，短边至少要 {lo} 像素")
    if max(w, h) / max(min(w, h), 1) > max_ratio:
        raise RuntimeError(f"[阿里 {what}] {label}太窄长：{w}×{h}，长边不能超过短边的 {max_ratio:g} 倍")


class _FormBody:
    """multipart/form-data 的请求体，当成「带长度的文件」交给 requests：它会一小块一小块地发，每块有自己的超时。
    不能把整个 bytes 一次交出去：德国 → 阿里这条线上传得慢（实测 0.03~0.3 MB/s），整块写操作共用一个很短的限期，传几 MB 就超时（实测 4 MB 的视频三次都超时）。
    fields = [(名字, 值)]；file 放在最后（阿里的要求）。"""
    def __init__(self, fields, filename, data, check=None):
        self.check = check                                  # 每读一块先调一次（点了「停止」就在这里抛出中断，上传立刻停）
        self.boundary = os.urandom(12).hex()
        head = "".join(f'--{self.boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields)
        head += f'--{self.boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        self.parts = [head.encode("utf-8"), memoryview(data), f"\r\n--{self.boundary}--\r\n".encode()]
        self.total = sum(len(x) for x in self.parts)
        self.i = self.off = 0

    @property
    def content_type(self):
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self):
        return self.total

    def read(self, n=-1):
        if self.check:
            self.check()
        out = []
        while self.i < len(self.parts) and n != 0:
            part = self.parts[self.i]
            take = len(part) - self.off if n is None or n < 0 else min(n, len(part) - self.off)
            out.append(bytes(part[self.off:self.off + take]))
            self.off += take
            n = n - take if n is not None and n > 0 else n
            if self.off >= len(part):
                self.i, self.off = self.i + 1, 0
        return b"".join(out)


class _NoRetry(RuntimeError):
    """上传里重试也没用的错（文件太大、地址不对）：别再试了。"""


def _oss_upload(base, key, model, data, name, what):
    """把一份文件（字节）传到阿里的免费临时存储 → oss:// 地址（48 小时有效，只能给同一个模型用）。上传不计费，连接问题重试几次。
    上传地址由阿里的接口返回，只认 https 的 *.aliyuncs.com（文件不会发到别处）。"""
    last = None
    for attempt in range(3):
        try:
            s = _open(base, what)
            try:
                r = s.get(base + UPLOAD_API, params={"action": "getPolicy", "model": model}, headers={"Authorization": "Bearer " + key}, timeout=(POST_CONNECT_TIMEOUT, 30))
            finally:
                s.close()
            try:
                d = r.json()
            except Exception:
                d = {}
            if r.status_code != 200 or not isinstance(d.get("data"), dict):
                if r.status_code in (400, 401, 403):                       # Key 不对 / 没权限 / 模型名不对：重试也没用
                    raise _NoRetry(f"[阿里 {what}] 拿上传凭证失败：HTTP {r.status_code} {d.get('code', '')} {str(d.get('message', ''))[:200]}")
                raise RuntimeError(f"拿上传凭证 HTTP {r.status_code}")
            p = d["data"]
            host = urlparse(str(p.get("upload_host") or ""))
            if host.scheme != "https" or not (host.hostname or "").endswith(".aliyuncs.com"):
                raise _NoRetry(f"[阿里 {what}] 阿里给的上传地址不对（{p.get('upload_host')}）：为安全起见没有上传")
            max_mb = p.get("max_file_size_mb")
            if isinstance(max_mb, (int, float)) and len(data) > max_mb * 1048576:
                raise _NoRetry(f"[阿里 {what}] 文件太大：{len(data) / 1048576:.0f} MB，阿里这个模型的临时存储最多收 {max_mb} MB")
            okey = f"{p['upload_dir']}/{name}"
            body = _FormBody([("OSSAccessKeyId", p["oss_access_key_id"]), ("Signature", p["signature"]), ("policy", p["policy"]),
                              ("x-oss-object-acl", p["x_oss_object_acl"]), ("x-oss-forbid-overwrite", p["x_oss_forbid_overwrite"]),
                              ("key", okey), ("success_action_status", "200")], name, data, check=_check_interrupt)   # file 在最后一个表单域（_FormBody 保证）
            u = requests.post(p["upload_host"], data=body, headers={"Content-Type": body.content_type}, timeout=UPLOAD_TIMEOUT)
            if u.status_code != 200:
                raise RuntimeError(f"HTTP {u.status_code} {u.text[:120]}")
            return "oss://" + okey
        except _NoRetry:
            raise
        except (requests.RequestException, RuntimeError, KeyError) as e:
            last = e
    raise RuntimeError(f"[阿里 {what}] 上传到阿里的临时存储失败（试了 3 次，{type(last).__name__}：{str(last)[:120]}）：还没提交任务，不会计费，稍后再试")


def _submit_video_task(base, key, api, body, what):
    """提交视频任务（只发一次）→ task_id。用了 oss:// 地址要带 X-DashScope-OssResourceResolve。"""
    r = _send(base, key, VIDEO_API[api], body, SUBMIT_TIMEOUT, what, headers={"X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"})
    try:
        d = r.json()
    except Exception:
        raise RuntimeError(f"[阿里 {what}] HTTP {r.status_code}，返回不是 JSON：{r.text[:200]}")
    if r.status_code != 200:
        raise RuntimeError(f"[阿里 {what}] HTTP {r.status_code} {d.get('code', '')}：{str(d.get('message', ''))[:300]}")
    tid = (d.get("output") or {}).get("task_id")
    if not tid:
        raise RuntimeError(f"[阿里 {what}] 提交成功但没有拿到任务编号：{json.dumps(d, ensure_ascii=False)[:300]}")
    return tid


def _cancel_task(base, key, task_id):
    """点了「停止」：取消还在排队的任务（已经在做的取消不了，会照常计费）。尽力而为，出什么错都不管。"""
    try:
        requests.post(f"{base}{TASK_API}{task_id}/cancel", headers={"Authorization": "Bearer " + key}, timeout=(CONNECT_TIMEOUT * 2, 10))
    except Exception:
        pass


def _task_failed(d, status, what):
    out = d.get("output") or {}
    code, msg = out.get("code") or d.get("code") or "", str(out.get("message") or d.get("message") or "")
    if status == "UNKNOWN":
        return RuntimeError(f"[阿里 {what}] 任务不见了（过期或者编号不对）")
    if status == "CANCELED":
        return RuntimeError(f"[阿里 {what}] 任务被取消了")
    hint = VIDEO_ERROR_HINTS.get(code) or VIDEO_ERROR_HINTS.get(str(code).split(".")[0])
    return RuntimeError(f"[阿里 {what}] 任务失败（{code or '没有错误码'}）：{hint + '。原文：' if hint else ''}{msg[:300]}（失败的任务阿里不收费）")


def _wait_task(base, key, task_id, what, poll_s=VIDEO_POLL_S, timeout_s=VIDEO_TASK_TIMEOUT):
    """隔几秒查一次任务，成功返回整份 JSON，失败抛错。查询是 GET：连不上 / 对方暂时出错就等下一轮，连着 POLL_FAILS 次都不行才放弃；点了「停止」就取消排队中的任务。"""
    headers = {"Authorization": "Bearer " + key}
    t0, fails = time.monotonic(), 0
    s = requests.Session()
    try:
        while True:
            try:
                r = s.get(base + TASK_API + task_id, headers=headers, timeout=(POST_CONNECT_TIMEOUT, 30))
                d = r.json()
                bad = r.status_code in (429, 500, 502, 503, 504)
            except (requests.RequestException, ValueError) as e:
                d, bad, r = None, True, None
                err = type(e).__name__
            if bad:
                fails += 1
                if fails >= POLL_FAILS:
                    raise RuntimeError(f"[阿里 {what}] 连着 {fails} 次查不到任务进度（{err if r is None else 'HTTP ' + str(r.status_code)}）：任务 {task_id} 在阿里那边可能还在做（可能已计费），"
                                       "稍后到阿里云百炼控制台看一下，先别重跑")
            else:
                if r.status_code != 200:
                    raise RuntimeError(f"[阿里 {what}] 查任务出错：HTTP {r.status_code} {d.get('code', '')} {str(d.get('message', ''))[:200]}（任务编号 {task_id}）")
                st = (d.get("output") or {}).get("task_status")
                if st == "SUCCEEDED":
                    return d
                if st in ("FAILED", "CANCELED", "UNKNOWN"):
                    raise _task_failed(d, st, what)
                if st is None:                                  # 200 但里面没有状态（格式不对）：当成这次没查到，连着 POLL_FAILS 次就放弃，别空转到超时
                    fails += 1
                    if fails >= POLL_FAILS:
                        raise RuntimeError(f"[阿里 {what}] 连着 {fails} 次查不到任务进度（返回里没有任务状态）：任务 {task_id} 在阿里那边可能还在做（可能已计费），稍后到阿里云百炼控制台看一下，先别重跑")
                else:
                    fails = 0
            if time.monotonic() - t0 > timeout_s:
                _cancel_task(base, key, task_id)
                raise RuntimeError(f"[阿里 {what}] 等了 {timeout_s // 60} 分钟还没出结果（任务 {task_id}）：阿里那边可能还在做（可能已计费），先别连着重跑")
            for _ in range(int(poll_s / 0.5)):
                _check_interrupt()
                time.sleep(0.5)
    except BaseException as e:
        if type(e).__name__ == "InterruptProcessingException":
            _cancel_task(base, key, task_id)
        raise
    finally:
        s.close()


def _download_file(url, path, what):
    """把结果视频下载到 path（流式写盘）。GET，失败重试：结果已经生成并计费了，下载失败不重试就白花钱。"""
    last = None
    for _ in range(3):
        try:
            with requests.get(url, stream=True, timeout=(CONNECT_TIMEOUT * 2, DOWNLOAD_TIMEOUT)) as r:
                r.raise_for_status()
                with open(path + ".part", "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        _check_interrupt()                      # 点了「停止」就别再下了
                        f.write(chunk)
            os.replace(path + ".part", path)
            return
        except requests.HTTPError as e:
            last = e
            if e.response is not None and e.response.status_code < 500:
                break
        except requests.RequestException as e:
            last = e
        except BaseException:                                   # 被中断：没下完的半个文件不留
            if os.path.exists(path + ".part"):
                os.remove(path + ".part")
            raise
    if os.path.exists(path + ".part"):
        os.remove(path + ".part")
    raise RuntimeError(f"[阿里 {what}] 视频已经做好并计费了，但下载失败（{type(last).__name__}）：链接 24 小时内有效，可以手动下载：{url}")


def _temp_video_path(tag):
    """ComfyUI 临时目录里的一个新文件名（顺手清掉自己留下的一天前的旧文件）：最终要给用户看的由「保存视频」节点复制进 output/。"""
    import folder_paths
    d = folder_paths.get_temp_directory()
    os.makedirs(d, exist_ok=True)
    cutoff = time.time() - 24 * 3600
    for f in os.listdir(d):
        if f.startswith("pro_") and f.endswith(".mp4"):
            try:
                if os.path.getmtime(os.path.join(d, f)) < cutoff:
                    os.remove(os.path.join(d, f))
            except OSError:
                pass
    return os.path.join(d, f"pro_{tag}_{int(time.time())}_{hashlib.sha1(os.urandom(8)).hexdigest()[:6]}.mp4")


def _finish_video(d, what, task_id):
    """任务成功的 JSON → 把结果视频下载到临时目录 → VideoFromFile。"""
    out = d.get("output") or {}
    url = out.get("video_url") or (out.get("results") or {}).get("video_url")
    if not url:
        raise RuntimeError(f"[阿里 {what}] 任务成功了但返回里没有视频地址（任务 {task_id}）：{json.dumps(d, ensure_ascii=False)[:300]}")
    path = _temp_video_path("ali")
    _download_file(url, path, what)
    return InputImpl.VideoFromFile(path)


class ProAliVideoEdit:
    """AI 改视频：一段视频 + 一句话（+ 最多几张参考图）→ 改过的新视频：换物体 / 换背景 / 换风格 / 删东西，或把参考图里的衣服、道具用到视频里，也能复刻原视频的动作和运镜。
    万相 2.7 视频编辑（默认）或 HappyHorse 视频编辑（输入视频可以更长、更贵）。计费 = 输入视频秒数 + 输出视频秒数。"""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "video": ("VIDEO",),
            "prompt": ("STRING", {"multiline": True, "default": "", "placeholder": "例：把背景换成海边日落，其他保持不变",
                                  "tooltip": "怎么改：一两句话说清改什么、改成什么，不想变的写「其他保持不变」。带参考图时用「图片中的……」指代参考图（例如：把视频里女孩的衣服换成图片中的衣服）"}),
            "model": (EDIT_VIDEO_MODELS, {"default": EDIT_VIDEO_MODELS[0],
                                          "tooltip": "wan2.7-videoedit（默认）：输入视频 2~10 秒，720P 约 0.6 元/秒；happyhorse-1.0-video-edit：输入视频 3~15 秒（超过只用前 15 秒），720P 约 0.9 元/秒，不能改比例 / 时长。"
                                                     "两个都是按「输入视频秒数 + 输出视频秒数」计费"}),
            "resolution": (["720P", "1080P"], {"default": "720P", "tooltip": "出片清晰度；1080P 更贵（万相 2.7：720P 约 0.6 元/秒，1080P 约 1 元/秒）"}),
            "audio": (list(EDIT_AUDIO), {"default": next(iter(EDIT_AUDIO)), "tooltip": "出片的声音：保留原视频的，或让 AI 自己决定"}),
            "duration": ("INT", {"default": 0, "min": 0, "max": 10, "tooltip": "0 = 和原视频一样长；2~10 = 只取前几秒（也更省钱）。只有万相 2.7 能设"}),
            "ratio": (EDIT_RATIOS, {"default": EDIT_RATIOS[0], "tooltip": "出片比例；默认跟原视频一样。只有万相 2.7 能设"}),
            "prompt_extend": ("BOOLEAN", {"default": True, "label_on": "让 AI 润色指令", "label_off": "指令照原样用", "tooltip": "让阿里先把你的指令改写得更详细（指令很短时有帮助，会多等一会儿）。只有万相 2.7 能设"}),
            "watermark": ("BOOLEAN", {"default": False, "label_on": "加水印", "label_off": "不加水印"}),
            # seed 也送给阿里（0~2147483647）；同时让每次运行都重新做（ComfyUI 输入没变就直接用上次的结果）
            "seed": ("INT", {"default": 0, "min": 0, "max": 2147483646, "control_after_generate": True})},
            "optional": dict({f"ref_image{i}": ("IMAGE",) for i in range(1, 5)}, info=("STRING", {"default": "", "forceInput": True}))}

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "response")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, video, prompt, model, resolution, audio, duration, ratio, prompt_extend, watermark, seed,
            ref_image1=None, ref_image2=None, ref_image3=None, ref_image4=None, info=""):
        what = "改视频"
        key, base = _creds(info)
        if model not in EDIT_VIDEO_MODELS:
            raise RuntimeError(f"[阿里 {what}] 不认识的模型「{model}」：可选 {'、'.join(EDIT_VIDEO_MODELS)}")
        lim = VIDEO_LIMITS[model]
        if not prompt.strip():
            raise RuntimeError(f"[阿里 {what}] 「怎么改」是空的：写一两句话，说清改什么、改成什么")
        refs = [t for t in (ref_image1, ref_image2, ref_image3, ref_image4) if t is not None]
        if len(refs) > lim["refs"]:
            raise RuntimeError(f"[阿里 {what}] 这个模型最多 {lim['refs']} 张参考图，现在有 {len(refs)} 张")
        for i, t in enumerate(refs, 1):                                  # 先把便宜的检查做完，再去传视频
            _check_image_size(t, lim["ref_side"], lim["ref_ratio"], f"参考图 {i}", what)
        data, vname, use_s, notes = _prepare_video(video.get_stream_source(), model, what)
        vid_url = _oss_upload(base, key, model, data, vname, what)
        ref_urls = [_to_data_url(t) for t in refs]
        params = {"resolution": resolution, "watermark": bool(watermark), "audio_setting": EDIT_AUDIO[audio], "seed": int(seed)}
        if model == "wan2.7-videoedit":
            params["prompt_extend"] = bool(prompt_extend)
            if ratio != EDIT_RATIOS[0]:
                params["ratio"] = ratio
            if duration >= 1:
                params["duration"] = max(2, min(int(duration), int(use_s)))
        elif ratio != EDIT_RATIOS[0] or duration:
            notes.append("这个模型不能改比例 / 时长，这两项没用上")
        body = {"model": model, "input": {"prompt": prompt.strip(), "media": [{"type": "video", "url": vid_url}] + [{"type": "reference_image", "url": u} for u in ref_urls]},
                "parameters": params}
        tid = _submit_video_task(base, key, lim["api"], body, what)
        d = _wait_task(base, key, tid, what)
        return (_finish_video(d, what, tid), json.dumps({"code": "success", "model": model, "task_id": tid, "usage": d.get("usage"), "notes": notes}, ensure_ascii=False))


class ProAliVideoSwap:
    """视频换人（万相 2.2 animate-mix）：一张人物照片 + 一段视频 → 视频里的人换成照片里的人，动作、表情、场景光线保留。整个人（含衣服发型）跟着照片走，不是只换脸。
    只能用本人或已获授权的人像（节点里要勾选确认）。按输出视频的秒数计费（标准 0.6 元/秒，专业 0.9 元/秒）。"""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "video": ("VIDEO",),
            "authorized": ("BOOLEAN", {"default": False, "label_on": "我确认人像是本人或已获授权", "label_off": "还没确认（不勾选不能运行）",
                                       "tooltip": "只能换成你本人或已经获得授权的人像：用别人的照片换人可能侵犯肖像权。勾上表示你确认这张人物照片可以用"}),
            "quality": (list(SWAP_QUALITY), {"default": next(iter(SWAP_QUALITY)), "tooltip": "标准：快一点、便宜（0.6 元/秒）；专业：动作更流畅、画质更好、更慢更贵（0.9 元/秒）"}),
            # seed 不参与请求：只是让每次运行都重新做（ComfyUI 输入没变就直接用上次的结果）
            "seed": ("INT", {"default": 0, "min": 0, "max": 2147483646, "control_after_generate": True})},
            "optional": {"check_image": ("BOOLEAN", {"default": True, "label_on": "先检查人物照片", "label_off": "不检查", "tooltip": "处理前先检查照片合不合格（是不是清晰的单人照）；不合格会直接报错，不会白等"}),
                         "watermark": ("BOOLEAN", {"default": False, "label_on": "加水印", "label_off": "不加水印"}),
                         "info": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "response")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, image, video, authorized, quality, seed, check_image=True, watermark=False, info=""):
        what = "视频换人"
        if not authorized:
            raise RuntimeError(f"[阿里 {what}] 请先勾选「我确认人像是本人或已获授权」：只能换成你本人或已经获得授权的人像（用别人的照片换人可能侵犯肖像权）")
        key, base = _creds(info)
        lim = VIDEO_LIMITS[SWAP_MODEL]
        _check_image_size(image, 200, 3.0, "人物照片", what)
        try:
            img = _image_bytes(image)
        except RuntimeError as e:
            raise RuntimeError(f"[阿里 {what}] 人物照片：{e}")
        data, vname, use_s, notes = _prepare_video(video.get_stream_source(), SWAP_MODEL, what)
        img_url = _oss_upload(base, key, SWAP_MODEL, img, hashlib.sha1(img).hexdigest()[:16] + ".jpg", what)
        vid_url = _oss_upload(base, key, SWAP_MODEL, data, vname, what)
        body = {"model": SWAP_MODEL, "input": {"image_url": img_url, "video_url": vid_url, "watermark": bool(watermark)},
                "parameters": {"mode": SWAP_QUALITY[quality], "check_image": bool(check_image)}}
        tid = _submit_video_task(base, key, lim["api"], body, what)
        d = _wait_task(base, key, tid, what)
        return (_finish_video(d, what, tid), json.dumps({"code": "success", "model": SWAP_MODEL, "task_id": tid, "usage": d.get("usage"), "notes": notes}, ensure_ascii=False))


NODE_CLASS_MAPPINGS = {"ProAliImage": ProAliImage, "ProAliImageEdit": ProAliImageEdit, "ProAliTTS": ProAliTTS, "ProAliDialogue": ProAliDialogue, "ProAliASR": ProAliASR,
                      "ProAliVoiceDesign": ProAliVoiceDesign, "ProAliVoiceClone": ProAliVoiceClone, "ProAliVoiceAdmin": ProAliVoiceAdmin,
                      "ProAliPromptWriter": ProAliPromptWriter, "ProAliVideoEdit": ProAliVideoEdit, "ProAliVideoSwap": ProAliVideoSwap}
NODE_DISPLAY_NAME_MAPPINGS = {"ProAliImage": "阿里 文生图（qwen-image，支持 2K）", "ProAliImageEdit": "阿里 改图（qwen-image-edit）",
                              "ProAliTTS": "阿里 配音（qwen3-tts）", "ProAliDialogue": "阿里 多角色对白配音（每行「角色：台词」）", "ProAliASR": "阿里 语音识别（听写，qwen3-asr）",
                              "ProAliPromptWriter": "写提示词（阿里 / 谷歌：一句话需求→完整提示词）",
                              "ProAliVoiceDesign": "阿里 音色设计（文字描述→新音色）", "ProAliVoiceClone": "阿里 声音克隆（样音→新音色）",
                              "ProAliVoiceAdmin": "阿里 音色管理（列出/删除）",
                              "ProAliVideoEdit": "阿里 AI 改视频（一句话 + 参考图改视频，万相 2.7 / HappyHorse）", "ProAliVideoSwap": "阿里 视频换人（人像 + 视频，万相 animate-mix）"}
