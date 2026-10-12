"""AI 助手的对话逻辑：拼提示词、调阿里文字模型、解析并校验模型给出的方案。

模型只负责「听懂需求 → 排步骤 → 每步选应用 / 模式 → 写字段 → 说明每步用哪些素材」，输出一个 JSON；真正能不能用由 catalog.clean_selection 校验，
模型编的应用 / 模式 / 字段 / 素材 / 步骤引用一律丢掉，所以不会让它改到目录之外的东西。requests 只在真正调阿里时才 import。

方案 = steps 列表（按运行顺序，每步一个应用）。文件字段的来源有两种：素材库编号（用户上传的、或之前跑出来存进素材库的）→ {"asset": 3}；
前面某一步的产出 → {"step": 1, "n": 1}（第 1 步产出的第 1 个同类型文件）。运行时由前端把它们换成真实文件名再交给 /pro/run。
"""
import json
import math
import re

from . import catalog as cat_mod
from . import presets

CHAT_API = "/compatible-mode/v1/chat/completions"
GATEWAY_CHAT_API = "/v1/chat/completions"   # 网关（New API）的 OpenAI 兼容地址（没有阿里的 /compatible-mode）：谷歌的 gemini 走这里
TEXT_MODEL = "qwen3.7-plus"              # 「自动」时纯文字这一轮用它：改方案（「标题加上…」「改成阿里引擎」）flash 档常改错 / 丢信息，plus 档稳
VISION_MODEL = "qwen3.8-omni-flash"      # 「自动」时有图片的这一轮用它（和「阿里 写提示词」的看图模式同一个模型）
LLM_TIMEOUT = (6, 60)                    # (连接, 等回复) 秒：德国 → 阿里大陆的线路偶尔握手卡 10~25 秒后被重置，连接阶段短超时、快速重连比干等强
GATEWAY_LLM_TIMEOUT = (6, 120)           # 网关 → 谷歌网页版比阿里慢（真实的助手提示词约 2.4 万字符，实测 flash-lite 5~15 秒、flash-thinking 10~30 秒、3.1-pro 30~80 秒，带图片的一轮最长约 90 秒）：等回复放宽到 120 秒；网关在内网，连接不会卡握手，也不用并发补发
LLM_ATTEMPTS = 3                          # 连接失败时最多连这么多次
HEDGE_AFTER = 8                           # 秒：请求这么久没回，就并发再发一个（线路卡住时换条新连接多半几秒就回），谁先回用谁；最多同时 3 个
MAX_PARALLEL = 3
MAX_HISTORY = 14                          # 只带最近这几条对话
MAX_CONTENT = 4000
MAX_ASSETS = 200                          # 素材库最多这么多个（编号和前端一致）。每次重跑都会产出新文件，所以比一次对话用得到的多得多
ASSETS_SHOWN = 40                         # 给模型看的素材清单只列最近的这么多个（编号照旧）；更早的省略，模型一般用不到
MAX_STEPS = 6

