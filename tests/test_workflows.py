"""工作流校验：模板与生成脚本一致、连线完整、节点类型存在。"""
import glob
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

# ComfyUI 核心 / 自带插件里工作流用到的节点；自带节点从源码里的 NODE_CLASS_MAPPINGS 读
# 带「★ 一句话需求 → AI 扩写」的工作流编号（会出图 / 出视频 / 出音乐 / 设计音色、且有提示词可写的）
AI_NUMS = {"01", "02", "03", "04", "05", "07", "10", "11", "12", "13", "14", "15", "17", "18", "20", "25"}

KNOWN_EXTERNAL = {
    "RelayAPISettings", "RelayImageGenerator", "RelayVideoGenerator", "RelaySoundGenerator", "RelayTextGenerator",
    "LoadImage", "LoadAudio", "LoadVideo", "SaveImage", "SaveAudioAdvanced", "SaveVideo", "PreviewImage", "PreviewAny",
    "ImageScale", "ImageScaleBy", "ImageScaleToMaxDimension", "ImageSharpen", "ImageBatch", "Note", "MarkdownNote",
    "PrimitiveNode", "PrimitiveStringMultiline", "StringConcatenate",
}


def own_node_names():
    names = set()
    for p in glob.glob(os.path.join(ROOT, "custom-nodes", "pro-*", "__init__.py")):
        with open(p, encoding="utf-8") as f:
            src = f.read()
        m = re.search(r"NODE_CLASS_MAPPINGS\s*=\s*\{(.*?)\}", src, re.S)
        names |= set(re.findall(r'"(\w+)"\s*:', m.group(1))) if m else set()
    return names


def all_workflows():
    return sorted(glob.glob(os.path.join(WF, "*", "*.json")))


class Workflows(unittest.TestCase):
    def test_generator_output_matches_committed(self):
        """改了 gen_workflows.py 没重新生成、或手改了 JSON，这里会报。"""
        tmp = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(ROOT, "tools"), os.path.join(tmp, "tools"))
            r = subprocess.run([sys.executable, os.path.join(tmp, "tools", "gen_workflows.py")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            def rd(p):
                with open(p, "rb") as f:
                    return f.read()
            gen = {os.path.relpath(p, os.path.join(tmp, "workflows")): rd(p)
                   for p in glob.glob(os.path.join(tmp, "workflows", "*", "*.json"))}
            cur = {os.path.relpath(p, WF): rd(p) for p in all_workflows()}
            self.assertEqual(sorted(gen), sorted(cur), "生成的文件列表和已提交的不一致")
            for k in cur:
                self.assertEqual(gen[k], cur[k], f"{k} 和生成脚本输出不一致，请重新运行 tools/gen_workflows.py")
        finally:
            shutil.rmtree(tmp)

    def test_numbers_unique(self):
        nums = [os.path.basename(p).split("-")[0] for p in all_workflows()]
        self.assertEqual(len(nums), len(set(nums)), "工作流编号重复")

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

    def test_ai_prompt_block(self):
        """AI 扩写块：有且只有 AI_NUMS 里的工作流带；在画布最上面；扩写结果连到了下游；不压住别的节点。"""
        for p in all_workflows():
            name = os.path.relpath(p, WF)
            num = os.path.basename(p).split("-")[0]
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            idea = [n for n in wf["nodes"] if n.get("title", "").startswith("★")]
            if num not in AI_NUMS:
                self.assertEqual(idea, [], f"{name}: 不该带 AI 扩写")
                continue
            self.assertEqual(len(idea), 1, f"{name}: 应有 1 个「★ 一句话需求」")
            self.assertEqual(idea[0]["pos"][1], min(n["pos"][1] for n in wf["nodes"]), f"{name}: 「★ 一句话需求」应在最上面")
            self.assertEqual(idea[0]["type"], "ProAliPromptWriter")
            self.assertEqual(idea[0]["id"], 70)
            self.assertGreaterEqual(len(idea[0]["outputs"][0]["links"] or []), 2, f"{name}: 扩写结果应连到预览和下游")
            info = next(i for i in idea[0]["inputs"] if i["name"] == "info")
            self.assertIsNotNone(info["link"], f"{name}: 写提示词节点没接 Key（Settings 节点 id 31）")

    # 界面会把「生图」节点（16 个图片输入口）自动撑高到约 576，再加标题栏约 30；按这个算才和实际看到的一致
    MIN_HEIGHT = {"RelayImageGenerator": 576}

    def test_no_overlapping_nodes(self):
        """带 AI 扩写的工作流里，节点按界面里的实际尺寸不能互相压住。"""
        for p in all_workflows():
            num = os.path.basename(p).split("-")[0]
            if num not in AI_NUMS:
                continue
            with open(p, encoding="utf-8") as f:
                wf = json.load(f)
            rect = lambda n: (n["pos"][0], n["pos"][1], n["pos"][0] + n["size"][0],
                              n["pos"][1] + max(n["size"][1], self.MIN_HEIGHT.get(n["type"], 0)) + 30)
            ns = wf["nodes"]
            for i, a in enumerate(ns):
                for b in ns[i + 1:]:
                    ra, rb = rect(a), rect(b)
                    overlap = ra[0] < rb[2] - 4 and rb[0] < ra[2] - 4 and ra[1] < rb[3] - 4 and rb[1] < ra[3] - 4
                    self.assertFalse(overlap, f"{os.path.basename(p)}: 节点 {a['id']} 和 {b['id']} 重叠")

    def test_no_api_keys_in_templates(self):
        for p in all_workflows():
            with open(p, encoding="utf-8") as f:
                txt = f.read()
            self.assertIsNone(re.search(r"sk-[A-Za-z0-9]{16,}", txt), f"{p} 疑似含 API Key")


if __name__ == "__main__":
    unittest.main()
