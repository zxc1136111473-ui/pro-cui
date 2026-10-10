"""pro-chat 前端的纯函数（web/pro-chat-lib.js）：用 node 直接跑；没有 node 就跳过。
界面本身（web/pro-chat-ui.js 画页面、web/pro-chat.js 接 ComfyUI）没法在这里跑，靠假页面 / 真实界面验证（见 tests/ui_harness）；这里只做静态检查：
用到的每个 lib.xxx 都必须真的被库导出、不能有会把模型输出当 HTML 写进页面的写法、只和我们自己的接口说话。"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WEB = os.path.join(ROOT, "custom-nodes", "pro-chat", "web")
LIB = os.path.join(WEB, "pro-chat-lib.js")
NODE = shutil.which("node")

# 测试里用的方案：app(…) 造一步，带一份够用的 schema
PRELUDE = r'''
const field = (key, kind, extra = {}) => ({ key, label: key, kind, default: kind === "text" ? "" : kind === "bool" ? false : 0, ...extra });
const app = (workflow, over = {}) => L.newStep({ workflow, name: "应用" + workflow, path: workflow + ".app.json", schema: {
  mode: { key: "77:mode", label: "模式", options: ["A 模式", "B 模式"], default: "A 模式" },
  fields: { "70:idea": field("70:idea", "text"), "3:speed": field("3:speed", "float", { default: 1, min: 0.5, max: 2, step: 0.05 }), "3:loop": field("3:loop", "bool"),
            "3:model": field("3:model", "choice", { options: ["m1", "m2"], default: "m1", advanced: true }) },
  files: { "4:image": { key: "4:image", label: "商品图", kind: "image" }, "5:audio": { key: "5:audio", label: "背景音乐", kind: "audio" } },
  produces: over.produces || ["image"], cost: over.cost || "", outputs: [9] }, ...over.step });
const done = (st, byKind = {}, runs = 1) => { st.state = "done"; st.runs = runs; st.result = { items: [], error: "", warnings: [], elapsed: 1000, byKind: { image: [], video: [], audio: [], ...byKind } }; st.ran = L.stepParts(st); return st; };
const plan = (...steps) => ({ steps, note: "" });
'''


def js(script):
    """在 node 里（ES 模块）跑 script，它里面用 L 访问 lib，最后 console.log(JSON.stringify(结果))；返回解析后的结果。"""
    code = f'import * as L from "file://{LIB}";\n{PRELUDE}\n{script}'
    r = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr[-1500:])
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "没有 node")
class Lib(unittest.TestCase):
    def test_file_kind_and_urls(self):
        r = js('''console.log(JSON.stringify([L.fileKind("成片.MP4"), L.fileKind("a.png"), L.fileKind("配音_00001.mp3"), L.fileKind("x.wav"),
          L.fileKind("AI 写的提示词_00001.txt"), L.fileKind("x.bin"), L.fileKind("没有后缀"), L.fileKind(""), L.fileKind(null),
          L.viewPath({filename: "出错 信息.txt", subfolder: "text", type: "output"}), L.inputPath("助手/a b.png"), L.inputPath("demo.png"), L.baseName("助手/a.png"), L.baseName("a.png")]))''')
        self.assertEqual(r[:9], ["video", "image", "audio", "audio", "text", "other", "other", "other", "other"])
        self.assertEqual(r[9], "/view?filename=%E5%87%BA%E9%94%99+%E4%BF%A1%E6%81%AF.txt&type=output&subfolder=text")
        self.assertEqual(r[10], "/view?filename=a+b.png&type=input&subfolder=%E5%8A%A9%E6%89%8B")
        self.assertEqual(r[11], "/view?filename=demo.png&type=input&subfolder=")
        self.assertEqual(r[12:], ["a.png", "a.png"])

    ENTRY = {"outputs": {
        "4": {"images": [{"filename": "txt2img_00001_.png", "subfolder": "", "type": "output"}]},
        "5": {"images": [{"filename": "成片_00001_.mp4", "subfolder": "video", "type": "output"}], "animated": [True]},
        "7": {"audio": [{"filename": "配音_00001.mp3", "subfolder": "audio", "type": "output"}]},
        "11": {"text": ["{\"code\": \"success\"}"]},                                   # PreviewAny 的裸字符串：不是结果项
        "90": {"files": [{"filename": "AI 写的提示词_00001.txt", "subfolder": "text", "type": "output", "display_name": "AI 写的提示词"}]},
        "91": {"files": [{"filename": "出错信息_00001.txt", "subfolder": "text", "type": "output", "display_name": "出错信息：视频"}]},
        "92": {"files": [{"filename": "x.bin", "subfolder": "", "type": "output"}]}}}

    def test_history_items_follow_the_app_order_and_kinds(self):
        r = js(f'''const e = {json.dumps(self.ENTRY)}; console.log(JSON.stringify(L.historyItems(e, [5, 4, 7, 90, 91, 92, 11, 99])))''')
        self.assertEqual([(i["kind"], i["file"]["filename"], i["label"], i["isError"]) for i in r], [
            ("video", "成片_00001_.mp4", "", False), ("image", "txt2img_00001_.png", "", False), ("audio", "配音_00001.mp3", "", False),
            ("text", "AI 写的提示词_00001.txt", "AI 写的提示词", False), ("text", "出错信息_00001.txt", "出错信息：视频", True)])
        self.assertEqual(js('console.log(JSON.stringify(L.historyItems(null, [1])))'), [])
        self.assertEqual(js('console.log(JSON.stringify(L.historyItems({outputs: {}}, undefined)))'), [])

    def test_only_media_files_in_the_output_folder_are_staged_into_the_asset_library(self):
        r = js(f'''const items = L.historyItems({json.dumps(self.ENTRY)}, [5, 4, 7, 90, 91]);
          items.push({{kind: "image", file: {{filename: "t.png", type: "temp"}}}}, {{kind: "image", file: {{filename: "d.png"}}}});
          console.log(JSON.stringify(L.stageable(items).map((i) => i.file.filename)))''')
        self.assertEqual(r, ["成片_00001_.mp4", "txt2img_00001_.png", "配音_00001.mp3", "d.png"])      # 文字不放进素材库；temp 目录的不要；没写 type 当 output

    def test_history_error_and_finished(self):
        err = {"status": {"status_str": "error", "completed": False, "messages": [["execution_start", {}], ["execution_error", {"node_id": "7", "node_type": "ProAliTTS", "exception_message": "[阿里 配音] HTTP 401"}]]}}
        ok = {"status": {"status_str": "success", "completed": True, "messages": []}}
        r = js(f'''console.log(JSON.stringify([L.historyError({json.dumps(err)}, {{"7": "配音"}}), L.historyError({json.dumps(err)}, {{}}), L.historyError({json.dumps(ok)}, {{}}),
          L.historyError(undefined, {{}}), L.isFinished({json.dumps(err)}), L.isFinished({json.dumps(ok)}), L.isFinished(undefined), L.isFinished({{status: {{status_str: "running"}}}})]))''')
        self.assertEqual(r, ["「配音」[阿里 配音] HTTP 401", "「ProAliTTS」[阿里 配音] HTTP 401", "", "", True, True, False, False])

    def test_interrupted_outcome_and_queue(self):
        stopped = {"status": {"status_str": "error", "completed": False, "messages": [["execution_start", {}], ["execution_interrupted", {"node_id": "74"}]]}}
        errored = {"status": {"status_str": "error", "completed": False, "messages": [["execution_error", {"node_id": "7"}]]}}
        r = js(f'''const T = (kind, isError = false) => ({{kind, isError}});
          console.log(JSON.stringify([L.wasInterrupted({json.dumps(stopped)}), L.wasInterrupted({json.dumps(errored)}), L.wasInterrupted(undefined),
            L.runOutcome([T("text"), T("text", true)], ""), L.runOutcome([T("image"), T("text", true)], ""), L.runOutcome([T("text")], ""), L.runOutcome([], ""), L.runOutcome([], "运行出错了"),
            L.runOutcome([T("text")], "「配音」HTTP 401"),
            L.inQueue({{queue_running: [[0, "p1", {{}}, {{}}, []]], queue_pending: [[1, "p2", {{}}, {{}}, []]]}}, "p2"), L.inQueue({{queue_running: [], queue_pending: []}}, "p1"), L.inQueue(null, "p1")]))''')
        self.assertEqual(r, [True, False, False, "failed", "done", "done", "done", "failed", "done", True, False, False])

    def test_asset_library_numbers_by_position_dedupes_by_name_and_has_a_cap(self):
        r = js('''const a = [];
          const r1 = L.addAsset(a, {name: "助手/a.png", label: "第一张"}), r2 = L.addAsset(a, {name: "助手/v.mp4"}), r3 = L.addAsset(a, {name: "助手/a.png", label: "改了名"});
          const full = Array.from({length: L.MAX_ASSETS - 2}, (_, i) => ({name: "助手/f" + i + ".png"})); full.forEach((x) => L.addAsset(a, x));
          const over = L.addAsset(a, {name: "助手/new.png"}), again = L.addAsset(a, {name: "助手/f0.png"});
          console.log(JSON.stringify([r1, r2, r3, a[0], a[1].kind, a.length, over, again, L.assetLabel(a[0]), L.assetLabel(a[1]), L.addAsset([], {name: "x", label: "长".repeat(100)}).n]))''')
        self.assertEqual(r[:3], [{"n": 1, "added": True}, {"n": 2, "added": True}, {"n": 1, "added": False}])
        self.assertEqual(r[3], {"name": "助手/a.png", "kind": "image", "label": "第一张"})            # 同名不覆盖原来的说明
        self.assertEqual((r[4], r[5]), ("video", 200))
        self.assertEqual(r[6], {"n": 0, "added": False})                                              # 满了：n = 0，不加
        self.assertEqual(r[7], {"n": 3, "added": False})                                              # 已经有的照样能找到编号
        self.assertEqual((r[8], r[9]), ("第一张", "v.mp4"))
        self.assertEqual(r[10], 1)

    def test_set_field_keeps_only_real_changes(self):
        r = js('''const st = app("01"), f = st.schema.fields;
          L.setField(st, f["3:speed"], 1.5); const a = {...st.fields};
          L.setField(st, f["3:speed"], 1); const b = {...st.fields};                    // 改回默认：去掉
          L.setField(st, f["70:idea"], "  橘猫  "); const c = {...st.fields};
          L.setField(st, f["70:idea"], "   "); const d = {...st.fields};               // 清空：去掉
          L.setField(st, f["3:loop"], true); L.setField(st, f["3:model"], "m2"); const e = {...st.fields};
          L.setField(st, f["3:loop"], false); L.setField(st, f["3:model"], "m1"); const g = {...st.fields};
          console.log(JSON.stringify([a, b, c, d, e, g, L.fieldValue(st, f["3:speed"]), L.fieldValue(st, f["70:idea"]), L.fieldValue(st, f["3:loop"])]))''')
        self.assertEqual(r[0], {"3:speed": 1.5})
        self.assertEqual(r[1], {})
        self.assertEqual(r[2], {"70:idea": "  橘猫  "})
        self.assertEqual(r[3], {})
        self.assertEqual(r[4], {"3:loop": True, "3:model": "m2"})
        self.assertEqual(r[5], {})
        self.assertEqual(r[6:], [1, "", False])

    def test_number_helpers(self):
        r = js('''const f = {kind: "float", min: 0.5, max: 2, step: 0.05}, i = {kind: "int", min: 1, max: 8, step: 1};
          console.log(JSON.stringify([L.decimals(0.05), L.decimals(0.5), L.decimals(1), L.decimals(undefined), L.decimals(1e-7),
            L.clampNumber(0.1 + 0.2 + 1, f), L.clampNumber("9", f), L.clampNumber(-3, f), L.clampNumber(3.6, i), L.clampNumber(0, i), L.clampNumber("x", f), L.clampNumber(NaN, f), L.clampNumber(Infinity, i),
            L.clampNumber("1.2345", {kind: "float", min: 0, max: 5, step: 0.5})]))''')
        self.assertEqual(r, [2, 1, 0, 0, 7, 1.3, 2, 0.5, 4, 1, None, None, None, 1.23])          # 无穷大和 NaN 一样不是有效的数

    def test_signature_ignores_defaults_spaces_and_the_default_mode(self):
        r = js('''const a = app("01"), b = app("01");
          b.fields = {"3:speed": 1, "70:idea": "", "3:model": "m1"}; b.mode = "A 模式";                      // 全是默认值
          const c = app("01"); c.fields = {"70:idea": "橘猫  "}; const d = app("01"); d.fields = {"70:idea": "  橘猫"};
          const e = app("01"); e.files = {"4:image": {asset: 3}}; const f = app("01"); f.files = {"4:image": {asset: 3}}; const g = app("01"); g.files = {"4:image": {step: 1, n: 1}}; const h = app("01"); h.files = {"4:image": {n: 1, step: 1}};
          const i = app("01"); i.mode = "B 模式";
          console.log(JSON.stringify([L.stepSig(a) === L.stepSig(b), L.stepSig(c) === L.stepSig(d), L.stepSig(a) === L.stepSig(c), L.stepSig(e) === L.stepSig(f), L.stepSig(e) === L.stepSig(g), L.stepSig(g) === L.stepSig(h), L.stepSig(a) === L.stepSig(i), L.stepSig(a) === L.stepSig(app("02"))]))''')
        self.assertEqual(r, [True, True, False, True, False, True, False, False])

    def test_step_status_and_staleness(self):
        r = js('''const s1 = done(app("01"), {image: [1, 2]}), s2 = done(app("03")); s2.files = {"4:image": {step: 1, n: 1}}; s2.usedRuns = {1: 1}; s2.ran = L.stepParts(s2);
          const p = plan(s1, s2); const out = [];
          out.push(L.stepStatus(p, 0).kind, L.stepStatus(p, 1).kind);                                        // done done
          s1.fields = {"70:idea": "改了"}; out.push(L.stepStatus(p, 0).kind, L.stepStatus(p, 0).reason, L.stepStatus(p, 1).kind);   // 1 stale（设置改过），2 仍然 done（它自己没变、依赖也没重跑）
          s1.runs = 2; s1.ran = L.stepParts(s1); out.push(L.stepStatus(p, 0).kind, L.stepStatus(p, 1).kind, L.stepStatus(p, 1).reason);   // 1 重跑完了 → 2 stale
          s2.usedRuns = {1: 2}; out.push(L.stepStatus(p, 1).kind);
          const s3 = app("02"); out.push(L.stepStatus(plan(s3), 0).kind, L.needsRun(plan(s3), 0));
          for (const k of ["running", "failed", "stopped", "interrupted"]) { s3.state = k; out.push(L.stepStatus(plan(s3), 0).kind); }
          s3.state = "done"; delete s3.ran; out.push(L.stepStatus(plan(s3), 0).kind);                      // 跑过但没记签名：当过期，宁可重跑
          console.log(JSON.stringify(out))''')
        self.assertEqual(r, ["done", "done", "stale", "设置改过了", "done", "done", "stale", "步骤 1 重新跑过了", "done", "idle", True, "running", "failed", "stopped", "interrupted", "stale"])

    def test_next_to_run_and_pending_count(self):
        r = js('''const a = done(app("01")), b = app("02"), c = done(app("03")), d = app("04");
          const p = plan(a, b, c, d); b.state = "failed";
          console.log(JSON.stringify([L.nextToRun(p, -1), L.nextToRun(p, 1), L.nextToRun(p, 3), L.pendingCount(p), L.pendingCount(plan(a, c)), L.nextToRun(plan(a, c), -1)]))''')
        self.assertEqual(r, [1, 3, -1, 2, 0, -1])

    def test_resolving_the_files_a_step_needs(self):
        r = js('''const s1 = done(app("01"), {image: [2, 3], video: [4]}); const s2 = app("10", {produces: ["video"]});
          const assets = [{name: "助手/up.png", kind: "image"}, {name: "助手/r1.png", kind: "image"}, {name: "助手/r2.png", kind: "image"}, {name: "助手/v.mp4", kind: "video"}];
          const s3 = app("03"); s3.files = {"4:image": {step: 1, n: 2}, "5:audio": {asset: 1}};
          const p = plan(s1, s2, s3);
          const a = L.resolveStepFiles(p, assets, 2);
          s3.files = {"4:image": {step: 1, n: 9}}; const b = L.resolveStepFiles(p, assets, 2);                  // 第 9 个不存在
          s3.files = {"4:image": {step: 2, n: 1}}; const c = L.resolveStepFiles(p, assets, 2);                  // 步骤 2 没跑过
          s3.files = {"4:image": {step: 3}}; const d = L.resolveStepFiles(p, assets, 2);                        // 不能引用自己 / 后面的
          s3.files = {"4:image": {asset: 99}}; const e = L.resolveStepFiles(p, assets, 2);
          s3.files = {"4:image": {asset: 1}}; const f = L.resolveStepFiles(p, assets, 2);
          console.log(JSON.stringify([a, b, c, d, e, f]))''')
        self.assertEqual(r[0]["files"], {"4:image": "助手/r2.png", "5:audio": "助手/up.png"})        # 第 1 步产出的第 2 张图 = 素材 3 号 = r2；音频控件里放了素材 1（解析不检查类型，后端会）
        self.assertEqual(r[0]["deps"], [1])
        self.assertEqual((r[1]["files"], len(r[1]["missing"])), ({}, 1))
        self.assertIn("它还没有结果", r[2]["missing"][0])
        self.assertIn("不在它前面", r[3]["missing"][0])
        self.assertIn("不存在", r[4]["missing"][0])
        self.assertEqual((r[5]["files"], r[5]["missing"], r[5]["deps"]), ({"4:image": "助手/up.png"}, [], []))

    def test_required_files_block_the_step_until_they_are_given(self):
        r = js('''const st = app("03"); st.schema.files["4:image"].required = true;
          const p = plan(st); const a = L.missingRequired(st).map((f) => f.key), b = L.resolveStepFiles(p, [], 0);
          st.files = {"4:image": {asset: 1}}; const c = L.missingRequired(st).length, d = L.resolveStepFiles(p, [{name: "助手/a.png", kind: "image"}], 0);
          console.log(JSON.stringify([a, b.missing, c, d.missing, d.files, L.missingRequired({}), L.missingRequired({schema: {}})]))''')
        self.assertEqual(r[0], ["4:image"])
        self.assertEqual(len(r[1]), 1)
        self.assertIn("「商品图」必须有一个图片", r[1][0])
        self.assertEqual((r[2], r[3], r[4]), (0, [], {"4:image": "助手/a.png"}))
        self.assertEqual(r[5:], [[], []])

    def test_adopting_a_new_plan_keeps_the_results_of_unchanged_steps_only(self):
        r = js('''const mk = () => { const a = done(app("01"), {image: [1]}, 2), b = done(app("03"), {image: [2]}); b.files = {"4:image": {step: 1, n: 1}}; b.usedRuns = {1: 2}; b.ran = L.stepParts(b); return plan(a, b); };
          const dep = (s, n = 1) => Object.assign(app("03"), {files: {"4:image": {step: s, n}}});
          const out = (p) => p.steps.map((s, i) => [s.state, L.stepStatus(p, i).kind, s.runs]);
          // 1) 什么都没改：全部保留
          const n1 = L.adoptPlan(mk(), plan(app("01"), dep(1)));
          // 2) 只改第 2 步的设置：第 1 步保留，第 2 步重来
          const n2 = L.adoptPlan(mk(), plan(app("01"), Object.assign(dep(1), {fields: {"70:idea": "新要求"}})));
          // 3) 改第 1 步：第 1 步重来；第 2 步设置没变，但它依赖的第 1 步是新的（配不上）→ 也重来
          const n3 = L.adoptPlan(mk(), plan(Object.assign(app("01"), {fields: {"70:idea": "换个主体"}}), dep(1)));
          // 4) 在最前面插入一步，第 2 步改成引用「步骤 2」（还是原来那个第 1 步）：原来的两步都认得出来，结果保留，依赖序号跟着换
          const n4 = L.adoptPlan(mk(), plan(app("07"), app("01"), dep(2)));
          // 5) 同样是插入，但引用没跟着改（还写「步骤 1」，现在指向新插的那一步）→ 依赖变了，当新步骤
          const n5 = L.adoptPlan(mk(), plan(app("07"), app("01"), dep(1)));
          // 6) 引用的是同一步的另一个结果（第 2 个）→ 输入变了，当新步骤
          const n6 = L.adoptPlan(mk(), plan(app("01"), dep(1, 2)));
          // 7) 把第 1 步删了：第 2 步引用的步骤没了，当新步骤
          const n7 = L.adoptPlan(mk(), plan(Object.assign(app("03"), {files: {}})));
          console.log(JSON.stringify([out(n1), out(n2), out(n3), out(n4), n4.steps[2].usedRuns, n4.steps[2].ran.refs, n4.steps[2].files, out(n5), out(n6), out(n7)]))''')
        self.assertEqual(r[0], [["done", "done", 2], ["done", "done", 1]])
        self.assertEqual(r[1], [["done", "done", 2], ["idle", "idle", 0]])
        self.assertEqual(r[2], [["idle", "idle", 0], ["idle", "idle", 0]])
        self.assertEqual(r[3], [["idle", "idle", 0], ["done", "done", 2], ["done", "done", 1]])         # 插了一步：原来的两步还是「已完成」，不会白白重跑（视频要花额度）
        self.assertEqual(r[4], {"2": 2})                                                               # 依赖的序号跟着换：原来依赖第 1 步（跑过 2 次），现在依赖第 2 步
        self.assertEqual(r[5], {"4:image": "s2.1"})
        self.assertEqual(r[6], {"4:image": {"step": 2, "n": 1}})
        self.assertEqual(r[7], [["idle", "idle", 0], ["done", "done", 2], ["idle", "idle", 0]])
        self.assertEqual(r[8], [["done", "done", 2], ["idle", "idle", 0]])
        self.assertEqual(r[9], [["idle", "idle", 0]])

    def test_adopting_matches_steps_that_moved_and_same_looking_steps_in_order(self):
        r = js('''const a = done(app("01"), {image: [1]}, 3), b = done(app("01"), {image: [2]}, 5);          // 两个设置一模一样的步骤：按顺序一一对上，不会都认成同一个
          a.fields = {"70:idea": "A"}; b.fields = {"70:idea": "A"}; a.ran = L.stepParts(a); b.ran = L.stepParts(b);
          const c = done(app("02"), {image: [3]}, 7);
          const n = L.adoptPlan(plan(a, b, c), plan(Object.assign(app("02")), Object.assign(app("01"), {fields: {"70:idea": "A"}}), Object.assign(app("01"), {fields: {"70:idea": "A"}})));
          console.log(JSON.stringify(n.steps.map((s, i) => [s.workflow, L.stepStatus(n, i).kind, s.runs])))''')
        self.assertEqual(r, [["02", "done", 7], ["01", "done", 3], ["01", "done", 5]])

    def test_removing_a_step_renumbers_references(self):
        r = js('''const a = done(app("01"), {image: [1]}), b = app("02"), c = app("03"), d = app("04");
          c.files = {"4:image": {step: 1, n: 1}, "5:audio": {step: 2, n: 1}}; c.usedRuns = {1: 1, 2: 1};
          d.files = {"4:image": {step: 3, n: 2}}; d.usedRuns = {3: 1};
          const p = plan(a, b, c, d);
          const q = L.removeStep(p, 1);                                                                           // 删掉第 2 步
          console.log(JSON.stringify([q.steps.map((s) => s.workflow), q.steps[1].files, q.steps[1].usedRuns, q.steps[2].files, q.steps[2].usedRuns, p.steps.length, L.removeStep(plan(a), 0).steps.length]))''')
        self.assertEqual(r[0], ["01", "03", "04"])
        self.assertEqual(r[1], {"4:image": {"step": 1, "n": 1}})                      # 引用被删步骤的去掉
        self.assertEqual(r[2], {"1": 1})
        self.assertEqual(r[3], {"4:image": {"step": 2, "n": 2}})                      # 引用更靠后的：序号减 1
        self.assertEqual(r[4], {"2": 1})
        self.assertEqual((r[5], r[6]), (4, 0))

    def test_removing_a_step_does_not_make_the_steps_that_stay_look_changed(self):
        r = js('''const a = done(app("07", {produces: ["text"]})), b = done(app("04"), {image: [1, 2]}, 2), c = done(app("12", {produces: ["video"]}), {video: [3]}, 4);
          c.files = {"2:image": {step: 2, n: 2}}; c.usedRuns = {2: 2}; c.ran = L.stepParts(c);
          const p = plan(a, b, c);
          const kinds = (q) => q.steps.map((s, i) => L.stepStatus(q, i).kind);
          const before = kinds(p);
          const q = L.removeStep(p, 0);                                                                         // 删掉第 1 步（文案）：套图和视频都不该变成要重跑
          const after = kinds(q);
          const mid = JSON.parse(JSON.stringify([q.steps[1].files, q.steps[1].ran.refs, q.steps[1].usedRuns]));    // removeStep 会就地改步骤对象，下一步之前先拍下来
          const r2 = L.removeStep(q, 0);                                                                        // 再删掉套图（视频依赖它）：视频没了输入，要重跑
          console.log(JSON.stringify([before, after, mid[0], mid[1], mid[2], kinds(r2), r2.steps[0].files, r2.steps[0].ran.refs]))''')
        self.assertEqual(r[0], ["done", "done", "done"])
        self.assertEqual(r[1], ["done", "done"])
        self.assertEqual(r[2], {"2:image": {"step": 1, "n": 2}})
        self.assertEqual(r[3], {"2:image": "s1.2"})
        self.assertEqual(r[4], {"1": 2})
        self.assertEqual(r[5], ["stale"])                                                                       # 它用过的那一步被删了：输入变成「用默认」，结果就不能再算数
        self.assertEqual((r[6], r[7]), ({}, {"2:image": "gone"}))

    def test_append_step_has_a_cap_and_does_not_touch_the_old_plan(self):
        r = js('''const p = plan(app("01")); const q = L.appendStep(p, app("02"));
          let big = null; for (let i = 0; i < L.MAX_STEPS; i++) big = L.appendStep(big, app("0" + (i % 9 + 1)));
          console.log(JSON.stringify([p.steps.length, q.steps.length, big.steps.length, L.appendStep(big, app("01")), L.appendStep(null, app("01")).steps.length]))''')
        self.assertEqual(r, [1, 2, 6, None, 1])

    def test_file_choices_offer_earlier_steps_and_matching_assets(self):
        r = js('''const s1 = done(app("01"), {image: [2, 3]}), s2 = app("10", {produces: ["video"]}), s3 = app("03"), s0 = app("07", {produces: ["text"]});
          const assets = [{name: "助手/up.png", kind: "image", label: "我的商品图"}, {name: "助手/r1.png", kind: "image"}, {name: "助手/r2.png", kind: "image"}, {name: "助手/v.mp4", kind: "video"}, {name: "助手/m.mp3", kind: "audio"}];
          const p = plan(s0, s1, s2, s3);
          const img = L.fileChoices(p, assets, 3, "image"), vid = L.fileChoices(p, assets, 3, "video"), early = L.fileChoices(p, assets, 1, "image");
          console.log(JSON.stringify([img, vid.steps, vid.assets, early.steps, L.refToValue({step: 1, n: 2}), L.refToValue({asset: 3}), L.refToValue(undefined), L.refToValue({}),
            L.valueToRef("s:1:2"), L.valueToRef("a:3"), L.valueToRef(""), L.valueToRef("x:1"), L.valueToRef("s:2"), L.valueToRef(null)]))''')
        self.assertEqual([c["value"] for c in r[0]["steps"]], ["s:2:1", "s:2:2"])                    # 第 1 步（文案）不产出图；第 2 步出了 2 张
        self.assertEqual([c["value"] for c in r[0]["assets"]], ["a:1", "a:2", "a:3"])
        self.assertEqual(r[0]["assets"][0]["label"], "#1 我的商品图")
        self.assertEqual([c["value"] for c in r[1]], ["s:3:1"])                                       # 视频：第 3 步还没跑过，也给一个「运行后才有」的选项
        self.assertIn("运行后才有", r[1][0]["label"])
        self.assertEqual(r[2], [{"value": "a:4", "label": "#4 v.mp4"}])
        self.assertEqual(r[3], [])
        self.assertEqual(r[4:10], ["s:1:2", "a:3", "", "", {"step": 1, "n": 2}, {"asset": 3}])
        self.assertEqual(r[10:], [None, None, {"step": 2, "n": 1}, None])

    def test_payloads_do_not_carry_the_schema_or_results_or_share_objects(self):
        r = js('''const st = app("01"); st.mode = "B 模式"; st.fields = {"70:idea": "猫"}; st.files = {"4:image": {asset: 2}}; done(st);
          const a = L.runPayload(st, {"4:image": "助手/a.png"}, "cid"), b = L.serverPlan(plan(st)); a.fields["70:idea"] = "改了"; b.steps[0].fields.x = 1;
          console.log(JSON.stringify([a, b, st.fields, L.serverPlan(null), L.serverPlan({steps: []}), L.runPayload({workflow: "04"}, undefined, undefined)]))''')
        self.assertEqual(r[0], {"workflow": "01", "mode": "B 模式", "fields": {"70:idea": "改了"}, "files": {"4:image": "助手/a.png"}, "client_id": "cid"})
        self.assertEqual(r[1], {"steps": [{"workflow": "01", "mode": "B 模式", "fields": {"70:idea": "猫", "x": 1}, "files": {"4:image": {"asset": 2}}}]})
        self.assertEqual(r[2], {"70:idea": "猫"})                                       # 原方案没被改
        self.assertEqual((r[3], r[4]), (None, None))
        self.assertEqual(r[5], {"workflow": "04", "mode": None, "fields": {}, "files": {}, "client_id": ""})

    def test_snapshots_and_api_messages_speak_the_model_format(self):
        r = js('''const a = app("01"); a.mode = "B 模式"; a.fields = {"70:idea": "红苹果"}; a.note = "出图";
          const b = app("03"); b.files = {"4:image": {step: 1, n: 1}, "5:audio": {asset: 4}}; const c = app("04"); c.files = {"4:image": {step: 1, n: 3}}; c.mode = "不在选项里";
          const snap = L.snapshotPlan(plan(a, b, c));
          const msgs = [{role: "user", text: "你好", attachments: [2, 3]}, {role: "assistant", text: "好的", snap}, {role: "error", text: "出错了"}, {role: "note", text: "x"},
                        {role: "assistant", text: "   "}, {role: "assistant", text: "闲聊"}, {role: "user", text: ""}, {role: "user", text: "再亮一点", attachments: []}];
          console.log(JSON.stringify([snap, L.apiMessages(msgs), L.snapshotPlan(null), L.snapshotPlan({steps: []}), L.refForModel({step: 2, n: 1}), L.refForModel({step: 2, n: 3}), L.refForModel({asset: 5}), L.refForModel(null)]))''')
        self.assertEqual(r[0]["steps"][0], {"workflow": "01", "mode": 2, "fields": {"70:idea": "红苹果"}, "files": {}, "note": "出图"})
        self.assertEqual(r[0]["steps"][1]["files"], {"4:image": "步骤1", "5:audio": 4})
        self.assertEqual((r[0]["steps"][2]["files"], r[0]["steps"][2]["mode"]), ({"4:image": "步骤1.3"}, None))
        msgs = r[1]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "assistant", "user"])
        self.assertEqual(msgs[0]["content"], "你好\n（这条消息附带了素材库的 2 号、3 号）")
        self.assertEqual(json.loads(msgs[1]["content"]), {"reply": "好的", "plan": r[0]})
        self.assertEqual(json.loads(msgs[2]["content"]), {"reply": "闲聊", "plan": None})
        self.assertEqual(msgs[3]["content"], "再亮一点")
        self.assertEqual(r[2:], [None, None, "步骤2", "步骤2.3", 5, None])

    def test_cost_notes_and_summary(self):
        r = js('''const a = app("10", {cost: "Veo 每天约 3 个额度"}), b = app("03"); const p = plan(b, a);
          console.log(JSON.stringify([L.costNotes(p), L.planSummary(p), L.planSummary(null), L.costNotes(null)]))''')
        self.assertEqual(r[0], [{"index": 2, "name": "应用10", "text": "Veo 每天约 3 个额度"}])
        self.assertEqual((r[1], r[2], r[3]), ("1. 应用03 → 2. 应用10", "", []))

    def test_enter_to_send_ignores_ime_and_modifiers(self):
        r = js('''const k = (o) => L.isEnterToSend(Object.assign({key: "Enter", shiftKey: false, ctrlKey: false, metaKey: false, altKey: false, isComposing: false, keyCode: 13}, o));
          console.log(JSON.stringify([k({}), k({shiftKey: true}), k({isComposing: true}), k({keyCode: 229}), k({ctrlKey: true}), k({key: "a"}), k({metaKey: true})]))''')
        self.assertEqual(r, [True, False, False, False, False, False, False])

    def test_short_title_drops_the_star_prefix_and_truncates(self):
        r = js('console.log(JSON.stringify([L.shortTitle("★ 只填这里：商品信息（翻译模式：贴上要翻译的中文文案）"), L.shortTitle("阿里 写提示词"), L.shortTitle(""), L.shortTitle(null), L.shortTitle("abcdefghij", 5)]))')
        self.assertEqual(r, ["商品信息（翻译模式：贴上要翻…", "阿里 写提示词", "", "", "abcde…"])

    def test_elapsed_text(self):
        self.assertEqual(js('console.log(JSON.stringify([L.elapsedText(0), L.elapsedText(4600), L.elapsedText(59400), L.elapsedText(75000), L.elapsedText(-5)]))'),
                         ["0 秒", "5 秒", "59 秒", "1 分 15 秒", "0 秒"])

    def test_storage_round_trip_trims_and_keeps_only_runs_that_can_be_resumed(self):
        r = js('''const msgs = Array.from({length: 80}, (_, i) => ({id: "m" + i, role: "user", text: "第" + i + "句"}));
          const a = app("01"), b = app("03"), c = app("04"), d = done(app("07"));
          a.state = "running"; a.run = {started: 1};                                                   // 还没拿到 prompt_id：刷新后跟不上
          b.state = "running"; b.run = {started: 2, promptId: "p-9", outputs: [4], titles: {}, snap: {sig: "x", usedRuns: {}}};   // 已提交：刷新后接着等
          d.result.items = [{kind: "text", text: "长".repeat(L.MAX_STORED_TEXT + 500), file: {filename: "a.txt"}}];
          const st = {messages: msgs, assets: [{name: "助手/a.png", kind: "image", label: "图"}], plan: plan(a, b, c, d), chain: {mode: "all", cursor: 1}};
          const e = app("05"); e.state = "running"; e.run = {started: 3, runId: "r-7"};                       // 提交到一半（有 run_id 没有 prompt_id）：刷新后用同一个 run_id 再提交
          st.plan.steps.push(e);
          c.lostRunId = "r-lost-1";                                                                            // 提交时网络断了、不知道服务器收没收到：刷新后再试这一步还是用它
          const text = L.toStorage(st);
          const back = L.fromStorage(text);
          console.log(JSON.stringify([back.messages.length, back.messages[0].id, back.plan.steps.map((s) => s.state), back.plan.steps[1].run.promptId, back.plan.steps[0].run === undefined, back.chain,
            back.assets, back.plan.steps[3].result.items[0].text.length, st.plan.steps[0].state, JSON.parse(text).v,
            L.fromStorage("不是 json"), L.fromStorage('{"v": 1}'), L.fromStorage(null), L.fromStorage('{"v": 2, "messages": "x"}'), back.plan.steps[2].lostRunId]))''')
        self.assertEqual(r[0], 60)
        self.assertEqual(r[1], "m20")
        self.assertEqual(r[2], ["interrupted", "running", "idle", "done", "running"])
        self.assertEqual((r[3], r[4]), ("p-9", True))
        self.assertEqual(r[5], {"mode": "all", "cursor": 1, "stopped": False})
        self.assertEqual(r[6], [{"name": "助手/a.png", "kind": "image", "label": "图"}])
        self.assertEqual(r[7], 4000)                                                      # 存进浏览器的文字结果有上限
        self.assertEqual((r[8], r[9]), ("running", 2))                                    # 不改原状态对象
        self.assertEqual(r[10:14], [None, None, None, None])                              # 旧版本 / 坏数据一律当没有
        self.assertEqual(r[14], "r-lost-1")                                               # 网络断了没拿到回复的 run_id 刷新后还在

    def test_files_required_only_in_some_modes_follow_the_chosen_mode(self):
        """有的文件只在某个模式必填（16：背景图只在「放到背景图上」要）：必填的集合、缺的集合、界面上的「必填」标记都跟着当前模式走；存档往返不丢。"""
        r = js('''const st = app("16");
          st.schema.mode = { key: "77:mode", label: "模式", options: ["放到背景图上", "放到纯色背景上"], default: "放到背景图上" };
          st.schema.files = { "2:image": { key: "2:image", label: "商品", kind: "image", required: true },
                              "3:image": { key: "3:image", label: "背景图", kind: "image", required_modes: [1] },
                              "4:image": { key: "4:image", label: "没人要的", kind: "image" } };
          const keys = (x) => x.map((f) => f.key);
          const a = [keys(L.requiredFiles(st)), keys(L.missingRequired(st)), L.isRequired(st, st.schema.files["3:image"]), L.isRequired(st, st.schema.files["4:image"])];   // 默认模式 = 第 1 个
          st.mode = "放到纯色背景上";
          const b = [keys(L.requiredFiles(st)), keys(L.missingRequired(st)), L.isRequired(st, st.schema.files["3:image"])];
          st.mode = "放到背景图上"; st.files = { "2:image": { asset: 1 } };
          const c = keys(L.missingRequired(st)), d = L.resolveStepFiles(plan(st), [{ name: "助手/a.png", kind: "image" }], 0).missing;
          st.files = { "2:image": { asset: 1 }, "3:image": { asset: 1 } };
          const e = L.missingRequired(st).length;
          const back = L.fromStorage(L.toStorage({ messages: [], assets: [], plan: plan(st), chain: null })).plan.steps[0].schema.files;
          const noMode = app("04"); noMode.schema.mode = null; noMode.schema.files = { "9:image": { key: "9:image", label: "x", kind: "image", required_modes: [1] } };
          console.log(JSON.stringify([a, b, c, d.length, e, back["3:image"].required_modes, back["2:image"].required_modes === undefined, back["4:image"].required_modes === undefined, keys(L.requiredFiles(noMode)), keys(L.requiredFiles({}))]))''')
        self.assertEqual(r[0], [["2:image", "3:image"], ["2:image", "3:image"], True, False])
        self.assertEqual(r[1], [["2:image"], ["2:image"], False])                                 # 纯色模式：背景图不必填
        self.assertEqual(r[2], ["3:image"])                                                       # 商品给了，背景图模式下还缺背景图
        self.assertEqual(r[3], 1)                                                                 # 缺的文件会列进「不能运行」的提示里
        self.assertEqual(r[4], 0)
        self.assertEqual(r[5:8], [[1], True, True])                                               # 存档往返：只在某些模式必填的信息还在，没有的不会凭空多出来
        self.assertEqual(r[8:], [[], []])                                                         # 没有模式的应用 / 空步骤：不会因为 required_modes 误报

    def test_option_labels_survive_storage_and_bad_ones_are_dropped(self):
        """下拉的选项说明（音色：名字 → 名字 · 性别 · 特点）：存进浏览器再读出来还在；不属于选项的 / 不是文字的丢掉。"""
        r = js('''const f = {key: "3:voice", label: "音色", kind: "choice", options: ["Cherry", "Ethan"], default: "Cherry",
                  labels: {Cherry: "Cherry · 女 · 阳光亲切", Ethan: 5, Nobody: "x", Ghost: ""}};
          const g = {key: "3:v2", label: "无说明", kind: "choice", options: ["a", "b"], default: "a", labels: "坏的"};
          const text = JSON.stringify({v: 2, messages: [], plan: {steps: [{workflow: "08", name: "配音", schema: {fields: {"3:voice": f, "3:v2": g}}}]}});
          const sc = L.fromStorage(text).plan.steps[0].schema.fields;
          console.log(JSON.stringify([sc["3:voice"].labels, "labels" in sc["3:v2"]]))''')
        self.assertEqual(r, [{"Cherry": "Cherry · 女 · 阳光亲切"}, False])

    def test_single_line_text_flag_survives_storage_and_only_false_counts(self):
        """文字控件标了「单行」（角色名、颜色）：存进浏览器再读出来还在；没标的 / 标成别的值的都当多行，别的类型的控件不带这个标记。"""
        r = js('''const f = (key, extra) => Object.assign({key, label: key, kind: "text", default: "x"}, extra);
          const text = JSON.stringify({v: 2, messages: [], plan: {steps: [{workflow: "13", name: "多角色配音", schema: {fields: {
            "3:a": f("3:a", {multiline: false}), "3:b": f("3:b"), "3:c": f("3:c", {multiline: true}), "3:d": f("3:d", {multiline: "false"}),
            "3:e": {key: "3:e", label: "e", kind: "bool", default: true, multiline: false}}}}]}});
          const sc = L.fromStorage(text).plan.steps[0].schema.fields;
          console.log(JSON.stringify(["3:a", "3:b", "3:c", "3:d", "3:e"].map((k) => sc[k].multiline === false)))''')
        self.assertEqual(r, [True, False, False, False, False])

    def test_storage_repairs_odd_shapes(self):
        r = js('''const text = JSON.stringify({v: 2, messages: [], assets: [{name: 5}, null, {name: "助手/x.mp4"}, {name: "助手/y.png", kind: "image", label: 7}],
            plan: {steps: [null, {workflow: ""}, {workflow: "01", fields: "坏的", files: null, state: "怪状态", runs: "x", usedRuns: 3, lostRunId: 5}, {workflow: "02", schema: "坏的"}], note: ""}, chain: "坏的"});
          const b = L.fromStorage(text);
          console.log(JSON.stringify([b.assets, b.plan.steps.length, b.plan.steps[0].fields, b.plan.steps[0].files, b.plan.steps[0].state, b.plan.steps[0].runs, b.plan.steps[0].usedRuns,
            b.plan.steps[1].schema, b.chain, L.fromStorage(JSON.stringify({v: 2, messages: [], plan: {steps: []}})).plan, b.plan.steps[0].lostRunId]))''')
        self.assertEqual(r[0], [{"name": "", "kind": "other", "label": ""}, {"name": "", "kind": "other", "label": ""},          # 坏条目留空位：编号是位置，不能让后面的编号前移
                                {"name": "助手/x.mp4", "kind": "video", "label": ""}, {"name": "助手/y.png", "kind": "image", "label": "7"}])
        self.assertEqual(r[1:7], [2, {}, {}, "idle", 0, {}])
        self.assertEqual(r[7], {"mode": None, "fields": {}, "files": {}, "outputs": [], "results": [], "produces": [], "cost": "", "desc": "", "titles": {}})
        self.assertIsNone(r[8])
        self.assertIsNone(r[9])
        self.assertIsNone(r[10])                                                                # 不是文字的 lostRunId 丢掉

    def test_storage_drops_broken_parts_instead_of_crashing_the_whole_workbench(self):
        r = js('''const good = {workflow: "01", name: "文生图", fields: {"70:idea": "猫", "x": {"a": 1}, "y": NaN, "z": null}, files: {"4:image": {asset: 2}, "5:audio": {step: "x"}, "6:image": {step: 1}},
            schema: {fields: {"70:idea": {key: "70:idea", kind: "text", default: 5}, "bad": {kind: "text"}, "c": {key: "c", kind: "choice"}, "c2": {key: "c2", kind: "choice", options: ["a", 2], default: 9},
                              "n": {key: "n", kind: "float", min: "x", max: 5, step: -1, default: "y"}, "k": {key: "k", kind: "weird"}},
                     files: {"4:image": {key: "4:image", kind: "image", label: "图", required: 1}, "x": {key: "x", kind: "text"}}, mode: {key: "77:mode", options: []}},
            result: {items: [null, 5, {kind: "image"}, {kind: "image", file: {}}, {kind: "image", file: {filename: "a.png"}, asset: 3.5}, {kind: "text", file: {filename: "t.txt"}, text: 5, label: 7, isError: 1}], error: 5, warnings: [1, "w"], elapsed: "x", byKind: {image: [1, "a", -2, 3]}},
            run: {started: "x"}, ran: {base: 5}, state: "running"};
          const text = JSON.stringify({v: 2, messages: [null, 5, "x", {role: "weird", text: "a"}, {role: "user", text: 5, attachments: [1, "a", -3, 2.5], warnings: "w"}, {role: "assistant", text: "好", snap: 5, planText: 7}],
                                       assets: [], plan: {note: 5, steps: [good]}, chain: {mode: "weird"}});
          const b = L.fromStorage(text), st = b.plan.steps[0];
          console.log(JSON.stringify([b.messages, st.fields, st.files, st.state, Object.keys(st.schema.fields), st.schema.fields["70:idea"], st.schema.fields["c2"], st.schema.fields["n"],
            st.schema.files, st.schema.mode, st.result.items, st.result.byKind, st.result.error, st.result.elapsed, st.run, st.ran, b.chain, b.plan.note,
            L.fromStorage(JSON.stringify({v: 2, messages: [], plan: {steps: [null, 5]}})).plan]))''')
        self.assertEqual([m["role"] for m in r[0]], ["user", "assistant"])                                   # 不认识的消息直接丢
        self.assertEqual(r[0][0]["attachments"], [1])
        self.assertEqual((r[0][0]["text"], r[0][0]["warnings"], r[0][1]["snap"], r[0][1]["planText"]), ("5", [], None, "7"))
        self.assertEqual(r[1], {"70:idea": "猫"})                                                            # 值只留文字 / 数字 / 开关
        self.assertEqual(r[2], {"4:image": {"asset": 2}, "6:image": {"step": 1, "n": 1}})
        self.assertEqual(r[3], "interrupted")                                                                # 运行中但 run 不完整：当作中断
        self.assertEqual(r[4], ["70:idea", "c2", "n"])                                                       # 缺 key / 没有选项 / 类型不认识的设置项丢掉
        self.assertEqual(r[5]["default"], "5")
        self.assertEqual((r[6]["options"], r[6]["default"]), (["a", "2"], "a"))
        self.assertEqual((r[7]["min"], r[7]["max"], r[7]["step"], r[7]["default"]), (None, 5, None, 0))
        self.assertEqual(list(r[8]), ["4:image"])
        self.assertTrue(r[8]["4:image"]["required"])
        self.assertIsNone(r[9])
        self.assertEqual([(i["kind"], i["file"]["filename"], i.get("asset"), i.get("text")) for i in r[10]], [("image", "a.png", None, None), ("text", "t.txt", None, None)])
        self.assertEqual(r[11], {"image": [1, 3], "video": [], "audio": []})
        self.assertEqual((r[12], r[13]), ("5", 0))
        self.assertIsNone(r[14])
        self.assertIsNone(r[15])
        self.assertIsNone(r[16])
        self.assertEqual(r[17], "5")
        self.assertIsNone(r[18])

    def test_dropdown_values_are_text_everywhere(self):
        r = js('''const f = {key: "8:fps", kind: "choice", options: ["24", "30"], default: "24", cast: "int"};
          const st = app("11"); st.schema.fields["8:fps"] = f;
          L.setField(st, f, 30); const a = {...st.fields};                                  // 数字 30 也存成文字 "30"
          L.setField(st, f, "24"); const b = {...st.fields};                                // 回到默认：去掉
          st.fields = {"8:fps": 30}; const p1 = L.stepParts(st).base;
          st.fields = {"8:fps": "30"}; const p2 = L.stepParts(st).base;
          st.fields = {"8:fps": 24}; const p3 = L.stepParts(st).base;                       // 数字 24 = 默认「24」：没改
          st.fields = {}; const p4 = L.stepParts(st).base;
          console.log(JSON.stringify([a, b, p1 === p2, p3 === p4, p1 === p4, L.runPayload(st, {}, "c", "r-1"), L.runPayload(st, {}, "c"), L.runPayload(st, {}, "c", "")]))''')
        self.assertEqual((r[0], r[1], r[2], r[3], r[4]), ({"8:fps": "30"}, {}, True, True, False))
        self.assertEqual(r[5]["run_id"], "r-1")
        self.assertNotIn("run_id", r[6])
        self.assertNotIn("run_id", r[7])

    def test_frontend_and_backend_agree_on_the_limits(self):
        with open(LIB, encoding="utf-8") as f:
            src = f.read()
        with open(os.path.join(ROOT, "custom-nodes", "pro-chat", "chat.py"), encoding="utf-8") as f:
            py = f.read()
        for name in ("MAX_ASSETS", "MAX_STEPS"):
            m = re.search(rf"export const {name} = (\d+)", src)
            self.assertIsNotNone(m, name)
            self.assertEqual(int(m.group(1)), int(re.search(rf"^{name} = (\d+)", py, re.M).group(1)), name)


class Static(unittest.TestCase):
    UI_FILES = ("pro-chat.js", "pro-chat-ui.js")

    def read(self, name):
        with open(os.path.join(WEB, name), encoding="utf-8") as f:
            return f.read()

    @unittest.skipUnless(NODE, "没有 node")
    def test_every_lib_function_used_by_the_ui_exists(self):
        exported = set(js('console.log(JSON.stringify(Object.keys(L)))'))
        for name in self.UI_FILES:
            used = set(re.findall(r"(?<![\w-])lib\.(\w+)", self.read(name)))      # 排除 "pro-chat-lib.js" 这类文件名
            self.assertTrue(used, f"{name} 没用 lib？")
            self.assertEqual(used - exported, set(), f"{name} 用了库里没有的函数")

    def test_model_output_is_never_written_as_html(self):
        for name in (*self.UI_FILES, "pro-chat-lib.js"):
            src = self.read(name)
            for bad in (".innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval(", "new Function"):
                self.assertNotIn(bad, src, f"{name} 里有 {bad}：模型 / 用户的文字不能当 HTML")

    def test_ui_script_only_talks_to_our_routes_and_comfy_apis(self):
        src = self.read("pro-chat.js")
        routes = set(re.findall(r'(?:post|fetchApi)\(\s*[`"\'](/[^`"\'$?]*)', src))
        self.assertEqual(routes, {"/pro/chat", "/pro/run", "/pro/stage", "/pro/catalog", "/interrupt", "/upload/image", "/history/", "/queue", "/userdata/"})
        for name in self.UI_FILES:
            text = self.read(name)
            self.assertNotIn("http://", text.replace("http://127", ""), name)
            self.assertNotIn("https://", text, name)
        self.assertNotIn("fetch(", self.read("pro-chat-ui.js").replace("//", ""), "界面文件自己不发请求")

    def test_drop_and_paste_into_the_workbench_do_not_leak_to_other_handlers(self):
        src = self.read("pro-chat.js")
        for name in ("onDrop", "onPaste"):
            body = re.search(r"function %s\(e\) \{(.*?)\n\}" % name, src, re.S)
            self.assertIsNotNone(body, name)
            self.assertIn("stopPropagation()", body.group(1), f"{name} 要 stopPropagation：文件只给工作台用")
        self.assertIn("e.stopPropagation(); root.classList.add", self.read("pro-chat-ui.js"))            # dragover 也一样

    def test_keyboard_events_stay_inside_the_workbench(self):
        """工作台盖在 ComfyUI 上面：按键不能冒泡出去，否则 Delete 会删掉后面画布上选中的节点、Ctrl+Enter 会把后面的工作流排进队列。"""
        src = self.read("pro-chat-ui.js")
        m = re.search(r'for \(const t of \[([^\]]*)\]\) root\.addEventListener\(t, \(e\) => e\.stopPropagation\(\)\)', src)
        self.assertIsNotNone(m)
        self.assertEqual(set(re.findall(r'"(\w+)"', m.group(1))), {"keydown", "keyup", "keypress"})

    def test_lib_has_no_side_effects_and_no_imports(self):
        src = self.read("pro-chat-lib.js")
        self.assertIsNone(re.search(r"^\s*import\s", src, re.M))
        self.assertNotIn("document.", src)
        self.assertNotIn("window.", src)

    def test_ui_module_does_not_import_comfyui(self):
        """界面文件不依赖 ComfyUI 的页面对象，所以能放进假页面里单独跑（tests/ui_harness）。"""
        src = self.read("pro-chat-ui.js")
        self.assertEqual(re.findall(r'^import .* from "([^"]+)"', src, re.M), ["./pro-chat-lib.js"])

    def test_no_files_left_over_from_the_old_sidebar_ui(self):
        for name in (*self.UI_FILES, "pro-chat-lib.js"):
            src = self.read(name)
            for old in ("proChat.v1", "lib.inputImagePath", "lib.runPayload(m.plan", "planState"):
                self.assertNotIn(old, src, f"{name} 里还有旧版侧栏聊天的残留：{old}")


if __name__ == "__main__":
    unittest.main()
