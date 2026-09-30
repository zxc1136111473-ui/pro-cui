"""视频配音合成节点：给视频配上「配音 + 背景音乐」，画面流直接复制（不解码、不重编码）。

为什么自己写：ComfyUI 自带的 GetVideoComponents / CreateVideo 会把整段视频解码成浮点张量
（10 秒 720p ≈ 2.6GB），在 4GB 内存的机器上会被系统 OOM 杀掉。这里只用 PyAV 复制画面包 + 编码一条 AAC 音轨，内存很小。
"""
import os
from fractions import Fraction

import av
import folder_paths
import numpy as np
import torch
import torchaudio.functional as taf
from comfy_api.latest import InputImpl

SR = 44100
CHUNK = 1024  # AAC 一帧的采样数


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


def _original_audio(src):
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


class ProVideoDub:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "video": ("VIDEO",),
            "bgm_volume_db": ("FLOAT", {"default": -14.0, "min": -60.0, "max": 12.0, "step": 0.5}),
            "voice_volume_db": ("FLOAT", {"default": 0.0, "min": -60.0, "max": 12.0, "step": 0.5}),
            "bgm_fade_out_s": ("FLOAT", {"default": 1.5, "min": 0.0, "max": 10.0, "step": 0.1}),
            "keep_original_audio": ("BOOLEAN", {"default": False, "label_on": "保留原视频声音", "label_off": "替换原视频声音"}),
            "filename_prefix": ("STRING", {"default": "video/成片"})},
            "optional": {"voice": ("AUDIO",), "bgm": ("AUDIO",)}}

    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("video",)
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "pro/video"

    def run(self, video, bgm_volume_db, voice_volume_db, bgm_fade_out_s, keep_original_audio, filename_prefix, voice=None, bgm=None):
        if voice is None and bgm is None and not keep_original_audio:
            raise RuntimeError("[视频合成] 没有任何音频：请接入配音或背景音乐，或勾选保留原视频声音")
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
                orig = _original_audio(src)
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
            ov = out.add_stream_from_template(vs)
            oa = out.add_stream("aac", rate=SR)
            oa.layout = "stereo"
            pkts = []
            for p in inp.demux(vs):
                if p.dts is None:
                    continue
                p.stream = ov
                pkts.append(p)
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


NODE_CLASS_MAPPINGS = {"ProVideoDub": ProVideoDub}
NODE_DISPLAY_NAME_MAPPINGS = {"ProVideoDub": "视频配音合成（配音 + 背景音乐，不重编码画面）"}
