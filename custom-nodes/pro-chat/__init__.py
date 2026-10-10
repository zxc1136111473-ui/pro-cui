"""AI 助手 / 工作台：ComfyUI 里的聊天 + 多步方案 + 素材库。

说一句话（可以附商品图、视频、音频）→ 阿里文字模型按服务器上的「应用」目录（*.app.json）排出一步或几步（出图 → 出视频 → 配音 → 成片……），
选好每步的模式、写好字段、指定用哪些素材 → 你在工作台里确认 / 改 → 点「运行」，后端把每一步的工作流转成 API 格式、填好值、提交到 ComfyUI 队列；
结果出来后放进素材库，下一步直接拿来用。不碰你的画布，也不需要打开那些工作流。

- 前端：web/pro-chat.js（工作台：聊天 / 方案卡片 / 结果板 / 素材库）。
- 后端：POST /pro/chat（对话，调阿里）、POST /pro/run（运行一步）、POST /pro/stage（把输出目录里的结果放进素材库）、GET /pro/catalog（应用目录）；前端用 /api/pro/... 访问。
- Key：和各阿里工作流用同一个，读服务器 relay_config.json 的 node_settings["31"]（只发往 dashscope.aliyuncs.com，不会回给前端）。
- 目录、校验、对话、运行的逻辑在 catalog.py / chat.py / run.py（不依赖 ComfyUI，tests/test_chat.py 直接测）；这里只管接 ComfyUI 的环境。
"""
import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import time

from . import catalog as cat_mod
from . import chat, run
from .chat import ChatError

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

ALI_BASE = "https://dashscope.aliyuncs.com"
KEY_NODE_ID = "31"                      # 工作流里「Key：阿里」节点的 id（Key 按节点 id 存）
OBJECT_INFO_TTL = 30
VISION_SIDE = 768                       # 给模型看的图先缩到最长边 768（大图传阿里很慢）
MAX_VISION_IMAGES = 4
STAGE_MAX_BYTES = 300 * 1024 * 1024      # 一个结果文件放进素材库的大小上限
RUN_ID_TTL = 6 * 3600                    # 同一个 run_id 在这么久之内再来，直接返回上次提交的结果，不重复提交
RUN_ID_KEEP = 300


# ── 环境：路径 / 端口 / Key / 节点定义（tests 里直接替换这几个函数）──────────────────────────────────────
def workflows_dir():
    import folder_paths
    return os.path.join(folder_paths.get_user_directory(), "default", "workflows")


def input_dir():
    import folder_paths
    return folder_paths.get_input_directory()


def output_dir():
    import folder_paths
    return folder_paths.get_output_directory()


def models_dir():
    import folder_paths
    return folder_paths.models_dir


def comfy_port():
    try:
        from comfy.cli_args import args
        return int(args.port)
    except Exception:
        return 8188


def relay_config_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ComfyUI-relayapi", "relay_config.json")


