"""视频节点：配音合成 / 取音轨 / 字幕生成。

为什么自己写：ComfyUI 自带的 GetVideoComponents / CreateVideo 会把整段视频解码成浮点张量
（10 秒 720p ≈ 2.6GB），在 4GB 内存的机器上会被系统 OOM 杀掉。这里用 PyAV 复制画面包（不烧字幕时）
或逐帧流式处理（烧字幕时），内存很小。
"""
import os
import re
from fractions import Fraction

import av
import folder_paths
import numpy as np
import torch
import torchaudio.functional as taf
from PIL import Image, ImageDraw, ImageFont
from comfy_api.latest import InputImpl

SR = 44100
CHUNK = 1024  # AAC 一帧的采样数
# 烧字幕要中文字体：容器里默认没有，部署脚本会把宿主机的 fonts-noto-cjk 只读挂进来；也可用环境变量 PRO_SUBTITLE_FONT 指定
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
]


# ── 音频小工具 ───────────────────────────────────────────────────────────────
def _stereo(audio):
    """ComfyUI 的 AUDIO {"waveform":[B,C,T],"sample_rate"} → 44.1kHz 立体声 float32 [2,T]"""
    w = audio["waveform"]
    w = (w[0] if w.dim() == 3 else w).float()
    w = w.repeat(2, 1) if w.shape[0] == 1 else w[:2]
    if int(audio["sample_rate"]) != SR:
        w = taf.resample(w, int(audio["sample_rate"]), SR)
    return w.cpu().numpy().astype(np.float32)