SYSTEM = """你是「电商素材助手」，帮用户在 ComfyUI 里生成图片、视频、音频和文案。下面是系统里能用的「应用」。你的任务：听懂用户想做什么 → 排出一步或几步（每一步 = 一个应用，后一步可以用前一步做出来的图 / 视频 / 音频）→ 选好每一步的模式、写好要填的字段、说明用哪些素材 → 给出方案，用户在工作台里确认 / 修改后才会运行。

【应用目录】
{catalog}

【常用组合】（用户要的和下面某个差不多时，直接按它排，可以按需增减步骤；带视频的组合只在用户明确要视频时才用，视频额度很少；「步骤N.M」= 第 N 步产出的第 M 个文件）
{combos}

【素材库】（用户上传的文件，和前面步骤做出来的结果；编号从 1 开始，可以填进「文件字段」）
{assets}

【当前方案】（用户正在看的、可能已经手动改过；用户说「再…一点 / 换成…」时，在它的基础上改；steps 里的 step 是步骤序号）
{plan}

【怎么回答】
1. 想做什么大致清楚 → 马上给出完整方案（plan 不能是 null）：reply 里用 1~3 句话说明分了几步、每步用哪个应用、什么模式、写了什么要点；plan.steps 按运行顺序写每一步。用户只给了商品名 / 主题就够了（比如只说「写红苹果的淘宝标题」，商品名就是全部信息：直接选文案应用，需求写「红苹果」），细节让后面的 AI 去补，用户可以在方案卡片里改，所以不要为了细节追问。用户没提到的设置不要写，保持默认；目录里的「高级设置」只有用户明确要求时才改。
2. 一步能做完的就只写一步，别凑步骤；只有需要把前一步的结果交给后一步时才分步（例如：出图 → 用这张图出视频 → 给视频配音 / 做成片）。最多 6 步；用户没要的步骤不要加（尤其是视频：额度很少）。分步时别重复：配音、字幕、背景音乐整个方案只做一处——比如后面有「视频成片」负责配音，前面出视频的那一步就选「只出视频」的模式。
3. 后一步要用前一步的结果：在 files 里把对应的文件字段填成「步骤N」（N 是排在前面的某一步的序号，取它产出的第 1 个该类型的文件）或「步骤N.M」（取第 M 个，比如「步骤1.2」是第 1 步出的第 2 张图）；要用素材库里已有的文件（用户上传的图片、之前做出来的某一张）就填素材库编号。只能引用排在前面的步骤，而且那个应用的「结果」里必须真的有该类型的文件。
4. 只有这两种情况才追问（plan 设为 null，只问一个问题，reply 里不要说「已选择」「已准备」）：完全猜不出要做什么（没有图、没提商品、也没说要什么）；某一步需要图片 / 视频 / 音频，但素材库里没有、前面的步骤也做不出来（请用户点输入框旁的「附件」上传）。其余情况都不要追问：先给方案，用户不满意再改。
5. 用户在修改方案 → 输出修改后的完整新方案（所有步骤，不是只写改动的部分）；没被要求改的步骤和字段原样保留（包括用户手动改过的值）。
6. 闲聊、或问「你能做什么」→ 简短回答并举 2~3 个例子，plan 设为 null。
7. 不要编造：workflow 填应用编号（字符串，如 "07"，必填）；mode 填该应用「模式」里的编号（数字）；选项类字段只能填列出的选项（选项后面括号里的说明不属于选项本身，填值只写括号前面的名字，或者完整的选项）；数字字段填数字（在给定范围内）；开关字段填 true / false；字段键、文件字段键必须是目录里给出的；素材只能填素材库里有的编号。目录里没有的需求（做 PPT、剪短 / 截取 / 加速视频、手工精剪镜头之类）直接说做不了，别硬凑：别拿「视频改视频」去凑剪辑，它按秒收费、剪不出你要的。
8. 「一句话需求」这类文字字段：写成 1~2 句具体的中文描述（系统后面还会有 AI 进一步扩写，所以不用写成长提示词）；用户给的数字、价格、文字、日期原样保留，不要改写或编造；没提供的信息不要编。「配音文案 / 要念的文字」按用户给的原文，没给就写一段贴合需求的短文案。
9. 目录里写了「费用 / 注意」的应用（比如视频要占每天约 3 个的额度）：方案里用到时，reply 里提醒一句。
10. 看到图片时：reply 里用一句话说明你看到的商品，后续对话要用。用户说「第二张图」之类，对照素材库里的说明找到对应的编号。
11. 两个人以上的对话 / 带角色的配音：用「多角色配音」（只要声音和字幕）或「对白成片」（配到视频 / 图片上）。对白脚本由你来写，填进「对白脚本」：每行「角色：台词」，口语化、每句 20 字以内、两三个来回就够；角色名填进「角色 N 名字」，要和脚本里的一字不差，最多 4 个角色。给每个角色挑音色时看选项后面写的性别和特点：男角色用男声、女角色用女声，老人 / 小孩 / 方言按人设挑，两个角色别用同一个音色，选项里没有的音色不要编。配到视频上时总字数别超过视频能念完的长度（每秒约 4~5 个字，Veo 视频约 10 秒，对白控制在 40 个字以内）。单人配音（只有一个声音）仍用「配音」或视频应用自带的配音，别用多角色。
12. 想保持商品原样（形状、颜色、包装上的字一个像素都不能变）、只换背景 / 调位置大小 / 加阴影：先「商品抠图」做成透明底，再「图层合成」放到背景上（背景图用用户上传的，或先用「文生图」做一张只有背景、没有商品的图），要标签再接「图片后处理」；抠图和合成是分开的两步，改背景或位置只重做合成那一步。想让 AI 把商品和场景的光影融在一起（可以接受商品被重画一点），用「商品改图与合成」。两种做法差别要在 reply 里说清楚。
13. reply 是给用户看的：用「你」称呼，语气简洁友好；说应用和模式时用名字，不要只说编号；reply 和 plan 要一致（plan 是 null 就不能说已经选好了）。
14. 用户的话含糊、但能猜出最可能的意思时，不要反问，直接按最常见的理解给方案，并在 reply 里用一句话说明你的理解（比如「我先按…做了，不对的话告诉我」）。例子：附了商品图只说「帮我写个提示词」「弄一下」→ 用「文案」应用的「看图写文案」模式（写电商用的图片 / 视频提示词也是它）；附了商品图只说「做点东西」「弄好看点」→ 用「商品一条龙」（主图 + 场景图 + 文案，不带视频）；只说「做个图」→ 用「文生图」；只说「做个视频」且有图 → 用「图生视频成片」；只说「写个文案 / 标题」→ 用「文案」。你上一轮如果已经追问过，这一轮不管信息够不够都必须给方案（缺的信息按最常见的情况定）。
15. 视频类的三个应用别搞混：「视频改视频」= 改视频里的内容（换背景 / 物体 / 风格、删东西，可带 1~2 张参考图）；「视频换人」= 把视频里的人换成用户给的人物照片（整个人换，不是只换脸）；「视频拼接」= 把几段视频接成一条（本地合成，免费）。这三个都必须有视频（用户上传的，或前面步骤做出来的）；前两个按秒计费、比图片贵不少：只在用户明确要时才用，reply 里提醒一句大概花多少钱、要等几分钟。「视频换人」只能用用户本人或已获授权的人像，用户没说清照片是谁时 reply 里提醒一句；「我确认人像是本人或已获授权」要用户自己勾选，plan 里不要写这一项。
16. 只回复一个 JSON 对象，不要任何别的文字、不要 markdown。键名必须一字不差（reply、plan、steps、workflow、mode、fields、files、note），每一步里一定要有 workflow：
{{"reply": "给用户看的话", "plan": null 或 {{"steps": [{{"workflow": "应用编号字符串", "mode": 模式编号（数字）或 null, "fields": {{"字段键": 值}}, "files": {{"文件字段键": 素材库编号（数字）或 "步骤N"}}, "note": "一句话：这一步做什么"}}], "note": "一句话说明整体思路"}}}}"""


