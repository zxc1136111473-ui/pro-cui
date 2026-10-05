"""AI 助手：ComfyUI 左侧栏的聊天标签页。

说一句话（可以附商品图）→ 阿里文字模型按服务器上的「应用」目录（*.app.json）选应用、选模式、写好字段 → 你在方案卡片里确认 / 改 →
点「生成」，后端把工作流转成 API 格式、填好值、提交到 ComfyUI 队列，结果显示在对话里。不碰你的画布，也不需要打开那个工作流。

- 前端：web/pro-chat.js（侧栏标签页，聊天 / 附图 / 方案卡片 / 结果）。
- 后端：POST /pro/chat（对话，调阿里）、POST /pro/run（运行方案）；前端用 /api/pro/... 访问。
- Key：和各阿里工作流用同一个，读服务器 relay_config.json 的 node_settings["31"]（只发往 dashscope.aliyuncs.com，不会回给前端）。
- 目录、校验、对话、运行的逻辑在 catalog.py / chat.py / run.py（不依赖 ComfyUI，tests/test_chat.py 直接测）；这里只管接 ComfyUI 的环境。
"""
import asyncio
import base64
import io
import json
import os
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


# ── 环境：路径 / 端口 / Key / 节点定义（tests 里直接替换这几个函数）──────────────────────────────────────
def workflows_dir():
    import folder_paths
    return os.path.join(folder_paths.get_user_directory(), "default", "workflows")


def input_dir():
    import folder_paths
    return folder_paths.get_input_directory()


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


def _image_path(name):
    """聊天上传的图片 → 本地路径。名字要符合 safe_image_name 的规则，而且**真实路径**要在 input/助手/ 里（符号链接逃出去的拒绝）、是文件；
    不满足返回 None（防路径穿越）。"""
    name = cat_mod.safe_image_name(name)
    if not name:
        return None
    try:
        root = os.path.realpath(os.path.join(input_dir(), cat_mod.IMAGE_DIR))
        p = os.path.realpath(os.path.join(root, *name.split("/")[1:]))
    except (ValueError, OSError):
        return None
    return p if p.startswith(root + os.sep) and os.path.isfile(p) else None


def image_exists(name):
    return _image_path(name) is not None


def image_data_url(name):
    from PIL import Image
    im = Image.open(_image_path(name)).convert("RGB")
    im.thumbnail((VISION_SIDE, VISION_SIDE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ── 两个入口（路由和 tests 都调它们）──────────────────────────────────────────────────────────────────
def handle_chat(payload, post=None):
    if not isinstance(payload, dict):
        raise ChatError(400, "请求格式不对")
    cat = cat_mod.build_catalog(workflows_dir(), object_info())
    names = [n for n in (payload.get("images") or []) if isinstance(n, str)][:chat.MAX_IMAGES]
    new = [i for i in (payload.get("new_images") or []) if isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= len(names)]
    vision, broken = [], []
    for i in new:
        if len(vision) >= MAX_VISION_IMAGES:
            break
        n = names[i - 1]
        if not image_exists(n):
            continue
        try:
            vision.append(image_data_url(n))
        except Exception:                                    # 不是图片 / 打不开（HEIC、SVG、被改坏的文件）：跳过它，别让整轮对话 500
            broken.append(n)
    usable = [n for n in names if image_exists(n) and n not in broken]    # 编号要和前端一致：文件没了 / 打不开的也占一个编号，只是不让模型用
    out = chat.chat_turn(payload, cat, ali_key(), ALI_BASE, names, vision, usable=usable, post=post)
    out["warnings"] = out["warnings"] + [f"图片「{n.split('/', 1)[-1]}」打不开，已忽略（换 PNG / JPG 再传）" for n in broken]
    return out


def handle_run(payload, post=None):
    if not isinstance(payload, dict):
        raise ChatError(400, "请求格式不对")
    cat = cat_mod.build_catalog(workflows_dir(), object_info())
    app = cat.get(str(payload.get("workflow", "")).strip())
    if app is None:
        raise ChatError(404, f"没有编号为「{payload.get('workflow')}」的应用")
    with open(os.path.join(workflows_dir(), app["path"]), encoding="utf-8") as f:
        wf = json.load(f)
    api, sel, warns = run.prepare_run(app, wf, object_info(), payload, image_exists)
    resp = run.submit_prompt(api, str(payload.get("client_id") or ""), comfy_port(), app["titles"], post=post)
    partial = run.describe_error({"node_errors": resp.get("node_errors")}, app["titles"])
    if partial:                                              # ComfyUI 部分校验失败时仍返回 200：有的输出分支不会运行，要告诉用户为什么少了结果
        warns = warns + ["有些步骤没通过校验，不会运行：" + partial]
    return {"prompt_id": resp.get("prompt_id"), "name": app["name"], "outputs": app["outputs"], "titles": app["titles"],
            "selection": sel, "warnings": warns}


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

    async def _call(request, fn):
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


_register_routes()
