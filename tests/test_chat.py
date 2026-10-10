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
    def test_every_app_has_a_description_and_results(self):
        self.assertEqual(sorted(CAT), [f"{i:02d}" for i in range(1, 17)])                   # 目录按类别文件夹排（图片 / 文案与配音 / 视频），编号不一定连着
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

    def test_fields_and_files(self):
        common = lambda app: {k for k, f in CAT[app]["fields"].items() if not f.get("advanced")}
        advanced = lambda app: {k for k, f in CAT[app]["fields"].items() if f.get("advanced")}
        self.assertEqual(common("01"), {"70:idea", "2:ratio", "3:ratio", "3:level"})
        self.assertEqual(advanced("01"), {"2:size", "2:quality", "3:model"})
        self.assertEqual(CAT["01"]["fields"]["2:ratio"]["label"], "网关·比例")
        self.assertIn("16:9", CAT["01"]["fields"]["2:ratio"]["options"])
        self.assertEqual(CAT["01"]["fields"]["2:ratio"]["default"], "1:1")                    # 默认值取自工作流里存的值
        self.assertEqual({k: f["kind"] for k, f in CAT["03"]["files"].items()}, {"4:image": "image", "5:image": "image"})
        self.assertEqual(CAT["03"]["files"]["5:image"]["label"], "场景图（合成模式）")
        self.assertEqual({k: f["kind"] for k, f in CAT["11"]["files"].items() if f["kind"] != "image"}, {"2:file": "video", "4:audio": "audio"})
        self.assertEqual({k: f["kind"] for k, f in CAT["08"]["files"].items()}, {"6:audio": "audio"})
        self.assertTrue({"6:text1", "6:text2", "6:text3", "6:position1", "6:style2"} <= common("05"))            # 标签文字在右栏登记，位置 / 样式是常用设置
        self.assertTrue({"6:size1_pct", "6:shape", "3:scale_by"} <= advanced("05"))
        for app in CAT.values():                                  # 文件 / 字段两类互不重叠
            keys = [*app["fields"], *app["files"]]
            self.assertEqual(len(keys), len(set(keys)), app["id"])

    def test_every_field_has_a_default_that_is_valid_for_its_kind(self):
        for app in CAT.values():
            for k, f in app["fields"].items():
                where = f"{app['id']} {k}"
                if f["kind"] == "choice":
                    self.assertIn(f["default"], f["options"], where)
                elif f["kind"] in ("int", "float"):
                    self.assertIsInstance(f["default"], (int, float), where)
                    self.assertNotIsInstance(f["default"], bool, where)
                    if f["min"] is not None:
                        self.assertGreaterEqual(f["default"], f["min"], where)
                    if f["max"] is not None:
                        self.assertLessEqual(f["default"], f["max"], where)
                elif f["kind"] == "bool":
                    self.assertIsInstance(f["default"], bool, where)
                else:
                    self.assertEqual(f["kind"], "text", where)
                    self.assertIsInstance(f["default"], str, where)

    def test_extra_settings_and_costs_come_from_the_workflow_extras(self):
        f = CAT["08"]["fields"]
        sp = f["3:speed"]
        self.assertEqual((sp["kind"], sp["default"], sp["min"], sp["max"], sp["label"]), ("float", 1.0, 0.5, 2.0, "配音语速"))
        self.assertFalse(sp.get("advanced"))                                              # appExtra 是常用设置
        self.assertIn("慢一点", sp["hint"])                                               # 登记时写的说明
        self.assertTrue(f["3:volume_db"]["advanced"])                                     # appAdvanced 是高级设置
        self.assertEqual(f["3:model"]["options"], ["qwen3-tts-flash", "qwen3-tts-instruct-flash"])
        self.assertEqual((f["3:language"]["default"], f["3:model"]["default"]), ("Chinese", "qwen3-tts-flash"))
        self.assertEqual((f["3:instructions"]["kind"], f["3:instructions"]["default"], f["3:instructions"]["advanced"]), ("text", "", True))
        s22 = CAT["09"]["fields"]["22:make_instrumental"]
        self.assertEqual((s22["kind"], s22["default"], s22["advanced"]), ("bool", True, True))
        self.assertIn("新建音色", CAT["08"]["cost"])
        self.assertIn("额度", CAT["10"]["cost"])
        self.assertEqual(CAT["07"]["cost"], "")
        text = cat_mod.catalog_text({"08": CAT["08"], "10": CAT["10"]})
        self.assertIn("费用 / 注意：", text)
        self.assertIn("高级设置（用户明确要求才改", text)
        self.assertIn("3:speed｜配音语速｜小数，范围 0.5~2，默认 1，说明：", text)
        self.assertIn("3:model｜配音模型｜选项，选项：qwen3-tts-flash / qwen3-tts-instruct-flash，默认 qwen3-tts-flash，说明：", text)
        self.assertLess(text.index("3:speed｜"), text.index("高级设置"))                  # 常用的在前、高级的在后

    def test_no_registered_setting_is_silently_skipped(self):
        """工作流里登记的每个右栏控件、常用设置、高级设置，目录里都要有（登记错了节点 / 控件名，load_app 会悄悄跳过，这里抓出来）。"""
        for app_id, app in CAT.items():
            ex = load_wf(app_id)["extra"]
            want = {f"{nid}:{w}" for nid, w in ex["linearData"]["inputs"]} | {f"{e[0]}:{e[1]}" for k in ("appExtra", "appAdvanced") for e in ex.get(k, [])}
            have = set(app["fields"]) | set(app["files"]) | ({app["mode"]["key"]} if app["mode"] else set())
            self.assertEqual(want - have, set(), app_id)
            for k, f in app["fields"].items():
                self.assertEqual(bool(f.get("advanced")), any(f"{e[0]}:{e[1]}" == k for e in ex.get("appAdvanced", [])), f"{app_id} {k}")

    def test_required_files_are_marked_and_shown_to_the_model(self):
        required = {a: [k for k, f in app["files"].items() if f.get("required")] for a, app in CAT.items()}
        self.assertEqual({a: k for a, k in required.items() if k}, {"03": ["4:image"], "04": ["2:image"], "05": ["2:image"], "06": ["3:image"], "12": ["2:image"], "15": ["2:image"], "16": ["2:image"]})
        self.assertIn("4:image｜商品图｜图片（必填）", cat_mod.catalog_text({"03": CAT["03"]}))
        self.assertNotIn("（必填）", cat_mod.catalog_text({"02": CAT["02"]}))                       # 02 的商品图只在「带商品图」模式才用
        wf = load_wf("03")
        wf["extra"]["appRequired"] = [[4, "image"], [99, "image"], [70, "idea"]]                   # 不存在的节点 / 不是文件控件：忽略
        app = cat_mod.load_app(wf, "03-x.app.json", OI)
        self.assertEqual([k for k, f in app["files"].items() if f.get("required")], ["4:image"])

    def test_extra_settings_can_restrict_a_shared_dropdown_to_the_options_that_make_sense(self):
        for app_id in ("10", "12"):
            f = CAT[app_id]["fields"]
            self.assertEqual(f["12:ratio"]["options"], ["16:9", "9:16"], app_id)                  # Veo 只有横屏 / 竖屏，节点下拉里别的比例（1:1、4:3……）发出去也没用
            self.assertEqual(f["12:duration"]["options"], ["4", "6", "8"], app_id)
            self.assertEqual((f["12:ratio"]["default"], f["12:duration"]["default"]), ("16:9", "8"))
            self.assertIn("1:1", OI["RelayVideoGenerator"]["input"]["required"]["ratio"][1]["options"] if isinstance(OI["RelayVideoGenerator"]["input"]["required"]["ratio"][0], str)
                          else OI["RelayVideoGenerator"]["input"]["required"]["ratio"][0])         # 节点本身是有这些选项的
            s, w = cat_mod.clean_selection(CAT[app_id], None, {"12:ratio": "1:1", "12:duration": "10"}, None, lambda k, r, kind: r)
            self.assertEqual(s["fields"], {}, app_id)
            self.assertEqual(len(w), 2, app_id)
            self.assertEqual(cat_mod.clean_selection(CAT[app_id], None, {"12:ratio": "9:16", "12:duration": "6"}, None, lambda k, r, kind: r)[0]["fields"], {"12:ratio": "9:16", "12:duration": "6"})
        wf = load_wf("12")
        wf["extra"]["appExtra"] = [[12, "ratio", "视频比例", "", ["4:3", "没有这个选项"]], [12, "duration", "时长", "", ["没有这个选项"]]]
        app = cat_mod.load_app(wf, "12-x.app.json", OI)
        self.assertEqual(app["fields"]["12:ratio"]["options"], ["4:3"])                           # 只留节点里真有的
        self.assertTrue(len(app["fields"]["12:duration"]["options"]) > 1)                         # 一个都对不上就不限制（宁可多给选项，不能让下拉变空）

    def test_dropdowns_with_numeric_options_are_text_in_the_ui_but_numbers_in_the_workflow(self):
        for key, want in (("8:fps", {"options": ["24", "30"], "default": "24", "cast": "int"}), ("8:short_side", {"options": ["720", "1080"], "default": "720", "cast": "int"})):
            f = CAT["11"]["fields"][key]
            self.assertEqual({k: f[k] for k in want}, want, key)
        wf = load_wf("11")
        api, sel, warns = run.prepare_run(CAT["11"], wf, OI, {"mode": CAT["11"]["mode"]["options"][2], "fields": {"8:fps": "30", "8:short_side": 1080}, "files": {"12:image": "助手/a.png"}}, lambda n: True)
        self.assertEqual((api["8"]["inputs"]["fps"], api["8"]["inputs"]["short_side"], warns), (30, 1080, []))
        self.assertIsInstance(api["8"]["inputs"]["fps"], int)                                  # 整数 30，不是文字 "30"（ComfyUI 的下拉是严格比对的）
        self.assertEqual(sel["fields"], {"8:fps": 30, "8:short_side": 1080})
        s, w = cat_mod.clean_selection(CAT["11"], None, {"8:fps": "25", "8:short_side": "最高"}, None, lambda k, r, kind: r)
        self.assertEqual((s["fields"], len(w)), ({}, 2))
        plan_side = cat_mod.clean_selection(CAT["11"], None, {"8:fps": 30, "8:short_side": "1080"}, None, lambda k, r, kind: r)[0]["fields"]
        self.assertEqual(plan_side, {"8:fps": "30", "8:short_side": "1080"})                      # 方案里一直是文字（界面下拉、给模型看的都是文字）
        out = chat.make_plan({"workflow": "11", "mode": 3, "fields": {"8:fps": 30}}, CAT, [])[0]
        self.assertEqual(out["steps"][0]["fields"], {"8:fps": "30"})
        self.assertEqual(cat_mod.option_cast([24, 30]), "int")
        self.assertEqual(cat_mod.option_cast([0.5, 1]), "float")
        for raw in ([], None, ["a", 1], [True, 2], ["1", "2"]):
            self.assertIsNone(cat_mod.option_cast(raw), repr(raw))
        self.assertEqual(cat_mod.raw_options(["COMBO", {"options": [1, 2]}]), [1, 2])
        self.assertEqual(cat_mod.raw_options([[3, 4], {}]), [3, 4])
        self.assertIsNone(cat_mod.raw_options(["INT", {}]))

    def test_a_stored_default_is_matched_to_its_dropdown_option_by_value_too(self):
        # 前端的 JS 会把 1.0 存成 1：选项是 "1.0" 时默认值也要对得上，不能掉回第一个选项
        self.assertEqual(cat_mod.option_for(1, ["0.5", "1.0", "2.0"]), "1.0")
        self.assertEqual(cat_mod.option_for(24, ["24", "30"]), "24")
        self.assertEqual(cat_mod.option_for("30", ["24", "30"]), "30")
        self.assertEqual(cat_mod.option_for(2.0, ["1", "2"]), "2")
        for bad in (None, 7, "x", True, float("nan"), float("inf")):
            self.assertIsNone(cat_mod.option_for(bad, ["24", "30"]), repr(bad))
        oi = copy.deepcopy(OI)
        oi["ProSlideshow"]["input"]["required"]["fps"] = [[24.0, 30.0], {"default": 24.0}]
        wf = load_wf("11")
        next(n for n in wf["nodes"] if n["id"] == 8)["widgets_values"][5] = 30                  # 工作流里存的是整数 30
        f = cat_mod.load_app(wf, "11-x.app.json", oi)["fields"]["8:fps"]
        self.assertEqual((f["options"], f["default"], f["cast"]), (["24.0", "30.0"], "30.0", "float"))

    def test_dialogue_apps_are_in_the_catalog_with_described_voices(self):
        for app_id, name in (("13", "多角色配音"), ("14", "对白成片")):
            app = CAT[app_id]
            self.assertEqual(app["name"], name)
            f = app["fields"]
            self.assertEqual({k for k in f if k.endswith(("name", "voice"))}, {f"3:role{i}_{w}" for i in (1, 2, 3, 4) for w in ("name", "voice")} | {"3:narrator_voice"})
            v = f["3:role1_voice"]
            self.assertEqual(v["kind"], "choice")
            self.assertEqual(len(v["options"]), 49)
            self.assertEqual(v["default"], "Cherry · 女 · 阳光亲切")
            self.assertEqual(f["3:role2_voice"]["default"], "Ethan · 男 · 朝气温暖，带北方口音")
            self.assertEqual(f["3:script"]["kind"], "text")
            self.assertTrue(f["3:script"]["default"].startswith("小美："))
            self.assertFalse(f["3:script"].get("advanced"))
            self.assertTrue(f["3:gap_s"]["advanced"] and f["3:role1_style"]["advanced"])       # 停顿 / 语气指令是高级设置
            self.assertFalse(f["3:role3_name"].get("advanced") or f["3:narrator_voice"].get("advanced"))   # 第 3、4 个角色和旁白要能顺手填
        self.assertIsNone(CAT["13"]["mode"])
        self.assertEqual(CAT["14"]["mode"]["options"], ["对白配到上传的视频上（替换原声，烧字幕）", "图片轮播 + 对白（烧字幕）"])
        self.assertEqual(CAT["13"]["produces"], ["audio", "text"])
        self.assertEqual(CAT["14"]["produces"], ["video"])
        self.assertEqual(sorted(CAT["14"]["files"]), ["12:image", "13:image", "14:image", "2:file", "4:audio"])       # 上传视频 / 最多 3 张图 / 背景音乐，都不是必填（看模式）
        self.assertFalse(any(x.get("required") for x in CAT["14"]["files"].values()))

    def test_short_text_fields_are_marked_single_line_and_long_ones_are_not(self):
        f13, f16 = CAT["13"]["fields"], CAT["16"]["fields"]
        for i in (1, 2, 3, 4):
            self.assertIs(f13[f"3:role{i}_name"].get("multiline"), False)                         # 角色名：界面里用单行输入框
        self.assertIs(f16["5:bg_color"].get("multiline"), False)
        for key in ("3:script", "3:role1_style"):
            self.assertNotIn("multiline", f13[key])                                                # 对白脚本 / 语气指令是多行（节点里写的是 multiline: True）：不带标记，界面还是多行框
        self.assertNotIn("multiline", CAT["01"]["fields"]["70:idea"])                              # 文生图的「想画什么」没声明单行：还是多行框
        self.assertTrue(all(f.get("multiline") is not False for a in CAT.values() for f in a["fields"].values() if f["kind"] != "text"))   # 只有文字控件才会带这个标记

    def test_a_bare_widget_name_without_the_node_id_is_accepted_when_it_is_unambiguous(self):
        f13, f15 = CAT["13"]["fields"], CAT["15"]["files"]
        self.assertIs(cat_mod.lookup_key(f13, "script"), f13["3:script"])
        self.assertIs(cat_mod.lookup_key(f13, "Role1_Voice"), f13["3:role1_voice"])                 # 大小写不计
        self.assertIsNone(cat_mod.lookup_key(f13, "不存在"))
        self.assertIs(cat_mod.lookup_key(f15, "image"), f15["2:image"])                              # 只有一个图片上传：「image」就是它
        self.assertIsNone(cat_mod.lookup_key(CAT["11"]["files"], "image"))                           # 11 有三个图片上传：说不清是哪个，不猜
        self.assertIsNone(cat_mod.lookup_key(f13, ""))
        sel, warns = cat_mod.clean_selection(CAT["13"], None, {"script": "小美：你好\n阿强：你好", "role1_name": "小美", "role2_name": "阿强", "role1_voice": "Serena", "speed": 1.1}, {}, lambda *a: None)
        self.assertEqual(warns, [])
        self.assertEqual(sel["fields"]["3:role1_voice"], "Serena · 女 · 温柔")
        self.assertEqual((sel["fields"]["3:speed"], sel["fields"]["3:role1_name"]), (1.1, "小美"))

    def test_layer_apps_are_in_the_catalog(self):
        a, b = CAT["15"], CAT["16"]
        self.assertEqual((a["name"], b["name"]), ("商品抠图", "图层合成"))
        self.assertIsNone(a["mode"])
        self.assertEqual(b["mode"]["options"], ["放到背景图上", "放到纯色背景上"])
        self.assertEqual((a["produces"], b["produces"]), (["image"], ["image"]))
        self.assertTrue(a["files"]["2:image"]["required"] and b["files"]["2:image"]["required"])        # 商品一定要有（不然会悄悄用演示图）
        self.assertFalse(b["files"]["3:image"].get("required"))                                        # 背景图只在背景图模式要
        self.assertEqual(sorted(k for k, f in a["fields"].items() if f["kind"] == "choice"), ["3:edge", "3:model"])
        self.assertEqual(a["fields"]["3:model"]["default"], "通用（u2net，约 176MB：只抠画面里最主要的那一个）")
        self.assertTrue(a["fields"]["3:edge"]["advanced"] and not a["fields"]["3:model"].get("advanced"))
        f = b["fields"]
        self.assertEqual((f["5:position"]["default"], f["5:scale_pct"]["default"], f["5:shadow"]["default"]), ("正中", 60.0, "地面接触阴影"))
        self.assertEqual(f["5:bg_color"]["kind"], "text")
        self.assertTrue(f["5:save_layers"]["advanced"] and f["5:offset_x_pct"]["advanced"])
        self.assertFalse(f["5:position"].get("advanced") or f["5:scale_pct"].get("advanced") or f["5:shadow"].get("advanced"))
        self.assertIn("第一次用要先下载抠图模型", a["cost"])
        self.assertEqual(b["cost"], "")                                                                  # 图层合成没有要提醒的费用 / 额度（用途里已经写了纯本地）
        text = cat_mod.catalog_text(CAT)
        self.assertIn("应用 15「商品抠图」", text)
        self.assertIn("应用 16「图层合成」", text)

    def test_plain_voice_dropdowns_borrow_the_descriptions_but_keep_plain_values(self):
        for app_id in ("08", "10", "11", "12"):
            f = CAT[app_id]["fields"]["3:voice"]
            self.assertEqual(f["options"][0], "Cherry")                                      # 值还是纯名字
            self.assertEqual(f["labels"]["Cherry"], "Cherry · 女 · 阳光亲切")
            self.assertEqual(f["labels"]["Ethan"], "Ethan · 男 · 朝气温暖，带北方口音")
            self.assertEqual(len(f["labels"]), 49)
        self.assertNotIn("labels", CAT["13"]["fields"]["3:role1_voice"])                   # 选项本身就写了说明的不用再借
        text = cat_mod.catalog_text(CAT)
        self.assertIn("Cherry（女 · 阳光亲切）", text)                                         # 给模型看的目录：名字（说明）
        # 值：模型照目录抄成「名字（说明）」/ 只写名字 / 写完整选项，都认
        sel = lambda app, key, v: cat_mod.clean_selection(CAT[app], None, {key: v}, {}, lambda *a: None)
        for v in ("Ethan", "ethan", "Ethan（男 · 朝气温暖，带北方口音）", "Ethan · 男 · 朝气温暖，带北方口音"):
            self.assertEqual(sel("10", "3:voice", v), ({"mode": None, "fields": {"3:voice": "Ethan"}, "files": {}}, []), v)
            self.assertEqual(sel("13", "3:role1_voice", v)[0]["fields"], {"3:role1_voice": "Ethan · 男 · 朝气温暖，带北方口音"}, v)
        self.assertEqual(sel("13", "3:role1_voice", "没有这个音色")[0]["fields"], {})
        self.assertEqual(len(sel("13", "3:role1_voice", "没有这个音色")[1]), 1)
        # 模式仍然容忍长选项的小出入，括号处理不能把它弄坏
        self.assertEqual(cat_mod.pick_option("上传视频 + 配音文案", CAT["11"]["mode"]["options"], True), "上传视频 + 配音文案（替换原声）")
        self.assertEqual(cat_mod.pick_option(3, CAT["11"]["mode"]["options"], True), "图片轮播 + 配音文案")

    def test_extra_entries_that_cannot_be_used_are_skipped(self):
        wf = load_wf("08")
        wf["extra"]["appExtra"] = [[3, "speed", "语速", ""], [3, "text", "重复的", ""], [99, "speed", "没有这个节点", ""], [3, "不存在的控件", "x", ""], [10, "branch", "被连线占用", ""],
                                  [3, "audio", "输出口不是控件", ""], "坏的", [3, "speed"], [4, "filename_prefix", "只有三项也行"]]
        app = cat_mod.load_app(wf, "08-配音.app.json", OI)
        self.assertEqual(app["fields"]["3:speed"]["label"], "语速")
        self.assertEqual(app["fields"]["3:text"]["label"], "要念的文字")                  # 已经在右栏里登记过的，不被后面的登记覆盖
        self.assertEqual([k for k in app["fields"] if k.startswith(("99:", "10:"))], [])
        self.assertNotIn("3:不存在的控件", app["fields"])
        self.assertNotIn("3:audio", app["fields"])
        self.assertEqual(app["fields"]["4:filename_prefix"]["label"], "只有三项也行")

    def test_catalog_text_lists_everything_the_model_needs(self):
        text = cat_mod.catalog_text(CAT)
        for app in CAT.values():
            self.assertIn(f"应用 {app['id']}「{app['name']}」", text)
            if app["mode"]:
                for o in app["mode"]["options"]:
                    self.assertIn(o, text)
        self.assertIn("2:ratio｜网关·比例", text)
        self.assertIn("文件字段", text)
        self.assertIn("步骤N", text)
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
        self.assertEqual(cat_mod.widget_kind(["INT", {"min": 1}]), ("int", None, {"min": 1}))
        self.assertEqual(cat_mod.widget_kind(["FLOAT", {}])[0], "float")
        self.assertEqual(cat_mod.widget_kind(["BOOLEAN", {}])[0], "bool")
        self.assertIsNone(cat_mod.widget_kind(["IMAGE", {}])[0])                       # 连线输入不是控件
        self.assertIsNone(cat_mod.widget_kind(None)[0])


