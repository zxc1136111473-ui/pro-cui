"""应用目录：把工作流目录里的 *.app.json（ComfyUI 的「应用」）整理成助手能看懂、后端能校验的结构。

每个应用 = 一个工作流文件。从 extra.linearData（右栏控件 / 结果节点）和节点定义（/object_info）推出：
- mode：模式下拉（ProMode* 节点的 mode），选项原文
- fields：能设的控件，键是「节点 id:控件名」：文字 / 选项 / 数字（整数、小数）/ 开关，每个带工作流里的默认值。来源有三处：
  应用右栏登记的（extra.linearData.inputs）、只给助手 / 工作台登记的常用设置（extra.appExtra）、高级设置（extra.appAdvanced，用户明确要求才改，方案卡片里折叠）；
  后两处的每一项是 [节点 id, 控件名, 中文名, 一句话说明]，下拉可以再加第 5 项：只开放节点下拉里的这几个选项
- files：能放文件的控件（LoadImage / LoadVideo / LoadAudio），带文件类型 image / video / audio；extra.appRequired 里的是必须有的：
  [节点 id, 控件名] = 不管什么模式都必须有（required）；[节点 id, 控件名, [模式编号…]] = 只在这几个模式必须有（required_modes，编号从 1 数）
- outputs / results / produces：结果节点 id、人话描述、会产出哪几类文件（后一步可以拿来当输入）
- cost：费用 / 额度说明（extra.appCost）
应用的用途描述来自 extra.appDescription（tools/gen_workflows.py 里每个工作流写一句）。
用户自己存的 *.app.json 只要有 extra.linearData 也会进目录（没有描述就只靠文件名和控件名）。只用标准库。
"""
import glob
import json
import math
import os
import re
from difflib import get_close_matches

from .api_convert import convert

