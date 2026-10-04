#!/usr/bin/env python3
# 生成内置工作流模板（workflows/*.json 的来源，改模板改这里再运行：python3 tools/gen_workflows.py）。
# 模板里不带 key：key 存在服务器 relayapi 的 relay_config.json 的 node_settings[节点 id] 里。
import json, os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workflows")
os.makedirs(OUT, exist_ok=True)

def wf(nodes, links, last_node, last_link):
    return {"last_node_id": last_node, "last_link_id": last_link, "nodes": nodes,
            "links": links, "groups": [], "config": {}, "extra": {}, "version": 0.4}

# 类别文件夹（编号不变：文档和对话里都按编号称呼；侧栏里会显示成树）
FOLDERS = {"1-一条龙": ("12", "20"), "2-图片生成": ("01", "03", "10", "14", "17", "18"),
           "3-改图与合成": ("02", "06", "11", "13", "15", "24"), "4-文案": ("08", "09", "21"),
           "5-配音与音乐": ("05", "07", "16", "25", "26"), "6-视频": ("04", "19", "22", "23")}


def write(name, obj):
    folder = next((d for d, nums in FOLDERS.items() if name[:2] in nums), None)
    assert folder, f"{name} 没有归到任何类别文件夹（改 FOLDERS）"
    p = os.path.join(OUT, folder, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print("写出:", p)

# 06 图片放大（不用任何模型/密钥：Lanczos 2 倍 + 轻度锐化，CPU 上 1~2 秒）
# 上游生图（gemini-image）实际只出约 1K，选 2K/4K 无效；要大图就把出的图丢进这里放大 2 倍（可重复两次得 4 倍）。
def upscale_nodes():
    return [
        {"id": 31, "type": "LoadImage", "pos": [60, 120], "size": [380, 400], "flags": {}, "order": 0, "mode": 0, "inputs": [],
         "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1], "slot_index": 0},
                     {"name": "MASK", "type": "MASK", "links": None, "slot_index": 1}],
         "properties": {"Node name for S&R": "LoadImage"}, "widgets_values": ["demo_product.png", "image"]},
        {"id": 32, "type": "ImageScaleBy", "pos": [500, 120], "size": [320, 90], "flags": {}, "order": 1, "mode": 0,
         "inputs": [{"name": "image", "type": "IMAGE", "link": 1}],
         "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [2], "slot_index": 0}],
         "properties": {"Node name for S&R": "ImageScaleBy"}, "widgets_values": ["lanczos", 2.0]},
        {"id": 33, "type": "ImageSharpen", "pos": [500, 260], "size": [320, 120], "flags": {}, "order": 2, "mode": 0,
         "inputs": [{"name": "image", "type": "IMAGE", "link": 2}],
         "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [3], "slot_index": 0}],
         "properties": {"Node name for S&R": "ImageSharpen"}, "widgets_values": [1, 1.0, 0.5]},
        {"id": 34, "type": "SaveImage", "pos": [880, 120], "size": [340, 320], "flags": {}, "order": 3, "mode": 0,
         "inputs": [{"name": "images", "type": "IMAGE", "link": 3}], "outputs": [],
         "properties": {"Node name for S&R": "SaveImage"}, "widgets_values": ["upscaled"]},
    ]

write("06-图片放大-2倍.json", wf(upscale_nodes(),
    [[1, 31, 0, 32, 0, "IMAGE"], [2, 32, 0, 33, 0, "IMAGE"], [3, 33, 0, 34, 0, "IMAGE"]], 34, 3))

# ════════════════════════════════════════════════════════════════════════════
# 搭图辅助（除 06 外的模板都用它：按名字连线、自动编号，比手写 links 不容易错）
# ════════════════════════════════════════════════════════════════════════════
def _imgs(n):
    return [(f"image{i}", "IMAGE") for i in range(1, n + 1)]

# 类型 → (输入槽 [(名字, 类型, 是否 widget 输入)], 输出槽 [(名字, 类型)], 连线时才出现的 widget 输入名)
SPEC = {
    "RelayAPISettings": ([], [("STRING", "STRING")], []),
    "RelayImageGenerator": ([("info", "STRING", True)] + [(n, t, False) for n, t in _imgs(16)],
                            [("image", "IMAGE"), ("response", "STRING"), ("image_url", "STRING")], ["prompt"]),
    "RelayTextGenerator": ([("info", "STRING", True)] + [(n, t, False) for n, t in _imgs(8)],
                           [("text", "STRING"), ("response", "STRING")], ["prompt"]),
    "LoadImage": ([], [("IMAGE", "IMAGE"), ("MASK", "MASK")], []),
    "SaveImage": ([("images", "IMAGE", False)], [], []),
    "ImageScaleBy": ([("image", "IMAGE", False)], [("IMAGE", "IMAGE")], []),
    "ImageSharpen": ([("image", "IMAGE", False)], [("IMAGE", "IMAGE")], []),
    "PreviewAny": ([("source", "*", False)], [], []),
    "ImageScaleToMaxDimension": ([("image", "IMAGE", False)], [("IMAGE", "IMAGE")], []),
    "ProPosterPrompt": ([], [("prompt", "STRING")], ["title", "subtitle", "badge", "elements"]),
    "ProPosterFields": ([("text", "STRING", False)], [("title", "STRING"), ("subtitle", "STRING"), ("badge", "STRING"), ("elements", "STRING")], []),
    "RelaySoundGenerator": ([("info", "STRING", True)], [("audio", "AUDIO"), ("clip_id", "STRING"), ("task_id", "STRING"), ("response", "STRING"), ("audio_url", "STRING")], ["prompt"]),
    "ProGeminiMusic": ([("info", "STRING", True)], [("audio", "AUDIO"), ("response", "STRING")], ["prompt"]),
    "ProAliImage": ([("info", "STRING", True)], [("image", "IMAGE"), ("response", "STRING")], ["prompt"]),
    "ProAliImageEdit": ([("info", "STRING", True), ("image1", "IMAGE", False), ("image2", "IMAGE", False), ("image3", "IMAGE", False)],
                        [("image", "IMAGE"), ("response", "STRING")], ["prompt"]),
    "ProAliPromptWriter": ([("info", "STRING", True)], [("text", "STRING"), ("response", "STRING")], []),
    "ProAliTTS": ([("info", "STRING", True)], [("audio", "AUDIO"), ("response", "STRING")], ["text", "custom_voice"]),
    "ProAliVoiceDesign": ([("info", "STRING", True)], [("voice", "STRING"), ("preview", "AUDIO")], ["voice_prompt"]),
    "ProAliVoiceClone": ([("audio", "AUDIO", False), ("info", "STRING", True)], [("voice", "STRING")], []),
    "SaveAudioAdvanced": ([("audio", "AUDIO", False)], [], []),
    "LoadVideo": ([], [("VIDEO", "VIDEO")], []),
    "SaveVideo": ([("video", "VIDEO", False)], [], []),
    "LoadAudio": ([], [("AUDIO", "AUDIO")], []),
    "ProVideoDub": ([("video", "VIDEO", False), ("voice", "AUDIO", False), ("bgm", "AUDIO", False), ("subtitles", "STRING", False)], [("video", "VIDEO")], []),
    "ProSubtitles": ([("voice", "AUDIO", False)], [("srt", "STRING")], ["text"]),
    "ProVideoAudio": ([("video", "VIDEO", False)], [("audio", "AUDIO")], []),
    "ProAliASR": ([("audio", "AUDIO", False), ("info", "STRING", True)], [("text", "STRING"), ("response", "STRING")], []),
    "StringConcatenate": ([("string_a", "STRING", True), ("string_b", "STRING", True)], [("STRING", "STRING")], []),
    "RelayVideoGenerator": ([("info", "STRING", True)] + [(n, t, False) for n, t in _imgs(7)],
                            [("video", "VIDEO"), ("task_id", "STRING"), ("response", "STRING"), ("video_url", "STRING")], ["prompt"]),
    "PrimitiveStringMultiline": ([], [("STRING", "STRING")], []),
    "ProLabels": ([("image", "IMAGE", False)], [("image", "IMAGE")], []),
    "ProSlideshow": ([(n, t, False) for n, t in _imgs(8)] + [("fit_audio", "AUDIO", False)], [("video", "VIDEO")], []),
}


