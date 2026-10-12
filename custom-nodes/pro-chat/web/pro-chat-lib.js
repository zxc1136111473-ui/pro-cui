// AI 工作台前端的纯函数：不依赖 ComfyUI 的页面对象，也不碰 DOM，tests/test_chat_js.py 用 node 直接测。
// ComfyUI 会把 web/ 下的每个 .js 都当扩展加载；这个文件只导出函数，加载它没有任何副作用。
//
// 数据模型：
//   素材库 assets = [{name, kind, label}]：name 是 input 里的相对路径（"助手/xxx.png"），编号 = 下标 + 1，只增不减（清空才重置）；
//   方案 plan = {steps: [step], note}；step = {workflow, name, path, mode, fields, files, note, schema, state, run, result, runs, ranSig, usedRuns}：
//     files 的值是引用：{asset: 3}（素材库 3 号）或 {step: 1, n: 2}（第 1 步产出的第 2 个同类型文件）；运行时才换成真实文件名；
//     state: idle / running / done / failed / stopped / interrupted；result = {items, error, warnings, elapsed, byKind}（byKind = 各类结果在素材库里的编号）；
//     runs = 成功跑完的次数，usedRuns = 上次运行时各依赖步骤的 runs，ran = 上次运行时的设置（stepParts 的结果）：三者一起判断「设置改了 / 前面的步骤重跑过」要不要重跑。

const EXT = {
  image: ["png", "jpg", "jpeg", "webp", "gif", "bmp"],
  video: ["mp4", "webm", "mov", "mkv"],
  audio: ["mp3", "wav", "flac", "ogg", "m4a", "aac"],
  text: ["txt", "md", "json", "csv"],
};

export const ERROR_LABEL = "出错信息";   // 应用里「只在出错时出现」的文字结果，标题都以它开头
export const MAX_STORED_MESSAGES = 60;
export const MAX_ASSETS = 200;          // 素材库最多放这么多，和后端 chat.MAX_ASSETS 一致（tests 里核对）
export const MAX_STEPS = 6;             // 一个方案最多几步，和后端 chat.MAX_STEPS 一致（tests 里核对）
export const MAX_STORED_TEXT = 4000;    // 存进浏览器的单条文字结果最多这么长
export const STORE_VERSION = 2;
export const KIND_LABEL = { image: "图片", video: "视频", audio: "音频", text: "文字", other: "文件" };
export const MEDIA_KINDS = ["image", "video", "audio"];

export function fileKind(name) {
  const m = /\.([A-Za-z0-9]+)$/.exec(String(name || ""));
  const ext = m ? m[1].toLowerCase() : "";
  for (const [kind, list] of Object.entries(EXT)) if (list.includes(ext)) return kind;
  return "other";
}

export function baseName(name) {
  const s = String(name || "");
  return s.slice(s.lastIndexOf("/") + 1);
}

// /view 的相对地址（外面再用 api.apiURL 加上前缀）
export function viewPath(file) {
  const q = new URLSearchParams({ filename: file.filename, type: file.type || "output", subfolder: file.subfolder || "" });
  return "/view?" + q.toString();
}

// input 里的素材 "助手/a.png" → /view 的相对地址
export function inputPath(name) {
  const i = String(name).lastIndexOf("/");
  return viewPath({ filename: i < 0 ? name : name.slice(i + 1), subfolder: i < 0 ? "" : name.slice(0, i), type: "input" });
}

// ── 一次运行的结果 ─────────────────────────────────────────────────────────────────────────────────────
// 一次运行的 /history 记录 → 要显示的结果项（按应用登记的结果节点顺序）。
// 返回 [{kind: image|video|audio|text, file, label, isError}]
export function historyItems(entry, outputIds) {
  const items = [];
  const outs = (entry && entry.outputs) || {};
  for (const id of outputIds || []) {
    const o = outs[String(id)];
    if (!o) continue;
    for (const f of o.images || []) {
      const kind = fileKind(f.filename);
      items.push({ kind: kind === "video" ? "video" : "image", file: f, label: "", isError: false });
    }
    for (const f of o.audio || []) items.push({ kind: "audio", file: f, label: "", isError: false });
    for (const f of o.video || []) items.push({ kind: "video", file: f, label: "", isError: false });
    for (const f of o.files || []) {
      const kind = fileKind(f.filename);
      const label = f.display_name || "";
      if (kind === "text") items.push({ kind: "text", file: f, label, isError: label.startsWith(ERROR_LABEL) });
      else if (kind !== "other") items.push({ kind, file: f, label, isError: false });
    }
  }
  return items;
}

// 运行出错（节点抛异常）时的人话；没出错返回 ""
export function historyError(entry, titles) {
  const st = entry && entry.status;
  if (!st || st.status_str !== "error") return "";
  for (const m of st.messages || []) {
    if (m[0] === "execution_error" && m[1]) {
      const d = m[1];
      const title = (titles || {})[String(d.node_id)] || d.node_type || "";
      return `${title ? "「" + title + "」" : ""}${d.exception_message || "运行出错"}`.slice(0, 600);
    }
  }
  return "运行出错了";
}

export function isFinished(entry) {
  const st = entry && entry.status;
  return !!st && (st.completed === true || st.status_str === "error" || st.status_str === "success");
}

// 这次运行是被人中断的（点了「停止」，或者在 ComfyUI 里取消）：历史记录里没有 execution_error，只有 execution_interrupted
export function wasInterrupted(entry) {
  const msgs = (entry && entry.status && entry.status.messages) || [];
  return msgs.some((m) => m[0] === "execution_interrupted");
}

// 一次运行算成功还是失败：节点抛了异常且什么都没产出 → 失败；应用里的「出错信息」文字出现了、而且没有任何图片 / 视频 / 音频 → 失败
// （比如音乐的 Suno 渠道出错：工作流本身「成功」跑完了，只是产出里只有一段出错文字）；其余算成功（部分出错的会在结果里用红字列出）
export function runOutcome(items, error) {
  if (error && !items.length) return "failed";
  const media = items.some((i) => i.kind !== "text");
  return items.some((i) => i.isError) && !media ? "failed" : "done";
}