def _fit(x, n, loop):
    if x.shape[1] >= n:
        return x[:, :n]
    if loop and x.shape[1] > 0:
        return np.tile(x, (1, -(-n // x.shape[1])))[:, :n]
    out = np.zeros((2, n), np.float32)
    out[:, : x.shape[1]] = x
    return out


def _db(x, db):
    return x * float(10 ** (db / 20.0))


def _decode_audio(src):
    """只解码音轨（不碰画面）→ float32 [2,T] @44.1kHz；没有音轨返回 None"""
    if hasattr(src, "seek"):
        src.seek(0)
    with av.open(src) as c:
        if not c.streams.audio:
            return None
        rs = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
        parts = []
        for fr in c.decode(audio=0):
            for r in rs.resample(fr) or []:
                parts.append(r.to_ndarray())
        for r in rs.resample(None) or []:
            parts.append(r.to_ndarray())
    return np.concatenate(parts, axis=1).astype(np.float32) if parts else None


# ── 字幕：切句 + 用配音里的停顿对齐时间 ─────────────────────────────────────
def _split_cues(text, max_chars):
    """以标点为界切成字幕条（标点本身不显示）；太长的再按 max_chars 硬切"""
    cues = []
    for p in re.split(r"[，。！？；、：,.!?;:\n]+", text):
        p = p.strip()
        while len(p) > max_chars:
            cues.append(p[:max_chars])
            p = p[max_chars:]
        if p:
            cues.append(p)
    return cues


def _speech_layout(x, min_gap=0.12):
    """单声道 float 数组 → (说话开始, 说话结束, [(停顿开始, 停顿结束)…])，单位秒"""
    hop = int(0.01 * SR)
    n = len(x) // hop
    if n == 0:
        return 0.0, 0.0, []
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    active = rms > max(1e-4, 0.03 * float(rms.max()))
    idx = np.flatnonzero(active)
    if len(idx) == 0:
        return 0.0, 0.0, []
    first, last = int(idx[0]), int(idx[-1])
    gaps, i = [], first
    while i <= last:
        if active[i]:
            i += 1
            continue
        j = i
        while j <= last and not active[j]:
            j += 1
        if (j - i) * 0.01 >= min_gap:
            gaps.append((i * 0.01, j * 0.01))
        i = j
    return first * 0.01, (last + 1) * 0.01, gaps


def _time_cues(cues, voice_mono, offset=0.0):
    """给每条字幕定时间：停顿够多就用 N-1 个最长停顿做句子边界（最准）；否则按字数比例分配"""
    s, e, gaps = _speech_layout(voice_mono)
    n = len(cues)
    if n == 0 or e <= s:
        return [], None
    if n > 1 and len(gaps) >= n - 1:
        cut = sorted(sorted(gaps, key=lambda g: g[1] - g[0], reverse=True)[: n - 1])
        starts = [s] + [g[1] for g in cut]
        ends = [g[0] for g in cut] + [e]
        method = "停顿"
    else:
        w = np.array([max(1, len(c)) for c in cues], dtype=float)
        edges = s + (e - s) * np.concatenate([[0.0], np.cumsum(w) / w.sum()])
        starts, ends = list(edges[:-1]), list(edges[1:])
        method = "按字数比例"
    # 停顿里字幕不消失（保持到下一句开始前一点），最后一条多留一会儿
    for k in range(n - 1):
        ends[k] = max(ends[k], starts[k + 1] - 0.04)
    ends[-1] = ends[-1] + 0.3
    return [(starts[k] + offset, ends[k] + offset, cues[k]) for k in range(n)], method


def _srt_time(t):
    t = max(0.0, t)
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _to_srt(cues):
    return "\n".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1))


def _parse_srt(text):
    cues = []
    for blk in re.split(r"\n\s*\n", text.strip().replace("\r\n", "\n")):
        lines = [l for l in blk.split("\n") if l.strip()]
        for k, l in enumerate(lines):
            m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", l.strip())
            if m:
                g = [int(v) for v in m.groups()]
                cues.append((g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000, g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000, "\n".join(lines[k + 1:])))
                break
    return cues


class ProSubtitles:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "text": ("STRING", {"multiline": True, "default": "夏日清凉节，全场满一百九十九减五十，限时三天，欢迎选购。"}),
            "voice": ("AUDIO",),
            "max_chars": ("INT", {"default": 16, "min": 4, "max": 60}),
            "offset_s": ("FLOAT", {"default": 0.0, "min": -10.0, "max": 60.0, "step": 0.1}),
            "filename_prefix": ("STRING", {"default": "subtitles/字幕"})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("srt",)
    FUNCTION = "run"
    CATEGORY = "pro/video"   # 不是输出节点：srt 在 run() 里写盘；输出节点会无视「选择分支」被强制执行

    def run(self, text, voice, max_chars, offset_s, filename_prefix):
        cues = _split_cues(text, max_chars)
        if not cues:
            raise RuntimeError("[字幕] 文字是空的")
        mono = _stereo(voice).mean(axis=0)
        timed, method = _time_cues(cues, mono, offset_s)
        if not timed:
            raise RuntimeError("[字幕] 配音里没有检测到声音，没法对齐")
        srt = _to_srt(timed)
        folder, name, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, folder_paths.get_output_directory())
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f"{name}_{counter:05}_.srt"), "w", encoding="utf-8") as f:
            f.write(srt)
        return {"ui": {"text": [f"（按{method}对齐，共 {len(timed)} 条，已存 output/{subfolder}/）\n\n{srt}"]}, "result": (srt,)}


class ProVideoAudio:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"video": ("VIDEO",)}}

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio",)
    FUNCTION = "run"
    CATEGORY = "pro/video"

    def run(self, video):
        a = _decode_audio(video.get_stream_source())
        if a is None:
            raise RuntimeError("[取音轨] 这个视频没有声音")
        return ({"waveform": torch.from_numpy(a)[None,], "sample_rate": SR},)


# ── 烧字幕 ───────────────────────────────────────────────────────────────────
def _font(size):
    paths = ([os.environ["PRO_SUBTITLE_FONT"]] if os.environ.get("PRO_SUBTITLE_FONT") else []) + FONT_CANDIDATES
    for p in paths:
        if not os.path.exists(p):
            continue
        for idx in range(0, 6):  # .ttc 里有 JP/KR/SC/TC/HK 几套，挑简体中文那套
            try:
                f = ImageFont.truetype(p, size, index=idx)
            except OSError:
                break
            fam = f.getname()[0]
            if p.endswith(".ttc") and not fam.endswith("SC"):
                continue
            return f
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    raise RuntimeError("[视频合成] 烧字幕需要中文字体：容器里没有。用部署脚本重建容器（会挂载宿主机的 fonts-noto-cjk），或设环境变量 PRO_SUBTITLE_FONT")


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for ch in text.replace("\n", " "):
        if cur and draw.textlength(cur + ch, font=font) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    return lines + ([cur] if cur else [])