class Graph:
    def __init__(self):
        self.nodes, self.links, self._lid, self._order = [], [], 0, 0

    def add(self, nid, ntype, pos, size, widgets=None, title=None):
        ins, outs, _ = SPEC[ntype]
        n = {"id": nid, "type": ntype, "pos": list(pos), "size": list(size), "flags": {}, "order": self._order, "mode": 0,
             "inputs": [dict({"name": a, "type": b, "link": None}, **({"widget": {"name": a}} if c else {})) for a, b, c in ins],
             "outputs": [{"name": a, "type": b, "links": None, "slot_index": i} for i, (a, b) in enumerate(outs)],
             "properties": {"Node name for S&R": ntype}, "widgets_values": widgets if widgets is not None else []}
        if title:
            n["title"] = title
        self._order += 1
        self.nodes.append(n)
        return nid

    def node(self, nid):
        return next(n for n in self.nodes if n["id"] == nid)

    def connect(self, src, out_name, dst, in_name):
        s, d = self.node(src), self.node(dst)
        slot = next(i for i, o in enumerate(s["outputs"]) if o["name"] == out_name)
        inp = next((i for i in d["inputs"] if i["name"] == in_name), None)
        if inp is None:  # 只在连线时才出现的 widget 输入（如 prompt）
            assert in_name in SPEC[d["type"]][2], (d["type"], in_name)
            inp = {"name": in_name, "type": "STRING", "link": None, "widget": {"name": in_name}}
            d["inputs"].append(inp)
        assert inp["link"] is None, f"{dst}.{in_name} 已连过线"
        self._lid += 1
        inp["link"] = self._lid
        s["outputs"][slot]["links"] = (s["outputs"][slot]["links"] or []) + [self._lid]
        self.links.append([self._lid, src, slot, dst, next(i for i, x in enumerate(d["inputs"]) if x is inp), s["outputs"][slot]["type"]])

    def build(self):
        return {"last_node_id": max(n["id"] for n in self.nodes), "last_link_id": self._lid, "nodes": self.nodes,
                "links": self.links, "groups": [], "config": {}, "extra": {}, "version": 0.4}


ALI_TXT_BASE = "https://dashscope.aliyuncs.com/compatible-mode"
ALI_BASE = "https://dashscope.aliyuncs.com"
ali_settings = lambda base, model: ["text", "OpenaiText", "v1/chat/completions", "https://www.runninghub.cn", "claude-opus-4-6", "", base, model]
IMG_SET = ["image", "banana-2", "v1/chat/completions", "https://www.runninghub.cn", "grok-video-3", "", "http://airelay-newapi:3000", "gemini-image"]
WHITE_BG = "请编辑这张商品照片：把原来的背景完全去掉，换成干净、无缝的纯白色（#FFFFFF）影棚背景；商品本身保持原样（形状、细节、颜色、角度都不要改），在商品下方加柔和自然的接触阴影；专业电商产品图。"


def status(g, gen, out="response", pos=(0, 0), title="状态（出错时这里显示原因）", nid=None):
    nid = nid or max(n["id"] for n in g.nodes) + 1  # 节点编号不是连续分配的模板要显式传 nid，避免撞号
    g.add(nid, "PreviewAny", pos, (360, 110), title=title)
    g.connect(gen, out, nid, "source")
    return nid