// /queue 的返回里还有没有这个 prompt（排队中或正在跑）
export function inQueue(queue, promptId) {
  const all = [...((queue && queue.queue_running) || []), ...((queue && queue.queue_pending) || [])];
  return all.some((it) => Array.isArray(it) && it[1] === promptId);
}

// 结果项里值得放进素材库的：图片 / 视频 / 音频文件，而且在输出目录里（后端只认 output）
export function stageable(items) {
  return (items || []).filter((it) => MEDIA_KINDS.includes(it.kind) && it.file && (it.file.type || "output") === "output");
}

// ── 素材库 ────────────────────────────────────────────────────────────────────────────────────────────
// 加一个素材。同名文件不重复占编号（内容相同的文件上传 / 中转后名字相同）；满了返回 n = 0。返回 {n, added}。
export function addAsset(assets, item) {
  const i = assets.findIndex((a) => a.name === item.name);
  if (i >= 0) return { n: i + 1, added: false };
  if (assets.length >= MAX_ASSETS) return { n: 0, added: false };
  assets.push({ name: item.name, kind: item.kind || fileKind(item.name), label: String(item.label || "").slice(0, 60) });
  return { n: assets.length, added: true };
}

export function assetLabel(a) {
  return a.label || baseName(a.name);
}

// 步骤 i（0 起）产出的结果在素材库里的编号：{image: [n…], video: [n…], audio: [n…]}
export function stepOutputs(step) {
  const b = (step && step.result && step.result.byKind) || {};
  return { image: b.image || [], video: b.video || [], audio: b.audio || [] };
}

// ── 方案：设置 ─────────────────────────────────────────────────────────────────────────────────────────
const same = (a, b) => (typeof a === "number" && typeof b === "number" ? Math.abs(a - b) < 1e-9 : a === b);

export function fieldValue(step, f) {
  return step.fields && f.key in step.fields ? step.fields[f.key] : f.default;
}

// 改一个设置：和默认值一样（或文字被清空）就去掉覆盖，方案里只留用户真改过的
export function setField(step, f, value) {
  step.fields = step.fields || {};
  const empty = value === undefined || value === null || (f.kind === "text" && !String(value).trim());
  const v = !empty && f.kind === "choice" ? String(value) : value;          // 下拉的值一律是文字（数字下拉的选项原值是数字，运行时由后端转回去）
  if (empty || same(v, f.default)) delete step.fields[f.key];
  else step.fields[f.key] = v;
}

export function currentMode(step) {
  const m = step.schema && step.schema.mode;
  return step.mode || (m ? m.default : null);
}

// 步骤的设置拆成两部分：base = 应用、模式、真正改过的设置、素材库里的文件；refs = 引用「前面某一步的结果」的文件（值是 "s序号.第几个"）。
// 拆开是因为步骤序号会变（删了前面的步骤、AI 在前面插了一步）：序号变了、指向的还是同一个步骤时不算「设置改了」，所以 refs 要能跟着序号一起换（removeStep / adoptPlan）。
export function stepParts(step) {
  const sc = step.schema || {};
  const eff = {}, assetFiles = {}, refs = {};
  for (const [k, v0] of Object.entries(step.fields || {})) {
    const f = (sc.fields || {})[k];
    const text = f && f.kind === "choice";                       // 下拉的值一律当文字比（后端里数字下拉的选项原值是数字，方案里统一成文字）
    const v = typeof v0 === "string" ? v0.trim() : text ? String(v0) : v0;
    if (!f || !same(v, text ? String(f.default) : f.default)) eff[k] = v;
  }
  for (const [k, ref] of Object.entries(step.files || {})) {
    if (ref && ref.step) refs[k] = `s${ref.step}.${ref.n || 1}`;
    else assetFiles[k] = ref && ref.asset ? `a${ref.asset}` : "";
  }
  const sorted = (o) => Object.keys(o).sort().map((k) => [k, o[k]]);
  return { base: JSON.stringify([step.workflow, currentMode(step), sorted(eff), sorted(assetFiles)]), refs };
}

const refsKey = (refs) => JSON.stringify(Object.keys(refs || {}).sort().map((k) => [k, refs[k]]));

// 整体签名（两个步骤签名相同 = 跑出来的东西一样，除了随机种子；序号相关，只用来快速比较）
export function stepSig(step) {
  const p = stepParts(step);
  return JSON.stringify([p.base, refsKey(p.refs)]);
}

export function newStep(app) {
  return { workflow: app.workflow, name: app.name, path: app.path || "", mode: null, fields: {}, files: {}, note: "", schema: app.schema, state: "idle", runs: 0 };
}

// 方案 → /pro/chat 的「当前方案」：只带设置，不带结果和 schema
export function serverPlan(plan) {
  if (!plan || !plan.steps || !plan.steps.length) return null;
  return { steps: plan.steps.map((s) => ({ workflow: s.workflow, mode: s.mode || null, fields: { ...(s.fields || {}) }, files: { ...(s.files || {}) } })) };
}

// 文件引用 → 模型读的写法：素材库编号（数字）/ "步骤N" / "步骤N.M"
export function refForModel(ref) {
  if (ref && ref.step) return `步骤${ref.step}` + (ref.n && ref.n !== 1 ? `.${ref.n}` : "");
  return ref && ref.asset ? ref.asset : null;
}

// 方案 → 模型自己该输出的那种 JSON（存在助手消息里，下一轮对话当历史发回去）：模式换成编号、引用换成编号 / 「步骤N」
export function snapshotPlan(plan) {
  if (!plan || !plan.steps || !plan.steps.length) return null;
  return {
    steps: plan.steps.map((s) => {
      const opts = (s.schema && s.schema.mode && s.schema.mode.options) || [];
      const mi = s.mode ? opts.indexOf(s.mode) + 1 : 0;
      const files = {};
      for (const [k, ref] of Object.entries(s.files || {})) { const r = refForModel(ref); if (r != null) files[k] = r; }
      return { workflow: s.workflow, mode: mi > 0 ? mi : null, fields: { ...(s.fields || {}) }, files, note: s.note || "" };
    }),
    note: plan.note || "",
  };
}

