"""阿里云百炼（DashScope）节点：生图 / 改图 / 配音 / 听写 / 写提示词。

Key 与地址取自 Relay API Settings 节点的 info 输出（存在服务器 relay_config.json 的 node_settings[节点 id]，不进 git）。
只允许把 Key 发往 *.aliyuncs.com，Settings 里地址配错时宁可报错也不外发。
错误直接抛出（红色节点 + 阿里返回的原因）。计费请求（生图 / 改图 / 配音 / 听写 / 音色）只发一次、不重试：重试会重复计费。
连接阶段单独处理：发请求前先用一个免费的 HEAD 把 TCP + TLS 连接建好（建不好就重试，这时什么都没发出去，不会计费），请求走这条已经连好的连接；
结果下载是 GET，失败重试。写提示词的文字请求几乎不花钱，连不上（含超时）整个请求重试一次。
"""
import base64
import io
import json
import wave
from urllib.parse import urlparse

import numpy as np
import requests
import torch
import torchaudio.functional as taf
from PIL import Image
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
CHAT_API = "/compatible-mode/v1/chat/completions"
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
# 首字母大写：instruct 模型只认大写（Chinese），小写会 400；flash 两种都认
LANGS = ["Chinese", "English", "Japanese", "Korean", "German", "French", "Spanish", "Italian", "Portuguese", "Russian", "Auto"]
IMAGE_MODELS = ["qwen-image-2.0-pro", "qwen-image-3.0"]
EDIT_MODELS = ["qwen-image-edit-max"]
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


def _open(base, what):
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
    raise RuntimeError(f"[阿里 {what}] 连不上：{type(last).__name__}（连接 / 握手试了 {WARMUP_TRIES} 次都没成功，请求没有发出，不会计费）")


def _send(base, key, path, body, timeout, what):
    """（预热连接后）把 JSON 发给阿里，只发一次，返回 Response；连不上 / 等回复超时抛 RuntimeError。"""
    s = _open(base, what)
    try:
        return s.post(base + path, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                      json=body, timeout=(POST_CONNECT_TIMEOUT, timeout))
    except requests.ReadTimeout:
        raise RuntimeError(f"[阿里 {what}] 等了 {timeout} 秒没有回应：请求已经发出，阿里那边可能还在处理（可能已计费），先别连着重跑")
    except requests.RequestException as e:
        raise RuntimeError(f"[阿里 {what}] 连不上：{type(e).__name__}")
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
        inp = {"text": text, "voice": voice, "language_type": language}
        if model == "qwen3-tts-instruct-flash" and instructions.strip():
            inp["instructions"] = instructions.strip()
            inp["optimize_instructions"] = True
        d = _post(base, key, {"model": model, "input": inp}, TTS_TIMEOUT, "配音")
        try:
            url = d["output"]["audio"]["url"]
        except Exception:
            raise RuntimeError("[阿里 配音] 返回里没有音频：" + json.dumps(d, ensure_ascii=False)[:300])
        audio = _adjust(audio_bytes_to_audio_input(_download(url, "配音")), speed, volume_db)
        return (audio, json.dumps({"code": "success", "model": model, "voice": voice, "usage": d.get("usage")}, ensure_ascii=False))


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


def _chat(base, key, body, timeout, what):
    """OpenAI 兼容的 chat/completions。连不上（含超时）整个请求重试一次（文字请求几乎不花钱），HTTP 错误不重试；返回解析后的 JSON。"""
    for attempt in (1, 2):
        try:
            r = _send(base, key, CHAT_API, body, timeout, what)
            break
        except RuntimeError:
            if attempt == 2:
                raise
    try:
        d = r.json()
    except Exception:
        raise RuntimeError(f"[阿里 {what}] HTTP {r.status_code}，返回不是 JSON：{r.text[:200]}")
    if r.status_code != 200:
        err = d.get("error", d)
        raise RuntimeError(f"[阿里 {what}] HTTP {r.status_code} {err.get('code', '')}：{str(err.get('message', ''))[:300]}")
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
            "model": ("STRING", {"default": WRITER_MODEL, "display_name": "模型"}),
            # seed 不参与请求：只是让每次运行都重新扩写（ComfyUI 输入没变就直接用上次的结果）
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "control_after_generate": True, "display_name": "随机数（变一下就重新扩写）"})},
            "optional": {"info": ("STRING", {"default": "", "forceInput": True}), "image": ("IMAGE",)}}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "response")
    FUNCTION = "run"
    CATEGORY = "pro/aliyun"

    def run(self, idea, instruction, model, seed, info="", image=None):
        key, base = _creds(info)
        if not idea.strip():
            raise RuntimeError("[阿里 写提示词] 一句话需求是空的")
        if not instruction.strip():
            raise RuntimeError("[阿里 写提示词] 扩写指令是空的")
        model = model.strip() or WRITER_MODEL
        user = idea.strip() if image is None else [{"type": "image_url", "image_url": {"url": _to_data_url(image)}}, {"type": "text", "text": idea.strip()}]
        body = {"model": model, "stream": False, "enable_thinking": False,
                "messages": [{"role": "system", "content": instruction.strip()}, {"role": "user", "content": user}]}
        d = _chat(base, key, body, WRITER_TIMEOUT, "写提示词")
        try:
            text = d["choices"][0]["message"]["content"].strip()
        except Exception:
            text = ""
        if not text:
            raise RuntimeError("[阿里 写提示词] 返回里没有文字：" + json.dumps(d, ensure_ascii=False)[:300])
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


NODE_CLASS_MAPPINGS = {"ProAliImage": ProAliImage, "ProAliImageEdit": ProAliImageEdit, "ProAliTTS": ProAliTTS, "ProAliASR": ProAliASR,
                      "ProAliVoiceDesign": ProAliVoiceDesign, "ProAliVoiceClone": ProAliVoiceClone, "ProAliVoiceAdmin": ProAliVoiceAdmin,
                      "ProAliPromptWriter": ProAliPromptWriter}
NODE_DISPLAY_NAME_MAPPINGS = {"ProAliImage": "阿里 文生图（qwen-image，支持 2K）", "ProAliImageEdit": "阿里 改图（qwen-image-edit）",
                              "ProAliTTS": "阿里 配音（qwen3-tts）", "ProAliASR": "阿里 语音识别（听写，qwen3-asr）",
                              "ProAliPromptWriter": "阿里 写提示词（一句话需求→完整提示词）",
                              "ProAliVoiceDesign": "阿里 音色设计（文字描述→新音色）", "ProAliVoiceClone": "阿里 声音克隆（样音→新音色）",
                              "ProAliVoiceAdmin": "阿里 音色管理（列出/删除）"}