# ── AI 扩写提示词：一句话需求 → 阿里文字模型（pro-ali 的「阿里 写提示词」节点）→ 接到生成节点的 prompt ──────────
# 用阿里 Settings 节点 id=31（和其他阿里工作流共用一个 Key）。「★ 一句话需求」节点放在画布最上面，
# 原有节点整体下移，打开就能看到该填哪里；扩写结果用 PreviewAny 展示，方便看模型写了什么。
# 不用 relayapi 的文字节点：它出错不抛异常，下游会拿着空提示词白白出图；写提示词节点出错直接报红。
_ONLY = "只输出{}本身，不要解释，不要加引号或标题。"
EXPAND = {
    "image": "你是生图提示词写手。把下面的一句话需求扩写成一段中文生图提示词（80~150 字）：写清主体、场景、光线、构图、风格和画质；"
             "忠于原意，不要添加需求里没有的主体；画面里不要出现任何文字、水印、标志。" + _ONLY.format("提示词"),
    "video": "你是文生视频提示词写手。把下面的一句话需求扩写成一段中文视频提示词（60~120 字）：写清主体、动作、场景、镜头运动（如跟拍/推近/环绕）、光线和风格；"
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
}
# 海报：文字必须一字不差，所以不让 AI 写整段提示词，只让它按固定格式填 4 个字段，再由「海报提示词」节点用「」原样嵌进去
POSTER_EXPAND = ("你是电商促销海报文案策划。根据下面的一句话需求，严格按下面 4 行格式输出，不要输出任何其他内容：\n"
                 "标题：（4~8 个字，醒目）\n副标题：（8~16 个字，写核心卖点或优惠）\n角标：（2~6 个字，如「限时三天」，没有合适的就留空）\n"
                 "画面元素：（5~8 个具体的视觉元素，用顿号分隔，不要写文字）\n"
                 "要求：全部用中文；价格、折扣、日期、数字一律照搬需求里的写法（需求写 199 就写 199，不要改成汉字），需求里没有就不要编造；标题、副标题、角标里不要用英文和特殊符号。")
IDEA_TITLE = "★ 只填这里：一句话需求"


def ai_block(g, instruction, idea, idea_title=IDEA_TITLE, settings_id=31):
    """在画布最上面加「一句话需求 → AI 写提示词」（节点 70，预览 74），其余节点整体下移；返回写提示词节点 id（70）。"""
    have = [n for n in g.nodes if n["id"] == settings_id]
    assert not have or have[0]["type"] == "RelayAPISettings", f"节点 {settings_id} 要留给阿里 Settings（Key 按节点 id 存），换个编号"
    need_settings = not have
    shift = (960 if need_settings else 620) + 60
    for n in g.nodes:
        n["pos"][1] += shift
    if need_settings:
        g.add(settings_id, "RelayAPISettings", (60, 660), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"), title="Relay API Settings（阿里文字）")
    # idea, instruction, model, seed, control_after_generate
    g.add(70, "ProAliPromptWriter", (60, 60), (520, 560), [idea, instruction, "qwen3.8-flash", 1, "randomize"], title=idea_title)
    g.add(74, "PreviewAny", (620, 60), (460, 300), title="AI 扩写出的结果（想看模型写了什么）")
    g.connect(settings_id, "STRING", 70, "info"); g.connect(70, "text", 74, "source")
    return 70


def ai_prompt(g, gen, kind, idea, target="prompt", **kw):
    """给生成节点（可以是 id 列表）的 prompt 前接上「一句话需求 → AI 扩写」。"""
    txt = ai_block(g, EXPAND[kind], idea, **kw)
    for t in (gen if isinstance(gen, (list, tuple)) else [gen]):
        g.connect(txt, "text", t, target)


def ai_poster(g, poster, idea, **kw):
    """海报：AI 按固定格式写文案 → 「海报文案拆分」→ 接到「海报提示词」的标题 / 副标题 / 角标 / 画面元素。"""
    txt = ai_block(g, POSTER_EXPAND, idea, **kw)
    g.add(75, "ProPosterFields", (1120, 60), (300, 140), title="拆成标题 / 副标题 / 角标 / 画面元素")
    g.connect(txt, "text", 75, "text")
    for f in ("title", "subtitle", "badge", "elements"):
        g.connect(75, f, poster, f)


VEO_SET = ["video", "Veo", "v1/videos", "https://www.runninghub.cn", "veo3.1", "", "http://airelay-geminiweb:8083", "gemini-video"]
SUNO_SET = ["sound", "Suno", "suno/submit", "https://www.runninghub.cn", "suno_music", "", "http://airelay-newapi:3000", "suno_music"]
AI_NOTE = "（由 AI 扩写节点提供）"
EDIT_IDEA = "把背景换成干净的纯白色影棚背景，商品保持原样，下方加柔和的接触阴影"


def text_to_image(idea, ratio, prefix):      # 01 / 03
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 120), (400, 300), IMG_SET)
    g.add(2, "RelayImageGenerator", (520, 120), (380, 420), [AI_NOTE, ratio, "1K", "medium", "jpeg", "low", 42, "randomize"])
    g.add(3, "SaveImage", (960, 120), (340, 320), [prefix])
    g.connect(1, "STRING", 2, "info"); g.connect(2, "image", 3, "images")
    status(g, 2, pos=(960, 500), title="结果 / 错误信息（出错时这里显示原因）")
    ai_prompt(g, 2, "image", idea)
    return g.build()


def product_edit():     # 02：商品图换背景改图（网关 gemini-image，LoadImage → image1）
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 120), (400, 300), IMG_SET)
    g.add(4, "LoadImage", (60, 470), (380, 400), ["demo_product.png", "image"], title="① 上传商品图")
    g.add(2, "RelayImageGenerator", (520, 120), (380, 420), [AI_NOTE, "1:1", "1K", "medium", "jpeg", "low", 42, "randomize"], title="改图")
    g.add(3, "SaveImage", (960, 120), (340, 320), ["product"])
    g.connect(1, "STRING", 2, "info"); g.connect(4, "IMAGE", 2, "image1"); g.connect(2, "image", 3, "images")
    status(g, 2, pos=(960, 500), title="结果 / 错误信息（出错时这里显示原因）")
    ai_prompt(g, 2, "edit", EDIT_IDEA)
    return g.build()


def text_to_video():    # 04
    g = Graph()
    g.add(11, "RelayAPISettings", (60, 120), (400, 300), VEO_SET)
    g.add(12, "RelayVideoGenerator", (520, 120), (380, 420), [AI_NOTE, "16:9", "720P", "8", 1, "fixed", "false", "false"])
    g.add(13, "SaveVideo", (960, 120), (340, 360), ["video/文生视频", "auto", "auto"])
    g.connect(11, "STRING", 12, "info"); g.connect(12, "video", 13, "video")
    status(g, 12, out="response", pos=(960, 540), title="结果 / 错误信息（出错时这里显示原因）")
    ai_prompt(g, 12, "video", "一只橘猫在绿色草地上奔跑，写实风格，白天，镜头跟拍")
    return g.build()


