"""流程控制节点：让一个工作流里「选哪条路」只靠顶部一个中文下拉。

- 模式节点（ProMode*）：中文下拉 → 输出「走第几路」(branch) 和随模式变化的常量（开关、指令文字…），放在画布最上面。
- 选择分支（ProPick*）：按 branch 选一路输出。没选中的那一路**不会执行**（lazy），所以不会调接口、不花钱，
  也不会因为没填那一路的 Key 而报错。
- 开关门（ProGate）：enabled 为假时输出 None（下游当作没连），为真时放行 value；同样 lazy。
- 应用界面文字（ProAppText）：让文字结果（AI 写的提示词 / 文案 / 出错信息）能在 ComfyUI 的「应用」界面里显示（见该类的说明）。

注意：输出节点（保存 / 预览）不能直接接在某一路的节点上，否则 ComfyUI 会为了它强制执行那一路；要经过选择分支再接。
本文件只用标准库（folder_paths 在运行时才 import），tools/gen_workflows.py 和 tests 直接 import 它拿模式表（MODES）。
"""
import json
import os
import re

# ── AI 写提示词用的扩写指令（「阿里 写提示词」节点的第二个框）──────────────────────────────────────────────
# 放这里是因为模式节点要把它们当常量输出（同一个工作流里，改图 / 合成、写文案 / 看图 / 翻译用的指令不同）；
# tools/gen_workflows.py 也从这里取，不另写一份。
_ONLY = "只输出{}本身，不要解释，不要加引号或标题。"
TEMPLATES = {
    "image": "你是生图提示词写手。把下面的一句话需求扩写成一段中文生图提示词（80~150 字）：写清主体、场景、光线、构图、风格和画质；"
             "忠于原意，不要添加需求里没有的主体；画面里不要出现任何文字、水印、标志。" + _ONLY.format("提示词"),
    "video": "你是文生视频提示词写手。把下面的一句话需求扩写成一段中文视频提示词（60~120 字）：写清主体、动作、场景、镜头运动（如跟拍/推近/环绕）、光线和风格；"
             "只描述一个连续镜头，不要切镜头，画面里不要出现文字字幕。" + _ONLY.format("提示词"),
    # 图生视频（要接图，让模型看图）：先把图里的主体 / 背景 / 光线写进提示词，再写怎么动。不能只写「怎么动」：实测 Veo 网关拿到没有主体的提示词
    # （「镜头缓慢推近，物体轻轻摆动」）会回一句话而不出片（Google 不触发视频工具），有具体主体的才出片；参考图有没有被网关传给 Veo 也没证实，所以提示词自己要把画面交代清楚
    "video_image": "你是图生视频提示词写手。你会看到一张图（视频的第一帧 / 参考图）和用户的一句话需求（想让画面怎么动）。请写成一段中文视频提示词（60~120 字）：先用一两句话写清图里有什么——"
                   "主体的外观（形状、颜色、材质）、背景和光线，只写你看得到的，不要编造；再写画面怎么动——主体的动作、镜头运动（缓慢推近/环绕/平移/固定）；主体的外观和背景保持不变；"
                   "只描述一个连续镜头，不要切镜头，画面里不要出现文字字幕。" + _ONLY.format("提示词"),
    # 改图：商品要保持原样是硬要求（含糊的说法会让模型原样返回原图，也会让商品被重画），所以模板里写死
    "edit": "你是商品图编辑指令写手。用户会用一句话说明想怎么修改商品照片，请写成一条清楚的中文改图指令（60~120 字）：开头写「请编辑这张商品照片：」；"
            "明确写出要改什么（例如把背景换成什么材质、什么光线，或去掉什么、改成什么颜色）；用户没有要求改的部分"
            "（商品的形状、细节、颜色、包装上的文字、角度）一律写明保持原样不要改；换背景时要写明在商品下方加柔和自然的接触阴影；"
            "不要添加任何文字、标签、水印；末尾写「专业电商产品图。」" + _ONLY.format("指令"),
    "scene": "你是商品场景图指令写手。用户会用一句话描述想要的场景，请写成一条中文合成指令（80~150 字）：开头写「请以图1的商品照片为素材，创作一张全新的电商场景图"
             "（不要直接返回原图）：」；接着写清场景（地点、台面和背景材质、光线、氛围、少量陪衬物），把商品自然地放进场景；商品的形状、颜色、包装上的文字"
             "都保持原样不要改动，光影和透视与场景一致；画面里不要添加任何文字、标签、水印；末尾写「专业商品摄影。」" + _ONLY.format("指令"),
    "compose": "你是商品合成指令写手。图1是商品照片，图2是场景照片。用户会用一句话说明想怎么合成，请写成一条中文合成指令（60~120 字）：开头写"
               "「请把图1里的商品自然地放进图2的场景里，生成一张全新的合成图（不要直接返回其中任何一张原图）：」；写清商品放在场景里的位置、大小和朝向；"
               "商品的形状、颜色、包装上的文字都保持原样不要改动；光影、透视、比例与图2的场景一致；末尾写「专业商品摄影。」" + _ONLY.format("指令"),
    "promo": "你是电商宣传图指令写手。用户会用一句话说明想要的宣传图风格，请写成一条中文指令（80~150 字）：开头写「请以图1的商品照片为素材，创作一张全新的电商宣传图"
             "（不要直接返回原图）：」；写清背景、光线、配色和氛围，商品居中突出；构图要能适配 1:1、3:4、9:16、16:9 多种画幅；商品的形状、颜色、包装上的文字"
             "都保持原样不要改动；画面里不要添加任何文字、标签、水印或小牌子；末尾写「专业电商摄影。」" + _ONLY.format("指令"),
    "music": "你是配乐提示词写手。把下面的一句话需求扩写成一段中文音乐创作提示词（50~100 字）：写清风格、情绪、主要乐器、节奏速度和用途；"
             "纯音乐，不要歌词；时长约 30 秒。" + _ONLY.format("提示词"),
    "voice": "你是配音音色描述写手。把下面的一句话需求扩写成一段中文音色描述（40~80 字）：写清性别和年龄感、音色质感、语速、情绪语气和适用场景；"
             "只描述声音，不要写台词。" + _ONLY.format("描述"),
    # 海报：文字必须一字不差，所以不让 AI 写整段提示词，只让它按固定格式填 4 个字段，再由「海报提示词」节点用「」原样嵌进去
    "poster": ("你是电商促销海报文案策划。根据下面的一句话需求，严格按下面 4 行格式输出，不要输出任何其他内容：\n"
               "标题：（4~8 个字，醒目）\n副标题：（8~16 个字，写核心卖点或优惠）\n角标：（2~6 个字，如「限时三天」，没有合适的就留空）\n"
               "画面元素：（5~8 个具体的视觉元素，用顿号分隔，不要写文字）\n"
               "要求：全部用中文；价格、折扣、日期、数字一律照搬需求里的写法（需求写 199 就写 199，不要改成汉字），需求里没有就不要编造；标题、副标题、角标里不要用英文和特殊符号。"),
    # 文案：用户消息是商品信息（或要翻译的原文），指令决定写什么
    "copy": "你是电商文案写手。根据用户给的商品信息，写 3 条淘宝标题（每条不超过 30 字，含核心卖点），再写 5 条商品详情页卖点短句。"
            "中文，不要编造用户没给的参数。",
    "copy_image": "你是电商文案写手。看这张商品图，结合用户给的补充信息，写 1 个电商标题（30 字内）和 3 条卖点（每条 15 字内）。"
                  "中文，不要写图里看不出来、用户也没给的参数。",
    "translate": "你是跨境电商文案翻译。把下面的商品标题和卖点分别翻译成：英语、日语、韩语、西班牙语。每种语言前单独一行写【语言名】。"
                 "保持电商营销口吻、简洁有吸引力，符合当地表达习惯，不要逐字直译，不要添加原文没有的参数。只输出译文。",
}
T = TEMPLATES

