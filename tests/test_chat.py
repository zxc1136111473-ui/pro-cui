"""pro-chat（AI 助手）后端：应用目录、选择校验、对话（假的阿里响应）、运行（转 API 格式 + 填值 + 提交）、环境与路径防护。

不需要 ComfyUI 和网络：节点定义用 tests/object_info_subset.json（取自服务器 /object_info，工作流用到的 44 种节点），
阿里和 ComfyUI 的 HTTP 都换成假的 post。前端的纯函数测试在 tests/test_chat_js.py。
"""
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _load  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WF_DIR = os.path.join(ROOT, "workflows")
OI = json.load(open(os.path.join(os.path.dirname(__file__), "object_info_subset.json"), encoding="utf-8"))
pc = _load.load("pro-chat")
cat_mod, chat, run = pc.cat_mod, pc.chat, pc.run
ChatError = chat.ChatError

_spec = importlib.util.spec_from_file_location("t_pro_flow_chat", os.path.join(ROOT, "custom-nodes", "pro-flow", "__init__.py"))
FLOW = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FLOW)


class Resp:
    def __init__(self, status=200, data=None, text=None):
        self.status_code, self._d = status, data
        self.text = text if text is not None else json.dumps(data if data is not None else {}, ensure_ascii=False)

    def json(self):
        if self._d is None:
            raise ValueError("不是 JSON")
        return self._d


