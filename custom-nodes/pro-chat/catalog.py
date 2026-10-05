"""应用目录：把工作流目录里的 *.app.json（ComfyUI 的「应用」）整理成助手能看懂、后端能校验的结构。

每个应用 = 一个工作流文件。从 extra.linearData（右栏控件 / 结果节点）和节点定义（/object_info）推出：
- mode：模式下拉（ProMode* 节点的 mode），选项原文
- fields：能填的文字 / 选项控件，键是「节点 id:控件名」
- images：能传图片的控件（LoadImage）
- uploads：聊天里设不了的上传（音频 / 视频），只提醒用户去应用里传
- outputs / results：结果节点 id 和人话描述
应用的用途描述来自 extra.appDescription（tools/gen_workflows.py 里每个工作流写一句）。
用户自己存的 *.app.json 只要有 extra.linearData 也会进目录（没有描述就只靠文件名和控件名）。只用标准库。
"""
import glob
import json
import os
import re
from difflib import get_close_matches

MAX_TEXT = 2000
IMAGE_WIDGETS = {("LoadImage", "image")}
UPLOAD_WIDGETS = {("LoadAudio", "audio"): "音频", ("LoadVideo", "file"): "视频"}
RESULT_KINDS = {"SaveImage": "图片", "SaveVideo": "视频", "ProVideoDub": "视频", "SaveAudioAdvanced": "音频", "SaveAudio": "音频"}
IMAGE_DIR = "助手"          # 聊天里上传的图片放在 input/助手/ 下，只允许用这个目录里的
# 给模型看的字段说明：写提示词节点自己的 tooltip 提到「下面的扩写指令」，在目录里读起来会把模型带偏，换成这句
FIELD_HINTS = {("ProAliPromptWriter", "idea"): "写 1~2 句具体的中文描述，系统会再自动扩写成完整提示词"}


def safe_image_name(name):
    """聊天里上传的图片名只能是「助手/文件名」：按路径分段判断（不是子串：「photo..png」是合法文件名），不接受 ..、空段、反斜杠、NUL。
    返回规整后的名字，不合规返回 None。（这只是字符串规则；文件真在目录里、没有符号链接逃出去，由后端的 _image_path 用真实路径再查。）"""
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


def widget_kind(spec):
    """控件规格 → (kind, options, meta)。kind: text / choice / None（数字、开关这类聊天里不设）。"""
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


def load_app(wf, rel, oi):
    """一个工作流 → 应用条目；没有应用配置（extra.linearData）返回 None。"""
    ex = wf.get("extra") or {}
    ld = ex.get("linearData") or {}
    ins, outs = ld.get("inputs") or [], ld.get("outputs") or []
    if not ins or not outs:
        return None
    nodes = {n["id"]: n for n in wf.get("nodes", [])}
    app_id, name = app_id_name(rel)
    app = {"id": app_id, "name": name, "path": rel.replace(os.sep, "/"), "desc": str(ex.get("appDescription") or "").strip(),
           "mode": None, "fields": {}, "images": {}, "uploads": [], "outputs": [], "results": [],
           "titles": {str(n["id"]): (n.get("title") or n["type"]) for n in wf.get("nodes", [])}}
    for nid, widget in ins:
        n = nodes.get(nid)
        if n is None:
            continue
        key = f"{nid}:{widget}"
        spec = widget_spec(oi, n["type"], widget)
        kind, options, meta = widget_kind(spec)
        label = next((i["label"] for i in n.get("inputs", []) if i.get("name") == widget and i.get("label")), None) or meta.get("display_name") or widget
        if n["type"].startswith("ProMode") and widget == "mode" and options:
            default = (n.get("widgets_values") or [options[0]])[0]
            app["mode"] = {"key": key, "label": label, "options": options, "default": default if default in options else options[0]}
        elif (n["type"], widget) in IMAGE_WIDGETS:
            app["images"][key] = {"key": key, "label": label}
        elif (n["type"], widget) in UPLOAD_WIDGETS:
            app["uploads"].append({"key": key, "label": label, "kind": UPLOAD_WIDGETS[(n["type"], widget)]})
        elif kind == "text":
            app["fields"][key] = {"key": key, "label": label, "kind": "text", "hint": FIELD_HINTS.get((n["type"], widget), str(meta.get("tooltip") or ""))}
        elif kind == "choice" and options:
            app["fields"][key] = {"key": key, "label": label, "kind": "choice", "options": options}
    counts = {}
    for nid in outs:
        n = nodes.get(nid)
        if n is None:
            continue
        app["outputs"].append(nid)
        if n["type"] == "ProAppText":
            label, only_on_error = (n.get("widgets_values") or ["文字", False])[:2]
            desc = f"文字「{label}」" + ("（只在出错时出现）" if only_on_error else "")
        else:
            desc = RESULT_KINDS.get(n["type"], n["type"])
        counts[desc] = counts.get(desc, 0) + 1
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
    return cat