# 模式表：类名 → (显示名, 下拉选项, 输出 [(名字, 类型, 每个选项对应的值)])。
# 新增一个模式节点只改这里；生成工作流的脚本和测试都读这张表，不会和节点实现各写一份。
# 约定：branch / source / subs / upscale / labels 这类 INT 接「选择分支」的 branch；BOOLEAN 接「开关门」的 enabled 或节点自带的开关。
MODES = {
    "ProModeImage": ("模式：文生图选引擎",
                     ["网关 Gemini（自然、约 1K）", "阿里 qwen-image（2K，中文字准）"],
                     [("branch", "INT", [0, 1])]),
    "ProModePoster": ("模式：海报",
                      ["网关 Gemini · 不带商品图", "网关 Gemini · 带商品图（要上传商品图）", "阿里 qwen-image 2K · 不带商品图（文字更准）"],
                      [("branch", "INT", [0, 0, 1]), ("use_product", "BOOLEAN", [False, True, False])]),
    "ProModeEdit": ("模式：商品改图与合成",
                    ["网关 · 改商品图（换背景 / 去水印 / 换色…）", "网关 · 把商品放进场景图（要再传场景图）",
                     "阿里 · 改商品图（换背景 / 去水印 / 换色…）", "阿里 · 把商品放进场景图（要再传场景图）"],
                    [("branch", "INT", [0, 0, 1, 1]), ("compose", "BOOLEAN", [False, True, False, True]),
                     ("instruction", "STRING", [T["edit"], T["compose"], T["edit"], T["compose"]])]),
    "ProModeProcess": ("模式：图片后处理",
                       ["放大 2 倍", "贴促销标签", "先放大 2 倍，再贴促销标签"],
                       [("upscale", "INT", [1, 0, 1]), ("labels", "INT", [0, 1, 1])]),
    "ProModeCopy": ("模式：文案",
                    ["文字写文案（按商品信息）", "看图写文案（要上传商品图）", "把已有文案翻译成多语言"],
                    [("instruction", "STRING", [T["copy"], T["copy_image"], T["translate"]]), ("use_image", "BOOLEAN", [False, True, False])]),
    "ProModeVoice": ("模式：配音",
                     ["预置音色（在配音节点里选）", "设计新音色（文字描述 → 新音色）", "克隆声音（要上传样音）"],
                     [("branch", "INT", [0, 1, 2])]),
    "ProModeMusic": ("模式：音乐",
                     ["Gemini Lyria（约 1 分钟，成功率约一半）", "Suno（要有 Suno 渠道）"],
                     [("branch", "INT", [0, 1])]),
    "ProModeTextVideo": ("模式：文生视频成片",
                         ["只出视频（保留 Veo 自带声音）", "出视频，再配音 + 烧字幕"],
                         [("dub", "BOOLEAN", [False, True]), ("keep_original", "BOOLEAN", [True, False])]),
    "ProModeCompose": ("模式：视频成片",
                       ["上传视频 + 配音文案（替换原声）", "上传视频 + 听写字幕（保留原声）", "图片轮播 + 配音文案"],
                       [("source", "INT", [0, 0, 1]), ("subs", "INT", [0, 1, 0]), ("use_voice", "BOOLEAN", [True, False, True]),
                        ("use_bgm", "BOOLEAN", [True, False, True]), ("keep_original", "BOOLEAN", [False, True, False])]),
    "ProModeLayer": ("模式：图层合成",
                     ["放到背景图上", "放到纯色背景上"],
                     [("use_bg", "BOOLEAN", [True, False])]),
    "ProModeDialogueVideo": ("模式：对白成片",
                             ["对白配到上传的视频上（替换原声，烧字幕）", "图片轮播 + 对白（烧字幕）"],
                             [("source", "INT", [0, 1])]),
}


