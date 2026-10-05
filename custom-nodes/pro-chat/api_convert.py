"""工作流 UI 格式（JSON）→ ComfyUI API 格式（{节点 ID: {class_type, inputs}}）。

tools/batch_run.py（按 CSV 批量跑）和 pro-chat（AI 助手的后端）共用这一份，只用标准库。
oi = ComfyUI 的 /object_info（节点定义）：用它把 widgets_values 按顺序对回控件名。
"""

SKIP = {"Note", "MarkdownNote", "Reroute"}
PRIM = {"STRING", "INT", "FLOAT", "BOOLEAN", "COMBO"}
UPLOAD = {"LoadImage": 1, "LoadVideo": 1, "LoadAudio": 2}  # 上传控件占的额外 widgets_values 个数


def _is_widget(spec):
    t = spec[0]
    if len(spec) > 1 and spec[1].get("forceInput"):
        return False
    return isinstance(t, list) or t in PRIM or t == "COMFY_DYNAMICCOMBO_V3"


def _dyn(name, spec, vals, out, top):
    """动态下拉：先取自身的值，再按所选项展开子控件（名字 父.子；顶层同名控件也写一份）。"""
    v = vals.pop(0)
    out[name] = v
    for opt in spec[1]["options"]:
        if opt["key"] != v:
            continue
        for sect in ("required", "optional"):
            for sub, sspec in opt["inputs"].get(sect, {}).items():
                if sspec[0] == "COMFY_DYNAMICCOMBO_V3":
                    _dyn(f"{name}.{sub}", sspec, vals, out, top)
                    if sub in top:
                        out[sub] = out[f"{name}.{sub}"]
                elif vals:
                    out[f"{name}.{sub}"] = vals.pop(0)
                    if sub in top:
                        out[sub] = out[f"{name}.{sub}"]


def convert(wf, oi):
    """UI 格式 -> API 格式（{节点ID: {class_type, inputs}}）。"""
    src = {l[0]: (l[1], l[2]) for l in wf["links"]}
    api = {}
    for n in wf["nodes"]:
        if n["type"] in SKIP or n.get("mode", 0) in (2, 4):
            continue
        spec = oi[n["type"]]["input"]
        order = oi[n["type"]].get("input_order", {})
        names = [k for s in ("required", "optional") for k in order.get(s, spec.get(s, {}))]
        allspec = {**spec.get("required", {}), **spec.get("optional", {})}
        vals = list(n.get("widgets_values") or [])
        inputs = {}
        skip = set()
        for k in names:
            s = allspec[k]
            if k in skip or not _is_widget(s):
                continue
            if s[0] == "COMFY_DYNAMICCOMBO_V3":
                skip.update(k2 for k2 in names if k2 != k and k2 in _subnames(s))
                _dyn(k, s, vals, inputs, allspec)
                continue
            if not vals:
                break
            inputs[k] = vals.pop(0)
            opts = s[1] if len(s) > 1 else {}
            if opts.get("control_after_generate") or k in ("seed", "noise_seed"):
                vals.pop(0) if vals else None
            if opts.get("image_upload") or opts.get("video_upload") or opts.get("audio_upload"):
                del vals[:UPLOAD.get(n["type"], 1)]
        for i in n.get("inputs", []):
            if i.get("link") is not None:
                a, b = src[i["link"]]
                inputs[i["name"]] = [str(a), b]
        api[str(n["id"])] = {"class_type": n["type"], "inputs": inputs}
    return api


def _subnames(spec):
    out = set()
    for opt in spec[1]["options"]:
        for sect in opt["inputs"].values():
            out.update(sect)
    return out
