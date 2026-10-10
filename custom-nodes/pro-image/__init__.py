"""图片节点：促销标签精确叠加 / 商品抠图 / 图层合成。

促销标签：用真字体渲染文字（限时三天 / ¥99 / 满199减50 …），文字 100% 准确；AI 生成的海报文字偶尔会错字，
这种「必须一字不差」的价格、活动语适合用它叠加。字体：宿主机的 fonts-noto-cjk（部署脚本会只读挂进容器），
也可用环境变量 PRO_SUBTITLE_FONT 指定。

商品抠图 / 图层合成：把商品从照片里抠成透明底的一层（本地跑一个小模型，不调接口、不花钱），再和背景、阴影一层层叠起来；
每一层都是真正的像素，商品不会像 AI 改图那样被重画（形状、颜色、包装上的字都原样），换背景、改位置、改大小只重做叠的这一步。
"""
import os

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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


# ── 商品抠图 ──────────────────────────────────────────────────────────────────
# 引擎（本地 ONNX 小模型、下载模型文件、单独的小进程里跑）在 matte_engine.py；这里是下拉的名字、边缘处理、和 ComfyUI 的节点。
from . import matte_engine as engine  # noqa: E402

MATTE_MODELS = engine.MATTE_MODELS
# 下拉里显示的名字 → 模型（第一个是默认）
MATTE_LABELS = {
    "通用（u2net，约 176MB：只抠画面里最主要的那一个）": "u2net",
    "最省内存（u2netp，约 5MB：精度差一点，内存只要通用的一半）": "u2netp",
    "更利落（isnet，约 170MB：边缘更干净，但画面里别的物体也会留下，更占内存）": "isnet-general-use",
}
# 边缘处理：模型出的是一张「像不像前景」的概率图，边缘一圈是软的，里面混着背景色，叠到别的背景上会有一圈灰白边。
# (拉硬下限, 拉硬上限, 往里缩几个像素, 柔化半径)，像素按最长边 1024 算（更大的图按比例缩到 1024 再做）
MATTE_EDGES = {"标准（去掉白边）": (0.40, 0.80, 2, 1.0), "轻（只去一点点）": (0.30, 0.75, 1, 0.8), "强（边缘更紧）": (0.50, 0.90, 3, 1.0), "不处理（模型原样）": None}
MATTE_WORK_SIDE = 1024


def refine_alpha(alpha, edge):
    """概率图 → 边缘利落的透明度：先拉硬（低于下限算背景、高于上限算前景，中间线性过渡），再往里缩几个像素去掉混着背景色的一圈，最后轻轻柔化。
    在最长边不超过 1024 的尺寸上做（更大的图按比例缩着做、再放大回来：形态学滤波在几千像素的图上很慢，而模型本身的精度也就这么高；小图原尺寸做）。"""
    cfg = MATTE_EDGES.get(edge, MATTE_EDGES["标准（去掉白边）"]) if isinstance(edge, str) else edge
    if cfg is None:
        return alpha
    lo, hi, shrink, soften = cfg
    work = alpha
    if max(alpha.size) > MATTE_WORK_SIDE:
        k = MATTE_WORK_SIDE / max(alpha.size)
        work = alpha.resize((max(1, round(alpha.width * k)), max(1, round(alpha.height * k))), Image.LANCZOS)
    work = work.point([int(round(min(1.0, max(0.0, (v / 255 - lo) / (hi - lo))) * 255)) for v in range(256)])
    if shrink:
        work = work.filter(ImageFilter.MinFilter(2 * shrink + 1))
    if soften:
        work = work.filter(ImageFilter.GaussianBlur(soften))
    return work if work.size == alpha.size else work.resize(alpha.size, Image.LANCZOS)


def matte_image(img, model="u2net", edge="标准（去掉白边）", predict=None):
    """一张图（PIL）→ 透明底的 RGBA（颜色是原图的，透明度是抠出来的）。predict(img) → 前景概率图，默认在单独的小进程里跑模型。"""
    if model not in MATTE_MODELS:
        raise RuntimeError(f"[商品抠图] 不认识的抠图模型「{model}」（可选：{'、'.join(MATTE_MODELS)}）")
    rgb = img.convert("RGB")
    alpha = refine_alpha((predict or (lambda im: engine.run_worker(im, model)))(rgb), edge)
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def trim_to_content(rgba, threshold=8, pad=0):
    """裁掉四周的透明空白（透明度不超过 threshold 的算空白）。全透明（什么都没抠到）报错。"""
    box = rgba.split()[3].point(lambda v: 255 if v > threshold else 0).getbbox()
    if box is None:
        raise RuntimeError("没有抠到商品：这张图是全透明的（换一种抠图模型，或者商品和背景太接近）")
    return rgba.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(rgba.width, box[2] + pad), min(rgba.height, box[3] + pad)))