def _burn(img, text, font, bottom_margin):
    d = ImageDraw.Draw(img)
    lines = _wrap(d, text, font, img.width * 0.9)
    lh = int(font.size * 1.25)
    y = img.height - bottom_margin - lh * len(lines)
    for ln in lines:
        x = (img.width - d.textlength(ln, font=font)) / 2
        d.text((x, y), ln, font=font, fill=(255, 255, 255), stroke_width=max(2, font.size // 14), stroke_fill=(0, 0, 0))
        y += lh
    return img


class ProVideoDub:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "video": ("VIDEO",),
            "bgm_volume_db": ("FLOAT", {"default": -14.0, "min": -60.0, "max": 12.0, "step": 0.5}),
            "voice_volume_db": ("FLOAT", {"default": 0.0, "min": -60.0, "max": 12.0, "step": 0.5}),
            "bgm_fade_out_s": ("FLOAT", {"default": 1.5, "min": 0.0, "max": 10.0, "step": 0.1}),
            "keep_original_audio": ("BOOLEAN", {"default": False, "label_on": "保留原视频声音", "label_off": "替换原视频声音"}),
            "filename_prefix": ("STRING", {"default": "video/成片"}),
            "subtitle_mode": (["不加字幕", "烧进画面"], {"default": "不加字幕"}),
            "subtitle_font_size": ("INT", {"default": 46, "min": 16, "max": 120}),
            "subtitle_bottom_margin": ("INT", {"default": 70, "min": 0, "max": 400})},
            "optional": {"voice": ("AUDIO",), "bgm": ("AUDIO",), "subtitles": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("video",)
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "pro/video"

    def run(self, video, bgm_volume_db, voice_volume_db, bgm_fade_out_s, keep_original_audio, filename_prefix,
            subtitle_mode, subtitle_font_size, subtitle_bottom_margin, voice=None, bgm=None, subtitles=""):
        burn = subtitle_mode == "烧进画面" and subtitles is not None   # None = 开关门关着，这次不加字幕
        cues = _parse_srt(subtitles) if burn else []
        if burn and not cues:
            raise RuntimeError("[视频合成] 选了烧字幕，但没有接入有效的字幕（SRT）")
        if voice is None and bgm is None and not keep_original_audio:
            raise RuntimeError("[视频合成] 没有任何音频：请接入配音或背景音乐，或勾选保留原视频声音")
        font = _font(subtitle_font_size) if burn else None  # 先找字体：没有就在开始前报错
        src = video.get_stream_source()
        if hasattr(src, "seek"):
            src.seek(0)
        inp = av.open(src)
        try:
            if not inp.streams.video:
                raise RuntimeError("[视频合成] 输入里没有视频流")
            vs = inp.streams.video[0]
            dur = float(inp.duration / av.time_base) if inp.duration else float(vs.duration * vs.time_base)
            n = max(1, int(dur * SR))
            mix = np.zeros((2, n), np.float32)
            if keep_original_audio:
                orig = _decode_audio(src)
                if orig is not None:
                    mix += _fit(orig, n, False)
            if bgm is not None:
                b = _db(_fit(_stereo(bgm), n, True), bgm_volume_db)
                f = int(bgm_fade_out_s * SR)
                if 0 < f < n:
                    b[:, n - f:] *= np.linspace(1.0, 0.0, f, dtype=np.float32)
                mix += b
            if voice is not None:
                mix += _db(_fit(_stereo(voice), n, False), voice_volume_db)
            peak = float(np.abs(mix).max())
            if peak > 0.98:  # 叠加后爆音就整体压下来
                mix *= 0.98 / peak

            out_dir = folder_paths.get_output_directory()
            folder, name, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, out_dir)
            os.makedirs(folder, exist_ok=True)
            fname = f"{name}_{counter:05}_.mp4"
            path = os.path.join(folder, fname)

            out = av.open(path, "w", format="mp4")
            pkts = []
            if burn:
                # 烧字幕必须重编码画面：逐帧流式解码 → 画字 → 编码（内存只有几帧）
                fps = vs.average_rate or Fraction(24)
                ov = out.add_stream("libx264", rate=fps)
                ov.width, ov.height, ov.pix_fmt = vs.codec_context.width, vs.codec_context.height, "yuv420p"
                ov.options = {"crf": "20", "preset": "veryfast"}
                for i, fr in enumerate(inp.decode(vs)):
                    t = float(i / fps)
                    txt = next((c[2] for c in cues if c[0] <= t < c[1]), None)
                    if txt:
                        nf = av.VideoFrame.from_image(_burn(fr.to_image(), txt, font, subtitle_bottom_margin))
                    else:
                        nf = fr.reformat(format="yuv420p")
                    nf.pts, nf.time_base = i, Fraction(1, 1) / fps
                    pkts.extend(ov.encode(nf))
                pkts.extend(ov.encode(None))
            else:
                ov = out.add_stream_from_template(vs)  # 画面流直接复制
                for p in inp.demux(vs):
                    if p.dts is None:
                        continue
                    p.stream = ov
                    pkts.append(p)
            oa = out.add_stream("aac", rate=SR)
            oa.layout = "stereo"
            for pos in range(0, n, CHUNK):
                ch = np.ascontiguousarray(mix[:, pos:pos + CHUNK])
                fr = av.AudioFrame.from_ndarray(ch, format="fltp", layout="stereo")
                fr.sample_rate = SR
                fr.pts = pos
                fr.time_base = Fraction(1, SR)
                pkts.extend(oa.encode(fr))
            pkts.extend(oa.encode(None))
            # 音视频包按时间排序再写入，播放器/网页才能边下边播
            pkts.sort(key=lambda p: float(p.dts * p.time_base))
            for p in pkts:
                out.mux(p)
            out.close()
        finally:
            inp.close()
        return {"ui": {"images": [{"filename": fname, "subfolder": subfolder, "type": "output"}], "animated": (True,)},
                "result": (InputImpl.VideoFromFile(path),)}


# ── 图片轮播成视频 ────────────────────────────────────────────────────────────
RATIOS = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9), "3:4": (3, 4), "4:3": (4, 3)}