def llm(content):
    """阿里 chat/completions 的成功响应。"""
    return Resp(200, {"choices": [{"message": {"content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)}}]})


class FakePost:
    """按顺序返回准备好的响应，并记下每次请求（请求体深拷贝：被测代码可能事后改它）。"""
    def __init__(self, *resps):
        self.resps, self.calls = list(resps), []

    def __call__(self, url, headers=None, json=None, timeout=None):   # noqa: A002
        self.calls.append({"url": url, "headers": headers, "json": copy.deepcopy(json), "timeout": timeout})
        r = self.resps.pop(0) if len(self.resps) > 1 else self.resps[0]
        if isinstance(r, Exception):
            raise r
        return r


CAT = cat_mod.build_catalog(WF_DIR, OI)


def load_wf(app_id):
    with open(os.path.join(WF_DIR, CAT[app_id]["path"]), encoding="utf-8") as f:
        return json.load(f)


class Convert(unittest.TestCase):
    def test_every_workflow_converts_and_links_point_at_real_nodes(self):
        for app_id, app in CAT.items():
            wf = load_wf(app_id)
            api = run.convert(wf, OI)
            self.assertEqual(len(api), len(wf["nodes"]), f"{app_id}: 节点数不对")
            for nid, n in api.items():
                for k, v in n["inputs"].items():
                    self.assertIsNotNone(v, f"{app_id}#{nid}.{k}")
                    if isinstance(v, list):
                        self.assertIn(v[0], api, f"{app_id}#{nid}.{k} 连到了不存在的节点")

    def test_spot_checks(self):
        api = run.convert(load_wf("07"), OI)
        self.assertEqual(api["70"]["inputs"]["instruction"], ["77", 0])                 # 指令连模式节点
        self.assertEqual(api["70"]["inputs"]["idea"], load_wf("07")["nodes"][[n["id"] for n in load_wf("07")["nodes"]].index(70)]["widgets_values"][0])
        self.assertEqual(api["90"]["inputs"]["only_on_error"], False)
        self.assertEqual(api["90"]["inputs"]["text"], ["70", 0])
        api8 = run.convert(load_wf("08"), OI)
        self.assertEqual(api8["4"]["inputs"]["format.quality"], "V0")                   # 动态下拉的子控件
        self.assertEqual(api8["3"]["inputs"]["voice"], "Cherry")

    def test_batch_run_uses_the_same_converter(self):
        spec = importlib.util.spec_from_file_location("t_batch_run", os.path.join(ROOT, "tools", "batch_run.py"))
        bm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bm)
        self.assertEqual(bm.convert.__module__, "pro_chat_api_convert")
        for app_id in ("01", "08", "11"):
            self.assertEqual(bm.convert(load_wf(app_id), OI), run.convert(load_wf(app_id), OI))


class Catalog(unittest.TestCase):
    def test_all_eleven_apps_with_descriptions_and_results(self):
        self.assertEqual(list(CAT), [f"{i:02d}" for i in range(1, 12)])
        for app in CAT.values():
            self.assertTrue(app["desc"], f"{app['id']} 缺用途描述")
            self.assertTrue(app["results"] and app["outputs"], app["id"])

    def test_modes_match_the_mode_table(self):
        for app_id, app in CAT.items():
            if app["mode"] is None:
                continue
            node_type = next(n["type"] for n in load_wf(app_id)["nodes"] if n["type"].startswith("ProMode"))
            self.assertEqual(app["mode"]["options"], FLOW.MODES[node_type][1], app_id)
            self.assertEqual(app["mode"]["default"], app["mode"]["options"][0], app_id)
        self.assertIsNone(CAT["04"]["mode"])
        self.assertIsNone(CAT["06"]["mode"])

    def test_fields_images_and_uploads(self):
        self.assertEqual(set(CAT["01"]["fields"]), {"70:idea", "2:ratio", "3:ratio", "3:level"})
        self.assertEqual(CAT["01"]["fields"]["2:ratio"]["label"], "网关·比例")
        self.assertIn("16:9", CAT["01"]["fields"]["2:ratio"]["options"])
        self.assertEqual(set(CAT["03"]["images"]), {"4:image", "5:image"})
        self.assertEqual(CAT["03"]["images"]["5:image"]["label"], "场景图（合成模式）")
        self.assertEqual({u["key"]: u["kind"] for u in CAT["11"]["uploads"]}, {"2:file": "视频", "4:audio": "音频"})
        self.assertEqual({u["key"]: u["kind"] for u in CAT["08"]["uploads"]}, {"6:audio": "音频"})
        self.assertEqual(set(CAT["05"]["fields"]), {"6:text1", "6:text2", "6:text3"})
        for app in CAT.values():                                  # 上传 / 图片 / 字段三类互不重叠
            keys = [*app["fields"], *app["images"], *(u["key"] for u in app["uploads"])]
            self.assertEqual(len(keys), len(set(keys)), app["id"])

    def test_video_apps_expose_the_dubbing_voice_like_the_voice_app_does(self):
        want = CAT["08"]["fields"]["3:voice"]["options"]
        self.assertGreaterEqual(len(want), 40)
        for app_id in ("10", "11"):
            f = CAT[app_id]["fields"]["3:voice"]
            self.assertEqual((f["kind"], f["label"]), ("choice", "配音音色（配音模式）"), app_id)
            self.assertEqual(f["options"], want, app_id)                                    # 和 08 配音里是同一份音色
            self.assertIn("Cherry", f["options"])
            self.assertIn("默认 Cherry", CAT[app_id]["desc"])                               # 让模型知道默认是 Cherry、没说就别改

    def test_result_descriptions(self):
        self.assertEqual(CAT["04"]["results"][0], "图片 ×4")
        self.assertIn("文字「AI 写的提示词」", CAT["01"]["results"])
        self.assertIn("文字「出错信息」（只在出错时出现）", CAT["01"]["results"])
        self.assertEqual(CAT["10"]["results"][0], "视频")

    def test_catalog_text_lists_everything_the_model_needs(self):
        text = cat_mod.catalog_text(CAT)
        for app in CAT.values():
            self.assertIn(f"应用 {app['id']}「{app['name']}」", text)
            if app["mode"]:
                for o in app["mode"]["options"]:
                    self.assertIn(o, text)
        self.assertIn("2:ratio｜网关·比例", text)
        self.assertIn("聊天里设不了", text)
        self.assertIn("Suno", text)
        self.assertNotRegex(text, r"sk-[A-Za-z0-9]{10,}")

    def test_user_made_app_without_description_still_loads_and_plain_workflows_do_not(self):
        wf = load_wf("07")
        wf["extra"].pop("appDescription")
        app = cat_mod.load_app(wf, "我的/99-自己做的.app.json", OI)
        self.assertEqual((app["id"], app["name"], app["desc"]), ("99", "自己做的", ""))
        wf["extra"].pop("linearData")
        self.assertIsNone(cat_mod.load_app(wf, "x.app.json", OI))
        self.assertEqual(cat_mod.app_id_name("没有数字.app.json"), ("没有数字", "没有数字"))

    def test_unreadable_files_are_skipped(self):
        d = tempfile.mkdtemp()
        try:
            write_text(os.path.join(d, "坏的.app.json"), "{不是 json")
            shutil.copy(os.path.join(WF_DIR, CAT["07"]["path"]), os.path.join(d, "07-文案.app.json"))
            self.assertEqual(list(cat_mod.build_catalog(d, OI)), ["07"])
        finally:
            shutil.rmtree(d)

    def test_widget_kind_handles_both_combo_forms(self):
        self.assertEqual(cat_mod.widget_kind([["a", "b"], {}])[:2], ("choice", ["a", "b"]))
        self.assertEqual(cat_mod.widget_kind(["COMBO", {"options": ["x", "y"]}])[:2], ("choice", ["x", "y"]))
        self.assertEqual(cat_mod.widget_kind(["STRING", {}])[0], "text")
        self.assertIsNone(cat_mod.widget_kind(["INT", {}])[0])
        self.assertIsNone(cat_mod.widget_kind(None)[0])


class Selection(unittest.TestCase):
    def sel(self, app_id, mode=None, fields=None, images=None, resolve=lambda r: r):
        return cat_mod.clean_selection(CAT[app_id], mode, fields, images, resolve)

    def test_mode_by_number_text_and_close_text(self):
        opts = CAT["03"]["mode"]["options"]
        self.assertEqual(self.sel("03", 2)[0]["mode"], opts[1])
        self.assertEqual(self.sel("03", "3")[0]["mode"], opts[2])
        self.assertEqual(self.sel("03", opts[0])[0]["mode"], opts[0])
        self.assertEqual(self.sel("03", opts[3][:-2])[0]["mode"], opts[3])             # 长选项少了几个字也认
        self.assertEqual(self.sel("01", None, {"2:ratio": "5:4"})[0]["fields"], {})     # 短选项（比例）必须一字不差，不能把 5:4 当成 3:4
        self.assertEqual(self.sel("01", None, {"2:ratio": " 16:9 "})[0]["fields"], {"2:ratio": "16:9"})
        sel, warns = self.sel("03", 9)
        self.assertIsNone(sel["mode"])
        self.assertTrue(warns)
        self.assertEqual(self.sel("03", None)[0]["mode"], None)
        self.assertEqual(self.sel("04", 1)[0]["mode"], None)                           # 没有模式的应用忽略 mode

    def test_text_choice_and_unknown_fields(self):
        sel, warns = self.sel("01", None, {"70:idea": "  一只橘猫  ", "2:ratio": "16:9", "3:level": "8K", "99:x": "y", "31:apikey": "偷偷改 Key"})
        self.assertEqual(sel["fields"], {"70:idea": "一只橘猫", "2:ratio": "16:9"})
        self.assertEqual(len(warns), 3)                                                  # 8K 不在选项里、99:x、31:apikey 都被丢掉
        self.assertEqual(self.sel("01", None, {"70:idea": "   "})[0]["fields"], {})
        self.assertEqual(len(self.sel("01", None, {"70:idea": "字" * 5000})[0]["fields"]["70:idea"]), cat_mod.MAX_TEXT)
        self.assertEqual(self.sel("01", None, "不是字典")[0]["fields"], {})

    def test_text_values_of_other_types_and_long_text_are_handled_not_repr(self):
        s, w = self.sel("01", None, {"70:idea": ["标题一", "标题二"]})
        self.assertEqual(s["fields"], {"70:idea": "标题一\n标题二"})                      # 列表按行拼，不是 "['标题一', '标题二']"
        self.assertEqual(self.sel("01", None, {"70:idea": {"text": "x"}})[0]["fields"], {"70:idea": '{"text": "x"}'})
        self.assertEqual(self.sel("01", None, {"70:idea": 123})[0]["fields"], {"70:idea": "123"})
        self.assertEqual(self.sel("01", None, {"70:idea": None})[0]["fields"], {})
        s, w = self.sel("01", None, {"70:idea": "字" * (cat_mod.MAX_TEXT + 1)})
        self.assertEqual(len(s["fields"]["70:idea"]), cat_mod.MAX_TEXT)
        self.assertEqual(len(w), 1)                                                       # 截断要告诉用户，不能悄悄少一截
        self.assertIn("太长", w[0])
        self.assertEqual(self.sel("01", None, {"70:idea": "字" * cat_mod.MAX_TEXT})[1], [])

    def test_bare_node_id_keys_are_accepted_when_unambiguous(self):
        s, w = self.sel("08", None, {"70": "温暖的女声", "3": "你好"})                      # 08 的 70 号节点只有一个可填字段；3 号节点有「要念的文字」和「音色」两个，不唯一
        self.assertEqual(s["fields"], {"70:idea": "温暖的女声"})                            # 写进的是规整后的键「70:idea」
        self.assertEqual(len(w), 1)                                                       # 「3」认不出，仍然提示
        self.assertEqual(self.sel("01", None, {"2": "16:9"})[0]["fields"], {"2:ratio": "16:9"})   # 01 的 2 号节点只有比例一个
        s, w = self.sel("03", None, None, {"4": "助手/a.png", "5": "助手/b.png"})
        self.assertEqual(s["images"], {"4:image": "助手/a.png", "5:image": "助手/b.png"})
        for key in ("７０", "70 ", "", "x", "70:", ":idea"):                                # 全角数字、带空格、残缺的键都不认
            self.assertEqual(self.sel("01", None, {key: "x"})[0]["fields"], {}, repr(key))

    def test_weird_mode_numbers_do_not_crash(self):
        opts = CAT["03"]["mode"]["options"]
        for weird in (float("nan"), float("inf"), "②", "²", "٣", "9" * 5000, True, False, [], {}, "  "):
            sel, warns = self.sel("03", weird)                                            # 不抛异常，只是认不出（用默认模式）
            self.assertIsNone(sel["mode"], repr(weird)[:20])
        self.assertEqual(self.sel("03", " 2 ")[0]["mode"], opts[1])
        self.assertEqual(self.sel("03", 2.0)[0]["mode"], opts[1])

    def test_safe_image_name_is_a_segment_rule_not_a_substring_rule(self):
        for good in ("助手/a.png", "助手/photo..png", "助手/子/b.png", "助手/a b.png"):
            self.assertEqual(cat_mod.safe_image_name(good), good)
        for bad in ("助手/../x.png", "助手/./x.png", "助手//x.png", "助手/", "助手", "助手\\x.png", "助手/x\x00.png", "demo.png", "/助手/a.png", "", None, 5):
            self.assertIsNone(cat_mod.safe_image_name(bad), repr(bad))

    def test_images(self):
        sel, warns = self.sel("03", None, None, {"4:image": "助手/a.png", "5:image": "不存在", "9:image": "助手/b.png"},
                              resolve=lambda r: r if r == "助手/a.png" else None)
        self.assertEqual(sel["images"], {"4:image": "助手/a.png"})
        self.assertEqual(len(warns), 2)


class ChatTurn(unittest.TestCase):
    PLAN = {"workflow": "07", "mode": 1, "fields": {"70:idea": "新鲜红苹果，产地直发，脆甜多汁"}, "images": {}, "note": "写文案"}

    def turn(self, post, messages=None, names=(), vision=(), plan=None, usable=None):
        payload = {"messages": messages or [{"role": "user", "content": "帮我写苹果的文案"}], "plan": plan}
        return chat.chat_turn(payload, CAT, "sk-test-key-123", "https://dashscope.aliyuncs.com", list(names), list(vision), usable=usable, post=post)

    def test_plan_is_validated_and_carries_schema(self):
        post = FakePost(llm({"reply": "好的，用文案应用写。", "plan": self.PLAN}))
        out = self.turn(post)
        self.assertEqual(out["reply"], "好的，用文案应用写。")
        p = out["plan"]
        self.assertEqual((p["workflow"], p["name"], p["mode"]), ("07", "文案", CAT["07"]["mode"]["options"][0]))
        self.assertEqual(p["fields"], {"70:idea": "新鲜红苹果，产地直发，脆甜多汁"})
        self.assertEqual(p["schema"]["outputs"], [90])
        self.assertEqual(out["warnings"], [])
        call = post.calls[0]
        self.assertEqual(call["url"], "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk-test-key-123")
        self.assertEqual(call["json"]["model"], chat.TEXT_MODEL)
        self.assertEqual(call["json"]["response_format"], {"type": "json_object"})
        self.assertFalse(call["json"]["enable_thinking"])
        self.assertIn("应用 07「文案」", call["json"]["messages"][0]["content"])         # 目录进了系统提示词

    def test_json_in_code_fence_and_extra_words(self):
        for wrapped in ("```json\n" + json.dumps({"reply": "嗨", "plan": None}) + "\n```", "好的：" + json.dumps({"reply": "嗨", "plan": None}) + " 完毕"):
            out = self.turn(FakePost(llm(wrapped)))
            self.assertEqual((out["reply"], out["plan"]), ("嗨", None))

    def test_retry_once_when_reply_is_not_json(self):
        post = FakePost(llm("我觉得可以做文案"), llm({"reply": "好", "plan": self.PLAN}))
        out = self.turn(post)
        self.assertEqual(out["plan"]["workflow"], "07")
        self.assertEqual(len(post.calls), 2)
        self.assertIn("不是合法的 JSON", post.calls[1]["json"]["messages"][-1]["content"])
        with self.assertRaises(ChatError) as cm:
            self.turn(FakePost(llm("不是 json")))
        self.assertEqual(cm.exception.status, 502)

    def test_response_format_unsupported_is_retried_without_it(self):
        post = FakePost(Resp(400, {"error": {"message": "response_format is not supported"}}), llm({"reply": "ok", "plan": None}))
        self.assertEqual(self.turn(post)["reply"], "ok")
        self.assertTrue("response_format" in post.calls[0]["json"])
        self.assertFalse("response_format" in post.calls[1]["json"])

    def test_http_error_is_reported_without_leaking_the_key(self):
        for body in ({"error": {"code": "invalid_api_key", "message": "Incorrect API key provided: sk-te****123"}},
                     {"error": {"code": "bad", "message": "key sk-test-key-123 is wrong"}},
                     {"error": "sk-test-key-123 在这里"}):
            with self.assertRaises(ChatError) as cm:
                self.turn(FakePost(Resp(401, body)))
            msg = str(cm.exception)
            self.assertEqual(cm.exception.status, 502)
            self.assertIn("401", msg)
            for leaked in ("sk-test-key-123", "sk-te****123", "te****123"):               # 完整的 Key 和掩码后的 Key 都不能原样转发给浏览器
                self.assertNotIn(leaked, msg, body)
        with self.assertRaises(ChatError) as cm:
            self.turn(FakePost(Resp(500, None, "<html>bad gateway sk-test-key-123</html>")))
        self.assertNotIn("sk-test-key-123", str(cm.exception))
        self.assertIn("bad gateway", str(cm.exception))                                   # 其余文字还在
        self.assertEqual(chat.scrub("x sk-abc_DEF-123 y", ""), "x sk-*** y")

    def test_an_early_error_response_does_not_beat_a_slower_successful_one(self):
        import threading
        import time
        lock, n = threading.Lock(), []

        def post(url, headers=None, json=None, timeout=None):                          # noqa: A002
            with lock:
                n.append(1)
                me = len(n)
            if me == 1:
                time.sleep(0.6)                                                           # 第一个慢，但会成功
                return llm({"reply": "慢但成功", "plan": None})
            return Resp(429, {"error": {"code": "Throttling", "message": "too many"}})   # 并发补发的那个很快就被限流了

        text = chat.call_llm([{"role": "user", "content": "hi"}], "k", "https://x", "m", post=post, hedge_after=0.05)
        self.assertEqual(json.loads(text)["reply"], "慢但成功")
        # 全都失败时报第一个错误响应，而不是别的
        with self.assertRaises(ChatError) as cm:
            chat.call_llm([{"role": "user", "content": "hi"}], "k", "https://x", "m", post=FakePost(Resp(429, {"error": {"code": "Throttling", "message": "too many"}})), hedge_after=0.05)
        self.assertIn("429", str(cm.exception))

    def test_malformed_current_plan_from_the_client_does_not_crash(self):
        for plan in ({"workflow": "01", "images": ["x"], "fields": "y"}, {"workflow": 1, "mode": ["a"], "fields": {"k": [1, {"a": 2}]}, "images": {"4:image": 5}}, {"workflow": "x", "images": None}):
            post = FakePost(llm({"reply": "ok", "plan": None}))
            self.assertEqual(self.turn(post, plan=plan)["reply"], "ok")

    def test_connection_errors_reconnect_quickly_then_report(self):
        import requests
        post = FakePost(requests.ConnectionError("x"), requests.ConnectTimeout("y"), llm({"reply": "ok", "plan": None}))
        self.assertEqual(self.turn(post)["reply"], "ok")
        self.assertEqual(len(post.calls), 3)
        self.assertIsInstance(post.calls[0]["timeout"], tuple)                           # 连接阶段是短超时，不是干等
        self.assertLess(post.calls[0]["timeout"][0], 10)
        post = FakePost(requests.ConnectionError("x"))
        with self.assertRaises(ChatError) as cm:
            self.turn(post)
        self.assertIn("连不上阿里", str(cm.exception))
        self.assertEqual(len(post.calls), chat.LLM_ATTEMPTS)

    def test_slow_first_request_is_hedged_with_a_second_one(self):
        import threading
        import time
        lock, order = threading.Lock(), []

        def post(url, headers=None, json=None, timeout=None):                          # noqa: A002
            with lock:
                order.append(len(order) + 1)
                me = order[-1]
            if me == 1:
                time.sleep(1.0)                                                           # 第一个卡住
                return llm({"reply": "慢的", "plan": None})
            return llm({"reply": "快的", "plan": None})

        t0 = time.time()
        text = chat.call_llm([{"role": "user", "content": "hi"}], "k", "https://x", "m", post=post, hedge_after=0.05)
        self.assertEqual(json.loads(text)["reply"], "快的")                              # 用先回来的
        self.assertLess(time.time() - t0, 0.8)                                            # 没有等那个卡住的
        self.assertEqual(len(order), 2)

        t0 = time.time()
        order.clear()
        post2 = FakePost(llm({"reply": "好", "plan": None}))                              # 第一个很快回了：不会再发第二个
        chat.call_llm([{"role": "user", "content": "hi"}], "k", "https://x", "m", post=post2, hedge_after=0.5)
        self.assertEqual(len(post2.calls), 1)

    def test_hedging_adds_a_request_every_interval_up_to_three(self):
        import threading
        import time
        lock, started = threading.Lock(), []

        def post(url, headers=None, json=None, timeout=None):                          # noqa: A002
            with lock:
                started.append(time.time())
                me = len(started)
            if me < 3:
                time.sleep(1.5)                                                           # 前两个都卡住
            return llm({"reply": f"第{me}个", "plan": None})

        t0 = time.time()
        text = chat.call_llm([{"role": "user", "content": "hi"}], "k", "https://x", "m", post=post, hedge_after=0.1)
        self.assertEqual(json.loads(text)["reply"], "第3个")
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual(len(started), 3)                                                 # 不会超过 MAX_PARALLEL
        self.assertEqual(chat.MAX_PARALLEL, 3)

    def test_hedge_waits_for_the_other_when_the_first_one_fails(self):
        import requests
        import threading
        import time

        def post(url, headers=None, json=None, timeout=None):                          # noqa: A002
            if threading.current_thread().name.endswith("_0"):                            # 线程池里先提交的那个请求：慢，然后一直连不上
                time.sleep(0.1)
                raise requests.ConnectionError("reset")
            time.sleep(0.6)                                                               # 并发发的第二个：比第一个失败得晚，但成功了
            return llm({"reply": "第二个成功了", "plan": None})

        t0 = time.time()
        text = chat.call_llm([{"role": "user", "content": "hi"}], "k", "https://x", "m", post=post, hedge_after=0.05)
        self.assertEqual(json.loads(text)["reply"], "第二个成功了")
        self.assertGreater(time.time() - t0, 0.5)                                         # 第一个早就失败了，还是等了第二个

    def test_read_timeout_is_not_resent(self):
        import requests
        post = FakePost(requests.ReadTimeout("slow"))
        with self.assertRaises(ChatError) as cm:
            self.turn(post)
        self.assertEqual(cm.exception.status, 504)
        self.assertIn("回得太慢", str(cm.exception))
        self.assertEqual(len(post.calls), 1)                                             # 请求已经发出去了，重发只会白等一倍的时间

    def test_unknown_workflow_drops_the_plan_but_keeps_the_reply(self):
        post = FakePost(llm({"reply": "试试这个", "plan": {"workflow": "99", "fields": {}}}))
        out = self.turn(post)
        self.assertEqual(out["reply"], "试试这个")
        self.assertIsNone(out["plan"])
        self.assertEqual(out["warnings"], ["没有编号为「99」的应用，方案已丢弃"])
        self.assertEqual(len(post.calls), 2)                                             # 重写一次还是不行就放弃，不会一直重试

    def test_plan_without_a_usable_app_id_is_retried_once(self):
        bad = {"reply": "好", "plan": {"mode": 1, "fields": {"70:idea": "红苹果"}}}      # 漏了 workflow
        post = FakePost(llm(bad), llm({"reply": "好", "plan": self.PLAN}))
        out = self.turn(post)
        self.assertEqual(out["plan"]["workflow"], "07")
        self.assertEqual(out["warnings"], [])
        self.assertEqual(len(post.calls), 2)
        sent = post.calls[1]["json"]["messages"]
        self.assertIn("没有写对 workflow", sent[-1]["content"])
        self.assertEqual(sent[-2]["role"], "assistant")                                  # 把它上一次的回复一起带上
        post = FakePost(llm(bad), llm(bad))
        out = self.turn(post)
        self.assertEqual((out["reply"], out["plan"], len(post.calls)), ("好", None, 2))
        self.assertEqual(out["warnings"], ["方案里没有写应用编号，已丢弃"])

    def test_no_retry_when_the_model_gave_no_plan_on_purpose(self):
        post = FakePost(llm({"reply": "先点「附图」传一张商品图", "plan": None}))
        out = self.turn(post)
        self.assertEqual((out["plan"], out["warnings"], len(post.calls)), (None, [], 1))

    def test_app_id_is_found_under_alias_keys_and_loose_spellings(self):
        for raw in ({"workflow_id": "07"}, {"app_id": 7}, {"workflow": "7"}, {"workflow": "应用 07「文案」"}, {"app": " 07 "}, {"workflow": None, "id": "07"}):
            out = self.turn(FakePost(llm({"reply": "r", "plan": {**raw, "mode": 1, "fields": {}}})))
            self.assertEqual(out["plan"]["workflow"], "07", raw)
        for raw in ({"workflow": True}, {"workflow": ["07"]}, {"workflow": "0"}, {"workflow": "第七个"}):
            self.assertIsNone(self.turn(FakePost(llm({"reply": "r", "plan": raw})))["plan"], raw)

    def test_model_inventions_are_dropped(self):
        raw = {"reply": "r", "plan": {"workflow": "01", "mode": 7, "fields": {"70:idea": "猫", "2:ratio": "5:4", "88:x": "y"}, "images": {"6:image": 1}}}
        out = self.turn(FakePost(llm(raw)), names=["助手/a.png"])
        p = out["plan"]
        self.assertEqual(p["mode"], None)
        self.assertEqual(p["fields"], {"70:idea": "猫"})
        self.assertEqual(p["images"], {})
        self.assertEqual(len(out["warnings"]), 4)

    def test_images_numbers_become_file_names_and_vision_model_is_used(self):
        raw = {"reply": "r", "plan": {"workflow": "03", "mode": 1, "fields": {}, "images": {"4:image": 2, "5:image": 9}}}
        post = FakePost(llm(raw))
        out = self.turn(post, names=["助手/a.png", "助手/b.png"], vision=["data:image/jpeg;base64,AAA"])
        self.assertEqual(out["plan"]["images"], {"4:image": "助手/b.png"})
        call = post.calls[0]["json"]
        self.assertEqual(call["model"], chat.VISION_MODEL)
        last = call["messages"][-1]["content"]
        self.assertEqual([c["type"] for c in last], ["image_url", "text"])
        self.assertIn("1. a.png", call["messages"][0]["content"])
        self.assertIn("2. b.png", call["messages"][0]["content"])

    def test_lost_image_files_are_marked_and_never_used(self):
        raw = {"reply": "r", "plan": {"workflow": "03", "mode": 1, "fields": {}, "images": {"4:image": 1}}}
        post = FakePost(llm(raw))
        out = self.turn(post, names=["助手/gone.png"], usable=[])
        self.assertEqual(out["plan"]["images"], {})
        self.assertIn("文件已经不存在", post.calls[0]["json"]["messages"][0]["content"])

    def test_history_is_trimmed_and_current_plan_is_shown_to_the_model(self):
        msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}句"} for i in range(30)] + [{"role": "user", "content": "最后一句"}]
        plan = {"workflow": "01", "mode": CAT["01"]["mode"]["options"][1], "fields": {"70:idea": "橘猫"}, "images": {}}
        post = FakePost(llm({"reply": "ok", "plan": None}))
        self.turn(post, messages=msgs, plan=plan)
        sent = post.calls[0]["json"]["messages"]
        self.assertEqual(len(sent), 1 + chat.MAX_HISTORY)
        self.assertEqual(sent[-1]["content"], "最后一句")
        self.assertIn('"workflow": "01"', sent[0]["content"])
        self.assertIn("橘猫", sent[0]["content"])

    def test_bad_requests(self):
        for payload in ({"messages": []}, {"messages": [{"role": "assistant", "content": "x"}]}, {"messages": [{"role": "user", "content": "  "}]}):
            with self.assertRaises(ChatError) as cm:
                chat.chat_turn(payload, CAT, "k", "b", [], [], post=FakePost(llm("{}")))
            self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(ChatError) as cm:
            chat.chat_turn({"messages": [{"role": "user", "content": "x"}]}, {}, "k", "b", [], [], post=FakePost(llm("{}")))
        self.assertEqual(cm.exception.status, 503)


class Run(unittest.TestCase):
    def prep(self, app_id, payload, exists=lambda n: True):
        return run.prepare_run(CAT[app_id], load_wf(app_id), OI, payload, exists)

    def test_values_are_filled_into_the_api_prompt(self):
        opts = CAT["01"]["mode"]["options"]
        api, sel, warns = self.prep("01", {"mode": opts[1], "fields": {"70:idea": "海边日落", "2:ratio": "16:9"}})
        self.assertEqual(api["77"]["inputs"]["mode"], opts[1])
        self.assertEqual(api["70"]["inputs"]["idea"], "海边日落")
        self.assertEqual(api["2"]["inputs"]["ratio"], "16:9")
        self.assertEqual(warns, [])
        base = run.convert(load_wf("01"), OI)
        for nid in base:                                   # 没选的控件一个都没动（种子除外）
            for k, v in base[nid]["inputs"].items():
                if (nid, k) not in {("77", "mode"), ("70", "idea"), ("2", "ratio")} and k != "seed":
                    self.assertEqual(api[nid]["inputs"][k], v, f"{nid}.{k}")

    def test_seeds_change_every_run_but_links_are_untouched(self):
        a, _, _ = self.prep("01", {})
        b, _, _ = self.prep("01", {})
        base = run.convert(load_wf("01"), OI)
        seeds = lambda api: [n["inputs"]["seed"] for n in api.values() if isinstance(n["inputs"].get("seed"), int)]
        self.assertTrue(seeds(a) and seeds(a) != seeds(b))
        for nid, n in base.items():
            for k, v in n["inputs"].items():
                if isinstance(v, list):
                    self.assertEqual(a[nid]["inputs"][k], v, f"{nid}.{k} 是连线，不能被改")

    def test_the_dubbing_voice_of_the_video_apps_can_be_changed_and_bad_voices_are_dropped(self):
        for app_id in ("10", "11"):
            base = run.convert(load_wf(app_id), OI)
            self.assertEqual(base["3"]["inputs"]["voice"], "Cherry")                       # 默认音色
            api, sel, warns = self.prep(app_id, {"fields": {"3:voice": "Ethan"}})
            self.assertEqual((api["3"]["inputs"]["voice"], sel["fields"], warns), ("Ethan", {"3:voice": "Ethan"}, []), app_id)
            api, sel, warns = self.prep(app_id, {"fields": {"3:voice": "不存在的音色"}})
            self.assertEqual(api["3"]["inputs"]["voice"], "Cherry", app_id)                # 编的音色不进工作流，保持默认
            self.assertEqual(sel["fields"], {})
            self.assertTrue(warns)
            self.assertEqual(api["3"]["inputs"]["model"], base["3"]["inputs"]["model"])    # 别的配音设置不受影响

    def test_the_client_cannot_change_anything_outside_the_catalog(self):
        base = run.convert(load_wf("01"), OI)
        api, sel, warns = self.prep("01", {"fields": {"31:apikey": "偷偷改 Key", "31:custom_api_base": "http://evil", "1:custom_api_base": "http://evil", "70:instruction": "x"}})
        self.assertEqual(api["31"]["inputs"], base["31"]["inputs"])
        self.assertEqual(api["1"]["inputs"], base["1"]["inputs"])
        self.assertEqual(api["70"]["inputs"]["instruction"], base["70"]["inputs"]["instruction"])
        self.assertEqual(len(warns), 4)

    def test_images_only_from_the_assistant_folder_that_exist(self):
        ok = lambda n: n == "助手/a.png"
        api, sel, warns = self.prep("03", {"images": {"4:image": "助手/a.png"}}, ok)
        self.assertEqual(api["4"]["inputs"]["image"], "助手/a.png")
        api, sel, warns = self.prep("03", {"images": {"4:image": "助手/不存在.png"}}, ok)       # 文件不存在：用默认图
        self.assertEqual(api["4"]["inputs"]["image"], "demo_product.png")
        self.assertTrue(warns)

    def test_image_name_rules_hold_even_if_the_file_check_says_yes(self):
        anything = lambda n: True                                                         # 文件检查放行一切：名字规则自己必须挡住
        api, sel, warns = self.prep("03", {"images": {"4:image": "助手/photo..png"}}, anything)
        self.assertEqual(api["4"]["inputs"]["image"], "助手/photo..png")                  # 合法文件名（含两个点）不能误杀
        for bad in ("助手/../../etc/passwd", "other.png", "../助手/a.png", "助手\\x.png", "助手//x.png", "助手/./x.png", "助手/", "助手/a\x00.png", "", None):
            api, sel, warns = self.prep("03", {"images": {"4:image": bad}}, anything)
            self.assertEqual(api["4"]["inputs"]["image"], "demo_product.png", repr(bad))
            self.assertEqual(sel["images"], {}, repr(bad))
            self.assertTrue(warns, repr(bad))

    def test_submit_ok_and_validation_errors_become_readable(self):
        post = FakePost(Resp(200, {"prompt_id": "abc", "number": 3, "node_errors": {}}))
        out = run.submit_prompt({"1": {}}, "cid-1", 8188, {}, post=post)
        self.assertEqual(out["prompt_id"], "abc")
        self.assertEqual(post.calls[0]["url"], "http://127.0.0.1:8188/prompt")
        self.assertEqual(post.calls[0]["json"], {"prompt": {"1": {}}, "client_id": "cid-1"})
        err = {"error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation", "details": ""},
               "node_errors": {"3": {"class_type": "LoadImage", "errors": [{"message": "Value not in list", "details": "image: 'x.png' not in [...]"}]}}}
        with self.assertRaises(ChatError) as cm:
            run.submit_prompt({}, "", 8188, {"3": "① 上传商品图"}, post=FakePost(Resp(400, err)))
        self.assertIn("① 上传商品图", str(cm.exception))
        self.assertIn("Value not in list", str(cm.exception))
        with self.assertRaises(ChatError):
            run.submit_prompt({}, "", 8188, {}, post=FakePost(Resp(500, None, "boom")))


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Glue(unittest.TestCase):
    """__init__.py：环境（Key / 路径 / 节点定义）和两个入口，ComfyUI 相关的函数换成假的。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inp = os.path.join(self.tmp, "input")
        os.makedirs(os.path.join(self.inp, "助手"))
        from PIL import Image
        Image.new("RGB", (1500, 900), (200, 30, 30)).save(os.path.join(self.inp, "助手", "a.png"))
        write_text(os.path.join(self.tmp, "secret.txt"), "机密")
        self.cfg = os.path.join(self.tmp, "relay_config.json")
        write_text(self.cfg, json.dumps({"node_settings": {"31": {"api_key": "sk-glue-key-999"}, "1": {"api_key": "sk-gateway-key"}}}))
        self.saved = {k: getattr(pc, k) for k in ("workflows_dir", "input_dir", "comfy_port", "relay_config_path", "object_info")}
        pc.workflows_dir, pc.input_dir, pc.comfy_port = (lambda: WF_DIR), (lambda: self.inp), (lambda: 8188)
        pc.relay_config_path, pc.object_info = (lambda: self.cfg), (lambda: OI)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(pc, k, v)
        shutil.rmtree(self.tmp)

    def test_key_comes_from_node_31_only_and_missing_key_is_a_clear_error(self):
        self.assertEqual(pc.ali_key(), "sk-glue-key-999")
        write_text(self.cfg, json.dumps({"node_settings": {"1": {"api_key": "x"}}}))
        with self.assertRaises(ChatError) as cm:
            pc.ali_key()
        self.assertEqual(cm.exception.status, 503)
        self.assertIn("Key：阿里", str(cm.exception))
        os.remove(self.cfg)
        with self.assertRaises(ChatError):
            pc.ali_key()

    def test_image_paths_are_confined_to_the_assistant_folder(self):
        self.assertTrue(pc.image_exists("助手/a.png"))
        for bad in ("助手/../secret.txt", "../secret.txt", "secret.txt", "助手/不存在.png", "助手\\a.png", "/etc/passwd", "", None, "助手/", "助手", "助手/a\x00.png"):
            self.assertFalse(pc.image_exists(bad), bad)
        os.symlink(os.path.join(self.tmp, "secret.txt"), os.path.join(self.inp, "助手", "link.png"))     # 符号链接指到目录外也不行
        self.assertFalse(pc.image_exists("助手/link.png"))
        os.makedirs(os.path.join(self.inp, "private"))                                    # input/ 里、但不在 助手/ 里的文件，符号链接过去也不行
        shutil.copy(os.path.join(self.inp, "助手", "a.png"), os.path.join(self.inp, "private", "x.png"))
        os.symlink(os.path.join(self.inp, "private", "x.png"), os.path.join(self.inp, "助手", "link2.png"))
        self.assertFalse(pc.image_exists("助手/link2.png"))
        os.symlink(os.path.join(self.inp, "private"), os.path.join(self.inp, "助手", "dirlink"))        # 目录符号链接同理
        self.assertFalse(pc.image_exists("助手/dirlink/x.png"))
        shutil.copy(os.path.join(self.inp, "助手", "a.png"), os.path.join(self.inp, "助手", "photo..png"))
        self.assertTrue(pc.image_exists("助手/photo..png"))                               # 文件名里有两个点是合法的
        write_text(os.path.join(self.inp, "助手", "notes.txt"), "不是图片")
        self.assertTrue(pc.image_exists("助手/notes.txt"))                                # 这一层只管「在不在目录里」；能不能当图片用见下一个测试

    def test_an_undecodable_image_is_skipped_with_a_warning_not_a_500(self):
        write_text(os.path.join(self.inp, "助手", "bad.png"), "这不是图片")
        post = FakePost(llm({"reply": "好", "plan": None}))
        out = pc.handle_chat({"messages": [{"role": "user", "content": "看看这些图"}], "images": ["助手/a.png", "助手/bad.png"], "new_images": [1, 2]}, post=post)
        self.assertEqual(out["reply"], "好")
        self.assertEqual([w for w in out["warnings"] if "打不开" in w], ["图片「bad.png」打不开，已忽略（换 PNG / JPG 再传）"])
        sent = post.calls[0]["json"]
        self.assertEqual(sent["model"], chat.VISION_MODEL)                                # 好的那张照样送去看图
        self.assertEqual(len([c for c in sent["messages"][-1]["content"] if c["type"] == "image_url"]), 1)
        self.assertIn("2. bad.png（文件已经不存在，不要用）", sent["messages"][0]["content"])   # 坏图占着编号，但不让模型用
        post = FakePost(llm({"reply": "好", "plan": None}))                                # 只有坏图：退回文字模型
        out = pc.handle_chat({"messages": [{"role": "user", "content": "看看"}], "images": ["助手/bad.png"], "new_images": [1]}, post=post)
        self.assertEqual(post.calls[0]["json"]["model"], chat.TEXT_MODEL)
        self.assertEqual(len(out["warnings"]), 1)

    def test_vision_image_is_shrunk_and_is_a_jpeg_data_url(self):
        import base64
        import io
        from PIL import Image
        url = pc.image_data_url("助手/a.png")
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        self.assertEqual(max(im.size), pc.VISION_SIDE)

    def test_handle_chat_end_to_end(self):
        raw = {"reply": "做一张海报", "plan": {"workflow": "02", "mode": 2, "fields": {"70:idea": "夏日促销，满199减50"}, "images": {"6:image": 1}}}
        post = FakePost(llm(raw))
        out = pc.handle_chat({"messages": [{"role": "user", "content": "做个夏日海报"}], "images": ["助手/a.png"], "new_images": [1]}, post=post)
        self.assertEqual(out["plan"]["workflow"], "02")
        self.assertEqual(out["plan"]["images"], {"6:image": "助手/a.png"})
        sent = post.calls[0]
        self.assertEqual(sent["json"]["model"], chat.VISION_MODEL)
        self.assertTrue(sent["json"]["messages"][-1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg"))
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sk-glue-key-999")
        self.assertNotIn("sk-glue-key-999", json.dumps(out, ensure_ascii=False))                # Key 不会回给前端
        self.assertNotIn("sk-gateway-key", json.dumps(sent["json"], ensure_ascii=False))        # 也不会误把别的 Key 发给阿里

    def test_handle_run_end_to_end(self):
        post = FakePost(Resp(200, {"prompt_id": "p-1", "number": 1, "node_errors": {}}))
        opts = CAT["07"]["mode"]["options"]
        out = pc.handle_run({"workflow": "07", "mode": opts[2], "fields": {"70:idea": "红苹果，脆甜"}, "client_id": "cid"}, post=post)
        self.assertEqual((out["prompt_id"], out["name"], out["outputs"]), ("p-1", "文案", [90]))
        sent = post.calls[0]["json"]
        self.assertEqual(sent["client_id"], "cid")
        self.assertEqual(sent["prompt"]["77"]["inputs"]["mode"], opts[2])
        self.assertEqual(sent["prompt"]["70"]["inputs"]["idea"], "红苹果，脆甜")
        with self.assertRaises(ChatError) as cm:
            pc.handle_run({"workflow": "99"}, post=post)
        self.assertEqual(cm.exception.status, 404)
        for bad in (None, [], "x"):
            with self.assertRaises(ChatError):
                pc.handle_run(bad, post=post)
            with self.assertRaises(ChatError):
                pc.handle_chat(bad, post=post)

    def test_partial_validation_errors_from_comfyui_are_shown_as_warnings(self):
        ne = {"node_errors": {"31": {"class_type": "ProAliPromptWriter", "errors": [{"message": "Value not in list", "details": "x"}]}}}
        post = FakePost(Resp(200, {"prompt_id": "p-2", "number": 1, **ne}))
        out = pc.handle_run({"workflow": "07", "client_id": "c"}, post=post)
        self.assertEqual(out["prompt_id"], "p-2")
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("没通过校验", out["warnings"][0])
        self.assertIn("Value not in list", out["warnings"][0])
        post = FakePost(Resp(200, {"prompt_id": "p-3", "number": 2, "node_errors": {}}))
        self.assertEqual(pc.handle_run({"workflow": "07"}, post=post)["warnings"], [])

    def test_package_is_a_pure_frontend_plus_routes_package(self):
        self.assertEqual(pc.NODE_CLASS_MAPPINGS, {})
        self.assertEqual(pc.WEB_DIRECTORY, "./web")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "custom-nodes", "pro-chat", "web", "pro-chat.js")))


if __name__ == "__main__":
    unittest.main()
