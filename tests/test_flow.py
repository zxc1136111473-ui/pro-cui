"""pro-flow：模式 / 选择分支 / 开关门。这几个节点只用标准库，直接 import，不用 stub。"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_spec = importlib.util.spec_from_file_location("t_pro_flow", os.path.join(ROOT, "custom-nodes", "pro-flow", "__init__.py"))
flow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flow)
CLS = flow.NODE_CLASS_MAPPINGS


def lazy_run(node, **given):
    """按 ComfyUI 执行器的真实行为跑一个带 check_lazy_status 的节点（见 execution.py）：
    还没算的惰性输入以 None 传进来；节点要求的名字里，已经算过的会被滤掉；要求了就去算，算完再问，直到不缺。
    given 里以 in 开头的和 value 是惰性输入的「真实值」，只有被要求过才交给节点。"""
    lazy = {k: v for k, v in given.items() if k.startswith("in") or k == "value"}
    kw = {k: v for k, v in given.items() if k not in lazy}
    kw.update({k: None for k in lazy})                # 没算的惰性输入 = None
    done, asked = set(), []
    for _ in range(5):
        need = [x for x in node.check_lazy_status(**kw) if x not in done]
        if not need:
            break
        for x in need:
            assert x in lazy, f"要求了没连线的输入 {x}（ComfyUI 会报 NodeInputError）"
            kw[x] = lazy[x]
            done.add(x)
            asked.append(x)
    return asked, kw


class Pick(unittest.TestCase):
    def test_only_selected_branch_is_requested(self):
        node = CLS["ProPick3x1"]()
        asked, kw = lazy_run(node, branch=1, in0_0="A", in1_0="B", in2_0="C")
        self.assertEqual(asked, ["in1_0"])
        self.assertEqual(node.pick(**kw), ("B",))

    def test_two_channels(self):
        node = CLS["ProPick2x2"]()
        asked, kw = lazy_run(node, branch=0, in0_0="img", in0_1="status", in1_0="img2", in1_1="status2")
        self.assertEqual(asked, ["in0_0", "in0_1"])
        self.assertEqual(node.pick(**kw), ("img", "status"))

    def test_evaluated_none_does_not_loop(self):
        node = CLS["ProPick2x1"]()
        asked, kw = lazy_run(node, branch=0, in0_0=None, in1_0="B")   # 选中那一路算出来就是 None（比如开关门关着）
        self.assertEqual(asked, ["in0_0"])
        self.assertEqual(node.pick(**kw), (None,))

    def test_branch_range_matches_branch_count(self):
        self.assertEqual(CLS["ProPick3x2"].INPUT_TYPES()["required"]["branch"][1]["max"], 2)
        self.assertEqual(len(CLS["ProPick3x2"].INPUT_TYPES()["optional"]), 6)
        self.assertEqual(CLS["ProPick3x2"].RETURN_TYPES, ("*", "*"))


class Gate(unittest.TestCase):
    def test_enabled_requests_and_passes_value(self):
        node = CLS["ProGate"]()
        asked, kw = lazy_run(node, enabled=True, value="img")
        self.assertEqual(asked, ["value"])
        self.assertEqual(node.gate(**kw), ("img",))

    def test_unselected_input_is_never_asked_for(self):
        # lazy_run 对「没连线却被要求」的输入会直接断言失败；这里 value 没给，开关关着就不能要求
        asked, _ = lazy_run(CLS["ProGate"](), enabled=False)
        self.assertEqual(asked, [])

    def test_disabled_never_requests_value(self):
        node = CLS["ProGate"]()
        asked, kw = lazy_run(node, enabled=False, value="img")
        self.assertEqual(asked, [])
        self.assertEqual(node.gate(**kw), (None,))


class Block(unittest.TestCase):
    """ProBlock（功能开关）：功能被选中才放行；没选中输出 ExecutionBlocker(None)（下游静默跳过），而且不去要上游的值（lazy）。"""

    def setUp(self):
        self.blockers = types.ModuleType("comfy_execution.graph_utils")

        class ExecutionBlocker:
            def __init__(self, message):
                self.message = message
        self.blockers.ExecutionBlocker = ExecutionBlocker
        self._saved = {k: sys.modules.get(k) for k in ("comfy_execution", "comfy_execution.graph_utils")}
        sys.modules["comfy_execution"] = types.ModuleType("comfy_execution")
        sys.modules["comfy_execution.graph_utils"] = self.blockers

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_selected_function_requests_and_passes_the_value(self):
        node = CLS["ProBlock"]()
        asked, kw = lazy_run(node, enabled=True, value="img")
        self.assertEqual(asked, ["value"])
        self.assertEqual(node.block(**kw), ("img",))

    def test_unselected_function_is_blocked_silently_and_upstream_is_never_asked_for(self):
        node = CLS["ProBlock"]()
        asked, kw = lazy_run(node, enabled=False, value="img")
        self.assertEqual(asked, [], "没选中的功能不能去要上游的值，否则上游（可能是要花钱的接口）会被运行")
        (out,) = node.block(**kw)
        self.assertIsInstance(out, self.blockers.ExecutionBlocker)
        self.assertIsNone(out.message, "message 必须是 None：有 message 的拦截 ComfyUI 会弹出错误")

    def test_a_selected_function_passes_none_through_like_an_open_gate(self):
        node = CLS["ProBlock"]()
        asked, kw = lazy_run(node, enabled=True, value=None)       # 上游算出来就是 None（比如里面的开关门关着）：照样放行，不死循环
        self.assertEqual(asked, ["value"])
        self.assertEqual(node.block(**kw), (None,))

    def test_node_definition(self):
        cls = CLS["ProBlock"]
        self.assertEqual(cls.RETURN_TYPES, ("*",))
        self.assertEqual(list(cls.INPUT_TYPES()["required"]), ["enabled"])
        self.assertTrue(cls.INPUT_TYPES()["optional"]["value"][1]["lazy"])
        self.assertFalse(getattr(cls, "OUTPUT_NODE", False))


class Func(unittest.TestCase):
    """ProFunc*（合并应用顶上的「做什么」）：每个功能一个 BOOLEAN 输出，选中的那个是真，其余是假。"""

    def test_each_function_node_has_one_boolean_per_function_and_exactly_one_is_true(self):
        self.assertEqual(sorted(flow.FUNCS), ["ProFuncEdit", "ProFuncImage", "ProFuncProcess", "ProFuncText", "ProFuncVideo"])
        for name, (title, options) in flow.FUNCS.items():
            cls = CLS[name]
            self.assertEqual(cls.INPUT_TYPES()["required"]["mode"][0], options)
            self.assertEqual(cls.INPUT_TYPES()["required"]["mode"][1]["display_name"], "做什么")
            self.assertEqual(cls.RETURN_NAMES, tuple(f"f{i}" for i in range(len(options))))
            self.assertEqual(set(cls.RETURN_TYPES), {"BOOLEAN"})
            for i, opt in enumerate(options):
                out = cls().go(opt)
                self.assertEqual(out, tuple(j == i for j in range(len(options))), f"{name} 选「{opt}」")
                self.assertEqual(sum(out), 1)

    def test_function_names_are_unique_across_apps(self):
        names = [o for _t, opts in flow.FUNCS.values() for o in opts]
        self.assertEqual(len(names), len(set(names)), "功能名就是目录里的应用名，不能重复")
        self.assertEqual(len(names), 19)

    def test_a_function_node_is_not_confused_with_a_mode_node(self):
        self.assertFalse([n for n in flow.FUNCS if n.startswith("ProMode")], "目录靠 ProMode 开头认模式节点，功能节点不能叫这个名字")
        self.assertFalse(set(flow.FUNCS) & set(flow.MODES))


class Mode(unittest.TestCase):
    def test_every_mode_returns_one_value_per_output(self):
        for name, (title, options, outputs) in flow.MODES.items():
            cls = CLS[name]
            self.assertEqual(cls.INPUT_TYPES()["required"]["mode"][0], options)
            for i, opt in enumerate(options):
                out = cls().go(opt)
                self.assertEqual(len(out), len(outputs), name)
                self.assertEqual(out, tuple(vals[i] for _, _, vals in outputs))

    def test_branch_outputs_are_valid_ints(self):
        for name, (_, options, outputs) in flow.MODES.items():
            for oname, otype, vals in outputs:
                if otype == "INT":
                    self.assertTrue(all(isinstance(v, int) and v >= 0 for v in vals), f"{name}.{oname}")

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            CLS["ProModeImage"]().go("不存在的模式")


class AppText(unittest.TestCase):
    """ProAppText：文字存成 .txt 并按「文件结果」返回，应用界面才显示；空文字不存；只在出错时显示。"""

    def setUp(self):
        self.out = tempfile.mkdtemp()
        fp = types.ModuleType("folder_paths")
        fp.get_output_directory = lambda: self.out

        def get_save_image_path(prefix, out_dir, w=0, h=0):      # 和 ComfyUI 的同名函数一样：前缀里的目录是子文件夹，序号接着已有的往下数
            sub, name = os.path.split(prefix)
            folder = os.path.join(out_dir, sub)
            os.makedirs(folder, exist_ok=True)
            counter = 1 + len([f for f in os.listdir(folder) if f.startswith(name + "_")])
            return folder, name, counter, sub, prefix
        fp.get_save_image_path = get_save_image_path
        self._saved = sys.modules.get("folder_paths")
        sys.modules["folder_paths"] = fp
        self.node = CLS["ProAppText"]()

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("folder_paths", None)
        else:
            sys.modules["folder_paths"] = self._saved
        shutil.rmtree(self.out)

    def read(self, item):
        with open(os.path.join(self.out, item["subfolder"], item["filename"]), encoding="utf-8") as f:
            return f.read()

    def test_node_definition(self):
        cls = CLS["ProAppText"]
        self.assertTrue(cls.OUTPUT_NODE)
        self.assertEqual(cls.RETURN_TYPES, ())
        req = cls.INPUT_TYPES()["required"]
        self.assertTrue(req["text"][1]["forceInput"])
        self.assertEqual(list(req), ["text", "label", "only_on_error"], "控件顺序 = 工作流里 widgets_values 的顺序（名称、显示时机）")

    def test_text_is_saved_and_returned_as_a_file_result(self):
        ui = self.node.show("AI 写的提示词：一只橘猫", "AI 写的提示词", False)["ui"]
        (item,) = ui["files"]            # 应用界面认「文件结果」：filename / subfolder / type，display_name 是显示的标题
        self.assertEqual((item["subfolder"], item["type"], item["display_name"]), ("text", "output", "AI 写的提示词"))
        self.assertTrue(item["filename"].endswith(".txt"))
        self.assertEqual(self.read(item), "AI 写的提示词：一只橘猫")

    def test_counter_goes_up_instead_of_overwriting(self):
        a = self.node.show("第一次", "文案", False)["ui"]["files"][0]["filename"]
        b = self.node.show("第二次", "文案", False)["ui"]["files"][0]["filename"]
        self.assertNotEqual(a, b)
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, "text"))), sorted([a, b]))

    def test_empty_text_is_not_saved_or_shown(self):
        for t in (None, "", "   \n"):
            self.assertEqual(self.node.show(t, "音色描述", False), {"ui": {}})
        self.assertFalse(os.path.exists(os.path.join(self.out, "text")), "什么都没存就不该建目录")

    def test_only_on_error_hides_success_and_shows_the_reason_on_failure(self):
        ok = json.dumps({"code": "success", "type": "base64"})
        self.assertEqual(self.node.show(ok, "状态", True), {"ui": {}})
        err = json.dumps({"code": "error", "message": "HTTPConnectionPool: Max retries exceeded"}, ensure_ascii=False)
        item = self.node.show(err, "状态：白底主图", True)["ui"]["files"][0]
        self.assertEqual(self.read(item), "出错了：HTTPConnectionPool: Max retries exceeded")
        self.assertEqual(item["display_name"], "状态：白底主图")
        # 不是 JSON 的（意料之外的格式）一律显示，宁可多显示也别吞掉错误
        self.assertEqual(self.read(self.node.show("Suno create error: 503", "状态", True)["ui"]["files"][0]), "Suno create error: 503")
        # 「总是显示」时成功的 JSON 也照样显示
        self.assertEqual(len(self.node.show(ok, "状态", False)["ui"]["files"]), 1)

    def test_label_cannot_escape_the_text_folder(self):
        item = self.node.show("x", "../../etc/a:b\\c", False)["ui"]["files"][0]
        self.assertEqual(item["subfolder"], "text")
        self.assertNotIn("/", item["filename"])
        self.assertTrue(os.path.exists(os.path.join(self.out, "text", item["filename"])))
        self.assertEqual(os.listdir(self.out), ["text"])


if __name__ == "__main__":
    unittest.main()