def music_suno():       # 05：Suno（经网关的 Suno 渠道，Settings 节点 id=21）
    g = Graph()
    g.add(21, "RelayAPISettings", (60, 120), (400, 300), SUNO_SET)
    # generation_mode, title, tags, prompt, make_instrumental, version, seed, control_after_generate, negative_tags, extend_mode, continue_clip_id, continue_at
    g.add(22, "RelaySoundGenerator", (520, 120), (400, 420), ["描述模式", "", "pop, electronic", AI_NOTE, True, "V4.5", 1, "randomize", "", False, "", 0])
    g.add(23, "SaveAudioAdvanced", (980, 120), (340, 200), ["audio/音乐", "mp3", "V0"])
    g.connect(21, "STRING", 22, "info"); g.connect(22, "audio", 23, "audio")
    status(g, 22, pos=(980, 380), title="结果 / 错误信息（出错时这里显示原因）")
    ai_prompt(g, 22, "music", "一首轻快的电子流行歌曲，适合做短视频配乐")
    return g.build()


def music_gemini():     # 07：Google Lyria（经 geminiweb 的 gemini-music，Settings 节点 id=11，与 04 共用 Key）
    g = Graph()
    g.add(11, "RelayAPISettings", (60, 120), (400, 300), VEO_SET)
    g.add(12, "ProGeminiMusic", (520, 120), (400, 300), [AI_NOTE, "gemini-music", 1, "randomize"])
    g.add(13, "SaveAudioAdvanced", (980, 120), (340, 200), ["audio/Gemini音乐", "mp3", "V0"])
    g.connect(11, "STRING", 12, "info"); g.connect(12, "audio", 13, "audio")
    status(g, 12, pos=(980, 380), title="结果 / 错误信息（出错时这里显示原因）")
    ai_prompt(g, 12, "music", "舒缓钢琴加弦乐纯音乐，温暖治愈，适合产品视频配乐")
    return g.build()


def poster(use_image, idea, defaults, prefix):   # 10 促销海报 / 11 商品海报（带商品图）
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 120), (400, 300), IMG_SET)
    g.add(2, "RelayImageGenerator", (520, 120), (380, 420), ["（由左下「海报提示词」节点生成）", "3:4", "1K", "medium", "jpeg", "low", 42, "randomize"])
    g.add(3, "SaveImage", (960, 120), (340, 320), [prefix])
    # title, subtitle, badge, style, elements, use_product_image, extra —— 标题/副标题/角标/画面元素会被 AI 拆出来的值覆盖，这里的值只是断开连线后的备用
    g.add(5, "ProPosterPrompt", (60, 470), (400, 430), list(defaults) + [use_image, ""], title="海报提示词（风格在这里选；文字由 AI 填）")
    g.connect(1, "STRING", 2, "info"); g.connect(5, "prompt", 2, "prompt"); g.connect(2, "image", 3, "images")
    if use_image:
        g.add(6, "LoadImage", (520, 780), (380, 400), ["demo_product.png", "image"], title="商品图（海报主体）")
        g.connect(6, "IMAGE", 2, "image1")
    status(g, 2, pos=(960, 500), title="结果 / 错误信息（出错时这里显示原因）")
    ai_poster(g, 5, idea)
    return g.build()


# 01 文生图 / 03 封面横图 16:9 / 04 文生视频 / 02 商品图换背景改图 / 05、07 音乐 / 10、11 海报：都是「一句话 → AI 扩写 → 生成」
write("01-文生图.json", text_to_image("一只橘猫坐在窗台上，阳光，写实风格", "1:1", "txt2img"))
write("02-商品图-换背景改图.json", product_edit())
write("03-封面横图-16x9.json", text_to_image("科技感直播封面，蓝紫色渐变背景，中间留白用于放标题", "16:9", "cover"))
write("04-文生视频.json", text_to_video())
write("05-音乐-Suno.json", music_suno())
write("07-音乐-Gemini.json", music_gemini())
write("10-促销海报.json", poster(False, "夏日清凉节促销，全场满199减50，限时三天，清爽冰饮风格",
                               ("夏日清凉节", "全场满199减50", "限时三天", "清爽夏日（蓝白）", "冰饮、柠檬片、水花、椰树叶、遮阳草帽"), "poster"))
write("11-商品海报.json", poster(True, "鲜果季红富士苹果，脆甜多汁，产地直发，自然清新风格",
                               ("鲜果季", "红富士 脆甜多汁", "产地直发", "自然绿意", "树叶、木质托盘、水珠、清晨阳光"), "poster_product"))