def _mode_class(name, title, options, outputs):
    class Mode:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"mode": (list(options), {"default": options[0], "display_name": "模式"})}}

        RETURN_TYPES = tuple(t for _, t, _ in outputs)
        RETURN_NAMES = tuple(n for n, _, _ in outputs)
        FUNCTION = "go"
        CATEGORY = "pro/flow"

        def go(self, mode):
            i = options.index(mode)
            return tuple(vals[i] for _, _, vals in outputs)

    for _, _, vals in outputs:
        assert len(vals) == len(options), f"{name}: 每个输出都要给出每个选项对应的值"
    Mode.__name__ = Mode.__qualname__ = name
    return Mode


def _pick_class(branches, channels):
    ins = [(b, c) for b in range(branches) for c in range(channels)]

    class Pick:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"branch": ("INT", {"default": 0, "min": 0, "max": branches - 1, "display_name": "走第几路（从 0 数）"})},
                    "optional": {f"in{b}_{c}": ("*", {"lazy": True}) for b, c in ins}}

        RETURN_TYPES = ("*",) * channels
        RETURN_NAMES = tuple(f"out{c}" for c in range(channels))
        FUNCTION = "pick"
        CATEGORY = "pro/flow"

        def check_lazy_status(self, branch, **kw):
            # 只要求选中那一路的输入。ComfyUI 会把「还没算」的惰性输入以 None 传进来，所以「是 None」就要求；
            # 算过且值本来就是 None 的（比如开关门关着），ComfyUI 自己会滤掉，不会死循环
            return [f"in{branch}_{c}" for c in range(channels) if kw.get(f"in{branch}_{c}") is None]

        def pick(self, branch, **kw):
            return tuple(kw.get(f"in{branch}_{c}") for c in range(channels))

    name = f"ProPick{branches}x{channels}"
    Pick.__name__ = Pick.__qualname__ = name
    return name, Pick