class Selection(unittest.TestCase):
    def sel(self, app_id, mode=None, fields=None, files=None, resolve=lambda key, ref, kind: ref):
        return cat_mod.clean_selection(CAT[app_id], mode, fields, files, resolve)

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
        self.assertEqual(s["files"], {"4:image": "助手/a.png", "5:image": "助手/b.png"})
        for key in ("７０", "70 ", "", "x", "70:", ":idea"):                                # 全角数字、带空格、残缺的键都不认
            self.assertEqual(self.sel("01", None, {key: "x"})[0]["fields"], {}, repr(key))

    def test_weird_mode_numbers_do_not_crash(self):
        opts = CAT["03"]["mode"]["options"]
        for weird in (float("nan"), float("inf"), "②", "²", "٣", "9" * 5000, True, False, [], {}, "  "):
            sel, warns = self.sel("03", weird)                                            # 不抛异常，只是认不出（用默认模式）
            self.assertIsNone(sel["mode"], repr(weird)[:20])
        self.assertEqual(self.sel("03", " 2 ")[0]["mode"], opts[1])
        self.assertEqual(self.sel("03", 2.0)[0]["mode"], opts[1])

    def test_safe_asset_name_is_a_segment_rule_not_a_substring_rule(self):
        for good in ("助手/a.png", "助手/photo..png", "助手/子/b.png", "助手/a b.png"):
            self.assertEqual(cat_mod.safe_asset_name(good), good)
        for bad in ("助手/../x.png", "助手/./x.png", "助手//x.png", "助手/", "助手", "助手\\x.png", "助手/x\x00.png", "demo.png", "/助手/a.png", "", None, 5):
            self.assertIsNone(cat_mod.safe_asset_name(bad), repr(bad))

    def test_file_kinds_come_from_the_extension(self):
        for name, kind in (("a.PNG", "image"), ("x.jpeg", "image"), ("助手/v.mp4", "video"), ("b.MOV", "video"), ("c.mp3", "audio"), ("d.wav", "audio"), ("e.flac", "audio")):
            self.assertEqual(cat_mod.file_kind(name), kind, name)
        for name in ("a.txt", "b.py", "c", "", None, ".png.exe", "x.svg", 5):
            self.assertIsNone(cat_mod.file_kind(name), repr(name))

    def test_files_are_resolved_per_control_and_kind(self):
        seen = []

        def resolve(key, ref, kind):
            seen.append((key, kind))
            return ref if ref == "助手/a.png" else None

        sel, warns = self.sel("03", None, None, {"4:image": "助手/a.png", "5:image": "不存在", "9:image": "助手/b.png", "不是字典的": 1}, resolve=resolve)
        self.assertEqual(sel["files"], {"4:image": "助手/a.png"})
        self.assertEqual(len(warns), 3)                                                   # 5:image 不能用、9:image 没这个控件、「不是字典的」没这个控件
        self.assertEqual(seen, [("4:image", "image"), ("5:image", "image")])             # 解析函数拿到的是控件键和它要的类型
        self.assertEqual(self.sel("03", None, None, "不是字典")[0]["files"], {})
        self.assertEqual(self.sel("11", None, None, {"2": "助手/v.mp4"}, resolve=lambda k, r, kind: (r, kind))[0]["files"], {"2:file": ("助手/v.mp4", "video")})


