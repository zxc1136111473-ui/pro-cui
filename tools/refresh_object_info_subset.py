#!/usr/bin/env python3
# 把 pro-* 新节点（服务器上还没有、没法从 /object_info 抓的）按它们现在的 INPUT_TYPES 写进 tests/object_info_subset.json。
# 改了这些节点的控件（增删 / 改选项 / 改默认值）以后运行一次：python3 tools/refresh_object_info_subset.py
# 这几个节点部署到服务器之后，也可以改成从真实的 /object_info 抓（格式一样）。名单在 tests/_load.py 的 NEW_NODES。
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import _load  # noqa: E402

path = os.path.join(ROOT, "tests", "object_info_subset.json")
with open(path, encoding="utf-8") as f:
    oi = json.load(f)
for name, klass in _load.new_node_classes().items():
    oi[name] = _load.object_info_entry(klass)
with open(path, "w", encoding="utf-8") as f:
    f.write(json.dumps(oi, sort_keys=True, ensure_ascii=False, indent=1))
print(f"已更新 {len(_load.NEW_NODES)} 个节点的条目：{', '.join(_load.NEW_NODES)}")