// 界面里的消息 → 发给模型的对话。助手那几条还原成它自己该输出的 JSON 格式（只带文字回复和方案），
// 这样它多轮之后仍然按格式回复，也记得自己当时给的方案；用户那几条带上「附了哪几号素材」。
export function apiMessages(messages) {
  const out = [];
  for (const m of messages) {
    if (m.role === "user" && String(m.text || "").trim()) {
      const att = (m.attachments || []).filter((n) => n > 0);
      out.push({ role: "user", content: m.text + (att.length ? `\n（这条消息附带了素材库的 ${att.map((n) => n + " 号").join("、")}）` : "") });
    } else if (m.role === "assistant" && String(m.text || "").trim()) {
      out.push({ role: "assistant", content: JSON.stringify({ reply: m.text, plan: m.snap || null }) });
    }
  }
  return out;
}

// ── 方案：运行状态、过期判断 ───────────────────────────────────────────────────────────────────────────
// 步骤依赖哪些前面的步骤（序号，从 1 数）
export function depsOf(step) {
  const s = new Set();
  for (const ref of Object.values(step.files || {})) if (ref && ref.step) s.add(ref.step);
  return [...s].sort((a, b) => a - b);
}

// 步骤现在的状态：{kind, reason}
//   kind: idle（没运行过）/ running / done / stale（跑过，但设置改了或前面的步骤重跑过）/ failed / stopped / interrupted
export function stepStatus(plan, i) {
  const st = plan.steps[i];
  if (st.state === "running") return { kind: "running", reason: "" };
  if (st.state === "failed" || st.state === "stopped" || st.state === "interrupted") return { kind: st.state, reason: "" };
  if (st.state !== "done") return { kind: "idle", reason: "" };
  const cur = stepParts(st);
  if (!st.ran || st.ran.base !== cur.base || refsKey(st.ran.refs) !== refsKey(cur.refs)) return { kind: "stale", reason: "设置改过了" };
  for (const d of depsOf(st)) {
    const up = plan.steps[d - 1];
    if (!up || (st.usedRuns || {})[d] !== (up.runs || 0)) return { kind: "stale", reason: `步骤 ${d} 重新跑过了` };
  }
  return { kind: "done", reason: "" };
}

export function needsRun(plan, i) {
  const k = stepStatus(plan, i).kind;
  return k !== "done" && k !== "running";
}

// 排在 after（下标，-1 = 从头）后面的、第一个需要运行的步骤；没有返回 -1
export function nextToRun(plan, after) {
  for (let i = after + 1; i < plan.steps.length; i++) if (needsRun(plan, i)) return i;
  return -1;
}

export function pendingCount(plan) {
  return plan.steps.reduce((n, _, i) => n + (needsRun(plan, i) ? 1 : 0), 0);
}

// 这一步在当前模式下必填的文件控件：不管选哪个模式都必须有的（比如商品图）+ 只在这个模式必须有的（比如「放到背景图上」模式要背景图，纯色模式不要）
export function requiredFiles(step) {
  const sc = step.schema || {};
  const n = sc.mode && Array.isArray(sc.mode.options) ? sc.mode.options.indexOf(currentMode(step)) + 1 : 0;
  return Object.values(sc.files || {}).filter((f) => f.required || (n > 0 && Array.isArray(f.required_modes) && f.required_modes.includes(n)));
}

export const isRequired = (step, f) => requiredFiles(step).some((x) => x.key === f.key);

// 这一步里必填、但还没选的文件控件
export function missingRequired(step) {
  return requiredFiles(step).filter((f) => !(step.files && step.files[f.key]));
}

// 步骤 i 运行要用的文件：{files: {控件键: 文件名}, missing: [人话], deps: [前面步骤的序号]}
export function resolveStepFiles(plan, assets, i) {
  const step = plan.steps[i];
  const files = {}, missing = [], deps = new Set();
  for (const f of missingRequired(step)) missing.push(`「${f.label}」必须有一个${KIND_LABEL[f.kind]}（点「上传…」选一个，或选前面步骤做出来的）`);
  for (const [key, ref] of Object.entries(step.files || {})) {
    const f = ((step.schema || {}).files || {})[key];
    const label = f ? f.label : key;
    const kind = f ? f.kind : "image";
    if (ref && ref.step) {
      const up = ref.step >= 1 && ref.step <= i ? plan.steps[ref.step - 1] : null;
      const n = up ? stepOutputs(up)[kind][(ref.n || 1) - 1] : 0;
      const a = n ? assets[n - 1] : null;
      if (a && a.name) { files[key] = a.name; deps.add(ref.step); }
      else missing.push(up ? `「${label}」要用步骤 ${ref.step} 做出的${KIND_LABEL[kind]}，它还没有结果（先运行步骤 ${ref.step}）` : `「${label}」引用的步骤 ${ref.step} 不在它前面`);
    } else if (ref && ref.asset) {
      const a = assets[ref.asset - 1];
      if (a && a.name) files[key] = a.name;
      else missing.push(`「${label}」用的素材 ${ref.asset} 号不存在`);
    }
  }
  return { files, missing, deps: [...deps].sort((a, b) => a - b) };
}

// 方案 → /pro/run 的请求体（后端会再按目录校验一遍）。runId：这一次运行的编号，后端同一个编号只会真的提交一次（刷新 / 重试不会重复提交）
export function runPayload(step, files, clientId, runId) {
  const p = { workflow: step.workflow, mode: step.mode || null, fields: { ...(step.fields || {}) }, files: { ...(files || {}) }, client_id: clientId || "" };
  if (runId) p.run_id = runId;
  return p;
}