# 12 商品一条龙：一张商品图 → 白底主图(放大 2 倍) + 场景图 + 标题/卖点文案，一次跑完
# Settings：图片 id=1（网关 Key）、文字 id=2（同一个网关 Key；服务器上要预置 node_settings["2"]）
def pipeline():
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 60), (400, 300), IMG_SET)
    g.add(31, "RelayAPISettings", (60, 1240), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-omni-flash"), title="Relay API Settings（阿里文字）")
    g.add(3, "LoadImage", (60, 420), (400, 420), ["demo_product.png", "image"], title="① 上传商品图")
    # A 白底主图 → 放大 → 保存
    g.add(4, "RelayImageGenerator", (540, 60), (400, 420), [WHITE_BG, "1:1", "1K", "medium", "jpeg", "low", 42, "randomize"], title="② 白底主图")
    g.add(5, "ImageScaleBy", (1000, 60), (300, 90), ["lanczos", 2.0], title="放大 2 倍")
    g.add(6, "ImageSharpen", (1000, 200), (300, 120), [1, 1.0, 0.5])
    g.add(7, "SaveImage", (1360, 60), (340, 300), ["pipeline/白底主图"], title="保存：白底主图")
    # B 场景图
    g.add(8, "RelayImageGenerator", (540, 740), (400, 420), [AI_NOTE, "3:4", "1K", "medium", "jpeg", "low", 43, "randomize"], title="③ 场景图")
    g.add(9, "SaveImage", (1000, 740), (340, 300), ["pipeline/场景图"], title="保存：场景图")
    # C 文案
    g.add(10, "RelayTextGenerator", (540, 1440), (420, 300),
          ["看这张商品图，写 1 个电商标题（30 字内）和 3 条卖点（每条 15 字内），中文，不要写图里看不出来的参数。", 44, "randomize"], title="④ 标题 / 卖点文案")
    g.add(11, "PreviewAny", (1000, 1440), (460, 260), title="生成的文案")
    for a, b in [(1, 4), (1, 8)]:
        g.connect(a, "STRING", b, "info")
    g.connect(31, "STRING", 10, "info")
    for t in (4, 8):
        g.connect(3, "IMAGE", t, "image1")
    # 文字分支的图先缩到最长边 768：大图从服务器传到阿里很慢（实测 1024 的 PNG 超过 180 秒，缩小后约 10 秒）
    g.add(15, "ImageScaleToMaxDimension", (540, 1780), (320, 90), ["lanczos", 768], title="缩到最长边 768（别删）")
    g.connect(3, "IMAGE", 15, "image"); g.connect(15, "IMAGE", 10, "image1")
    g.connect(4, "image", 5, "image"); g.connect(5, "IMAGE", 6, "image"); g.connect(6, "IMAGE", 7, "images")
    g.connect(8, "image", 9, "images")
    g.connect(10, "text", 11, "source")
    status(g, 4, pos=(1000, 380), title="状态：白底主图"); status(g, 8, pos=(1000, 1080), title="状态：场景图"); status(g, 10, pos=(1000, 1740), title="状态：文案")
    ai_prompt(g, 8, "scene", "明亮的现代厨房台面，窗边自然光", idea_title="★ 只填这里：场景一句话（只管③场景图，白底主图和文案不受影响）")
    return g.build()

write("12-商品一条龙.json", pipeline())


# 13 商品场景合成：商品图(图1) + 场景图(图2) → 商品自然放进场景（实测杯子放进厨房台面，花纹形状保持、光影一致）

def compose():
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 60), (400, 300), IMG_SET)
    g.add(2, "LoadImage", (60, 420), (400, 420), ["demo_product.png", "image"], title="① 商品图（图1）")
    g.add(3, "LoadImage", (60, 900), (400, 420), ["demo_product.png", "image"], title="② 场景图（图2，换成你的场景）")
    g.add(4, "RelayImageGenerator", (540, 60), (400, 420), [AI_NOTE, "4:3", "1K", "medium", "jpeg", "low", 42, "randomize"], title="合成")
    g.add(5, "SaveImage", (1000, 60), (340, 300), ["compose/商品场景合成"], title="保存")
    g.connect(1, "STRING", 4, "info"); g.connect(2, "IMAGE", 4, "image1"); g.connect(3, "IMAGE", 4, "image2")
    g.connect(4, "image", 5, "images")
    status(g, 4, pos=(1000, 420))
    ai_prompt(g, 4, "compose", "商品放在场景里最合理的位置，大小自然")
    return g.build()

write("13-商品场景合成.json", compose())


# ════════════════════════════════════════════════════════════════════════════
# 阿里百炼系列（Settings 节点 id=31，Key 存服务器 relay_config.json 的 node_settings["31"]）
# 文字：RelayTextGenerator 指向兼容接口 …/compatible-mode；生图/改图/配音：自带 pro-ali 节点（地址只取 host）
# ════════════════════════════════════════════════════════════════════════════
COPY_PROMPT = "为一款便携榨汁杯写 3 条淘宝标题（每条不超过 30 字，含核心卖点），再写 5 条商品详情页卖点短句。"
IMG_COPY_PROMPT = "看这张商品图，写 1 个电商标题（30 字内）和 3 条卖点（每条 15 字内），中文，不要写图里看不出来的参数。"


def copy_text():      # 08 文案生成
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"))
    g.add(2, "RelayTextGenerator", (520, 120), (420, 300), [COPY_PROMPT, 1, "randomize"])
    g.add(3, "PreviewAny", (1000, 120), (460, 320), title="生成的文案")
    g.connect(31, "STRING", 2, "info"); g.connect(2, "text", 3, "source")
    status(g, 2, pos=(1000, 500))
    return g.build()


def copy_from_image():   # 09 看图写文案（图先缩到最长边 768：大图从服务器传到阿里很慢，实测 1024 的 PNG 超过 180 秒，缩小后约 10 秒）
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-omni-flash"))
    g.add(4, "LoadImage", (60, 470), (400, 420), ["demo_product.png", "image"], title="上传商品图")
    g.add(5, "ImageScaleToMaxDimension", (520, 520), (320, 90), ["lanczos", 768], title="缩到最长边 768（别删）")
    g.add(2, "RelayTextGenerator", (900, 120), (420, 300), [IMG_COPY_PROMPT, 1, "randomize"])
    g.add(3, "PreviewAny", (1380, 120), (460, 320), title="生成的文案")
    g.connect(31, "STRING", 2, "info"); g.connect(4, "IMAGE", 5, "image"); g.connect(5, "IMAGE", 2, "image1"); g.connect(2, "text", 3, "source")
    status(g, 2, pos=(1380, 500))
    return g.build()


write("08-文案生成.json", copy_text())
write("09-看图写文案.json", copy_from_image())


def ali_out(g, gen, prefix, pos_save=(1000, 120)):
    g.add(90, "SaveImage", pos_save, (340, 320), [prefix], title="保存")
    g.connect(gen, "image", 90, "images")
    status(g, gen, pos=(pos_save[0], pos_save[1] + 380))


def ali_image():      # 14 高清出图（阿里）
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"))
    g.add(2, "ProAliImage", (520, 120), (420, 420), ["（由 AI 扩写节点提供）", "qwen-image-2.0-pro", "3:4", "2K", 1, "randomize"], title="阿里 文生图（2K）")
    g.connect(31, "STRING", 2, "info"); ali_out(g, 2, "ali/高清出图")
    ai_prompt(g, 2, "image", "一杯冰美式咖啡放在木桌上，自然光，产品摄影，细节丰富")
    return g.build()


def ali_edit():       # 15 商品改图（阿里）：一句话说明怎么改（换背景 / 去水印 / 换颜色 / 改文字等），AI 写成精确的改图指令
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"))
    g.add(4, "LoadImage", (60, 470), (400, 420), ["demo_product.png", "image"], title="① 上传商品图（图1）")
    g.add(2, "ProAliImageEdit", (520, 120), (440, 460), [AI_NOTE, "qwen-image-edit-max", 1, "randomize"], title="阿里 改图（一句话需求可做局部修改）")
    g.connect(31, "STRING", 2, "info"); g.connect(4, "IMAGE", 2, "image1"); ali_out(g, 2, "ali/商品改图", (1020, 120))
    ai_prompt(g, 2, "edit", EDIT_IDEA)
    return g.build()


