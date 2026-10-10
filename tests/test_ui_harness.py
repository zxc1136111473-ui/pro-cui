"""工作台在真浏览器里的整套检查（tests/ui_harness/check_ui.py：假 ComfyUI + 真前端 + 真后端 + Playwright 驱动系统 Chrome）。
要 playwright 和 Chrome，跑一遍约 3 分钟（两个标签页接手、断网重试这些场景要等运行和 7 秒的接手计时），所以默认跳过；想跑：PC_UI_TESTS=1 python3 -m unittest discover -s tests -p "test_ui_harness.py"。
没有 playwright / Chrome 时脚本自己会说「跳过」并正常退出（这里也算通过）。"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@unittest.skipUnless(os.environ.get("PC_UI_TESTS"), "设置 PC_UI_TESTS=1 才跑（要 playwright + Chrome，约 3 分钟）")
class UiHarness(unittest.TestCase):
    def test_the_whole_workbench_in_a_real_browser(self):
        shots = tempfile.mkdtemp(prefix="pc_ui_shots_")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "ui_harness", "check_ui.py"), shots], capture_output=True, text=True, timeout=1500)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out[-3000:])
        first = (out.splitlines() or [""])[0]
        if first.startswith("跳过"):
            self.skipTest(first)
        self.assertIn("失败 0 项", out)


if __name__ == "__main__":
    unittest.main()
