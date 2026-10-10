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

CHAT_API = "/compatible-mode/v1/chat/completions"
TEXT_MODEL = "qwen3.7-plus"              # 改方案（「标题加上…」「改成阿里引擎」）flash 档常改错 / 丢信息，plus 档稳
VISION_MODEL = "qwen3.8-omni-flash"      # 有图片时用（和「阿里 写提示词」的看图模式同一个模型）
LLM_TIMEOUT = (6, 60)                    # (连接, 等回复) 秒：德国 → 阿里大陆的线路偶尔握手卡 10~25 秒后被重置，连接阶段短超时、快速重连比干等强
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

【素材库】（用户上传的文件，和前面步骤做出来的结果；编号从 1 开始，可以填进「文件字段」）
{assets}

【当前方案】（用户正在看的、可能已经手动改过；用户说「再…一点 / 换成…」时，在它的基础上改；steps 里的 step 是步骤序号）
{plan}

【怎么回答】
1. 想做什么大致清楚 → 马上给出完整方案（plan 不能是 null）：reply 里用 1~3 句话说明分了几步、每步用哪个应用、什么模式、写了什么要点；plan.steps 按运行顺序写每一步。用户只给了商品名 / 主题就够了（比如只说「写红苹果的淘宝标题」，商品名就是全部信息：直接选文案应用，需求写「红苹果」），细节让后面的 AI 去补，用户可以在方案卡片里改，所以不要为了细节追问。用户没提到的设置不要写，保持默认；目录里的「高级设置」只有用户明确要求时才改。
2. 一步能做完的就只写一步，别凑步骤；只有需要把前一步的结果交给后一步时才分步（例如：出图 → 用这张图出视频 → 给视频配音 / 做成片）。最多 6 步；用户没要的步骤不要加（尤其是视频：额度很少）。分步时别重复：配音、字幕、背景音乐整个方案只做一处——比如后面有「视频成片」负责配音，前面出视频的那一步就选「只出视频」的模式。
3. 后一步要用前一步的结果：在 files 里把对应的文件字段填成「步骤N」（N 是排在前面的某一步的序号，取它产出的第 1 个该类型的文件）或「步骤N.M」（取第 M 个，比如「步骤1.2」是第 1 步出的第 2 张图）；要用素材库里已有的文件（用户上传的图片、之前做出来的某一张）就填素材库编号。只能引用排在前面的步骤，而且那个应用的「结果」里必须真的有该类型的文件。
4. 只有这两种情况才追问（plan 设为 null，只问一个问题，reply 里不要说「已选择」「已准备」）：完全不知道要做什么；某一步需要图片 / 视频 / 音频，但素材库里没有、前面的步骤也做不出来（请用户点输入框旁的「附件」上传）。
5. 用户在修改方案 → 输出修改后的完整新方案（所有步骤，不是只写改动的部分）；没被要求改的步骤和字段原样保留（包括用户手动改过的值）。
6. 闲聊、或问「你能做什么」→ 简短回答并举 2~3 个例子，plan 设为 null。
7. 不要编造：workflow 填应用编号（字符串，如 "07"，必填）；mode 填该应用「模式」里的编号（数字）；选项类字段只能填列出的选项（选项后面括号里的说明不属于选项本身，填值只写括号前面的名字，或者完整的选项）；数字字段填数字（在给定范围内）；开关字段填 true / false；字段键、文件字段键必须是目录里给出的；素材只能填素材库里有的编号。目录里没有的需求（改视频内容、做 PPT 之类）直接说做不了，别硬凑。
8. 「一句话需求」这类文字字段：写成 1~2 句具体的中文描述（系统后面还会有 AI 进一步扩写，所以不用写成长提示词）；用户给的数字、价格、文字、日期原样保留，不要改写或编造；没提供的信息不要编。「配音文案 / 要念的文字」按用户给的原文，没给就写一段贴合需求的短文案。
9. 目录里写了「费用 / 注意」的应用（比如视频要占每天约 3 个的额度）：方案里用到时，reply 里提醒一句。
10. 看到图片时：reply 里用一句话说明你看到的商品，后续对话要用。用户说「第二张图」之类，对照素材库里的说明找到对应的编号。
11. 两个人以上的对话 / 带角色的配音：用「多角色配音」（只要声音和字幕）或「对白成片」（配到视频 / 图片上）。对白脚本由你来写，填进「对白脚本」：每行「角色：台词」，口语化、每句 20 字以内、两三个来回就够；角色名填进「角色 N 名字」，要和脚本里的一字不差，最多 4 个角色。给每个角色挑音色时看选项后面写的性别和特点：男角色用男声、女角色用女声，老人 / 小孩 / 方言按人设挑，两个角色别用同一个音色，选项里没有的音色不要编。配到视频上时总字数别超过视频能念完的长度（每秒约 4~5 个字，Veo 视频约 10 秒，对白控制在 40 个字以内）。单人配音（只有一个声音）仍用「配音」或视频应用自带的配音，别用多角色。
12. 想保持商品原样（形状、颜色、包装上的字一个像素都不能变）、只换背景 / 调位置大小 / 加阴影：先「商品抠图」做成透明底，再「图层合成」放到背景上（背景图用用户上传的，或先用「文生图」做一张只有背景、没有商品的图），要标签再接「图片后处理」；抠图和合成是分开的两步，改背景或位置只重做合成那一步。想让 AI 把商品和场景的光影融在一起（可以接受商品被重画一点），用「商品改图与合成」。两种做法差别要在 reply 里说清楚。
13. reply 是给用户看的：用「你」称呼，语气简洁友好；说应用和模式时用名字，不要只说编号；reply 和 plan 要一致（plan 是 null 就不能说已经选好了）。
14. 只回复一个 JSON 对象，不要任何别的文字、不要 markdown。键名必须一字不差（reply、plan、steps、workflow、mode、fields、files、note），每一步里一定要有 workflow：
{{"reply": "给用户看的话", "plan": null 或 {{"steps": [{{"workflow": "应用编号字符串", "mode": 模式编号（数字）或 null, "fields": {{"字段键": 值}}, "files": {{"文件字段键": 素材库编号（数字）或 "步骤N"}}, "note": "一句话：这一步做什么"}}], "note": "一句话说明整体思路"}}}}"""


class ChatError(Exception):
    """要原样告诉用户的错误（status 是 HTTP 状态码）。"""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


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
    return SYSTEM.format(catalog=cat_mod.catalog_text(cat), assets=assets_text(assets), plan=plan_text(plan, cat))


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


def call_llm(messages, key, base, model, timeout=LLM_TIMEOUT, post=None, hedge_after=HEDGE_AFTER):
    """OpenAI 兼容的 chat/completions，要求 JSON 回复，返回模型的文字。
    连接失败（卡住 / 被重置）自动重连；等回复超时、HTTP 错误不重发；请求 hedge_after 秒还没回就并发再发一个（最多 MAX_PARALLEL 个），谁先回用谁。"""
    import concurrent.futures as cf
    import requests
    post = post or requests.post
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}

    def attempt():                            # 一个请求；返回 Response（不管状态码），连不上 / 等回复超时抛 ChatError
        body = {"model": model, "messages": messages, "stream": False, "enable_thinking": False, "temperature": 0.2, "max_tokens": 1500,
                "response_format": {"type": "json_object"}}
        for n in range(1, LLM_ATTEMPTS + 1):
            try:
                r = post(base + CHAT_API, headers=headers, json=body, timeout=timeout)
                if r.status_code == 400 and "response_format" in (r.text or "") and "response_format" in body:
                    body.pop("response_format")           # 个别模型不支持 JSON 模式：去掉再试，靠提示词约束
                    r = post(base + CHAT_API, headers=headers, json=body, timeout=timeout)
                return r
            except requests.ReadTimeout:
                wait = timeout[1] if isinstance(timeout, tuple) else timeout
                raise ChatError(504, f"阿里这次回得太慢（等了 {wait} 秒没有回应），再发一次试试")
            except requests.RequestException as e:
                if n == LLM_ATTEMPTS:
                    raise ChatError(502, f"连不上阿里（{type(e).__name__}），稍后再试")

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
        raise ChatError(502, f"阿里返回的不是 JSON（HTTP {r.status_code}）：{scrub((r.text or '')[:150], key)}")
    if r.status_code != 200:
        err = d.get("error", d) if isinstance(d, dict) else {}
        if not isinstance(err, dict):
            err = {"message": err}
        raise ChatError(502, f"阿里返回错误（HTTP {r.status_code} {scrub(err.get('code', ''), key)}）：{scrub(err.get('message', ''), key)[:200]}")
    try:
        return d["choices"][0]["message"]["content"] or ""
    except Exception:
        raise ChatError(502, "阿里的返回里没有文字：" + scrub(json.dumps(d, ensure_ascii=False)[:200], key))


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


def missing_required(plan):
    """方案里没给必填文件的地方：[(步骤序号, 控件名, 文件类型)]。"""
    return [(i, f["label"], f["kind"]) for i, st in enumerate(plan["steps"], 1) for f in cat_mod.required_files(st["schema"], st["mode"]) if f["key"] not in st["files"]]


def chat_turn(payload, cat, key, base, assets, vision, post=None):
    """一轮对话：payload = {messages, plan}；assets = normalize_assets 的结果（usable 已设好）；vision = [(素材库编号, data URL)]（这一轮新附的图片）。
    返回 {reply, plan, warnings}。"""
    if not cat:
        raise ChatError(503, "服务器上没有找到任何「应用」（*.app.json）；先部署工作流")
    history = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    if not any(isinstance(m, dict) and m.get("role") == "user" and str(m.get("content", "")).strip() for m in history):
        raise ChatError(400, "没有收到消息")
    msgs = build_messages(system_prompt(cat, assets, payload.get("plan")), history, vision)
    model = VISION_MODEL if vision else TEXT_MODEL
    for attempt in (1, 2):                    # 格式不对 / 方案里没有可用的应用编号：把问题告诉模型，让它重写一次
        text = call_llm(msgs, key, base, model, post=post)
        try:
            reply, raw_plan = parse_reply(text)
        except ValueError:
            if attempt == 2:
                raise ChatError(502, "模型两次都没有按格式回复，换个说法再试一次")
            fix = "你的回复不是合法的 JSON。请只回复要求的那个 JSON 对象。"
        else:
            plan, warns = make_plan(raw_plan, cat, assets)
            need = missing_required(plan) if plan else []
            if raw_plan is None or (plan is not None and not need) or attempt == 2:
                break
            if plan is not None:                   # 有的步骤漏了必填的文件（比如改商品图却没有图）：让它改一次，还不行就带着提示给用户
                what = "、".join(f"步骤 {i} 的「{label}」" for i, label, _ in need)
                fix = (f"你的方案里{what}必须有文件，但你没有给它指定。请重新输出完整的 JSON：素材库里有合适的就填编号；前面某一步能做出来就填「步骤N」；"
                       "都没有的话，改用不需要上传文件的应用（比如 01 文生图 / 02 海报），或者把 plan 设为 null、在 reply 里请用户先点「附件」上传。")
            else:
                fix = "你的 plan 里没有写对 workflow：每一步都必须是【应用目录】里的应用编号（字符串，如 \"07\"）。请重新输出完整的 JSON，plan.steps 里每一步一定要有 workflow。"
        msgs = msgs + [{"role": "assistant", "content": text or ""}, {"role": "user", "content": fix}]
    if not reply:
        reply = "我给你准备了一个方案，看看合不合适：" if plan else "我没太明白，能再说具体一点吗？"
    return {"reply": reply, "plan": plan, "warnings": warns}
