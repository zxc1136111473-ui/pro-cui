"""AI 助手的对话逻辑：拼提示词、调阿里文字模型、解析并校验模型给出的方案。

模型只负责「听懂需求 → 选应用 → 选模式 → 写字段」，输出一个 JSON；真正能不能用由 catalog.clean_selection 校验，
模型编的应用 / 模式 / 字段 / 图片一律丢掉，所以不会让它改到目录之外的东西。requests 只在真正调阿里时才 import。
"""
import json
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
MAX_IMAGES = 12

SYSTEM = """你是「电商素材助手」，帮用户在 ComfyUI 里生成图片、视频、音频和文案。下面是系统里能用的「应用」。你的任务：听懂用户想做什么 → 选一个最合适的应用 → 选好模式、写好要填的字段 → 给出方案，用户点「生成」确认后才会运行。

【应用目录】
{catalog}

【用户已上传的图片】（编号从 1 开始，可以填进「图片」字段）
{images}

【当前方案】（用户正在看的、可能已经手动改过；用户说「再…一点 / 换成…」时，在它的基础上改）
{plan}

【怎么回答】
1. 想做什么大致清楚 → 马上给出完整方案（plan 不能是 null）：reply 里用 1~3 句话说明选了哪个应用、什么模式、写了什么要点；plan 里写好 workflow、mode 和 fields。用户只给了商品名 / 主题就够了（比如只说「写红苹果的淘宝标题」，商品名就是全部信息：直接选文案应用，需求写「红苹果」），细节让后面的 AI 去补，用户可以在方案卡片里改，所以不要为了细节追问。用户没提到的设置不要写，保持默认。
2. 只有这两种情况才追问（plan 设为 null，只问一个问题，reply 里不要说「已选择」「已准备」）：完全不知道要做什么；选中的模式需要图片，但「用户已上传的图片」里没有（请用户点输入框旁的「附图」上传）。
3. 用户在修改方案 → 输出修改后的完整新方案（不是只写改动的部分）。
4. 闲聊、或问「你能做什么」→ 简短回答并举 2~3 个例子，plan 设为 null。
5. 不要编造：workflow 填应用编号（字符串，如 "07"，必填）；mode 填该应用「模式」里的编号（数字）；选项类字段只能填列出的选项；字段键、图片字段键必须是目录里给出的；图片只能填已上传图片的编号。目录里没有的需求（改视频内容、做 PPT 之类）直接说做不了，别硬凑。
6. 聊天里不能上传视频 / 音频：应用需要用户自己的视频 / 音频时，在 reply 里提醒用户去「应用」里上传，这种情况 plan 设为 null。
7. 「一句话需求」这类文字字段：写成 1~2 句具体的中文描述（系统后面还会有 AI 进一步扩写，所以不用写成长提示词）；用户给的数字、价格、文字、日期原样保留，不要改写或编造；没提供的信息不要编。「配音文案 / 要念的文字」按用户给的原文，没给就写一段贴合需求的短文案。
8. 视频（Veo）要占每天约 3 个的额度：选到视频类应用时，reply 里提醒一句。
9. 看到图片时：reply 里用一句话说明你看到的商品，后续对话要用。
10. reply 是给用户看的：用「你」称呼，语气简洁友好；说应用和模式时用名字，不要只说编号；reply 和 plan 要一致（plan 是 null 就不能说已经选好了）。
11. 只回复一个 JSON 对象，不要任何别的文字、不要 markdown。键名必须一字不差（reply、plan、workflow、mode、fields、images、note），plan 里一定要有 workflow：
{{"reply": "给用户看的话", "plan": null 或 {{"workflow": "应用编号字符串", "mode": 模式编号（数字）或 null, "fields": {{"字段键": "值"}}, "images": {{"图片字段键": 图片编号}}, "note": "一句话说明为什么这么选"}}}}"""


class ChatError(Exception):
    """要原样告诉用户的错误（status 是 HTTP 状态码）。"""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def plan_text(plan, names):
    """当前方案（前端传来的）→ 给模型看的一行 JSON；图片文件名换回编号。字段类型不对（客户端乱传）的一律当没有。"""
    if not isinstance(plan, dict) or not plan.get("workflow"):
        return "（还没有）"
    images = plan.get("images") if isinstance(plan.get("images"), dict) else {}
    fields = plan.get("fields") if isinstance(plan.get("fields"), dict) else {}
    imgs = {str(k): names.index(v) + 1 for k, v in images.items() if isinstance(v, str) and v in names}
    return json.dumps({"workflow": str(plan.get("workflow")), "mode": plan.get("mode") if isinstance(plan.get("mode"), (str, int, type(None))) else None,
                       "fields": {str(k): cat_mod.as_text(v) for k, v in fields.items()}, "images": imgs}, ensure_ascii=False)


