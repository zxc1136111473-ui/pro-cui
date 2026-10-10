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


def object_info_entry(klass):
    """节点类 → /object_info 里的一项，格式和 tests/object_info_subset.json 一致（自带的 pro-* 新节点没法从服务器抓，就按它们的 INPUT_TYPES 生成，
    test_workflows 会检查文件里这几项没和节点定义走样）。"""
    import json
    spec = klass.INPUT_TYPES()
    outs = list(klass.RETURN_TYPES)
    entry = {"has_intermediate_output": False, "input": spec, "input_order": {k: list(v) for k, v in spec.items() if not k.startswith("_")},
             "is_input_list": False, "name": klass.__name__, "output": outs, "output_is_list": [False] * len(outs),
             "output_name": list(getattr(klass, "RETURN_NAMES", None) or outs), "output_node": bool(getattr(klass, "OUTPUT_NODE", False))}
    return json.loads(json.dumps(entry))


# tests/object_info_subset.json 里不是从服务器抓、而是按节点定义生成的那几个 pro-* 节点（服务器上还没有它们）：{节点名: (包名, 类名)}
NEW_NODES = {"ProAliDialogue": ("pro-ali", "ProAliDialogue"), "ProMatte": ("pro-image", "ProMatte"), "ProSaveCutout": ("pro-image", "ProSaveCutout"),
             "ProLayerCompose": ("pro-image", "ProLayerCompose"), "ProModeDialogueVideo": ("pro-flow", "ProModeDialogueVideo"), "ProModeLayer": ("pro-flow", "ProModeLayer")}


def new_node_classes():
    return {name: load(pkg).NODE_CLASS_MAPPINGS[cls] for name, (pkg, cls) in NEW_NODES.items()}