class ChatError(Exception):
    """要原样告诉用户的错误（status 是 HTTP 状态码）。"""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# 助手（聊天）能选的模型（界面下拉里的就是这一份，后端也只认它）。provider：ali = 阿里（Key 用节点 31），gateway = 网关（谷歌 gemini，Key 用节点 1）；vision：能不能看图。
# 每个模型（2026-10-11）都用助手真实的系统提示词 + 真实的应用目录在服务器上试过：主图 / 文案 / 海报（要保留 199、50）/ 改方案（加「全场包邮」）/ 闲聊（不该给方案）/ 两步（出图再出视频）/ 看图（能看图的）。
# 能按格式给出可用的方案才放进来：qwen3.7-plus、qwen3.8-max、qwen3.8-omni-flash、deepseek-v4-pro、kimi-k3、gemini-3.5-flash-lite、gemini-3.6-flash-thinking、gemini-3.1-pro 全过；
# qwen3.8-flash 海报那条丢了数字（5/6），放进来但标明。没放的：gemini-3.1-pro-thinking（两分钟不回）、gemini-3.6-flash（十次里卡住了两次）、gemini-canvas（是做网页的）。
CHAT_MODELS = [
    {"id": "qwen3.7-plus", "provider": "ali", "vision": False, "label": "qwen3.7-plus · 阿里（稳，纯文字的默认）"},
    {"id": "qwen3.8-max", "provider": "ali", "vision": False, "label": "qwen3.8-max · 阿里（更强，慢一点）"},
    {"id": "qwen3.8-flash", "provider": "ali", "vision": False, "label": "qwen3.8-flash · 阿里（最快，但常漏掉你说的数字，不推荐）"},
    {"id": "qwen3.8-omni-flash", "provider": "ali", "vision": True, "label": "qwen3.8-omni-flash · 阿里（能看图，有图片时的默认）"},
    {"id": "deepseek-v4-pro", "provider": "ali", "vision": False, "label": "deepseek-v4-pro · 阿里"},
    {"id": "kimi-k3", "provider": "ali", "vision": False, "no_temperature": True, "label": "kimi-k3 · 阿里"},       # 阿里这边它不收 temperature 参数（HTTP 400），所以不带
    {"id": "gemini-3.5-flash-lite", "provider": "gateway", "vision": True, "label": "gemini-3.5-flash-lite · 谷歌（快，约 5~15 秒）"},
    {"id": "gemini-3.6-flash-thinking", "provider": "gateway", "vision": True, "label": "gemini-3.6-flash-thinking · 谷歌（约 10~30 秒）"},
    {"id": "gemini-3.1-pro", "provider": "gateway", "vision": True, "label": "gemini-3.1-pro · 谷歌（最强，约 30~80 秒）"},
]
MODEL_BY_ID = {m["id"]: m for m in CHAT_MODELS}
_CITE = re.compile(r"\s*\[cite:[^\]]*\]")             # 谷歌网页版的回复里夹着「[cite: 1]」这样的引用标记，不是正文，要去掉