def ali_key():
    try:
        with open(relay_config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    key = str(((cfg.get("node_settings") or {}).get(KEY_NODE_ID) or {}).get("api_key") or "").strip()
    if not key:
        raise ChatError(503, "还没有阿里 Key：在任一工作流的节点图里展开「Key：阿里」节点，填一次 apikey（或在服务器的 relay_config.json 里预置），助手才能用")
    return key


_OI = {"t": 0.0, "v": None}


def object_info():
    """本机 ComfyUI 的 /object_info（节点定义），缓存 30 秒。"""
    if _OI["v"] is None or time.time() - _OI["t"] > OBJECT_INFO_TTL:
        import requests
        try:
            r = requests.get(f"http://127.0.0.1:{comfy_port()}/object_info", timeout=30)
            r.raise_for_status()
            _OI["v"], _OI["t"] = r.json(), time.time()
        except Exception as e:
            raise ChatError(502, f"读取 ComfyUI 的节点定义失败（{type(e).__name__}）")
    return _OI["v"]


def _asset_path(name):
    """素材库里的文件 → 本地路径。名字要符合 safe_asset_name 的规则，而且**真实路径**要在 input/助手/ 里（符号链接逃出去的拒绝）、是文件；
    不满足返回 None（防路径穿越）。"""
    name = cat_mod.safe_asset_name(name)
    if not name:
        return None
    try:
        root = os.path.realpath(os.path.join(input_dir(), cat_mod.IMAGE_DIR))
        p = os.path.realpath(os.path.join(root, *name.split("/")[1:]))
    except (ValueError, OSError):
        return None
    return p if p.startswith(root + os.sep) and os.path.isfile(p) else None


def asset_exists(name):
    return _asset_path(name) is not None


def image_data_url(name):
    from PIL import Image
    im = Image.open(_asset_path(name)).convert("RGB")
    im.thumbnail((VISION_SIDE, VISION_SIDE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ── 入口（路由和 tests 都调它们）──────────────────────────────────────────────────────────────────────
def handle_chat(payload, post=None):
    if not isinstance(payload, dict):
        raise ChatError(400, "请求格式不对")
    cat = cat_mod.build_catalog(workflows_dir(), object_info(), models_dir())
    assets = chat.normalize_assets(payload.get("assets"))
    new = []                                                 # 这一轮新附的素材编号（客户端传来的，什么类型都可能有：只收在范围内的整数，去重）
    for i in payload.get("new_assets") if isinstance(payload.get("new_assets"), list) else []:
        if isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= len(assets) and i not in new:
            new.append(i)
    vision, broken = [], []
    for i in new:                                            # 这一轮新附的图片给模型看（视频 / 音频不看）
        a = assets[i - 1]
        if a["kind"] != "image" or not asset_exists(a["name"]):
            continue
        if len(vision) >= MAX_VISION_IMAGES:
            break
        try:
            vision.append((i, image_data_url(a["name"])))
        except Exception:                                    # 不是图片 / 打不开（HEIC、SVG、被改坏的文件）：跳过它，别让整轮对话 500
            broken.append(a["name"])
    for a in assets:                                         # 编号要和前端一致：文件没了 / 打不开的也占一个编号，只是不让模型用
        a["usable"] = asset_exists(a["name"]) and a["name"] not in broken
    out = chat.chat_turn(payload, cat, ali_key(), ALI_BASE, assets, vision, post=post)
    out["warnings"] = out["warnings"] + [f"图片「{n.split('/', 1)[-1]}」打不开，已忽略（换 PNG / JPG 再传）" for n in broken]
    return out


_RUN_LOCK = threading.Lock()
_RUNS = {}                                # run_id → (时间, 返回给前端的结果)
_RUN_LOCKS = {}                           # run_id → 锁：同一个 run_id 的请求排队，第二个等第一个提交完再看结果


def handle_run(payload, post=None):
    """运行一步。前端每次运行带一个 run_id（写进它的存档里）：提交到一半网页被刷新 / 点了两次 / 两个标签页同时来，同一个 run_id 只会向 ComfyUI 提交一次，
    之后的请求拿到的是第一次的结果（repeated=True）。视频等有额度的步骤不能因为这种意外重复花钱。服务重启后这个记录就没了。"""
    if not isinstance(payload, dict):
        raise ChatError(400, "请求格式不对")
    rid = payload.get("run_id") if isinstance(payload.get("run_id"), str) else ""
    rid = rid if 0 < len(rid) <= 80 else ""
    if not rid:
        return _run_once(payload, post)
    with _RUN_LOCK:
        lock = _RUN_LOCKS.setdefault(rid, threading.Lock())
    same = json.dumps([payload.get(k) for k in ("workflow", "mode", "fields", "files")], sort_keys=True, ensure_ascii=False, default=str)   # 同一个 run_id 但内容不一样，不能当成同一次
    with lock:
        with _RUN_LOCK:
            hit = _RUNS.get(rid)
        if hit and hit[2] == same and time.time() - hit[0] < RUN_ID_TTL:
            return dict(hit[1], repeated=True)
        out = _run_once(payload, post)
        with _RUN_LOCK:
            _RUNS[rid] = (time.time(), out, same)
            for old in sorted(_RUNS, key=lambda k: _RUNS[k][0])[:-RUN_ID_KEEP]:
                _RUNS.pop(old, None)
                _RUN_LOCKS.pop(old, None)
            if len(_RUN_LOCKS) > RUN_ID_KEEP * 2:                      # 提交失败的 run_id 不进 _RUNS，它们的锁也别一直留着
                for k in [k for k, lk in _RUN_LOCKS.items() if k not in _RUNS and not lk.locked()]:
                    del _RUN_LOCKS[k]
        return out


def _run_once(payload, post=None):
    cat = cat_mod.build_catalog(workflows_dir(), object_info(), models_dir())
    app = cat.get(str(payload.get("workflow", "")).strip())
    if app is None:
        raise ChatError(404, f"没有编号为「{payload.get('workflow')}」的应用")
    with open(os.path.join(workflows_dir(), app["path"]), encoding="utf-8") as f:
        wf = json.load(f)
    api, sel, warns = run.prepare_run(app, wf, object_info(), payload, asset_exists)
    resp = run.submit_prompt(api, str(payload.get("client_id") or ""), comfy_port(), app["titles"], post=post)
    partial = run.describe_error({"node_errors": resp.get("node_errors")}, app["titles"])
    if partial:                                              # ComfyUI 部分校验失败时仍返回 200：有的输出分支不会运行，要告诉用户为什么少了结果
        warns = warns + ["有些步骤没通过校验，不会运行：" + partial]
    return {"prompt_id": resp.get("prompt_id"), "name": app["name"], "outputs": app["outputs"], "titles": app["titles"],
            "selection": sel, "warnings": warns}


def handle_stage(payload):
    """把 ComfyUI 输出目录里的一个结果文件放进素材库（input/助手/，用内容哈希命名，同一个文件只存一份）：之后它就能当作后面步骤的输入。
    只认输出目录里、真实路径没跑出去的、图片 / 视频 / 音频扩展名的文件。返回 {name, kind}。"""
    if not isinstance(payload, dict):
        raise ChatError(400, "请求格式不对")
    filename, subfolder = str(payload.get("filename") or ""), str(payload.get("subfolder") or "")
    if str(payload.get("type") or "output") != "output":
        raise ChatError(400, "只能取输出目录里的结果")
    kind = cat_mod.file_kind(filename)
    if kind is None:
        raise ChatError(400, "不是图片 / 视频 / 音频文件")
    try:
        root = os.path.realpath(output_dir())
        src = os.path.realpath(os.path.join(root, subfolder, filename))
    except (ValueError, OSError):
        raise ChatError(400, "文件路径不对")
    if not src.startswith(root + os.sep) or not os.path.isfile(src):
        raise ChatError(404, "找不到这个结果文件")
    if os.path.getsize(src) > STAGE_MAX_BYTES:
        raise ChatError(413, f"文件太大（超过 {STAGE_MAX_BYTES >> 20} MB），不能放进素材库")
    h = hashlib.sha1()
    with open(src, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    base = h.hexdigest()[:16] + os.path.splitext(filename)[1].lower()
    folder = os.path.join(input_dir(), cat_mod.IMAGE_DIR)
    dst = os.path.join(folder, base)
    if not os.path.isfile(dst):
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".part")      # 先写临时文件再改名：同时来两个请求也不会写出半个文件
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return {"name": f"{cat_mod.IMAGE_DIR}/{base}", "kind": kind}


def handle_catalog(_payload=None):
    """应用目录（给工作台手动加步骤用）：每个应用的编号、名字、用途、能改什么。"""
    cat = cat_mod.build_catalog(workflows_dir(), object_info(), models_dir())
    return {"apps": [{"workflow": a["id"], "name": a["name"], "path": a["path"], "desc": a["desc"], "schema": cat_mod.schema(a)} for a in cat.values()]}


# ── 路由（ComfyUI 里才注册；单独 import 这个文件做测试时跳过）──────────────────────────────────────
def _register_routes():
    try:
        from aiohttp import web
        from server import PromptServer
    except Exception:
        return
    if getattr(PromptServer, "instance", None) is None:
        return
    routes = PromptServer.instance.routes

    async def _call(request, fn, body=True):
        payload = None
        if body:
            try:
                payload = await request.json()
            except Exception:
                return web.json_response({"error": "请求不是 JSON"}, status=400)
        loop = asyncio.get_running_loop()
        try:
            return web.json_response(await loop.run_in_executor(None, lambda: fn(payload)))
        except ChatError as e:
            return web.json_response({"error": str(e)}, status=e.status)
        except Exception as e:                               # 不把内部细节原样给前端，日志里留完整的
            import logging
            logging.exception("[pro-chat] 处理请求出错")
            return web.json_response({"error": f"助手出错了（{type(e).__name__}），详情看服务器日志"}, status=500)

    @routes.post("/pro/chat")
    async def pro_chat(request):
        return await _call(request, handle_chat)

    @routes.post("/pro/run")
    async def pro_run(request):
        return await _call(request, handle_run)

    @routes.post("/pro/stage")
    async def pro_stage(request):
        return await _call(request, handle_stage)

    @routes.get("/pro/catalog")
    async def pro_catalog(request):
        return await _call(request, handle_catalog, body=False)


_register_routes()
