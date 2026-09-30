"""海报提示词节点：把标题 / 副标题 / 角标 / 风格 / 画面元素拼成一段给生图模型的提示词。

不调用任何接口，只做字符串拼接；输出接到 Relay Image Generator 的 prompt 输入即可。
文字渲染要点（实测 gemini-image 的中文字很准，但要明确「只出现这些字」，否则会自己多加字）：
用「」括起要出现的文字，并要求不出现其它文字、水印、乱码。
"""

STYLES = {
    "清爽夏日（蓝白）": "清爽夏日风，蓝白配色，柔和的天空与水的质感，明亮通透",
    "喜庆红金": "喜庆促销风，红金配色，节日氛围，光泽感强",
    "高级黑金": "高级质感，黑金配色，深色背景，金属光泽，克制留白",
    "小清新（马卡龙）": "小清新风，马卡龙粉蓝黄配色，柔和阴影，圆润可爱的元素",
    "科技蓝紫": "科技感，蓝紫渐变背景，发光线条与光效，现代简洁",
    "极简白底": "极简风，纯净白色背景，大面积留白，细腻柔和的阴影，商品杂志质感",
    "自然绿意": "自然清新风，绿色植物与木质元素，柔和自然光，健康有机的感觉",
    "自定义（只用下面的画面元素）": "",
}


class ProPosterPrompt:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "title": ("STRING", {"default": "夏日清凉节"}),
                "subtitle": ("STRING", {"default": "全场满199减50"}),
                "badge": ("STRING", {"default": "限时三天"}),
                "style": (list(STYLES.keys()), {"default": "清爽夏日（蓝白）"}),
                "elements": ("STRING", {"multiline": True, "default": "冰饮、柠檬片、水花、椰树叶、遮阳草帽"}),
                "use_product_image": ("BOOLEAN", {"default": False, "label_on": "用图1的商品做主体", "label_off": "不带商品图"}),
            },
            "optional": {"extra": ("STRING", {"multiline": True, "default": ""})},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("prompt",)
    FUNCTION = "build"
    CATEGORY = "pro/poster"

    def build(self, title, subtitle, badge, style, elements, use_product_image, extra=""):
        # 带商品图时必须明确「创作新海报、不要返回原图」：实测含糊的说法会让模型原样返回商品照片。
        parts = ["请以图1的商品照片为素材，创作一张全新的电商促销海报（不要直接返回原图），竖版构图。"
                 if use_product_image else "电商促销海报，竖版构图。"]
        texts = []
        if title.strip():
            texts.append(f"大标题「{title.strip()}」")
        if subtitle.strip():
            texts.append(f"副标题「{subtitle.strip()}」")
        if badge.strip():
            texts.append(f"角标小字「{badge.strip()}」")
        if texts:
            parts.append("海报上的文字：" + "，".join(texts) + "。")
        if STYLES.get(style):
            parts.append("风格与配色：" + STYLES[style] + "。")
        if elements.strip():
            parts.append("画面元素：" + elements.strip() + "。")
        if use_product_image:
            parts.append("把图1里的商品作为海报主体，放在画面视觉中心，并把原照片的背景替换成海报背景；"
                         "商品的形状、颜色、包装上的文字都保持原样不要改动。")
        if extra.strip():
            e = extra.strip()
            parts.append(e if e[-1] in "。.！!？?" else e + "。")
        if texts:
            parts.append("要求：海报上只出现上面给出的文字，文字必须清晰、正确、无错别字，"
                         "不要出现其它多余文字、水印或乱码；主次分明、版面留白合理，专业电商海报质感。")
        else:
            parts.append("要求：画面里不要出现任何文字、水印或乱码；主次分明、版面留白合理，专业电商海报质感。")
        return ("".join(parts),)


NODE_CLASS_MAPPINGS = {"ProPosterPrompt": ProPosterPrompt}
NODE_DISPLAY_NAME_MAPPINGS = {"ProPosterPrompt": "海报提示词（标题/副标题/角标/风格）"}
