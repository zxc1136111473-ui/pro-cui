#!/usr/bin/env python3
# 生成内置工作流模板（workflows/*.json 的来源，改模板改这里再运行：python3 tools/gen_workflows.py）。
# 模板里不带 key：key 存在服务器 relayapi 的 relay_config.json 的 node_settings[节点 id] 里。
#
# 11 个工作流按「任务」归并（同一任务的不同引擎 / 模式放进一个工作流）：
#   画布最上面是「★ 先选模式」（pro-flow 的模式节点，中文下拉）和「★ 只填这里：一句话需求」（pro-ali 的写提示词节点）；
#   模式节点输出「走第几路」和各种开关，「选择分支」「开关门」按它只执行选中的那一路（没选中的引擎不调接口、不花钱）。
#   输出节点（保存 / 预览）不能直接接在某一路的节点上，否则 ComfyUI 会为了它强制执行那一路——要经过选择分支再接。
#   relayapi 的生图 / 出片 / 出歌节点失败时，媒体输出是 ExecutionBlocker（下游被静默跳过），错误只在 response 输出里；
#   所以「媒体」和「状态」要分成两个选择分支，状态那个只接字符串，失败时界面才看得到错误（tests 里有检查）。
import glob, importlib.util, json, os

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = os.path.join(ROOT, "workflows")

# pro-flow 只用标准库：模式表（MODES）和扩写指令（TEMPLATES）在那边定义，这里和测试都读它，不各写一份
_spec = importlib.util.spec_from_file_location("pro_flow", os.path.join(ROOT, "custom-nodes", "pro-flow", "__init__.py"))
FLOW = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FLOW)

# 类别文件夹（侧栏里显示成树）；编号 = 文件名前两位
FOLDERS = {"1-图片": ("01", "02", "03", "04", "05", "06"), "2-文案与配音": ("07", "08", "09"), "3-视频": ("10", "11")}

# 重新生成前清掉旧的，不留已经不再生成的文件
for _f in glob.glob(os.path.join(OUT, "*", "*.json")):
    os.remove(_f)
for _d in glob.glob(os.path.join(OUT, "*")):
    if os.path.isdir(_d) and not os.listdir(_d):
        os.rmdir(_d)
os.makedirs(OUT, exist_ok=True)


