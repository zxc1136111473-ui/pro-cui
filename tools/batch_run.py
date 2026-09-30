"""批量跑 ComfyUI 工作流：把 UI 模板转成 API 格式，按 CSV 每行改参数，逐行提交并保存结果。

用法：
  python3 tools/batch_run.py <模板.json> <表.csv> [--server http://127.0.0.1:8188] [--timeout 600]
CSV 表头写 `节点ID:控件名`，例如 `3:prompt`、`2:image`；值是本地图片路径时会先上传到 ComfyUI。
可选列 `out`：本行保存文件名前缀（默认 batch/行号），会覆盖模板里 SaveImage/SaveVideo/SaveAudio* 的 filename_prefix。
已完成的行记在 <表>.done，重跑自动跳过。
"""
import argparse, csv, json, os, random, time, urllib.request, uuid

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


def _req(server, path, data=None, ctype="application/json"):
    r = urllib.request.Request(server + path, data=data, headers={"Content-Type": ctype})
    return json.loads(urllib.request.urlopen(r, timeout=60).read())


def upload(server, path):
    b = uuid.uuid4().hex
    name = os.path.basename(path)
    body = (f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n').encode() + open(path, "rb").read() + \
           f'\r\n--{b}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n--{b}--\r\n'.encode()
    return _req(server, "/upload/image", body, f"multipart/form-data; boundary={b}")["name"]


def run_row(server, api, row, idx, timeout):
    api = json.loads(json.dumps(api))
    for col, v in row.items():
        if col == "out" or not v:
            continue
        nid, w = col.split(":", 1)
        api[nid]["inputs"][w] = upload(server, v) if os.path.isfile(v) else _cast(api[nid]["inputs"].get(w), v)
    prefix = row.get("out") or f"batch/{idx}"
    for n in api.values():
        if "filename_prefix" in n["inputs"]:
            n["inputs"]["filename_prefix"] = prefix
        for w in ("seed", "noise_seed"):
            if w in n["inputs"] and not isinstance(n["inputs"][w], list):
                n["inputs"][w] = random.randint(0, 2**31)
    pid = _req(server, "/prompt", json.dumps({"prompt": api}).encode())["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        h = _req(server, f"/history/{pid}").get(pid)
        if h and h["status"].get("completed") is not None:
            if h["status"]["status_str"] != "success":
                return False, str(h["status"]["messages"][-1])[:300]
            files = [f["filename"] for o in h["outputs"].values() for k in ("images", "video", "audio")
                     for f in o.get(k, []) if f.get("type") == "output"]
            return bool(files), ", ".join(files) or "成功但没有输出文件"
    return False, f"超时（>{timeout}s）"


def _cast(old, v):
    if isinstance(old, bool):
        return v.lower() in ("1", "true", "是")
    if isinstance(old, int):
        return int(float(v))
    if isinstance(old, float):
        return float(v)
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("template")
    ap.add_argument("csv")
    ap.add_argument("--server", default="http://127.0.0.1:8188")
    ap.add_argument("--timeout", type=int, default=600, help="单行等待上限（秒）")
    a = ap.parse_args()
    api = convert(json.load(open(a.template)), _req(a.server, "/object_info"))
    donef = a.csv + ".done"
    done = set(open(donef).read().split()) if os.path.exists(donef) else set()
    for i, row in enumerate(csv.DictReader(open(a.csv, encoding="utf-8-sig")), 1):
        if str(i) in done:
            continue
        ok, msg = run_row(a.server, api, row, i, a.timeout)
        print(f"第 {i} 行 {'✅' if ok else '❌'} {msg}", flush=True)
        if ok:
            open(donef, "a").write(f"{i}\n")


if __name__ == "__main__":
    main()