MAX_TEXT = 2000
FILE_WIDGETS = {("LoadImage", "image"): "image", ("LoadVideo", "file"): "video", ("LoadAudio", "audio"): "audio"}
FILE_EXTS = {"image": (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"),
             "video": (".mp4", ".webm", ".mov", ".mkv"),
             "audio": (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac")}
KIND_LABEL = {"image": "图片", "video": "视频", "audio": "音频", "text": "文字"}
RESULT_KINDS = {"SaveImage": ("图片", "image"), "SaveVideo": ("视频", "video"), "ProVideoDub": ("视频", "video"),
                "SaveAudioAdvanced": ("音频", "audio"), "SaveAudio": ("音频", "audio"),
                "ProSaveCutout": ("抠好的商品（透明底图片）", "image"), "ProLayerCompose": ("合成图片（开了「同时存分层 PNG」时，后面还跟着背景层 / 商品层 / 阴影层）", "image")}
NUMBER_KINDS = ("int", "float")
LONG_OPTIONS = 8             # 选项多于这个数的下拉，目录里同一份选项只完整写一次（音色有 49 个）
IMAGE_DIR = "助手"          # 聊天里上传 / 中转的素材放在 input/助手/ 下，只允许用这个目录里的
# 给模型看的字段说明：写提示词节点自己的 tooltip 提到「下面的扩写指令」，在目录里读起来会把模型带偏，换成这句
FIELD_HINTS = {("ProAliPromptWriter", "idea"): "写 1~2 句具体的中文描述，系统会再自动扩写成完整提示词"}


def file_kind(name):
    """文件名 → image / video / audio；认不出返回 None（按扩展名）。"""
    ext = os.path.splitext(str(name or ""))[1].lower()
    for kind, exts in FILE_EXTS.items():
        if ext in exts:
            return kind
    return None


def safe_asset_name(name):
    """聊天里上传 / 中转的素材名只能是「助手/文件名」：按路径分段判断（不是子串：「photo..png」是合法文件名），不接受 ..、空段、反斜杠、NUL。
    返回规整后的名字，不合规返回 None。（这只是字符串规则；文件真在目录里、没有符号链接逃出去，由后端的 _asset_path 用真实路径再查。）"""
    name = str(name or "")
    if not name.startswith(IMAGE_DIR + "/") or "\\" in name or "\x00" in name:
        return None
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return name


def widget_spec(oi, ntype, widget):
    inp = (oi.get(ntype) or {}).get("input") or {}
    for sect in ("required", "optional"):
        if widget in (inp.get(sect) or {}):
            return inp[sect][widget]
    return None


def raw_options(spec):
    """下拉控件的选项原值（可能是整数 / 小数，比如帧率 24 / 30）；不是下拉返回 None。"""
    if not spec:
        return None
    typ = spec[0]
    if isinstance(typ, list):
        return list(typ)
    meta = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    return list(meta.get("options", [])) if typ == "COMBO" else None


def option_cast(raw):
    """选项原值全是整数 → "int"，全是数字 → "float"，否则 None。下拉在界面 / 提示词里一律当文字，运行时要按这个转回原来的类型，
    不然 ComfyUI 会说「值不在选项里」（它的下拉是严格比对的：整数 24 ≠ 文字 "24"）。"""
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    if raw and all(isinstance(x, int) and not isinstance(x, bool) for x in raw):
        return "int"
    return "float" if raw and all(num(x) for x in raw) else None


def option_for(default, options):
    """工作流里存的默认值对应哪个选项（文字）：先按文字比；对不上再按数值比（前端的 JS 会把 1.0 存成 1，而选项里是 "1.0"）。都对不上返回 None。"""
    if default is None:
        return None
    if str(default) in options:
        return str(default)
    d = _num(default)
    if d is not None:
        for o in options:
            try:
                if float(o) == d:
                    return o
            except ValueError:
                pass
    return None


def widget_kind(spec):
    """控件规格 → (kind, options, meta)。kind: text / choice / int / float / bool；认不出（连线输入、图片……）是 None。"""
    if not spec:
        return None, None, {}
    typ = spec[0]
    meta = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    if isinstance(typ, list):
        return "choice", [str(x) for x in typ], meta
    if typ == "COMBO":
        return "choice", [str(x) for x in meta.get("options", [])], meta
    if typ == "STRING":
        return "text", None, meta
    if typ == "INT":
        return "int", None, meta
    if typ == "FLOAT":
        return "float", None, meta
    if typ == "BOOLEAN":
        return "bool", None, meta
    return None, None, meta


def app_id_name(rel):
    """1-图片/01-文生图.app.json → ("01", "文生图")；没有数字前缀就用整个文件名。"""
    stem = os.path.basename(rel)
    for suffix in (".app.json", ".json"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    head, sep, rest = stem.partition("-")
    if sep and head.isdigit():
        return head, rest
    return stem, stem


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def load_app(wf, rel, oi):
    """一个工作流 → 应用条目；没有应用配置（extra.linearData）返回 None。"""
    ex = wf.get("extra") or {}
    ld = ex.get("linearData") or {}
    ins, outs = ld.get("inputs") or [], ld.get("outputs") or []
    if not ins or not outs:
        return None
    nodes = {n["id"]: n for n in wf.get("nodes", [])}
    try:
        defaults = convert(wf, oi)                      # 各控件在工作流里存的值（= 应用里的默认值）
    except Exception:
        defaults = {}
    app_id, name = app_id_name(rel)
    app = {"id": app_id, "name": name, "path": rel.replace(os.sep, "/"), "desc": str(ex.get("appDescription") or "").strip(),
           "cost": str(ex.get("appCost") or "").strip(), "mode": None, "fields": {}, "files": {}, "outputs": [], "results": [], "produces": [],
           "titles": {str(n["id"]): (n.get("title") or n["type"]) for n in wf.get("nodes", [])}}
    entries = [(nid, widget, "", "", False, None) for nid, widget in ins]
    for sect, adv in (("appExtra", False), ("appAdvanced", True)):
        entries += [(e[0], e[1], str(e[2]), str(e[3]) if len(e) > 3 else "", adv, e[4] if len(e) > 4 and isinstance(e[4], list) else None)
                    for e in ex.get(sect) or [] if isinstance(e, list) and len(e) >= 3]
    for nid, widget, label_set, hint_set, adv, only in entries:
        n = nodes.get(nid)
        if n is None:
            continue
        key = f"{nid}:{widget}"
        if key in app["fields"] or key in app["files"] or (app["mode"] and app["mode"]["key"] == key):
            continue                                    # 同一个控件只登记一次
        if any(i.get("name") == widget and i.get("link") is not None for i in n.get("inputs", [])):
            continue                                    # 被连线占用的控件不是用户填的
        spec = widget_spec(oi, n["type"], widget)
        kind, options, meta = widget_kind(spec)
        label = label_set or next((i["label"] for i in n.get("inputs", []) if i.get("name") == widget and i.get("label")), None) or meta.get("display_name") or widget
        hint = hint_set or FIELD_HINTS.get((n["type"], widget)) or str(meta.get("tooltip") or "")
        default = (defaults.get(str(nid)) or {}).get("inputs", {}).get(widget)
        if n["type"].startswith("ProMode") and widget == "mode" and options:
            d = (n.get("widgets_values") or [options[0]])[0]
            app["mode"] = {"key": key, "label": label, "options": options, "default": d if d in options else options[0]}
            continue
        if (n["type"], widget) in FILE_WIDGETS:
            app["files"][key] = {"key": key, "label": label, "kind": FILE_WIDGETS[(n["type"], widget)]}
            continue
        if kind == "text":
            f = {"key": key, "label": label, "kind": "text", "default": str(default or "")[:300], "hint": hint}
            if meta.get("multiline") is False:
                f["multiline"] = False                  # 节点明确写了「单行」（角色名、颜色这类短文字）：界面里用单行输入框
        elif kind == "choice" and options:
            if only:
                options = [o for o in (str(x) for x in only) if o in options] or options              # 顺序按登记时写的
            dstr = option_for(default, options)
            f = {"key": key, "label": label, "kind": "choice", "options": options, "default": dstr if dstr is not None else options[0]}
            cast = option_cast(raw_options(spec))
            if cast:
                f["cast"] = cast
            if hint_set:
                f["hint"] = hint_set
        elif kind in NUMBER_KINDS:
            lo, hi = _num(meta.get("min")), _num(meta.get("max"))
            f = {"key": key, "label": label, "kind": kind, "min": lo, "max": hi, "step": _num(meta.get("step")),
                 "default": _num(default) if _num(default) is not None else (lo if lo is not None else 0), "hint": hint}
        elif kind == "bool":
            f = {"key": key, "label": label, "kind": "bool", "default": bool(default), "hint": hint}
        else:
            continue
        if adv:
            f["advanced"] = True
        app["fields"][key] = f
    for entry in ex.get("appRequired") or []:
        key = f"{entry[0]}:{entry[1]}"
        if key in app["files"]:
            modes = entry[2] if len(entry) > 2 and isinstance(entry[2], list) else None
            if modes:
                app["files"][key]["required_modes"] = sorted({int(m) for m in modes})
            else:
                app["files"][key]["required"] = True
    counts = {}
    for nid in outs:
        n = nodes.get(nid)
        if n is None:
            continue
        app["outputs"].append(nid)
        if n["type"] == "ProAppText":
            label, only_on_error = (n.get("widgets_values") or ["文字", False])[:2]
            desc, pk = f"文字「{label}」" + ("（只在出错时出现）" if only_on_error else ""), "text"
        else:
            desc, pk = RESULT_KINDS.get(n["type"], (n["type"], None))
        counts[desc] = counts.get(desc, 0) + 1
        if pk and pk not in app["produces"]:
            app["produces"].append(pk)
    app["results"] = [d if c == 1 else f"{d} ×{c}" for d, c in counts.items()]
    return app


def build_catalog(workflows_dir, oi):
    """目录：{应用编号: 应用条目}，按文件路径排序；读不了的文件跳过。"""
    cat = {}
    for p in sorted(glob.glob(os.path.join(workflows_dir, "**", "*.app.json"), recursive=True)):
        try:
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            app = load_app(wf, os.path.relpath(p, workflows_dir), oi)
        except Exception:
            continue
        if app and app["id"] not in cat:
            cat[app["id"]] = app
    add_labels(cat)
    return cat


def option_notes(cat):
    """选项里「名字 · 说明」这种写法（多角色对白的音色下拉：Cherry · 女 · 阳光亲切）→ {小写名字: 说明}。"""
    notes = {}
    for app in cat.values():
        for f in app["fields"].values():
            if f["kind"] == "choice":
                for o in f["options"]:
                    head, sep, rest = o.partition(" · ")
                    if sep and head.strip() and rest.strip():
                        notes.setdefault(head.strip().lower(), rest.strip())
    return notes


def add_labels(cat):
    """别的下拉里有同名的纯名字选项（08 / 10 / 11 / 12 的「配音音色」只有名字）：借上面的说明给它们加 labels（选项 → 显示文字），
    工作台的下拉里就能看到性别和特点，目录里也写给模型看。值还是纯名字，不变。"""
    notes = option_notes(cat)
    if not notes:
        return
    for app in cat.values():
        for f in app["fields"].values():
            if f["kind"] == "choice" and not any(" · " in o for o in f["options"]):
                labels = {o: f"{o} · {notes[o.lower()]}" for o in f["options"] if o.lower() in notes}
                if labels:
                    f["labels"] = labels


def _fmt_num(x):
    return str(int(x)) if isinstance(x, float) and x == int(x) else str(x)


def field_line(f, tables):
    """一个字段在目录里的一行：键｜名称｜类型（范围 / 选项）、默认值、说明。
    tables：{选项元组: [表名, 是否已写过]}，只含「选项很多、而且好几个字段共用」的下拉（比如 49 个音色）：第一次完整写出，之后写「选项同…」。"""
    kind = f["kind"]
    if kind == "text":
        typ = "文字"
        extra = f"，说明：{f['hint']}" if f.get("hint") else ""
    elif kind == "choice":
        typ = "选项"
        opts = f["options"]
        shared = tables.get(tuple(opts))
        labels = f.get("labels") or {}
        shown = " / ".join(f"{o}（{labels[o][len(o) + 3:]}）" if o in labels else o for o in opts)       # 选项后面括号里的是说明，填值时只写括号前面的名字
        if shared and shared[1]:
            extra = f"，选项同「{shared[0]}」"
        else:
            if shared:
                shared[1] = True
            extra = (f"，选项（{shared[0]}）：" if shared else "，选项：") + shown
        extra += f"，默认 {f['default']}" + (f"，说明：{f['hint']}" if f.get("hint") else "")
    elif kind in NUMBER_KINDS:
        typ = "整数" if kind == "int" else "小数"
        lo, hi = f.get("min"), f.get("max")
        rng = f"{_fmt_num(lo)}~{_fmt_num(hi)}" if lo is not None and hi is not None and hi < 1e9 else ""
        extra = (f"，范围 {rng}" if rng else "") + f"，默认 {_fmt_num(f['default'])}"
        if f.get("hint"):
            extra += f"，说明：{f['hint']}"
    else:
        typ = "开关"
        extra = f"，默认{'开' if f['default'] else '关'}"
        if f.get("hint"):
            extra += f"，说明：{f['hint']}"
    return f"    - {f['key']}｜{f['label']}｜{typ}{extra}"


def catalog_text(cat):
    """给模型看的文字版目录。"""
    counts = {}
    for app in cat.values():
        for f in app["fields"].values():
            if f["kind"] == "choice" and len(f["options"]) > LONG_OPTIONS:
                counts[tuple(f["options"])] = counts.get(tuple(f["options"]), 0) + 1
    tables = {o: [f"选项表{i}", False] for i, o in enumerate((o for o, c in counts.items() if c > 1), 1)}
    out = []
    for app in cat.values():
        out.append(f"应用 {app['id']}「{app['name']}」")
        if app["desc"]:
            out.append(f"  用途：{app['desc']}")
        if app["cost"]:
            out.append(f"  费用 / 注意：{app['cost']}")
        if app["mode"]:
            out.append("  模式（选一个，填编号）：")
            out += [f"    {i}. {o}" for i, o in enumerate(app["mode"]["options"], 1)]
        common = [f for f in app["fields"].values() if not f.get("advanced")]
        adv = [f for f in app["fields"].values() if f.get("advanced")]
        if common:
            out.append("  常用字段（键｜名称｜类型）：")
            out += [field_line(f, tables) for f in common]
        if app["files"]:
            out.append("  文件字段（填素材库编号，或前面某一步产出的「步骤N」；类型写在最后；标了「必填」的一定要指定（标了「选模式 N 时必填」的，选了那个模式就一定要指定），素材库里没有又没有前一步能做出来，就先请用户上传）：")
            out += [f"    - {f['key']}｜{f['label']}｜{KIND_LABEL[f['kind']]}" + required_note(f) for f in app["files"].values()]
        if adv:
            out.append("  高级设置（用户明确要求才改，不要主动写）：")
            out += [field_line(f, tables) for f in adv]
        got = "、".join(KIND_LABEL[k] for k in app["produces"] if k != "text")
        out.append("  结果：" + "、".join(app["results"]) + (f"（{got}可以交给后面的步骤当输入）" if got else ""))
        out.append("")
    return "\n".join(out).rstrip()


def schema(app):
    """给前端画方案卡片用的：能改什么、结果怎么取。"""
    return {"mode": app["mode"], "fields": app["fields"], "files": app["files"], "outputs": app["outputs"], "results": app["results"],
            "produces": app["produces"], "cost": app["cost"], "desc": app["desc"], "titles": app["titles"]}


_PAREN_TAIL = re.compile(r"\s*[（(][^）)]*[）)]\s*$")


def _bare(x):
    """去掉末尾括号里的说明（全角 / 半角都行）。"""
    return _PAREN_TAIL.sub("", x).strip()


def _loose(x):
    """比较用的写法：半角括号当全角、去空白、不分大小写。"""
    return x.replace("(", "（").replace(")", "）").strip().lower()


def pick_option(v, options, allow_index):
    """模型 / 用户给的值 → 选项原文；认不出返回 None。allow_index（模式用）：数字当成「第几个」（从 1 数），
    并且容忍长选项文字有小出入；短选项（比例、风格名……）必须一字不差（只忽略首尾空格和大小写），不然「5:4」会被当成「3:4」。"""
    if isinstance(v, bool) or v is None:
        return None
    if allow_index and (isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"[0-9]+", v.strip()))):
        try:
            i = int(v)
        except (ValueError, OverflowError):                 # NaN / Infinity（json 能解出来）/ 超长数字串
            return None
        return options[i - 1] if 1 <= i <= len(options) else None
    s = str(v).strip()
    if s in options:
        return s
    for o in options:
        if o.lower() == s.lower():
            return o
    if not allow_index:
        # 模型常常不照抄整个选项：写成「Cherry（女 · 阳光亲切）」「强」「强(边缘更紧)」「Cherry」都行 —— 括号（全角 / 半角）里的说明不参与比较；
        # 选项写成「名字 · 说明」时只写名字也行。必须能唯一确定是哪个选项，不然当没填
        for name in dict.fromkeys(_loose(c) for c in (s, _bare(s), s.split(" · ")[0]) if c.strip()):
            hits = [o for o in options if name in {_loose(o), _loose(_bare(o)), _loose(o.split(" · ")[0])}]
            if len(hits) == 1:
                return hits[0]
        return None
    if len(s) >= 6:
        hit = get_close_matches(s, options, n=1, cutoff=0.75)
        return hit[0] if hit else None
    return None


def as_text(v):
    """模型 / 客户端给的字段值 → 文字：列表按行拼，字典转 JSON，None 是空，其余 str()。"""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "\n".join(as_text(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def coerce_number(val, f):
    """数字字段的值 → (数, 是否被调整过)；不是有效数字返回 None。超出范围的收回范围内，整数四舍五入。"""
    if isinstance(val, bool):
        return None
    try:
        x = float(val.strip() if isinstance(val, str) else val)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(x):
        return None
    lo, hi = f.get("min"), f.get("max")
    clamped = False
    if lo is not None and x < lo:
        x, clamped = lo, True
    if hi is not None and x > hi:
        x, clamped = hi, True
    if f["kind"] == "int":
        x = int(round(x))
    return x, clamped


def coerce_bool(val):
    """开关字段的值 → True / False；认不出返回 None。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)) and val in (0, 1):
        return bool(val)
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("true", "yes", "on", "1", "是", "开", "开启", "打开", "要"):
            return True
        if s in ("false", "no", "off", "0", "否", "关", "关闭", "不要"):
            return False
    return None


def lookup_key(table, key):
    """字段键 → 目录里的条目。键应该是「节点 id:控件名」；模型偶尔只写节点 id（"70"）或只写控件名（"script"），这时能唯一确定就认它。"""
    if key in table:
        return table[key]
    if isinstance(key, str) and key.isascii() and key.isdigit():
        hits = [v for k, v in table.items() if k.startswith(key + ":")]
        common = [v for v in hits if not v.get("advanced")]
        if len(hits) == 1 or len(common) == 1:              # 该节点只有一个可填的，或只有一个常用的（其余是高级设置）
            return (hits if len(hits) == 1 else common)[0]
    elif isinstance(key, str) and key.strip() and ":" not in key:
        hits = [v for k, v in table.items() if k.split(":", 1)[-1].lower() == key.strip().lower()]
        if len(hits) == 1:                                  # 模型漏写了节点编号（"script"），整个应用里只有一个叫这个名字的控件就认它
            return hits[0]
    return None


def mode_number(app, mode):
    """模式文字 → 第几个模式（从 1 数）；没选 / 认不出就是默认模式；这个应用没有模式返回 None。app 也可以是方案里的 schema（有 mode / files 两项就行）。"""
    m = app.get("mode")
    if not m:
        return None
    opts = m["options"]
    chosen = mode if mode in opts else m.get("default")
    return opts.index(chosen) + 1 if chosen in opts else 1


def required_files(app, mode):
    """选定模式（模式文字，None = 默认模式）下必须有的文件控件：不管什么模式都必填的 + 只在这个模式必填的（比如「放到背景图上」模式要背景图，纯色模式不要）。"""
    n = mode_number(app, mode)
    return [f for f in app["files"].values() if f.get("required") or (n is not None and n in (f.get("required_modes") or ()))]


def required_note(f):
    """目录 / 界面里文件控件后面的必填标记。"""
    if f.get("required"):
        return "（必填）"
    modes = f.get("required_modes") or []
    return f"（选模式 {'、'.join(str(m) for m in modes)} 时必填）" if modes else ""


def range_text(f):
    lo, hi = f.get("min"), f.get("max")
    if lo is not None and hi is not None:
        return f"{_fmt_num(lo)}~{_fmt_num(hi)}"
    return f"不小于 {_fmt_num(lo)}" if lo is not None else f"不大于 {_fmt_num(hi)}"


def clean_selection(app, mode, fields, files, resolve_file, cast=False):
    """校验并整理一次「模式 + 字段 + 文件」的选择；不认识的丢掉并给出提示。
    resolve_file(键, 引用, 文件类型) → 能用的引用（聊天方案里是素材 / 前面某一步的结果，运行时是 input 里的文件名）或 None。返回 (selection, warnings)。
    cast=True（运行时）：选项原值是数字的下拉（帧率 24 / 30）转回数字再交给 ComfyUI；方案里（cast=False）一直是文字，和界面、提示词里一致。"""
    warns = []
    sel = {"mode": None, "fields": {}, "files": {}}
    if app["mode"] and mode not in (None, ""):
        m = pick_option(mode, app["mode"]["options"], True)
        if m is None:
            warns.append(f"模式「{mode}」不在可选项里，已用默认模式")
        else:
            sel["mode"] = m
    for key, val in (fields if isinstance(fields, dict) else {}).items():
        f = lookup_key(app["fields"], key)
        if f is None:
            warns.append(f"没有字段「{key}」，已忽略")
            continue
        kind, label = f["kind"], f["label"]
        if kind == "text":
            t = as_text(val).strip()
            if len(t) > MAX_TEXT:
                warns.append(f"「{label}」太长，只用了前 {MAX_TEXT} 个字")
                t = t[:MAX_TEXT]
            if t:
                sel["fields"][f["key"]] = t
        elif kind == "choice":
            v = pick_option(val, f["options"], False)
            if v is None:
                warns.append(f"「{label}」的值「{val}」不在可选项里，已保持默认")
            else:
                sel["fields"][f["key"]] = {"int": int, "float": float}[f["cast"]](v) if cast and f.get("cast") else v
        elif kind in NUMBER_KINDS:
            r = coerce_number(val, f)
            if r is None:
                warns.append(f"「{label}」的值「{val}」不是有效的数字，已保持默认")
            else:
                x, clamped = r
                if clamped:
                    warns.append(f"「{label}」超出范围（{range_text(f)}），已调整为 {_fmt_num(x)}")
                sel["fields"][f["key"]] = x
        else:
            b = coerce_bool(val)
            if b is None:
                warns.append(f"「{label}」的值「{val}」不是开 / 关，已保持默认")
            else:
                sel["fields"][f["key"]] = b
    for key, ref in (files if isinstance(files, dict) else {}).items():
        fl = lookup_key(app["files"], key)
        if fl is None:
            warns.append(f"没有文件字段「{key}」，已忽略")
            continue
        r = resolve_file(fl["key"], ref, fl["kind"])
        if r is None:
            warns.append(f"「{fl['label']}」指定的{KIND_LABEL[fl['kind']]}不存在或不能用，已用默认")
        else:
            sel["files"][fl["key"]] = r
    return sel, warns
