"""批量跑 ComfyUI 工作流：把 UI 模板转成 API 格式，按 CSV 每行改参数，逐行提交并保存结果。

用法：
  python3 tools/batch_run.py <模板.json> <表.csv> [--server http://127.0.0.1:8188] [--timeout 600]
CSV 表头写 `节点ID:控件名`，例如 `3:prompt`、`2:image`；值是本地图片路径时会先上传到 ComfyUI。
可选列 `out`：本行保存文件名前缀（默认 batch/行号），会覆盖模板里 SaveImage/SaveVideo/SaveAudio* 的 filename_prefix。
已完成的行记在 <表>.done，重跑自动跳过。
"""
import argparse, csv, importlib.util, json, os, random, time, urllib.request, uuid

# 工作流 → API 格式的转换和 pro-chat（AI 助手）共用同一份：custom-nodes/pro-chat/api_convert.py
_spec = importlib.util.spec_from_file_location("pro_chat_api_convert", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom-nodes", "pro-chat", "api_convert.py"))
_conv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conv)
convert = _conv.convert


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
