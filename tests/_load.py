"""按路径加载自带节点模块；本机没有 ComfyUI / torch 时，用 MagicMock 顶替这些重依赖（只测纯函数）。"""
import importlib.util
import os
import sys
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_STUBS = ["torch", "torchaudio", "torchaudio.functional", "av", "folder_paths", "comfy_api", "comfy_api.latest",
          "comfy_api_nodes", "comfy_api_nodes.util"]


def load(pkg):
    for name in _STUBS:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = mock.MagicMock()
    path = os.path.join(ROOT, "custom-nodes", pkg, "__init__.py")
    spec = importlib.util.spec_from_file_location("t_" + pkg.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod        # 和 ComfyUI 的加载器一样先登记：包里用相对导入（pro-chat）才找得到同级模块
    spec.loader.exec_module(mod)
    return mod