// 步骤开始运行时记下的东西：当时的设置、依赖步骤当时的运行次数（结束后写进 ran / usedRuns）
export function runSnapshot(plan, i, deps) {
  const usedRuns = {};
  for (const d of deps) usedRuns[d] = plan.steps[d - 1].runs || 0;
  return { parts: stepParts(plan.steps[i]), usedRuns };
}

// 把 refs 里的步骤序号按 map（旧序号 → 新序号）换掉；对应的步骤不在 map 里（被删了）就换成 "gone"，永远对不上，会被判成「设置改过了」
function remapRefs(refs, map) {
  const out = {};
  for (const [k, v] of Object.entries(refs || {})) {
    const m = /^s(\d+)\.(\d+)$/.exec(v);
    out[k] = m && map[Number(m[1])] ? `s${map[Number(m[1])]}.${m[2]}` : "gone";
  }
  return out;
}

// 新方案接手旧方案的运行结果：「同一个步骤」保留状态和结果（改哪步只重跑哪步），变了的重来。
// 同一个步骤 = 应用、模式、设置、素材库里的文件都一样，并且引用的前面步骤也是配对上的那一个（序号可以变：前面插了一步、删了一步都认得出来）。
// 设置一模一样的步骤按先后顺序一一配对（旧的第一个对新的第一个）。会修改 newPlan.steps，返回 newPlan。
export function adoptPlan(oldPlan, newPlan) {
  const olds = oldPlan && oldPlan.steps ? oldPlan.steps : [];
  const oparts = olds.map(stepParts);
  const used = new Set();
  const map = {};                                                // 旧序号 → 新序号（从 1 数）
  const match = newPlan.steps.map((st, i) => {
    const np = stepParts(st);
    for (let j = 0; j < olds.length; j++) {
      if (used.has(j) || oparts[j].base !== np.base) continue;
      const orefs = oparts[j].refs;
      const keys = Object.keys(orefs);
      if (keys.length !== Object.keys(np.refs).length) continue;
      const sameDeps = keys.every((k) => {
        const a = /^s(\d+)\.(\d+)$/.exec(orefs[k]), b = /^s(\d+)\.(\d+)$/.exec(np.refs[k] || "");
        return a && b && a[2] === b[2] && map[Number(a[1])] === Number(b[1]);
      });
      if (!sameDeps) continue;
      used.add(j);
      map[j + 1] = i + 1;
      return j;
    }
    return -1;
  });
  newPlan.steps.forEach((st, i) => {
    const o = match[i] >= 0 ? olds[match[i]] : null;
    st.state = o ? o.state : "idle";
    st.run = o ? o.run : undefined;
    st.result = o ? o.result : undefined;
    st.runs = o ? o.runs || 0 : 0;
    if (o && typeof o.collapsed === "boolean") st.collapsed = o.collapsed;                    // 用户折叠 / 展开过的，换了新方案（同一个步骤）也保持
    st.ran = o && o.ran ? { base: o.ran.base, refs: remapRefs(o.ran.refs, map) } : undefined;
    st.usedRuns = {};
    if (o) for (const [d, n] of Object.entries(o.usedRuns || {})) if (map[d]) st.usedRuns[map[d]] = n;
  });
  return newPlan;
}

// 删掉第 i 步（下标）：后面的步骤序号往前挪，引用被删步骤的文件设置去掉（上次运行时的引用也跟着换序号，不然会被当成「设置改过了」）。
// 返回新方案（不改原对象的 steps 数组，步骤对象本身是同一个）。
export function removeStep(plan, i) {
  const gone = i + 1;
  const steps = plan.steps.filter((_, k) => k !== i);
  const map = {};
  plan.steps.forEach((_, k) => { if (k !== i) map[k + 1] = k + 1 > gone ? k : k + 1; });
  for (const st of steps) {
    const files = {};
    for (const [k, ref] of Object.entries(st.files || {})) {
      if (ref && ref.step) {
        if (ref.step === gone) continue;
        files[k] = ref.step > gone ? { ...ref, step: ref.step - 1 } : ref;
      } else files[k] = ref;
    }
    st.files = files;
    if (st.ran) st.ran = { base: st.ran.base, refs: remapRefs(st.ran.refs, map) };
    const used = {};
    for (const [d, n] of Object.entries(st.usedRuns || {})) if (map[d]) used[map[d]] = n;
    st.usedRuns = used;
  }
  return { ...plan, steps };
}

// 在末尾加一步。已经 MAX_STEPS 步返回 null
export function appendStep(plan, step) {
  const steps = plan && plan.steps ? plan.steps : [];
  if (steps.length >= MAX_STEPS) return null;
  return { ...(plan || { note: "" }), steps: [...steps, step] };
}

// 文件控件下拉里的可选项：{steps: 前面步骤的结果, assets: 素材库里同类型的}，每项 {value, label}。
// value 的写法见 refToValue：素材 "a:3"，前面步骤的产出 "s:1:2"（第 1 步的第 2 个）
export function fileChoices(plan, assets, i, kind) {
  const steps = [];
  for (let s = 0; s < i; s++) {
    const up = plan.steps[s];
    if (!(((up.schema || {}).produces) || []).includes(kind)) continue;
    const have = stepOutputs(up)[kind].length;
    for (let n = 1; n <= Math.max(1, have); n++) steps.push({ value: `s:${s + 1}:${n}`, label: `步骤 ${s + 1}「${up.name}」的第 ${n} 个${KIND_LABEL[kind]}${have ? "" : "（运行后才有）"}` });
  }
  const list = [];
  assets.forEach((a, k) => { if (a.kind === kind) list.push({ value: `a:${k + 1}`, label: `#${k + 1} ${assetLabel(a)}` }); });
  return { steps, assets: list };
}

export function refToValue(ref) {
  if (ref && ref.step) return `s:${ref.step}:${ref.n || 1}`;
  return ref && ref.asset ? `a:${ref.asset}` : "";
}