# ── 图层合成 ──────────────────────────────────────────────────────────────────
MAX_CANVAS_SIDE = 4096     # 合成画布最长边的上限：每一层都是整张画布大小的 RGBA，几千万像素的背景图会把 4GB 内存的服务器吃光
COMPOSE_POSITIONS = ["正中", "下中", "上中", "左中", "右中", "左下", "右下", "左上", "右上"]
SHADOWS = ["地面接触阴影", "柔和投影", "没有阴影"]
RATIOS = {"1:1": (1, 1), "3:4": (3, 4), "4:3": (4, 3), "9:16": (9, 16), "16:9": (16, 9)}


def parse_color(text, default=(255, 255, 255)):
    """「#FFFFFF」「ffffff」「#fff」→ (r, g, b)；写错了用 default。"""
    t = (text or "").strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    try:
        return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4)) if len(t) == 6 else default
    except ValueError:
        return default


def fit_scale(canvas_wh, obj_wh, pct):
    """缩放倍数：让商品的外框放进画面宽高的 pct%（宽和高两边都不超过）。"""
    return min(canvas_wh[0] * pct / 100 / obj_wh[0], canvas_wh[1] * pct / 100 / obj_wh[1])


def make_shadow(canvas_wh, placed_alpha, box, style, strength, softness):
    """阴影层（RGBA：黑色，只有透明度）。placed_alpha = 商品已经放到画布上的透明度（'L'，和画布一样大），box = 商品的外框 (x0, y0, x1, y1)。
    地面接触阴影：商品底下一个压扁的椭圆；柔和投影：商品形状往右下偏一点再模糊。"""
    w, h = box[2] - box[0], box[3] - box[1]
    layer = Image.new("L", canvas_wh, 0)
    if style == "柔和投影":
        layer.paste(placed_alpha, (int(w * 0.03), int(h * 0.04)))
        blur = max(1.0, w * 0.035 * softness)
    else:
        ew, eh = max(4, int(w * 0.9)), max(4, int(min(w * 0.14, h * 0.25)))
        cx, cy = (box[0] + box[2]) // 2, box[3] - int(eh * 0.35)
        ImageDraw.Draw(layer).ellipse((cx - ew // 2, cy - eh // 2, cx + ew // 2, cy + eh // 2), fill=255)
        blur = max(1.0, eh * 0.45 * softness)
    layer = layer.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * max(0.0, min(1.0, strength))))
    out = Image.new("RGBA", canvas_wh, (0, 0, 0, 0))
    out.putalpha(layer)
    return out


def compose(product, background=None, canvas_wh=(1024, 1024), bg_color=(255, 255, 255), position="正中", scale_pct=60.0, margin_pct=4.0,
            offset=(0.0, 0.0), shadow="地面接触阴影", strength=0.35, softness=1.0):
    """商品（RGBA 抠图）放到背景上，返回各层：{"final": RGB 合成图, "background": RGBA, "product": RGBA（整张画布大小，只有商品）, "shadow": RGBA}。
    background=None 时用纯色（bg_color）铺满 canvas_wh；有背景图就以背景图的大小为画布。"""
    canvas = background.convert("RGBA") if background is not None else Image.new("RGBA", canvas_wh, tuple(bg_color) + (255,))
    if max(canvas.size) > MAX_CANVAS_SIDE:
        k = MAX_CANVAS_SIDE / max(canvas.size)
        canvas = canvas.resize((max(1, round(canvas.width * k)), max(1, round(canvas.height * k))), Image.LANCZOS)
    prod = trim_to_content(product.convert("RGBA"))
    k = fit_scale(canvas.size, prod.size, scale_pct)
    pw, ph = max(1, round(prod.width * k)), max(1, round(prod.height * k))
    prod = prod.resize((pw, ph), Image.LANCZOS)
    x, y = place(canvas.size, (pw, ph), position, int(canvas.width * margin_pct / 100))
    x, y = x + int(canvas.width * offset[0] / 100), y + int(canvas.height * offset[1] / 100)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    layer.paste(prod, (x, y))                                  # 不带 mask：原样拷贝 RGBA（含透明度），超出画布的部分自动裁掉
    sh = make_shadow(canvas.size, layer.split()[3], (x, y, x + pw, y + ph), shadow, strength, softness) if shadow != "没有阴影" else Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    final = canvas.copy()
    final.alpha_composite(sh)
    final.alpha_composite(layer)
    return {"final": final.convert("RGB"), "background": canvas, "product": layer, "shadow": sh}