def ali_tts():        # 16 配音（阿里）
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_BASE, "qwen3.8-flash"))
    g.add(2, "ProAliTTS", (520, 120), (440, 420), ["夏日清凉节，全场满一百九十九减五十，限时三天，欢迎选购。", "Cherry", "qwen3-tts-flash", "Chinese", "", 1.0, 0.0, ""], title="阿里 配音（instruct 模型可写语气指令）")
    g.add(3, "SaveAudioAdvanced", (1020, 120), (340, 200), ["audio/配音", "mp3", "V0"], title="保存配音")
    g.connect(31, "STRING", 2, "info"); g.connect(2, "audio", 3, "audio")
    status(g, 2, pos=(1020, 380))
    return g.build()


def ali_poster():     # 17 高清海报（阿里）：海报提示词节点 → 阿里 2K 出图
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"))
    g.add(5, "ProPosterPrompt", (60, 470), (400, 430), ["夏日清凉节", "全场满199减50", "限时三天", "清爽夏日（蓝白）", "冰饮、柠檬片、水花、椰树叶、遮阳草帽", False, ""], title="海报提示词（风格在这里选；文字由 AI 填）")
    g.add(2, "ProAliImage", (520, 120), (440, 420), ["（由左下「海报提示词」节点生成）", "qwen-image-2.0-pro", "3:4", "2K", 1, "randomize"], title="阿里 文生图（2K）")
    g.connect(31, "STRING", 2, "info"); g.connect(5, "prompt", 2, "prompt"); ali_out(g, 2, "ali/高清海报")
    ai_poster(g, 5, "夏日清凉节促销，全场满199减50，限时三天，清爽冰饮风格")
    return g.build()


write("14-高清出图-阿里.json", ali_image())
write("15-商品改图-阿里.json", ali_edit())
write("16-配音-阿里.json", ali_tts())
write("17-高清海报-阿里.json", ali_poster())


# ════════════════════════════════════════════════════════════════════════════
# 18 多尺寸套图 / 19 成片合成 / 20 文生视频成片
# ════════════════════════════════════════════════════════════════════════════


def size_set():       # 18：同一张商品图、同一段提示词，一次出 4 个平台尺寸（ComfyUI 队列按顺序跑，约 1.5 分钟）
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 60), (400, 300), IMG_SET)
    g.add(2, "LoadImage", (60, 420), (400, 420), ["demo_product.png", "image"], title="① 上传商品图")
    plats = [("1:1", "主图 1:1（淘宝/拼多多）", "sizes/主图1x1"), ("3:4", "小红书 3:4", "sizes/小红书3x4"),
             ("9:16", "抖音 / 视频号 9:16", "sizes/抖音9x16"), ("16:9", "封面 16:9", "sizes/封面16x9")]
    for i, (ratio, title, prefix) in enumerate(plats):
        y = 60 + i * 680
        gid, sid = 10 + i, 20 + i
        g.add(gid, "RelayImageGenerator", (540, y), (400, 420), [AI_NOTE, ratio, "1K", "medium", "jpeg", "low", 100 + i, "randomize"], title=title)
        g.add(sid, "SaveImage", (1000, y), (340, 300), [prefix], title="保存：" + title)
        g.connect(1, "STRING", gid, "info"); g.connect(2, "IMAGE", gid, "image1")
        g.connect(gid, "image", sid, "images")
        status(g, gid, pos=(1000, y + 340), title="状态：" + title, nid=50 + i)  # 31 号留给阿里 Settings（Key 按节点 id 存）
    ai_prompt(g, [10, 11, 12, 13], "promo", "干净明亮的浅色背景，商品居中突出，柔和自然的光影和阴影", idea_title="★ 只填这里：一句话需求（4 个尺寸共用）")
    return g.build()


DUB_WIDGETS = [-14, 0, 1.5, False, "video/成片", "烧进画面", 46, 70]  # 背景音乐/配音音量、淡出、保留原声、文件名、字幕模式、字号、底边距
SCRIPT = "夏日清凉节，全场满一百九十九减五十，限时三天，欢迎选购。"


def dub():            # 19：视频 + 配音 + 背景音乐 + 字幕 → 成片（不烧字幕时画面不重编码）
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_BASE, "qwen3.8-flash"))
    g.add(2, "LoadVideo", (60, 470), (400, 400), ["请上传视频.mp4", "image"], title="① 上传视频（点节点上的上传按钮）")
    g.add(6, "PrimitiveStringMultiline", (520, 60), (440, 200), [SCRIPT], title="② 配音文案（同时用于配音和字幕）")
    g.add(3, "ProAliTTS", (520, 320), (440, 420), ["（由文案框提供）", "Cherry", "qwen3-tts-flash", "Chinese", "", 1.0, 0.0, ""], title="配音")
    g.add(7, "ProSubtitles", (520, 800), (440, 260), ["（由文案框提供）", 16, 0.0, "subtitles/字幕"], title="字幕（按配音停顿对齐，同时存 .srt）")
    g.add(4, "LoadAudio", (60, 930), (400, 200), ["无背景音乐.wav", "", ""], title="③ 背景音乐（默认是静音文件=不加；想加就上传自己的）")
    g.add(5, "ProVideoDub", (1020, 60), (420, 480), DUB_WIDGETS, title="④ 合成成片（字幕模式可选「不加字幕」）")
    g.connect(31, "STRING", 3, "info"); g.connect(6, "STRING", 3, "text"); g.connect(6, "STRING", 7, "text"); g.connect(3, "audio", 7, "voice")
    g.connect(2, "VIDEO", 5, "video"); g.connect(3, "audio", 5, "voice"); g.connect(4, "AUDIO", 5, "bgm"); g.connect(7, "srt", 5, "subtitles")
    status(g, 3, pos=(1020, 600), title="状态：配音")
    return g.build()


