"""图片节点：促销标签精确叠加。

用真字体渲染文字（限时三天 / ¥99 / 满199减50 …），文字 100% 准确；AI 生成的海报文字偶尔会错字，
这种「必须一字不差」的价格、活动语适合用它叠加。字体：宿主机的 fonts-noto-cjk（部署脚本会只读挂进容器），
也可用环境变量 PRO_SUBTITLE_FONT 指定。
"""
import os

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
]
POSITIONS = ["左上", "右上", "左下", "右下", "上中", "下中", "正中"]
# 样式：(底色 RGB 或 None, 文字色, 描边色)
STYLES = {
    "红底白字": ((222, 32, 44), (255, 255, 255), None), "黄底黑字": ((255, 207, 0), (24, 24, 24), None),
    "黑底金字": ((22, 22, 22), (255, 208, 96), None), "蓝底白字": ((24, 92, 220), (255, 255, 255), None),
    "绿底白字": ((20, 152, 72), (255, 255, 255), None), "白底红字": ((255, 255, 255), (222, 32, 44), None),
    "无底白字描边": (None, (255, 255, 255), (0, 0, 0)),
}
SHAPES = ["圆角矩形", "胶囊", "直角", "圆形"]
SS = 3  # 超采样倍数：先在 3 倍大的图层上画，再缩回来，边缘才不会有锯齿


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
            if p.endswith(".ttc") and not f.getname()[0].endswith("SC"):
                continue
            return f
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    raise RuntimeError("[促销标签] 需要中文字体：容器里没有。用部署脚本重建容器（会挂载宿主机的 fonts-noto-cjk），或设环境变量 PRO_SUBTITLE_FONT")


def render_label(text, style, shape, font_px):
    """画一张标签（RGBA，已缩回 1 倍）。text 里的 \\n 是换行。"""
    bg, fg, stroke = STYLES[style]
    f = _font(font_px * SS)
    lines = text.replace("\\n", "\n").split("\n")
    d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lh = int(font_px * SS * 1.2)
    tw = max(int(d0.textlength(l, font=f)) for l in lines)
    th = lh * len(lines)
    padx, pady = int(font_px * SS * 0.5), int(font_px * SS * 0.32)
    if bg is None:
        padx = pady = int(font_px * SS * 0.15)
    w, h = tw + padx * 2, th + pady * 2
    if shape == "圆形":
        w = h = int(max(w, h) * 1.05)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if bg is not None:
        col = bg + (255,)
        if shape == "圆形":
            d.ellipse((0, 0, w - 1, h - 1), fill=col)
        elif shape == "胶囊":
            d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=col)
        elif shape == "直角":
            d.rectangle((0, 0, w - 1, h - 1), fill=col)
        else:
            d.rounded_rectangle((0, 0, w - 1, h - 1), radius=int(font_px * SS * 0.35), fill=col)
    y = (h - th) // 2
    for l in lines:
        x = (w - d0.textlength(l, font=f)) / 2
        kw = {"stroke_width": max(2, font_px * SS // 12), "stroke_fill": stroke + (255,)} if stroke else {}
        d.text((x, y), l, font=f, fill=fg + (255,), **kw)
        y += lh
    return im.resize((max(1, w // SS), max(1, h // SS)), Image.LANCZOS)


def place(base_wh, label_wh, pos, margin):
    """位置名 → 左上角坐标。「左上/右下…」是 横向+纵向；「上中/下中/正中」横向居中"""
    W, H = base_wh
    w, h = label_wh
    hz, v = ("中", pos[0]) if pos in ("上中", "下中", "正中") else (pos[0], pos[1])
    x = margin if hz == "左" else W - margin - w if hz == "右" else (W - w) // 2
    y = margin if v == "上" else H - margin - h if v == "下" else (H - h) // 2
    return int(x), int(y)


class ProLabels:
    @classmethod
    def INPUT_TYPES(cls):
        req = {"image": ("IMAGE",), "shape": (SHAPES, {"default": "圆角矩形"}),
               "margin_pct": ("FLOAT", {"default": 3.0, "min": 0.0, "max": 20.0, "step": 0.5})}
        defaults = [("限时三天", "左上", "红底白字", 5.0), ("满199减50", "右下", "黄底黑字", 6.0), ("", "左下", "黑底金字", 4.5)]
        for i, (t, p, st, sz) in enumerate(defaults, 1):
            req[f"text{i}"] = ("STRING", {"default": t, "multiline": False})
            req[f"position{i}"] = (POSITIONS, {"default": p})
            req[f"style{i}"] = (list(STYLES), {"default": st})
            req[f"size{i}_pct"] = ("FLOAT", {"default": sz, "min": 1.0, "max": 30.0, "step": 0.5})
        return {"required": req}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "run"
    CATEGORY = "pro/image"

    def run(self, image, shape, margin_pct, **kw):
        outs = []
        for b in image:
            im = Image.fromarray((b.cpu().numpy().clip(0, 1) * 255).astype(np.uint8)).convert("RGBA")
            W, H = im.size
            for i in (1, 2, 3):
                text = kw[f"text{i}"].strip()
                if not text:
                    continue
                lab = render_label(text, kw[f"style{i}"], shape, max(8, int(W * kw[f"size{i}_pct"] / 100)))
                x, y = place(im.size, lab.size, kw[f"position{i}"], int(W * margin_pct / 100))
                im.alpha_composite(lab, (max(0, x), max(0, y)))
            outs.append(torch.from_numpy(np.asarray(im.convert("RGB")).astype(np.float32) / 255.0))
        return (torch.stack(outs),)


NODE_CLASS_MAPPINGS = {"ProLabels": ProLabels}
NODE_DISPLAY_NAME_MAPPINGS = {"ProLabels": "促销标签叠加（价格 / 活动语，文字精确）"}