def write(name, obj):
    folder = next((d for d, nums in FOLDERS.items() if name[:2] in nums), None)
    assert folder, f"{name} 没有归到任何类别文件夹（改 FOLDERS）"
    p = os.path.join(OUT, folder, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print("写出:", p)


# ════════════════════════════════════════════════════════════════════════════
# 搭图辅助：按名字连线、自动编号，比手写 links 不容易错
# ════════════════════════════════════════════════════════════════════════════
def _imgs(n):
    return [(f"image{i}", "IMAGE") for i in range(1, n + 1)]

# 类型 → (输入槽 [(名字, 类型, 是否 widget 输入)], 输出槽 [(名字, 类型)], 连线时才出现的 widget 输入：名字（STRING）或 (名字, 类型))
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
    "ProPosterPrompt": ([], [("prompt", "STRING")], ["title", "subtitle", "badge", "elements", ("use_product_image", "BOOLEAN")]),
    "ProPosterFields": ([("text", "STRING", False)], [("title", "STRING"), ("subtitle", "STRING"), ("badge", "STRING"), ("elements", "STRING")], []),
    "RelaySoundGenerator": ([("info", "STRING", True)], [("audio", "AUDIO"), ("clip_id", "STRING"), ("task_id", "STRING"), ("response", "STRING"), ("audio_url", "STRING")], ["prompt"]),
    "ProGeminiMusic": ([("info", "STRING", True)], [("audio", "AUDIO"), ("response", "STRING")], ["prompt"]),
    "ProAliImage": ([("info", "STRING", True)], [("image", "IMAGE"), ("response", "STRING")], ["prompt"]),
    "ProAliImageEdit": ([("info", "STRING", True), ("image1", "IMAGE", False), ("image2", "IMAGE", False), ("image3", "IMAGE", False)],
                        [("image", "IMAGE"), ("response", "STRING")], ["prompt"]),
    "ProAliPromptWriter": ([("info", "STRING", True), ("image", "IMAGE", False)], [("text", "STRING"), ("response", "STRING")], ["instruction"]),
    "ProAliTTS": ([("info", "STRING", True)], [("audio", "AUDIO"), ("response", "STRING")], ["text", "custom_voice"]),
    "ProAliVoiceDesign": ([("info", "STRING", True)], [("voice", "STRING"), ("preview", "AUDIO")], ["voice_prompt"]),
    "ProAliVoiceClone": ([("audio", "AUDIO", False), ("info", "STRING", True)], [("voice", "STRING")], []),
    "SaveAudioAdvanced": ([("audio", "AUDIO", False)], [], []),
    "LoadVideo": ([], [("VIDEO", "VIDEO")], []),
    "SaveVideo": ([("video", "VIDEO", False)], [], []),
    "LoadAudio": ([], [("AUDIO", "AUDIO")], []),
    "ProVideoDub": ([("video", "VIDEO", False), ("voice", "AUDIO", False), ("bgm", "AUDIO", False), ("subtitles", "STRING", False)], [("video", "VIDEO")], [("keep_original_audio", "BOOLEAN")]),
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

# pro-flow：模式节点（输出由 MODES 表决定）、选择分支（惰性）、开关门
for _n, (_title, _opts, _outs) in FLOW.MODES.items():
    SPEC[_n] = ([], [(o, t) for o, t, _ in _outs], [])
for _b in (2, 3, 4):
    for _c in (1, 2, 3):
        SPEC[f"ProPick{_b}x{_c}"] = ([(f"in{b}_{c}", "*", False) for b in range(_b) for c in range(_c)],
                                    [(f"out{c}", "*") for c in range(_c)], [("branch", "INT")])
SPEC["ProGate"] = ([("value", "*", False)], [("value", "*")], [("enabled", "BOOLEAN")])


class Graph:
    def __init__(self):
        self.nodes, self.links, self._lid, self._order = [], [], 0, 0
        self.dy = 0   # top_block 把已有节点整体下移后，之后新增节点的 y 也自动加上，所以位置都可以按「下移前」的坐标写

    def add(self, nid, ntype, pos, size, widgets=None, title=None):
        ins, outs, _ = SPEC[ntype]
        n = {"id": nid, "type": ntype, "pos": [pos[0], pos[1] + self.dy], "size": list(size), "flags": {}, "order": self._order, "mode": 0,
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
        if inp is None:  # 只在连线时才出现的 widget 输入（如 prompt）；SPEC 里写 "名字"（STRING）或 ("名字", 类型)
            wtypes = {(e if isinstance(e, str) else e[0]): ("STRING" if isinstance(e, str) else e[1]) for e in SPEC[d["type"]][2]}
            assert in_name in wtypes, (d["type"], in_name)
            inp = {"name": in_name, "type": wtypes[in_name], "link": None, "widget": {"name": in_name}}
            d["inputs"].append(inp)
        assert inp["link"] is None, f"{dst}.{in_name} 已连过线"
        self._lid += 1
        inp["link"] = self._lid
        s["outputs"][slot]["links"] = (s["outputs"][slot]["links"] or []) + [self._lid]
        self.links.append([self._lid, src, slot, dst, next(i for i, x in enumerate(d["inputs"]) if x is inp), s["outputs"][slot]["type"]])

    def build(self):
        return {"last_node_id": max(n["id"] for n in self.nodes), "last_link_id": self._lid, "nodes": self.nodes,
                "links": self.links, "groups": [], "config": {}, "extra": {}, "version": 0.4}


# 工作流里 Key 按节点 id 存（服务器 relay_config.json 的 node_settings[节点 id]）：1 网关、11 geminiweb、21 Suno、31 阿里
ALI_TXT_BASE = "https://dashscope.aliyuncs.com/compatible-mode"   # pro-ali 节点只取 host，文字节点要带 /compatible-mode，一个 Key 通用
ali_settings = lambda base, model: ["text", "OpenaiText", "v1/chat/completions", "https://www.runninghub.cn", "claude-opus-4-6", "", base, model]
IMG_SET = ["image", "banana-2", "v1/chat/completions", "https://www.runninghub.cn", "grok-video-3", "", "http://airelay-newapi:3000", "gemini-image"]
VEO_SET = ["video", "Veo", "v1/videos", "https://www.runninghub.cn", "veo3.1", "", "http://airelay-geminiweb:8083", "gemini-video"]
SUNO_SET = ["sound", "Suno", "suno/submit", "https://www.runninghub.cn", "suno_music", "", "http://airelay-newapi:3000", "suno_music"]
WHITE_BG = "请编辑这张商品照片：把原来的背景完全去掉，换成干净、无缝的纯白色（#FFFFFF）影棚背景；商品本身保持原样（形状、细节、颜色、角度都不要改），在商品下方加柔和自然的接触阴影；专业电商产品图。"
AI_NOTE = "（由 AI 扩写节点提供）"
DEMO = ["demo_product.png", "image"]
DUB_WIDGETS = [-14, 0, 1.5, False, "video/成片", "烧进画面", 46, 70]   # 背景音乐 / 配音音量、淡出、保留原声、文件名、字幕模式、字号、底边距
TTS_DEFAULT = ["（由文案框提供）", "Cherry", "qwen3-tts-flash", "Chinese", "", 1.0, 0.0, ""]   # text, voice, model, language, instructions, speed, volume_db, custom_voice


def status(g, gen, out="response", pos=(0, 0), title="状态（出错时这里显示原因）", nid=None):
    nid = nid or max(n["id"] for n in g.nodes) + 1  # 节点编号不是连续分配的模板要显式传 nid，避免撞号
    g.add(nid, "PreviewAny", pos, (360, 110), title=title)
    g.connect(gen, out, nid, "source")
    return nid


# ── 画布最上面：「★ 先选模式」+「★ 只填这里：一句话需求」，其余节点整体下移 ───────────────────────────────────
# 节点编号固定：70 写提示词、74 它的预览、75 海报文案拆分、77 模式。阿里 Settings 固定 31（Key 按节点 id 存）。
IDEA_TITLE = "★ 只填这里：一句话需求"


def top_block(g, mode=None, writer=None, settings_id=31):
    """writer: None 或 dict(kind=EXPAND 里的指令名 / None=指令由模式节点输出, idea=默认需求, idea_title, model, preview(默认 True), preview_title, fields=True 表示海报)。
    返回 None；调用方再自己连 70.text → 生成节点的 prompt 等。"""
    have = [n for n in g.nodes if n["id"] == settings_id]
    assert not have or have[0]["type"] == "RelayAPISettings", f"节点 {settings_id} 要留给阿里 Settings（Key 按节点 id 存），换个编号"
    wy = 240 if mode else 60                       # 写提示词节点的 y
    bottom = (wy + 560 + 30) if writer else (60 + 110 + 30)
    need_settings = bool(writer) and not have
    if need_settings:
        bottom += 40 + 300 + 30
    shift = bottom + 60
    for n in g.nodes:
        n["pos"][1] += shift
    if mode:
        g.add(77, mode, (60, 60), (520, 110), [FLOW.MODES[mode][1][0]], title="★ 先选模式")
    if writer:
        w = dict(writer)
        if need_settings:
            g.add(settings_id, "RelayAPISettings", (60, wy + 560 + 70), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"), title="Relay API Settings（阿里文字）")
        instruction = FLOW.TEMPLATES[w["kind"]] if w.get("kind") else "（由模式节点提供）"
        # idea, instruction, model, seed, control_after_generate
        g.add(70, "ProAliPromptWriter", (60, wy), (520, 560), [w["idea"], instruction, w.get("model", "qwen3.8-flash"), 1, "randomize"],
              title=w.get("idea_title", IDEA_TITLE))
        g.connect(settings_id, "STRING", 70, "info")
        if not w.get("kind"):
            g.connect(77, "instruction", 70, "instruction")
        if w.get("preview", True):
            g.add(74, "PreviewAny", (620, wy), (460, 300), title=w.get("preview_title", "AI 扩写出的结果（想看模型写了什么）"))
            g.connect(70, "text", 74, "source")
        if w.get("fields"):                          # 海报：AI 按固定格式写文案 → 拆成 4 个字段
            g.add(75, "ProPosterFields", (1120, wy), (300, 140), title="拆成标题 / 副标题 / 角标 / 画面元素")
            g.connect(70, "text", 75, "text")
    g.dy = shift                                     # 之后新增的节点按「下移前」的坐标写，这里自动补上


def pick(g, nid, branches, channels, pos, branch_from, outs, title="按模式取结果（没选中的那一路不会运行）"):
    """选择分支：outs[b][c] = (源节点 id, 源输出名)；branch_from = (模式节点 id, 输出名)。"""
    g.add(nid, f"ProPick{branches}x{channels}", pos, (300, 120 + 30 * channels), [0], title=title)
    g.connect(branch_from[0], branch_from[1], nid, "branch")
    for b in range(branches):
        for c in range(channels):
            g.connect(outs[b][c][0], outs[b][c][1], nid, f"in{b}_{c}")


def gate(g, nid, pos, enabled_from, value_from, title="开关门（不用时输出空，上游也不会运行）"):
    g.add(nid, "ProGate", pos, (260, 100), [True], title=title)
    g.connect(enabled_from[0], enabled_from[1], nid, "enabled")
    g.connect(value_from[0], value_from[1], nid, "value")


# ════════════════════════════════════════════════════════════════════════════
# 01~06 图片
# ════════════════════════════════════════════════════════════════════════════
def wf_text_to_image():       # 01 文生图（原 01 / 03 / 14）：改比例就是封面横图
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 120), (400, 300), IMG_SET, title="Relay API Settings（网关 Key）")
    g.add(2, "RelayImageGenerator", (520, 120), (380, 420), [AI_NOTE, "1:1", "1K", "medium", "jpeg", "low", 42, "randomize"], title="引擎 ① 网关 Gemini（比例在这里选，16:9 就是封面横图）")
    g.add(3, "ProAliImage", (960, 120), (440, 420), [AI_NOTE, "qwen-image-2.0-pro", "1:1", "2K", 1, "randomize"], title="引擎 ② 阿里 qwen-image（比例、清晰度在这里选）")
    g.add(4, "SaveImage", (1820, 120), (340, 320), ["txt2img"])
    top_block(g, mode="ProModeImage", writer=dict(kind="image", idea="一只橘猫坐在窗台上，阳光，写实风格"))
    g.connect(1, "STRING", 2, "info"); g.connect(31, "STRING", 3, "info")
    g.connect(70, "text", 2, "prompt"); g.connect(70, "text", 3, "prompt")
    pick(g, 10, 2, 1, (1460, 120), (77, "branch"), [[(2, "image")], [(3, "image")]], title="按模式取图（没选中的引擎不会运行）")
    pick(g, 12, 2, 1, (1460, 330), (77, "branch"), [[(2, "response")], [(3, "response")]], title="按模式取状态（单独一个，失败时才看得到错误）")
    g.connect(10, "out0", 4, "images")
    g.add(11, "PreviewAny", (1820, 500), (360, 110), title="结果 / 错误信息（出错时这里显示原因）"); g.connect(12, "out0", 11, "source")
    return g.build()


POSTER_NOTE = "（由「海报提示词」节点生成）"
POSTER_DEFAULTS = ["夏日清凉节", "全场满199减50", "限时三天", "清爽夏日（蓝白）", "冰饮、柠檬片、水花、椰树叶、遮阳草帽"]   # 标题 / 副标题 / 角标 / 风格 / 画面元素：AI 会覆盖，断开连线后才用到


def wf_poster():              # 02 海报（原 10 / 11 / 17）
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 120), (400, 300), IMG_SET, title="Relay API Settings（网关 Key）")
    # title, subtitle, badge, style, elements, use_product_image, extra —— 标题/副标题/角标/画面元素由 AI 填，use_product_image 跟模式走
    g.add(5, "ProPosterPrompt", (60, 470), (400, 430), POSTER_DEFAULTS + [False, ""], title="海报提示词（风格在这里选；文字由 AI 填）")
    g.add(2, "RelayImageGenerator", (520, 120), (380, 420), [POSTER_NOTE, "3:4", "1K", "medium", "jpeg", "low", 42, "randomize"], title="引擎 ① 网关 Gemini")
    g.add(3, "ProAliImage", (960, 120), (440, 420), [POSTER_NOTE, "qwen-image-2.0-pro", "3:4", "2K", 1, "randomize"], title="引擎 ② 阿里 qwen-image 2K（不带商品图）")
    g.add(6, "LoadImage", (520, 780), (380, 400), DEMO, title="商品图（只在「带商品图」模式用）")
    g.add(4, "SaveImage", (1820, 120), (340, 320), ["poster"])
    top_block(g, mode="ProModePoster", writer=dict(kind="poster", idea="夏日清凉节促销，全场满199减50，限时三天，清爽冰饮风格", fields=True))
    for f in ("title", "subtitle", "badge", "elements"):
        g.connect(75, f, 5, f)
    g.connect(77, "use_product", 5, "use_product_image")
    g.connect(1, "STRING", 2, "info"); g.connect(31, "STRING", 3, "info")
    g.connect(5, "prompt", 2, "prompt"); g.connect(5, "prompt", 3, "prompt")
    gate(g, 7, (960, 780), (77, "use_product"), (6, "IMAGE"), title="商品图开关（跟模式走）")
    g.connect(7, "value", 2, "image1")
    pick(g, 10, 2, 1, (1460, 120), (77, "branch"), [[(2, "image")], [(3, "image")]], title="按模式取图（没选中的引擎不会运行）")
    pick(g, 12, 2, 1, (1460, 330), (77, "branch"), [[(2, "response")], [(3, "response")]], title="按模式取状态（单独一个，失败时才看得到错误）")
    g.connect(10, "out0", 4, "images")
    g.add(11, "PreviewAny", (1820, 500), (360, 110), title="结果 / 错误信息（出错时这里显示原因）"); g.connect(12, "out0", 11, "source")
    return g.build()