def sub_video():      # 22：给已有视频加字幕：抽音轨 → 阿里听写 → 按停顿对齐 → 烧进画面（保留原声）
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_BASE, "qwen3.8-flash"))
    g.add(2, "LoadVideo", (60, 470), (400, 400), ["请上传视频.mp4", "image"], title="① 上传有人声的视频")
    g.add(8, "ProVideoAudio", (520, 120), (300, 90), title="取音轨（不解码画面）")
    g.add(9, "ProAliASR", (520, 270), (400, 160), ["auto"], title="② 阿里听写")
    g.add(10, "PreviewAny", (520, 480), (440, 200), title="听写结果（可先看一眼对不对）")
    g.add(7, "ProSubtitles", (1000, 120), (440, 260), ["（由听写结果提供）", 16, 0.0, "subtitles/字幕"], title="③ 字幕（同时存 .srt）")
    g.add(5, "ProVideoDub", (1000, 460), (420, 480), [-14, 0, 1.5, True, "video/加字幕", "烧进画面", 46, 70], title="④ 烧字幕（保留原声）")
    g.connect(31, "STRING", 9, "info"); g.connect(2, "VIDEO", 8, "video"); g.connect(8, "audio", 9, "audio")
    g.connect(9, "text", 10, "source"); g.connect(9, "text", 7, "text"); g.connect(8, "audio", 7, "voice")
    g.connect(2, "VIDEO", 5, "video"); g.connect(7, "srt", 5, "subtitles")
    status(g, 9, pos=(520, 720), title="状态：听写")
    return g.build()


TRANSLATE = ("你是跨境电商文案翻译。把下面的商品标题和卖点分别翻译成：英语、日语、韩语、西班牙语。每种语言前单独一行写【语言名】。"
             "保持电商营销口吻、简洁有吸引力，符合当地表达习惯，不要逐字直译，不要添加原文没有的参数。只输出译文。")
SRC_COPY = "标题：新鲜带叶红苹果 果形圆润 脆甜多汁\n卖点：\n1. 果皮红润光滑，光泽饱满\n2. 带果梗鲜叶，新鲜看得见\n3. 果形圆润匀称，品相出众"


def translate():      # 21：多语言文案（阿里通用文字模型，实测英/日质量地道）
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"))
    g.add(4, "PrimitiveStringMultiline", (60, 470), (460, 260), [TRANSLATE], title="翻译要求（改语言在这里改）")
    g.add(5, "PrimitiveStringMultiline", (60, 780), (460, 300), [SRC_COPY], title="原文（换成你的中文文案）")
    g.add(6, "StringConcatenate", (580, 470), (340, 140), ["", "", "\n\n"], title="拼成完整指令")
    g.add(2, "RelayTextGenerator", (580, 120), (420, 300), ["（由「拼成完整指令」提供）", 1, "randomize"])
    g.add(3, "PreviewAny", (1060, 120), (500, 480), title="译文")
    g.connect(31, "STRING", 2, "info"); g.connect(4, "STRING", 6, "string_a"); g.connect(5, "STRING", 6, "string_b")
    g.connect(6, "STRING", 2, "prompt"); g.connect(2, "text", 3, "source")
    status(g, 2, pos=(1060, 660))
    return g.build()


def video_pipeline():  # 20：文生视频（Veo，每天约 3 个额度）→ 配音 + 字幕 → 成片，一条龙
    g = Graph()
    g.add(11, "RelayAPISettings", (60, 60), (400, 300), ["video", "Veo", "v1/videos", "https://www.runninghub.cn", "veo3.1", "", "http://airelay-geminiweb:8083", "gemini-video"], title="Relay API Settings（geminiweb 视频）")
    g.add(31, "RelayAPISettings", (60, 420), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"), title="Relay API Settings（阿里：扩写 + 配音）")
    g.add(12, "RelayVideoGenerator", (520, 60), (420, 420), ["（由 AI 扩写节点提供）", "16:9", "720P", "8", 1, "fixed", "false", "false"], title="① 文生视频（Veo）")
    g.add(6, "PrimitiveStringMultiline", (520, 560), (440, 200), ["快来看，这只橘猫在草地上撒欢奔跑，太可爱啦！"], title="② 配音文案（同时用于配音和字幕）")
    g.add(3, "ProAliTTS", (520, 820), (440, 420), ["（由文案框提供）", "Cherry", "qwen3-tts-flash", "Chinese", "", 1.0, 0.0, ""], title="配音")
    g.add(7, "ProSubtitles", (520, 1300), (440, 260), ["（由文案框提供）", 16, 0.0, "subtitles/字幕"], title="字幕（按配音停顿对齐）")
    g.add(4, "LoadAudio", (60, 780), (440, 200), ["无背景音乐.wav", "", ""], title="③ 背景音乐（默认是静音文件=不加；想加就上传自己的）")
    g.add(5, "ProVideoDub", (1020, 60), (420, 480), DUB_WIDGETS, title="④ 合成成片（替换 Veo 自带声音）")
    g.connect(11, "STRING", 12, "info"); g.connect(31, "STRING", 3, "info"); g.connect(6, "STRING", 3, "text"); g.connect(6, "STRING", 7, "text")
    g.connect(3, "audio", 7, "voice"); g.connect(12, "video", 5, "video"); g.connect(3, "audio", 5, "voice"); g.connect(4, "AUDIO", 5, "bgm")
    g.connect(7, "srt", 5, "subtitles")
    status(g, 12, pos=(1020, 620), title="状态：视频", nid=40); status(g, 3, pos=(1020, 780), title="状态：配音", nid=41)
    ai_prompt(g, 12, "video", "一只橘猫在绿色草地上奔跑，写实风格，白天，镜头跟拍")
    return g.build()


write("18-多尺寸套图.json", size_set())
write("19-成片合成.json", dub())
write("20-文生视频成片.json", video_pipeline())
write("21-多语言文案.json", translate())
write("22-视频加字幕.json", sub_video())


def slideshow():      # 23：图片轮播短视频：图片 → 推拉 + 转场（时长跟配音走）→ 配音 + 字幕 + 背景音乐 → 成片。不依赖任何视频服务，不占 Veo 额度
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_BASE, "qwen3.8-flash"))
    for i, nid in enumerate((2, 12, 13)):
        g.add(nid, "LoadImage", (60, 470 + i * 460), (400, 420), ["demo_product.png", "image"], title=f"① 图片 {i + 1}（最多接 8 张，可用 12/18 出的图）")
    g.add(6, "PrimitiveStringMultiline", (520, 60), (440, 200), ["夏日清凉，新鲜脆甜，限时三天，欢迎选购。"], title="② 配音文案（同时用于配音和字幕）")
    g.add(3, "ProAliTTS", (520, 320), (440, 420), ["（由文案框提供）", "Cherry", "qwen3-tts-flash", "Chinese", "", 1.0, 0.0, ""], title="配音")
    g.add(7, "ProSubtitles", (520, 800), (440, 260), ["（由文案框提供）", 12, 0.0, "subtitles/字幕"], title="字幕（按配音停顿对齐，同时存 .srt）")
    g.add(8, "ProSlideshow", (1020, 60), (420, 460), ["9:16", 720, 2.5, 0.5, 1.15, 24, "video/轮播"], title="③ 图片轮播（时长自动跟配音走）")
    g.add(4, "LoadAudio", (520, 1120), (440, 200), ["无背景音乐.wav", "", ""], title="④ 背景音乐（默认静音=不加；想加就上传自己的）")
    g.add(5, "ProVideoDub", (1020, 600), (420, 480), [-14, 0, 1.5, False, "video/轮播成片", "烧进画面", 46, 70], title="⑤ 合成成片")
    g.connect(31, "STRING", 3, "info"); g.connect(6, "STRING", 3, "text"); g.connect(6, "STRING", 7, "text"); g.connect(3, "audio", 7, "voice")
    for k, nid in enumerate((2, 12, 13), 1):
        g.connect(nid, "IMAGE", 8, f"image{k}")
    g.connect(3, "audio", 8, "fit_audio")
    g.connect(8, "video", 5, "video"); g.connect(3, "audio", 5, "voice"); g.connect(4, "AUDIO", 5, "bgm"); g.connect(7, "srt", 5, "subtitles")
    status(g, 3, pos=(1020, 1120), title="状态：配音")
    return g.build()