export function valueToRef(v) {
  const m = /^(a|s):(\d+)(?::(\d+))?$/.exec(String(v || ""));
  if (!m) return null;
  return m[1] === "a" ? { asset: Number(m[2]) } : { step: Number(m[2]), n: Number(m[3] || 1) };
}

// 方案里有没有「动过的东西」：跑过 / 有结果 / 改过设置 / 换过模式 / 选过文件。换方案之前要不要先问一声用
export function planHasWork(plan) {
  return !!plan && (plan.steps || []).some((st) => st.state !== "idle" || st.result || st.mode || Object.keys(st.fields || {}).length || Object.keys(st.files || {}).length);
}

// 方案里用到的费用 / 注意（给方案顶部的提醒）：[{index, name, text}]
export function costNotes(plan) {
  return (plan ? plan.steps : []).map((s, i) => ({ index: i + 1, name: s.name, text: (s.schema && s.schema.cost) || "" })).filter((c) => c.text);
}

export function planSummary(plan) {
  return (plan ? plan.steps : []).map((s, i) => `${i + 1}. ${s.name}`).join(" → ");
}

// ── 分类 / 预计时间 / 报错说人话 / 折叠 / 对比原图 ────────────────────────────────────────────────────────
// 应用目录 → [{group, apps}]（按目录里出现的先后；没有分类的归「其他」）：「添加一步」的下拉按它分组
export function groupApps(apps) {
  const order = [], by = {};
  for (const a of apps || []) {
    if (!a) continue;
    const g = String(a.group || "") || "其他";
    if (!by[g]) { by[g] = []; order.push(g); }
    by[g].push(a);
  }
  return order.map((group) => ({ group, apps: by[group] }));
}

// 一次运行耗时的记录（存在这个浏览器里）：key = 应用 + 模式（同一个应用不同模式差很多：网关出图约 1 分钟、阿里约半分钟、视频要好几分钟），
// 每个 key 只留最近 5 次，预计时间取中位数（偶尔一次卡很久不会带偏）
export const MAX_DURATIONS = 5;

export function durationKey(step) {
  const m = step.schema && step.schema.mode;
  return `${step.workflow}|${m && Array.isArray(m.options) ? m.options.indexOf(currentMode(step)) : -1}`;
}

export function recordDuration(durations, key, ms) {
  if (!durations || typeof durations !== "object" || !Number.isFinite(ms) || ms < 1000) return;      // 不到 1 秒的是缓存命中 / 没真跑，不当「用时」
  const list = (durations[key] = (Array.isArray(durations[key]) ? durations[key] : []).filter((x) => Number.isFinite(x) && x > 0));
  list.push(Math.round(ms));
  while (list.length > MAX_DURATIONS) list.shift();
}

export function estimateMs(durations, key) {
  const list = ((durations && durations[key]) || []).filter((x) => Number.isFinite(x) && x > 0).sort((a, b) => a - b);
  return list.length ? list[Math.floor(list.length / 2)] : 0;
}

// 「约 40 秒」「约 1 分钟」「约 3 分钟」：不装精确
export function etaText(ms) {
  const s = Math.max(1, Math.round(ms / 1000));
  if (s < 50) return `约 ${Math.max(5, Math.round(s / 5) * 5)} 秒`;
  if (s < 90) return "约 1 分钟";
  return `约 ${Math.round(s / 60)} 分钟`;
}

// 已经过去 / 预计，0~95：到不了 100（预计只是估的，没跑完就不显示满）；没有预计返回 null
export function progressPercent(elapsedMs, estMs) {
  if (!(estMs > 0) || !(elapsedMs >= 0)) return null;
  return Math.min(95, Math.round((elapsedMs / estMs) * 100));
}

// 第 i 步（下标）跑完之后，「运行全部」还会接着跑的步骤（下标）：本来就要跑的，加上用到它们结果的（前面的一重跑，后面用它结果的就过期了，也要重跑）
export function pendingAfter(plan, i) {
  const will = new Set([i]);
  const out = [];
  for (let j = i + 1; j < plan.steps.length; j++) {
    if (needsRun(plan, j) || depsOf(plan.steps[j]).some((d) => will.has(d - 1))) { will.add(j); out.push(j); }
  }
  return out;
}

// 从第 fromIdx 步（下标，正在跑的那一步）起，把后面要接着跑的步骤也算上，大概还要多久；任何一步没有记录就返回 null（不瞎猜）
export function remainingMs(plan, durations, fromIdx, elapsedMs) {
  let total = 0;
  for (const i of [fromIdx, ...pendingAfter(plan, fromIdx)]) {
    const est = estimateMs(durations, durationKey(plan.steps[i]));
    if (!est) return null;
    total += i === fromIdx ? Math.max(est - (elapsedMs || 0), 5000) : est;
  }
  return total;
}