def catalog_text(cat):
    """给模型看的文字版目录。"""
    out = []
    for app in cat.values():
        out.append(f"应用 {app['id']}「{app['name']}」")
        if app["desc"]:
            out.append(f"  用途：{app['desc']}")
        if app["mode"]:
            out.append("  模式（选一个，填编号）：")
            out += [f"    {i}. {o}" for i, o in enumerate(app["mode"]["options"], 1)]
        if app["fields"]:
            out.append("  可填字段（键｜名称｜类型）：")
            for f in app["fields"].values():
                extra = f"，选项：{' / '.join(f['options'])}" if f["kind"] == "choice" else (f"，说明：{f['hint']}" if f.get("hint") else "")
                out.append(f"    - {f['key']}｜{f['label']}｜{'文字' if f['kind'] == 'text' else '选项'}{extra}")
        if app["images"]:
            out.append("  图片字段（填已上传图片的编号）：")
            out += [f"    - {i['key']}｜{i['label']}" for i in app["images"].values()]
        if app["uploads"]:
            out.append("  聊天里设不了、要用户到「应用」里自己上传：" + "、".join(f"「{u['label']}」（{u['kind']}）" for u in app["uploads"]))
        out.append("  结果：" + "、".join(app["results"]))
        out.append("")
    return "\n".join(out).rstrip()


def schema(app):
    """给前端画方案卡片用的：能改什么、结果怎么取。"""
    return {"mode": app["mode"], "fields": app["fields"], "images": app["images"],
            "uploads": app["uploads"], "outputs": app["outputs"], "results": app["results"], "titles": app["titles"]}


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
    if allow_index and len(s) >= 6:
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


def lookup_key(table, key):
    """字段键 → 目录里的条目。键应该是「节点 id:控件名」；模型偶尔只写节点 id（"70"），这时该节点在表里只有一个条目就认它。"""
    if key in table:
        return table[key]
    if isinstance(key, str) and key.isascii() and key.isdigit():
        hits = [v for k, v in table.items() if k.startswith(key + ":")]
        if len(hits) == 1:
            return hits[0]
    return None


def clean_selection(app, mode, fields, images, resolve_image):
    """校验并整理一次「模式 + 字段 + 图片」的选择；不认识的丢掉并给出提示。
    resolve_image(ref) → 图片文件名（相对 input 目录）或 None。返回 (selection, warnings)。"""
    warns = []
    sel = {"mode": None, "fields": {}, "images": {}}
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
        elif f["kind"] == "text":
            t = as_text(val).strip()
            if len(t) > MAX_TEXT:
                warns.append(f"「{f['label']}」太长，只用了前 {MAX_TEXT} 个字")
                t = t[:MAX_TEXT]
            if t:
                sel["fields"][f["key"]] = t
        else:
            v = pick_option(val, f["options"], False)
            if v is None:
                warns.append(f"「{f['label']}」的值「{val}」不在可选项里，已保持默认")
            else:
                sel["fields"][f["key"]] = v
    for key, ref in (images if isinstance(images, dict) else {}).items():
        im = lookup_key(app["images"], key)
        if im is None:
            warns.append(f"没有图片字段「{key}」，已忽略")
            continue
        name = resolve_image(ref)
        if name is None:
            warns.append(f"「{im['label']}」指定的图片不存在，已用默认图")
        else:
            sel["images"][im["key"]] = name
    return sel, warns