def _out_size(ratio, short_side):
    w, h = RATIOS[ratio]
    k = short_side / min(w, h)
    return int(round(w * k / 2) * 2), int(round(h * k / 2) * 2)  # 编码要偶数


def _cover_base(img, out_w, out_h, zoom_max):
    """按「铺满」缩放成比输出大 zoom_max 倍的底图（推拉时从里面取窗口，最清晰处是原生分辨率）"""
    s = max(out_w * zoom_max / img.width, out_h * zoom_max / img.height)
    return img.resize((int(np.ceil(img.width * s)), int(np.ceil(img.height * s))), Image.LANCZOS)


def _kb_frame(base, out_w, out_h, zoom_max, p, zoom_in, pan):
    """Ken Burns 一帧：p∈[0,1]；zoom_in 决定是推近还是拉远；pan=-1/0/1 左右缓移"""
    e = p * p * (3 - 2 * p)  # 缓入缓出
    z = 1 + (zoom_max - 1) * (e if zoom_in else 1 - e)
    ww, wh = out_w * zoom_max / z, out_h * zoom_max / z
    ww, wh = min(ww, base.width), min(wh, base.height)
    x0 = (base.width - ww) / 2 + pan * (base.width - ww) / 2 * (e - 0.5) * 1.0
    y0 = (base.height - wh) / 2
    x0 = min(max(0.0, x0), base.width - ww)
    return np.asarray(base.resize((out_w, out_h), Image.BILINEAR, box=(x0, y0, x0 + ww, y0 + wh)))