def system_prompt(cat, names, plan, usable=None):
    usable = set(names) if usable is None else usable
    imgs = "\n".join(f"{i}. {n.split('/', 1)[-1]}" + ("" if n in usable else "（文件已经不存在，不要用）") for i, n in enumerate(names, 1)) or "（还没有）"
    return SYSTEM.format(catalog=cat_mod.catalog_text(cat), images=imgs, plan=plan_text(plan, names))


def build_messages(system, history, vision_urls):
    """system + 最近的对话；最后一条用户消息带上这一轮新附的图片（视觉输入）。"""
    msgs = [{"role": "system", "content": system}]
    hist = [m for m in (history or []) if isinstance(m, dict) and m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()]
    for m in hist[-MAX_HISTORY:]:
        msgs.append({"role": m["role"], "content": str(m["content"]).strip()[:MAX_CONTENT]})
    if vision_urls and msgs[-1]["role"] == "user":
        text = msgs[-1]["content"]
        msgs[-1]["content"] = [{"type": "image_url", "image_url": {"url": u}} for u in vision_urls] + [{"type": "text", "text": text}]
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
        raise ChatError(502, "阿里的返回里没有文字：" + json.dumps(d, ensure_ascii=False)[:200])


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


def make_plan(raw, cat, resolve_image):
    """模型给的方案 → 校验后的方案（带 schema，前端直接画卡片）；应用都不存在就返回 None。"""
    if not isinstance(raw, dict):
        return None, []
    app, given = find_app(cat, raw)
    if app is None:
        return None, [f"没有编号为「{given}」的应用，方案已丢弃" if given else "方案里没有写应用编号，已丢弃"]
    sel, warns = cat_mod.clean_selection(app, raw.get("mode"), raw.get("fields"), raw.get("images"), resolve_image)
    plan = {"workflow": app["id"], "name": app["name"], "path": app["path"], "mode": sel["mode"], "fields": sel["fields"],
            "images": sel["images"], "note": str(raw.get("note") or "").strip()[:200], "schema": cat_mod.schema(app)}
    return plan, warns


def chat_turn(payload, cat, key, base, image_names, vision_urls, usable=None, post=None):
    """一轮对话：payload = {messages, plan}；image_names = 这次对话里已上传的图片（按编号顺序）；usable = 其中文件还在的；
    vision_urls = 这一轮新附图片的 data URL。返回 {reply, plan, warnings}。"""
    if not cat:
        raise ChatError(503, "服务器上没有找到任何「应用」（*.app.json）；先部署工作流")
    history = payload.get("messages") or []
    if not any(isinstance(m, dict) and m.get("role") == "user" and str(m.get("content", "")).strip() for m in history):
        raise ChatError(400, "没有收到消息")
    names = list(image_names)[:MAX_IMAGES]
    usable = set(names) if usable is None else set(usable)
    msgs = build_messages(system_prompt(cat, names, payload.get("plan"), usable), history, vision_urls)
    model = VISION_MODEL if vision_urls else TEXT_MODEL

    def resolve(ref):                         # 模型给的是图片编号
        if isinstance(ref, bool):
            return None
        try:
            i = int(ref)
        except (TypeError, ValueError, OverflowError):
            return None
        return names[i - 1] if 1 <= i <= len(names) and names[i - 1] in usable else None

    for attempt in (1, 2):                    # 格式不对 / 方案里没有可用的应用编号：把问题告诉模型，让它重写一次
        text = call_llm(msgs, key, base, model, post=post)
        try:
            reply, raw_plan = parse_reply(text)
        except ValueError:
            if attempt == 2:
                raise ChatError(502, "模型两次都没有按格式回复，换个说法再试一次")
            fix = "你的回复不是合法的 JSON。请只回复要求的那个 JSON 对象。"
        else:
            plan, warns = make_plan(raw_plan, cat, resolve)
            if raw_plan is None or plan is not None or attempt == 2:
                break
            fix = "你的 plan 里没有写对 workflow：必须是【应用目录】里的应用编号（字符串，如 \"07\"）。请重新输出完整的 JSON，plan 里一定要有 workflow。"
        msgs = msgs + [{"role": "assistant", "content": text or ""}, {"role": "user", "content": fix}]
    if not reply:
        reply = "我给你准备了一个方案，看看合不合适：" if plan else "我没太明白，能再说具体一点吗？"
    return {"reply": reply, "plan": plan, "warnings": warns}