EDIT_IDEA = "把背景换成干净的纯白色影棚背景，商品保持原样，下方加柔和的接触阴影"


def wf_edit():                # 03 商品改图与合成（原 02 / 15 / 13）
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 120), (400, 300), IMG_SET, title="Relay API Settings（网关 Key）")
    g.add(4, "LoadImage", (60, 470), (400, 420), DEMO, title="① 上传商品图（图1）")
    g.add(5, "LoadImage", (60, 950), (400, 420), DEMO, title="② 场景图（图2，只在「放进场景图」模式用，换成你的场景）")
    g.add(2, "RelayImageGenerator", (520, 120), (380, 420), [AI_NOTE, "1:1", "1K", "medium", "jpeg", "low", 42, "randomize"], title="引擎 ① 网关 Gemini")
    g.add(3, "ProAliImageEdit", (960, 120), (440, 460), [AI_NOTE, "qwen-image-edit-max", 1, "randomize"], title="引擎 ② 阿里 qwen-image-edit")
    g.add(8, "SaveImage", (1820, 120), (340, 320), ["edit"])
    top_block(g, mode="ProModeEdit", writer=dict(kind=None, idea=EDIT_IDEA, idea_title="★ 只填这里：一句话需求（改图：怎么改；合成：放在哪、多大）"))
    gate(g, 6, (520, 780), (77, "compose"), (5, "IMAGE"), title="场景图开关（跟模式走）")
    g.connect(1, "STRING", 2, "info"); g.connect(31, "STRING", 3, "info")
    g.connect(70, "text", 2, "prompt"); g.connect(70, "text", 3, "prompt")
    for gen in (2, 3):
        g.connect(4, "IMAGE", gen, "image1"); g.connect(6, "value", gen, "image2")
    pick(g, 10, 2, 1, (1460, 120), (77, "branch"), [[(2, "image")], [(3, "image")]], title="按模式取图（没选中的引擎不会运行）")
    pick(g, 12, 2, 1, (1460, 330), (77, "branch"), [[(2, "response")], [(3, "response")]], title="按模式取状态（单独一个，失败时才看得到错误）")
    g.connect(10, "out0", 8, "images")
    g.add(11, "PreviewAny", (1820, 500), (360, 110), title="结果 / 错误信息（出错时这里显示原因）"); g.connect(12, "out0", 11, "source")
    return g.build()


