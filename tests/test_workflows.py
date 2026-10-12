"""工作流校验：模板与生成脚本一致、连线完整、节点类型存在、不压住节点；
以及「惰性执行模拟」：每种模式下按 ComfyUI 的规则算出会执行哪些节点，没选中的引擎 / 设计 / 克隆 / 轮播不能被执行。

工作流文件是合并后的 5 个应用（每个文件里好几个功能，见 tools/gen_workflows.py）。两类检查：
- 文件本身（合并后的）：连线、节点不重叠、分组框、颜色、功能开关……用 all_workflows()（文件路径）；
- 每个功能（旧的 19 个单功能工作流）：用 app_views() / load(编号) —— 从合并文件里把这个功能抠出来（api_convert.family_view，工作台运行时用的就是它），
  再做以前对单个工作流做的所有检查；另有一个测试保证抠出来的和 gen_workflows.py 里单独搭出来的完全一样。"""
import copy
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
_spec = importlib.util.spec_from_file_location("t_api_convert_wf", os.path.join(ROOT, "custom-nodes", "pro-chat", "api_convert.py"))
AC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AC)
_spec = importlib.util.spec_from_file_location("t_gen_workflows", os.path.join(ROOT, "tools", "gen_workflows.py"))      # 导入不会写文件（只有当脚本运行才会）
GEN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(GEN)