write("23-图片轮播短视频.json", slideshow())


def labels():         # 24：给图片叠加价格 / 活动语标签（真字体渲染，文字一字不差；AI 海报里的价格文字偶尔会错）
    g = Graph()
    g.add(2, "LoadImage", (60, 120), (400, 420), ["demo_product.png", "image"], title="① 上传主图（可接 12/18/14 出的图）")
    g.add(3, "ProLabels", (520, 120), (460, 700),
          ["圆角矩形", 3.0, "限时三天", "左上", "红底白字", 5.0, "满199减50", "右下", "黄底黑字", 6.0, "新品首发", "左下", "黑底金字", 4.5],
          title="② 促销标签（3 个，文字留空=不加；样式/位置/大小按图片宽度的百分比）")
    g.add(4, "SaveImage", (1040, 120), (340, 320), ["labels/促销图"], title="保存")
    g.connect(2, "IMAGE", 3, "image"); g.connect(3, "image", 4, "images")
    return g.build()


write("24-促销标签叠加.json", labels())


def voice_design():   # 25：文字描述设计新音色 → 试听 + 用它配音；音色 id 存在阿里账号里，可粘进 16/19/20 配音节点的「自定义音色」
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"))
    g.add(2, "ProAliVoiceDesign", (520, 120), (460, 420),
          [AI_NOTE, "欢迎选购我们的新品，限时三天，满一百九十九减五十。", "myvoice", "zh"], title="① 描述你要的声音（试听文字用来生成试听音频）")
    g.add(3, "ProAliTTS", (1040, 120), (440, 460), ["这是用新设计的音色配的音，夏日清凉节，欢迎选购。", "Cherry", "qwen3-tts-flash", "Chinese", "", 1.0, 0.0, ""], title="② 用新音色配音（自定义音色已连上，上面的音色/模型被忽略）")
    g.add(4, "SaveAudioAdvanced", (520, 600), (340, 200), ["audio/音色试听", "mp3", "V0"], title="保存试听")
    g.add(5, "SaveAudioAdvanced", (1540, 120), (340, 200), ["audio/自定义音色配音", "mp3", "V0"], title="保存配音")
    g.add(6, "PreviewAny", (900, 700), (400, 120), title="音色 id（复制到其他工作流配音节点的「自定义音色」框里可反复使用）")
    g.connect(31, "STRING", 2, "info"); g.connect(31, "STRING", 3, "info")
    g.connect(2, "voice", 3, "custom_voice"); g.connect(2, "voice", 6, "source")
    g.connect(2, "preview", 4, "audio"); g.connect(3, "audio", 5, "audio")
    ai_prompt(g, 2, "voice", "沉稳的中年男性，语速适中，声音温暖有磁性，适合产品介绍", target="voice_prompt")
    return g.build()


write("25-音色设计.json", voice_design())


def voice_clone():    # 26：上传一段样音克隆声音 → 用它配音（只克隆本人或已获授权的声音）
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_BASE, "qwen3.8-flash"))
    g.add(4, "LoadAudio", (60, 470), (400, 300), ["无背景音乐.wav", "", ""], title="① 上传样音（10~60 秒清晰人声，只用本人或已获授权的声音；点上传按钮换掉默认的静音）")
    g.add(2, "ProAliVoiceClone", (520, 120), (420, 200), ["myclone"], title="② 克隆音色")
    g.add(3, "ProAliTTS", (1000, 120), (440, 460), ["这是用克隆出来的音色配的音，夏日清凉节，欢迎选购。", "Cherry", "qwen3-tts-flash", "Chinese", "", 1.0, 0.0, ""], title="③ 用克隆音色配音")
    g.add(5, "SaveAudioAdvanced", (1500, 120), (340, 200), ["audio/克隆配音", "mp3", "V0"], title="保存配音")
    g.add(6, "PreviewAny", (520, 400), (400, 120), title="音色 id（复制到其他工作流配音节点的「自定义音色」框里可反复使用）")
    g.connect(31, "STRING", 2, "info"); g.connect(31, "STRING", 3, "info"); g.connect(4, "AUDIO", 2, "audio")
    g.connect(2, "voice", 3, "custom_voice"); g.connect(2, "voice", 6, "source"); g.connect(3, "audio", 5, "audio")
    return g.build()


write("26-声音克隆.json", voice_clone())