def _to_pil(t):
    """ComfyUI 的 IMAGE 一张 [H, W, C] float 0~1 → PIL RGB。"""
    return Image.fromarray((t.cpu().numpy().clip(0, 1) * 255).astype(np.uint8)).convert("RGB")


def _alpha_from_mask(m, size):
    """ComfyUI 的 MASK（LoadImage 的透明度反过来：1 = 透明）一张 [H, W] → 'L' 透明度，缩到 size。"""
    a = Image.fromarray(np.rint((1.0 - m.cpu().numpy().clip(0, 1)) * 255).astype(np.uint8), "L")
    return a if a.size == size else a.resize(size, Image.LANCZOS)


def _slot(prefix):
    """output/ 里下一个可用的文件序号：(文件夹, 名字, 序号, 子目录)。同一次合成的合成图和各层共用一个序号（_00001_.png / _00001_背景层.png …）。"""
    import folder_paths
    folder, name, counter, subfolder, _ = folder_paths.get_save_image_path(prefix, folder_paths.get_output_directory())
    os.makedirs(folder, exist_ok=True)
    return folder, name, counter, subfolder


def _save(img, slot, suffix=""):
    """存成 output/<名字>_00001_<suffix>.png，返回前端要用的 {filename, subfolder, type}。"""
    folder, name, counter, subfolder = slot
    fname = f"{name}_{counter:05}_{suffix}.png"
    img.save(os.path.join(folder, fname))
    return {"filename": fname, "subfolder": subfolder, "type": "output"}


class ProMatte:
    """商品抠图：照片 → 透明底的商品（本地小模型，不调接口、不花钱，一张约几秒）。输出原图的颜色 + 抠出来的透明度（MASK 和 LoadImage 的一样：1 = 透明）。"""
    @classmethod
    def INPUT_TYPES(cls):
        first = next(iter(MATTE_LABELS))
        return {"required": {
            "image": ("IMAGE",),
            "model": (list(MATTE_LABELS), {"default": first, "tooltip": "默认通用就够用；商品边缘很细、通用抠不干净再换「更利落」（更占内存，画面里别的物体也会留下）。第一次用某个模型要先下载它（约几十到一百多 MB），之后不用再下"}),
            "edge": (list(MATTE_EDGES), {"default": "标准（去掉白边）", "tooltip": "模型抠出来的边缘是软的，叠到别的背景上会有一圈白边 / 灰边；标准会把边缘收紧一点点"})}}

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    FUNCTION = "run"
    CATEGORY = "pro/image"

    def run(self, image, model, edge):
        name = MATTE_LABELS.get(model, model)
        masks = []
        for b in image:
            a = np.asarray(matte_image(_to_pil(b), name, edge).split()[3], dtype=np.float32) / 255.0
            masks.append(torch.from_numpy(1.0 - a))
        return (image, torch.stack(masks))                     # 图片输出就是输入本身（颜色原样，透明度在 mask 里）：不用再复制一份，大图省几百 MB 内存


class ProSaveCutout:
    """把抠好的商品存成透明底的 PNG（output/）。存之前可以裁掉四周的透明空白。"""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",), "mask": ("MASK",), "trim": ("BOOLEAN", {"default": True, "label_on": "裁掉四周的透明空白", "label_off": "保持原图大小"}),
                             "filename_prefix": ("STRING", {"default": "商品抠图/商品"})}}

    RETURN_TYPES = ()
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "pro/image"

    def run(self, image, mask, trim, filename_prefix):
        files = []
        for k, b in enumerate(image):
            rgba = _to_pil(b).convert("RGBA")
            rgba.putalpha(_alpha_from_mask(mask[min(k, len(mask) - 1)], rgba.size))
            files.append(_save(trim_to_content(rgba) if trim else rgba, _slot(filename_prefix)))
        return {"ui": {"images": files}}


