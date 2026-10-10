"""工作流校验：模板与生成脚本一致、连线完整、节点类型存在、不压住节点；
以及「惰性执行模拟」：每种模式下按 ComfyUI 的规则算出会执行哪些节点，没选中的引擎 / 设计 / 克隆 / 轮播不能被执行。"""
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WF = os.path.join(ROOT, "workflows")

_spec = importlib.util.spec_from_file_location("t_pro_flow_wf", os.path.join(ROOT, "custom-nodes", "pro-flow", "__init__.py"))
FLOW = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FLOW)

# 有「★ 只填这里：一句话需求」（阿里 写提示词节点，编号 70）/ 有「★ 先选模式」（模式节点，编号 77）的工作流
WRITER_NUMS = {"01", "02", "03", "04", "06", "07", "08", "09", "10", "12"}
MODE_NUMS = {"01", "02", "03", "05", "07", "08", "09", "10", "11", "12", "14", "16"}

# ComfyUI 核心 / 插件里工作流用到的节点；自带节点从源码里的 NODE_CLASS_MAPPINGS 读（pro-flow 是函数生成的，直接 import）
KNOWN_EXTERNAL = {
    "RelayAPISettings", "RelayImageGenerator", "RelayVideoGenerator", "RelaySoundGenerator", "RelayTextGenerator",
    "LoadImage", "LoadAudio", "LoadVideo", "SaveImage", "SaveAudioAdvanced", "SaveVideo", "PreviewImage", "PreviewAny",
    "ImageScale", "ImageScaleBy", "ImageScaleToMaxDimension", "ImageSharpen", "ImageBatch", "Note", "MarkdownNote",
    "PrimitiveNode", "PrimitiveStringMultiline", "StringConcatenate",
}
OUTPUT_TYPES = {"SaveImage", "PreviewImage", "PreviewAny", "SaveVideo", "SaveAudioAdvanced", "SaveAudio", "ProVideoDub", "ProAppText", "ProSaveCutout", "ProLayerCompose"}


def load_node_pkg(pkg):
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    try:
        import _load
        return _load.load(pkg)
    finally:
        sys.path.pop(0)


def own_node_names():
    names = set(FLOW.NODE_CLASS_MAPPINGS)
    for p in glob.glob(os.path.join(ROOT, "custom-nodes", "pro-*", "__init__.py")):
        if os.path.basename(os.path.dirname(p)) == "pro-flow":
            continue
        with open(p, encoding="utf-8") as f:
            src = f.read()
        m = re.search(r"NODE_CLASS_MAPPINGS\s*=\s*\{(.*?)\}", src, re.S)
        names |= set(re.findall(r'"(\w+)"\s*:', m.group(1))) if m else set()
    return names


def all_workflows():
    return sorted(glob.glob(os.path.join(WF, "*", "*.json")))