# 有「★ 只填这里：一句话需求」（阿里 写提示词节点，编号 70）/ 有「★ 先选模式」（模式节点，编号 77）的工作流
WRITER_NUMS = {"01", "02", "03", "04", "06", "07", "08", "09", "10", "12"}
MODE_NUMS = {"01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "14", "16", "17"}      # 这两组是功能的旧编号（目录里的应用编号）

# ComfyUI 核心 / 插件里工作流用到的节点；自带节点从源码里的 NODE_CLASS_MAPPINGS 读（pro-flow 是函数生成的，直接 import）
KNOWN_EXTERNAL = {
    "RelayAPISettings", "RelayImageGenerator", "RelayVideoGenerator", "RelaySoundGenerator", "RelayTextGenerator",
    "LoadImage", "LoadAudio", "LoadVideo", "SaveImage", "SaveAudioAdvanced", "SaveVideo", "PreviewImage", "PreviewAny",
    "ImageScale", "ImageScaleBy", "ImageScaleToMaxDimension", "ImageSharpen", "ImageBatch", "Note", "MarkdownNote",
    "PrimitiveNode", "PrimitiveStringMultiline", "StringConcatenate",
}
OUTPUT_TYPES = {"SaveImage", "PreviewImage", "PreviewAny", "SaveVideo", "SaveAudioAdvanced", "SaveAudio", "ProVideoDub", "ProAppText", "ProSaveCutout", "ProLayerCompose", "ProVideoSave"}


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
    """合并后的工作流文件（5 个）。"""
    return sorted(glob.glob(os.path.join(WF, "*", "*.json")))


def read(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


_VIEWS = []


def app_views():
    """[(功能编号, 功能名, 单功能工作流)]：每个合并文件里的每个功能抠出来（和工作台运行时用的一样），按文件顺序。"""
    if not _VIEWS:
        for p in all_workflows():
            wf = read(p)
            for k, fn in enumerate(AC.family_of(wf)["funcs"]):
                _VIEWS.append((str(fn["id"]), fn["name"], AC.family_view(wf, k)))
    return [(fid, name, copy.deepcopy(wf)) for fid, name, wf in _VIEWS]          # 给副本：有的测试会改它


def load(num):
    """功能 num 的单功能工作流（节点编号还是旧的 1~99）。"""
    return next(wf for fid, _name, wf in app_views() if fid == num)            # app_views 给的是副本


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
        """合并后的应用文件编号 01~05 连续、按 FAMILIES 放进类别文件夹；里面的功能（旧编号）一共 01~19，不重复，顺序和功能节点的下拉一致。"""
        nums = [os.path.basename(p).split("-")[0] for p in all_workflows()]
        self.assertEqual(nums, [f"{i:02d}" for i in range(1, 6)], "应用文件应该是 01~05 连续编号")
        for p, (num, name, cls, ids) in zip(all_workflows(), GEN.FAMILIES):
            self.assertEqual(os.path.basename(p), f"{num}-{name}.app.json")
            self.assertEqual(os.path.basename(os.path.dirname(p)), next(d for d, ns in GEN.FOLDERS.items() if num in ns))
            fam = AC.family_of(read(p))
            self.assertEqual([f["id"] for f in fam["funcs"]], ids)
            self.assertEqual([f["name"] for f in fam["funcs"]], FLOW.FUNCS[cls][1], "功能名要和功能节点（pro-flow 的 FUNCS）下拉里的一一对应")
        ids = [fid for fid, _n, _wf in app_views()]
        self.assertEqual(sorted(ids), [f"{i:02d}" for i in range(1, 20)], "功能应该是 01~19 连续编号，每个只出现一次")

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
        want = {("ProPick2x1", "branch"): "INT", ("ProGate", "enabled"): "BOOLEAN", ("ProBlock", "enabled"): "BOOLEAN", ("ProPosterPrompt", "use_product_image"): "BOOLEAN",
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
        for num, _name, wf in app_views():
            nodes = {n["id"]: n for n in wf["nodes"]}
            stars = [n for n in wf["nodes"] if n.get("title", "").startswith("★")]
            self.assertEqual(len(stars), (num in MODE_NUMS) + (num in WRITER_NUMS), f"{num}: ★ 节点数量不对")
            if not stars:
                continue
            own = [n for n in wf["nodes"] if n["type"] != "RelayAPISettings"]                    # 共用的 Key 节点摆在合并文件里第一个功能的块里，不算这个功能自己的
            self.assertEqual(min(n["pos"][1] for n in own), min(n["pos"][1] for n in stars), f"{num}: ★ 节点应该在最上面")
            if num in MODE_NUMS:
                self.assertTrue(nodes[77]["type"].startswith("ProMode"), num)
                self.assertEqual(nodes[77]["widgets_values"][0], FLOW.MODES[nodes[77]["type"]][1][0], f"{num}: 模式默认值应是第一个选项")
            if num in WRITER_NUMS:
                w = nodes[70]
                self.assertEqual(w["type"], "ProAliPromptWriter")
                self.assertIsNotNone(next(i for i in w["inputs"] if i["name"] == "info")["link"], f"{num}: 写提示词节点没接 Key（Settings 节点 id 31）")
                # 谷歌的写提示词模型走网关：每个有写提示词的应用都要接「Key：网关」（Settings 节点 id 1，Key 按节点 id 存、各应用共用），阿里的 Key 还是节点 31
                links = {l[0]: l for l in wf["links"]}
                gw = links[next(i for i in w["inputs"] if i["name"] == "gateway_info")["link"]]
                self.assertEqual((gw[1], nodes[gw[1]]["type"], nodes[gw[1]]["title"]), (1, "RelayAPISettings", "Key：网关（填一次）"), f"{num}: 写提示词节点没接网关 Key：选谷歌模型会用不了")
                self.assertEqual(nodes[1]["widgets_values"][6], "http://airelay-newapi:3000", f"{num}: 网关地址（custom_api_base）不对")
                self.assertTrue(nodes[1]["flags"].get("collapsed"), f"{num}: Key 节点要折叠")
                self.assertEqual(links[next(i for i in w["inputs"] if i["name"] == "info")["link"]][1], 31, f"{num}: 阿里的 Key 应该是节点 31")
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
                if t in self.MUST_GREEN or t.startswith("ProMode") or t.startswith("ProFunc"):
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

    def test_zone_groups(self):
        """分组框：最上面一个「先选做什么」框（里面只有功能节点）；之后每个功能三个框，标题以「【功能名】」开头：① 先填这里（绿）、② 自动处理（含黄色 Key、功能开关）、③ 结果（蓝）。
        每个节点整个落在自己角色的框里（共用的 Key 在第一个用到它的功能的 ② 里），所有框互不相交，每个功能的三个框里都有对应角色的节点。"""
        zone_of = {"IN": "①", "KEY": "②", "AUTO": "②", "OUT": "③"}
        for p in all_workflows():
            name = os.path.basename(p)
            wf = read(p)
            fam = AC.family_of(wf)
            groups = wf["groups"]
            self.assertEqual(len(groups), 1 + 3 * len(fam["funcs"]), f"{name}: 分组框个数不对（要 1 个「先选做什么」+ 每个功能 3 个）")
            self.assertTrue(groups[0]["title"].startswith("★ 先选做什么"), name)
            self.assertEqual([g["id"] for g in groups], list(range(1, len(groups) + 1)), f"{name}: 分组框编号要从 1 连续")
            boxes = [(g["bounding"][0], g["bounding"][1], g["bounding"][0] + g["bounding"][2], g["bounding"][1] + g["bounding"][3]) for g in groups]
            for i, a in enumerate(boxes):
                for b in boxes[i + 1:]:
                    self.assertFalse(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3], f"{name}: 分组框互相重叠")
            for k, f in enumerate(fam["funcs"]):
                titles = [g["title"] for g in groups[1 + 3 * k: 4 + 3 * k]]
                self.assertEqual([t[:len(f["name"]) + 3] for t in titles], [f"【{f['name']}】{z}" for z in "①②③"], f"{name}: 功能「{f['name']}」的三个分组框标题不对：{titles}")
            owner = {}                                                                 # 节点 → 它该落在第几个框里（下标）
            func_node = fam["func_node"]
            for n in wf["nodes"]:
                if n["id"] == func_node:
                    want = 0
                else:
                    k = next((k for k, f in enumerate(fam["funcs"]) if f["base"] < n["id"] < f["base"] + 1000), None)
                    if k is None:                                                      # 共用的 Key：第一个用到它的功能的 ② 里
                        k = next(k for k, f in enumerate(fam["funcs"]) if n["id"] in f["shared"])
                        self.assertEqual(n["type"], "RelayAPISettings", f"{name}: 节点 {n['id']} 不属于任何功能")
                    want = 1 + 3 * k + "①②③".index(zone_of[self.role_of(n)])
                owner[n["id"]] = want
                r = self.rect(n)
                inside = [i for i, b in enumerate(boxes) if b[0] <= r[0] and b[1] <= r[1] and r[2] <= b[2] and r[3] <= b[3]]
                self.assertEqual(inside, [want], f"{name}: 节点 {n['id']}（{n['type']}，{self.role_of(n)}）应该整个落在分组框 {want + 1} 里，实际 {[i + 1 for i in inside]}")
            for i in range(1, len(groups)):                                            # 没有空框
                self.assertIn(i, owner.values(), f"{name}: 分组框 {i + 1}（{groups[i]['title']}）是空的")

    def test_opens_focused_on_the_function_picker(self):
        """打开时直接对准最上面的「先选做什么」：缩放不低于 0.62（再小 ComfyUI 不画节点上的字），分组框左上角避开左侧栏和顶部工具栏（标题不被盖住），功能节点一屏内看得到。"""
        for p in all_workflows():
            name = os.path.basename(p)
            wf = read(p)
            ds = wf["extra"]["ds"]
            self.assertGreaterEqual(ds["scale"], 0.62, f"{name}: 打开时缩放太小，节点上的字会不显示")
            to_screen = lambda x, y: ((x + ds["offset"][0]) * ds["scale"], (y + ds["offset"][1]) * ds["scale"])
            gx, gy = wf["groups"][0]["bounding"][:2]
            sx, sy = to_screen(gx, gy)
            self.assertTrue(60 <= sx <= 100 and 100 <= sy <= 150, f"{name}: 「先选做什么」应该在屏幕左上角、避开侧栏和顶部工具栏，实际 ({sx:.0f}, {sy:.0f})")
            func = next(n for n in wf["nodes"] if n["type"].startswith("ProFunc"))
            r = self.rect(func)
            ex, ey = to_screen(r[2], r[3])
            self.assertTrue(ex <= 1100 and ey <= 720, f"{name}: 功能节点打开时超出一屏（右下角 {ex:.0f}, {ey:.0f}）")


    # ── 应用界面（ComfyUI 的 App 模式）：extra.linearData = {inputs: [[节点 id, 控件名]], outputs: [节点 id]}，extra.linearMode = True ──
    # 核心节点 / relayapi 的控件名（来自服务器 /object_info；自带的 pro-* 节点直接读它们的 INPUT_TYPES，不在这里重复写）
    KNOWN_WIDGETS = {
        "LoadImage": {"image"}, "LoadAudio": {"audio"}, "LoadVideo": {"file"}, "PrimitiveStringMultiline": {"value"},
        "RelayImageGenerator": {"prompt", "ratio", "size", "quality", "format", "moderation", "seed"},
        "RelayVideoGenerator": {"prompt", "ratio", "size", "duration", "seed", "enhance_prompt", "enable_HD"},
        "ImageScaleBy": {"upscale_method", "scale_by"}, "ImageSharpen": {"sharpen_radius", "sigma", "alpha"},
        "RelaySoundGenerator": {"generation_mode", "title", "tags", "prompt", "make_instrumental", "version", "seed", "negative_tags", "extend_mode", "continue_clip_id", "continue_at"},
        # 网关 Settings：故意不含 apikey —— Key 绝不能当「可调设置」登记出来（工作台 / 聊天里会明文显示、存进浏览器）
        "RelayAPISettings": {"api_base", "api_format", "model", "platform", "task_type", "custom_api_base", "custom_model"},
    }
    APP_OUTPUT_TYPES = {"SaveImage", "SaveVideo", "SaveAudioAdvanced", "ProVideoDub", "ProAppText", "ProSaveCutout", "ProLayerCompose", "ProVideoSave"}
    # 应用界面默认显示「最后执行完的那一条」。这些预览节点的文字不放进应用：它们和成片几乎同时执行完，会把成片挤到第二位（11：字幕已烧进成片、也存了 .srt）
    APP_TEXT_EXEMPT = {"11": {20}}
    MEDIA_SAVERS = {"SaveImage", "SaveVideo", "SaveAudioAdvanced", "ProVideoDub", "ProSaveCutout", "ProLayerCompose", "ProVideoSave"}

    def test_the_engine_mode_offers_exactly_the_ali_models_the_ali_node_accepts(self):
        """04 / 06 的「出图引擎和模型」下拉：第 1 项是网关（不指定阿里模型），后面每一项对应阿里改图节点真能用的一个模型（顺序也一样）；
        每个阿里改图节点的 use_model 接的都是模式节点的 model 输出，选择分支的 branch 接的是它的 branch 输出。"""
        ali = load_node_pkg("pro-ali")
        _title, options, outs = FLOW.MODES["ProModeEngine"]
        outputs = {name: vals for name, _t, vals in outs}
        self.assertEqual(outputs["model"][0], "")
        self.assertEqual(outputs["model"][1:], ali.EDIT_MODELS)
        self.assertEqual(outputs["branch"], [0] + [1] * len(ali.EDIT_MODELS))
        self.assertEqual(len(options), len(outputs["model"]))
        for opt, model in zip(options[1:], outputs["model"][1:]):
            self.assertIn(model, opt)                                                        # 每个选项的文字里写着它用的模型名
        self.assertIn("gemini-image", options[0])
        for num, n_ali in (("04", 4), ("06", 2)):
            wf = load(num)
            nodes = {n["id"]: n for n in wf["nodes"]}
            src = {l[0]: (l[1], l[2]) for l in wf["links"]}
            ali_nodes = [n for n in wf["nodes"] if n["type"] == "ProAliImageEdit"]
            self.assertEqual(len(ali_nodes), n_ali, num)
            for n in ali_nodes:
                sid, slot = src[next(i for i in n["inputs"] if i["name"] == "use_model")["link"]]
                self.assertEqual((nodes[sid]["type"], nodes[sid]["outputs"][slot]["name"]), ("ProModeEngine", "model"), num)
            picks = [n for n in wf["nodes"] if n["type"] == "ProPick2x1"]
            self.assertEqual(len(picks), 2 * n_ali, num)                                       # 每个出图步骤两个：取图 + 取状态
            for n in picks:
                sid, slot = src[next(i for i in n["inputs"] if i["name"] == "branch")["link"]]
                self.assertEqual((nodes[sid]["type"], nodes[sid]["outputs"][slot]["name"]), ("ProModeEngine", "branch"), num)

    def test_dubbing_voice_is_selectable_in_every_app_with_a_dubbing_node(self):
        """有「阿里 配音」节点的应用（08 / 10 / 11 / 12），右栏都要能选音色：音色是配音最常想换的东西，聊天里（pro-chat）也靠右栏登记的控件来设。"""
        found = 0
        for num, fname, wf in app_views():
            name = f"{num}-{fname}"
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
        for num, fname, wf in app_views():
            name = f"{num}-{fname}"
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
        for num, fname, wf in app_views():
            name = f"{num}-{fname}"
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
        self.assertEqual(load("04")["extra"]["linearData"]["inputs"], [[77, "mode"], [70, "idea"], [2, "image"]], "04 右栏：出图引擎和模型 + 一句话 + 商品图")

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
        no_writer = lambda wf: [e for e in wf["extra"]["appExtra"] if e[:2] != [70, "model"]]
        self.assertEqual(no_writer(wf12), no_writer(wf10))                                  # 视频 / 配音 / 字幕的可调设置两个应用一样（只有「写提示词的模型」的说明不同：12 要看图，默认 omni）
        hint = lambda wf: next(e[3] for e in wf["extra"]["appExtra"] if e[:2] == [70, "model"])
        self.assertIn("看图", hint(wf12))
        self.assertIn("qwen3.8-flash", hint(wf10))

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
        for num, fname, wf in app_views():
            name = f"{num}-{fname}"
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
    # 04 多尺寸套图 / 06 商品一条龙：模式 0 = 网关 Gemini（4 个 / 2 个网关生图节点），模式 1~5 = 阿里的 5 个模型（同样数量的阿里改图节点）；没选中的引擎不能跑（不然没填 Key 的那一路会报错、白花钱）
    "04": {0: ({10: "网关 1:1", 11: "网关 3:4", 12: "网关 9:16", 13: "网关 16:9", 2: "商品图", 70: "写提示词"}, {40: "阿里 1:1", 41: "阿里 3:4", 42: "阿里 9:16", 43: "阿里 16:9"}),
           **{m: ({40: "阿里 1:1", 41: "阿里 3:4", 42: "阿里 9:16", 43: "阿里 16:9", 2: "商品图", 70: "写提示词"}, {10: "网关 1:1", 11: "网关 3:4", 12: "网关 9:16", 13: "网关 16:9"}) for m in range(1, 6)}},
    "05": {0: ({3: "放大"}, {6: "标签"}), 1: ({6: "标签"}, {3: "放大"}), 2: ({3: "放大", 6: "标签"}, {})},
    "06": {0: ({4: "网关白底主图", 8: "网关场景图", 10: "文案", 70: "写场景指令", 3: "商品图"}, {24: "阿里白底主图", 28: "阿里场景图"}),
           **{m: ({24: "阿里白底主图", 28: "阿里场景图", 10: "文案", 70: "写场景指令", 3: "商品图"}, {4: "网关白底主图", 8: "网关场景图"}) for m in range(1, 6)}},
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
    # 视频改视频：参考图只在带参考图的模式读（没选的槽位读一张默认图再传给模型就是错的）
    "17": {0: ({2: "视频", 5: "改视频", 6: "保存"}, {3: "参考图 1", 4: "参考图 2"}), 1: ({2: "视频", 3: "参考图 1", 5: "改视频", 6: "保存"}, {4: "参考图 2"}),
           2: ({2: "视频", 3: "参考图 1", 4: "参考图 2", 5: "改视频", 6: "保存"}, {})},
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
        """没有模式的工作流（13 15 18 19）所有节点都该参与；否则说明有节点没接到输出。"""
        for num in ("13", "15", "18", "19"):
            wf = load(num)
            got, nodes = ran(wf, None)
            self.assertEqual(got, set(nodes), f"{num}: 有节点没被任何输出用到：{sorted(set(nodes) - got)}")

    def test_picks_and_gates_are_fully_wired(self):
        """ComfyUI 要求选中那一路的输入必须连了线（否则 NodeInputError）；这里要求所有路都连好，换模式才不会炸。"""
        for num, fname, wf in app_views():
            name = f"{num}-{fname}"
            for n in wf["nodes"]:
                if n["type"].startswith("ProPick") or n["type"] == "ProGate":
                    ins = [i for i in n["inputs"] if i["name"].startswith("in") or i["name"] == "value"]
                    self.assertTrue(ins and all(i.get("link") is not None for i in ins), f"{name}: 节点 {n['id']} 有没连线的输入")

    # relayapi 的生成节点失败时，媒体输出是 ExecutionBlocker（下游被静默跳过），错误只在 response 输出里
    BLOCKABLE = {("RelayImageGenerator", "image"), ("RelayVideoGenerator", "video"), ("RelaySoundGenerator", "audio")}

    def test_failure_status_is_not_blocked_together_with_media(self):
        """选择分支只要有一个输入被 ExecutionBlocker 拦住，它的所有输出都会被拦——状态和媒体放在同一个选择分支里，
        生成失败时界面会「成功」却什么都没有（Suno 实测踩过）。所以带媒体的选择分支只能有 1 个输出；每个生成节点的 response 必须有人显示。"""
        for num, fname, wf in app_views():
            name = f"{num}-{fname}"
            nodes = {n["id"]: n for n in wf["nodes"]}
            src = {l[0]: (l[1], l[2]) for l in wf["links"]}
            for n in wf["nodes"]:
                m = re.match(r"ProPick\dx(\d)", n["type"])
                if m and int(m.group(1)) > 1:
                    for i in n["inputs"]:
                        if i.get("link") is not None and i["name"].startswith("in"):
                            sid, slot = src[i["link"]]
                            key = (nodes[sid]["type"], nodes[sid]["outputs"][slot]["name"])
                            self.assertNotIn(key, self.BLOCKABLE, f"{name}: 选择分支 {n['id']} 同时接了会被拦的媒体输出 {key} 和别的通道")
                if n["type"] in ("RelayImageGenerator", "RelayVideoGenerator", "RelaySoundGenerator"):
                    resp = next(o for o in n["outputs"] if o["name"] == "response")
                    self.assertTrue(resp.get("links"), f"{name}: 节点 {n['id']}（{n['type']}）的 response 没人接，失败时看不到原因")

    def test_branch_values_in_range(self):
        for num, fname, wf in app_views():
            name = f"{num}-{fname}"
            nodes = {n["id"]: n for n in wf["nodes"]}
            src = {l[0]: (l[1], l[2]) for l in wf["links"]}
            for n in wf["nodes"]:
                if not n["type"].startswith("ProPick"):
                    continue
                branches = int(re.match(r"ProPick(\d)x", n["type"]).group(1))
                link = next(i for i in n["inputs"] if i["name"] == "branch")["link"]
                sid, slot = src[link]
                for v in FLOW.MODES[nodes[sid]["type"]][2][slot][2]:
                    self.assertTrue(0 <= v < branches, f"{name}: 节点 {n['id']} 的 branch 取到 {v}，只有 {branches} 路")

    def test_simulator_itself_catches_a_bad_wiring(self):
        """模拟器本身要能抓到错误：把输出节点直接接到某一路的引擎上，没选中的引擎就会被执行。"""
        wf = load("01")
        preview = next(n for n in wf["nodes"] if n["type"] == "PreviewAny")
        link = next(i for i in preview["inputs"] if i["name"] == "source")["link"]
        bad_src = 3  # 阿里生图
        wf["links"] = [l if l[0] != link else [l[0], bad_src, 1, l[3], l[4], l[5]] for l in wf["links"]]
        got, _ = ran(wf, 0)
        self.assertIn(3, got, "输出节点直接接到某一路节点上，就会强制执行它——模拟器必须能发现")


# ── 合并后的应用（一个文件里好几个功能）──────────────────────────────────────────────────────────────────────
def graph_signature(wf):
    """图的本质：节点（编号、类型、控件值、标题、输入输出名和类型、控件名标签）+ 连线（起点、起点输出、终点、终点输入、类型）+ 应用设置；不比位置、分组、颜色、顺序、连线编号。"""
    nodes = {}
    for n in wf["nodes"]:
        nodes[n["id"]] = (n["type"], json.dumps(n.get("widgets_values"), ensure_ascii=False), n.get("title"), n.get("mode"),
                          tuple((i["name"], i["type"], bool(i.get("widget")), i.get("label"), i.get("link") is not None) for i in n["inputs"]),
                          tuple((o["name"], o["type"], len(o.get("links") or [])) for o in n["outputs"]))
    links = sorted((l[1], l[2], l[3], l[4], l[5]) for l in wf["links"])
    extra = {k: wf["extra"][k] for k in GEN.FAMILY_EXTRA_KEYS if k in wf["extra"]}
    return nodes, links, json.dumps(extra, sort_keys=True, ensure_ascii=False)


def owner_index(fam, nid):
    """节点编号 → 它属于第几个功能（编号落在这个功能的 (基数, 基数 + 1000) 里）；功能节点、共用的 Key 返回 None。"""
    return next((k for k, f in enumerate(fam["funcs"]) if f["base"] < nid < f["base"] + 1000), None)


class SimMerged(Sim):
    """合并应用的惰性执行模拟：选中第 func 个功能、它的第 mode_index 个模式时，会执行哪些节点。
    输出节点只要有一条输入线来自没放行的功能开关，就被拦住、不执行（ComfyUI 的 ExecutionBlocker 规则）；功能开关本身会执行，但没放行时不要它的上游（lazy）。"""

    def __init__(self, wf, func, mode_index):
        super().__init__(wf, mode_index)
        self.func = func
        self.fam = AC.family_of(wf)

    def literal(self, node, name):
        inp = next((i for i in node["inputs"] if i["name"] == name), None)
        if inp is not None and inp.get("link") is not None:
            sid, slot = self.src[inp["link"]]
            src = self.nodes[sid]
            if src["type"].startswith("ProFunc"):
                return FLOW.func_outputs(FLOW.FUNCS[src["type"]][1])[slot][2][self.func]
            assert src["type"].startswith("ProMode"), f"{node['id']}.{name} 应连到功能节点或模式节点，实际连到 {src['type']}"
            active = owner_index(self.fam, sid) == self.func
            return FLOW.MODES[src["type"]][2][slot][2][(self.mode_index or 0) if active else 0]
        return node["widgets_values"][0]

    def closed(self, inp):
        """这条输入线来自一个没放行的功能开关吗。"""
        if inp.get("link") is None:
            return False
        src = self.nodes[self.src[inp["link"]][0]]
        return src["type"] == "ProBlock" and not self.literal(src, "enabled")

    def executed(self):
        done = set()

        def need(nid):
            if nid in done:
                return
            n = self.nodes[nid]
            if n["type"] in OUTPUT_TYPES and any(self.closed(i) for i in n["inputs"]):
                for i in n["inputs"]:                       # 输出节点被拦住，不执行；它前面的功能开关会执行（算出「拦住」），但不要开关的上游
                    if i.get("link") is not None:
                        need(self.src[i["link"]][0])
                return
            done.add(nid)
            skip = set()
            if n["type"].startswith("ProPick"):
                b = self.literal(n, "branch")
                skip = {i["name"] for i in n["inputs"] if i["name"].startswith("in") and not i["name"].startswith(f"in{b}_")}
            elif n["type"] in ("ProGate", "ProBlock") and not self.literal(n, "enabled"):
                skip = {"value"}
            for i in n["inputs"]:
                if i.get("link") is not None and i["name"] not in skip:
                    need(self.src[i["link"]][0])

        for nid, n in self.nodes.items():
            if n["type"] in OUTPUT_TYPES:
                need(nid)
        return done


class MergedApps(unittest.TestCase):
    """5 个合并应用：每个功能抠出来和单独搭的单功能工作流一模一样；选中一个功能只运行它自己的节点；原生应用界面（右栏）的登记。"""

    def test_every_function_cut_out_of_the_merged_file_equals_the_single_function_workflow(self):
        """工作台运行的是抠出来的那一份（api_convert.family_view）：它必须和 gen_workflows.py 里单独搭出来的完全一样（节点、控件值、标题、连线、右栏 / 设置 / 费用 / 描述）。"""
        seen = []
        for p in all_workflows():
            wf = read(p)
            for k, f in enumerate(AC.family_of(wf)["funcs"]):
                self.assertEqual(graph_signature(AC.family_view(wf, k)), graph_signature(GEN.LEGACY[f["id"]]()), f"{os.path.basename(p)} 里的功能 {f['id']}「{f['name']}」抠出来和单独搭的不一样")
                seen.append(f["id"])
        self.assertEqual(sorted(seen), [f"{i:02d}" for i in range(1, 20)])

    def test_the_view_has_no_function_gates_and_no_other_functions_nodes(self):
        for p in all_workflows():
            wf = read(p)
            fam = AC.family_of(wf)
            for k, f in enumerate(fam["funcs"]):
                view = AC.family_view(wf, k)
                ids = {n["id"] for n in view["nodes"]}
                self.assertTrue(all(i < 1000 for i in ids), f"{f['id']}: 抠出来的节点编号要还原成 1~99")
                self.assertFalse([n for n in view["nodes"] if n["type"] == "ProBlock" or n["type"].startswith("ProFunc")], f"{f['id']}: 不该带功能节点 / 功能开关")
                for l in view["links"]:
                    self.assertTrue(l[1] in ids and l[3] in ids, f"{f['id']}: 连线指向了不在这个功能里的节点")
                self.assertTrue(set(f["shared"]) <= ids or f["shared"] == [], f"{f['id']}: 共用的 Key 节点要带上")

    # 功能编号 → 名字（目录里的应用名、AI 方案里说的名字）和一个只有这个功能的描述里才有的词：FAMILIES 的编号按位置对应功能节点（pro-flow FUNCS）的下拉选项，
    # 对调两个名字时「用名字比名字」发现不了，这里写死
    PINNED = {"01": ("文生图", "生成一张图片"), "02": ("海报", "促销海报"), "03": ("商品改图与合成", "修改商品图"), "04": ("多尺寸套图", "4 个平台尺寸"), "05": ("图片后处理", "放大 2 倍"),
              "06": ("商品一条龙", "白底主图"), "07": ("文案", "电商文案"), "08": ("配音", "念成语音"), "09": ("音乐", "纯音乐配乐"), "10": ("文生视频成片", "用文字生成约 10 秒的视频"),
              "11": ("视频成片", "已有的素材合成成片"), "12": ("图生视频成片", "用一张图"), "13": ("多角色配音", "多角色对话"), "14": ("对白成片", "对白配到视频上"),
              "15": ("商品抠图", "抠出来"), "16": ("图层合成", "放到背景图或纯色背景上"), "17": ("视频改视频", "AI 改视频"), "18": ("视频拼接", "按顺序接成一条"), "19": ("视频换人", "视频换人")}

    def test_function_numbers_names_and_contents_are_pinned(self):
        got = {}
        for p in all_workflows():
            for f in AC.family_of(read(p))["funcs"]:
                got[str(f["id"])] = (f["name"], f["extra"]["appDescription"])
        self.assertEqual({k: v[0] for k, v in got.items()}, {k: v[0] for k, v in self.PINNED.items()}, "功能编号和名字对不上（FAMILIES 的编号顺序和 pro-flow FUNCS 的名字顺序要一致）")
        for k, (name, word) in self.PINNED.items():
            self.assertIn(word, got[k][1], f"功能 {k}「{name}」的描述里没有「{word}」：编号对应的单功能工作流搭错了？")

    def test_the_function_node_and_the_family_table_agree(self):
        for p, (num, name, cls, ids) in zip(all_workflows(), GEN.FAMILIES):
            wf = read(p)
            fam = AC.family_of(wf)
            nodes = {n["id"]: n for n in wf["nodes"]}
            fnode = nodes[fam["func_node"]]
            self.assertEqual(fnode["type"], cls)
            self.assertEqual(fnode["widgets_values"], [FLOW.FUNCS[cls][1][0]], "默认选第一个功能")
            self.assertEqual([o["name"] for o in fnode["outputs"]], [f"f{i}" for i in range(len(ids))])
            self.assertEqual(fam["name"], name)
            bases = [f["base"] for f in fam["funcs"]]
            self.assertEqual(bases, [1000 * (k + 1) for k in range(len(ids))], "功能的节点编号基数 1000、2000、……")
            for f in fam["funcs"]:
                self.assertTrue(set(f["shared"]) <= set(GEN.SHARED_KEYS))
                for i in f["extra"]["linearData"]["inputs"] + [[o] for o in f["extra"]["linearData"]["outputs"]]:
                    self.assertTrue((i[0] if i[0] in f["shared"] else f["base"] + i[0]) in nodes, f"功能 {f['id']} 登记的节点 {i[0]} 在合并文件里找不到")
            for n in wf["nodes"]:                                            # 每个节点要么属于一个功能，要么是功能节点，要么是共用的 Key
                if n["id"] == fam["func_node"]:
                    continue
                if owner_index(fam, n["id"]) is None:
                    self.assertEqual((n["type"], n["id"] in GEN.SHARED_KEYS), ("RelayAPISettings", True), f"节点 {n['id']}（{n['type']}）不属于任何功能")

    def test_shared_key_nodes_exist_once_and_the_widest_setting_wins(self):
        """Key 按节点编号存：1 网关 / 11 geminiweb / 21 Suno / 31 阿里，每个文件里各只有一个。31 的模型名取能看图的 qwen3.8-omni-flash（只有 06 的文案节点真会读它）。"""
        for p in all_workflows():
            wf = read(p)
            keys = [n["id"] for n in wf["nodes"] if n["type"] == "RelayAPISettings"]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertTrue(set(keys) <= set(GEN.SHARED_KEYS))
            fam = AC.family_of(wf)
            models = {f["keys"]["31"][-1] for f in fam["funcs"] if "31" in f["keys"]}
            for n in wf["nodes"]:
                if n["type"] == "RelayAPISettings" and n["id"] == 31:
                    self.assertEqual(n["widgets_values"][-1], "qwen3.8-omni-flash" if "qwen3.8-omni-flash" in models else models.pop(), os.path.basename(p))      # 有功能要用能看图的，就用它；否则就是大家一样的那个

    def test_every_output_node_input_goes_through_the_function_gate_of_its_own_function(self):
        """没选中的功能不能运行：每个输出节点（保存 / 预览 / 文字结果）的**每一条**输入线都要先过一个功能开关（ComfyUI 先算完输出节点所有没拦的输入，才发现有一条被拦住，
        漏掉一条，那条线的上游就会白白运行）；开关属于同一个功能、enabled 接的是功能节点里这个功能自己那个输出、value 接着线；开关只接输出节点。"""
        oi = read(os.path.join(ROOT, "tests", "object_info_subset.json"))
        flagged = {t for t, v in oi.items() if v.get("output_node")}
        used = {n["type"] for p in all_workflows() for n in read(p)["nodes"]}
        self.assertEqual((flagged & used) - GEN.OUTPUT_NODE_TYPES, set(), "有输出节点类型没登记进 gen_workflows.OUTPUT_NODE_TYPES，它的输入线不会被功能开关拦住")
        gates_seen = 0
        for p in all_workflows():
            name = os.path.basename(p)
            wf = read(p)
            fam = AC.family_of(wf)
            nodes = {n["id"]: n for n in wf["nodes"]}
            src = {l[0]: (l[1], l[2]) for l in wf["links"]}
            for n in wf["nodes"]:
                k = owner_index(fam, n["id"])
                if n["type"] in GEN.OUTPUT_NODE_TYPES:
                    for i in n["inputs"]:
                        if i.get("link") is None:
                            continue
                        sid, _slot = src[i["link"]]
                        g = nodes[sid]
                        self.assertEqual(g["type"], "ProBlock", f"{name}: 输出节点 {n['id']}（{n['type']}）的输入 {i['name']} 没经过功能开关")
                        self.assertEqual(owner_index(fam, g["id"]), k, f"{name}: 功能开关 {g['id']} 和它后面的输出节点 {n['id']} 不是同一个功能")
                        enabled = next(x for x in g["inputs"] if x["name"] == "enabled")
                        self.assertEqual(src[enabled["link"]], (fam["func_node"], k), f"{name}: 功能开关 {g['id']} 的 enabled 要接功能节点的第 {k} 个输出")
                        self.assertIsNotNone(next(x for x in g["inputs"] if x["name"] == "value")["link"], f"{name}: 功能开关 {g['id']} 的 value 没接线")
                        gates_seen += 1
                if n["type"] == "ProBlock":
                    outs = [l for l in wf["links"] if l[1] == n["id"]]
                    self.assertEqual(len(outs), 1, f"{name}: 功能开关 {n['id']} 要刚好接一个输出节点的一条输入线")
                    self.assertIn(nodes[outs[0][3]]["type"], GEN.OUTPUT_NODE_TYPES, f"{name}: 功能开关 {n['id']} 后面接的不是输出节点")
        self.assertEqual(gates_seen, 41 + 6 + 15 + 28 + 3, "5 个应用一共这么多条输出节点的输入线（每条一个开关）")

    def test_selecting_a_function_and_mode_runs_exactly_what_the_single_function_workflow_ran(self):
        """每个功能 × 每个模式：合并文件里（功能开关、功能节点都算进去）实际会执行的节点，去掉开关和功能节点后，和单功能工作流时一模一样；别的功能的节点一个都不执行。"""
        checked = 0
        for p in all_workflows():
            name = os.path.basename(p)
            wf = read(p)
            fam = AC.family_of(wf)
            nodes = {n["id"]: n for n in wf["nodes"]}
            for k, f in enumerate(fam["funcs"]):
                legacy = GEN.LEGACY[f["id"]]()
                mode_type = next((n["type"] for n in legacy["nodes"] if n["type"].startswith("ProMode")), None)
                for m in range(len(FLOW.MODES[mode_type][1]) if mode_type else 1):
                    want = Sim(legacy, m if mode_type else None).executed()
                    got = SimMerged(wf, k, m if mode_type else None).executed()
                    mine = {i - f["base"] for i in got if f["base"] < i < f["base"] + 1000 and nodes[i]["type"] != "ProBlock"}
                    shared = {i for i in got if i in GEN.SHARED_KEYS}
                    foreign = {i for i in got if nodes[i]["type"] != "ProBlock" and (owner_index(fam, i) not in (None, k) or (owner_index(fam, i) is None and i not in GEN.SHARED_KEYS and i != fam["func_node"]))}      # 别的功能的开关会执行（它们要算出「拦住」），别的节点一个都不行
                    self.assertEqual(foreign, set(), f"{name}: 选 {f['id']}「{f['name']}」模式 {m}，别的功能的节点被执行了：{sorted(foreign)}")
                    self.assertEqual(mine | shared, want, f"{name}: 选 {f['id']}「{f['name']}」模式 {m}，执行的节点和单功能工作流不一样")
                    checked += 1
        self.assertGreaterEqual(checked, 46 + 4, "46 个模式 + 4 个没有模式的功能")

    def test_the_simulator_catches_a_missing_gate(self):
        """模拟器本身要能抓到错误：去掉一个功能开关（输出节点直接接上游），没选中的功能就会被执行。"""
        wf = read(all_workflows()[4])                                                      # 05 改视频：选 17（第 0 个）时，18 / 19 不该执行
        fam = AC.family_of(wf)
        nodes = {n["id"]: n for n in wf["nodes"]}
        victim = next(n for n in wf["nodes"] if n["type"] == "ProVideoSave" and owner_index(fam, n["id"]) == 2)            # 19 的保存节点
        link = next(i for i in victim["inputs"] if i["name"] == "video")["link"]
        gate = next(l for l in wf["links"] if l[0] == link)[1]
        raw = next(l for l in wf["links"] if l[3] == gate)                                  # 开关前面那条线：起点 → 开关
        wf["links"] = [[l[0], raw[1], raw[2], l[3], l[4], l[5]] if l[0] == link else l for l in wf["links"]]
        got = SimMerged(wf, 0, 0).executed()
        self.assertTrue(any(owner_index(fam, i) == 2 and nodes[i]["type"] == "ProAliVideoSwap" for i in got), "漏了一个开关，没选中的功能（19 视频换人）就会被执行——模拟器必须能发现")

    def test_a_leftover_single_function_file_never_shadows_the_merged_app_in_the_catalog(self):
        """升级后旧的单功能文件还没移走时（排在合并文件前面也一样）：同编号的功能以合并应用里的为准；用户自己存的、编号不重复的单功能应用照旧进目录。"""
        pc = load_node_pkg("pro-chat")
        oi = read(os.path.join(ROOT, "tests", "object_info_subset.json"))
        d = tempfile.mkdtemp()
        try:
            wf_dir = os.path.join(d, "wf")
            shutil.copytree(WF, wf_dir)
            for rel, fid in (("0-旧版/06-商品一条龙.app.json", "06"), ("工具/99-自己做的.app.json", "06")):
                os.makedirs(os.path.dirname(os.path.join(wf_dir, rel)), exist_ok=True)
                with open(os.path.join(wf_dir, rel), "w", encoding="utf-8") as f:
                    json.dump(GEN.LEGACY[fid](), f, ensure_ascii=False)
            cat = pc.cat_mod.build_catalog(wf_dir, oi)
            self.assertEqual(sorted(cat), [f"{i:02d}" for i in range(1, 20)] + ["99"])
            self.assertEqual((cat["06"]["family"]["name"], cat["06"]["path"]), ("做图", "1-图片/01-做图.app.json"), "旧文件（排在前面）不能把合并应用里的 06 顶掉")
            self.assertIsNone(cat["99"].get("family"))
            self.assertEqual(cat["99"]["name"], "自己做的")
        finally:
            shutil.rmtree(d)

    def test_native_app_panel_lists_every_function_with_a_prefixed_name(self):
        """原生应用界面（右栏）：第一项是「做什么」，后面每个功能的控件（真实存在、没连线）名字前面都标了「【功能名】」，整个应用里不重名；结果节点都是输出节点、每个功能至少有一个。"""
        for p in all_workflows():
            name = os.path.basename(p)
            wf = read(p)
            fam = AC.family_of(wf)
            nodes = {n["id"]: n for n in wf["nodes"]}
            data = wf["extra"]["linearData"]
            self.assertEqual(data["inputs"][0], [fam["func_node"], "mode"], name)
            self.assertEqual(next(i for i in nodes[fam["func_node"]]["inputs"] if i["name"] == "mode")["label"], "做什么")
            labels = ["做什么"]
            for nid, widget in data["inputs"][1:]:
                n = nodes[nid]
                k = owner_index(fam, nid)
                self.assertIsNotNone(k, f"{name}: 右栏登记了不属于任何功能的节点 {nid}")
                names = Workflows.widget_names(n["type"])
                self.assertTrue(names is not None and widget in names, f"{name}: 节点 {nid}（{n['type']}）没有控件 {widget}")
                inp = next(i for i in n["inputs"] if i["name"] == widget)
                self.assertIsNone(inp.get("link"), f"{name}: {nid}.{widget} 连了线，不能放进右栏")
                self.assertTrue(inp["label"].startswith(f"【{fam['funcs'][k]['name']}】"), f"{name}: 右栏控件 {nid}.{widget} 的名字「{inp['label']}」前面要标功能名")
                labels.append(inp["label"])
            self.assertEqual(len(labels), len(set(labels)), f"{name}: 右栏有重复的名字：{sorted(l for l in labels if labels.count(l) > 1)}")
            outs = data["outputs"]
            self.assertEqual(len(outs), len(set(outs)))
            self.assertTrue(all(nodes[o]["type"] in GEN.OUTPUT_NODE_TYPES for o in outs), name)
            self.assertEqual({owner_index(fam, o) for o in outs}, set(range(len(fam["funcs"]))), f"{name}: 每个功能都要有结果显示在应用里")


if __name__ == "__main__":
    unittest.main()
