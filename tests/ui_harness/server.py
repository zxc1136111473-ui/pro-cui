"""AI 工作台的「假 ComfyUI」：不需要真的 ComfyUI、阿里和 Veo，就能在真浏览器里把工作台从头跑到尾。

- 页面：index.html（假画布）+ scripts_app.js / scripts_api.js（假的 ComfyUI 页面对象）+ 真的 web/pro-chat*.js（/extensions/pro-chat/ 下）。
- 后端：用真的 pro-chat 后端代码（catalog / chat / run / handle_*），环境换成临时目录；
  阿里换成「按关键词出方案」的假回复（见 FakeLLM），ComfyUI 的 /prompt 换成假队列：过一会儿按工作流里的输出节点在 output/ 里生成真的 PNG / MP4 / MP3 / TXT，
  并记进 /history，所以「运行 → 结果 → 放进素材库 → 下一步用它」整条链路都是真实的，只有生成内容是假的。
- 每次运行的实际输入（节点 id → 输入值）记在 runs 里，测试用它核对「第二步拿到的确实是第一步的结果」。

用法：python3 tests/ui_harness/server.py [端口]   （直接运行时在浏览器里打开 http://127.0.0.1:端口/）
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import _load  # noqa: E402

WEB = os.environ.get("PC_WEB_DIR") or os.path.join(ROOT, "custom-nodes", "pro-chat", "web")      # PC_WEB_DIR：换一份前端（做变异检查时用改坏的副本，不动仓库里的文件）
WF_DIR = os.path.join(ROOT, "workflows")
OI = json.load(open(os.path.join(ROOT, "tests", "object_info_subset.json"), encoding="utf-8"))


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d = status, data
        self.text = json.dumps(data if data is not None else {}, ensure_ascii=False)

    def json(self):
        return self._d


class FakeLLM:
    """按用户这句话里的关键词给出固定的方案（不是真的模型）。"""

    def __init__(self):
        self.calls = []
        self.delay = 0                                         # 回复前先等几秒（测「等 AI 回复时方案被锁住」）

    def __call__(self, url, headers=None, json=None, timeout=None):      # noqa: A002
        self.calls.append(json)
        if self.delay:
            time.sleep(self.delay)
        user = [m for m in json["messages"] if m["role"] == "user"][-1]["content"]
        text = user if isinstance(user, str) else " ".join(c.get("text", "") for c in user)
        if "让它出错" in text:                                  # 模拟阿里返回错误（比如 Key 不对）
            return Resp(401, {"error": {"code": "invalid_api_key", "message": "Incorrect API key provided: sk-hari****ss"}})
        return Resp(200, {"choices": [{"message": {"content": __import__("json").dumps(self.reply(text), ensure_ascii=False)}}]})

    @staticmethod
    def reply(text):
        if "做一套" in text:                                  # 套图（4 张）→ 用第 2 张出视频
            return {"reply": "好的：先出 4 个尺寸的套图，再用第 2 张做 8 秒竖屏视频（视频会占 Veo 额度）。", "plan": {"steps": [
                {"workflow": "04", "fields": {"70:idea": "红苹果，干净明亮的浅色背景"}, "files": {"2:image": 1}, "note": "先出套图"},
                {"workflow": "12", "mode": 2, "fields": {"70:idea": "镜头缓慢推近，苹果轻轻转动", "6:value": "脆甜多汁，欢迎选购", "12:ratio": "9:16", "3:speed": 0.9}, "files": {"2:image": "步骤1.2"}, "note": "用套图的第 2 张做视频"}],
                "note": "套图 → 视频"}}
        if "第三张" in text:                                  # 改成用第 3 张：第 1 步原样，第 2 步换图
            return {"reply": "改成用第 3 张图了。", "plan": {"steps": [
                {"workflow": "04", "fields": {"70:idea": "红苹果，干净明亮的浅色背景"}, "files": {"2:image": 1}, "note": "先出套图"},
                {"workflow": "12", "mode": 2, "fields": {"70:idea": "镜头缓慢推近，苹果轻轻转动", "6:value": "脆甜多汁，欢迎选购", "12:ratio": "9:16", "3:speed": 0.9}, "files": {"2:image": "步骤1.3"}, "note": "用套图的第 3 张做视频"}],
                "note": "套图 → 视频"}}
        if "前面加一步" in text:                              # 在最前面加了文案；套图和视频原样，视频改成引用「步骤2.2」（序号跟着挪）
            return {"reply": "在最前面加了一步文案。", "plan": {"steps": [
                {"workflow": "07", "mode": 1, "fields": {"70:idea": "红苹果，脆甜多汁"}, "note": "先写文案"},
                {"workflow": "04", "fields": {"70:idea": "红苹果，干净明亮的浅色背景"}, "files": {"2:image": 1}, "note": "先出套图"},
                {"workflow": "12", "mode": 2, "fields": {"70:idea": "镜头缓慢推近，苹果轻轻转动", "6:value": "脆甜多汁，欢迎选购", "12:ratio": "9:16", "3:speed": 0.9}, "files": {"2:image": "步骤2.2"}, "note": "用套图的第 2 张做视频"}],
                "note": "文案 + 套图 → 视频"}}
        if "接成片" in text:                                  # 视频 → 成片：后一步的「上传视频」用前一步出的视频
            return {"reply": "先出视频，再接成片。", "plan": {"steps": [
                {"workflow": "12", "mode": 1, "fields": {"70:idea": "苹果缓缓旋转"}, "files": {"2:image": 1}, "note": "出视频"},
                {"workflow": "11", "mode": 1, "fields": {"6:value": "脆甜多汁，欢迎选购"}, "files": {"2:file": "步骤1"}, "note": "配音 + 成片"}]}}
        if "图层" in text:                                    # 抠图 → 放到纯色背景上（商品用素材库里的图）；要分层 PNG
            return {"reply": "先把商品抠出来，再放到浅色背景上，同时存分层 PNG。", "plan": {"steps": [
                {"workflow": "15", "files": {"2:image": 1}, "note": "抠出商品"},
                {"workflow": "16", "mode": 2, "fields": {"5:bg_color": "#F5F0E6", "5:position": "下中", "5:scale_pct": 55, "5:save_layers": True}, "files": {"2:image": "步骤1"}, "note": "放到米色背景上"}]}}
        if "对白成片" in text:                                # 先出视频（只出视频），再把两个人的对白配上去（音色只写名字：目录里的写法是「名字（说明）」，模型常常照抄名字）
            return {"reply": "先用图出一段视频，再把两个人的对白配上去。", "plan": {"steps": [
                {"workflow": "12", "mode": 1, "fields": {"70:idea": "苹果缓缓旋转"}, "files": {"2:image": 1}, "note": "出视频"},
                {"workflow": "14", "mode": 1, "fields": {"3:script": "小美：好红的苹果！\n阿强：脆甜多汁，快来尝尝。", "3:role1_name": "小美", "3:role1_voice": "Cherry", "3:role2_name": "阿强", "3:role2_voice": "Ethan"},
                 "files": {"2:file": "步骤1"}, "note": "把对白配到视频上"}]}}
        if "对白" in text:
            return {"reply": "写了一段两个人的对白，小美用女声、阿强用男声。", "plan": {"steps": [
                {"workflow": "13", "fields": {"3:script": "小美：老板，这个苹果怎么卖？\n阿强：九块九一斤，脆甜多汁。\n小美：来五斤！", "3:role1_name": "小美", "3:role1_voice": "Serena · 女 · 温柔",
                                               "3:role2_name": "阿强", "3:role2_voice": "Ryan · 男 · 节奏感强、戏感足"}, "note": "两个人的对白配音"}]}}
        if "文案" in text:
            return {"reply": "用文案应用写。", "plan": {"steps": [{"workflow": "07", "mode": 1, "fields": {"70:idea": "红苹果，脆甜多汁"}, "note": "写文案"}]}}
        if "配音" in text:
            return {"reply": "先写文案再配音。", "plan": {"steps": [
                {"workflow": "08", "mode": 1, "fields": {"3:text": "夏日清凉，限时三天，欢迎选购。", "3:speed": 0.85}, "note": "配音"}]}}
        return {"reply": "我能帮你出图、出视频、配音、写文案。", "plan": None}


class FakeComfy:
    """假队列：/prompt 收到工作流后过 delay 秒，按输出节点在 output/ 生成真文件，记进 history。"""

    def __init__(self, out_dir, delay=1.2):
        self.out_dir, self.delay = out_dir, delay
        self.history, self.running, self.runs, self.interrupted = {}, set(), [], set()
        self.deleted = []                                      # /queue 里被删掉的（用户点了「停止」）
        self.counter = 0

    def __call__(self, url, json=None, timeout=None):                       # noqa: A002  ← 假的 requests.post（/prompt）
        pid = uuid.uuid4().hex[:12]
        self.runs.append({"prompt_id": pid, "prompt": json["prompt"], "client_id": json.get("client_id")})
        self.running.add(pid)
        threading.Thread(target=self._finish, args=(pid, json["prompt"]), daemon=True).start()
        return Resp(200, {"prompt_id": pid, "number": len(self.runs), "node_errors": {}})

    def _finish(self, pid, prompt):
        t_end = time.time() + self.delay
        while time.time() < t_end and pid not in self.interrupted:
            time.sleep(0.05)
        if pid in self.interrupted:
            self.history[pid] = {"status": {"status_str": "error", "completed": False, "messages": [["execution_interrupted", {"node_id": "1"}]]}, "outputs": {}}
            self.running.discard(pid)
            return
        outs = {}
        for nid, node in prompt.items():
            ct, inp = node["class_type"], node["inputs"]
            self.counter += 1
            n = self.counter
            if ct in ("SaveImage", "ProSaveCutout", "ProLayerCompose"):
                from PIL import Image, ImageDraw
                os.makedirs(os.path.join(self.out_dir, "pro"), exist_ok=True)
                name = f"fake_{n:05d}_.png"
                if ct == "ProSaveCutout":                              # 抠图：透明底，中间一个红圆
                    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
                    ImageDraw.Draw(im).ellipse((40, 40, 216, 216), fill=(200, 40, 40, 255))
                else:
                    im = Image.new("RGB", (512, 512), [(220, 60, 60), (60, 160, 90), (70, 110, 220), (230, 170, 40)][n % 4])
                    ImageDraw.Draw(im).text((20, 20), f"{ct} #{nid} n={n}", fill=(255, 255, 255))
                im.save(os.path.join(self.out_dir, "pro", name))
                files = [{"filename": name, "subfolder": "pro", "type": "output"}]
                if ct == "ProLayerCompose" and inp.get("save_layers"):  # 分层 PNG：合成图排最前，再是各层
                    for suffix in ("背景层", "商品层", "阴影层"):
                        layer = f"fake_{n:05d}_{suffix}.png"
                        Image.new("RGBA", (512, 512), (0, 0, 0, 0)).save(os.path.join(self.out_dir, "pro", layer))
                        files.append({"filename": layer, "subfolder": "pro", "type": "output"})
                outs[nid] = {"images": files}
            elif ct in ("SaveVideo", "ProVideoDub"):
                os.makedirs(os.path.join(self.out_dir, "video"), exist_ok=True)
                name = f"成片_{n:05d}_.mp4"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x3366aa:s=270x480:d=1:r=10", "-pix_fmt", "yuv420p", os.path.join(self.out_dir, "video", name)], check=True)
                outs[nid] = {"images": [{"filename": name, "subfolder": "video", "type": "output"}], "animated": [True]}
            elif ct == "SaveAudioAdvanced":
                os.makedirs(os.path.join(self.out_dir, "audio"), exist_ok=True)
                name = f"配音_{n:05d}.mp3"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", os.path.join(self.out_dir, "audio", name)], check=True)
                outs[nid] = {"audio": [{"filename": name, "subfolder": "audio", "type": "output"}]}
            elif ct == "ProAppText" and not inp.get("only_on_error"):
                os.makedirs(os.path.join(self.out_dir, "text"), exist_ok=True)
                name = f"{inp.get('label', '文字')}_{n:05d}.txt"
                with open(os.path.join(self.out_dir, "text", name), "w", encoding="utf-8") as f:
                    f.write(f"（假的文字结果）{inp.get('label', '')}")
                outs[nid] = {"files": [{"filename": name, "subfolder": "text", "type": "output", "display_name": inp.get("label", "")}]}
        self.history[pid] = {"status": {"status_str": "success", "completed": True, "messages": []}, "outputs": outs}
        self.running.discard(pid)


class Harness:
    def __init__(self, delay=1.2):
        self.tmp = tempfile.mkdtemp(prefix="pc_harness_")
        self.inp, self.out = os.path.join(self.tmp, "input"), os.path.join(self.tmp, "output")
        os.makedirs(os.path.join(self.inp, "助手"))
        os.makedirs(self.out)
        self.cfg = os.path.join(self.tmp, "relay_config.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"node_settings": {"31": {"api_key": "sk-harness-key"}}}, f)
        self.pc = _load.load("pro-chat")
        self.pc.workflows_dir, self.pc.input_dir, self.pc.output_dir = (lambda: WF_DIR), (lambda: self.inp), (lambda: self.out)
        self.pc.relay_config_path, self.pc.object_info, self.pc.comfy_port = (lambda: self.cfg), (lambda: OI), (lambda: 8188)
        self.llm, self.comfy = FakeLLM(), FakeComfy(self.out, delay)
        self.app = self.make_app()
        self.runner = self.loop = self.port = None
        self.run_delay = 0                                     # /pro/run 提交成功后先等几秒再回给网页（测「提交到一半刷新网页」）
        self.drop_run = 0                                      # 接下来这么多次 /pro/run：提交成功之后不回话直接断开连接（模拟「服务器收到了，回复没回到网页」）

    # ── 路由 ──
    def make_app(self):
        pc = self.pc
        app = web.Application(client_max_size=200 * 1024 * 1024)

        async def call(fn, *a):
            return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*a))

        def err(e):
            return web.json_response({"error": str(e)}, status=getattr(e, "status", 500))

        async def chat(request):
            try:
                return web.json_response(await call(lambda p: pc.handle_chat(p, post=self.llm), await request.json()))
            except pc.ChatError as e:
                return err(e)

        async def run(request):
            try:
                out = await call(lambda p: pc.handle_run(p, post=self.comfy), await request.json())
                if self.run_delay:
                    await asyncio.sleep(self.run_delay)
                if self.drop_run > 0:
                    self.drop_run -= 1
                    request.transport.close()
                    return web.Response(status=499)
                return web.json_response(out)
            except pc.ChatError as e:
                return err(e)

        async def queue_edit(request):
            d = await request.json()
            self.comfy.deleted.extend(d.get("delete") or [])
            return web.json_response({})

        async def stage(request):
            try:
                return web.json_response(await call(pc.handle_stage, await request.json()))
            except pc.ChatError as e:
                return err(e)

        async def catalog(request):
            return web.json_response(await call(pc.handle_catalog))

        async def history(request):
            pid = request.match_info["pid"]
            return web.json_response({pid: self.comfy.history[pid]} if pid in self.comfy.history else {})

        async def queue(request):
            return web.json_response({"queue_running": [[0, p, {}, {}, []] for p in self.comfy.running], "queue_pending": []})

        async def interrupt(request):
            d = await request.json()
            self.comfy.interrupted.add(d.get("prompt_id"))
            return web.json_response({})

        async def view(request):
            q = request.query
            base = {"input": self.inp, "output": self.out}.get(q.get("type", "output"))
            path = os.path.realpath(os.path.join(base, q.get("subfolder", ""), q["filename"]))
            if not base or not path.startswith(os.path.realpath(base) + os.sep) or not os.path.isfile(path):
                return web.Response(status=404)
            return web.FileResponse(path)

        async def upload(request):
            post = await request.post()
            f = post["image"]
            sub = post.get("subfolder", "")
            folder = os.path.join(self.inp, sub)
            os.makedirs(folder, exist_ok=True)
            name, i = f.filename, 0
            while os.path.exists(os.path.join(folder, name)):
                i += 1
                stem, ext = os.path.splitext(f.filename)
                name = f"{stem} ({i}){ext}"
            with open(os.path.join(folder, name), "wb") as out:
                out.write(f.file.read())
            return web.json_response({"name": name, "subfolder": sub, "type": "input"})

        async def userdata(request):
            rel = request.match_info["path"]
            p = os.path.realpath(os.path.join(ROOT, rel))
            if not p.startswith(WF_DIR + os.sep) or not os.path.isfile(p):
                return web.Response(status=404)
            return web.FileResponse(p)

        def static(root):
            async def handler(request):
                p = os.path.realpath(os.path.join(root, request.match_info["name"]))
                if not p.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(p):
                    return web.Response(status=404)
                return web.FileResponse(p, headers={"Cache-Control": "no-store"})
            return handler

        async def index(request):
            return web.FileResponse(os.path.join(HERE, "index.html"))

        def script(name):
            async def handler(request):
                return web.FileResponse(os.path.join(HERE, name), headers={"Content-Type": "text/javascript", "Cache-Control": "no-store"})
            return handler

        app.add_routes([
            web.get("/", index),
            web.get("/scripts/app.js", script("scripts_app.js")), web.get("/scripts/api.js", script("scripts_api.js")),
            web.get("/extensions/pro-chat/{name}", static(WEB)),
            web.post("/api/pro/chat", chat), web.post("/api/pro/run", run), web.post("/api/pro/stage", stage), web.get("/api/pro/catalog", catalog),
            web.get("/api/history/{pid}", history), web.get("/api/queue", queue), web.post("/api/queue", queue_edit), web.post("/api/interrupt", interrupt),
            web.get("/api/view", view), web.post("/api/upload/image", upload), web.get("/api/userdata/{path:.*}", userdata),
        ])
        return app

    # ── 起停（放在后台线程里，测试脚本同步用）──
    def start(self, port=0):
        ready = threading.Event()

        def serve():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.runner = web.AppRunner(self.app)
            self.loop.run_until_complete(self.runner.setup())
            site = web.TCPSite(self.runner, "127.0.0.1", port)
            self.loop.run_until_complete(site.start())
            self.port = site._server.sockets[0].getsockname()[1]
            ready.set()
            self.loop.run_forever()

        threading.Thread(target=serve, daemon=True).start()
        ready.wait(10)
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self.loop:
            fut = asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop)
            fut.result(5)
            self.loop.call_soon_threadsafe(self.loop.stop)
        shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    h = Harness()
    base = h.start(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    print("假 ComfyUI 已启动：", base, "（Ctrl+C 退出）", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        h.stop()
