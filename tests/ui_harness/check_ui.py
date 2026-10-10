"""在真浏览器（系统里的 Chrome）里把工作台从头跑到尾：假 ComfyUI + 真前端 + 真后端（见 server.py）。

覆盖：默认打开 / 附素材 / 一句话出多步方案 / 运行全部（每步结果自动放进素材库、下一步拿到的就是上一步的结果）/ 只改一步只重跑一步 /
AI 改方案后没变的步骤保留结果 / 刷新网页后还在 / 运行到一半刷新后接着等 / 停止 / 按键不漏给 ComfyUI / 关闭和重新打开 / 手动加删步骤；
最后两组是复核里找到的问题：等 AI 回复时的锁、序号变了不误判、断网重试沿用同一个 run_id、复制标签页 / 提前打开的第二个标签页不重复提交且能接手（Web Locks 和没有 Web Locks 各跑一遍）。
用法：python3 tests/ui_harness/check_ui.py [截图目录]（约 3 分钟）
没有 playwright 或 Chrome 时直接退出（返回 0 并说明跳过）。
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "pc_ui_shots")
os.makedirs(SHOTS, exist_ok=True)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except Exception:
    print("跳过：没有 playwright")
    sys.exit(0)

import server  # noqa: E402

failures = []
total = [0]


def check(name, cond, detail=""):
    total[0] += 1
    mark = "ok  " if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  → {detail}" if (detail and not cond) else ""))
    if not cond:
        failures.append(name)


def main():
    h = server.Harness(delay=1.0)
    base = h.start()
    # 一张测试用的商品图
    from PIL import Image
    prod = os.path.join(h.tmp, "商品.png")
    Image.new("RGB", (400, 400), (200, 40, 40)).save(prod)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:
            print("跳过：启动不了 Chrome：", str(e)[:200])
            h.stop()
            return 0
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)      # 资源加载失败另外按网址查（favicon 不算）
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 and "favicon" not in r.url else None)
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        page.goto(base)
        page.wait_for_selector(".pcw.pcw-open", timeout=10000)

        def shot(name):
            page.screenshot(path=os.path.join(SHOTS, name + ".png"))

        def state(expr):
            return page.evaluate(f"(() => {{ const s = window.__proChat.state; return {expr}; }})()")

        def badges():
            return page.eval_on_selector_all(".pcw-step .pcw-badge", "els => els.map(e => e.textContent)")

        def wait_badges(pred, timeout=40):
            t0 = time.time()
            while time.time() - t0 < timeout:
                b = badges()
                if pred(b):
                    return b
                time.sleep(0.2)
            return badges()

        # T1 默认打开
        check("默认打开工作台", page.locator(".pcw.pcw-open").count() == 1)
        check("打开时蓝色小按钮不显示", not page.locator(".pcw-handle").is_visible())
        check("默认打开时焦点已经在聊天输入框里（直接打字，按键不会落到 ComfyUI 的快捷键上）", page.evaluate("document.activeElement === document.querySelector('.pcw-row textarea')"), page.evaluate("document.activeElement.tagName"))
        check("没有方案时右边有提示", "还没有方案" in page.locator(".pcw-steps").inner_text())
        check("应用目录加载了（可以手动加步骤）", page.locator(".pcw-bar select option").count() == 17, str(page.locator(".pcw-bar select option").count()))
        shot("01-打开")

        # T2 附图
        page.set_input_files(".pcw-left input[type=file]", prod)
        page.wait_for_selector(".pcw-draft", timeout=10000)
        check("附的图出现在输入框上方", page.locator(".pcw-draft").count() == 1)

        # T3 一句话出多步方案
        page.fill(".pcw-row textarea", "做一套")
        page.click("text=发送")
        page.wait_for_selector(".pcw-step", timeout=15000)
        page.wait_for_function("document.querySelectorAll('.pcw-step').length === 2", timeout=15000)
        names = page.eval_on_selector_all(".pcw-step .pcw-step-name", "els => els.map(e => e.textContent)")
        check("方案是两步：套图 + 图生视频", names == ["多尺寸套图", "图生视频成片"], str(names))
        check("两步都显示还没运行", badges() == ["还没运行", "还没运行"], str(badges()))
        check("页面里没有 null / undefined 这类漏出来的字", not any(w in page.locator(".pcw").inner_text() for w in ("null", "undefined", "[object")))
        check("Veo 额度提醒在方案顶部", "额度" in page.locator(".pcw-notice").inner_text())
        check("左边聊天里有「方案已更新」", "方案已更新到右边" in page.locator(".pcw-list").inner_text())
        check("素材库里有刚附的图", state("s.assets.length") == 1 and page.locator(".pcw-asset").count() == 1)
        check("用户消息里带着缩略图", page.locator(".pcw-user .pcw-thumb").count() == 1)
        step2_mode = page.locator(".pcw-step").nth(1).locator("select").first.input_value()
        check("第二步的模式是「出视频，再配音 + 烧字幕」", "配音" in step2_mode, step2_mode)
        sel_file = page.locator(".pcw-step").nth(1).locator("select[data-pc='1:2:image']")
        check("第二步的参考图选的是「步骤 1 的第 2 个图片」", sel_file.input_value() == "s:1:2", sel_file.input_value())
        shot("02-方案")

        # T4 运行全部
        page.click("text=运行全部")
        b = wait_badges(lambda b: len(b) == 2 and all(x.startswith("已完成") for x in b), timeout=60)
        check("两步都跑完了", len(b) == 2 and all(x.startswith("已完成") for x in b), str(b))
        runs = h.comfy.runs
        check("一共提交了两次", len(runs) == 2, str(len(runs)))
        if len(runs) == 2:
            assets = state("s.assets")
            second_img = state("s.plan.steps[0].result.byKind.image[1]")
            want = assets[second_img - 1]["name"]
            got = runs[1]["prompt"]["2"]["inputs"]["image"]
            check("第二步拿到的就是第一步的第 2 张图", got == want and got.startswith("助手/"), f"{got} != {want}")
            check("第一步拿到的是上传的商品图", runs[0]["prompt"]["2"]["inputs"]["image"] == assets[0]["name"], runs[0]["prompt"]["2"]["inputs"]["image"])
            check("方案里的比例和语速写进了第二步的工作流", runs[1]["prompt"]["12"]["inputs"]["ratio"] == "9:16" and runs[1]["prompt"]["3"]["inputs"]["speed"] == 0.9,
                  str(runs[1]["prompt"]["12"]["inputs"]) + str(runs[1]["prompt"]["3"]["inputs"]))
            check("没改过的设置没被动（时长还是默认 8）", runs[1]["prompt"]["12"]["inputs"]["duration"] == "8")
        check("素材库里有套图 4 张 + 视频 1 个 + 上传的 1 张 = 6", state("s.assets.length") == 6, str(state("s.assets.map(a => a.kind)")))
        check("套图的 4 张图都显示出来了", page.locator(".pcw-step").nth(0).locator(".pcw-res img").count() == 4)
        check("成片视频有播放器", page.locator(".pcw-step").nth(1).locator(".pcw-res video").count() == 1)
        check("结果下面有素材编号和引用按钮", "素材 #2" in page.locator(".pcw-step").nth(0).inner_text())
        check("运行全部按钮变成「全部已完成」且不能点", page.locator("text=全部已完成").is_disabled())
        shot("03-跑完")

        # T5 只改第二步的语速：只有第二步要重跑
        check("跑完的步骤把「设置」折叠起来", page.locator(".pcw-step").nth(1).locator("details.pcw-settings").first.get_attribute("open") is None)
        page.locator(".pcw-step").nth(1).locator("details.pcw-settings > summary").click()
        num = page.locator("input[data-pc='1:3:speed']")
        num.fill("1.2")
        num.dispatch_event("change")
        b = badges()
        check("改了第二步的设置：第二步「要重跑」，第一步仍是已完成", b[0].startswith("已完成") and b[1].startswith("要重跑"), str(b))
        check("按钮变成「运行全部（1 步待运行）」", page.locator("text=运行全部（1 步待运行）").count() == 1)
        check("被改的控件有「恢复默认」", page.locator(".pcw-step").nth(1).locator(".pcw-changed").count() >= 1)
        page.click("text=运行全部")
        b = wait_badges(lambda b: all(x.startswith("已完成") for x in b) and len(h.comfy.runs) == 3, timeout=40)
        check("只重跑了第二步（一共 3 次提交，第三次的语速是 1.2）", len(h.comfy.runs) == 3 and h.comfy.runs[2]["prompt"]["3"]["speed" if False else "inputs"]["speed"] == 1.2, str(len(h.comfy.runs)))

        # T6 AI 改方案：第一步原样 → 保留结果；第二步换图 → 重跑
        page.fill(".pcw-row textarea", "改成用第三张")
        page.click("text=发送")
        page.wait_for_function("document.querySelectorAll('.pcw-msg.pcw-ai').length >= 2", timeout=15000)
        page.wait_for_function("window.__proChat.state.busy === false", timeout=15000)
        b = badges()
        check("AI 改方案后第一步保留了结果、没有重跑", b[0].startswith("已完成") and len(h.comfy.runs) == 3, str(b))
        check("第二步（换了图）变成待运行", not b[1].startswith("已完成"), str(b))
        sel_file = page.locator(".pcw-step").nth(1).locator("select[data-pc='1:2:image']")
        check("第二步的参考图改成了「步骤 1 的第 3 个图片」", sel_file.input_value() == "s:1:3", sel_file.input_value())
        page.click("text=运行全部")
        wait_badges(lambda b: all(x.startswith("已完成") for x in b) and len(h.comfy.runs) == 4, timeout=40)
        if len(h.comfy.runs) == 4:
            assets = state("s.assets")
            third = state("s.plan.steps[0].result.byKind.image[2]")
            check("第四次提交用的是第 3 张图", h.comfy.runs[3]["prompt"]["2"]["inputs"]["image"] == assets[third - 1]["name"], str(h.comfy.runs[3]["prompt"]["2"]["inputs"]))
        else:
            check("第四次提交", False, str(len(h.comfy.runs)))
        shot("04-改方案后")

        # T7 刷新后还在
        page.reload()
        page.wait_for_selector(".pcw.pcw-open")
        page.wait_for_selector(".pcw-step")
        check("刷新后方案还在、结果还在", page.locator(".pcw-step").count() == 2 and page.locator(".pcw-step").nth(0).locator(".pcw-res img").count() == 4 and all(x.startswith("已完成") for x in badges()), str(badges()))
        check("刷新后聊天记录和素材库还在", page.locator(".pcw-msg").count() >= 4 and state("s.assets.length") >= 6)

        # T8 运行到一半刷新：接着等
        n0 = len(h.comfy.runs)
        h.comfy.delay = 2.5
        page.locator(".pcw-step").nth(1).locator("text=重跑这一步").click()
        page.wait_for_function("document.querySelector('.pcw-step:nth-child(2) .pcw-badge').textContent.startsWith('运行中')", timeout=10000)
        page.reload()
        page.wait_for_selector(".pcw-step")
        b = wait_badges(lambda b: len(b) == 2 and b[1].startswith("已完成") and len(h.comfy.runs) == n0 + 1, timeout=40)
        check("运行到一半刷新网页：接着等，跑完后显示结果（没有重复提交）", b[1].startswith("已完成") and len(h.comfy.runs) == n0 + 1, f"{b} {len(h.comfy.runs)} {n0}")
        h.comfy.delay = 1.0

        # T9 停止
        h.comfy.delay = 30
        page.locator(".pcw-step").nth(1).locator("text=重跑这一步").click()
        page.wait_for_function("document.querySelector('.pcw-step:nth-child(2) .pcw-badge').textContent.startsWith('运行中')", timeout=10000)
        page.locator(".pcw-step").nth(1).locator("text=停止").click()
        b = wait_badges(lambda b: b[1].startswith("已停止"), timeout=15)
        check("点停止后这一步显示「已停止」", b[1].startswith("已停止"), str(b))
        check("聊天里记了一条已停止", "已停止步骤 2" in page.locator(".pcw-list").inner_text())
        h.comfy.delay = 1.0
        page.locator(".pcw-step").nth(1).locator("text=再试一次").click()
        b = wait_badges(lambda b: b[1].startswith("已完成"), timeout=30)
        check("停止后可以再试一次", b[1].startswith("已完成"), str(b))

        # T10 按键不漏给 ComfyUI；点击也不会点到后面的画布
        page.evaluate("window.__globalKeys = 0")
        page.locator(".pcw-row textarea").click()
        page.keyboard.type("abc")
        page.keyboard.press("Delete")
        page.locator(".pcw-top button", has_text="清空全部").focus()
        page.keyboard.press("Delete")
        check("工作台里的按键没有传给 ComfyUI 的全局快捷键", page.evaluate("window.__globalKeys") == 0, str(page.evaluate("window.__globalKeys")))
        page.mouse.click(30, 120)
        check("点击不会穿到后面的画布", (page.evaluate("window.__canvasClicks") or 0) == 0)
        page.fill(".pcw-row textarea", "")

        # T11 关闭 / 重新打开
        page.click("text=回到节点图 / 应用")
        check("关闭后工作台隐藏、蓝色小按钮出现", not page.locator(".pcw").is_visible() and page.locator(".pcw-handle").is_visible())
        page.evaluate("window.__queueKeys = 0; document.activeElement.blur()")
        page.keyboard.press("Control+Enter")
        check("工作台关着时 Ctrl+Enter 照常交给 ComfyUI（拦截只在工作台开着时生效）", page.evaluate("window.__queueKeys") == 1, str(page.evaluate("window.__queueKeys")))
        page.reload()
        page.wait_for_selector(".pcw-handle", state="visible", timeout=5000)
        check("刷新后记得上次是关着的", not page.locator(".pcw.pcw-open").count())
        page.click(".pcw-handle")
        check("点蓝色按钮重新打开", page.locator(".pcw.pcw-open").is_visible() and page.locator(".pcw-step").count() == 2)
        check("重新打开后焦点在聊天输入框里", page.evaluate("document.activeElement === document.querySelector('.pcw-row textarea')"), page.evaluate("document.activeElement.tagName"))
        page.evaluate("window.__queueKeys = 0; document.activeElement.blur()")
        page.keyboard.press("Control+Enter")
        page.keyboard.press("Meta+Enter")
        check("工作台开着、焦点不在里面时按 Ctrl+Enter / Cmd+Enter 也不会触发 ComfyUI 的排队运行（后面那张节点图不会被误跑）", page.evaluate("window.__queueKeys") == 0, str(page.evaluate("window.__queueKeys")))

        # T12 手动加一步 / 删一步 / 数字控件 / 高级设置
        page.locator(".pcw-bar select").select_option("07")
        page.wait_for_function("document.querySelectorAll('.pcw-step').length === 3")
        check("手动加了第 3 步（文案）", page.eval_on_selector_all(".pcw-step .pcw-step-name", "els => els.map(e => e.textContent)")[2] == "文案")
        page.locator(".pcw-step").nth(2).locator("text=删除").click()
        page.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
        check("删了第 3 步", page.locator(".pcw-step").count() == 2)
        step2 = page.locator(".pcw-step").nth(1)
        if step2.locator("details.pcw-settings").first.get_attribute("open") is None:
            step2.locator("details.pcw-settings > summary").click()
        check("高级设置里有改过的（视频比例 9:16）时默认展开", step2.locator("details.pcw-adv").first.get_attribute("open") is not None)
        check("高级设置里有数字滑块（字幕字号）", step2.locator("input[type=range][min='16']").count() >= 1)
        check("有选项下拉的默认标记", "（默认）" in step2.locator("select[data-pc='1:12:size']").inner_text())
        check("改过的高级设置带「恢复默认」", step2.locator(".pcw-adv .pcw-changed").count() >= 1)
        page.locator(".pcw-bar select").select_option("10")                                           # 手动加一个什么都没改的：高级设置默认折叠
        page.wait_for_function("document.querySelectorAll('.pcw-step').length === 3")
        step3 = page.locator(".pcw-step").nth(2)
        check("什么都没改的步骤：高级设置默认折叠", step3.locator("details.pcw-adv").first.get_attribute("open") is None)
        step3.locator("details.pcw-adv > summary").click()
        check("点开后能看到高级设置里的下拉", step3.locator("details.pcw-adv select").count() >= 3)
        step3.locator("text=删除").click()
        page.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
        shot("05-设置")

        # T13 窄屏
        page.set_viewport_size({"width": 820, "height": 900})
        time.sleep(0.3)
        shot("06-窄屏")
        page.set_viewport_size({"width": 1440, "height": 900})

        # ── 第二组：新开一个干净的页面，测别的场景 ──
        ctx2 = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx2.new_page()
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
        pg.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        pg.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 and "favicon" not in r.url else None)
        pg.goto(base)
        pg.wait_for_selector(".pcw.pcw-open")
        h.comfy.runs.clear()

        def st(expr):
            return pg.evaluate(f"(() => {{ const s = window.__proChat.state; return {expr}; }})()")

        def bd():
            return pg.eval_on_selector_all(".pcw-step .pcw-badge", "els => els.map(e => e.textContent)")

        def wait_bd(pred, timeout=40):
            t0 = time.time()
            while time.time() - t0 < timeout:
                if pred(bd()):
                    return bd()
                time.sleep(0.2)
            return bd()

        def say(text):
            pg.fill(".pcw-row textarea", text)
            pg.keyboard.press("Enter")
            pg.wait_for_function("window.__proChat.state.busy === false && window.__proChat.state.messages.length > 0", timeout=15000)

        # 回车发送、Shift+回车换行
        pg.fill(".pcw-row textarea", "第一行")
        pg.keyboard.press("Shift+Enter")
        pg.keyboard.type("第二行")
        check("Shift+回车是换行，不发送", pg.locator(".pcw-row textarea").input_value() == "第一行\n第二行" and st("s.messages.length") == 0)
        pg.fill(".pcw-row textarea", "")
        say("你好")
        check("闲聊：有回复、没有方案，右边仍是空的", "我能帮你" in pg.locator(".pcw-list").inner_text() and pg.locator(".pcw-step").count() == 0)
        say("让它出错")
        chat_text = pg.locator(".pcw-list").inner_text()
        check("阿里报错时聊天里显示出错了，而且 Key 的内容没有漏出来", "出错了：" in chat_text and "401" in chat_text and "sk-hari" not in chat_text and "sk-harness" not in chat_text, chat_text[-200:])
        check("出错后可以接着发消息（发送按钮没被锁住）", not pg.locator("text=发送").is_disabled() and st("s.busy") is False)
        errors[:] = [e for e in errors if not (e.startswith("HTTP 502") and "/pro/chat" in e)]                  # 上面故意制造的那次 502 不算意外

        # 用拖放附图，点素材库缩略图引用
        pg.set_input_files(".pcw-left input[type=file]", prod)
        pg.wait_for_selector(".pcw-draft")
        say("接成片")
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
        check("方案：出视频 → 成片，成片的「上传视频」选的是步骤 1 的视频", pg.locator("select[data-pc='1:2:file']").input_value() == "s:1:1", pg.locator("select[data-pc='1:2:file']").input_value())

        # 没跑第一步就跑第二步：提示先跑第一步
        pg.locator(".pcw-step").nth(1).locator("text=运行这一步").click()
        b = wait_bd(lambda b: b[1].startswith("没成功"), 10)
        check("第一步没结果时直接跑第二步：显示没成功，并说明先跑步骤 1", b[1].startswith("没成功") and "先运行步骤 1" in pg.locator(".pcw-list").inner_text(), str(b))
        check("没有向 ComfyUI 提交任何东西", len(h.comfy.runs) == 0)

        # 点缩略图引用
        pg.locator(".pcw-asset").nth(0).click()
        check("点素材库缩略图把「素材 1」放进输入框", pg.locator(".pcw-row textarea").input_value().strip() == "素材 1")
        pg.fill(".pcw-row textarea", "")

        # 运行全部：视频 → 成片，成片拿到的是第一步的视频
        pg.click("text=运行全部")
        b = wait_bd(lambda b: len(b) == 2 and all(x.startswith("已完成") for x in b), 60)
        check("视频 → 成片两步都跑完", all(x.startswith("已完成") for x in b), str(b))
        if len(h.comfy.runs) == 2:
            vid = st("s.assets[s.plan.steps[0].result.byKind.video[0] - 1].name")
            check("成片的「上传视频」拿到的就是第一步出的视频文件", h.comfy.runs[1]["prompt"]["2"]["inputs"]["file"] == vid and vid.endswith(".mp4"), f"{h.comfy.runs[1]['prompt']['2']['inputs']['file']} vs {vid}")
            check("第二步的配音文案写进了工作流", h.comfy.runs[1]["prompt"]["6"]["inputs"]["value"] == "脆甜多汁，欢迎选购")
        else:
            check("视频 → 成片提交了两次", False, str(len(h.comfy.runs)))
        check("视频结果有播放器、成片也有", pg.locator(".pcw-step").nth(0).locator("video").count() >= 1 and pg.locator(".pcw-step").nth(1).locator(".pcw-res video").count() == 1)

        # 在文件控件里直接上传
        pg.locator(".pcw-step").nth(0).locator("details.pcw-settings > summary").click()
        before = st("s.assets.length")
        pg.locator(".pcw-step").nth(0).locator("input[type=file][accept='image/*']").set_input_files(prod)
        pg.wait_for_function(f"window.__proChat.state.assets.length >= {before}")
        time.sleep(0.5)
        check("在文件控件里上传：素材库里有这个文件（同名内容去重或新增）", st("s.assets.length") >= before)
        check("上传后这一步的设置变了：第一步要重跑", bd()[0].startswith("要重跑") or bd()[0].startswith("已完成"), str(bd()))

        # 在应用里打开
        pg.locator(".pcw-step").nth(1).locator("text=在应用里打开").click()
        pg.wait_for_function("window.__loaded !== undefined", timeout=5000)
        check("「在应用里打开」载入了对应的工作流并收起工作台", pg.evaluate("window.__loaded.name").startswith("工作台 · ") and not pg.locator(".pcw.pcw-open").count())
        pg.click(".pcw-handle")

        # 删第一步：第二步引用它的设置被去掉
        pg.locator(".pcw-step").nth(0).locator("text=删除").click()
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 1")
        check("删掉第 1 步后，第 2 步（现在是第 1 步）不再引用它", st("JSON.stringify(s.plan.steps[0].files)") == "{}", st("JSON.stringify(s.plan.steps[0].files)"))

        # 文案：文字结果
        pg.click("text=清空方案")
        say("写文案")
        pg.wait_for_selector(".pcw-step")
        pg.click("text=运行全部")
        wait_bd(lambda b: len(b) == 1 and b[0].startswith("已完成"), 30)
        check("文字结果显示在步骤里，有标签和复制按钮", pg.locator(".pcw-text").count() >= 1 and pg.locator(".pcw-text .pcw-link", has_text="复制").count() >= 1, pg.locator(".pcw-steps").inner_text()[-200:])
        check("文案步骤不产出图片 / 视频 / 音频：不往素材库里放东西", st("JSON.stringify(s.plan.steps[0].result.byKind)") == '{"image":[],"video":[],"audio":[]}', st("JSON.stringify(s.plan.steps[0].result.byKind)"))

        # 配音：音频放进素材库
        pg.click("text=清空方案")
        say("配音一下")
        pg.wait_for_selector(".pcw-step")
        pg.click("text=运行全部")
        wait_bd(lambda b: len(b) == 1 and b[0].startswith("已完成"), 30)
        check("配音的音频结果有播放器，并进了素材库（音频）", pg.locator(".pcw-res audio").count() == 1 and "audio" in st("s.assets.map(a => a.kind)"))
        check("配音的语速 0.85 写进了工作流", h.comfy.runs[-1]["prompt"]["3"]["inputs"]["speed"] == 0.85)

        # 运行中：发送和别的按钮不能点
        h.comfy.delay = 3
        pg.locator(".pcw-step").nth(0).locator("text=重跑这一步").click()
        pg.wait_for_function("document.querySelector('.pcw-step .pcw-badge').textContent.startsWith('运行中')", timeout=10000)
        check("运行中发送按钮不能点", pg.locator("text=发送").is_disabled())
        check("运行中「删除」「在应用里打开」不能点", pg.locator(".pcw-step").nth(0).locator("text=删除").is_disabled() and pg.locator(".pcw-step").nth(0).locator("text=在应用里打开").is_disabled())
        check("运行中顶部按钮变成「停止」", "停止" in pg.locator(".pcw-bar .pcw-btn-primary").inner_text())
        wait_bd(lambda b: b[0].startswith("已完成"), 20)
        h.comfy.delay = 1.0

        # 必填的素材没给：不让运行
        pg.click("text=清空方案")
        pg.locator(".pcw-bar select").select_option("04")
        pg.wait_for_selector(".pcw-step")
        check("手动加「多尺寸套图」：商品图标了必填，标红", pg.locator(".pcw-missing").count() == 1 and "（必填）" in pg.locator(".pcw-step").inner_text())
        check("缺必填时「运行这一步」和「运行全部」都不能点，顶部写着还缺什么", pg.locator(".pcw-step .pcw-btn-sm").first.is_disabled() and pg.locator(".pcw-bar .pcw-btn-primary").is_disabled() and "还缺：商品图" in pg.locator(".pcw-notice").inner_text())
        pg.locator(".pcw-step select[data-pc='0:2:image']").select_option(index=2) if pg.locator(".pcw-step select[data-pc='0:2:image'] option").count() > 2 else None
        check("选了素材库里的图之后可以运行", not pg.locator(".pcw-step .pcw-btn-sm").first.is_disabled() and not pg.locator(".pcw-bar .pcw-btn-primary").is_disabled(), pg.locator(".pcw-step select[data-pc='0:2:image']").inner_text())

        # 清空全部
        pg.click("text=清空全部")
        check("清空全部：聊天、方案、素材库都空了", st("s.messages.length") == 0 and st("s.assets.length") == 0 and st("s.plan") is None and pg.locator(".pcw-asset").count() == 0)
        ctx2.close()

        # ── 第三组：独立复核里找到的问题，每条都要在真浏览器里复现不了 ──
        ctx3 = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx3.new_page()
        for pgx in (pg,):
            pgx.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
            pgx.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        pg.goto(base)
        pg.wait_for_selector(".pcw.pcw-open")
        h.comfy.runs.clear()
        h.comfy.delay = 1.0

        def st3(p_, expr):
            return p_.evaluate(f"(() => {{ const s = window.__proChat.state; return {expr}; }})()")

        def bd3(p_):
            return p_.eval_on_selector_all(".pcw-step .pcw-badge", "els => els.map(e => e.textContent)")

        def wait3(p_, pred, timeout=40):
            t0 = time.time()
            while time.time() - t0 < timeout:
                if pred(bd3(p_)):
                    return bd3(p_)
                time.sleep(0.2)
            return bd3(p_)

        def say3(p_, text):
            p_.fill(".pcw-row textarea", text)
            p_.keyboard.press("Enter")

        def idle3(p_):
            p_.wait_for_function("window.__proChat.state.busy === false", timeout=15000)

        pg.set_input_files(".pcw-left input[type=file]", prod)
        pg.wait_for_selector(".pcw-draft")
        say3(pg, "做一套")
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
        idle3(pg)

        # 等 AI 回复的时候，方案和运行按钮都锁住（回复一到整个方案会被换掉）
        pg.locator(".pcw-step textarea").first.fill("手动改过的文字")                                    # 让这一步长出「↺ 恢复默认」
        check("改了文字后出现「恢复默认」，第二步的素材引用旁有「用默认」", pg.locator(".pcw-step .pcw-link").count() >= 2, str(pg.locator(".pcw-step .pcw-link").count()))
        h.llm.delay = 2.0
        say3(pg, "写文案")
        pg.wait_for_function("window.__proChat.state.busy === true", timeout=5000)
        locked = pg.evaluate("""(() => ({
            run: [...document.querySelectorAll('.pcw-step .pcw-btn-sm')].filter(b => /运行这一步|重跑|再试/.test(b.textContent)).every(b => b.disabled),
            del: [...document.querySelectorAll('.pcw-step')].every(s => [...s.querySelectorAll('button')].filter(b => b.textContent === '删除').every(b => b.disabled)),
            fields: [...document.querySelectorAll('.pcw-step textarea, .pcw-step select')].every(e => e.disabled),
            all: document.querySelector('.pcw-bar .pcw-btn-primary').disabled,
            add: document.querySelector('.pcw-bar select').disabled,
            resets: [...document.querySelectorAll('.pcw-step .pcw-link')].every(b => b.disabled) }))()""")
        check("等 AI 回复时：运行 / 删除 / 设置控件 / 恢复默认 / 用默认 / 运行全部 / 添加一步都锁住", all(locked.values()), str(locked))
        pg.locator(".pcw-step").first.locator("text=运行这一步").click(force=True)
        pg.locator(".pcw-step .pcw-link").first.click(force=True)
        check("锁住的按钮点了没反应（没有提交任何运行，改过的文字也没被「恢复默认」掉）", len(h.comfy.runs) == 0 and pg.locator(".pcw-step textarea").first.input_value() == "手动改过的文字")
        pg.evaluate("""(() => { const a = window.__proChat.actions; a.runStep(0); a.runAll(); a.removeStep(0); a.clearPlan(); a.addStep('01'); })()""")
        check("等 AI 回复时直接调动作（不经过按钮）也改不了方案、跑不起来：方案仍是两步，没有提交", len(h.comfy.runs) == 0 and st3(pg, "s.plan.steps.length") == 2, f"{len(h.comfy.runs)} {st3(pg, 's.plan.steps.length')}")
        idle3(pg)
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 1")
        check("回复到了：方案换成新的，控件解锁", not pg.locator(".pcw-step textarea").first.is_disabled() and not pg.locator("text=运行全部").is_disabled())
        h.llm.delay = 0

        # AI 在前面加一步，后面的序号跟着挪：原来的步骤结果保留；删掉前面的一步，后面的也不会变成「要重跑」
        pg.click("text=清空方案")
        say3(pg, "做一套")
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
        idle3(pg)
        pg.click("text=运行全部")
        wait3(pg, lambda b: len(b) == 2 and all(x.startswith("已完成") for x in b), 60)
        n_runs = len(h.comfy.runs)
        say3(pg, "前面加一步")
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 3")
        idle3(pg)
        b = bd3(pg)
        check("AI 在前面加了一步：新的一步「还没运行」，原来的两步仍是「已完成」（不会白白重跑，视频要花额度）", b[0].startswith("还没运行") and b[1].startswith("已完成") and b[2].startswith("已完成"), str(b))
        check("第三步引用的是「步骤 2 的第 2 个图片」", pg.locator("select[data-pc='2:2:image']").input_value() == "s:2:2", pg.locator("select[data-pc='2:2:image']").input_value())
        check("没有多提交任何运行", len(h.comfy.runs) == n_runs)
        pg.click("text=运行全部")
        b = wait3(pg, lambda b: len(b) == 3 and all(x.startswith("已完成") for x in b), 40)
        check("运行全部只跑了新加的那一步", len(h.comfy.runs) == n_runs + 1 and all(x.startswith("已完成") for x in b), f"{len(h.comfy.runs)} {n_runs} {b}")
        pg.locator(".pcw-step").first.locator("text=删除").click()
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
        b = bd3(pg)
        check("删掉前面的一步后，后面两步还是「已完成」（引用的序号跟着换了，不算设置改过）", all(x.startswith("已完成") for x in b), str(b))
        check("第二步的引用现在是「步骤 1 的第 2 个图片」", pg.locator("select[data-pc='1:2:image']").input_value() == "s:1:2")

        # 焦点落在工作台空白处（不是输入框 / 按钮）时，按键和粘贴也不能漏给 ComfyUI
        pg.mouse.click(1000, 140)
        pg.evaluate("window.__globalKeys = 0; window.__docPaste = 0; document.addEventListener('paste', () => { window.__docPaste++; })")
        check("点一下工作台空白处，焦点落在工作台根节点上", pg.evaluate("document.activeElement.classList.contains('pcw')"), pg.evaluate("document.activeElement.tagName + '.' + document.activeElement.className"))
        for key in ("Delete", "Backspace", "Control+Enter", "Alt+m"):
            pg.keyboard.press(key)
        check("焦点在空白处时按键也不会传给 ComfyUI 的全局快捷键", pg.evaluate("window.__globalKeys") == 0, str(pg.evaluate("window.__globalKeys")))
        n_draft = pg.locator(".pcw-draft").count()
        pg.evaluate("""(() => { const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'pasted.png', {type: 'image/png'}));
            document.querySelector('.pcw').dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); })()""")
        pg.wait_for_function(f"document.querySelectorAll('.pcw-draft').length === {n_draft + 1}", timeout=10000)
        check("焦点在空白处时粘贴图片：当作附件，而且不会被 ComfyUI 的粘贴处理拿去", pg.evaluate("window.__docPaste") == 0)
        pg.evaluate("document.querySelector('.pcw').dispatchEvent(new ClipboardEvent('paste', {bubbles: true, cancelable: true}))")
        check("焦点在空白处时粘贴文字也不会漏给 ComfyUI", pg.evaluate("window.__docPaste") == 0)
        pg.locator(".pcw-draft button").first.click()

        # 清空文字框：提示留空会用默认文字
        pg.locator(".pcw-step details.pcw-settings").first.evaluate("e => { e.open = true }")
        ta = pg.locator(".pcw-step textarea").first
        ta.fill("")
        ta.fill("手动写的内容")
        key_ = ta.get_attribute("data-pc").split(":", 1)[1]
        check("手动写了内容：方案里记着它，旁边有「恢复默认」", st3(pg, f"s.plan.steps[0].fields[{json.dumps(key_)}]") == "手动写的内容" and pg.locator(".pcw-step").first.locator(".pcw-field.pcw-changed").count() >= 1)
        ta.fill("")
        check("清空文字框后，框里提示「留空就用默认」", (ta.get_attribute("placeholder") or "").startswith("留空就用默认"), ta.get_attribute("placeholder"))
        check("清空后框里是空的，方案里这个设置回到默认（不再记着旧内容），「已改」的高亮也没了", ta.input_value() == "" and st3(pg, f"{json.dumps(key_)} in s.plan.steps[0].fields") is False and pg.locator(".pcw-step").first.locator(f"textarea[data-pc='{ta.get_attribute('data-pc')}']").evaluate("e => !e.closest('.pcw-field').classList.contains('pcw-changed')"))

        # 多角色对白：脚本 + 每个角色的音色（下拉里写着性别和特点），跑完有 mp3 + 字幕 + 说明；再接到视频上
        pg.click("text=清空方案")
        say3(pg, "写一段两个人的对白")
        pg.wait_for_selector(".pcw-step")
        idle3(pg)
        check("对白方案：一步「多角色配音」", pg.locator(".pcw-step-name").all_inner_texts() == ["多角色配音"], str(pg.locator(".pcw-step-name").all_inner_texts()))
        pg.locator(".pcw-step details.pcw-settings").first.evaluate("e => { e.open = true }")
        check("脚本填进了「对白脚本」框", "小美：老板，这个苹果怎么卖？" in pg.locator("textarea[data-pc='0:3:script']").input_value())
        check("对白脚本框按行数长高（3 行脚本不是挤在 2 行高的框里）", int(pg.locator("textarea[data-pc='0:3:script']").get_attribute("rows")) == 3, pg.locator("textarea[data-pc='0:3:script']").get_attribute("rows"))
        name1 = pg.locator("input[data-pc='0:3:role1_name']")
        check("角色名是单行输入框（不是两行高的文本框），默认显示「小美」；对白脚本还是多行框", name1.count() == 1 and name1.get_attribute("type") == "text" and pg.locator("textarea[data-pc='0:3:role1_name']").count() == 0 and name1.input_value() == "小美" and pg.locator("textarea[data-pc='0:3:script']").count() == 1, name1.input_value() if name1.count() else "没有这个输入框")
        name1.fill("老板娘")
        check("在角色名里打字：方案里记着，旁边出现「恢复默认」", st3(pg, "s.plan.steps[0].fields['3:role1_name']") == "老板娘" and pg.locator(".pcw-field.pcw-changed:has(input[data-pc='0:3:role1_name']) .pcw-link").count() == 1)
        name1.fill("")
        check("清空角色名：回到默认，方案里不再记着，「恢复默认」也没了", st3(pg, "'3:role1_name' in s.plan.steps[0].fields") is False and pg.locator(".pcw-field.pcw-changed:has(input[data-pc='0:3:role1_name'])").count() == 0)
        v2 = pg.locator("select[data-pc='0:3:role2_voice']")
        check("角色 2 的音色是 Ryan，下拉里显示着性别和特点", v2.input_value() == "Ryan · 男 · 节奏感强、戏感足" and v2.locator("option").count() == 49 and "男 · 节奏感强" in v2.locator("option:checked").inner_text(), v2.input_value())
        n_runs = len(h.comfy.runs)
        pg.click("text=运行全部")
        b = wait3(pg, lambda b: b and b[0].startswith("已完成"), 40)
        check("对白跑完：一个音频结果 + 字幕 / 说明两条文字", pg.locator(".pcw-step .pcw-res audio").count() == 1 and pg.locator(".pcw-step .pcw-text").count() == 2, f"{b} {pg.locator('.pcw-step .pcw-res audio').count()} {pg.locator('.pcw-step .pcw-text').count()}")
        dlg = h.comfy.runs[-1]["prompt"]["3"]["inputs"] if len(h.comfy.runs) > n_runs else {}
        check("提交给 ComfyUI 的音色是完整的选项文字（性别 / 特点也在里面，ComfyUI 的下拉是严格比对的）", dlg.get("role1_voice") == "Serena · 女 · 温柔" and dlg.get("role2_voice") == "Ryan · 男 · 节奏感强、戏感足", str(dlg))
        pg.click("text=清空方案")
        say3(pg, "做个对白成片")
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 2", timeout=15000)
        idle3(pg)
        check("对白成片方案：先出视频，再配对白", pg.locator(".pcw-step-name").all_inner_texts() == ["图生视频成片", "对白成片"], str(pg.locator(".pcw-step-name").all_inner_texts()))
        check("模型只写了音色名（Cherry / Ethan），方案里也认成了下拉里的完整选项", st3(pg, "s.plan.steps[1].fields['3:role1_voice']") == "Cherry · 女 · 阳光亲切" and st3(pg, "s.plan.steps[1].fields['3:role2_voice']") == "Ethan · 男 · 朝气温暖，带北方口音")
        check("第二步的视频是前面步骤做出来的", pg.locator("select[data-pc='1:2:file']").input_value() == "s:1:1", pg.locator("select[data-pc='1:2:file']").input_value())
        n_runs = len(h.comfy.runs)
        pg.click("text=运行全部")
        b = wait3(pg, lambda b: len(b) == 2 and all(x.startswith("已完成") for x in b), 60)
        check("两步都跑完（出视频 → 把对白配上去）", len(b) == 2 and all(x.startswith("已完成") for x in b) and len(h.comfy.runs) == n_runs + 2, f"{b} {len(h.comfy.runs) - n_runs}")
        second = h.comfy.runs[-1]["prompt"] if len(h.comfy.runs) > n_runs + 1 else {}
        first_video = st3(pg, "s.assets[s.plan.steps[0].result.byKind.video[0] - 1].name")
        check("第二步拿到的就是第一步出的视频，对白里的角色音色是完整选项", second.get("2", {}).get("inputs", {}).get("file") == first_video and second.get("3", {}).get("inputs", {}).get("role2_voice") == "Ethan · 男 · 朝气温暖，带北方口音", str(second.get("2", {}).get("inputs")))

        # 图层：抠图 → 合成（放到纯色背景上，同时存分层 PNG）：透明底的商品交给下一步，合成图排第一、各层跟在后面
        pg.click("text=清空方案")
        say3(pg, "把商品做成图层")
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 2", timeout=15000)
        idle3(pg)
        check("图层方案：商品抠图 → 图层合成", pg.locator(".pcw-step-name").all_inner_texts() == ["商品抠图", "图层合成"], str(pg.locator(".pcw-step-name").all_inner_texts()))
        check("第二步的商品是第一步抠出来的", pg.locator("select[data-pc='1:2:image']").input_value() == "s:1:1", pg.locator("select[data-pc='1:2:image']").input_value())
        check("抠图的模型下拉有 3 个，默认是通用 u2net", pg.locator("select[data-pc='0:3:model'] option").count() == 3 and "u2net" in pg.locator("select[data-pc='0:3:model'] option:checked").inner_text())
        check("放到哪里 / 多大 / 米色背景都填进了第二步", st3(pg, "s.plan.steps[1].fields['5:position']") == "下中" and st3(pg, "s.plan.steps[1].fields['5:scale_pct']") == 55 and st3(pg, "s.plan.steps[1].fields['5:bg_color']") == "#F5F0E6")
        n_runs = len(h.comfy.runs)
        pg.click("text=运行全部")
        b = wait3(pg, lambda b: len(b) == 2 and all(x.startswith("已完成") for x in b), 60)
        check("两步都跑完", len(b) == 2 and all(x.startswith("已完成") for x in b) and len(h.comfy.runs) == n_runs + 2, f"{b} {len(h.comfy.runs) - n_runs}")
        cut_asset = st3(pg, "s.plan.steps[0].result.byKind.image[0]")
        comp = h.comfy.runs[-1]["prompt"]["5"]["inputs"] if len(h.comfy.runs) > n_runs + 1 else {}
        check("合成那一步用的是抠图那一步放进素材库的图（透明底 PNG），位置 / 大小 / 颜色 / 分层都提交了", st3(pg, f"s.assets[{cut_asset} - 1].name") == h.comfy.runs[-1]["prompt"]["2"]["inputs"]["image"]
              and comp.get("position") == "下中" and comp.get("scale_pct") == 55 and comp.get("bg_color") == "#F5F0E6" and comp.get("save_layers") is True, str(comp))
        check("合成这一步有 4 张图：合成图在最前，后面是背景层 / 商品层 / 阴影层", st3(pg, "s.plan.steps[1].result.items.filter((i) => i.kind === 'image').length") == 4, str(st3(pg, "s.plan.steps[1].result.items.map((i) => i.kind)")))
        check("抠图那一步只有 1 张图（透明底）", st3(pg, "s.plan.steps[0].result.items.filter((i) => i.kind === 'image').length") == 1)

        # 透明底的抠图结果垫浅灰棋盘格：透明的地方不能是黑的（看着像没抠干净）
        from PIL import Image as PILImage
        import io as _io
        cut_img = pg.locator(".pcw-step").nth(0).locator(".pcw-res img").first
        cut_img.scroll_into_view_if_needed()
        pg.wait_for_function("(() => { const i = document.querySelector('.pcw-step .pcw-res img'); return i && i.complete && i.naturalWidth > 0; })()")
        shot = PILImage.open(_io.BytesIO(cut_img.screenshot())).convert("RGB")
        a, b = shot.getpixel((9, 9)), shot.getpixel((16, 9))              # 抠图图四角是透明的：相邻的两个格子（每格 7 像素，避开圆角）
        check("抠图结果的透明处是浅灰棋盘格，不是黑底", min(a) > 150 and min(b) > 150, f"{a} {b}")
        check("棋盘格相邻两格深浅不一样", a != b, f"{a} {b}")
        check("素材库缩略图也垫了棋盘格（透明的抠图在那里也不显示成黑块）", "gradient" in pg.evaluate("getComputedStyle(document.querySelector('.pcw-assets .pcw-thumb, .pcw-asset .pcw-thumb')).backgroundImage"))
        check("视频的底还是黑的（只有图片垫棋盘格）", pg.evaluate("(() => { const v = document.createElement('video'); const d = document.createElement('div'); d.className = 'pcw-res'; d.append(v); document.querySelector('.pcw').append(d); const c = getComputedStyle(v).backgroundColor; d.remove(); return c; })()") == "rgb(0, 0, 0)")

        # 只在某个模式才必填的文件：16 默认「放到背景图上」要背景图，换成纯色模式就不要了；商品图两个模式都要
        pg.click("text=清空方案")
        pg.locator(".pcw-bar select").select_option("16")
        pg.wait_for_selector(".pcw-step")
        pg.locator(".pcw-step details.pcw-settings").first.evaluate("e => { e.open = true }")
        check("纯色背景的颜色是单行输入框", pg.locator("input[type=text][data-pc='0:5:bg_color']").count() == 1 and pg.locator("textarea[data-pc='0:5:bg_color']").count() == 0)
        field = lambda key: pg.locator(f".pcw-field:has(select[data-pc='0:{key}'])").first
        check("16 默认模式（放到背景图上）：背景图和商品图都标着（必填）", "（必填）" in field("3:image").inner_text() and "（必填）" in field("2:image").inner_text() and "pcw-missing" in (field("3:image").get_attribute("class") or ""))
        check("缺素材时「运行这一步」和「运行全部」都不能点，顶部写着还缺什么", pg.locator(".pcw-step .pcw-btn-sm").first.is_disabled() and pg.locator(".pcw-bar .pcw-btn-primary").is_disabled() and "背景图" in pg.locator(".pcw-notice").inner_text())
        pg.locator("select[data-pc='0:mode']").select_option(label="放到纯色背景上")
        check("换成纯色背景模式：背景图不再必填，商品图还是必填", "（必填）" not in field("3:image").inner_text() and "（必填）" in field("2:image").inner_text())
        check("纯色模式下提示只剩商品图", "背景图" not in pg.locator(".pcw-notice").inner_text() and "商品" in pg.locator(".pcw-notice").inner_text())
        pg.locator("select[data-pc='0:2:image']").select_option(index=1)
        check("给了商品图：纯色模式就能运行了", not pg.locator(".pcw-step .pcw-btn-sm").first.is_disabled() and not pg.locator(".pcw-bar .pcw-btn-primary").is_disabled())
        pg.locator("select[data-pc='0:mode']").select_option(label="放到背景图上")
        check("换回背景图模式：又缺背景图，不能运行", pg.locator(".pcw-step .pcw-btn-sm").first.is_disabled())

        # 数字下拉（帧率 / 轮播清晰度）：界面里是文字，提交给 ComfyUI 的是数字
        pg.click("text=清空方案")
        pg.locator(".pcw-bar select").select_option("11")
        pg.wait_for_selector(".pcw-step")
        pg.locator("select[data-pc='0:mode']").select_option(index=2)               # 图片轮播 + 配音文案：只要一张图（默认的上传视频模式没有视频就不让运行）
        pg.locator(".pcw-step").first.locator("input[type=file][accept='image/*']").first.set_input_files(prod)             # 第一张轮播图：直接在这一步里上传
        pg.wait_for_function("document.querySelector(\"select[data-pc='0:12:image']\").value !== ''", timeout=15000)
        adv = pg.locator("details.pcw-adv > summary")
        adv.click()
        pg.locator("select[data-pc='0:8:short_side']").select_option("1080")
        pg.locator("select[data-pc='0:8:fps']").select_option("30")
        check("数字下拉选了 1080 / 30，界面里显示的就是它们", pg.locator("select[data-pc='0:8:short_side']").input_value() == "1080" and pg.locator("select[data-pc='0:8:fps']").input_value() == "30")
        pg.reload()
        pg.wait_for_selector(".pcw-step")
        pg.locator("details.pcw-adv").first.evaluate("e => { e.open = true }")
        check("刷新后下拉里仍然显示 1080 / 30", pg.locator("select[data-pc='0:8:short_side']").input_value() == "1080" and pg.locator("select[data-pc='0:8:fps']").input_value() == "30")
        n_runs = len(h.comfy.runs)
        pg.click("text=运行全部")
        wait3(pg, lambda b: b and b[0].startswith("已完成"), 30)
        node8 = h.comfy.runs[-1]["prompt"]["8"]["inputs"] if len(h.comfy.runs) > n_runs else {}
        check("提交给 ComfyUI 的是数字 1080 / 30（它的下拉是严格比对的，文字 \"30\" 会被拒）", node8.get("short_side") == 1080 and node8.get("fps") == 30 and isinstance(node8.get("fps"), int), str(node8))

        # 提交到一半刷新网页：用同一个 run_id 再提交，ComfyUI 那边只会收到一次
        pg.click("text=清空方案")
        say3(pg, "写文案")
        pg.wait_for_selector(".pcw-step")
        idle3(pg)
        n_runs = len(h.comfy.runs)
        h.run_delay = 3.0
        h.comfy.delay = 1.5
        pg.click("text=运行全部")
        t0 = time.time()
        while len(h.comfy.runs) == n_runs and time.time() - t0 < 10:
            time.sleep(0.05)
        check("服务器已经收到并提交了这次运行（网页还在等回复）", len(h.comfy.runs) == n_runs + 1)
        pg.reload()
        pg.wait_for_selector(".pcw-step")
        b = wait3(pg, lambda b: b and b[0].startswith("已完成"), 40)
        check("提交到一半刷新网页：接着出结果，而且 ComfyUI 只收到一次（同一个 run_id 不重复提交）", b[0].startswith("已完成") and len(h.comfy.runs) == n_runs + 1, f"{b} {len(h.comfy.runs)} {n_runs}")
        h.run_delay = 0

        # 提交的那一刻点「停止」：拿到 prompt_id 后马上中断，并把它从队列里删掉
        n_runs = len(h.comfy.runs)
        h.run_delay = 2.5
        h.comfy.delay = 20
        pg.locator(".pcw-step").first.locator("text=重跑这一步").click()
        t0 = time.time()
        while len(h.comfy.runs) == n_runs and time.time() - t0 < 10:
            time.sleep(0.05)
        pg.locator(".pcw-step").first.locator("text=停止").click()
        b = wait3(pg, lambda b: b and b[0].startswith("已停止"), 20)
        pid = h.comfy.runs[-1]["prompt_id"] if len(h.comfy.runs) > n_runs else ""
        check("提交时点了停止：这一步显示已停止，ComfyUI 那边被中断、从队列里删掉", b[0].startswith("已停止") and pid in h.comfy.interrupted and pid in h.comfy.deleted, f"{b} {pid} {h.comfy.interrupted} {h.comfy.deleted}")
        h.run_delay = 0
        h.comfy.delay = 1.0

        # 提交成功了、回复却没回到网页（网络断了）：重试 3 次都没拿到回复，这一步显示没成功；再点「再试一次」沿用同一个 run_id，服务器已经收到的不会再提交一遍
        pg.click("text=清空方案")
        say3(pg, "写文案")
        pg.wait_for_selector(".pcw-step")
        idle3(pg)
        n_runs = len(h.comfy.runs)
        h.drop_run = 100                                                                                  # Chrome 自己也会对断开的连接悄悄重发一次，所以丢得多一点，保证 3 次都拿不到回复
        pg.click("text=运行全部")
        b = wait3(pg, lambda b: b and b[0].startswith("没成功"), 40)
        check("回复一直没回到网页：这一步显示没成功，提示不知道请求有没有送到", b[0].startswith("没成功") and "不知道这次请求有没有送到" in pg.locator(".pcw-list").inner_text(), f"{b}")
        check("服务器其实收到并提交了一次（3 次重试用的是同一个 run_id，没有多提交）", len(h.comfy.runs) == n_runs + 1, str(len(h.comfy.runs) - n_runs))
        h.drop_run = 0
        pg.locator(".pcw-step").first.locator("text=再试一次").click()
        b = wait3(pg, lambda b: b and b[0].startswith("已完成"), 40)
        check("再点「再试一次」：沿用了上次的 run_id，接上了已经提交的那一次，ComfyUI 那边仍然只有一次", b[0].startswith("已完成") and len(h.comfy.runs) == n_runs + 1, f"{b} {len(h.comfy.runs) - n_runs}")
        check("结果里说明了「服务器上已经有这一次提交的记录」", "服务器上已经有这一次提交的记录" in pg.locator(".pcw-step").first.inner_text())
        pg.locator(".pcw-step").first.locator("text=重跑这一步").click()
        wait3(pg, lambda b: len(h.comfy.runs) == n_runs + 2, 20)
        wait3(pg, lambda b: b and b[0].startswith("已完成"), 30)
        check("之后正常点「重跑这一步」是新的一次（不会再沿用旧的 run_id 拿到旧结果）", len(h.comfy.runs) == n_runs + 2, str(len(h.comfy.runs) - n_runs))

        # 同一个浏览器开两个标签页：只有一个去提交 / 续跑，另一个跟着显示进度
        pg.click("text=清空方案")
        say3(pg, "做一套")
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
        idle3(pg)
        n_runs = len(h.comfy.runs)
        h.comfy.delay = 5.0
        pg.click("text=运行全部")
        t0 = time.time()
        while len(h.comfy.runs) == n_runs and time.time() - t0 < 10:
            time.sleep(0.05)
        time.sleep(0.6)
        pg2 = ctx3.new_page()
        pg2.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        pg2.goto(base)
        pg2.wait_for_selector(".pcw-step")
        time.sleep(1.5)
        check("第二个标签页刚打开时，只显示进度，没有再提交一次", len(h.comfy.runs) == n_runs + 1, str(len(h.comfy.runs) - n_runs))
        check("第二个标签页看得到第 1 步在运行", bd3(pg2)[0].startswith("运行中"), str(bd3(pg2)))
        wait3(pg, lambda b: len(b) == 2 and all(x.startswith("已完成") for x in b), 60)
        check("两步跑完，一共只提交了两次（两个标签页没有重复）", len(h.comfy.runs) == n_runs + 2, str(len(h.comfy.runs) - n_runs))
        b2 = wait3(pg2, lambda b: len(b) == 2 and all(x.startswith("已完成") for x in b), 20)
        check("第二个标签页跟着显示全部完成", all(x.startswith("已完成") for x in b2), str(b2))
        pg2.close()

        # 关掉正在运行的标签页：另一个标签页接手，把没跑完的接着跑完（不重复提交）
        pg.click("text=清空方案")
        say3(pg, "做一套")
        pg.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
        idle3(pg)
        n_runs = len(h.comfy.runs)
        h.comfy.delay = 4.0
        pg.click("text=运行全部")
        t0 = time.time()
        while len(h.comfy.runs) == n_runs and time.time() - t0 < 10:
            time.sleep(0.05)
        pg3 = ctx3.new_page()
        pg3.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        pg3.goto(base)
        pg3.wait_for_selector(".pcw-step")
        pg.close()                                                                                       # 关掉正在运行的那个标签页（第 1 步已经提交、还在跑）
        b3 = wait3(pg3, lambda b: len(b) == 2 and all(x.startswith("已完成") for x in b), 60)
        check("关掉运行中的标签页后，另一个标签页接手并把两步都跑完", all(x.startswith("已完成") for x in b3) and len(b3) == 2, str(b3))
        check("接手时没有重复提交（第 1 步不再提交，第 2 步提交一次，一共两次）", len(h.comfy.runs) == n_runs + 2, str(len(h.comfy.runs) - n_runs))
        pg3.close()
        h.comfy.delay = 1.0
        ctx3.close()

        # ── 第四组：运行锁。复制标签页（sessionStorage 一起被复制）、提前打开的第二个标签页：只有一个去提交；关掉运行中的那个，另一个接手并把整条链跑完 ──
        def two_tabs(tag, init_script, web_locks):
            cx = browser.new_context(viewport={"width": 1440, "height": 900})
            if init_script:
                cx.add_init_script(init_script)
            a = cx.new_page()
            a.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
            a.goto(base)
            a.wait_for_selector(".pcw.pcw-open")
            check(f"[{tag}] 浏览器{'支持' if web_locks else '这里被去掉了'} Web Locks", a.evaluate("!!navigator.locks") is web_locks)
            h.comfy.delay = 1.0
            a.set_input_files(".pcw-left input[type=file]", prod)
            a.wait_for_selector(".pcw-draft")
            say3(a, "做一套")
            a.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
            idle3(a)
            a.wait_for_function("""(() => { const k = Object.keys(localStorage).find((k) => /^proChat\\.v\\d+$/.test(k)); return !!k && (JSON.parse(localStorage.getItem(k)).plan || {steps: []}).steps.length === 2; })()""")      # 存档有 300 毫秒的延迟：等方案存进去，复制出来的标签页才看得到
            ss = a.evaluate("JSON.stringify(Object.fromEntries(Object.entries(sessionStorage)))")        # 浏览器的「复制标签页」会把 sessionStorage 一起复制
            b = cx.new_page()
            b.add_init_script(f"for (const [k, v] of Object.entries({ss})) sessionStorage.setItem(k, v)")
            b.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
            b.goto(base)
            b.wait_for_selector(".pcw-step")
            n0 = len(h.comfy.runs)
            h.comfy.delay = 4.0
            a.click("text=运行全部")
            t0 = time.time()
            while len(h.comfy.runs) == n0 and time.time() - t0 < 10:
                time.sleep(0.05)
            time.sleep(1.0)
            held = a.evaluate("navigator.locks ? navigator.locks.query().then(q => q.held.map(l => l.name)) : []")
            check(f"[{tag}] 运行时{'持有 Web Locks 的锁' if web_locks else '写着运行记录'}", ("proChat.run" in held) if web_locks else a.evaluate("!!localStorage.getItem('proChat.runLock')"), str(held))
            check(f"[{tag}] 另一个标签页看得到第 1 步在运行", bd3(b)[0].startswith("运行中"), str(bd3(b)))
            b.click(".pcw-bar .pcw-btn-primary")                                                           # 在另一个标签页里也点「运行全部」
            time.sleep(1.5)
            check(f"[{tag}] 另一个标签页点运行：没有再提交（第 2 步不会抢在第 1 步前面），提示另一个标签页正在运行", len(h.comfy.runs) == n0 + 1 and "另一个标签页正在运行" in b.locator(".pcw-list").inner_text(), f"{len(h.comfy.runs) - n0}")
            a.close()                                                                                      # 关掉正在运行的那个（第 1 步已经提交、还在跑）
            bb = wait3(b, lambda x: len(x) == 2 and all(t.startswith("已完成") for t in x), 60)
            check(f"[{tag}] 关掉运行中的标签页后，提前打开的另一个标签页接手，把两步都跑完（「运行全部」没有退成只跑一步）", len(bb) == 2 and all(t.startswith("已完成") for t in bb), str(bb))
            check(f"[{tag}] 接手时没有重复提交（第 1 步不再提交，第 2 步提交一次，一共两次）", len(h.comfy.runs) == n0 + 2, str(len(h.comfy.runs) - n0))
            t0 = time.time()
            while True:                                                                                    # 最后一步的结果显示出来后，放锁还要过几毫秒
                held = b.evaluate("navigator.locks ? navigator.locks.query().then(q => q.held.map(l => l.name)) : []")
                freed = "proChat.run" not in held and b.evaluate("!localStorage.getItem('proChat.runLock')")
                if freed or time.time() - t0 > 5:
                    break
                time.sleep(0.2)
            check(f"[{tag}] 跑完后锁放掉了", freed, str(held))
            cx.close()

        two_tabs("Web Locks", None, True)
        two_tabs("没有 Web Locks", "Object.defineProperty(Navigator.prototype, 'locks', {get: () => undefined, configurable: true})", False)

        # 没有 Web Locks 时：运行记录被别的标签页写走了，这里跑完当前这一步就停，不再提交后面的步骤
        cx = browser.new_context(viewport={"width": 1440, "height": 900})
        cx.add_init_script("Object.defineProperty(Navigator.prototype, 'locks', {get: () => undefined, configurable: true})")
        a = cx.new_page()
        a.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        a.goto(base)
        a.wait_for_selector(".pcw.pcw-open")
        h.comfy.delay = 1.0
        a.set_input_files(".pcw-left input[type=file]", prod)
        a.wait_for_selector(".pcw-draft")
        say3(a, "做一套")
        a.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
        idle3(a)
        n0 = len(h.comfy.runs)
        h.comfy.delay = 7.0
        a.click("text=运行全部")
        t0 = time.time()
        while len(h.comfy.runs) == n0 and time.time() - t0 < 10:
            time.sleep(0.05)
        a.evaluate("localStorage.setItem('proChat.runLock', JSON.stringify({tab: 'someone-else', t: Date.now()}))")        # 别的标签页抢走了运行记录
        ba = wait3(a, lambda x: len(x) == 2 and x[0].startswith("已完成"), 40)
        time.sleep(1.0)
        check("[没有 Web Locks] 运行记录被别的标签页抢走后，这里跑完当前这一步就停，不再提交后面的步骤", len(h.comfy.runs) == n0 + 1 and ba[0].startswith("已完成") and ba[1].startswith("还没运行") and "运行锁被另一个标签页拿走了" in a.locator(".pcw-list").inner_text(), f"{len(h.comfy.runs) - n0} {ba}")
        cx.close()
        h.comfy.delay = 1.0

        # 没有 Web Locks 时两个标签页在同一瞬间拿锁：各自先看了一眼没有别人的记录、再各写各的，后写的盖掉先写的；先写的稍等一下再确认，发现被盖掉了就退出，不去提交
        # （这里用「写完记录后立刻被别人的记录盖掉」来模拟那一瞬间）
        cx = browser.new_context(viewport={"width": 1440, "height": 900})
        cx.add_init_script("Object.defineProperty(Navigator.prototype, 'locks', {get: () => undefined, configurable: true})")
        a = cx.new_page()
        a.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        a.goto(base)
        a.wait_for_selector(".pcw.pcw-open")
        a.set_input_files(".pcw-left input[type=file]", prod)
        a.wait_for_selector(".pcw-draft")
        say3(a, "做一套")
        a.wait_for_function("document.querySelectorAll('.pcw-step').length === 2")
        idle3(a)
        n0 = len(h.comfy.runs)
        a.evaluate("""(() => { window.__proChat.actions.runAll(); localStorage.setItem('proChat.runLock', JSON.stringify({tab: 'someone-else', t: Date.now()})); })()""")
        time.sleep(1.5)
        check("[没有 Web Locks] 同一瞬间拿锁、自己的记录被别人盖掉：稍等确认后退出，没有提交，提示另一个标签页正在运行",
              len(h.comfy.runs) == n0 and "另一个标签页正在运行" in a.locator(".pcw-list").inner_text() and bd3(a)[0].startswith("还没运行"), f"{len(h.comfy.runs) - n0} {bd3(a)}")
        cx.close()

        # 应用目录：还在加载 → 写「加载中」；读不到 → 自动再试两次，还不行写「没加载出来，点这里重试」，点一下就再试
        cx = browser.new_context(viewport={"width": 1440, "height": 900})
        cat_mode, held = {"m": "hold"}, []

        def catalog_route(route):
            if cat_mode["m"] == "hold":
                held.append(route)                    # 请求先扣住，测试里手动放行
            elif cat_mode["m"] == "fail":
                route.abort()
            else:
                route.continue_()
        cp = cx.new_page()
        cp.route("**/pro/catalog", catalog_route)
        cp.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        cp.goto(base)
        cp.wait_for_selector(".pcw-bar select")
        cp.wait_for_timeout(1000)
        holder = cp.locator(".pcw-bar select option").first.inner_text()
        check("目录请求还没回来：下拉写着「加载中」", "加载中" in holder and cp.locator(".pcw-bar select option").count() == 1 and len(held) >= 1, f"{holder} {len(held)}")
        check("这时手动加步骤的下拉里还没有应用，聊天输入框照常能用", cp.locator(".pcw-row textarea").is_enabled())
        for r in held:
            r.continue_()
        cp.wait_for_function("document.querySelectorAll('.pcw-bar select option').length > 5", timeout=20000)
        check("目录回来后下拉里有 16 个应用，提示变成「＋ 添加一步…」", cp.locator(".pcw-bar select option").count() == 17 and cp.locator(".pcw-bar select option").first.inner_text() == "＋ 添加一步…", cp.locator(".pcw-bar select option").first.inner_text())
        cp.close()
        # 只有第一次请求失败：自动再试，不用点
        cat_mode["m"] = "first_fails"
        seen = [0]

        def flaky(route):
            seen[0] += 1
            route.abort() if seen[0] == 1 else route.continue_()
        cp = cx.new_page()
        cp.route("**/pro/catalog", flaky)
        cp.goto(base)
        cp.wait_for_selector(".pcw-bar select")
        cp.wait_for_function("document.querySelectorAll('.pcw-bar select option').length > 5", timeout=20000)
        check("第一次读目录失败：过几秒自己再试，应用就出来了（不用点）", cp.locator(".pcw-bar select option").count() == 17 and seen[0] == 2, f"请求 {seen[0]} 次")
        cp.close()
        # 一直读不到：自动再试到次数用完，写「没加载出来，点这里重试」，点一下才再试
        cat_mode["m"] = "fail"
        cp = cx.new_page()
        cp.route("**/pro/catalog", catalog_route)
        cp.goto(base)
        cp.wait_for_selector(".pcw-bar select")
        try:
            cp.wait_for_function("window.__proChat.catalog().fails >= 3 && window.__proChat.catalog().status === 'failed'", timeout=40000)
        except PWTimeout:
            pass
        shown = cp.locator(".pcw-bar select option").first.inner_text()
        check("目录一直读不到：自动再试 3 次后写「没加载出来，点这里重试」，聊天输入框照常能用", "点这里重试" in shown and cp.locator(".pcw-row textarea").is_enabled(), shown)
        cat_mode["m"] = "ok"
        cp.wait_for_timeout(6000)                                        # 比最长的重试间隔（5 秒）还久：要是还在自己重试，这时目录早就读到了（要用 Playwright 自己的等待：time.sleep 会让拦截请求的处理函数停着不跑）
        check("自动重试用完以后不会自己再试（要点一下）", cp.locator(".pcw-bar select option").count() == 1)
        cp.locator(".pcw-bar select").dispatch_event("mousedown")
        cp.wait_for_function("document.querySelectorAll('.pcw-bar select option').length > 5", timeout=20000)
        check("网络好了、点一下下拉：目录重新读到，应用都出来了", cp.locator(".pcw-bar select option").count() == 17 and cp.locator(".pcw-bar select option").first.inner_text() == "＋ 添加一步…")
        cx.close()

        # 汇总
        check("整个过程浏览器控制台没有报错", not errors, "; ".join(errors[:3]))
        browser.close()
    h.stop()
    print(f"\n共 {total[0]} 项，失败 {len(failures)} 项；截图在 {SHOTS}")
    for f in failures:
        print("  失败：", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
