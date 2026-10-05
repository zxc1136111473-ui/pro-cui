"""pro-chat 前端的纯函数（web/pro-chat-lib.js）：用 node 直接跑；没有 node 就跳过。
界面本身（web/pro-chat.js）要接 ComfyUI 的页面对象，没法在这里跑，靠真实界面验证；这里只做静态检查：
用到的每个 lib.xxx 都必须真的被库导出、不能有会把模型输出当 HTML 写进页面的写法。"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WEB = os.path.join(ROOT, "custom-nodes", "pro-chat", "web")
LIB = os.path.join(WEB, "pro-chat-lib.js")
NODE = shutil.which("node")


def js(script):
    """在 node 里（ES 模块）跑 script，它里面用 L 访问 lib，最后 console.log(JSON.stringify(结果))；返回解析后的结果。"""
    code = f'import * as L from "file://{LIB}";\n{script}'
    r = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr[-1500:])
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "没有 node")
class Lib(unittest.TestCase):
    def test_file_kind_and_urls(self):
        r = js('''console.log(JSON.stringify([L.fileKind("成片.MP4"), L.fileKind("a.png"), L.fileKind("配音_00001.mp3"), L.fileKind("x.wav"),
          L.fileKind("AI 写的提示词_00001.txt"), L.fileKind("x.bin"), L.fileKind("没有后缀"), L.fileKind(""), L.fileKind(null),
          L.viewPath({filename: "出错 信息.txt", subfolder: "text", type: "output"}), L.inputImagePath("助手/a b.png"), L.inputImagePath("demo.png")]))''')
        self.assertEqual(r[:9], ["video", "image", "audio", "audio", "text", "other", "other", "other", "other"])
        self.assertEqual(r[9], "/view?filename=%E5%87%BA%E9%94%99+%E4%BF%A1%E6%81%AF.txt&type=output&subfolder=text")
        self.assertEqual(r[10], "/view?filename=a+b.png&type=input&subfolder=%E5%8A%A9%E6%89%8B")
        self.assertEqual(r[11], "/view?filename=demo.png&type=input&subfolder=")

    ENTRY = {"outputs": {
        "4": {"images": [{"filename": "txt2img_00001_.png", "subfolder": "", "type": "output"}]},
        "5": {"images": [{"filename": "成片_00001_.mp4", "subfolder": "video", "type": "output"}], "animated": [True]},
        "7": {"audio": [{"filename": "配音_00001.mp3", "subfolder": "audio", "type": "output"}]},
        "11": {"text": ["{\"code\": \"success\"}"]},                                   # PreviewAny 的裸字符串：不是结果项
        "90": {"files": [{"filename": "AI 写的提示词_00001.txt", "subfolder": "text", "type": "output", "display_name": "AI 写的提示词"}]},
        "91": {"files": [{"filename": "出错信息_00001.txt", "subfolder": "text", "type": "output", "display_name": "出错信息：视频"}]},
        "92": {"files": [{"filename": "x.bin", "subfolder": "", "type": "output"}]}}}

    def test_history_items_follow_the_app_order_and_kinds(self):
        r = js(f'''const e = {json.dumps(self.ENTRY)}; console.log(JSON.stringify(L.historyItems(e, [5, 4, 7, 90, 91, 92, 11, 99])))''')
        self.assertEqual([(i["kind"], i["file"]["filename"], i["label"], i["isError"]) for i in r], [
            ("video", "成片_00001_.mp4", "", False), ("image", "txt2img_00001_.png", "", False), ("audio", "配音_00001.mp3", "", False),
            ("text", "AI 写的提示词_00001.txt", "AI 写的提示词", False), ("text", "出错信息_00001.txt", "出错信息：视频", True)])
        self.assertEqual(js('console.log(JSON.stringify(L.historyItems(null, [1])))'), [])
        self.assertEqual(js('console.log(JSON.stringify(L.historyItems({outputs: {}}, undefined)))'), [])

    def test_history_error_and_finished(self):
        err = {"status": {"status_str": "error", "completed": False, "messages": [["execution_start", {}], ["execution_error", {"node_id": "7", "node_type": "ProAliTTS", "exception_message": "[阿里 配音] HTTP 401"}]]}}
        ok = {"status": {"status_str": "success", "completed": True, "messages": []}}
        r = js(f'''console.log(JSON.stringify([L.historyError({json.dumps(err)}, {{"7": "配音"}}), L.historyError({json.dumps(err)}, {{}}), L.historyError({json.dumps(ok)}, {{}}),
          L.historyError(undefined, {{}}), L.isFinished({json.dumps(err)}), L.isFinished({json.dumps(ok)}), L.isFinished(undefined), L.isFinished({{status: {{status_str: "running"}}}})]))''')
        self.assertEqual(r, ["「配音」[阿里 配音] HTTP 401", "「ProAliTTS」[阿里 配音] HTTP 401", "", "", True, True, False, False])

    def test_interrupted_outcome_and_queue(self):
        stopped = {"status": {"status_str": "error", "completed": False, "messages": [["execution_start", {}], ["execution_interrupted", {"node_id": "74"}]]}}
        errored = {"status": {"status_str": "error", "completed": False, "messages": [["execution_error", {"node_id": "7"}]]}}
        r = js(f'''const T = (kind, isError = false) => ({{kind, isError}});
          console.log(JSON.stringify([L.wasInterrupted({json.dumps(stopped)}), L.wasInterrupted({json.dumps(errored)}), L.wasInterrupted(undefined),
            L.runOutcome([T("text"), T("text", true)], ""), L.runOutcome([T("image"), T("text", true)], ""), L.runOutcome([T("text")], ""), L.runOutcome([], ""), L.runOutcome([], "运行出错了"),
            L.runOutcome([T("text")], "「配音」HTTP 401"),
            L.inQueue({{queue_running: [[0, "p1", {{}}, {{}}, []]], queue_pending: [[1, "p2", {{}}, {{}}, []]]}}, "p2"), L.inQueue({{queue_running: [], queue_pending: []}}, "p1"), L.inQueue(null, "p1")]))''')
        self.assertEqual(r, [True, False, False, "failed", "done", "done", "done", "failed", "done", True, False, False])

    def test_payloads_do_not_carry_the_schema_or_share_objects(self):
        r = js('''const plan = {workflow: "01", name: "文生图", mode: "m", fields: {"70:idea": "猫"}, images: {}, schema: {big: 1}, note: "n"};
          const a = L.runPayload(plan, "cid"), b = L.serverPlan(plan); a.fields["70:idea"] = "改了"; b.fields.x = 1;
          console.log(JSON.stringify([a, b, plan.fields, L.serverPlan(null), L.runPayload({workflow: "04"}, undefined)]))''')
        self.assertEqual(r[0], {"workflow": "01", "mode": "m", "fields": {"70:idea": "改了"}, "images": {}, "client_id": "cid"})
        self.assertEqual(r[1], {"workflow": "01", "mode": "m", "fields": {"70:idea": "猫", "x": 1}, "images": {}})
        self.assertEqual(r[2], {"70:idea": "猫"})                                       # 原方案没被改
        self.assertIsNone(r[3])
        self.assertEqual(r[4], {"workflow": "04", "mode": None, "fields": {}, "images": {}, "client_id": ""})

    def test_api_messages_turn_assistant_replies_back_into_the_json_format(self):
        r = js('''const plan = {workflow: "03", mode: "B 模式", fields: {"70:idea": "换白底"}, images: {"4:image": "助手/b.png", "5:image": "助手/丢了.png"}, note: "n",
                   schema: {mode: {options: ["A 模式", "B 模式"]}}};
          const msgs = [{role: "user", text: "你好"}, {role: "assistant", text: "好的", plan}, {role: "error", text: "出错了"}, {role: "result", name: "x"},
                        {role: "assistant", text: "   "}, {role: "assistant", text: "闲聊"}, {role: "user", text: ""}, {role: "user", text: "再亮一点"}];
          console.log(JSON.stringify(L.apiMessages(msgs, ["助手/a.png", "助手/b.png"])))''')
        self.assertEqual([m["role"] for m in r], ["user", "assistant", "assistant", "user"])
        self.assertEqual(json.loads(r[1]["content"]), {"reply": "好的", "plan": {"workflow": "03", "mode": 2, "fields": {"70:idea": "换白底"}, "images": {"4:image": 2}, "note": "n"}})
        self.assertEqual(json.loads(r[2]["content"]), {"reply": "闲聊", "plan": None})

    def test_enter_to_send_ignores_ime_and_modifiers(self):
        r = js('''const k = (o) => L.isEnterToSend(Object.assign({key: "Enter", shiftKey: false, ctrlKey: false, metaKey: false, altKey: false, isComposing: false, keyCode: 13}, o));
          console.log(JSON.stringify([k({}), k({shiftKey: true}), k({isComposing: true}), k({keyCode: 229}), k({ctrlKey: true}), k({key: "a"}), k({metaKey: true})]))''')
        self.assertEqual(r, [True, False, False, False, False, False, False])

    def test_short_title_drops_the_star_prefix_and_truncates(self):
        r = js('console.log(JSON.stringify([L.shortTitle("★ 只填这里：商品信息（翻译模式：贴上要翻译的中文文案）"), L.shortTitle("阿里 写提示词"), L.shortTitle(""), L.shortTitle(null), L.shortTitle("abcdefghij", 5)]))')
        self.assertEqual(r, ["商品信息（翻译模式：贴上要翻…", "阿里 写提示词", "", "", "abcde…"])

    def test_elapsed_text(self):
        self.assertEqual(js('console.log(JSON.stringify([L.elapsedText(0), L.elapsedText(4600), L.elapsedText(59400), L.elapsedText(75000), L.elapsedText(-5)]))'),
                         ["0 秒", "5 秒", "59 秒", "1 分 15 秒", "0 秒"])

    def test_storage_round_trip_trims_and_keeps_only_runs_that_can_be_resumed(self):
        r = js('''const msgs = Array.from({length: 80}, (_, i) => ({id: "m" + i, role: "user", text: "第" + i + "句", images: i === 79 ? ["助手/keep.png"] : []}));
          msgs[77] = {id: "r1", role: "assistant", text: "方案", planState: "running", plan: {workflow: "01", images: {"6:image": "助手/plan.png"}}};                 // 还没拿到 prompt_id
          msgs[78] = {id: "r2", role: "assistant", text: "方案", planState: "running", plan: {workflow: "02"}, run: {promptId: "p-9", name: "海报", outputs: [4], titles: {}, started: 1}};   // 已提交：刷新后接着等
          const text = L.toStorage({messages: msgs, images: ["助手/keep.png", "助手/plan.png", "助手/unused.png"], plan: {workflow: "ignored"}});
          const back = L.fromStorage(text);
          console.log(JSON.stringify([back.messages.length, back.messages[57].planState, back.messages[58].planState, back.messages[58].run.promptId, back.messages[0].id, back.images,
            msgs[77].planState, JSON.parse(text).plan === undefined,
            L.fromStorage("不是 json"), L.fromStorage('{"v": 2}'), L.fromStorage(null), L.fromStorage('{"v": 1, "messages": "x"}')]))''')
        self.assertEqual(r[0], 60)
        self.assertEqual((r[1], r[2], r[3], r[4]), ("interrupted", "running", "p-9", "m20"))
        self.assertEqual(r[5], ["助手/keep.png", "助手/plan.png"])                       # 只留还被消息引用的图片
        self.assertEqual(r[6], "running")                                                # 不改原状态对象
        self.assertTrue(r[7])                                                            # 当前方案不再单独存（见 test_current_plan_is_the_object_in_the_message）
        self.assertEqual(r[8:], [None, None, None, None])

    def test_current_plan_is_the_object_in_the_message_so_edits_reach_the_model_after_a_reload(self):
        r = js('''const plan = {workflow: "01", fields: {"70:idea": "猫"}};
          const msgs = [{role: "user", text: "做图"}, {role: "assistant", text: "好", plan, planState: "open"}];
          const back = L.fromStorage(L.toStorage({messages: msgs, images: [], plan}));
          back.plan.fields["70:idea"] = "狗";                                              // 刷新后在卡片里改（卡片改的是消息里的方案）
          const serverSees = L.serverPlan(back.plan).fields["70:idea"];
          const dismissed = L.fromStorage(L.toStorage({messages: [{role: "assistant", plan, planState: "dismissed"}], images: []})).plan;
          const older = L.currentPlan([{role: "assistant", plan: {workflow: "A"}, planState: "superseded"}, {role: "assistant", plan: {workflow: "B"}, planState: "done"}, {role: "assistant", text: "闲聊"}]);
          console.log(JSON.stringify([back.plan === back.messages[1].plan, serverSees, dismissed, older.workflow, L.currentPlan([]), L.currentPlan(null)]))''')
        self.assertEqual(r, [True, "狗", None, "B", None, None])

    def test_live_plan_states_and_image_list(self):
        r = js('''console.log(JSON.stringify([["", undefined, "open", "failed", "stopped", "interrupted", "running", "done", "dismissed", "superseded"].map(L.isLivePlanState),
          L.addImages(["a", "b"], ["c"]), L.addImages(["a", "b"], ["a"]), L.addImages(["a", "b"], ["c", "c", "d"]),
          L.addImages(Array.from({length: 12}, (_, i) => "i" + i), ["n1", "n2"]), L.addImages([], [])]))''')
        self.assertEqual(r[0], [True, True, True, True, True, True, False, False, False, False])
        self.assertEqual(r[1], {"images": ["a", "b", "c"], "newIdx": [3]})
        self.assertEqual(r[2], {"images": ["b", "a"], "newIdx": [2]})                       # 同名的不重复占编号，算最新
        self.assertEqual(r[3], {"images": ["a", "b", "c", "d"], "newIdx": [3, 4]})
        full = r[4]
        self.assertEqual(len(full["images"]), 12)                                          # 超过 12 张丢最早的，编号跟着新列表走
        self.assertEqual((full["images"][0], full["images"][-2:], full["newIdx"]), ("i2", ["n1", "n2"], [11, 12]))
        self.assertEqual(r[5], {"images": [], "newIdx": []})

    def test_frontend_and_backend_agree_on_the_image_limit(self):
        with open(LIB, encoding="utf-8") as f:
            m = re.search(r"export const MAX_IMAGES = (\d+)", f.read())
        self.assertIsNotNone(m)
        with open(os.path.join(ROOT, "custom-nodes", "pro-chat", "chat.py"), encoding="utf-8") as f:
            self.assertEqual(int(m.group(1)), int(re.search(r"^MAX_IMAGES = (\d+)", f.read(), re.M).group(1)))


class Static(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(WEB, name), encoding="utf-8") as f:
            return f.read()

    @unittest.skipUnless(NODE, "没有 node")
    def test_every_lib_function_used_by_the_ui_exists(self):
        exported = set(js('console.log(JSON.stringify(Object.keys(L)))'))
        used = set(re.findall(r"(?<![\w-])lib\.(\w+)", self.read("pro-chat.js")))      # 排除 "pro-chat-lib.js" 这类文件名
        self.assertTrue(used, "界面脚本没用 lib？")
        self.assertEqual(used - exported, set(), "界面脚本用了库里没有的函数")

    def test_model_output_is_never_written_as_html(self):
        for name in ("pro-chat.js", "pro-chat-lib.js"):
            src = self.read(name)
            for bad in (".innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(bad, src, f"{name} 里有 {bad}：模型 / 用户的文字不能当 HTML")

    def test_ui_script_only_talks_to_our_routes_and_comfy_apis(self):
        src = self.read("pro-chat.js")
        routes = set(re.findall(r'(?:post|fetchApi)\(\s*[`"\'](/[^`"\'$?]*)', src))
        self.assertEqual(routes, {"/pro/chat", "/pro/run", "/interrupt", "/upload/image", "/history/", "/queue", "/userdata/"})
        self.assertNotIn("http://", src.replace("http://127", ""))
        self.assertNotIn("https://", src)

    def test_drop_and_paste_into_the_chat_do_not_leak_to_other_handlers(self):
        src = self.read("pro-chat.js")
        for name in ("onDrop", "onPaste"):
            body = re.search(r"function %s\(e\) \{(.*?)\n\}" % name, src, re.S)
            self.assertIsNotNone(body, name)
            self.assertIn("stopPropagation()", body.group(1), f"{name} 要 stopPropagation：文件只给聊天用")
        self.assertIn("e.stopPropagation(); root.classList.add", src)                    # dragover 也一样

    def test_lib_has_no_side_effects_and_no_imports(self):
        src = self.read("pro-chat-lib.js")
        self.assertIsNone(re.search(r"^\s*import\s", src, re.M))
        self.assertNotIn("document.", src)
        self.assertNotIn("window.", src)


if __name__ == "__main__":
    unittest.main()