NUM_APP = {"mode": None, "files": {}, "fields": {
    "3:speed": {"key": "3:speed", "label": "语速", "kind": "float", "min": 0.5, "max": 2.0, "step": 0.1, "default": 1.0, "hint": ""},
    "3:count": {"key": "3:count", "label": "张数", "kind": "int", "min": 1, "max": 8, "step": 1, "default": 4, "hint": ""},
    "3:big": {"key": "3:big", "label": "不限上限", "kind": "int", "min": 0, "max": None, "step": 1, "default": 0, "hint": ""},
    "3:loop": {"key": "3:loop", "label": "循环", "kind": "bool", "default": False, "hint": ""},
}}


class NumbersAndSwitches(unittest.TestCase):
    def sel(self, fields):
        return cat_mod.clean_selection(NUM_APP, None, fields, None, lambda k, r, kind: r)

    def test_numbers_are_parsed_clamped_and_rounded(self):
        s, w = self.sel({"3:speed": "1.5", "3:count": 3.6})
        self.assertEqual((s["fields"], w), ({"3:speed": 1.5, "3:count": 4}, []))
        self.assertIsInstance(s["fields"]["3:count"], int)                              # 整数字段给的是 int，ComfyUI 的校验才过
        s, w = self.sel({"3:speed": 9, "3:count": 0})
        self.assertEqual(s["fields"], {"3:speed": 2.0, "3:count": 1})                    # 超出范围收回范围内，并且告诉用户
        self.assertEqual(len(w), 2)
        self.assertIn("超出范围", w[0])
        s, w = self.sel({"3:big": 10 ** 9})
        self.assertEqual((s["fields"], w), ({"3:big": 10 ** 9}, []))                      # 没有上限就不夹
        self.assertEqual(self.sel({"3:count": " 5 "})[0]["fields"], {"3:count": 5})

    def test_out_of_range_message_names_only_the_bounds_that_exist(self):
        s, w = self.sel({"3:big": -5})
        self.assertEqual((s["fields"], w), ({"3:big": 0}, ["「不限上限」超出范围（不小于 0），已调整为 0"]))
        s, w = self.sel({"3:speed": 9})
        self.assertEqual(w, ["「语速」超出范围（0.5~2），已调整为 2"])
        self.assertEqual(cat_mod.range_text({"min": None, "max": 8}), "不大于 8")

    def test_garbage_numbers_are_dropped_with_a_warning(self):
        for bad in ("快一点", True, False, None, [], {}, float("nan"), float("inf"), "inf", "nan", "1e999", "", "  ", "一"):
            s, w = self.sel({"3:speed": bad})
            self.assertEqual(s["fields"], {}, repr(bad))
            self.assertEqual(len(w), 1, repr(bad))

    def test_switches_accept_the_usual_spellings(self):
        for yes in (True, 1, "true", "True", "是", "开", "开启", "打开", "on", "yes", "1"):
            self.assertEqual(self.sel({"3:loop": yes})[0]["fields"], {"3:loop": True}, repr(yes))
        for no in (False, 0, "false", "否", "关", "关闭", "off", "no", "0"):
            self.assertEqual(self.sel({"3:loop": no})[0]["fields"], {"3:loop": False}, repr(no))
        for bad in (2, "也许", None, [], 0.5):
            s, w = self.sel({"3:loop": bad})
            self.assertEqual((s["fields"], len(w)), ({}, 1), repr(bad))


def assets_of(*items, usable=None):
    """素材库（和 chat.normalize_assets 的结构一样）：items 是文件名或 (文件名, 说明)；usable 是还在的文件名，默认全都在。"""
    out = chat.normalize_assets([{"name": i[0], "label": i[1]} if isinstance(i, tuple) else {"name": i} for i in items])
    for a in out:
        a["usable"] = bool(a["name"]) and (usable is None or a["name"] in usable)          # 名字不合规的（normalize 后是空名字）本来就不能用
    return out


