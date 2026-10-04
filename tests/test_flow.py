"""pro-flow：模式 / 选择分支 / 开关门。这几个节点只用标准库，直接 import，不用 stub。"""
import importlib.util
import os
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


if __name__ == "__main__":
    unittest.main()