class ProSlideshow:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "ratio": (list(RATIOS), {"default": "9:16"}),
            "short_side": ([720, 1080], {"default": 720}),
            "seconds_per_image": ("FLOAT", {"default": 2.5, "min": 0.8, "max": 12.0, "step": 0.1}),
            "transition_s": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 2.0, "step": 0.1}),
            "zoom": ("FLOAT", {"default": 1.15, "min": 1.0, "max": 1.5, "step": 0.01}),
            "fps": ([24, 30], {"default": 24}),
            "filename_prefix": ("STRING", {"default": "video/轮播"})},
            "optional": dict({f"image{i}": ("IMAGE",) for i in range(1, 9)}, fit_audio=("AUDIO",))}

    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("video",)
    FUNCTION = "run"
    CATEGORY = "pro/video"   # 不是输出节点：视频在 run() 里写盘；输出节点会无视「选择分支」被强制执行

    def run(self, ratio, short_side, seconds_per_image, transition_s, zoom, fps, filename_prefix, fit_audio=None, **imgs):
        pil = []
        for k in sorted(imgs):
            t = imgs[k]
            if t is None:
                continue
            for b in t:  # 每个 IMAGE 输入可能是一批图
                pil.append(Image.fromarray((b.cpu().numpy().clip(0, 1) * 255).astype(np.uint8)).convert("RGB"))
        n = len(pil)
        if n == 0:
            raise RuntimeError("[轮播] 没有图片：至少接入一张")
        ow, oh = _out_size(ratio, short_side)
        tr = min(transition_s, seconds_per_image * 0.45) if n > 1 else 0.0
        dur = seconds_per_image
        if fit_audio is not None:  # 总时长 = 配音长度 + 0.8 秒，平均分给每张
            a_len = fit_audio["waveform"].shape[-1] / float(fit_audio["sample_rate"])
            dur = max(0.8, (a_len + 0.8 + (n - 1) * tr) / n)
        total = n * dur - (n - 1) * tr
        nframes = max(1, int(round(total * fps)))
        bases = [_cover_base(im, ow, oh, zoom) for im in pil]
        del pil

        folder, name, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, folder_paths.get_output_directory())
        os.makedirs(folder, exist_ok=True)
        fname = f"{name}_{counter:05}_.mp4"
        path = os.path.join(folder, fname)
        out = av.open(path, "w", format="mp4")
        ov = out.add_stream("libx264", rate=fps)
        ov.width, ov.height, ov.pix_fmt = ow, oh, "yuv420p"
        ov.options = {"crf": "20", "preset": "veryfast"}
        step = dur - tr  # 相邻两张的起点间隔
        for i in range(nframes):
            t = i / fps
            k = min(n - 1, int(t // step)) if step > 0 else 0
            def frame_of(j):
                return _kb_frame(bases[j], ow, oh, zoom, min(1.0, max(0.0, (t - j * step) / dur)), j % 2 == 0, (1 if j % 4 < 2 else -1))
            f = frame_of(k)
            if k > 0 and tr > 0 and t < k * step + tr:  # 处在与上一张的转场里：交叉淡化
                a = (t - k * step) / tr
                f = (frame_of(k - 1).astype(np.float32) * (1 - a) + f.astype(np.float32) * a).astype(np.uint8)
            fr = av.VideoFrame.from_ndarray(f, format="rgb24")
            fr.pts, fr.time_base = i, Fraction(1, fps)
            for p in ov.encode(fr):
                out.mux(p)
        for p in ov.encode(None):
            out.mux(p)
        out.close()
        return {"ui": {"images": [{"filename": fname, "subfolder": subfolder, "type": "output"}], "animated": (True,)},
                "result": (InputImpl.VideoFromFile(path),)}


NODE_CLASS_MAPPINGS = {"ProVideoDub": ProVideoDub, "ProVideoAudio": ProVideoAudio, "ProSubtitles": ProSubtitles, "ProSlideshow": ProSlideshow}
NODE_DISPLAY_NAME_MAPPINGS = {"ProVideoDub": "视频配音合成（配音 + 背景音乐 + 可烧字幕）", "ProVideoAudio": "视频取音轨（不解码画面）",
                              "ProSubtitles": "字幕生成（文字 + 配音 → SRT）", "ProSlideshow": "图片轮播成视频（推拉镜头 + 转场）"}