def wf_sizes():               # 04 多尺寸套图：同一张商品图、同一段提示词，一次出 4 个平台尺寸（ComfyUI 队列按顺序跑，约 1.5 分钟）
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 60), (400, 300), IMG_SET)
    g.add(2, "LoadImage", (60, 420), (400, 420), DEMO, title="① 上传商品图")
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
    top_block(g, writer=dict(kind="promo", idea="干净明亮的浅色背景，商品居中突出，柔和自然的光影和阴影", idea_title="★ 只填这里：一句话需求（4 个尺寸共用）"))
    for gid in (10, 11, 12, 13):
        g.connect(70, "text", gid, "prompt")
    return g.build()


def wf_post_process():        # 05 图片后处理（原 06 / 24）：放大 2 倍（Lanczos + 轻度锐化）和 / 或贴促销标签（真字体渲染，文字一字不差），都不用 AI、不用 Key
    g = Graph()
    g.add(2, "LoadImage", (60, 120), (400, 420), DEMO, title="① 上传图片（可接 01~04 出的图）")
    g.add(3, "ImageScaleBy", (520, 120), (300, 90), ["lanczos", 2.0], title="放大 2 倍")
    g.add(4, "ImageSharpen", (520, 260), (300, 120), [1, 1.0, 0.5], title="轻度锐化")
    # 圆角矩形 + 3 个标签（文字、位置、样式、大小）
    g.add(6, "ProLabels", (1260, 120), (460, 700),
          ["圆角矩形", 3.0, "限时三天", "左上", "红底白字", 5.0, "满199减50", "右下", "黄底黑字", 6.0, "新品首发", "左下", "黑底金字", 4.5],
          title="促销标签（3 个，文字留空=不加；样式/位置/大小按图片宽度的百分比）")
    g.add(8, "SaveImage", (2140, 120), (340, 320), ["post/处理后"])
    top_block(g, mode="ProModeProcess")
    g.connect(2, "IMAGE", 3, "image"); g.connect(3, "IMAGE", 4, "image")
    pick(g, 5, 2, 1, (900, 120), (77, "upscale"), [[(2, "IMAGE")], [(4, "IMAGE")]], title="要不要放大")
    g.connect(5, "out0", 6, "image")
    pick(g, 7, 2, 1, (1780, 120), (77, "labels"), [[(5, "out0")], [(6, "image")]], title="要不要贴标签")
    g.connect(7, "out0", 8, "images")
    return g.build()


