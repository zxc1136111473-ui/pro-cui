"""工作流 UI 格式（JSON）→ ComfyUI API 格式（{节点 ID: {class_type, inputs}}）。

tools/batch_run.py（按 CSV 批量跑）和 pro-chat（AI 助手的后端）共用这一份，只用标准库。
oi = ComfyUI 的 /object_info（节点定义）：用它把 widgets_values 按顺序对回控件名。
"""

import copy
import re

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


# ── 合并应用（一个工作流文件里好几个功能，tools/gen_workflows.py 生成）──────────────────────────────────────────
LABEL_PREFIX = re.compile(r"^【[^】]*】")


def family_of(wf):
    """合并应用的 extra.appFamily（{name, func_node, funcs: [{id, name, index, base, shared, extra}]}）；普通的单功能工作流返回 None。"""
    fam = (wf.get("extra") or {}).get("appFamily") if isinstance(wf, dict) else None
    return fam if isinstance(fam, dict) and isinstance(fam.get("funcs"), list) and fam["funcs"] else None


def family_view(wf, index):
    """合并应用里第 index 个功能 → 一个单功能工作流：这个功能的节点编号还原成 1~99（共用的 Key 节点编号本来就没变）、功能开关（ProBlock）去掉、
    控件名去掉「【功能名】」前缀、extra 换成这个功能自己的设置。工作台（pro-chat）和批量跑（tools/batch_run.py --func）用它：
    跑的就是合并之前的那个单功能工作流，没有功能开关，也不会带上别的功能的节点。"""
    f = family_of(wf)["funcs"][index]
    base, shared = int(f["base"]), set(f.get("shared") or [])
    nodes = {n["id"]: n for n in wf["nodes"]}
    links = {l[0]: l for l in wf["links"]}
    mine = {i for i in nodes if i in shared or base < i < base + 1000}
    gates = {i for i in mine if nodes[i]["type"] == "ProBlock"}
    back = lambda i: i if i in shared else i - base

    def source(link_id):                                     # 跳过功能开关，找到真正的来源
        l = links[link_id]
        while l[1] in gates:
            l = links[next(i for i in nodes[l[1]]["inputs"] if i["name"] == "value")["link"]]
        return l

    out = []
    for n in wf["nodes"]:
        if n["id"] in mine and n["id"] not in gates:
            c = copy.deepcopy(n)
            c["id"] = back(n["id"])
            for o in c["outputs"]:
                o["links"] = None
            for i in c["inputs"]:
                if i.get("label"):
                    i["label"] = LABEL_PREFIX.sub("", i["label"], count=1)
            if str(c["id"]) in (f.get("keys") or {}) and n["type"] == "RelayAPISettings":     # 共用的 Key 节点：还原成这个功能自己的设置（合并后留下的是几个功能里最能干的那一份）
                c["widgets_values"] = copy.deepcopy(f["keys"][str(c["id"])])
            out.append(c)
    byid = {c["id"]: c for c in out}
    new_links = []
    for c in out:
        for slot, i in enumerate(c["inputs"]):
            if i.get("link") is None:
                continue
            l = source(i["link"])
            new_links.append([len(new_links) + 1, back(l[1]), l[2], c["id"], slot, l[5]])
            i["link"] = new_links[-1][0]
            o = byid[back(l[1])]["outputs"][l[2]]
            o["links"] = (o["links"] or []) + [new_links[-1][0]]
    return {"last_node_id": max(byid), "last_link_id": len(new_links), "nodes": out, "links": new_links, "groups": [], "config": {},
            "extra": copy.deepcopy(f["extra"]), "version": 0.4}