def load(num):
    p = next(p for p in all_workflows() if os.path.basename(p).startswith(num + "-"))
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class Workflows(unittest.TestCase):
    def test_generator_output_matches_committed(self):
        """改了 gen_workflows.py / pro-flow 没重新生成、或手改了 JSON，这里会报。"""
        tmp = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(ROOT, "tools"), os.path.join(tmp, "tools"))
            os.makedirs(os.path.join(tmp, "custom-nodes"))
            shutil.copytree(os.path.join(ROOT, "custom-nodes", "pro-flow"), os.path.join(tmp, "custom-nodes", "pro-flow"))
            r = subprocess.run([sys.executable, os.path.join(tmp, "tools", "gen_workflows.py")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

            def rd(p):
                with open(p, "rb") as f:
                    return f.read()
            gen = {os.path.relpath(p, os.path.join(tmp, "workflows")): rd(p) for p in glob.glob(os.path.join(tmp, "workflows", "*", "*.json"))}
            cur = {os.path.relpath(p, WF): rd(p) for p in all_workflows()}
            self.assertEqual(sorted(gen), sorted(cur), "生成的文件列表和已提交的不一致")
            for k in cur:
                self.assertEqual(gen[k], cur[k], f"{k} 和生成脚本输出不一致，请重新运行 tools/gen_workflows.py")
        finally:
            shutil.rmtree(tmp)

    def test_numbers_unique_and_folders(self):
        nums = [os.path.basename(p).split("-")[0] for p in all_workflows()]
        self.assertEqual(len(nums), len(set(nums)), "工作流编号重复")
        self.assertEqual(sorted(nums), [f"{i:02d}" for i in range(1, 17)], "应该是 01~16 连续编号")

    def test_links_consistent(self):
        known = own_node_names() | KNOWN_EXTERNAL
        for p in all_workflows():
            name = os.path.relpath(p, WF)
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            ids = {n["id"] for n in wf["nodes"]}
            self.assertEqual(len(ids), len(wf["nodes"]), f"{name}: 节点 id 重复")
            links = {l[0]: l for l in wf["links"]}
            for n in wf["nodes"]:
                self.assertIn(n["type"], known, f"{name}: 未知节点类型 {n['type']}")
            for lid, (_, a, _, b, _, _t) in links.items():
                self.assertIn(a, ids, f"{name}: link {lid} 起点节点不存在")
                self.assertIn(b, ids, f"{name}: link {lid} 终点节点不存在")
            for n in wf["nodes"]:
                for i in n.get("inputs", []):
                    if i.get("link") is not None:
                        self.assertIn(i["link"], links, f"{name}: 节点 {n['id']} 输入引用了不存在的 link")
                        self.assertEqual(links[i["link"]][3], n["id"], f"{name}: link 终点与输入所属节点不符")
                for o in n.get("outputs", []):
                    for l in o.get("links") or []:
                        self.assertIn(l, links, f"{name}: 节点 {n['id']} 输出引用了不存在的 link")
                        self.assertEqual(links[l][1], n["id"], f"{name}: link 起点与输出所属节点不符")

    def test_widget_input_types_match_node_definitions(self):
        """连进控件的输入（widget）类型要写对（INT / BOOLEAN / STRING），前端按它判断。"""
        want = {("ProPick2x1", "branch"): "INT", ("ProGate", "enabled"): "BOOLEAN", ("ProPosterPrompt", "use_product_image"): "BOOLEAN",
                ("ProVideoDub", "keep_original_audio"): "BOOLEAN", ("ProAliPromptWriter", "instruction"): "STRING"}
        seen = set()
        for p in all_workflows():
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            for n in wf["nodes"]:
                t = "ProPick2x1" if n["type"].startswith("ProPick") else n["type"]
                for i in n["inputs"]:
                    if (t, i["name"]) in want and i.get("widget"):
                        self.assertEqual(i["type"], want[(t, i["name"])], f"{os.path.basename(p)}: {n['type']}.{i['name']}")
                        seen.add((t, i["name"]))
        self.assertEqual(seen, set(want), "有几个连线控件在所有工作流里都没出现，检查测试或模板")

    def test_top_blocks(self):
        """有模式 / 写提示词的工作流：★ 节点在画布最上面；写提示词接了 Key；扩写结果连到了下游。"""
        for p in all_workflows():
            num = os.path.basename(p).split("-")[0]
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            nodes = {n["id"]: n for n in wf["nodes"]}
            stars = [n for n in wf["nodes"] if n.get("title", "").startswith("★")]
            self.assertEqual(len(stars), (num in MODE_NUMS) + (num in WRITER_NUMS), f"{num}: ★ 节点数量不对")
            if not stars:
                continue
            self.assertEqual(min(n["pos"][1] for n in wf["nodes"]), min(n["pos"][1] for n in stars), f"{num}: ★ 节点应该在最上面")
            if num in MODE_NUMS:
                self.assertTrue(nodes[77]["type"].startswith("ProMode"), num)
                self.assertEqual(nodes[77]["widgets_values"][0], FLOW.MODES[nodes[77]["type"]][1][0], f"{num}: 模式默认值应是第一个选项")
            if num in WRITER_NUMS:
                w = nodes[70]
                self.assertEqual(w["type"], "ProAliPromptWriter")
                self.assertIsNotNone(next(i for i in w["inputs"] if i["name"] == "info")["link"], f"{num}: 写提示词节点没接 Key（Settings 节点 id 31）")
                self.assertGreaterEqual(len(w["outputs"][0]["links"] or []), 1, f"{num}: 扩写结果没有连到下游")

    # 界面会把「生图」节点（16 个图片输入口）自动撑高到约 576，再加标题栏约 30；按这个算才和实际看到的一致
    MIN_HEIGHT = {"RelayImageGenerator": 576}

    def rect(self, n):
        """节点在界面里实际占的矩形 (x0, y0, x1, y1)：pos 是本体左上角，标题栏（约 30）在它上面。
        折叠的节点只剩标题栏，宽度 = 标题文字宽 + 60（不超过原宽度）；加载音频 / 视频的标题会撑宽节点
        （实测 58 字的标题把宽度撑到 771px，约 13.5px/字 + 60px），长标题会压到右边的节点。"""
        x, y = n["pos"]
        w, h = n["size"]
        title_w = 13.5 * len(n.get("title", "")) + 60
        if n["flags"].get("collapsed"):
            return (x, y - 30, x + min(w, title_w), y)
        if n["type"] in ("LoadAudio", "LoadVideo"):
            w = max(w, title_w)
        return (x, y - 30, x + w, y + max(h, self.MIN_HEIGHT.get(n["type"], 0)))

    def test_no_overlapping_nodes(self):
        for p in all_workflows():
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            ns = wf["nodes"]
            for i, a in enumerate(ns):
                for b in ns[i + 1:]:
                    ra, rb = self.rect(a), self.rect(b)
                    overlap = ra[0] < rb[2] - 4 and rb[0] < ra[2] - 4 and ra[1] < rb[3] - 4 and rb[1] < ra[3] - 4
                    self.assertFalse(overlap, f"{os.path.basename(p)}: 节点 {a['id']}（{a['type']}）和 {b['id']}（{b['type']}）重叠")

    # 角色颜色：绿 = 要用户填 / 上传 / 调，蓝 = 结果，黄 = Key（折叠），没颜色 = 自动处理
    GREEN, BLUE, YELLOW = "#353", "#335", "#653"
    MUST_GREEN = {"LoadImage", "LoadAudio", "LoadVideo", "ProAliPromptWriter", "ProPosterFields"}
    MUST_BLUE = {"SaveImage", "SaveAudioAdvanced", "SaveVideo", "ProVideoDub", "ProAppText", "ProSaveCutout", "ProLayerCompose"}
    COLLAPSED = {"RelayAPISettings", "ProAppText"}        # 折叠成一条的：Key、应用界面用的文字小条

    def role_of(self, n):
        return {self.GREEN: "IN", self.BLUE: "OUT", self.YELLOW: "KEY"}.get(n.get("bgcolor"), "AUTO")

    def test_node_roles_and_colors(self):
        for p in all_workflows():
            name = os.path.basename(p)
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            for n in wf["nodes"]:
                t, r = n["type"], self.role_of(n)
                if t in self.MUST_GREEN or t.startswith("ProMode"):
                    self.assertEqual(r, "IN", f"{name}: 节点 {n['id']}（{t}）要用户填，应该是绿色")
                if t in self.MUST_BLUE:
                    self.assertEqual(r, "OUT", f"{name}: 节点 {n['id']}（{t}）是结果，应该是蓝色")
                if t == "PreviewAny":
                    self.assertIn(r, ("IN", "OUT"), f"{name}: 节点 {n['id']} 预览节点应该是绿色（扩写出的提示词）或蓝色（结果 / 状态）")
                # Key：折叠 + 黄色 + 标题写明是哪个 Key，且只有 Key 才这样
                self.assertEqual(r == "KEY", t == "RelayAPISettings", f"{name}: 节点 {n['id']}（{t}）")
                self.assertEqual(bool(n["flags"].get("collapsed")), t in self.COLLAPSED, f"{name}: 节点 {n['id']}（{t}）折叠状态不对")
                if t == "RelayAPISettings":
                    self.assertTrue(n["title"].startswith("Key："), f"{name}: Key 节点标题要写明是哪个 Key")

    def test_three_zone_groups(self):
        """三个分组框：① 先填这里（绿）、② 自动处理（含黄色 Key）、③ 结果（蓝）；每个节点整个落在自己角色的框里，三个框互不相交。"""
        zone_of = {"IN": 0, "KEY": 1, "AUTO": 1, "OUT": 2}
        for p in all_workflows():
            name = os.path.basename(p)
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            groups = wf["groups"]
            self.assertEqual([g["title"][:2] for g in groups], ["① ", "② ", "③ "], f"{name}: 应该有三个分组框，按 ①②③ 排")
            boxes = [(g["bounding"][0], g["bounding"][1], g["bounding"][0] + g["bounding"][2], g["bounding"][1] + g["bounding"][3]) for g in groups]
            for i, a in enumerate(boxes):
                for b in boxes[i + 1:]:
                    self.assertFalse(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3], f"{name}: 分组框互相重叠")
            for n in wf["nodes"]:
                r = self.rect(n)
                inside = [i for i, b in enumerate(boxes) if b[0] <= r[0] and b[1] <= r[1] and r[2] <= b[2] and r[3] <= b[3]]
                self.assertEqual(inside, [zone_of[self.role_of(n)]], f"{name}: 节点 {n['id']}（{n['type']}，{self.role_of(n)}）应该整个落在分组框 {zone_of[self.role_of(n)] + 1} 里，实际 {[i + 1 for i in inside]}")
            for z, want in enumerate(("IN", "AUTO", "OUT")):    # 框里至少有一个对应角色的节点（没有空框）
                self.assertTrue(any(zone_of[self.role_of(n)] == z for n in wf["nodes"]), f"{name}: 分组框 {z + 1}（{want}）是空的")

    def test_opens_focused_on_the_first_zone(self):
        """打开时直接对准「先填这里」：缩放不低于 0.62（再小 ComfyUI 不画节点上的字），分组框左上角避开左侧栏和顶部工具栏（标题不被盖住），★ 节点一屏内看得到。"""
        for p in all_workflows():
            name = os.path.basename(p)
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            ds = wf["extra"]["ds"]
            self.assertGreaterEqual(ds["scale"], 0.62, f"{name}: 打开时缩放太小，节点上的字会不显示")
            to_screen = lambda x, y: ((x + ds["offset"][0]) * ds["scale"], (y + ds["offset"][1]) * ds["scale"])
            gx, gy = wf["groups"][0]["bounding"][:2]
            sx, sy = to_screen(gx, gy)
            self.assertTrue(60 <= sx <= 100 and 100 <= sy <= 150, f"{name}: 「先填这里」应该在屏幕左上角、避开侧栏和顶部工具栏，实际 ({sx:.0f}, {sy:.0f})")
            for n in wf["nodes"]:
                if n.get("title", "").startswith("★"):
                    r = self.rect(n)
                    ex, ey = to_screen(r[2], r[3])
                    self.assertTrue(ex <= 1100 and ey <= 720, f"{name}: ★ 节点 {n['id']} 打开时超出一屏（右下角 {ex:.0f}, {ey:.0f}）")


    # ── 应用界面（ComfyUI 的 App 模式）：extra.linearData = {inputs: [[节点 id, 控件名]], outputs: [节点 id]}，extra.linearMode = True ──
    # 核心节点 / relayapi 的控件名（来自服务器 /object_info；自带的 pro-* 节点直接读它们的 INPUT_TYPES，不在这里重复写）
    KNOWN_WIDGETS = {
        "LoadImage": {"image"}, "LoadAudio": {"audio"}, "LoadVideo": {"file"}, "PrimitiveStringMultiline": {"value"},
        "RelayImageGenerator": {"prompt", "ratio", "size", "quality", "format", "moderation", "seed"},
        "RelayVideoGenerator": {"prompt", "ratio", "size", "duration", "seed", "enhance_prompt", "enable_HD"},
        "ImageScaleBy": {"upscale_method", "scale_by"}, "ImageSharpen": {"sharpen_radius", "sigma", "alpha"},
        "RelaySoundGenerator": {"generation_mode", "title", "tags", "prompt", "make_instrumental", "version", "seed", "negative_tags", "extend_mode", "continue_clip_id", "continue_at"},
    }
    APP_OUTPUT_TYPES = {"SaveImage", "SaveVideo", "SaveAudioAdvanced", "ProVideoDub", "ProAppText", "ProSaveCutout", "ProLayerCompose"}
    # 应用界面默认显示「最后执行完的那一条」。这些预览节点的文字不放进应用：它们和成片几乎同时执行完，会把成片挤到第二位（11：字幕已烧进成片、也存了 .srt）
    APP_TEXT_EXEMPT = {"11": {20}}
    MEDIA_SAVERS = {"SaveImage", "SaveVideo", "SaveAudioAdvanced", "ProVideoDub", "ProSaveCutout", "ProLayerCompose"}

    def test_dubbing_voice_is_selectable_in_every_app_with_a_dubbing_node(self):
        """有「阿里 配音」节点的应用（08 / 10 / 11 / 12），右栏都要能选音色：音色是配音最常想换的东西，聊天里（pro-chat）也靠右栏登记的控件来设。"""
        found = 0
        for p in all_workflows():
            name = os.path.basename(p)
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            inputs = [tuple(i) for i in wf["extra"]["linearData"]["inputs"]]
            for n in wf["nodes"]:
                if n["type"] == "ProAliTTS":
                    found += 1
                    self.assertIn((n["id"], "voice"), inputs, f"{name}: 配音节点 {n['id']} 的「音色」应该登记进应用右栏")
        self.assertGreaterEqual(found, 4, "08 / 10 / 11 / 12 都有配音节点")

    @classmethod
    def widget_names(cls, node_type):
        """节点类型的控件名：pro-* 节点读 INPUT_TYPES（不是强制连线的输入），其余查 KNOWN_WIDGETS；都没有返回 None。"""
        if node_type in cls.KNOWN_WIDGETS:
            return cls.KNOWN_WIDGETS[node_type]
        if not hasattr(cls, "_own"):
            cls._own = {}
            for pkg in sorted(os.listdir(os.path.join(ROOT, "custom-nodes"))):
                if not pkg.startswith("pro-"):
                    continue
                mod = FLOW if pkg == "pro-flow" else load_node_pkg(pkg)
                for name, klass in mod.NODE_CLASS_MAPPINGS.items():
                    spec = klass.INPUT_TYPES()
                    cls._own[name] = {k for sect in ("required", "optional") for k, v in spec.get(sect, {}).items()
                                      if not (len(v) > 1 and isinstance(v[1], dict) and v[1].get("forceInput")) and (isinstance(v[0], list) or v[0] in ("STRING", "INT", "FLOAT", "BOOLEAN"))}
        return cls._own.get(node_type)

    def test_every_workflow_is_an_app(self):
        """每个内置工作流都是应用：文件名 .app.json（左侧栏「应用」标签只列这种）、打开就是应用界面（linearMode）、右栏控件和结果节点都登记了。"""
        for p in all_workflows():
            name = os.path.basename(p)
            self.assertTrue(name.endswith(".app.json"), f"{name}: 文件名要以 .app.json 结尾")
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            self.assertIs(wf["extra"].get("linearMode"), True, f"{name}: extra.linearMode 要是 true，打开才是应用界面")
            data = wf["extra"]["linearData"]
            self.assertTrue(data["inputs"] and data["outputs"], f"{name}: 右栏控件和结果节点都不能是空的")

    def test_app_inputs_are_real_unlinked_widgets_with_distinct_labels(self):
        """右栏登记的 [节点 id, 控件名] 必须真有这个控件（写错了 ComfyUI 只在控制台警告、右栏悄悄少一项），且不能是连了线的；
        同一个工作流里不能出现两行一模一样的名字（两个「图像」上传、两个「比例」要各起名）。"""
        for p in all_workflows():
            name = os.path.basename(p)
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            nodes = {n["id"]: n for n in wf["nodes"]}
            shown = []
            for nid, widget in wf["extra"]["linearData"]["inputs"]:
                self.assertTrue(nid in nodes, f"{name}: 右栏登记了不存在的节点 {nid}")
                n = nodes[nid]
                names = self.widget_names(n["type"])
                self.assertIsNotNone(names, f"{name}: 节点 {nid}（{n['type']}）的控件名未知，请加进 KNOWN_WIDGETS")
                self.assertTrue(widget in names, f"{name}: 节点 {nid}（{n['type']}）没有控件 {widget}（有：{sorted(names)}）")
                inp = next((i for i in n["inputs"] if i["name"] == widget), None)
                self.assertTrue(inp is None or inp.get("link") is None, f"{name}: 节点 {nid} 的 {widget} 连了线，不能放进右栏")
                shown.append((inp or {}).get("label") or f"{n['type']}.{widget}")
            self.assertEqual(len(shown), len(set(shown)), f"{name}: 右栏有重复的名字（要用 g.app_in 的 label 参数各起名）：{shown}")

    def test_assistant_only_settings_are_real_unlinked_widgets_and_stay_out_of_the_app_panel(self):
        """appExtra / appAdvanced（只给助手 / 工作台用的可调设置）：格式 [节点 id, 控件名, 中文名, 说明]，控件真实存在、没连线、不和右栏重复登记；
        中文名不为空，在这个工作流里不和右栏 / 别的设置重名；右栏（linearData）本身不受影响。appCost 是文字。"""
        extras_seen = 0
        for p in all_workflows():
            name = os.path.basename(p)
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            ex = wf["extra"]
            nodes = {n["id"]: n for n in wf["nodes"]}
            panel = {tuple(i) for i in ex["linearData"]["inputs"]}
            labels = []
            for nid, widget in panel:
                inp = next((i for i in nodes[nid]["inputs"] if i["name"] == widget), None)
                labels.append((inp or {}).get("label") or f"{nodes[nid]['type']}.{widget}")
            seen = set()
            for sect in ("appExtra", "appAdvanced"):
                for e in ex.get(sect, []):
                    extras_seen += 1
                    self.assertTrue(isinstance(e, list) and len(e) in (4, 5) and isinstance(e[0], int) and all(isinstance(x, str) for x in e[1:4]), f"{name}: {sect} 条目格式不对：{e}")
                    self.assertTrue(len(e) == 4 or (isinstance(e[4], list) and e[4] and all(isinstance(x, str) for x in e[4])), f"{name}: {sect} 的可选项要是非空的文字列表：{e}")
                    nid, widget, label, hint = e[:4]
                    self.assertTrue(nid in nodes, f"{name}: {sect} 登记了不存在的节点 {nid}")
                    names = self.widget_names(nodes[nid]["type"])
                    self.assertTrue(names is not None and widget in names, f"{name}: 节点 {nid}（{nodes[nid]['type']}）没有控件 {widget}")
                    inp = next((i for i in nodes[nid]["inputs"] if i["name"] == widget), None)
                    self.assertTrue(inp is None or inp.get("link") is None, f"{name}: {nid}.{widget} 连了线，不能当可调设置")
                    self.assertNotIn((nid, widget), panel | seen, f"{name}: {nid}.{widget} 重复登记")
                    seen.add((nid, widget))
                    self.assertTrue(label.strip(), f"{name}: {nid}.{widget} 没有中文名")
                    labels.append(label)
            self.assertEqual(len(labels), len(set(labels)), f"{name}: 设置的中文名重复：{sorted(l for l in labels if labels.count(l) > 1)}")
            self.assertIsInstance(ex.get("appCost", ""), str, name)
            for entry in ex.get("appRequired", []):                                        # 必填文件：必须是右栏里登记过的上传控件；第 3 项（可有可无）是只在这些模式必填
                nid, widget = entry[:2]
                self.assertIn([nid, widget], ex["linearData"]["inputs"], f"{name}: 必填的 {nid}.{widget} 要先登记进右栏")
                self.assertIn(nodes[nid]["type"], ("LoadImage", "LoadVideo", "LoadAudio"), f"{name}: 必填的 {nid} 不是上传控件")
                if len(entry) > 2:
                    mode_type = next(n["type"] for n in wf["nodes"] if n["type"].startswith("ProMode"))
                    self.assertTrue(entry[2] and entry[2] == sorted(set(entry[2])) and 1 <= entry[2][0] and entry[2][-1] <= len(FLOW.MODES[mode_type][1]), f"{name}: {entry} 的模式编号不对")
        self.assertGreater(extras_seen, 40, "几个工作流都该登记了可调设置")
        self.assertEqual(load("04")["extra"]["linearData"]["inputs"], [[70, "idea"], [2, "image"]], "没改的应用右栏保持原样")

    def test_image_to_video_app_is_the_text_to_video_one_plus_a_reference_image(self):
        wf10, wf12 = load("10"), load("12")
        def veo(wf):
            return next(n for n in wf["nodes"] if n["type"] == "RelayVideoGenerator")
        img = [n for n in wf12["nodes"] if n["type"] == "LoadImage"]
        self.assertEqual(len(img), 1)
        link = next(i for i in veo(wf12)["inputs"] if i["name"] == "image1")["link"]
        self.assertEqual(next(l for l in wf12["links"] if l[0] == link)[1], img[0]["id"], "参考图要接到 Veo 的 image1")
        self.assertIn([img[0]["id"], "image"], wf12["extra"]["linearData"]["inputs"])
        self.assertFalse([n for n in wf10["nodes"] if n["type"] == "LoadImage"], "10 文生视频不带图")
        writer = next(n for n in wf12["nodes"] if n["type"] == "ProAliPromptWriter")
        self.assertEqual(writer["widgets_values"][1], FLOW.TEMPLATES["video_image"])
        self.assertNotEqual(FLOW.TEMPLATES["video_image"], FLOW.TEMPLATES["video"])
        self.assertEqual(writer["widgets_values"][2], "qwen3.8-omni-flash", "要看图，得用能看图的模型")
        by_id = {n["id"]: n for n in wf12["nodes"]}
        src = {l[0]: l for l in wf12["links"]}
        img_link = next(i["link"] for i in writer["inputs"] if i["name"] == "image")
        scaler = by_id[src[img_link][1]]
        self.assertEqual((scaler["type"], scaler["widgets_values"]), ("ImageScaleToMaxDimension", ["lanczos", 768]), "给阿里的图要先缩到最长边 768")
        self.assertEqual(by_id[src[next(i["link"] for i in scaler["inputs"] if i["name"] == "image")][1]]["type"], "LoadImage")
        self.assertTrue(wf12["extra"]["appCost"].startswith(wf10["extra"]["appCost"]))              # 12 在 10 的费用说明后面多一句参考图没证实
        self.assertIn("没能证实", wf12["extra"]["appCost"])
        self.assertEqual(wf12["extra"]["appExtra"], wf10["extra"]["appExtra"])             # 视频 / 配音 / 字幕的可调设置两个应用一样

    def assert_widget_values(self, num, node, klass):
        """工作流里这个节点的控件值（widgets_values）按节点定义逐项校验：个数、顺序、类型、范围、下拉选项。"""
        spec = klass.INPUT_TYPES()
        order = [(k, v) for sect in ("required", "optional") for k, v in spec.get(sect, {}).items()
                 if not (len(v) > 1 and isinstance(v[1], dict) and v[1].get("forceInput")) and (isinstance(v[0], list) or v[0] in ("STRING", "INT", "FLOAT", "BOOLEAN"))]   # 控件（不是图片 / 音频这类连线口）
        self.assertEqual(len(node["widgets_values"]), len(order), f"{num}: {node['type']} 的控件值个数和节点定义不一致")
        for (key, d), val in zip(order, node["widgets_values"]):
            typ, meta = d[0], (d[1] if len(d) > 1 else {})
            if isinstance(typ, list):
                self.assertIn(val, typ, f"{num}: {key} 的值不在下拉选项里")
            elif typ == "BOOLEAN":
                self.assertIsInstance(val, bool, f"{num}: {key}")
            elif typ in ("INT", "FLOAT"):
                self.assertTrue(not isinstance(val, bool) and meta["min"] <= val <= meta["max"], f"{num}: {key}={val} 超出范围")
            else:
                self.assertIsInstance(val, str, f"{num}: {key}")

    def test_layer_apps_wire_cutout_mask_and_background_and_use_valid_widget_values(self):
        image = load_node_pkg("pro-image")
        for num, cls in (("15", image.ProMatte), ("15", image.ProSaveCutout), ("16", image.ProLayerCompose)):
            wf = load(num)
            node = next(n for n in wf["nodes"] if n["type"] == cls.__name__)
            self.assert_widget_values(num, node, cls)
        for num in ("15", "16"):
            wf = load(num)
            nodes = {n["id"]: n for n in wf["nodes"]}
            src = {l[0]: (l[1], l[2]) for l in wf["links"]}

            def fed_by(dst_id, inp):
                link = next(i["link"] for i in nodes[dst_id]["inputs"] if i["name"] == inp)
                sid, slot = src[link]
                return nodes[sid]["id"], nodes[sid]["outputs"][slot]["name"]
            if num == "15":
                self.assertEqual((fed_by(3, "image"), fed_by(4, "image"), fed_by(4, "mask")), ((2, "IMAGE"), (3, "image"), (3, "mask")))
            else:
                self.assertEqual((fed_by(5, "product"), fed_by(5, "product_mask")), ((2, "IMAGE"), (2, "MASK")), "透明度要用商品那张图自己的 MASK")
                self.assertEqual(fed_by(5, "background"), (21, "value"))                      # 背景图经开关门：纯色模式不读它
                self.assertEqual(nodes[21]["type"], "ProGate")
                self.assertEqual(fed_by(21, "value"), (3, "IMAGE"))
            self.assertIn([2, "image"], wf["extra"]["appRequired"], f"{num}: 商品图是必填的（不然会悄悄用演示图）")
            self.assertEqual(wf["extra"]["linearData"]["outputs"], [4] if num == "15" else [5])
        wf16 = load("16")
        self.assertEqual(FLOW.MODES["ProModeLayer"][1], ["放到背景图上", "放到纯色背景上"])
        self.assertEqual(next(n for n in wf16["nodes"] if n["type"] == "ProModeLayer")["widgets_values"], ["放到背景图上"])

    def test_dialogue_apps_wire_the_dialogue_node_into_the_video_and_use_valid_widget_values(self):
        """13 / 14 里的对白节点：控件值按节点定义逐项校验（顺序、类型、范围、下拉选项）；14 里对白的音频 / 字幕接到合成成片和轮播，13 里接到保存音频和文字结果。"""
        ali = load_node_pkg("pro-ali")
        for num in ("13", "14"):
            wf = load(num)
            node = next(n for n in wf["nodes"] if n["type"] == "ProAliDialogue")
            self.assert_widget_values(num, node, ali.ProAliDialogue)
            nodes = {n["id"]: n for n in wf["nodes"]}
            src = {l[0]: (l[1], l[2]) for l in wf["links"]}

            def fed_by(dst_id, inp):
                link = next(i["link"] for i in nodes[dst_id]["inputs"] if i["name"] == inp)
                sid, slot = src[link]
                return nodes[sid]["id"], nodes[sid]["outputs"][slot]["name"]
            if num == "13":
                self.assertEqual(fed_by(4, "audio"), (3, "audio"))
                texts = {(fed_by(n["id"], "text")) for n in wf["nodes"] if n["type"] == "ProAppText"}
                self.assertEqual(texts, {(3, "srt"), (3, "report")})
            else:
                self.assertEqual((fed_by(5, "voice"), fed_by(5, "subtitles"), fed_by(5, "bgm")), ((3, "audio"), (3, "srt"), (4, "AUDIO")))
                self.assertEqual(fed_by(8, "fit_audio"), (3, "audio"), "轮播的时长要跟对白走")
                self.assertIs(nodes[5]["widgets_values"][3], False, "对白要替换原声（不保留原视频声音）")
                self.assertEqual(nodes[5]["widgets_values"][5], "烧进画面", "字幕默认烧进画面")
                for n in nodes.values():
                    if n["type"] == "ProAliDialogue":
                        self.assertIsNotNone(next(i for i in n["inputs"] if i["name"] == "info")["link"], f"{num}: 对白节点没接 Key")
            panel = [tuple(i) for i in wf["extra"]["linearData"]["inputs"]]
            for w in ("script", "role1_name", "role1_voice", "role2_name", "role2_voice"):
                self.assertIn((3, w), panel, f"{num}: 右栏要能填 {w}")
            labels = [next((i for i in nodes[nid]["inputs"] if i["name"] == w), {}).get("label") for nid, w in panel if nid == 3]
            self.assertTrue(all(labels) and len(set(labels)) == len(labels), f"{num}: 对白的右栏控件要各起中文名：{labels}")
        # 默认的对白脚本和默认的角色表对得上（不改就能跑）
        wf = load("13")
        vals = next(n for n in wf["nodes"] if n["type"] == "ProAliDialogue")["widgets_values"]
        parsed = ali.parse_script(vals[0], [vals[1], vals[3], vals[5], vals[7]])
        self.assertEqual([(who, name) for who, name, _ in parsed], [(0, "小美"), (1, "阿强"), (0, "小美"), (1, "阿强")])

    def test_new_node_entries_in_the_object_info_subset_match_the_node_definitions(self):
        """tests/object_info_subset.json 里 pro-* 新节点的条目是按它们的 INPUT_TYPES 生成的（服务器上还没有这些节点可抓）：节点改了要同步，
        运行 python3 tools/refresh_object_info_subset.py。"""
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        try:
            import _load
        finally:
            sys.path.pop(0)
        with open(os.path.join(ROOT, "tests", "object_info_subset.json"), encoding="utf-8") as f:
            oi = json.load(f)
        for name, klass in _load.new_node_classes().items():
            self.assertEqual(oi[name], _load.object_info_entry(klass), f"{name} 在 object_info_subset.json 里的条目和节点定义不一致：运行 python3 tools/refresh_object_info_subset.py")

    def test_app_outputs_cover_every_result(self):
        """结果节点登记齐全：所有保存图片 / 视频 / 音频的节点都要在应用里显示；每个预览节点显示的文字，应用界面里也要有（ProAppText）。"""
        for p in all_workflows():
            name = os.path.basename(p)
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            nodes = {n["id"]: n for n in wf["nodes"]}
            src = {l[0]: (l[1], l[2]) for l in wf["links"]}
            outs = wf["extra"]["linearData"]["outputs"]
            self.assertEqual(len(outs), len(set(outs)), f"{name}: 结果节点重复登记")
            for o in outs:
                self.assertTrue(o in nodes, f"{name}: 登记了不存在的结果节点 {o}")
                self.assertTrue(nodes[o]["type"] in self.APP_OUTPUT_TYPES, f"{name}: 结果节点 {o}（{nodes[o]['type']}）应用界面显示不了")
            savers = {n["id"] for n in wf["nodes"] if n["type"] in self.MEDIA_SAVERS}
            self.assertEqual(savers, savers & set(outs), f"{name}: 这些保存结果的节点没登记成应用的结果，应用里看不到：{sorted(savers - set(outs))}")

            def source_of(n):
                link = next(i for i in n["inputs"] if i["name"] in ("source", "text"))["link"]
                return src[link]
            num = name.split("-")[0]
            previews = {source_of(n) for n in wf["nodes"] if n["type"] == "PreviewAny" and n["id"] not in self.APP_TEXT_EXEMPT.get(num, ())}
            app_texts = {source_of(n) for n in wf["nodes"] if n["type"] == "ProAppText"}
            self.assertEqual(previews - app_texts, set(), f"{name}: 这些预览节点显示的文字，应用界面里看不到（缺 app_text）：{sorted(previews - app_texts)}")
            for n in wf["nodes"]:
                if n["type"] == "ProAppText":
                    label, only_on_error = n["widgets_values"]
                    self.assertEqual(label.startswith("出错信息"), only_on_error, f"{name}: 节点 {n['id']}「{label}」：出错信息才用「只在出错时显示」，别的要总是显示")
                    self.assertEqual(n["title"], "应用界面 · " + label)

    def test_default_media_files_exist_in_assets(self):
        """ComfyUI 提交前会校验所有连着的节点（包括没选中的那一路）：加载音频 / 视频节点默认引用的文件必须随套件带上（assets/ → data/input）。
        加载图片的默认图 demo_product.png 是测试时手工放到服务器上的，不随套件走（第一步本来就是上传自己的商品图）。"""
        for p in all_workflows():
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            for n in wf["nodes"]:
                if n["type"] in ("LoadAudio", "LoadVideo"):
                    fname = n["widgets_values"][0]
                    self.assertTrue(os.path.exists(os.path.join(ROOT, "assets", fname)), f"{os.path.basename(p)}: {n['type']} 默认文件 {fname} 不在 assets/")

    def test_no_api_keys_in_templates(self):
        for p in all_workflows():
            with open(p, encoding="utf-8") as f:
                txt = f.read()
            self.assertIsNone(re.search(r"sk-[A-Za-z0-9]{16,}", txt), f"{p} 疑似含 API Key")


# ── 惰性执行模拟 ────────────────────────────────────────────────────────────────────────────
class Sim:
    """按 ComfyUI 的规则算会执行哪些节点：从输出节点倒推；选择分支只要选中那一路，开关门关着不要上游；其余节点要全部输入。"""

    def __init__(self, wf, mode_index=None):
        self.nodes = {n["id"]: n for n in wf["nodes"]}
        self.src = {l[0]: (l[1], l[2]) for l in wf["links"]}
        self.mode_index = mode_index

    def literal(self, node, name):
        """控件输入的值：连了模式节点的输出就取模式表里的常量，否则取控件自己的值（第一个控件）。"""
        inp = next((i for i in node["inputs"] if i["name"] == name), None)
        if inp is not None and inp.get("link") is not None:
            sid, slot = self.src[inp["link"]]
            src = self.nodes[sid]
            assert src["type"].startswith("ProMode"), f"{node['id']}.{name} 应连到模式节点，实际连到 {src['type']}"
            outs = FLOW.MODES[src["type"]][2]
            return outs[slot][2][self.mode_index]
        return node["widgets_values"][0]

    def executed(self):
        done = set()

        def need(nid):
            if nid in done:
                return
            done.add(nid)
            n = self.nodes[nid]
            skip = set()
            if n["type"].startswith("ProPick"):
                b = self.literal(n, "branch")
                skip = {i["name"] for i in n["inputs"] if i["name"].startswith("in") and not i["name"].startswith(f"in{b}_")}
            elif n["type"] == "ProGate" and not self.literal(n, "enabled"):
                skip = {"value"}
            for i in n["inputs"]:
                if i.get("link") is not None and i["name"] not in skip:
                    need(self.src[i["link"]][0])

        for nid, n in self.nodes.items():
            if n["type"] in OUTPUT_TYPES:
                need(nid)
        return done


def ran(wf, mode_index):
    sim = Sim(wf, mode_index)
    return sim.executed(), sim.nodes


# 每个工作流、每个模式（下标）：{必须执行的节点 id: 说明} / {绝不能执行的节点 id: 说明}。节点编号见 tools/gen_workflows.py
EXPECT = {
    "01": {0: ({2: "网关生图"}, {3: "阿里生图"}), 1: ({3: "阿里生图"}, {2: "网关生图"})},
    "02": {0: ({2: "网关生图"}, {3: "阿里生图", 6: "商品图"}), 1: ({2: "网关生图", 6: "商品图"}, {3: "阿里生图"}),
           2: ({3: "阿里生图"}, {2: "网关生图", 6: "商品图"})},
    "03": {0: ({2: "网关", 4: "商品图"}, {3: "阿里", 5: "场景图"}), 1: ({2: "网关", 4: "商品图", 5: "场景图"}, {3: "阿里"}),
           2: ({3: "阿里", 4: "商品图"}, {2: "网关", 5: "场景图"}), 3: ({3: "阿里", 4: "商品图", 5: "场景图"}, {2: "网关"})},
    "05": {0: ({3: "放大"}, {6: "标签"}), 1: ({6: "标签"}, {3: "放大"}), 2: ({3: "放大", 6: "标签"}, {})},
    "07": {0: ({70: "写文案"}, {3: "商品图"}), 1: ({70: "写文案", 3: "商品图"}, {}), 2: ({70: "翻译"}, {3: "商品图"})},
    # 设计 / 克隆会在阿里账号里建音色，没选就绝不能跑；AI 写音色描述只有设计模式用
    "08": {0: ({3: "配音"}, {2: "设计音色", 7: "克隆音色", 70: "写音色描述", 6: "样音"}),
           1: ({3: "配音", 2: "设计音色", 70: "写音色描述"}, {7: "克隆音色", 6: "样音"}),
           2: ({3: "配音", 7: "克隆音色", 6: "样音"}, {2: "设计音色", 70: "写音色描述"})},
    "09": {0: ({12: "Gemini 音乐"}, {22: "Suno"}), 1: ({22: "Suno"}, {12: "Gemini 音乐"})},
    "10": {0: ({12: "Veo", 5: "合成"}, {3: "配音", 7: "字幕", 4: "背景音乐"}), 1: ({12: "Veo", 5: "合成", 3: "配音", 7: "字幕", 4: "背景音乐"}, {})},
    "12": {0: ({12: "Veo", 5: "合成", 2: "参考图", 15: "缩图", 70: "写提示词"}, {3: "配音", 7: "字幕", 4: "背景音乐"}),
           1: ({12: "Veo", 5: "合成", 2: "参考图", 15: "缩图", 70: "写提示词", 3: "配音", 7: "字幕", 4: "背景音乐"}, {})},
    # 对白成片：对白（3）两个模式都要；上传视频和图片轮播二选一
    "14": {0: ({2: "上传视频", 3: "对白", 4: "背景音乐", 5: "合成"}, {8: "轮播", 12: "图片", 13: "图片", 14: "图片"}),
           1: ({8: "轮播", 12: "图片", 13: "图片", 14: "图片", 3: "对白", 4: "背景音乐", 5: "合成"}, {2: "上传视频"})},
    # 图层合成：背景图只在「放到背景图上」模式读（不然白白读一张默认图，用户也看不出来为什么）
    "16": {0: ({2: "商品", 3: "背景图", 5: "合成"}, {}), 1: ({2: "商品", 5: "合成"}, {3: "背景图"})},
    "11": {0: ({2: "上传视频", 3: "配音", 7: "文案字幕", 4: "背景音乐"}, {8: "轮播", 10: "听写", 15: "听写字幕", 12: "图片"}),
           1: ({2: "上传视频", 9: "取音轨", 10: "听写", 15: "听写字幕"}, {3: "配音", 7: "文案字幕", 8: "轮播", 4: "背景音乐", 12: "图片"}),
           2: ({8: "轮播", 12: "图片", 3: "配音", 7: "文案字幕", 4: "背景音乐"}, {2: "上传视频", 10: "听写", 15: "听写字幕"})},
}


class LazyExecution(unittest.TestCase):
    def test_every_mode_runs_only_what_it_should(self):
        for num, modes in EXPECT.items():
            wf = load(num)
            opts = FLOW.MODES[next(n["type"] for n in wf["nodes"] if n["type"].startswith("ProMode"))][1]
            self.assertEqual(sorted(modes), list(range(len(opts))), f"{num}: 每个模式都要有期望")
            for mi, (must, mustnt) in modes.items():
                got, nodes = ran(wf, mi)
                for nid, what in must.items():
                    self.assertIn(nid, got, f"{num} 模式 {mi}「{opts[mi]}」：{what}（节点 {nid}）应该执行")
                for nid, what in mustnt.items():
                    self.assertNotIn(nid, got, f"{num} 模式 {mi}「{opts[mi]}」：{what}（节点 {nid}）不该执行")

    def test_every_workflow_without_mode_runs_all_non_output_nodes(self):
        """没有模式的工作流（04 06 13）所有节点都该参与；否则说明有节点没接到输出。"""
        for num in ("04", "06", "13", "15"):
            wf = load(num)
            got, nodes = ran(wf, None)
            self.assertEqual(got, set(nodes), f"{num}: 有节点没被任何输出用到：{sorted(set(nodes) - got)}")

    def test_picks_and_gates_are_fully_wired(self):
        """ComfyUI 要求选中那一路的输入必须连了线（否则 NodeInputError）；这里要求所有路都连好，换模式才不会炸。"""
        for p in all_workflows():
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            for n in wf["nodes"]:
                if n["type"].startswith("ProPick") or n["type"] == "ProGate":
                    ins = [i for i in n["inputs"] if i["name"].startswith("in") or i["name"] == "value"]
                    self.assertTrue(ins and all(i.get("link") is not None for i in ins), f"{os.path.basename(p)}: 节点 {n['id']} 有没连线的输入")

    # relayapi 的生成节点失败时，媒体输出是 ExecutionBlocker（下游被静默跳过），错误只在 response 输出里
    BLOCKABLE = {("RelayImageGenerator", "image"), ("RelayVideoGenerator", "video"), ("RelaySoundGenerator", "audio")}

    def test_failure_status_is_not_blocked_together_with_media(self):
        """选择分支只要有一个输入被 ExecutionBlocker 拦住，它的所有输出都会被拦——状态和媒体放在同一个选择分支里，
        生成失败时界面会「成功」却什么都没有（Suno 实测踩过）。所以带媒体的选择分支只能有 1 个输出；每个生成节点的 response 必须有人显示。"""
        for p in all_workflows():
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            nodes = {n["id"]: n for n in wf["nodes"]}
            src = {l[0]: (l[1], l[2]) for l in wf["links"]}
            for n in wf["nodes"]:
                m = re.match(r"ProPick\dx(\d)", n["type"])
                if m and int(m.group(1)) > 1:
                    for i in n["inputs"]:
                        if i.get("link") is not None and i["name"].startswith("in"):
                            sid, slot = src[i["link"]]
                            key = (nodes[sid]["type"], nodes[sid]["outputs"][slot]["name"])
                            self.assertNotIn(key, self.BLOCKABLE, f"{os.path.basename(p)}: 选择分支 {n['id']} 同时接了会被拦的媒体输出 {key} 和别的通道")
                if n["type"] in ("RelayImageGenerator", "RelayVideoGenerator", "RelaySoundGenerator"):
                    resp = next(o for o in n["outputs"] if o["name"] == "response")
                    self.assertTrue(resp.get("links"), f"{os.path.basename(p)}: 节点 {n['id']}（{n['type']}）的 response 没人接，失败时看不到原因")

    def test_branch_values_in_range(self):
        for p in all_workflows():
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            nodes = {n["id"]: n for n in wf["nodes"]}
            src = {l[0]: (l[1], l[2]) for l in wf["links"]}
            for n in wf["nodes"]:
                if not n["type"].startswith("ProPick"):
                    continue
                branches = int(re.match(r"ProPick(\d)x", n["type"]).group(1))
                link = next(i for i in n["inputs"] if i["name"] == "branch")["link"]
                sid, slot = src[link]
                for v in FLOW.MODES[nodes[sid]["type"]][2][slot][2]:
                    self.assertTrue(0 <= v < branches, f"{os.path.basename(p)}: 节点 {n['id']} 的 branch 取到 {v}，只有 {branches} 路")

    def test_simulator_itself_catches_a_bad_wiring(self):
        """模拟器本身要能抓到错误：把输出节点直接接到某一路的引擎上，没选中的引擎就会被执行。"""
        wf = load("01")
        preview = next(n for n in wf["nodes"] if n["type"] == "PreviewAny")
        link = next(i for i in preview["inputs"] if i["name"] == "source")["link"]
        bad_src = 3  # 阿里生图
        wf["links"] = [l if l[0] != link else [l[0], bad_src, 1, l[3], l[4], l[5]] for l in wf["links"]]
        got, _ = ran(wf, 0)
        self.assertIn(3, got, "输出节点直接接到某一路节点上，就会强制执行它——模拟器必须能发现")


if __name__ == "__main__":
    unittest.main()