def model_list():
    """给界面下拉用的模型清单（id / 显示名 / 提供方 / 能不能看图）。"""
    return [{"id": m["id"], "label": m["label"], "provider": m["provider"], "vision": m["vision"]} for m in CHAT_MODELS]


def resolve_model(requested, has_images):
    """用户选的模型 → (模型条目, 要告诉用户的说明)。没选 / 不认识 → 自动：这一轮有图片用 VISION_MODEL，否则 TEXT_MODEL；
    选的模型看不了图而这一轮带了图片 → 这一轮改用能看图的 VISION_MODEL，并说明（图片不会因此被悄悄丢掉）。"""
    auto = MODEL_BY_ID[VISION_MODEL if has_images else TEXT_MODEL]
    want = MODEL_BY_ID.get(requested) if isinstance(requested, str) else None
    if want is None:
        return auto, ""
    if has_images and not want["vision"]:
        return auto, f"这一轮带了图片，{want['id']} 看不了图，这一轮改用 {auto['id']}"
    return want, ""


STEP_REF = re.compile(r"(?:步骤|step)\s*(\d+)(?:\s*[.．]\s*(\d+))?|第\s*(\d+)\s*步", re.I)


def _int(v):
    """整数（含 3.0、"3"）→ int；其余 None。数字串只认 ASCII 数字（「②」「٣」不算）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.strip()
        return int(v) if v.isascii() and v.isdigit() and len(v) < 6 else None
    if isinstance(v, (int, float)) and math.isfinite(v) and v == int(v):
        return int(v)
    return None


def parse_ref(ref):
    """模型 / 前端给的文件引用 → ("asset", 素材库编号) 或 ("step", 步骤序号, 第几个)；认不出返回 None。
    素材库编号可以是数字、数字字符串、{"asset": 3}；前面步骤的产出写「步骤N」「步骤N.M」「stepN」「第N步」或 {"step": N, "n": M}。"""
    if isinstance(ref, dict):
        if ref.get("step") is not None:
            s, n = _int(ref["step"]), _int(ref.get("n", 1))
            return ("step", s, n) if s and n and s > 0 and n > 0 else None
        n = _int(ref.get("asset"))
        return ("asset", n) if n and n > 0 else None
    n = _int(ref)
    if n is not None:
        return ("asset", n) if n > 0 else None
    m = STEP_REF.fullmatch(ref.strip()) if isinstance(ref, str) else None
    if m:
        s, n = int(m.group(1) or m.group(3)), int(m.group(2) or 1)
        return ("step", s, n) if s > 0 and n > 0 else None
    return None


def ref_text(ref):
    """文件引用 → 模型读的写法：素材库编号（数字）/ "步骤N" / "步骤N.M"。"""
    r = parse_ref(ref)
    if r is None:
        return cat_mod.as_text(ref)
    if r[0] == "asset":
        return r[1]
    return f"步骤{r[1]}" + (f".{r[2]}" if r[2] != 1 else "")


def plan_text(plan, cat):
    """当前方案（前端传来的）→ 给模型看的一行 JSON：模式换回编号、文件引用换成编号 / 「步骤N」。字段类型不对（客户端乱传）的一律当没有。"""
    steps = plan.get("steps") if isinstance(plan, dict) else None
    out = []
    for i, st in enumerate(steps[:MAX_STEPS] if isinstance(steps, list) else [], 1):
        if not isinstance(st, dict) or not st.get("workflow"):
            continue
        app = cat.get(str(st["workflow"]))
        mode = st.get("mode")
        if app and app["mode"] and isinstance(mode, str) and mode in app["mode"]["options"]:
            mode = app["mode"]["options"].index(mode) + 1
        fields = st.get("fields") if isinstance(st.get("fields"), dict) else {}
        files = st.get("files") if isinstance(st.get("files"), dict) else {}
        out.append({"step": i, "workflow": str(st["workflow"]), "mode": mode if isinstance(mode, (str, int)) and not isinstance(mode, bool) else None,
                    "fields": {str(k): v if isinstance(v, (bool, int, float)) else cat_mod.as_text(v) for k, v in fields.items()},
                    "files": {str(k): ref_text(v) for k, v in files.items()}})
    return json.dumps({"steps": out}, ensure_ascii=False) if out else "（还没有）"


def normalize_assets(raw):
    """前端传来的素材库 → [{name, kind, label, usable}]：顺序就是编号（从 1 开始）。名字不合规 / 认不出类型的条目也占一个编号，只是不能用；
    usable 由调用方（检查文件还在不在）再设。"""
    out = []
    for a in (raw if isinstance(raw, list) else [])[:MAX_ASSETS]:
        a = a if isinstance(a, dict) else {}
        name = cat_mod.safe_asset_name(a.get("name")) or ""
        out.append({"name": name, "kind": cat_mod.file_kind(name), "label": cat_mod.as_text(a.get("label")).strip()[:60], "usable": False})
    return out


def assets_text(assets):
    lines = []
    first = max(0, len(assets) - ASSETS_SHOWN)
    if first:
        lines.append(f"（共 {len(assets)} 个，前 {first} 个比较旧，没有列出；编号照旧）")
    for i, a in enumerate(assets[first:], first + 1):
        what = cat_mod.KIND_LABEL.get(a["kind"], "文件")
        lines.append(f"{i}. [{what}] {a['label'] or a['name'].split('/', 1)[-1] or '（无名）'}" + ("" if a["usable"] else "（文件已经不存在，不要用）"))
    return "\n".join(lines) or "（还没有）"


def system_prompt(cat, assets, plan):
    return SYSTEM.format(catalog=cat_mod.catalog_text(cat), combos=presets.combos_text(cat) or "（没有）", assets=assets_text(assets), plan=plan_text(plan, cat))


def build_messages(system, history, vision):
    """system + 最近的对话；最后一条用户消息带上这一轮新附的图片（视觉输入）：vision = [(素材库编号, data URL)]，并告诉模型各是几号素材。"""
    msgs = [{"role": "system", "content": system}]
    hist = [m for m in (history or []) if isinstance(m, dict) and m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()]
    for m in hist[-MAX_HISTORY:]:
        msgs.append({"role": m["role"], "content": str(m["content"]).strip()[:MAX_CONTENT]})
    if vision and msgs[-1]["role"] == "user":
        text = msgs[-1]["content"] + "\n（这条消息附带的图片，依次是素材库的 " + "、".join(f"{n} 号" for n, _ in vision) + "）"
        msgs[-1]["content"] = [{"type": "image_url", "image_url": {"url": u}} for _, u in vision] + [{"type": "text", "text": text}]
    return msgs


def scrub(text, key):
    """转发给浏览器的上游错误文字：去掉 Key 和任何长得像 Key 的串（上游有时会回显掩码后的 Key）。"""
    text = str(text or "")
    if key:
        text = text.replace(key, "***")
    return re.sub(r"sk-[A-Za-z0-9*._\-]+", "sk-***", text)


def call_llm(messages, key, base, model, timeout=None, post=None, hedge_after=HEDGE_AFTER, provider="ali", temperature=0.2):
    """OpenAI 兼容的 chat/completions，要求 JSON 回复，返回模型的文字。provider="ali" 发给阿里（base + /compatible-mode/…），"gateway" 发给网关（base + /v1/…，谷歌的模型）。
    连接失败（卡住 / 被重置）自动重连；等回复超时、HTTP 错误不重发；请求 hedge_after 秒还没回就并发再发一个（最多 MAX_PARALLEL 个），谁先回用谁（网关不并发补发）。"""
    import concurrent.futures as cf
    import requests
    gateway = provider == "gateway"
    who = "网关" if gateway else "阿里"
    path = GATEWAY_CHAT_API if gateway else CHAT_API
    if timeout is None:
        timeout = GATEWAY_LLM_TIMEOUT if gateway else LLM_TIMEOUT
    if gateway:
        hedge_after = None                    # 谷歌本来就慢，并发补发只会白白多发几倍请求；网关在内网，也没有握手卡住的问题
    post = post or requests.post
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}

    def attempt():                            # 一个请求；返回 Response（不管状态码），连不上 / 等回复超时抛 ChatError
        body = {"model": model, "messages": messages, "stream": False, "max_tokens": 1500, "response_format": {"type": "json_object"}}
        if temperature is not None:               # 个别模型（kimi-k3）不收这个参数，传 None 就不带
            body["temperature"] = temperature
        if not gateway:
            body["enable_thinking"] = False       # 阿里的参数（关思考，快）；网关不需要
        for n in range(1, LLM_ATTEMPTS + 1):
            try:
                r = post(base + path, headers=headers, json=body, timeout=timeout)
                if r.status_code == 400 and "response_format" in (r.text or "") and "response_format" in body:
                    body.pop("response_format")           # 个别模型不支持 JSON 模式：去掉再试，靠提示词约束
                    r = post(base + path, headers=headers, json=body, timeout=timeout)
                return r
            except requests.ReadTimeout:
                wait = timeout[1] if isinstance(timeout, tuple) else timeout
                raise ChatError(504, f"{who}这次回得太慢（等了 {wait} 秒没有回应），再发一次试试")
            except requests.RequestException as e:
                if n == LLM_ATTEMPTS:
                    raise ChatError(502, f"连不上{who}（{type(e).__name__}），稍后再试")

    ex = cf.ThreadPoolExecutor(max_workers=MAX_PARALLEL)
    try:
        running, done, sent = {ex.submit(attempt)}, set(), 1
        r, bad, failure = None, None, None
        while r is None and (running or done):
            if not done:
                done, running = cf.wait(running, timeout=hedge_after if sent < MAX_PARALLEL else None, return_when=cf.FIRST_COMPLETED)
                if not done:                  # 等了这么久还没有谁回：再并发发一个
                    running.add(ex.submit(attempt))
                    sent += 1
                    continue
            try:
                got = done.pop().result()
            except ChatError as e:
                failure = failure or e
                continue
            if got.status_code == 200 or not (running or done):
                r = got
            else:                             # 先回来的是 429 / 500：别让它压过还在路上的请求，等等看有没有成功的
                bad = bad or got
        if r is None:
            r = bad
        if r is None:
            raise failure
    finally:
        ex.shutdown(wait=False)               # 慢的请求自己跑完，结果不要了
    try:
        d = r.json()
    except Exception:
        raise ChatError(502, f"{who}返回的不是 JSON（HTTP {r.status_code}）：{scrub((r.text or '')[:150], key)}")
    if r.status_code != 200:
        err = d.get("error", d) if isinstance(d, dict) else {}
        if not isinstance(err, dict):
            err = {"message": err}
        raise ChatError(502, f"{who}返回错误（HTTP {r.status_code} {scrub(err.get('code') or '', key)}）：{scrub(err.get('message', ''), key)[:200]}")
    try:
        return _CITE.sub("", d["choices"][0]["message"]["content"] or "")
    except Exception:
        raise ChatError(502, f"{who}的返回里没有文字：" + scrub(json.dumps(d, ensure_ascii=False)[:200], key))


def parse_reply(text):
    """模型的文字 → (reply, plan 或 None)。容忍 ```json 围栏和前后多余的话。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    d = None
    try:
        d = json.loads(s)
    except ValueError:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
            except ValueError:
                d = None
    if not isinstance(d, dict):
        raise ValueError("模型没有按格式回复")
    plan = d.get("plan")
    return str(d.get("reply") or "").strip(), (plan if isinstance(plan, dict) else None)


APP_KEYS = ("workflow", "workflow_id", "app_id", "app", "id")      # 模型偶尔把「workflow」写成这些


def find_app(cat, raw):
    """方案里的应用编号 → (应用条目 或 None, 模型写的原值)。键名和写法都容错：workflow_id / app_id，7 / "应用 07" → 07。"""
    given = None
    for k in APP_KEYS:
        v = raw.get(k)
        if v in (None, "") or isinstance(v, (bool, dict, list)):
            continue
        s = str(v).strip()
        given = given if given is not None else s
        m = re.search(r"\d+", s)
        for cand in (s, s.zfill(2), m.group(0).zfill(2) if m else None):
            if cand in cat:
                return cat[cand], s
    return None, given


def resolve_ref(ref, kind, assets, kept, steps):
    """文件引用 → 校验后的 {"asset": 编号} / {"step": 序号, "n": 第几个}；不能用返回 None。
    kept：模型写的步骤序号 → 它在最终方案里的序号（被丢掉的步骤不在里面）；steps：已经通过校验的步骤。引用只能指向排在前面、且真的产出该类型文件的步骤。"""
    r = parse_ref(ref)
    if r is None:
        return None
    if r[0] == "asset":
        a = assets[r[1] - 1] if 1 <= r[1] <= len(assets) else None
        return {"asset": r[1]} if a and a["usable"] and a["kind"] == kind else None
    _, s, n = r
    if s not in kept or kind not in steps[kept[s] - 1]["schema"]["produces"]:
        return None
    return {"step": kept[s], "n": n}


def make_plan(raw, cat, assets):
    """模型给的方案 → (校验后的方案, 提示)。方案 = {steps: [...], note}，每一步带 schema，前端直接画卡片；一步都没剩就返回 None。
    也认只有一步的写法（plan 里直接写 workflow）。被丢掉的步骤：后面引用它的文件会被去掉并提示，其余步骤的序号顺延。"""
    if not isinstance(raw, dict):
        return None, []
    items = raw["steps"] if isinstance(raw.get("steps"), list) else [raw]
    steps, kept, warns = [], {}, []
    for pos, item in enumerate(items[:MAX_STEPS], 1):
        tag = f"步骤 {pos}：" if len(items) > 1 else ""
        item = item if isinstance(item, dict) else {}
        app, given = find_app(cat, item)
        if app is None:
            drop = "方案已丢弃" if len(items) == 1 else "这一步已丢弃"
            warns.append(tag + (f"没有编号为「{given}」的应用，{drop}" if given else f"没有写应用编号，{drop}"))
            continue
        sel, w = cat_mod.clean_selection(app, item.get("mode"), item.get("fields"), item.get("files"),
                                         lambda key, ref, kind: resolve_ref(ref, kind, assets, kept, steps))
        warns += [tag + x for x in w]
        warns += [f"{tag}「{f['label']}」必须有一个{cat_mod.KIND_LABEL[f['kind']]}，方案里还没指定（先附上，或让前面的步骤做出来）" for f in cat_mod.required_files(app, sel["mode"]) if f["key"] not in sel["files"]]
        steps.append({"workflow": app["id"], "name": app["name"], "path": app["path"], "mode": sel["mode"], "fields": sel["fields"],
                      "files": sel["files"], "note": str(item.get("note") or "").strip()[:200], "schema": cat_mod.schema(app)})
        kept[pos] = len(steps)
    if len(items) > MAX_STEPS:
        warns.append(f"最多 {MAX_STEPS} 步，后面的已忽略")
    if not steps:
        return None, warns
    return {"steps": steps, "note": str(raw.get("note") or "").strip()[:200]}, warns


USER_ONLY_WIDGETS = {"authorized"}                 # 只能用户自己勾的开关（控件名）：「我确认人像是本人或已获授权」——AI 排方案时不能替用户填


def strip_user_only(plan, prev):
    """方案里「只能用户自己勾」的开关：AI 填的一律去掉；用户在当前方案（prev）里已经勾上的（同一位置、同一个应用）保留，不然 AI 每改一次方案用户的勾就没了。返回提示列表。"""
    warns = []
    old = prev.get("steps") if isinstance(prev, dict) and isinstance(prev.get("steps"), list) else []
    for i, st in enumerate(plan["steps"] if plan else []):
        o = old[i] if i < len(old) and isinstance(old[i], dict) else {}
        had = o.get("fields") if isinstance(o.get("fields"), dict) and str(o.get("workflow")) == st["workflow"] else {}
        for key in [k for k in st["fields"] if k.split(":", 1)[-1] in USER_ONLY_WIDGETS]:
            if had.get(key) is True:
                continue
            if st["fields"].pop(key):
                warns.append(f"「{st['schema']['fields'][key]['label']}」要你自己勾选，我没有替你勾")
    return warns


def missing_required(plan):
    """方案里没给必填文件的地方：[(步骤序号, 控件名, 文件类型)]。"""
    return [(i, f["label"], f["kind"]) for i, st in enumerate(plan["steps"], 1) for f in cat_mod.required_files(st["schema"], st["mode"]) if f["key"] not in st["files"]]


ASK_MARK = re.compile(r"[？?]|还是|请问|告诉我")                                   # 回复里在问问题
CAPABILITY = re.compile(r"你能做什么|能做什么|你会什么|你是谁|有什么功能|有哪些功能|怎么用|介绍一下")      # 问你能干什么：这种问题本来就不用方案
ACTION = re.compile(r"帮我|给我|替我|请你|想要|需要|来一|做|写|生成|出一|弄|配音|画|改|换|抠|合成|拼|拍|剪")     # 用户在提要求
NUDGE = ("用户不会再补充信息了，请不要追问：按最常见的情况直接给出完整方案（plan 不能是 null），缺的信息你自己定，"
         "在 reply 里用一句话说明你的理解（比如「我先按…做了，不对的话告诉我」）。仍然只回复要求的那个 JSON 对象。")


def last_user_text(history):
    for m in reversed(history):
        if isinstance(m, dict) and m.get("role") == "user" and str(m.get("content", "")).strip():
            return str(m["content"])
    return ""


def asked_before(history):
    """用户这一句是不是在回答上一轮的追问：上一条助手消息没给方案、而且在问问题。"""
    for m in reversed(history):
        if isinstance(m, dict) and m.get("role") == "assistant":
            try:
                d = json.loads(str(m.get("content", "")))
            except ValueError:
                return False
            return isinstance(d, dict) and not d.get("plan") and bool(ASK_MARK.search(str(d.get("reply", ""))))
    return False


def nudge_wanted(history, reply, has_images):
    """模型没给方案、反过来在问用户：用户这一轮附了图并且提了要求（图就是商品，不用再问），或者这一句是在回答它上一轮的追问（不能再问第二遍）时，催它直接给方案；
    闲聊 / 问你能做什么不催；没图没商品的含糊要求（只说「做个图」）也不催：那是真的猜不出，问一句是对的。"""
    text = last_user_text(history)
    if not ASK_MARK.search(reply) or CAPABILITY.search(text):
        return False
    return (has_images and bool(ACTION.search(text))) or asked_before(history[:-1])


def chat_turn(payload, cat, key, base, assets, vision, post=None, model=None):
    """一轮对话：payload = {messages, plan}；assets = normalize_assets 的结果（usable 已设好）；vision = [(素材库编号, data URL)]（这一轮新附的图片）；
    model = resolve_model 给的模型条目（没给 = 自动）；key / base 要和它的 provider 对得上（阿里的 Key + 阿里地址，或网关的 Key + 网关地址）。
    返回 {reply, plan, warnings, model}（model = 这一轮实际用的模型 id）。"""
    if not cat:
        raise ChatError(503, "服务器上没有找到任何「应用」（*.app.json）；先部署工作流")
    history = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    if not any(isinstance(m, dict) and m.get("role") == "user" and str(m.get("content", "")).strip() for m in history):
        raise ChatError(400, "没有收到消息")
    msgs = build_messages(system_prompt(cat, assets, payload.get("plan")), history, vision)
    entry = model or MODEL_BY_ID[VISION_MODEL if vision else TEXT_MODEL]
    first = None                              # 第一次的回复（没给方案、在追问）：催了一次还是没方案，就用它
    for attempt in (1, 2):                    # 格式不对 / 方案里没有可用的应用编号 / 该给方案却在追问：把问题告诉模型，让它重写一次
        text = call_llm(msgs, key, base, entry["id"], post=post, provider=entry["provider"], temperature=None if entry.get("no_temperature") else 0.2)
        try:
            reply, raw_plan = parse_reply(text)
        except ValueError:
            if attempt == 2:
                if first:                         # 催的那次格式坏了：不报错，用第一次的回复
                    reply, plan, warns = first[0], None, first[1]
                    break
                raise ChatError(502, "模型两次都没有按格式回复，换个说法再试一次")
            fix = "你的回复不是合法的 JSON。请只回复要求的那个 JSON 对象。"
        else:
            plan, warns = make_plan(raw_plan, cat, assets)
            warns += strip_user_only(plan, payload.get("plan"))
            need = missing_required(plan) if plan else []
            if raw_plan is None and attempt == 1 and nudge_wanted(history, reply, bool(vision)):
                first = (reply, warns)
                fix = NUDGE
            elif raw_plan is None or (plan is not None and not need) or attempt == 2:
                if first and plan is None:        # 催了一次还是没给方案：用第一次的回复（它的追问）
                    reply, warns = first
                break
            elif plan is not None:                   # 有的步骤漏了必填的文件（比如改商品图却没有图）：让它改一次，还不行就带着提示给用户
                what = "、".join(f"步骤 {i} 的「{label}」" for i, label, _ in need)
                fix = (f"你的方案里{what}必须有文件，但你没有给它指定。请重新输出完整的 JSON：素材库里有合适的就填编号；前面某一步能做出来就填「步骤N」；"
                       "都没有的话，改用不需要上传文件的应用（比如 01 文生图 / 02 海报），或者把 plan 设为 null、在 reply 里请用户先点「附件」上传。")
            else:
                fix = "你的 plan 里没有写对 workflow：每一步都必须是【应用目录】里的应用编号（字符串，如 \"07\"）。请重新输出完整的 JSON，plan.steps 里每一步一定要有 workflow。"
        msgs = msgs + [{"role": "assistant", "content": text or ""}, {"role": "user", "content": fix}]
    if not reply:
        reply = "我给你准备了一个方案，看看合不合适：" if plan else "我没太明白，能再说具体一点吗？"
    return {"reply": reply, "plan": plan, "warnings": warns, "model": entry["id"]}
