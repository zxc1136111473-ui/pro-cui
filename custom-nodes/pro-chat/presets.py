"""一键方案：几个常用的多步组合，工作台里点一下就排好（不用等 AI）。也写进助手的系统提示词：用户说到类似的需求时，它会按同样的组合排。

写法和模型输出的方案一样，由 chat.make_plan 校验（应用 / 模式 / 字段 / 引用有一样不对就丢掉并提示）：workflow、mode（模式编号，从 1 数）、fields、files、note。
files 的值：素材库编号、"步骤N" / "步骤N.M"（第 N 步产出的第 M 个文件），或 "image" = 素材库里最近的一张能用的图片（没有就不填，卡片上会提示还缺图片）。
不依赖 ComfyUI，tests/test_chat.py 直接测（每个一键方案都要能在真实的应用目录上排出来、必填的文件都有着落）。
"""

PRESETS = [
    {"id": "product_full", "name": "商品全套（主图 + 场景图 + 文案 → 视频）", "image": True,
     "desc": "上传商品图：一次出白底主图、场景图、标题 / 卖点文案，再用场景图做一条视频（视频占 Veo 额度，每天约 3 个）",
     "steps": [
         {"workflow": "06", "files": {"3:image": "image"}, "note": "商品图 → 白底主图 + 场景图 + 标题 / 卖点文案"},
         {"workflow": "12", "mode": 1, "files": {"2:image": "步骤1.2"}, "note": "用第 1 步的场景图做视频（只出视频；要配音和字幕，把这一步的模式换成「出视频，再配音 + 烧字幕」）"}]},
    {"id": "cutout_bg", "name": "抠图换背景（商品原样不变）", "image": True,
     "desc": "先把商品抠成透明底，再放到纯色背景上：商品的形状、颜色、包装上的字一个像素都不变（想用自己的背景图，把第 2 步的模式换成「放到背景图上」）",
     "steps": [
         {"workflow": "15", "files": {"2:image": "image"}, "note": "把商品抠出来（透明底）"},
         {"workflow": "16", "mode": 2, "files": {"2:image": "步骤1"}, "note": "放到纯色背景上"}]},
    {"id": "image_to_video", "name": "文生图 → 视频", "image": False,
     "desc": "先按一句话出一张图，再用这张图做一条视频（视频占 Veo 额度，每天约 3 个）",
     "steps": [
         {"workflow": "01", "note": "按一句话出一张图"},
         {"workflow": "12", "mode": 1, "files": {"2:image": "步骤1"}, "note": "用第 1 步的图做视频"}]},
    {"id": "promo_set", "name": "促销全套（海报 + 文案 + 配音）", "image": False,
     "desc": "一张促销海报、一段淘宝标题和卖点、一段配音（三步互相独立，各改各的设置）",
     "steps": [
         {"workflow": "02", "mode": 1, "note": "做促销海报"},
         {"workflow": "07", "mode": 1, "note": "写标题和卖点"},
         {"workflow": "08", "mode": 1, "note": "把文案配成语音（要念的文字在这一步里填）"}]},
]


def find(preset_id):
    return next((p for p in PRESETS if p["id"] == preset_id), None) if isinstance(preset_id, str) else None


def usable(cat, preset):
    """这个一键方案用到的应用目录里都有（服务器上删了某个工作流，对应的一键方案就不显示）。"""
    return all(st["workflow"] in cat for st in preset["steps"])


def listing(cat):
    """给界面下拉用的清单：[{id, name, desc, image}]（image = 需要先有一张商品图）。"""
    return [{"id": p["id"], "name": p["name"], "desc": p["desc"], "image": p["image"]} for p in PRESETS if usable(cat, p)]


def raw_plan(preset, assets):
    """展开成 chat.make_plan 认的方案：files 里的 "image" 换成素材库里最近的一张能用的图片的编号（assets 是 chat.normalize_assets 的结果，usable 已设好）。"""
    last = next((i for i in range(len(assets), 0, -1) if assets[i - 1].get("usable") and assets[i - 1].get("kind") == "image"), None)
    steps = []
    for st in preset["steps"]:
        files = {}
        for key, ref in (st.get("files") or {}).items():
            if ref == "image":
                if last is not None:
                    files[key] = last
            else:
                files[key] = ref
        steps.append({**st, "files": files})
    return {"steps": steps, "note": preset["name"]}


def combos_text(cat):
    """给模型看的「常用组合」：用户要的和这些差不多时，直接按它排。"""
    lines = []
    for p in PRESETS:
        if not usable(cat, p):
            continue
        parts = []
        for st in p["steps"]:
            files = "".join(f"，{k} 填{'素材库里的商品图编号' if v == 'image' else v}" for k, v in (st.get("files") or {}).items())
            parts.append(f"{st['workflow']}「{cat[st['workflow']]['name']}」" + (f"（模式 {st['mode']}{files}）" if st.get("mode") else f"（{files[1:]}）" if files else ""))
        lines.append(f"- {p['name']}：" + " → ".join(parts) + f"。{p['desc']}")
    return "\n".join(lines)