class ChatTurn(unittest.TestCase):
    STEP = {"workflow": "07", "mode": 1, "fields": {"70:idea": "新鲜红苹果，产地直发，脆甜多汁"}, "files": {}, "note": "写文案"}
    PLAN = {"steps": [STEP], "note": "只写文案"}

    def turn(self, post, messages=None, assets=(), vision=(), plan=None):
        payload = {"messages": messages or [{"role": "user", "content": "帮我写苹果的文案"}], "plan": plan}
        return chat.chat_turn(payload, CAT, "sk-test-key-123", "https://dashscope.aliyuncs.com", list(assets), list(vision), post=post)

    def test_plan_is_validated_and_carries_schema(self):
        post = FakePost(llm({"reply": "好的，用文案应用写。", "plan": self.PLAN}))
        out = self.turn(post)
        self.assertEqual(out["reply"], "好的，用文案应用写。")
        self.assertEqual((len(out["plan"]["steps"]), out["plan"]["note"]), (1, "只写文案"))
        p = out["plan"]["steps"][0]
        self.assertEqual((p["workflow"], p["name"], p["mode"]), ("07", "文案", CAT["07"]["mode"]["options"][0]))
        self.assertEqual(p["fields"], {"70:idea": "新鲜红苹果，产地直发，脆甜多汁"})
        self.assertEqual(p["note"], "写文案")
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
        self.assertEqual(out["plan"]["steps"][0]["workflow"], "07")
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

    def test_a_200_reply_without_text_does_not_leak_the_key_either(self):
        with self.assertRaises(ChatError) as cm:
            self.turn(FakePost(Resp(200, {"weird": "回显了 sk-test-key-123 和 sk-abc_DEF-123456"})))
        self.assertEqual(cm.exception.status, 502)
        self.assertNotIn("sk-test-key-123", str(cm.exception))
        self.assertNotIn("sk-abc_DEF-123456", str(cm.exception))
        self.assertIn("没有文字", str(cm.exception))

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
        for plan in ({"steps": [{"workflow": "01", "files": ["x"], "fields": "y"}]}, {"steps": [{"workflow": 1, "mode": ["a"], "fields": {"k": [1, {"a": 2}]}, "files": {"4:image": 5}}]},
                     {"steps": [{"workflow": "x", "files": None}, None, 5, "s", {"workflow": ""}]}, {"steps": "不是列表"}, {"steps": None}, {"workflow": "01"}, "整个是字符串", [], 7,
                     {"steps": [{"workflow": "03", "mode": True, "fields": {"70:idea": float("inf")}, "files": {"4:image": {"step": "x"}, "5:image": {"asset": float("nan")}}}]}):
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
        post = FakePost(llm({"reply": "试试这个", "plan": {"steps": [{"workflow": "99"}]}}))        # 只有一步的 steps 写法，提示一样
        self.assertEqual(self.turn(post)["warnings"], ["没有编号为「99」的应用，方案已丢弃"])

    def test_plan_without_a_usable_app_id_is_retried_once(self):
        bad = {"reply": "好", "plan": {"mode": 1, "fields": {"70:idea": "红苹果"}}}      # 漏了 workflow
        post = FakePost(llm(bad), llm({"reply": "好", "plan": self.PLAN}))
        out = self.turn(post)
        self.assertEqual(out["plan"]["steps"][0]["workflow"], "07")
        self.assertEqual(out["warnings"], [])
        self.assertEqual(len(post.calls), 2)
        sent = post.calls[1]["json"]["messages"]
        self.assertIn("没有写对 workflow", sent[-1]["content"])
        self.assertEqual(sent[-2]["role"], "assistant")                                  # 把它上一次的回复一起带上
        post = FakePost(llm(bad), llm(bad))
        out = self.turn(post)
        self.assertEqual((out["reply"], out["plan"], len(post.calls)), ("好", None, 2))
        self.assertEqual(out["warnings"], ["没有写应用编号，方案已丢弃"])

    def test_no_retry_when_the_model_gave_no_plan_on_purpose(self):
        post = FakePost(llm({"reply": "先点「附图」传一张商品图", "plan": None}))
        out = self.turn(post)
        self.assertEqual((out["plan"], out["warnings"], len(post.calls)), (None, [], 1))

    def test_app_id_is_found_under_alias_keys_and_loose_spellings(self):
        for raw in ({"workflow_id": "07"}, {"app_id": 7}, {"workflow": "7"}, {"workflow": "应用 07「文案」"}, {"app": " 07 "}, {"workflow": None, "id": "07"}):
            out = self.turn(FakePost(llm({"reply": "r", "plan": {**raw, "mode": 1, "fields": {}}})))                 # 老写法：plan 里直接写 workflow（当作只有一步）
            self.assertEqual(out["plan"]["steps"][0]["workflow"], "07", raw)
            out = self.turn(FakePost(llm({"reply": "r", "plan": {"steps": [{**raw, "mode": 1, "fields": {}}]}})))
            self.assertEqual(out["plan"]["steps"][0]["workflow"], "07", raw)
        for raw in ({"workflow": True}, {"workflow": ["07"]}, {"workflow": "0"}, {"workflow": "第七个"}):
            self.assertIsNone(self.turn(FakePost(llm({"reply": "r", "plan": raw})))["plan"], raw)

    def test_model_inventions_are_dropped(self):
        raw = {"reply": "r", "plan": {"workflow": "01", "mode": 7, "fields": {"70:idea": "猫", "2:ratio": "5:4", "88:x": "y"}, "files": {"6:image": 1}}}
        out = self.turn(FakePost(llm(raw)), assets=assets_of("助手/a.png"))
        p = out["plan"]["steps"][0]
        self.assertEqual(p["mode"], None)
        self.assertEqual(p["fields"], {"70:idea": "猫"})
        self.assertEqual(p["files"], {})
        self.assertEqual(len(out["warnings"]), 4)

    def test_asset_numbers_stay_numbers_and_vision_model_is_used(self):
        raw = {"reply": "r", "plan": {"steps": [{"workflow": "03", "mode": 1, "fields": {}, "files": {"4:image": 2, "5:image": 9}}]}}
        post = FakePost(llm(raw))
        out = self.turn(post, assets=assets_of("助手/a.png", ("助手/b.png", "红苹果主图")), vision=[(2, "data:image/jpeg;base64,AAA")])
        self.assertEqual(out["plan"]["steps"][0]["files"], {"4:image": {"asset": 2}})       # 方案里留编号（运行时前端再换成文件名）；9 号不存在
        self.assertEqual(len(out["warnings"]), 1)
        call = post.calls[0]["json"]
        self.assertEqual(call["model"], chat.VISION_MODEL)
        last = call["messages"][-1]["content"]
        self.assertEqual([c["type"] for c in last], ["image_url", "text"])
        self.assertIn("2 号", last[1]["text"])                                                # 告诉模型这张图是素材库几号
        self.assertIn("1. [图片] a.png", call["messages"][0]["content"])
        self.assertIn("2. [图片] 红苹果主图", call["messages"][0]["content"])                 # 有说明用说明

    def test_lost_asset_files_are_marked_and_never_used(self):
        raw = {"reply": "r", "plan": {"workflow": "03", "mode": 1, "fields": {}, "files": {"4:image": 1}}}
        post = FakePost(llm(raw))
        out = self.turn(post, assets=assets_of("助手/gone.png", usable=[]))
        self.assertEqual(out["plan"]["steps"][0]["files"], {})
        self.assertIn("文件已经不存在", post.calls[0]["json"]["messages"][0]["content"])

    def test_a_plan_that_forgets_a_required_file_gets_a_warning(self):
        out = self.turn(FakePost(llm({"reply": "r", "plan": {"steps": [{"workflow": "03", "mode": 1}]}})))
        self.assertEqual(out["plan"]["steps"][0]["files"], {})                              # 方案照样给出，让用户在卡片里补
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("「商品图」必须有一个图片", out["warnings"][0])
        out = self.turn(FakePost(llm({"reply": "r", "plan": {"steps": [{"workflow": "03", "mode": 1, "files": {"4:image": 1}}]}})), assets=assets_of("助手/a.png"))
        self.assertEqual(out["warnings"], [])
        out = self.turn(FakePost(llm({"reply": "r", "plan": {"steps": [{"workflow": "01"}, {"workflow": "04"}]}})))              # 多步：提示里带上是第几步
        self.assertTrue(out["warnings"][0].startswith("步骤 2：「商品图」必须有一个图片"))
        out = self.turn(FakePost(llm({"reply": "r", "plan": {"steps": [{"workflow": "01"}, {"workflow": "04", "files": {"2:image": "步骤1"}}]}})))
        self.assertEqual(out["warnings"], [])                                               # 前一步能做出图，算指定了

    def test_a_plan_missing_a_required_file_gets_one_chance_to_fix_it(self):
        bad = {"reply": "好", "plan": {"steps": [{"workflow": "03", "mode": 1, "fields": {"70:idea": "换白底"}}]}}
        good = {"reply": "那就先出图", "plan": {"steps": [{"workflow": "01", "fields": {"70:idea": "红苹果白底主图"}}]}}
        post = FakePost(llm(bad), llm(good))
        out = self.turn(post)
        self.assertEqual((out["plan"]["steps"][0]["workflow"], out["warnings"], len(post.calls)), ("01", [], 2))
        sent = post.calls[1]["json"]["messages"]
        self.assertIn("步骤 1 的「商品图」必须有文件", sent[-1]["content"])
        self.assertEqual(sent[-2]["role"], "assistant")
        post = FakePost(llm(bad))                                                          # 第二次还是漏了：不再追问，带着提示给用户（方案照给，让用户补）
        out = self.turn(post)
        self.assertEqual((out["plan"]["steps"][0]["workflow"], len(out["warnings"]), len(post.calls)), ("03", 1, 2))
        post = FakePost(llm(bad), llm({"reply": "请先点「附件」传一张商品图", "plan": None}))      # 第二次改成先问用户要图：接受
        out = self.turn(post)
        self.assertEqual((out["plan"], out["reply"], len(post.calls)), (None, "请先点「附件」传一张商品图", 2))
        ok = {"reply": "好", "plan": {"steps": [{"workflow": "03", "mode": 1, "files": {"4:image": 1}}]}}
        post = FakePost(llm(ok))
        self.assertEqual(len(self.turn(post, assets=assets_of("助手/a.png"))["plan"]["steps"]), 1)
        self.assertEqual(len(post.calls), 1)                                               # 指定了文件就不会多问一次

    def test_what_counts_as_a_missing_file_follows_the_chosen_mode(self):
        """16 的背景图只在「放到背景图上」模式必填：选了纯色模式、只给商品图，不能因为默认模式要背景图就追问；选背景图模式却没给背景图，要追问一次。"""
        solid = {"reply": "好", "plan": {"steps": [{"workflow": "16", "mode": 2, "files": {"2:image": 1}}]}}
        post = FakePost(llm(solid))
        out = self.turn(post, assets=assets_of("助手/cut.png"))
        self.assertEqual((out["warnings"], len(post.calls), out["plan"]["steps"][0]["mode"]), ([], 1, "放到纯色背景上"))
        on_bg = {"reply": "好", "plan": {"steps": [{"workflow": "16", "mode": 1, "files": {"2:image": 1}}]}}
        post = FakePost(llm(on_bg), llm(solid))
        out = self.turn(post, assets=assets_of("助手/cut.png"))
        self.assertEqual((len(post.calls), out["warnings"], out["plan"]["steps"][0]["mode"]), (2, [], "放到纯色背景上"))
        self.assertIn("步骤 1 的「背景图（背景图模式）」必须有文件", post.calls[1]["json"]["messages"][-1]["content"])
        post = FakePost(llm(on_bg))                                                          # 追问之后还是没有背景图：方案照给，带着提示
        out = self.turn(post, assets=assets_of("助手/cut.png"))
        self.assertEqual((len(post.calls), len(out["warnings"])), (2, 1))
        self.assertIn("「背景图（背景图模式）」必须有一个图片", out["warnings"][0])

    def test_an_asset_must_be_of_the_kind_the_control_wants(self):
        assets = assets_of("助手/a.png", "助手/v.mp4", "助手/m.mp3", "助手/x.txt", "../坏的.png")
        raw = {"reply": "r", "plan": {"workflow": "11", "mode": 3, "fields": {}, "files": {"2:file": 2, "4:audio": 3, "12:image": 1, "13:image": 2, "14:image": 4}}}
        out = self.turn(FakePost(llm(raw)), assets=assets)
        self.assertEqual(out["plan"]["steps"][0]["files"], {"2:file": {"asset": 2}, "4:audio": {"asset": 3}, "12:image": {"asset": 1}})
        self.assertEqual(len(out["warnings"]), 2)                                            # 视频放进图片控件、txt 都不行
        self.assertEqual(chat.assets_text(assets).splitlines()[-1], "5. [文件] （无名）（文件已经不存在，不要用）")      # 名字不合规的占着编号但不能用
        self.assertEqual(chat.assets_text([]), "（还没有）")

    def test_history_is_trimmed_and_current_plan_is_shown_to_the_model(self):
        msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}句"} for i in range(30)] + [{"role": "user", "content": "最后一句"}]
        plan = {"steps": [{"workflow": "01", "mode": CAT["01"]["mode"]["options"][1], "fields": {"70:idea": "橘猫"}, "files": {}}]}
        post = FakePost(llm({"reply": "ok", "plan": None}))
        self.turn(post, messages=msgs, plan=plan)
        sent = post.calls[0]["json"]["messages"]
        self.assertEqual(len(sent), 1 + chat.MAX_HISTORY)
        self.assertEqual(sent[-1]["content"], "最后一句")
        self.assertIn('"workflow": "01"', sent[0]["content"])
        self.assertIn('"mode": 2', sent[0]["content"])                                       # 模式换回编号给模型看
        self.assertIn("橘猫", sent[0]["content"])

    def test_malformed_requests_fail_cleanly_instead_of_crashing(self):
        """客户端传来的字段什么类型都可能有（网页被改过、旧版网页对新版后端……）：要么按没传处理，要么给 400，不能 500。"""
        weird = [None, 0, -1, 1.5, float("nan"), float("inf"), True, 10 ** 30, "x", "", "9" * 400, [], {}, [None], [[]], {"a": 1}, [{"name": 5}], [{"name": ["x"]}], ["助手/a.png"]]
        ok = FakePost(llm({"reply": "好", "plan": None}))
        for key in ("messages", "assets", "new_assets", "plan"):
            for w in weird:
                payload = {"messages": [{"role": "user", "content": "你好"}], "assets": [], "new_assets": [], "plan": None}
                payload[key] = w
                try:
                    chat.chat_turn(payload, CAT, "k", "b", chat.normalize_assets(payload["assets"]), [], post=ok)
                except ChatError:
                    pass                                                                           # 400 / 提示都行，别的异常不行

    def test_bad_requests(self):
        for payload in ({"messages": []}, {"messages": [{"role": "assistant", "content": "x"}]}, {"messages": [{"role": "user", "content": "  "}]}):
            with self.assertRaises(ChatError) as cm:
                chat.chat_turn(payload, CAT, "k", "b", [], [], post=FakePost(llm("{}")))
            self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(ChatError) as cm:
            chat.chat_turn({"messages": [{"role": "user", "content": "x"}]}, {}, "k", "b", [], [], post=FakePost(llm("{}")))
        self.assertEqual(cm.exception.status, 503)