// 报错说人话：认得的几种错误给一句人话和怎么办，认不出返回 null（界面就只显示原文）。规则按「先匹配到的」，原文都是这个系统里真出现过的写法
export const ERROR_RULES = [
  [/没有拿到网关的 Key|没有 API Key|还没有(?:阿里|网关) Key|Key 没填/i, "还没填 Key", "到 ComfyUI 的节点图里展开标题是「Key：阿里」或「Key：网关」的节点，填一次 apikey（填过一次，其他应用也能用）"],
  [/HTTP 401|invalid_api_key|Invalid token|Incorrect API key|API key.*(?:invalid|incorrect)|未授权/i, "Key 不对或者已经失效", "检查「Key：…」节点里填的 Key；也可能是在服务商后台被停用 / 过期了"],
  [/HTTP 429|Throttling|限流|rate limit|too many requests/i, "调用太频繁，被对方限流了", "等一分钟再点「再试一次」"],
  [/model_not_found|Model not exist|不认识的模型|The model .* does not exist/i, "这个模型对方没有", "名字写错了或者已经下线：在这一步的设置里换一个模型"],
  [/等了 \d+ 秒没有回应|回得太慢|ReadTimeout|timed out|超时/i, "对方这次回得太慢了", "请求已经发出去了，可能已经计费——先别连着重跑，等一会儿再点「再试一次」"],
  [/还没出结果|查不到任务进度|任务不见了/, "阿里那边的视频还没做完", "任务可能还在做、也可能已经计费：先别连着重跑，过几分钟再看；报错里有任务编号，可以到阿里云百炼控制台查"],           // 要排在「连不上」前面：查不到进度的报错里也写着 ConnectionError，但这时任务已经提交了，不是「请求没发出去」。改视频 / 视频换人是几分钟的异步任务
  [/不知道这次请求有没有送到/, "没收到服务器的回复", "服务器可能已经收到了请求：再点「再试一次」，会带着同一个编号重试，服务器收到过的不会重复提交，也不会重复计费"],       // 工作台自己的提示（网页到服务器之间断了），要排在「连不上」前面：那条说的是「请求没发出去」
  [/连不上|ConnectionError|Connection aborted|Connection refused|Name or service not known/i, "网络没连上", "请求没有发出去（不会计费）：稍等几秒再点「再试一次」"],
  [/额度|quota|insufficient|余额不足|Arrearage|欠费/i, "额度用完了", "视频（Veo）每天只有约 3 个额度；阿里的（改视频 / 视频换人按秒计费）到阿里云百炼后台看看余额"],
  [/人物照片(?:里没有检测到人|没通过检查)|InvalidImage\./, "人物照片没通过检查", "换一张清晰的单人照（脸和上半身都在画面里、别太小）；检查没通过阿里不收费"],
  [/safety|审核|违规|敏感|blocked|content policy/i, "内容没通过对方的审核", "换一种说法，或者换一张图再试"],
];

export function explainError(text) {
  const t = String(text || "");
  for (const [re, title, tip] of ERROR_RULES) if (re.test(t)) return { title, tip };
  return null;
}

// 步骤折叠：step.collapsed 是 true / false 时按用户选的；没选过（undefined）时，方案有 4 步以上就只展开最该看的那一步（第一个还没做完的），其余折叠成一行，一屏看得下
// （2~3 步的方案都展开：步骤少，一屏放得下，设置也都摆在眼前）
export const AUTO_COLLAPSE_FROM = 4;

export function focusIndex(plan) {
  const i = plan.steps.findIndex((_, k) => needsRun(plan, k));
  return i < 0 ? 0 : i;
}

export function isCollapsed(plan, i) {
  const s = plan.steps[i];
  if (typeof s.collapsed === "boolean") return s.collapsed;
  return plan.steps.length >= AUTO_COLLAPSE_FROM && i !== focusIndex(plan);
}

export function setAllCollapsed(plan, on) {
  for (const s of plan.steps) s.collapsed = !!on;
}

// 折叠起来的步骤只剩一行，补一句：用的什么模式、改过几项设置
export function stepSummary(step) {
  const parts = [];
  const mode = currentMode(step);
  if (step.schema && step.schema.mode && mode) parts.push(mode);
  const n = Object.keys(step.fields || {}).length;
  if (n) parts.push(`改过 ${n} 项设置`);
  return parts.join(" · ");
}

// 折叠起来的步骤后面补一句结果有什么：「结果：2 张图片、2 段文字」（出错信息那种只在出错时出现的文字不算）
const RESULT_NOUN = { image: "张图片", video: "个视频", audio: "段音频", text: "段文字" };
export function resultBrief(result) {
  const c = {};
  for (const it of (result && result.items) || []) if (!it.isError) c[it.kind] = (c[it.kind] || 0) + 1;
  const parts = ["image", "video", "audio", "text"].filter((k) => c[k]).map((k) => `${c[k]} ${RESULT_NOUN[k]}`);
  return parts.length ? "结果：" + parts.join("、") : "";
}

// 这一步的「原图」：它用的第一个图片文件（素材库里的，或前面步骤做出来的）→ 素材库里的素材 {name, kind, label, n}，没有返回 null。结果大图的「对比原图」用
export function sourceImage(plan, assets, i) {
  const step = plan.steps[i];
  const files = (step.schema && step.schema.files) || {};
  for (const [k, ref] of Object.entries(step.files || {})) {
    if (!files[k] || files[k].kind !== "image" || !ref) continue;
    let n = 0;
    if (ref.asset) n = ref.asset;
    else if (ref.step && ref.step <= i) n = stepOutputs(plan.steps[ref.step - 1]).image[(ref.n || 1) - 1] || 0;
    const a = n ? assets[n - 1] : null;
    if (a && a.name && a.kind === "image") return { ...a, n };
  }
  return null;
}

// 素材库里的第 n 个被哪些步骤用着（步骤序号，从 1 数）
export function usersOfAsset(plan, n) {
  const hit = [];
  (plan ? plan.steps : []).forEach((st, i) => { if (Object.values(st.files || {}).some((r) => r && r.asset === n)) hit.push(i + 1); });
  return hit;
}

// 删掉素材库里的第 n 个：编号是位置，不能让后面的编号移位（方案里的引用、聊天里说过的「素材 3」都靠它），所以留一个空位（和存档里坏掉的条目一样）；
// 方案里直接引用它的文件设置也去掉（那一步会变成「还缺文件」/ 要重跑）。返回被改动的步骤序号
export function removeAsset(assets, plan, n) {
  if (!Number.isInteger(n) || n < 1 || !assets[n - 1] || !assets[n - 1].name) return [];
  assets[n - 1] = { name: "", kind: "other", label: "" };
  const hit = usersOfAsset(plan, n);
  for (const st of plan ? plan.steps : []) for (const [k, r] of Object.entries(st.files || {})) if (r && r.asset === n) delete st.files[k];
  return hit;
}