def wf_pipeline():            # 06 商品一条龙：一张商品图 → 白底主图(放大 2 倍) + 场景图 + 标题/卖点文案，一次跑完
    g = Graph()
    g.add(1, "RelayAPISettings", (60, 60), (400, 300), IMG_SET)
    g.add(31, "RelayAPISettings", (60, 1240), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-omni-flash"), title="Relay API Settings（阿里文字）")
    g.add(3, "LoadImage", (60, 420), (400, 420), DEMO, title="① 上传商品图")
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
    top_block(g, writer=dict(kind="scene", idea="明亮的现代厨房台面，窗边自然光", idea_title="★ 只填这里：场景一句话（只管③场景图，白底主图和文案不受影响）"))
    g.connect(70, "text", 8, "prompt")
    return g.build()


write("01-文生图.json", wf_text_to_image())
write("02-海报.json", wf_poster())
write("03-商品改图与合成.json", wf_edit())
write("04-多尺寸套图.json", wf_sizes())
write("05-图片后处理.json", wf_post_process())
write("06-商品一条龙.json", wf_pipeline())


# ════════════════════════════════════════════════════════════════════════════
# 07~09 文案 / 配音 / 音乐
# ════════════════════════════════════════════════════════════════════════════
COPY_IDEA = "新鲜红苹果：果形圆润，脆甜多汁，带果梗鲜叶，产地直发"   # 和演示商品图（苹果）一致，「看图写文案」模式才不会和图打架


def wf_copy():                # 07 文案（原 08 / 09 / 21）：写文案 / 看图写文案 / 翻译，都是「写提示词」节点换一条指令
    g = Graph()
    g.add(3, "LoadImage", (60, 470), (400, 420), DEMO, title="商品图（只在「看图写文案」模式用）")
    # 图先缩到最长边 768：大图从服务器传到阿里很慢（实测 1024 的 PNG 超过 180 秒，缩小后约 10 秒）
    g.add(5, "ImageScaleToMaxDimension", (520, 520), (320, 90), ["lanczos", 768], title="缩到最长边 768（别删）")
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-omni-flash"), title="Relay API Settings（阿里文字）")
    top_block(g, mode="ProModeCopy", writer=dict(kind=None, idea=COPY_IDEA, model="qwen3.8-omni-flash", preview_title="生成的文案",
                                                  idea_title="★ 只填这里：商品信息（翻译模式：贴上要翻译的中文文案）"))
    gate(g, 6, (900, 520), (77, "use_image"), (5, "IMAGE"), title="商品图开关（跟模式走）")
    g.connect(3, "IMAGE", 5, "image"); g.connect(6, "value", 70, "image")
    return g.build()


def wf_voice():               # 08 配音（原 16 / 25 / 26）：预置音色 / 设计新音色 / 克隆声音，三种音色来源只有选中的那条会运行（设计 / 克隆会在阿里账号里建音色，不能乱跑）
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"), title="Relay API Settings（阿里文字）")
    g.add(3, "ProAliTTS", (520, 120), (440, 460), ["夏日清凉节，全场满一百九十九减五十，限时三天，欢迎选购。", "Cherry", "qwen3-tts-flash", "Chinese", "", 1.0, 0.0, ""],
          title="配音（要念的文字在这里写；instruct 模型可写语气指令）")
    g.add(4, "SaveAudioAdvanced", (1020, 120), (340, 200), ["audio/配音", "mp3", "V0"], title="保存配音")
    g.add(2, "ProAliVoiceDesign", (520, 700), (460, 420), [AI_NOTE, "欢迎选购我们的新品，限时三天，满一百九十九减五十。", "myvoice", "zh"],
          title="设计新音色（只在该模式用；试听文字用来生成试听音频）")
    g.add(6, "LoadAudio", (60, 470), (400, 300), ["无背景音乐.wav", "", ""], title="上传样音（克隆模式用，10~60 秒人声）")
    g.add(7, "ProAliVoiceClone", (1040, 700), (420, 200), ["myclone"], title="克隆音色（只克隆本人或已获授权的声音）")
    g.add(8, "PrimitiveStringMultiline", (520, 1180), (300, 100), [""], title="（空：预置音色时用它占位）")
    top_block(g, mode="ProModeVoice", writer=dict(kind="voice", idea="沉稳的中年男性，语速适中，声音温暖有磁性，适合产品介绍", preview=False,
                                                  idea_title="★ 只填这里：一句话描述想要的声音（只在「设计新音色」模式用）"))
    g.connect(31, "STRING", 3, "info"); g.connect(31, "STRING", 2, "info"); g.connect(31, "STRING", 7, "info")
    g.connect(70, "text", 2, "voice_prompt"); g.connect(6, "AUDIO", 7, "audio")
    # 音色 id（和 AI 写的音色描述）都经过选择分支：直接接预览节点会强制执行设计 / 克隆
    pick(g, 10, 3, 2, (1520, 120), (77, "branch"),
         [[(8, "STRING"), (8, "STRING")], [(2, "voice"), (70, "text")], [(7, "voice"), (8, "STRING")]])
    g.connect(10, "out0", 3, "custom_voice"); g.connect(3, "audio", 4, "audio")
    g.add(11, "PreviewAny", (1520, 400), (400, 120), title="音色 id（复制到别的配音节点的「自定义音色」框里可反复使用）"); g.connect(10, "out0", 11, "source")
    g.add(12, "PreviewAny", (1520, 580), (460, 200), title="AI 写的音色描述（只有「设计新音色」模式有）"); g.connect(10, "out1", 12, "source")
    return g.build()


def wf_music():               # 09 音乐（原 05 / 07）：Gemini Lyria / Suno
    g = Graph()
    g.add(11, "RelayAPISettings", (60, 120), (400, 300), VEO_SET, title="Relay API Settings（geminiweb Key，Gemini 音乐用）")
    g.add(21, "RelayAPISettings", (60, 470), (400, 300), SUNO_SET, title="Relay API Settings（Suno Key）")
    g.add(12, "ProGeminiMusic", (520, 120), (400, 300), [AI_NOTE, "gemini-music", 1, "randomize"], title="引擎 ① Gemini Lyria（经 geminiweb）")
    # generation_mode, title, tags, prompt, make_instrumental, version, seed, control_after_generate, negative_tags, extend_mode, continue_clip_id, continue_at
    g.add(22, "RelaySoundGenerator", (520, 480), (400, 420), ["描述模式", "", "pop, electronic", AI_NOTE, True, "V4.5", 1, "randomize", "", False, "", 0], title="引擎 ② Suno（经网关的 Suno 渠道）")
    g.add(5, "SaveAudioAdvanced", (1380, 120), (340, 200), ["audio/音乐", "mp3", "V0"])
    top_block(g, mode="ProModeMusic", writer=dict(kind="music", idea="舒缓钢琴加弦乐纯音乐，温暖治愈，适合产品视频配乐"))
    g.connect(11, "STRING", 12, "info"); g.connect(21, "STRING", 22, "info")
    g.connect(70, "text", 12, "prompt"); g.connect(70, "text", 22, "prompt")
    pick(g, 10, 2, 1, (1000, 120), (77, "branch"), [[(12, "audio")], [(22, "audio")]], title="按模式取音频（没选中的引擎不会运行）")
    pick(g, 13, 2, 1, (1000, 330), (77, "branch"), [[(12, "response")], [(22, "response")]], title="按模式取状态（单独一个，失败时才看得到错误）")
    g.connect(10, "out0", 5, "audio")
    g.add(6, "PreviewAny", (1380, 380), (360, 110), title="结果 / 错误信息（出错时这里显示原因）"); g.connect(13, "out0", 6, "source")
    return g.build()


# ════════════════════════════════════════════════════════════════════════════
# 10~11 视频
# ════════════════════════════════════════════════════════════════════════════
def wf_text_to_video():       # 10 文生视频成片（原 04 / 20）：Veo 出片；可选再配音 + 烧字幕 + 背景音乐（Veo 每天约 3 个额度）
    g = Graph()
    g.add(11, "RelayAPISettings", (60, 60), (400, 300), VEO_SET, title="Relay API Settings（geminiweb 视频 Key）")
    g.add(12, "RelayVideoGenerator", (520, 60), (420, 420), [AI_NOTE, "16:9", "720P", "8", 1, "fixed", "false", "false"], title="① 文生视频（Veo）")
    g.add(6, "PrimitiveStringMultiline", (520, 640), (440, 200), ["快来看，这只橘猫在草地上撒欢奔跑，太可爱啦！"], title="② 配音文案（只在「出视频，再配音」模式用；同时用于配音和字幕）")
    g.add(3, "ProAliTTS", (520, 900), (440, 460), TTS_DEFAULT, title="配音")
    g.add(7, "ProSubtitles", (520, 1420), (440, 260), ["（由文案框提供）", 16, 0.0, "subtitles/字幕"], title="字幕（按配音停顿对齐，同时存 .srt）")
    g.add(4, "LoadAudio", (60, 420), (440, 200), ["无背景音乐.wav", "", ""], title="③ 背景音乐（配音模式用，默认静音=不加）")
    g.add(5, "ProVideoDub", (1100, 60), (420, 480), DUB_WIDGETS, title="④ 合成成片（保留原声 / 替换成配音，跟模式走）")
    top_block(g, mode="ProModeTextVideo", writer=dict(kind="video", idea="一只橘猫在绿色草地上奔跑，写实风格，白天，镜头跟拍"))
    g.connect(11, "STRING", 12, "info"); g.connect(70, "text", 12, "prompt")
    g.connect(31, "STRING", 3, "info"); g.connect(6, "STRING", 3, "text"); g.connect(6, "STRING", 7, "text"); g.connect(3, "audio", 7, "voice")
    g.connect(12, "video", 5, "video"); g.connect(77, "keep_original", 5, "keep_original_audio")
    gate(g, 21, (1100, 640), (77, "dub"), (3, "audio"), title="配音开关（跟模式走）"); g.connect(21, "value", 5, "voice")
    gate(g, 22, (1100, 780), (77, "dub"), (4, "AUDIO"), title="背景音乐开关（跟模式走）"); g.connect(22, "value", 5, "bgm")
    gate(g, 23, (1100, 920), (77, "dub"), (7, "srt"), title="字幕开关（跟模式走）"); g.connect(23, "value", 5, "subtitles")
    status(g, 12, pos=(1100, 1080), title="状态：视频（Veo）")
    return g.build()


def wf_compose():             # 11 视频成片（原 19 / 22 / 23）：上传视频 + 文案配音 / 上传视频 + 听写字幕 / 图片轮播 + 文案配音
    g = Graph()
    g.add(31, "RelayAPISettings", (60, 120), (400, 300), ali_settings(ALI_TXT_BASE, "qwen3.8-flash"), title="Relay API Settings（阿里：配音 + 听写）")
    g.add(2, "LoadVideo", (60, 470), (400, 400), ["请上传视频.mp4", "image"], title="① 上传视频（模式 1、2 用；点节点上的上传按钮）")
    g.add(4, "LoadAudio", (60, 930), (440, 200), ["无背景音乐.wav", "", ""], title="④ 背景音乐（模式 1、3 用，默认静音=不加）")
    g.add(6, "PrimitiveStringMultiline", (560, 120), (440, 200), ["夏日清凉，新鲜脆甜，限时三天，欢迎选购。"], title="② 配音文案（模式 1、3 用；同时用于配音和字幕）")
    g.add(3, "ProAliTTS", (560, 380), (440, 460), TTS_DEFAULT, title="配音")
    g.add(7, "ProSubtitles", (560, 900), (440, 260), ["（由文案框提供）", 16, 0.0, "subtitles/字幕"], title="字幕 · 文案版（按配音停顿对齐，同时存 .srt）")
    g.add(9, "ProVideoAudio", (1060, 120), (300, 90), title="取音轨（模式 2：不解码画面）")
    g.add(10, "ProAliASR", (1060, 270), (400, 160), ["auto"], title="阿里听写（模式 2）")
    g.add(15, "ProSubtitles", (1060, 480), (440, 260), ["（由听写结果提供）", 16, 0.0, "subtitles/字幕"], title="字幕 · 听写版（同时存 .srt）")
    for i, nid in enumerate((12, 13, 14)):
        g.add(nid, "LoadImage", (1560, 120 + i * 480), (400, 420), DEMO, title=f"③ 图片 {i + 1}（模式 3 用，最多接 8 张，可用 01~04 出的图）")
    g.add(8, "ProSlideshow", (2020, 120), (420, 460), ["9:16", 720, 2.5, 0.5, 1.15, 24, "video/轮播"], title="图片轮播（模式 3；时长自动跟配音走）")
    g.add(5, "ProVideoDub", (2020, 700), (420, 480), DUB_WIDGETS, title="⑤ 合成成片（保留原声 / 替换成配音，跟模式走）")
    top_block(g, mode="ProModeCompose")
    g.connect(31, "STRING", 3, "info"); g.connect(31, "STRING", 10, "info")
    g.connect(6, "STRING", 3, "text"); g.connect(6, "STRING", 7, "text"); g.connect(3, "audio", 7, "voice")
    g.connect(2, "VIDEO", 9, "video"); g.connect(9, "audio", 10, "audio"); g.connect(10, "text", 15, "text"); g.connect(9, "audio", 15, "voice")
    for k, nid in enumerate((12, 13, 14), 1):
        g.connect(nid, "IMAGE", 8, f"image{k}")
    g.connect(3, "audio", 8, "fit_audio")
    # 视频来源 / 字幕来源选一条；输出节点只有 ProVideoDub 一个，其余都经过选择分支 / 开关门
    pick(g, 16, 2, 1, (1060, 800), (77, "source"), [[(2, "VIDEO")], [(8, "video")]], title="视频来源（上传 / 图片轮播）")
    pick(g, 17, 2, 2, (1060, 980), (77, "subs"), [[(7, "srt"), (6, "STRING")], [(15, "srt"), (10, "text")]], title="字幕来源（文案 / 听写）")
    gate(g, 18, (1060, 1200), (77, "use_voice"), (3, "audio"), title="配音开关（跟模式走）")
    gate(g, 19, (1060, 1340), (77, "use_bgm"), (4, "AUDIO"), title="背景音乐开关（跟模式走）")
    g.connect(16, "out0", 5, "video"); g.connect(17, "out0", 5, "subtitles"); g.connect(18, "value", 5, "voice"); g.connect(19, "value", 5, "bgm")
    g.connect(77, "keep_original", 5, "keep_original_audio")
    g.add(20, "PreviewAny", (1560, 1580), (440, 200), title="字幕文字（文案 / 听写结果）"); g.connect(17, "out1", 20, "source")
    return g.build()


write("07-文案.json", wf_copy())
write("08-配音.json", wf_voice())
write("09-音乐.json", wf_music())
write("10-文生视频成片.json", wf_text_to_video())
write("11-视频成片.json", wf_compose())