class TestChatTurnHelper:
    @staticmethod
    def turn_with(post, assets):
        return chat.chat_turn({"messages": [{"role": "user", "content": "x"}]}, CAT, "k", "https://x", list(assets), [], post=post)


def ref_warns(warns):
    """去掉「必须有一个…方案里还没指定」这类必填提示，只留素材 / 步骤引用被丢掉的提示（这些测试关心的是引用）。"""
    return [w for w in warns if "必须有" not in w]


class MultiStep(unittest.TestCase):
    """多步方案：后一步的文件字段可以引用前面某一步的产出（步骤N / 步骤N.M），也可以引用素材库编号。"""

    def plan(self, steps, assets=(), note=""):
        post = FakePost(llm({"reply": "好", "plan": {"steps": steps, "note": note}}))
        payload = {"messages": [{"role": "user", "content": "做一套"}]}
        out = chat.chat_turn(payload, CAT, "sk", "https://x", list(assets), [], post=post)
        return out["plan"], out["warnings"], post

    def test_a_step_can_use_the_result_of_an_earlier_step(self):
        plan, warns, _ = self.plan([{"workflow": "01", "fields": {"70:idea": "红苹果"}}, {"workflow": "02", "mode": 2, "files": {"6:image": "步骤1"}},
                                    {"workflow": "02", "mode": 2, "files": {"6:image": "步骤1.2"}}], note="出图再改图")
        self.assertEqual([s["workflow"] for s in plan["steps"]], ["01", "02", "02"])
        self.assertEqual(plan["steps"][1]["files"], {"6:image": {"step": 1, "n": 1}})
        self.assertEqual(plan["steps"][2]["files"], {"6:image": {"step": 1, "n": 2}})
        self.assertEqual((warns, plan["note"]), ([], "出图再改图"))

    def test_loose_spellings_of_step_references(self):
        for ref, want in (("步骤1", {"step": 1, "n": 1}), ("步骤 1", {"step": 1, "n": 1}), ("步骤1.3", {"step": 1, "n": 3}), ("步骤 1 . 3", {"step": 1, "n": 3}), ("步骤1．2", {"step": 1, "n": 2}),
                          ("step1", {"step": 1, "n": 1}), ("Step 1.2", {"step": 1, "n": 2}), ("第1步", {"step": 1, "n": 1}), (" 步骤1 ", {"step": 1, "n": 1}),
                          ({"step": 1, "n": 2}, {"step": 1, "n": 2}), ({"step": "1"}, {"step": 1, "n": 1})):
            plan, warns, _ = self.plan([{"workflow": "01"}, {"workflow": "02", "mode": 2, "files": {"6:image": ref}}])
            self.assertEqual(plan["steps"][1]["files"], {"6:image": want}, repr(ref))

    def test_bad_references_are_dropped_with_a_warning_and_never_crash(self):
        for bad in ("步骤2", "步骤0", "步骤-1", "步骤1.0", "步骤99999999999999999999", "第0步", "步骤", "步骤一", "1.5", True, None, [], {}, {"step": 0}, {"step": "x"},
                    {"step": 1, "n": 0}, {"asset": 9}, 0, -3, 1.5, float("nan"), float("inf"), "②", "9" * 50):
            plan, warns, _ = self.plan([{"workflow": "01"}, {"workflow": "02", "mode": 2, "files": {"6:image": bad}}])
            self.assertEqual(plan["steps"][1]["files"], {}, repr(bad))
            self.assertEqual(len(ref_warns(warns)), 1, repr(bad))
            self.assertTrue(ref_warns(warns)[0].startswith("步骤 2："), repr(bad))            # 提示里带上是第几步

    def test_a_step_can_only_use_earlier_steps_that_really_produce_that_kind(self):
        plan, warns, _ = self.plan([{"workflow": "02", "mode": 2, "files": {"6:image": "步骤1"}}, {"workflow": "01"}])        # 自己引用自己
        self.assertEqual((plan["steps"][0]["files"], len(ref_warns(warns))), ({}, 1))
        plan, warns, _ = self.plan([{"workflow": "02", "mode": 2, "files": {"6:image": "步骤2"}}, {"workflow": "01"}])        # 引用排在后面的
        self.assertEqual((plan["steps"][0]["files"], len(ref_warns(warns))), ({}, 1))
        plan, warns, _ = self.plan([{"workflow": "07", "mode": 1}, {"workflow": "02", "mode": 2, "files": {"6:image": "步骤1"}}])  # 文案只出文字，不能当图
        self.assertEqual((plan["steps"][1]["files"], len(ref_warns(warns))), ({}, 1))
        plan, warns, _ = self.plan([{"workflow": "01"}, {"workflow": "11", "mode": 1, "files": {"2:file": "步骤1"}}])           # 图不能当视频
        self.assertEqual((plan["steps"][1]["files"], len(ref_warns(warns))), ({}, 1))
        plan, warns, _ = self.plan([{"workflow": "10", "mode": 1}, {"workflow": "11", "mode": 1, "files": {"2:file": "步骤1"}}])  # 视频可以交给成片
        self.assertEqual((plan["steps"][1]["files"], warns), ({"2:file": {"step": 1, "n": 1}}, []))
        plan, warns, _ = self.plan([{"workflow": "08", "mode": 1}, {"workflow": "11", "mode": 1, "files": {"4:audio": "步骤1"}}])  # 配音（音频）可以当背景音乐
        self.assertEqual((plan["steps"][1]["files"], ref_warns(warns)), ({"4:audio": {"step": 1, "n": 1}}, []))              # （这一步缺的上传视频是另一回事：会有一条必填提示）
        self.assertEqual(len(warns), 1)

    def test_dropping_a_step_renumbers_the_rest_and_references_follow(self):
        plan, warns, _ = self.plan([{"workflow": "01"}, {"workflow": "99"}, {"workflow": "02", "mode": 2, "files": {"6:image": "步骤1"}},
                                    {"workflow": "02", "mode": 2, "files": {"6:image": "步骤3"}}, {"workflow": "02", "mode": 2, "files": {"6:image": "步骤2"}}])
        self.assertEqual([s["workflow"] for s in plan["steps"]], ["01", "02", "02", "02"])
        self.assertEqual(plan["steps"][1]["files"], {"6:image": {"step": 1, "n": 1}})
        self.assertEqual(plan["steps"][2]["files"], {"6:image": {"step": 2, "n": 1}})        # 模型写的「步骤3」现在是第 2 步
        self.assertEqual(plan["steps"][3]["files"], {})                                       # 它引用的「步骤2」被丢掉了
        self.assertEqual(len(ref_warns(warns)), 2)
        self.assertTrue(ref_warns(warns)[0].startswith("步骤 2："))
        self.assertTrue(ref_warns(warns)[1].startswith("步骤 5："))

    def test_at_most_six_steps(self):
        plan, warns, _ = self.plan([{"workflow": "01"}] * 8)
        self.assertEqual(len(plan["steps"]), chat.MAX_STEPS)
        self.assertEqual(warns, [f"最多 {chat.MAX_STEPS} 步，后面的已忽略"])

    def test_steps_that_are_not_objects_are_dropped(self):
        plan, warns, _ = self.plan([None, 5, "x", [], {"workflow": "01"}])
        self.assertEqual([s["workflow"] for s in plan["steps"]], ["01"])
        self.assertEqual(len(warns), 4)

    def test_asset_references_accept_numbers_digits_and_dicts(self):
        assets = assets_of("助手/a.png", "助手/b.png", "助手/c.png")
        for ref in (2, "2", " 2 ", 2.0, {"asset": 2}, {"asset": "2"}):
            plan, warns, _ = self.plan([{"workflow": "02", "mode": 2, "files": {"6:image": ref}}], assets)
            self.assertEqual(plan["steps"][0]["files"], {"6:image": {"asset": 2}}, repr(ref))
        plan, warns, _ = self.plan([{"workflow": "03", "mode": 2, "files": {"4:image": 1, "5:image": "步骤1"}}, {"workflow": "01"}], assets)
        self.assertEqual(plan["steps"][0]["files"], {"4:image": {"asset": 1}})              # 同一步里「步骤1」是自己，不行
        self.assertEqual(len(ref_warns(warns)), 1)

    def test_a_plan_with_both_steps_and_a_flat_workflow_uses_the_steps(self):
        plan, warns, _ = self.plan([{"workflow": "07", "mode": 1}])
        post = FakePost(llm({"reply": "好", "plan": {"workflow": "01", "steps": [{"workflow": "07", "mode": 1}]}}))
        out = chat.chat_turn({"messages": [{"role": "user", "content": "x"}]}, CAT, "sk", "https://x", [], [], post=post)
        self.assertEqual([s["workflow"] for s in out["plan"]["steps"]], ["07"])

    def test_references_survive_the_round_trip_to_the_model_and_back(self):
        plan = {"steps": [{"workflow": "01", "mode": CAT["01"]["mode"]["options"][0], "fields": {"70:idea": "红苹果"}, "files": {}},
                          {"workflow": "03", "mode": CAT["03"]["mode"]["options"][1], "fields": {"70:idea": "换白底"},
                           "files": {"4:image": {"step": 1, "n": 2}, "5:image": {"asset": 3}}}]}
        text = json.loads(chat.plan_text(plan, CAT))
        self.assertEqual([s["step"] for s in text["steps"]], [1, 2])
        self.assertEqual(text["steps"][1]["files"], {"4:image": "步骤1.2", "5:image": 3})
        self.assertEqual(text["steps"][1]["mode"], 2)
        assets = assets_of("助手/a.png", "助手/b.png", "助手/c.png")
        back, warns, _ = self.plan([{"workflow": s["workflow"], "mode": s["mode"], "fields": s["fields"], "files": s["files"]} for s in text["steps"]], assets)
        self.assertEqual(back["steps"][1]["files"], plan["steps"][1]["files"])                 # 模型原样抄回来，得到同样的引用
        self.assertEqual(back["steps"][1]["mode"], plan["steps"][1]["mode"])
        self.assertEqual(warns, [])

    def test_the_system_prompt_lists_assets_and_the_current_plan(self):
        assets = assets_of(("助手/a.png", "步骤1 文生图 第2张"), "助手/m.mp3")
        text = chat.system_prompt(CAT, assets, {"steps": [{"workflow": "07", "fields": {"70:idea": "红苹果"}}]})
        self.assertIn("1. [图片] 步骤1 文生图 第2张", text)
        self.assertIn("2. [音频] m.mp3", text)
        self.assertIn('"workflow": "07"', text)
        self.assertIn("步骤N", text)
        self.assertIn("应用 11「视频成片」", text)
        self.assertIn("（还没有）", chat.system_prompt(CAT, [], None))

    def test_only_the_recent_assets_are_listed_for_the_model_but_numbers_stay(self):
        assets = assets_of(*[f"助手/{i}.png" for i in range(1, 61)])
        lines = chat.assets_text(assets).splitlines()
        self.assertEqual(len(lines), chat.ASSETS_SHOWN + 1)
        self.assertIn("共 60 个，前 20 个比较旧", lines[0])
        self.assertTrue(lines[1].startswith("21. [图片]"))
        self.assertTrue(lines[-1].startswith("60. [图片]"))
        out = TestChatTurnHelper.turn_with(FakePost(llm({"reply": "r", "plan": {"steps": [{"workflow": "02", "mode": 2, "files": {"6:image": 3}}]}})), assets)
        self.assertEqual(out["plan"]["steps"][0]["files"], {"6:image": {"asset": 3}})            # 没列出的旧素材照样能按编号引用

    def test_assets_are_numbered_by_position_and_capped(self):
        raw = [{"name": f"助手/{i}.png"} for i in range(chat.MAX_ASSETS + 10)]
        self.assertEqual(len(chat.normalize_assets(raw)), chat.MAX_ASSETS)
        weird = chat.normalize_assets([None, "x", 5, {"name": "../x.png"}, {"name": "助手/ok.PNG", "label": "标签" * 100}, {"name": ["助手/a.png"]}])
        self.assertEqual([a["name"] for a in weird], ["", "", "", "", "助手/ok.PNG", ""])      # 不合规的占位置、不能用，编号不会错位
        self.assertEqual(weird[4]["kind"], "image")
        self.assertEqual(len(weird[4]["label"]), 60)
        self.assertEqual(chat.normalize_assets("不是列表"), [])