// ── 小工具 ─────────────────────────────────────────────────────────────────────────────────────────────
// 回车发送；中文输入法选字时的回车（isComposing / keyCode 229）不算
export function isEnterToSend(e) {
  return e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey && !e.isComposing && e.keyCode !== 229;
}

// 节点标题常常很长（「★ 只填这里：商品信息（翻译模式：…）」），状态栏里只要个大概：去掉前面的星号标记，超过 n 个字截断
export function shortTitle(title, n = 14) {
  const t = String(title || "").replace(/^[★☆\s]+/, "").replace(/^只填这里[：:]\s*/, "");
  return t.length > n ? t.slice(0, n) + "…" : t;
}

export function elapsedText(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

// 数字控件的小数位数（按 step 算：0.05 → 2）
export function decimals(step) {
  if (!step || step >= 1) return 0;
  const s = String(step);
  return s.includes("e-") ? Number(s.split("e-")[1]) : (s.split(".")[1] || "").length;
}

// 把数字收进 [min, max]、按小数位取整（滑块拖出来的 0.30000000000000004 不要）
export function clampNumber(v, f) {
  let x = Number(v);
  if (!Number.isFinite(x)) return null;
  if (f.min != null && x < f.min) x = f.min;
  if (f.max != null && x > f.max) x = f.max;
  const d = decimals(f.step);
  x = f.kind === "int" ? Math.round(x) : Number(x.toFixed(Math.max(d, 2)));
  return x;
}

// ── 存进浏览器 ────────────────────────────────────────────────────────────────────────────────────────
const STATES = ["idle", "running", "done", "failed", "stopped", "interrupted"];
const FIELD_KINDS = ["text", "choice", "int", "float", "bool"];
const str = (v) => (typeof v === "string" ? v : v == null ? "" : String(v));
const isObj = (v) => !!v && typeof v === "object" && !Array.isArray(v);
const posInts = (v) => (Array.isArray(v) ? v.filter((n) => Number.isInteger(n) && n > 0) : []);
const finiteOr = (v, d) => (typeof v === "number" && Number.isFinite(v) ? v : d);

function trimItems(items) {
  return (items || []).map((it) => (it.text != null && it.text.length > MAX_STORED_TEXT ? { ...it, text: it.text.slice(0, MAX_STORED_TEXT) } : it));
}

// 存进 localStorage 的内容：只留最近的消息；方案里正在运行的步骤：已经拿到 prompt_id 的，刷新后接着等结果；
// 还没拿到 prompt_id 但有 run_id 的（提交的那一刻刷新了），刷新后用同一个 run_id 再提交一次（后端同一个 run_id 只会真的提交一次）；两样都没有的跟不上了，标成已中断
export function toStorage(state) {
  const messages = state.messages.slice(-MAX_STORED_MESSAGES);
  const plan = state.plan
    ? {
      ...state.plan,
      steps: state.plan.steps.map((s) => {
        const c = { ...s };
        if (c.result) c.result = { ...c.result, items: trimItems(c.result.items) };
        if (c.state === "running" && !(c.run && (c.run.promptId || c.run.runId))) { c.state = "interrupted"; delete c.run; }
        return c;
      }),
    }
    : null;
  return JSON.stringify({ v: STORE_VERSION, messages, assets: state.assets, plan, chain: state.chain || null });
}

function cleanMessage(m) {
  if (!isObj(m) || !["user", "assistant", "error", "note"].includes(m.role)) return null;
  return { id: str(m.id), role: m.role, text: str(m.text), attachments: posInts(m.attachments), warnings: Array.isArray(m.warnings) ? m.warnings.map(str) : [],
    snap: isObj(m.snap) ? m.snap : null, planText: str(m.planText), model: typeof m.model === "string" ? m.model : "" };
}

function cleanItem(it) {
  if (!isObj(it) || !["image", "video", "audio", "text"].includes(it.kind) || !isObj(it.file) || typeof it.file.filename !== "string") return null;
  const out = { kind: it.kind, file: { filename: it.file.filename, subfolder: str(it.file.subfolder), type: str(it.file.type) || "output" }, label: str(it.label), isError: !!it.isError };
  if (Number.isInteger(it.asset) && it.asset > 0) out.asset = it.asset;
  if (typeof it.text === "string") out.text = it.text;
  return out;
}

function cleanResult(r) {
  if (!isObj(r)) return undefined;
  const b = isObj(r.byKind) ? r.byKind : {};
  return { items: (Array.isArray(r.items) ? r.items : []).map(cleanItem).filter(Boolean), error: str(r.error), warnings: Array.isArray(r.warnings) ? r.warnings.map(str) : [],
    elapsed: finiteOr(r.elapsed, 0), byKind: { image: posInts(b.image), video: posInts(b.video), audio: posInts(b.audio) } };
}

function cleanField(f) {
  if (!isObj(f) || typeof f.key !== "string" || !FIELD_KINDS.includes(f.kind)) return null;
  const out = { key: f.key, label: str(f.label) || f.key, kind: f.kind, hint: str(f.hint) };
  if (f.advanced) out.advanced = true;
  if (f.kind === "choice") {
    const opts = Array.isArray(f.options) ? f.options.map(str) : [];
    if (!opts.length) return null;
    out.options = opts;
    out.default = opts.includes(str(f.default)) ? str(f.default) : opts[0];
    if (isObj(f.labels)) {                                                // 选项 → 显示文字（音色下拉里写上性别和特点）；值不变
      const labels = {};
      for (const [k, v] of Object.entries(f.labels)) if (opts.includes(k) && typeof v === "string" && v) labels[k] = v;
      if (Object.keys(labels).length) out.labels = labels;
    }
  } else if (f.kind === "text") {
    out.default = str(f.default);
    if (f.multiline === false) out.multiline = false;                      // 单行文字（角色名、颜色）：界面里用单行输入框
  } else if (f.kind === "bool") {
    out.default = !!f.default;
  } else {
    out.min = Number.isFinite(f.min) ? f.min : null;
    out.max = Number.isFinite(f.max) ? f.max : null;
    out.step = Number.isFinite(f.step) && f.step > 0 ? f.step : null;
    out.default = finiteOr(f.default, out.min != null ? out.min : 0);
  }
  return out;
}

function cleanSchema(sc) {
  const src = isObj(sc) ? sc : {};
  const fields = {}, files = {};
  for (const f of Object.values(isObj(src.fields) ? src.fields : {})) { const c = cleanField(f); if (c) fields[c.key] = c; }
  for (const f of Object.values(isObj(src.files) ? src.files : {})) {
    if (isObj(f) && typeof f.key === "string" && MEDIA_KINDS.includes(f.kind)) {
      files[f.key] = { key: f.key, label: str(f.label) || f.key, kind: f.kind, required: !!f.required };
      const rm = Array.isArray(f.required_modes) ? f.required_modes.filter((n) => Number.isInteger(n) && n > 0) : [];
      if (rm.length) files[f.key].required_modes = rm;
    }
  }
  let mode = null;
  if (isObj(src.mode) && typeof src.mode.key === "string" && Array.isArray(src.mode.options) && src.mode.options.length) {
    const opts = src.mode.options.map(str);
    mode = { key: src.mode.key, label: str(src.mode.label) || "模式", options: opts, default: opts.includes(str(src.mode.default)) ? str(src.mode.default) : opts[0] };
  }
  return { mode, fields, files, outputs: Array.isArray(src.outputs) ? src.outputs : [], results: Array.isArray(src.results) ? src.results.map(str) : [],
    produces: Array.isArray(src.produces) ? src.produces.filter((k) => typeof k === "string") : [], cost: str(src.cost), desc: str(src.desc), titles: isObj(src.titles) ? src.titles : {} };
}

function cleanRef(r) {
  if (isObj(r) && Number.isInteger(r.step) && r.step > 0) return { step: r.step, n: Number.isInteger(r.n) && r.n > 0 ? r.n : 1 };
  if (isObj(r) && Number.isInteger(r.asset) && r.asset > 0) return { asset: r.asset };
  return null;
}

function cleanRun(r) {
  if (!isObj(r) || !Number.isFinite(r.started)) return undefined;
  const snap = isObj(r.snap) && isObj(r.snap.parts) && typeof r.snap.parts.base === "string"
    ? { parts: { base: r.snap.parts.base, refs: isObj(r.snap.parts.refs) ? r.snap.parts.refs : {} }, usedRuns: isObj(r.snap.usedRuns) ? r.snap.usedRuns : {} } : undefined;
  return { started: r.started, promptId: typeof r.promptId === "string" ? r.promptId : undefined, runId: typeof r.runId === "string" ? r.runId : undefined, name: str(r.name),
    outputs: Array.isArray(r.outputs) ? r.outputs : [], titles: isObj(r.titles) ? r.titles : {}, warnings: Array.isArray(r.warnings) ? r.warnings.map(str) : [], snap };
}

function cleanStep(s) {
  if (!isObj(s) || typeof s.workflow !== "string" || !s.workflow) return null;
  const schema = cleanSchema(s.schema);
  const fields = {};
  for (const [k, v] of Object.entries(isObj(s.fields) ? s.fields : {})) if (["string", "number", "boolean"].includes(typeof v) && (typeof v !== "number" || Number.isFinite(v))) fields[k] = v;
  const files = {};
  for (const [k, v] of Object.entries(isObj(s.files) ? s.files : {})) { const r = cleanRef(v); if (r) files[k] = r; }
  const st = { workflow: s.workflow, name: str(s.name) || s.workflow, path: str(s.path), mode: typeof s.mode === "string" ? s.mode : null, fields, files, note: str(s.note), schema,
    state: STATES.includes(s.state) ? s.state : "idle", runs: Number.isFinite(s.runs) ? s.runs : 0, usedRuns: isObj(s.usedRuns) ? s.usedRuns : {} };
  const result = cleanResult(s.result);
  if (result) st.result = result;
  const run = cleanRun(s.run);
  if (run) st.run = run;
  if (typeof s.collapsed === "boolean") st.collapsed = s.collapsed;
  if (typeof s.lostRunId === "string" && s.lostRunId) st.lostRunId = s.lostRunId.slice(0, 80);       // 上次提交时网络断了、不知道服务器收没收到：再试这一步时沿用的 run_id
  if (isObj(s.ran) && typeof s.ran.base === "string") st.ran = { base: s.ran.base, refs: isObj(s.ran.refs) ? s.ran.refs : {} };
  if (st.state === "running" && !(st.run && (st.run.promptId || st.run.runId))) st.state = "interrupted";
  return st;
}

export function fromStorage(text) {
  try {
    const d = JSON.parse(text);
    if (!d || d.v !== STORE_VERSION || !Array.isArray(d.messages)) return null;
    const steps = d.plan && Array.isArray(d.plan.steps) ? d.plan.steps.map(cleanStep).filter(Boolean).slice(0, MAX_STEPS) : [];
    // 素材库的编号是位置：坏掉的条目留一个空位（不能删，不然后面的编号全移位、方案里的引用会指到别的素材）
    const assets = (Array.isArray(d.assets) ? d.assets : []).slice(0, MAX_ASSETS)
      .map((a) => (isObj(a) && typeof a.name === "string" && a.name ? { name: a.name, kind: MEDIA_KINDS.includes(a.kind) ? a.kind : fileKind(a.name), label: str(a.label) } : { name: "", kind: "other", label: "" }));
    const chain = isObj(d.chain) && ["all", "one"].includes(d.chain.mode) ? { mode: d.chain.mode, cursor: Number.isInteger(d.chain.cursor) ? d.chain.cursor : 0, stopped: !!d.chain.stopped } : null;
    return { messages: d.messages.map(cleanMessage).filter(Boolean), assets, plan: steps.length ? { note: str(d.plan.note), steps } : null, chain };
  } catch (e) {
    return null;
  }
}