class ProGate:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"enabled": ("BOOLEAN", {"default": True, "label_on": "用", "label_off": "不用", "display_name": "启用"})},
                "optional": {"value": ("*", {"lazy": True})}}

    RETURN_TYPES = ("*",)
    RETURN_NAMES = ("value",)
    FUNCTION = "gate"
    CATEGORY = "pro/flow"

    def check_lazy_status(self, enabled, **kw):
        return ["value"] if enabled and kw.get("value") is None else []

    def gate(self, enabled, value=None):
        return (value if enabled else None,)


def _app_text(text, only_on_error):
    """要显示的文字；None = 这次不显示。空文字不显示；only_on_error 时，成功（{"code": "success", ...}）不显示，失败显示出错原因。"""
    if text is None:
        return None
    s = text if isinstance(text, str) else str(text)
    if not s.strip():
        return None
    if only_on_error:
        try:
            d = json.loads(s)
        except ValueError:
            d = None
        if isinstance(d, dict):
            if d.get("code") == "success":
                return None
            if d.get("message"):
                return f"出错了：{d['message']}"
    return s


class ProAppText:
    """让文字结果在 ComfyUI 的「应用」界面（App 模式）里显示。

    应用界面只显示「文件类」结果（图片 / 视频 / 音频 / 文字文件）；预览节点（PreviewAny）返回的是裸字符串，应用界面不认，
    所以 AI 写的提示词 / 文案、relayapi 的出错信息要存成 .txt（output/text/）并按文件结果返回才看得到。
    - 空文字不存、不显示（比如「预置音色」模式下没有音色描述）；
    - 「只在出错时显示」给状态用：relayapi 成功时的 {"code": "success", ...} 不显示，失败原因才显示，应用界面里就不会多一堆没用的 JSON；
    - 名称就是应用界面里这条结果的标题。
    节点图里它折叠成一条蓝色的小条，不用管；节点图里看文字仍用旁边的预览节点。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "text": ("STRING", {"forceInput": True}),
            "label": ("STRING", {"default": "文字结果", "display_name": "名称（应用界面里显示的标题）"}),
            "only_on_error": ("BOOLEAN", {"default": False, "label_on": "只在出错时显示", "label_off": "总是显示", "display_name": "显示时机"})}}

    RETURN_TYPES = ()
    FUNCTION = "show"
    OUTPUT_NODE = True
    CATEGORY = "pro/flow"

    def show(self, text, label, only_on_error):
        shown = _app_text(text, only_on_error)
        if shown is None:
            return {"ui": {}}
        import folder_paths
        name = re.sub(r'[\\/:*?"<>|\r\n]+', "-", label).strip() or "文字结果"
        folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path("text/" + name, folder_paths.get_output_directory())
        os.makedirs(folder, exist_ok=True)
        fname = f"{filename}_{counter:05}.txt"
        with open(os.path.join(folder, fname), "w", encoding="utf-8") as f:
            f.write(shown)
        return {"ui": {"files": [{"filename": fname, "subfolder": subfolder, "type": "output", "display_name": label}]}}


def _build():
    classes, names = {"ProGate": ProGate, "ProAppText": ProAppText}, {"ProGate": "开关门（不用时输出空）", "ProAppText": "应用界面：文字结果（折叠的小条，不用管）"}
    for b in (2, 3, 4):
        for c in (1, 2, 3):
            n, cls = _pick_class(b, c)
            classes[n] = cls
            names[n] = f"选择分支（{b} 路 × {c} 个输出）"
    for n, (title, options, outputs) in MODES.items():
        classes[n] = _mode_class(n, title, options, outputs)
        names[n] = title
    return classes, names


NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = _build()