class Run(unittest.TestCase):
    def prep(self, app_id, payload, exists=lambda n: True):
        return run.prepare_run(CAT[app_id], load_wf(app_id), OI, payload, exists)

    def check_api_values(self, api):
        """照 ComfyUI 的规则（简化版）检查 API prompt 里每个非连线的控件值：下拉选项（严格）、数字范围、开关、文字。返回问题列表。"""
        bad = []
        for nid, node in api.items():
            spec = OI[node["class_type"]]["input"]
            defs = {**spec.get("required", {}), **spec.get("optional", {})}
            for k, v in node["inputs"].items():
                if isinstance(v, list):
                    continue
                if k not in defs and "." in k:                                       # 保存音频的 format.quality 这类动态下拉的子项
                    continue
                d = defs[k]
                typ, meta = d[0], (d[1] if len(d) > 1 and isinstance(d[1], dict) else {})
                where = f"{nid}.{k}={v!r}"
                if isinstance(typ, list):
                    if v not in typ and not meta.get("image_upload"):
                        bad.append(where + " 不在下拉选项里")
                elif typ in ("INT", "FLOAT"):
                    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < meta.get("min", v) or v > meta.get("max", v):
                        bad.append(where + " 数字不对")
                elif typ == "BOOLEAN" and not isinstance(v, bool):
                    bad.append(where + " 不是开关")
                elif typ == "STRING" and not isinstance(v, str):
                    bad.append(where + " 不是文字")
        return bad

    def test_layer_apps_produce_valid_api_prompts_with_the_users_choices(self):
        for app_id, files in (("15", {"2:image": "助手/p.png"}), ("16", {"2:image": "助手/p.png", "3:image": "助手/bg.png"})):   # 什么都不改：默认值合法（商品图必填；16 默认「放到背景图上」要背景图）
            api, _, warns = self.prep(app_id, {"mode": None, "fields": {}, "files": files}, lambda n: True)
            self.assertEqual((warns, self.check_api_values(api)), ([], []), app_id)
        api, sel, warns = self.prep("15", {"fields": {"3:model": "更利落（isnet，约 170MB：边缘更干净，但画面里别的物体也会留下，更占内存）", "3:edge": "强（边缘更紧）", "4:trim": False}, "files": {"2:image": "助手/p.png"}}, lambda n: True)
        self.assertEqual(warns, [])
        self.assertEqual((api["3"]["inputs"]["model"], api["3"]["inputs"]["edge"], api["4"]["inputs"]["trim"]), ("更利落（isnet，约 170MB：边缘更干净，但画面里别的物体也会留下，更占内存）", "强（边缘更紧）", False))
        self.assertEqual(self.check_api_values(api), [])
        fields = {"5:position": "右下", "5:scale_pct": 45, "5:shadow": "柔和投影", "5:bg_color": "#F5F0E6", "5:ratio": "3:4", "5:save_layers": True, "5:offset_y_pct": -8}
        api, sel, warns = self.prep("16", {"mode": 2, "fields": fields, "files": {"2:image": "助手/p.png"}}, lambda n: True)
        self.assertEqual(warns, [])
        d = api["5"]["inputs"]
        self.assertEqual((d["position"], d["scale_pct"], d["shadow"], d["bg_color"], d["ratio"], d["save_layers"], d["offset_y_pct"]), ("右下", 45, "柔和投影", "#F5F0E6", "3:4", True, -8))
        self.assertEqual(self.check_api_values(api), [])
        self.assertEqual((api["77"]["inputs"]["mode"], api["21"]["inputs"]["enabled"]), ("放到纯色背景上", ["77", 0]))   # 纯色模式：背景图开关跟模式走（模式节点输出 False，不读背景图）
        api, _, _ = self.prep("16", {"mode": 1, "fields": {}, "files": {"2:image": "助手/p.png", "3:image": "助手/bg.png"}}, lambda n: True)
        self.assertEqual(api["77"]["inputs"]["mode"], "放到背景图上")
        self.assertEqual(api["3"]["inputs"]["image"], "助手/bg.png")
        with self.assertRaises(ChatError):                                                              # 商品图必填
            self.prep("16", {"mode": 1, "fields": {}, "files": {}}, lambda n: True)

    def test_dialogue_apps_produce_valid_api_prompts_with_the_users_choices(self):
        fields = {"3:script": "小美：你好呀\n阿强：你好\n旁白：开始了", "3:role1_name": "小美", "3:role1_voice": "Serena", "3:role2_name": "阿强", "3:role2_voice": "Ryan（男 · 节奏感强、戏感足）",
                  "3:role3_name": "路人", "3:role3_voice": "Eldric Sage", "3:narrator_voice": "Neil", "3:speed": 1.2, "3:gap_s": 0.5, "3:show_names": False, "3:model": "qwen3-tts-instruct-flash", "3:role1_style": "语气温柔"}
        for app_id, mode, files in (("13", None, {}), ("14", 2, {"12:image": "助手/a.png"})):                      # 14 的图片轮播模式要第 1 张图
            api, sel, warns = self.prep(app_id, {"mode": mode, "fields": fields, "files": files})
            self.assertEqual(warns, [], app_id)
            self.assertEqual(self.check_api_values(api), [], app_id)
            d = api["3"]["inputs"]
            self.assertEqual((d["role1_voice"], d["role2_voice"], d["role3_voice"]), ("Serena · 女 · 温柔", "Ryan · 男 · 节奏感强、戏感足", "Eldric Sage · 男 · 沉稳睿智的老者"))
            self.assertEqual((d["speed"], d["gap_s"], d["show_names"], d["model"], d["role1_style"]), (1.2, 0.5, False, "qwen3-tts-instruct-flash", "语气温柔"))
            self.assertTrue(d["script"].startswith("小美：你好呀"))
        # 什么都不改：默认值本身就合法
        for app_id, files in (("13", {}), ("14", {"2:file": "助手/v.mp4"})):                                      # 14 默认模式（对白配到上传的视频上）要视频
            api, _, warns = self.prep(app_id, {"mode": None, "fields": {}, "files": files})
            self.assertEqual((warns, self.check_api_values(api)), ([], []), app_id)
        # 14 的两个模式各自用对了视频来源
        api, _, _ = self.prep("14", {"mode": 1, "fields": {}, "files": {"2:file": "助手/x.mp4"}})
        self.assertEqual(api["2"]["inputs"]["file"], "助手/x.mp4")

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
            files = {"2:file": "助手/v.mp4"} if app_id == "11" else {}                       # 11 默认模式（上传视频）要视频
            api, sel, warns = self.prep(app_id, {"fields": {"3:voice": "Ethan"}, "files": files})
            self.assertEqual((api["3"]["inputs"]["voice"], sel["fields"], warns), ("Ethan", {"3:voice": "Ethan"}, []), app_id)
            api, sel, warns = self.prep(app_id, {"fields": {"3:voice": "不存在的音色"}, "files": files})
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

    def test_files_only_from_the_assistant_folder_that_exist(self):
        ok = lambda n: n == "助手/a.png"
        api, sel, warns = self.prep("02", {"files": {"6:image": "助手/a.png"}}, ok)
        self.assertEqual(api["6"]["inputs"]["image"], "助手/a.png")
        api, sel, warns = self.prep("02", {"files": {"6:image": "助手/不存在.png"}}, ok)       # 文件不存在：这个控件用默认图（02 的商品图只在「带商品图」模式才用，不是必填）
        self.assertEqual(api["6"]["inputs"]["image"], "demo_product.png")
        self.assertTrue(warns)

    def test_required_files_must_be_given_or_the_run_is_refused(self):
        for app_id, key in (("03", "4:image"), ("04", "2:image"), ("05", "2:image"), ("06", "3:image"), ("12", "2:image"), ("15", "2:image"), ("16", "2:image")):
            self.assertTrue(CAT[app_id]["files"][key].get("required"), app_id)
            for payload in ({}, {"files": {}}, {"files": {key: "助手/不存在.png"}}, {"files": {key: "助手/../x.png"}}):
                with self.assertRaises(ChatError, msg=f"{app_id} {payload}") as cm:
                    self.prep(app_id, payload, lambda n: n == "助手/ok.png")
                self.assertEqual(cm.exception.status, 400)
                self.assertIn("必须指定", str(cm.exception))
            extra = {"mode": CAT[app_id]["mode"]["options"][1]} if app_id == "16" else {}                 # 16 的默认模式还要背景图，这里只看商品图：用纯色模式
            api, sel, warns = self.prep(app_id, dict(extra, files={key: "助手/ok.png"}), lambda n: n == "助手/ok.png")
            self.assertEqual(api[key.split(":")[0]]["inputs"]["image"], "助手/ok.png")
        for app_id in ("01", "02", "07", "08", "09", "10", "11", "13", "14"):                   # 只在某些模式才要文件（或根本不要）的应用：「不管什么模式都必填」里没有它们
            self.assertFalse(any(f.get("required") for f in CAT[app_id]["files"].values()), app_id)
        self.prep("10", {}, lambda n: True)

    def test_files_required_only_in_some_modes_are_refused_only_in_those_modes(self):
        ok = lambda n: n.startswith("助手/")
        for app_id, key, modes in (("02", "6:image", [2]), ("03", "5:image", [2, 4]), ("07", "3:image", [2]), ("08", "6:audio", [3]), ("11", "2:file", [1, 2]), ("11", "12:image", [3]),
                                   ("14", "2:file", [1]), ("14", "12:image", [2]), ("16", "3:image", [1])):
            f = CAT[app_id]["files"][key]
            self.assertEqual((f.get("required"), f.get("required_modes")), (None, modes), f"{app_id} {key}")
            opts = CAT[app_id]["mode"]["options"]
            have = {k: "助手/x.png" for k, g in CAT[app_id]["files"].items() if g.get("required")}                 # 不管什么模式都要的先给上
            others = {"2:file": "助手/v.mp4", "6:audio": "助手/a.mp3"}
            for n, text in enumerate(opts, 1):
                supply = dict(have)
                with_it = dict(supply, **{key: others.get(key, "助手/x.png")})
                if n in modes:                                                                             # 要这个文件的模式：不给就拒绝，给了就能跑
                    with self.assertRaises(ChatError, msg=f"{app_id} 模式 {n} 缺 {key}") as cm:
                        self.prep(app_id, {"mode": text, "files": supply}, ok)
                    self.assertIn(f["label"], str(cm.exception))
                    self.prep(app_id, {"mode": text, "files": with_it}, ok)
                else:                                                                                      # 不要它的模式：不给也能跑（别的模式要的文件另说，这里只看这一个文件）
                    try:
                        self.prep(app_id, {"mode": text, "files": supply}, ok)
                    except ChatError as e:
                        self.assertNotIn(f["label"], str(e), f"{app_id} 模式 {n} 不该要 {key}")
        # 默认模式（没写 mode）按第 1 个模式算
        with self.assertRaises(ChatError):
            self.prep("16", {"files": {"2:image": "助手/p.png"}}, ok)
        self.prep("16", {"mode": CAT["16"]["mode"]["options"][1], "files": {"2:image": "助手/p.png"}}, ok)
        # 目录里写明白：什么时候必填
        text = cat_mod.catalog_text(CAT)
        self.assertIn("（选模式 1 时必填）", text)
        self.assertIn("（选模式 2、4 时必填）", text)

    def test_file_name_rules_hold_even_if_the_file_check_says_yes(self):
        anything = lambda n: True                                                         # 文件检查放行一切：名字规则自己必须挡住
        api, sel, warns = self.prep("02", {"files": {"6:image": "助手/photo..png"}}, anything)
        self.assertEqual(api["6"]["inputs"]["image"], "助手/photo..png")                  # 合法文件名（含两个点）不能误杀
        for bad in ("助手/../../etc/passwd", "other.png", "../助手/a.png", "助手\\x.png", "助手//x.png", "助手/./x.png", "助手/", "助手/a\x00.png", "", None):
            api, sel, warns = self.prep("02", {"files": {"6:image": bad}}, anything)
            self.assertEqual(api["6"]["inputs"]["image"], "demo_product.png", repr(bad))
            self.assertEqual(sel["files"], {}, repr(bad))
            self.assertTrue(warns, repr(bad))

    def test_video_and_audio_files_are_filled_and_the_kind_must_match_the_control(self):
        anything = lambda n: True
        api, sel, warns = self.prep("11", {"mode": CAT["11"]["mode"]["options"][0], "files": {"2:file": "助手/v.mp4", "4:audio": "助手/m.mp3"}}, anything)
        self.assertEqual((api["2"]["inputs"]["file"], api["4"]["inputs"]["audio"], warns), ("助手/v.mp4", "助手/m.mp3", []))
        base = run.convert(load_wf("11"), OI)
        m3 = CAT["11"]["mode"]["options"][2]                                                                                    # 图片轮播：只要求第 1 张图，视频 / 音乐控件可以不给
        api, sel, warns = self.prep("11", {"mode": m3, "files": {"12:image": "助手/i.png", "2:file": "助手/a.png", "4:audio": "助手/v.mp4"}}, anything)   # 图片放进视频控件、视频放进音频控件：不收
        self.assertEqual((api["2"]["inputs"]["file"], api["4"]["inputs"]["audio"]), (base["2"]["inputs"]["file"], base["4"]["inputs"]["audio"]))
        self.assertEqual((sel["files"], len(warns)), ({"12:image": "助手/i.png"}, 2))
        api, sel, warns = self.prep("11", {"mode": m3, "files": {"12:image": "助手/i.png", "2:file": "助手/x.exe", "4:audio": "助手/m.txt"}}, anything)   # 不认识的扩展名也不收
        self.assertEqual((sel["files"], len(warns)), ({"12:image": "助手/i.png"}, 2))
        with self.assertRaises(ChatError):                                                                                      # 上传视频模式下，图片放进视频控件被丢掉 → 视频没给 → 不让运行
            self.prep("11", {"files": {"2:file": "助手/a.png"}}, anything)

    def test_numbers_and_switches_are_filled_into_the_api_prompt(self):
        wf = load_wf("08")
        wf["extra"]["linearData"]["inputs"] += [[3, "speed"]]
        app = cat_mod.load_app(wf, "08-配音.app.json", OI)
        base = run.convert(wf, OI)
        self.assertEqual(base["3"]["inputs"]["speed"], 1.0)
        api, sel, warns = run.prepare_run(app, wf, OI, {"fields": {"3:speed": "1.5"}}, lambda n: True)
        self.assertEqual((api["3"]["inputs"]["speed"], sel["fields"], warns), (1.5, {"3:speed": 1.5}, []))
        api, sel, warns = run.prepare_run(app, wf, OI, {"fields": {"3:speed": 9}}, lambda n: True)
        self.assertEqual((api["3"]["inputs"]["speed"], len(warns)), (2.0, 1))                # 超出范围收回上限
        api, sel, warns = run.prepare_run(app, wf, OI, {"fields": {"3:speed": "很快"}}, lambda n: True)
        self.assertEqual((api["3"]["inputs"]["speed"], len(warns)), (1.0, 1))                # 不是数字：保持默认

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
        self.out = os.path.join(self.tmp, "output")
        os.makedirs(os.path.join(self.inp, "助手"))
        os.makedirs(os.path.join(self.out, "pro"))
        from PIL import Image
        Image.new("RGB", (1500, 900), (200, 30, 30)).save(os.path.join(self.inp, "助手", "a.png"))
        write_text(os.path.join(self.tmp, "secret.txt"), "机密")
        self.cfg = os.path.join(self.tmp, "relay_config.json")
        write_text(self.cfg, json.dumps({"node_settings": {"31": {"api_key": "sk-glue-key-999"}, "1": {"api_key": "sk-gateway-key"}}}))
        self.saved = {k: getattr(pc, k) for k in ("workflows_dir", "input_dir", "output_dir", "comfy_port", "relay_config_path", "object_info")}
        pc.workflows_dir, pc.input_dir, pc.output_dir, pc.comfy_port = (lambda: WF_DIR), (lambda: self.inp), (lambda: self.out), (lambda: 8188)
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

    def test_asset_paths_are_confined_to_the_assistant_folder(self):
        self.assertTrue(pc.asset_exists("助手/a.png"))
        for bad in ("助手/../secret.txt", "../secret.txt", "secret.txt", "助手/不存在.png", "助手\\a.png", "/etc/passwd", "", None, "助手/", "助手", "助手/a\x00.png"):
            self.assertFalse(pc.asset_exists(bad), bad)
        os.symlink(os.path.join(self.tmp, "secret.txt"), os.path.join(self.inp, "助手", "link.png"))     # 符号链接指到目录外也不行
        self.assertFalse(pc.asset_exists("助手/link.png"))
        os.makedirs(os.path.join(self.inp, "private"))                                    # input/ 里、但不在 助手/ 里的文件，符号链接过去也不行
        shutil.copy(os.path.join(self.inp, "助手", "a.png"), os.path.join(self.inp, "private", "x.png"))
        os.symlink(os.path.join(self.inp, "private", "x.png"), os.path.join(self.inp, "助手", "link2.png"))
        self.assertFalse(pc.asset_exists("助手/link2.png"))
        os.symlink(os.path.join(self.inp, "private"), os.path.join(self.inp, "助手", "dirlink"))        # 目录符号链接同理
        self.assertFalse(pc.asset_exists("助手/dirlink/x.png"))
        shutil.copy(os.path.join(self.inp, "助手", "a.png"), os.path.join(self.inp, "助手", "photo..png"))
        self.assertTrue(pc.asset_exists("助手/photo..png"))                               # 文件名里有两个点是合法的
        write_text(os.path.join(self.inp, "助手", "notes.txt"), "不是图片")
        self.assertTrue(pc.asset_exists("助手/notes.txt"))                                # 这一层只管「在不在目录里」；能不能当图片用见下一个测试

    def test_an_undecodable_image_is_skipped_with_a_warning_not_a_500(self):
        write_text(os.path.join(self.inp, "助手", "bad.png"), "这不是图片")
        post = FakePost(llm({"reply": "好", "plan": None}))
        out = pc.handle_chat({"messages": [{"role": "user", "content": "看看这些图"}], "assets": [{"name": "助手/a.png"}, {"name": "助手/bad.png"}], "new_assets": [1, 2]}, post=post)
        self.assertEqual(out["reply"], "好")
        self.assertEqual([w for w in out["warnings"] if "打不开" in w], ["图片「bad.png」打不开，已忽略（换 PNG / JPG 再传）"])
        sent = post.calls[0]["json"]
        self.assertEqual(sent["model"], chat.VISION_MODEL)                                # 好的那张照样送去看图
        self.assertEqual(len([c for c in sent["messages"][-1]["content"] if c["type"] == "image_url"]), 1)
        self.assertIn("2. [图片] bad.png（文件已经不存在，不要用）", sent["messages"][0]["content"])   # 坏图占着编号，但不让模型用
        post = FakePost(llm({"reply": "好", "plan": None}))                                # 只有坏图：退回文字模型
        out = pc.handle_chat({"messages": [{"role": "user", "content": "看看"}], "assets": [{"name": "助手/bad.png"}], "new_assets": [1]}, post=post)
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
        raw = {"reply": "做一张海报", "plan": {"steps": [{"workflow": "02", "mode": 2, "fields": {"70:idea": "夏日促销，满199减50"}, "files": {"6:image": 1}}]}}
        post = FakePost(llm(raw))
        out = pc.handle_chat({"messages": [{"role": "user", "content": "做个夏日海报"}], "assets": [{"name": "助手/a.png"}], "new_assets": [1]}, post=post)
        self.assertEqual(out["plan"]["steps"][0]["workflow"], "02")
        self.assertEqual(out["plan"]["steps"][0]["files"], {"6:image": {"asset": 1}})
        sent = post.calls[0]
        self.assertEqual(sent["json"]["model"], chat.VISION_MODEL)
        self.assertTrue(sent["json"]["messages"][-1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg"))
        self.assertIn("1 号", sent["json"]["messages"][-1]["content"][-1]["text"])
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

    def test_handle_chat_ignores_odd_new_assets_values(self):
        weird = [None, 0, -1, 1.5, float("nan"), True, 10 ** 30, "x", "", {}, {"a": 1}, [None], [[]], [{}], [True, False, 0, -1, 99, "1", 1.0, 1, 1]]
        for w in weird:
            post = FakePost(llm({"reply": "好", "plan": None}))
            out = pc.handle_chat({"messages": [{"role": "user", "content": "看看"}], "assets": [{"name": "助手/a.png"}], "new_assets": w}, post=post)
            self.assertEqual(out["reply"], "好", repr(w))
        post = FakePost(llm({"reply": "好", "plan": None}))
        pc.handle_chat({"messages": [{"role": "user", "content": "看看"}], "assets": [{"name": "助手/a.png"}], "new_assets": [True, 99, "1", 1, 1.0, 1]}, post=post)
        self.assertEqual([c["type"] for c in post.calls[0]["json"]["messages"][-1]["content"]], ["image_url", "text"])   # 只有那个真的 1 被当成新图（且只算一次）
        for w in (5, "x", {"a": 1}, [None], None):
            with self.assertRaises(ChatError) as cm:
                pc.handle_chat({"messages": w, "assets": [], "new_assets": []}, post=post)
            self.assertEqual(cm.exception.status, 400, repr(w))

    def test_video_and_audio_assets_are_not_sent_to_the_vision_model(self):
        write_text(os.path.join(self.inp, "助手", "v.mp4"), "假的视频")
        write_text(os.path.join(self.inp, "助手", "m.mp3"), "假的音频")
        post = FakePost(llm({"reply": "好", "plan": None}))
        out = pc.handle_chat({"messages": [{"role": "user", "content": "用这个视频"}], "assets": [{"name": "助手/v.mp4"}, {"name": "助手/m.mp3"}], "new_assets": [1, 2]}, post=post)
        sent = post.calls[0]["json"]
        self.assertEqual((sent["model"], out["warnings"]), (chat.TEXT_MODEL, []))
        self.assertIsInstance(sent["messages"][-1]["content"], str)
        self.assertIn("1. [视频] v.mp4", sent["messages"][0]["content"])
        self.assertIn("2. [音频] m.mp3", sent["messages"][0]["content"])

    def test_handle_run_fills_video_and_audio_files_from_the_assistant_folder(self):
        write_text(os.path.join(self.inp, "助手", "v.mp4"), "假的视频")
        write_text(os.path.join(self.inp, "助手", "m.mp3"), "假的音频")
        post = FakePost(Resp(200, {"prompt_id": "p-9", "number": 1, "node_errors": {}}))
        out = pc.handle_run({"workflow": "11", "mode": CAT["11"]["mode"]["options"][0], "files": {"2:file": "助手/v.mp4", "4:audio": "助手/m.mp3", "12:image": "助手/m.mp3"}}, post=post)
        prompt = post.calls[0]["json"]["prompt"]
        self.assertEqual((prompt["2"]["inputs"]["file"], prompt["4"]["inputs"]["audio"]), ("助手/v.mp4", "助手/m.mp3"))
        self.assertEqual(out["selection"]["files"], {"2:file": "助手/v.mp4", "4:audio": "助手/m.mp3"})
        self.assertEqual(len(out["warnings"]), 1)                                            # 音频放进图片控件：丢掉并提示

    def test_stage_copies_an_output_file_into_the_assistant_folder_by_content(self):
        from PIL import Image
        Image.new("RGB", (64, 48), (10, 120, 200)).save(os.path.join(self.out, "pro", "r_00001_.png"))
        shutil.copy(os.path.join(self.out, "pro", "r_00001_.png"), os.path.join(self.out, "r_copy.png"))            # 内容一样、名字不同
        a = pc.handle_stage({"filename": "r_00001_.png", "subfolder": "pro", "type": "output"})
        self.assertEqual(a["kind"], "image")
        self.assertRegex(a["name"], r"^助手/[0-9a-f]{16}\.png$")
        self.assertTrue(pc.asset_exists(a["name"]))
        self.assertEqual(os.listdir(os.path.join(self.inp, "助手")).count(a["name"].split("/")[1]), 1)
        b = pc.handle_stage({"filename": "r_copy.png", "subfolder": ""})                      # 同样的内容只存一份；type 缺省当 output
        self.assertEqual(b, a)
        again = pc.handle_stage({"filename": "r_00001_.png", "subfolder": "pro", "type": "output"})
        self.assertEqual(again, a)
        self.assertEqual([f for f in os.listdir(os.path.join(self.inp, "助手")) if f.endswith(".part")], [])
        write_text(os.path.join(self.out, "clip.MP4"), "不是真视频也行，只看扩展名")
        write_text(os.path.join(self.out, "voice.wav"), "x")
        self.assertEqual(pc.handle_stage({"filename": "clip.MP4"})["kind"], "video")
        self.assertEqual(pc.handle_stage({"filename": "voice.wav"})["kind"], "audio")
        self.assertTrue(pc.handle_stage({"filename": "clip.MP4"})["name"].endswith(".mp4"))             # 扩展名统一小写

    def test_stage_refuses_anything_outside_the_output_folder_or_of_the_wrong_type(self):
        write_text(os.path.join(self.out, "ok.png"), "x")
        os.symlink(os.path.join(self.tmp, "secret.txt"), os.path.join(self.out, "link.png"))                 # 符号链接指到目录外
        os.symlink(self.inp, os.path.join(self.out, "inputlink"))                                         # 目录符号链接
        shutil.copy(os.path.join(self.inp, "助手", "a.png"), os.path.join(self.tmp, "outside.png"))
        cases = [({"filename": "../outside.png"}, 404), ({"filename": "outside.png", "subfolder": ".."}, 404), ({"filename": "x.png", "subfolder": self.tmp}, 404),
                 ({"filename": os.path.join(self.tmp, "outside.png")}, 404), ({"filename": "link.png"}, 404), ({"filename": "a.png", "subfolder": "inputlink/助手"}, 404),
                 ({"filename": "不存在.png"}, 404), ({"filename": "ok.png", "type": "input"}, 400), ({"filename": "ok.png", "type": "temp"}, 400),
                 ({"filename": "secret.txt"}, 400), ({"filename": "a.py"}, 400), ({"filename": ""}, 400), ({"filename": "x\x00.png"}, 400), ({"filename": None}, 400), ({}, 400)]
        for payload, status in cases:
            with self.assertRaises(ChatError, msg=repr(payload)) as cm:
                pc.handle_stage(payload)
            self.assertEqual(cm.exception.status, status, repr(payload))
        for bad in (None, [], "x"):
            with self.assertRaises(ChatError):
                pc.handle_stage(bad)
        self.assertEqual(sorted(os.listdir(os.path.join(self.inp, "助手"))), ["a.png"])                       # 一个都没放进去

    def run_post(self, status=200):
        return FakePost(Resp(status, {"prompt_id": "p-1", "number": 1, "node_errors": {}} if status == 200 else {"error": {"message": "不行"}, "node_errors": {}}))

    def test_the_same_run_id_is_submitted_to_comfyui_only_once(self):
        post = self.run_post()
        a = pc.handle_run({"workflow": "07", "run_id": "r-1", "client_id": "c"}, post=post)
        b = pc.handle_run({"workflow": "07", "run_id": "r-1", "client_id": "c"}, post=post)
        self.assertEqual(len(post.calls), 1)                                                   # 刷新后重来 / 点两次 / 两个标签页：不重复提交（视频要占额度）
        self.assertEqual((a["prompt_id"], b["prompt_id"], b.get("repeated"), a.get("repeated")), ("p-1", "p-1", True, None))
        pc.handle_run({"workflow": "07", "run_id": "r-2"}, post=post)                          # 换一个 run_id 才是新的一次
        pc.handle_run({"workflow": "07"}, post=post)                                           # 没有 run_id：每次都提交
        pc.handle_run({"workflow": "07"}, post=post)
        self.assertEqual(len(post.calls), 4)
        for bad in ("", None, 5, ["r"], "x" * 81):
            pc.handle_run({"workflow": "07", "run_id": bad}, post=post)                       # 不合规的 run_id 当没有
        self.assertEqual(len(post.calls), 9)

    def test_the_same_run_id_with_different_content_is_a_new_submission(self):
        post = self.run_post()
        pc.handle_run({"workflow": "07", "run_id": "r-x", "fields": {"70:idea": "红苹果"}}, post=post)
        out = pc.handle_run({"workflow": "07", "run_id": "r-x", "fields": {"70:idea": "绿苹果"}}, post=post)
        self.assertEqual((len(post.calls), out.get("repeated")), (2, None))
        pc.handle_run({"workflow": "07", "run_id": "r-x", "fields": {"70:idea": "绿苹果"}}, post=post)
        self.assertEqual(len(post.calls), 2)                                                   # 内容又一样了才算重复

    def test_a_failed_submission_can_be_retried_with_the_same_run_id(self):
        bad = self.run_post(400)
        with self.assertRaises(ChatError):
            pc.handle_run({"workflow": "07", "run_id": "r-fail"}, post=bad)
        ok = self.run_post()
        out = pc.handle_run({"workflow": "07", "run_id": "r-fail"}, post=ok)
        self.assertEqual((len(ok.calls), out.get("repeated")), (1, None))                      # 第一次没提交成功就不记，重试会真的提交

    def test_concurrent_requests_with_the_same_run_id_submit_once(self):
        import threading
        import time

        class SlowPost(FakePost):
            def __call__(self, *a, **k):
                time.sleep(0.2)
                return super().__call__(*a, **k)

        post = SlowPost(Resp(200, {"prompt_id": "p-9", "number": 1, "node_errors": {}}))
        outs = []
        ts = [threading.Thread(target=lambda: outs.append(pc.handle_run({"workflow": "07", "run_id": "r-race"}, post=post))) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual((len(post.calls), len(outs), {o["prompt_id"] for o in outs}), (1, 5, {"p-9"}))
        self.assertEqual(sum(1 for o in outs if o.get("repeated")), 4)

    def test_run_id_memory_expires_and_is_bounded(self):
        post = self.run_post()
        saved = (pc.RUN_ID_TTL, pc.RUN_ID_KEEP)
        try:
            pc.RUN_ID_TTL = 0.05
            pc.handle_run({"workflow": "07", "run_id": "r-old"}, post=post)
            import time
            time.sleep(0.1)
            pc.handle_run({"workflow": "07", "run_id": "r-old"}, post=post)
            self.assertEqual(len(post.calls), 2)                                               # 过期了就重新提交
            pc.RUN_ID_TTL, pc.RUN_ID_KEEP = 3600, 5
            for i in range(20):
                pc.handle_run({"workflow": "07", "run_id": f"r-many-{i}"}, post=post)
            self.assertLessEqual(len(pc._RUNS), 5)
            self.assertLessEqual(len(pc._RUN_LOCKS), 20)
        finally:
            pc.RUN_ID_TTL, pc.RUN_ID_KEEP = saved

    def test_stage_has_a_size_limit(self):
        write_text(os.path.join(self.out, "big.mp4"), "x" * 100)
        saved = pc.STAGE_MAX_BYTES
        pc.STAGE_MAX_BYTES = 10
        try:
            with self.assertRaises(ChatError) as cm:
                pc.handle_stage({"filename": "big.mp4"})
            self.assertEqual(cm.exception.status, 413)
        finally:
            pc.STAGE_MAX_BYTES = saved

    def test_catalog_lists_every_app_with_its_schema_but_no_secrets(self):
        out = pc.handle_catalog()
        self.assertEqual([a["workflow"] for a in out["apps"]], list(CAT))
        a07 = next(a for a in out["apps"] if a["workflow"] == "07")
        self.assertEqual((a07["name"], a07["schema"]["outputs"]), ("文案", [90]))
        self.assertIn("70:idea", a07["schema"]["fields"])
        self.assertTrue(a07["desc"])
        self.assertNotIn("sk-glue-key-999", json.dumps(out, ensure_ascii=False))

    def test_package_is_a_pure_frontend_plus_routes_package(self):
        self.assertEqual(pc.NODE_CLASS_MAPPINGS, {})
        self.assertEqual(pc.WEB_DIRECTORY, "./web")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "custom-nodes", "pro-chat", "web", "pro-chat.js")))


if __name__ == "__main__":
    unittest.main()
