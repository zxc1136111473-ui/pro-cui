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
WRITER_NUMS = {"01", "02", "03", "04", "06", "07", "08", "09", "10"}
MODE_NUMS = {"01", "02", "03", "05", "07", "08", "09", "10", "11"}

# ComfyUI 核心 / 插件里工作流用到的节点；自带节点从源码里的 NODE_CLASS_MAPPINGS 读（pro-flow 是函数生成的，直接 import）
KNOWN_EXTERNAL = {
    "RelayAPISettings", "RelayImageGenerator", "RelayVideoGenerator", "RelaySoundGenerator", "RelayTextGenerator",
    "LoadImage", "LoadAudio", "LoadVideo", "SaveImage", "SaveAudioAdvanced", "SaveVideo", "PreviewImage", "PreviewAny",
    "ImageScale", "ImageScaleBy", "ImageScaleToMaxDimension", "ImageSharpen", "ImageBatch", "Note", "MarkdownNote",
    "PrimitiveNode", "PrimitiveStringMultiline", "StringConcatenate",
}
OUTPUT_TYPES = {"SaveImage", "PreviewImage", "PreviewAny", "SaveVideo", "SaveAudioAdvanced", "SaveAudio", "ProVideoDub"}


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
        self.assertEqual(nums, [f"{i:02d}" for i in range(1, 12)], "应该是 01~11 连续编号")

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

    def test_no_overlapping_nodes(self):
        for p in all_workflows():
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            # 加载音频 / 视频的标题会撑宽节点（实测 58 字的标题把宽度撑到 771px，约 13.5px/字 + 60px），长标题会压到右边的节点
            title_wide = lambda n: 13.5 * len(n.get("title", "")) + 60 if n["type"] in ("LoadAudio", "LoadVideo") else 0
            rect = lambda n: (n["pos"][0], n["pos"][1], n["pos"][0] + max(n["size"][0], title_wide(n)),
                              n["pos"][1] + max(n["size"][1], self.MIN_HEIGHT.get(n["type"], 0)) + 30)
            ns = wf["nodes"]
            for i, a in enumerate(ns):
                for b in ns[i + 1:]:
                    ra, rb = rect(a), rect(b)
                    overlap = ra[0] < rb[2] - 4 and rb[0] < ra[2] - 4 and ra[1] < rb[3] - 4 and rb[1] < ra[3] - 4
                    self.assertFalse(overlap, f"{os.path.basename(p)}: 节点 {a['id']}（{a['type']}）和 {b['id']}（{b['type']}）重叠")

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
        """没有模式的工作流（04 06）所有节点都该参与；否则说明有节点没接到输出。"""
        for num in ("04", "06"):
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