class ProLayerCompose:
    """图层合成：商品（透明底）放到背景图（或纯色）上，加阴影，调位置和大小。每一层都是真像素，商品原样不变。
    可以同时存分层 PNG（背景 / 商品 / 阴影，和合成图一样大，在别的软件里能按原位置叠回去）。"""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "product": ("IMAGE",), "product_mask": ("MASK",),
            "position": (COMPOSE_POSITIONS, {"default": "正中", "tooltip": "商品放在画面的哪里：正中 / 下中 / 上中 / 左中 / 右中 / 四个角"}),
            "scale_pct": ("FLOAT", {"default": 60.0, "min": 5.0, "max": 100.0, "step": 1.0, "tooltip": "商品占画面的大小：商品的外框放进画面宽高的这个百分比里，默认 60"}),
            "margin_pct": ("FLOAT", {"default": 4.0, "min": 0.0, "max": 30.0, "step": 0.5, "tooltip": "靠边的位置（左上、右下……）离边多远，占画面宽度的百分比；正中不用"}),
            "offset_x_pct": ("FLOAT", {"default": 0.0, "min": -50.0, "max": 50.0, "step": 1.0, "tooltip": "在选好的位置上再往右挪（负数往左），占画面宽度的百分比"}),
            "offset_y_pct": ("FLOAT", {"default": 0.0, "min": -50.0, "max": 50.0, "step": 1.0, "tooltip": "在选好的位置上再往下挪（负数往上），占画面高度的百分比"}),
            "shadow": (SHADOWS, {"default": "地面接触阴影", "tooltip": "地面接触阴影（商品放在地面 / 桌面上）/ 柔和投影（商品悬空）/ 没有阴影"}),
            "shadow_strength": ("FLOAT", {"default": 0.35, "min": 0.0, "max": 1.0, "step": 0.05}),
            "shadow_softness": ("FLOAT", {"default": 1.0, "min": 0.2, "max": 3.0, "step": 0.1, "tooltip": "阴影边缘有多虚，1 是默认"}),
            "bg_color": ("STRING", {"default": "#FFFFFF", "multiline": False, "tooltip": "纯色背景的颜色，只在「放到纯色背景上」模式用，如 #FFFFFF（白）、#F5F0E6（米色）"}),
            "ratio": (list(RATIOS), {"default": "1:1", "tooltip": "纯色背景的比例，只在「放到纯色背景上」模式用；放到背景图上时画面大小就是背景图的大小"}),
            "short_side": ("INT", {"default": 1024, "min": 256, "max": 2048, "step": 64, "tooltip": "没有背景图时画面短边的像素；背景图太大（最长边超过 4096）会先缩到 4096 再合成"}),
            "save_layers": ("BOOLEAN", {"default": False, "label_on": "同时存分层 PNG", "label_off": "只存合成图",
                                        "tooltip": "合成图之外再存背景层 / 商品层 / 阴影层（和合成图一样大）。合成图是第一张；ComfyUI 自带的应用界面会把最后存的那张当主图，所以会先看到阴影层，在工作台里没有这个问题"}),
            "filename_prefix": ("STRING", {"default": "图层合成/商品"})},
            "optional": {"background": ("IMAGE",)}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "pro/image"

    def run(self, product, product_mask, position, scale_pct, margin_pct, offset_x_pct, offset_y_pct, shadow, shadow_strength, shadow_softness,
            bg_color, ratio, short_side, save_layers, filename_prefix, background=None):
        w, h = RATIOS[ratio]
        k = short_side / min(w, h)
        outs, files = [], []
        for n, b in enumerate(product):
            prod = _to_pil(b).convert("RGBA")
            prod.putalpha(_alpha_from_mask(product_mask[min(n, len(product_mask) - 1)], prod.size))
            if prod.split()[3].getextrema()[0] >= 250:                                                         # 整张图都不透明：商品没抠过，放上去只会是一个方块
                raise RuntimeError("[图层合成] 这张商品图没有透明底（整张图都是不透明的）：先用「商品抠图」把商品抠出来（或上传抠好的透明底 PNG），再放进来")
            bg = _to_pil(background[min(n, len(background) - 1)]) if background is not None else None
            r = compose(prod, bg, (round(w * k), round(h * k)), parse_color(bg_color), position, scale_pct, margin_pct, (offset_x_pct, offset_y_pct), shadow, shadow_strength, shadow_softness)
            slot = _slot(filename_prefix)
            files.append(_save(r["final"], slot))                                                               # 合成图排在最前面：后面的步骤取「第 1 张图」就是它
            if save_layers:
                files += [_save(r[key], slot, name) for key, name in (("background", "背景层"), ("product", "商品层"), ("shadow", "阴影层"))]
            outs.append(torch.from_numpy(np.asarray(r["final"]).astype(np.float32) / 255.0))
        return {"ui": {"images": files}, "result": (torch.stack(outs),)}


NODE_CLASS_MAPPINGS = {"ProLabels": ProLabels, "ProMatte": ProMatte, "ProSaveCutout": ProSaveCutout, "ProLayerCompose": ProLayerCompose}
NODE_DISPLAY_NAME_MAPPINGS = {"ProLabels": "促销标签叠加（价格 / 活动语，文字精确）", "ProMatte": "商品抠图（照片 → 透明底，本地模型）",
                              "ProSaveCutout": "保存抠好的商品（透明底 PNG）", "ProLayerCompose": "图层合成（商品 + 背景 + 阴影，可存分层 PNG）"}
