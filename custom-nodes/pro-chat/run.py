"""运行一个方案：校验 → 工作流转 API 格式 → 填值 → 提交到 ComfyUI 队列。

运行在后端做，不碰用户的画布 / 标签页；做法和 tools/batch_run.py 一样（转换用同一份 api_convert.py）。
前端传来的 mode / fields / images 一律用目录重新校验：目录之外的控件改不了，图片只能是 input/助手/ 里真实存在的文件。
"""
import random

from . import catalog as cat_mod
from .api_convert import convert
from .chat import ChatError


def prepare_run(app, wf, oi, payload, image_exists):
    """校验 payload（{mode, fields, images}）并生成 API prompt。返回 (api_prompt, selection, warnings)。"""
    def resolve(ref):
        name = cat_mod.safe_image_name(ref)
        return name if name and image_exists(name) else None

    sel, warns = cat_mod.clean_selection(app, payload.get("mode"), payload.get("fields"), payload.get("images"), resolve)
    api = convert(wf, oi)
    values = dict(sel["fields"], **sel["images"])
    if sel["mode"] is not None:
        values[app["mode"]["key"]] = sel["mode"]
    for key, val in values.items():
        nid, widget = key.split(":", 1)
        if nid not in api:
            raise ChatError(500, f"工作流里找不到节点 {nid}（工作流和目录对不上，重新部署一下）")
        api[nid]["inputs"][widget] = val
    for n in api.values():                     # 每次运行换种子：写提示词要重新扩写、生成要出新图（ComfyUI 输入没变就直接用缓存）
        for w in ("seed", "noise_seed"):
            if w in n["inputs"] and not isinstance(n["inputs"][w], list):
                n["inputs"][w] = random.randint(0, 2 ** 31 - 2)          # 个别节点（阿里生图）种子上限是 2147483646
    return api, sel, warns


def describe_error(d, titles):
    """ComfyUI /prompt 的报错 JSON → 人话。"""
    if not isinstance(d, dict):
        return ""
    parts = []
    err = d.get("error")
    if isinstance(err, dict):
        parts.append(str(err.get("message") or err.get("type") or "").strip())
        if err.get("details"):
            parts.append(str(err["details"]).strip())
    elif err:
        parts.append(str(err))
    for nid, ne in (d.get("node_errors") or {}).items():
        for e in (ne.get("errors") or [])[:2]:
            parts.append(f"「{titles.get(str(nid), ne.get('class_type', nid))}」：{e.get('message', '')} {e.get('details', '')}".strip())
    return "；".join(p for p in parts if p)[:600]


def submit_prompt(api, client_id, port, titles, post=None):
    """POST 到本机 ComfyUI 的 /prompt。返回 {prompt_id, number, node_errors}；被拒绝（校验没过）抛 ChatError。"""
    import requests
    post = post or requests.post
    try:
        r = post(f"http://127.0.0.1:{port}/prompt", json={"prompt": api, "client_id": client_id or "pro-chat"}, timeout=30)
    except requests.RequestException as e:
        raise ChatError(502, f"提交给 ComfyUI 失败（{type(e).__name__}）")
    try:
        d = r.json()
    except Exception:
        d = {}
    if r.status_code != 200:
        raise ChatError(400, describe_error(d, titles) or f"ComfyUI 拒绝了这次运行（HTTP {r.status_code}）")
    return d
