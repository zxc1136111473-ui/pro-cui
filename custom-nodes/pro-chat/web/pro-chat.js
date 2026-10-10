// AI 工作台：ComfyUI 里的大窗口（后端见 ../__init__.py，界面见 pro-chat-ui.js，纯函数见 pro-chat-lib.js）。
// 说一句话（可以附商品图 / 视频 / 音频）→ AI 排出一步或几步方案（每步 = 一个应用）→ 每步的设置都能改 → 一键跑完：
// 每一步的结果自动放进素材库，下一步直接用；改了哪一步只重跑哪一步。不碰你的画布，节点图在后面（右上角按钮回去）。
// 这个文件只管接 ComfyUI（页面对象、网络、队列、侧栏图标）和运行流程；所有文字都经 pro-chat-ui.js 用 textContent 写进页面。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import * as lib from "./pro-chat-lib.js";
import { createView } from "./pro-chat-ui.js";

const STORE_KEY = `proChat.v${lib.STORE_VERSION}`;
const UI_KEY = "proChat.ui";
const LOCK_KEY = "proChat.runLock";            // localStorage 里的运行记录：谁正在运行。别的标签页靠它知道「有人在跑」（决定存不存档、跟不跟着更新）；没有 Web Locks 的浏览器里它还是唯一的锁
const RUN_LOCK = "proChat.run";                // Web Locks 的锁名
const LOCK_TTL_MS = 90000;                     // 放在后台的标签页定时器可能被浏览器降到每分钟一次，所以记录要留够时间；代价是（没有 Web Locks 时）运行的标签页崩了，另一个要等这么久才接手
const LOCK_BEAT_MS = 5000;
const HISTORY_POLL_MS = 2000;
const RUN_TIMEOUT_MS = 15 * 60 * 1000;
const MAX_BYTES = { image: 20 * 1024 * 1024, video: 150 * 1024 * 1024, audio: 50 * 1024 * 1024 };
const TAB_ID = "pro-chat";                     // 侧栏标签页的 id
const EXAMPLES = [
  "给红苹果做一张淘宝主图，再用这张图做一条视频，配上配音和字幕",
  "做一张夏日清凉节促销海报，满199减50，限时三天",
  "帮我写一段红苹果的淘宝标题和卖点",
  "（先点「附件」传一张商品图）把这张图的背景换成纯白色，再用它做一条视频",
  "把「夏日清凉，限时三天」念成语音，声音温暖一点，语速慢一点",
  "给我 30 秒舒缓钢琴配乐，适合产品视频",
];

const state = { messages: [], assets: [], plan: null, draft: [], chain: null, busy: false, running: null };
// 这个网页的标识（每次加载都不同）。不能存进 sessionStorage 当「标签页」的标识：浏览器的「复制标签页」会把 sessionStorage 一起复制，
// 两个标签页就成了同一个，运行锁对它们不起作用；刷新网页后接手自己上一次没做完的运行靠的是 Web Locks / 刷新前放掉的记录，不靠这个标识
const INSTANCE = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
let seq = 0;
const nextId = () => `${INSTANCE}-${++seq}`;
let view, saveTimer, tickTimer, lockTimer, sendToken = 0, catalogApps = null, ui = { open: true };
let driving = false;                           // 这个标签页正在拿运行锁 / 运行（拿锁要等一下，这期间也不能再开始别的）
let lostLock = false;                          // 没有 Web Locks 时：运行记录被别的标签页写了，这里已经不是持有的那个
let releaseWeb = null;                         // 持有 Web Locks 的锁时：调用它放锁

const url = (path) => api.apiURL(path);
const post = (route, body) => api.fetchApi(route, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

async function readJson(r) {
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `请求失败（HTTP ${r.status}）`);
  return d;
}

// ── 存档 / 多标签页 ──────────────────────────────────────────────────────────────────────────────────
function lockedByOther() {
  try {
    const l = JSON.parse(localStorage.getItem(LOCK_KEY) || "null");
    return !!l && l.tab !== INSTANCE && Date.now() - l.t < LOCK_TTL_MS;
  } catch (e) { return false; }
}

function holdLock() {
  try { localStorage.setItem(LOCK_KEY, JSON.stringify({ tab: INSTANCE, t: Date.now() })); } catch (e) { /* 存不了就算了 */ }
}

function releaseLock() {
  try {
    const l = JSON.parse(localStorage.getItem(LOCK_KEY) || "null");
    if (l && l.tab === INSTANCE) localStorage.removeItem(LOCK_KEY);
  } catch (e) { /* 同上 */ }
}

function beat() {
  if (lockedByOther()) lostLock = true;                    // 记录被别的标签页写了（Web Locks 下不会发生）：别再往下提交
  else holdLock();
}

// 拿运行锁：同一个浏览器里同一时刻只让一个标签页去提交 / 续跑，不然会重复提交（视频要占额度）。拿到返回「放锁」的函数，拿不到返回 null。
// 优先用 Web Locks：独占、拿锁是原子的，标签页关了 / 崩了浏览器自己放，复制出来的标签页也各算各的。
// 用不了的（页面不是 https / localhost）退回 localStorage 里的记录 + 心跳：先看有没有别人的、再写自己的、稍等一下再确认一次（两个标签页同时写，后写的算数）
async function takeLock() {
  let viaWeb = false;
  if (navigator.locks && typeof navigator.locks.request === "function") {
    try {
      viaWeb = await new Promise((resolve, reject) => {
        navigator.locks.request(RUN_LOCK, { ifAvailable: true }, (lock) => {
          if (!lock) { resolve(false); return undefined; }
          return new Promise((release) => { releaseWeb = release; resolve(true); });    // 这个 Promise 不结束就一直占着，放锁时才结束
        }).catch(reject);
      });
      if (!viaWeb) return null;
    } catch (e) { viaWeb = false; }                        // Web Locks 报错（比如页面不是安全环境）：退回记录
  }
  if (!viaWeb) {
    if (lockedByOther()) return null;
    holdLock();
    await new Promise((r) => setTimeout(r, 150));
    if (lockedByOther()) return null;
  }
  lostLock = false;
  holdLock();
  clearInterval(lockTimer);
  lockTimer = setInterval(beat, LOCK_BEAT_MS);
  return () => {
    clearInterval(lockTimer);
    if (releaseWeb) { releaseWeb(); releaseWeb = null; }
    releaseLock();
  };
}

function saveNow() {
  clearTimeout(saveTimer);
  if (lockedByOther()) return;                 // 别的标签页正在运行：它存的才是最新的，别用这里（可能是旧的）去覆盖
  try { localStorage.setItem(STORE_KEY, lib.toStorage(state)); } catch (e) { /* 隐私模式 / 配额：不存就不存 */ }
}

function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, 300);
}

function load() {
  try {
    const d = lib.fromStorage(localStorage.getItem(STORE_KEY));
    if (d) Object.assign(state, d);
  } catch (e) { /* 读不了就从空白开始 */ }
  try {
    const u = JSON.parse(localStorage.getItem(UI_KEY) || "null");
    if (u && typeof u === "object") ui = { ...ui, ...u };
  } catch (e) { /* 默认打开 */ }
}

// 用存档里的状态换掉这里的（消息 / 素材库 / 方案）：另一个标签页在运行时，这里靠它跟着更新
function adoptStored(text) {
  const d = lib.fromStorage(text);
  if (!d) return false;
  Object.assign(state, { messages: d.messages, assets: d.assets, plan: d.plan });
  view.renderAll();
  return true;
}

// 另一个标签页在运行时，它每次存档这里都跟着更新界面（这个标签页自己闲着时才跟，忙着的不去动）
function onStorage(e) {
  if (e.key !== STORE_KEY || !e.newValue || planLocked() || !lockedByOther()) return;
  adoptStored(e.newValue);
}

// 步骤还停在「运行中」、但这个标签页没在运行它：刷新网页时还在跑的（接着做），或者另一个标签页的运行没了（关了 / 崩了；也可能是它跑完了、这里当时在后台没收到最后的存档）。
// 拿到运行锁才能接手：别的标签页还活着就拿不到，由它负责，这里靠存档事件跟着更新。拿到之后以存档里的为准（上一个运行的标签页最后存的才是最新的，包括「运行全部」还是「只跑这一步」）：
// 已经拿到 prompt_id 的接着等结果；提交到一半（有 run_id 没有 prompt_id）的用同一个 run_id 再提交（后端同一个 run_id 只会真的提交一次）；然后按原来的模式继续。
// 两样都没有的在 toStorage / fromStorage 里已经标成已中断
async function resumeRunning() {
  if (planLocked()) return;
  if (!state.plan || !state.plan.steps.some((s) => s.state === "running")) { state.chain = null; return; }
  driving = true;
  let release = null;
  try {
    release = await takeLock();
    if (!release) return;
    try {
      const d = lib.fromStorage(localStorage.getItem(STORE_KEY));
      if (d) { Object.assign(state, { messages: d.messages, assets: d.assets, plan: d.plan, chain: d.chain }); view.renderAll(); }
    } catch (e) { /* 读不了就按这里的状态来 */ }
    const i = state.plan ? state.plan.steps.findIndex((s) => s.state === "running") : -1;
    if (i < 0) { state.chain = null; return; }             // 已经跑完了：用存档里的结果，别再处理一遍
    const run = state.plan.steps[i].run;
    if (!run || !(run.promptId || run.runId)) {
      for (const s of state.plan.steps) if (s.state === "running") { s.state = "interrupted"; delete s.run; }
      state.chain = null;
      view.renderPlan();
      save();
      return;
    }
    state.running = { stepIdx: i, start: run.started, node: "", promptId: run.promptId || null };
    tick();
    await runChain(i, (state.chain && state.chain.mode) || "one", run.promptId ? "wait" : "resubmit");
  } finally {
    if (release) release();
    driving = false;
    view.refreshSend();
  }
}

// ── 应用目录（手动加步骤用；也用它刷新存在浏览器里的旧方案的设置项）──────────────────────────────────
async function loadCatalog() {
  try {
    const d = await readJson(await api.fetchApi("/pro/catalog"));
    catalogApps = d.apps || [];
    for (const st of state.plan ? state.plan.steps : []) {
      const a = catalogApps.find((x) => x.workflow === st.workflow);
      if (a) { st.schema = a.schema; st.name = a.name; st.path = a.path; }
    }
    view.renderPlan();
  } catch (e) { /* 目录读不到：只是不能手动加步骤，聊天不受影响 */ }
}

// ── 消息 ─────────────────────────────────────────────────────────────────────────────────────────────
function addMessage(m) {
  m.id = m.id || nextId();
  state.messages.push(m);
  view.renderChat();
  view.scrollChat();
  save();
  return m;
}

function changed() {
  save();
  view.refreshStatus();
}

function adoptPlan(plan) {
  state.plan = lib.adoptPlan(state.plan, plan);
  view.renderPlan();
  save();
}

// 等 AI 回复的时候、以及有步骤在运行 / 正在拿运行锁的时候，方案都不让改（回复一到整个方案会被换掉；运行中的步骤被换掉会卡住）
const planLocked = () => !!(state.running || state.busy || driving);

async function send() {
  const text = view.inputText();
  if ((!text && !state.draft.length) || planLocked()) return;
  const attached = state.draft.splice(0);
  const nums = attached.map((a) => lib.addAsset(state.assets, a).n);
  const kept = nums.filter((n) => n > 0);
  view.clearInput();
  view.renderDrafts();
  view.renderAssets();
  addMessage({ role: "user", text: text || "（附了素材）", attachments: kept });
  if (kept.length < nums.length) addMessage({ role: "note", text: `素材库满了（最多 ${lib.MAX_ASSETS} 个），有 ${nums.length - kept.length} 个没放进去；可以先「清空全部」再继续` });
  const token = ++sendToken;
  view.setBusy(true);
  try {
    const d = await readJson(await post("/pro/chat", {
      messages: lib.apiMessages(state.messages), assets: state.assets.map((a) => ({ name: a.name, label: a.label })), new_assets: kept, plan: lib.serverPlan(state.plan),
    }));
    if (token !== sendToken) return;                       // 等回复的时候点了「清空全部」：这条回复不要了
    if (d.plan && !Array.isArray(d.plan.steps)) throw new Error("服务器上的助手后端还是旧版本（方案格式对不上）：重启一下 ComfyUI 容器（deploy.sh --sync）再刷新网页");
    const m = { role: "assistant", text: d.reply, warnings: d.warnings || [] };
    if (d.plan && state.running) {                         // 正常到不了这里（运行中不让发消息）；万一别的标签页在跑，别把运行中的步骤换掉
      m.warnings.push("有步骤正在运行，这个新方案没有应用；跑完后再说一次");
    } else if (d.plan) {
      adoptPlan(d.plan);
      m.snap = lib.snapshotPlan(state.plan);
      m.planText = lib.planSummary(state.plan);
    }
    addMessage(m);
  } catch (e) {
    if (token === sendToken) addMessage({ role: "error", text: "出错了：" + (e.message || e) });
  } finally {
    if (token === sendToken) view.setBusy(false);
  }
}

// ── 上传：先传到 input/助手/，成功后才出现在输入框上方 / 用在步骤里 ───────────────────────────────────
async function uploadFile(f) {
  const kind = lib.fileKind(f.name);
  if (!lib.MEDIA_KINDS.includes(kind)) throw new Error(`「${f.name}」不是图片 / 视频 / 音频文件`);
  if (f.size > MAX_BYTES[kind]) throw new Error(`「${f.name}」太大（${lib.KIND_LABEL[kind]}最大 ${MAX_BYTES[kind] >> 20}MB）`);
  const body = new FormData();
  body.append("image", f, f.name || "image.png");
  body.append("type", "input");
  body.append("subfolder", "助手");
  body.append("overwrite", "false");
  const d = await readJson(await api.fetchApi("/upload/image", { method: "POST", body }));
  return { name: d.subfolder ? `${d.subfolder}/${d.name}` : d.name, kind, label: `上传的${lib.KIND_LABEL[kind]}：${f.name || d.name}` };
}

async function attach(files) {
  for (const f of files) {
    view.setStatus(`正在上传 ${f.name || "文件"} …`);
    try {
      state.draft.push(await uploadFile(f));
      view.setStatus("");
    } catch (e) {
      view.setStatus("上传失败：" + (e.message || e));
    }
  }
  view.renderDrafts();
}

// 在某一步的文件控件里直接上传：放进素材库，并用在这一步
async function uploadForField(i, key, kind, file) {
  if (planLocked()) return;
  try {
    const up = await uploadFile(file);
    if (up.kind !== kind) throw new Error(`这里要${lib.KIND_LABEL[kind]}，「${file.name}」是${lib.KIND_LABEL[up.kind]}`);
    const { n } = lib.addAsset(state.assets, up);
    if (!n) throw new Error(`素材库满了（最多 ${lib.MAX_ASSETS} 个）`);
    if (planLocked() || !state.plan || !state.plan.steps[i]) return;       // 上传的时候方案被换了 / 开始运行了：文件留在素材库里，不往方案里放
    state.plan.steps[i].files[key] = { asset: n };
    view.renderAssets();
    view.renderPlan();
    changed();
  } catch (e) {
    addMessage({ role: "error", text: "上传失败：" + (e.message || e) });
  }
}

// 粘贴：有图片 / 视频 / 音频文件就当附件；焦点不在输入框时（点了一下工作台的空白处）的粘贴也不能漏给 ComfyUI（它会把图粘成画布上的节点）
function onPaste(e) {
  const files = [...(e.clipboardData?.files || [])].filter((f) => lib.MEDIA_KINDS.includes(lib.fileKind(f.name)) || f.type.startsWith("image/"));
  if (files.length) { e.preventDefault(); e.stopPropagation(); attach(files); return; }
  const t = e.target;
  if (!(t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT"))) e.stopPropagation();
}

function onDrop(e) {
  e.preventDefault();
  e.stopPropagation();                      // 拖进工作台的文件只给工作台用，不让别的拖入处理（ComfyUI / 其他扩展）再处理一遍
  attach([...(e.dataTransfer?.files || [])]);
}

// ── 方案：增删 ───────────────────────────────────────────────────────────────────────────────────────
function addStep(workflow) {
  const a = (catalogApps || []).find((x) => x.workflow === workflow);
  if (!a || planLocked()) return;
  const plan = lib.appendStep(state.plan, lib.newStep(a));
  if (!plan) { addMessage({ role: "note", text: `一个方案最多 ${lib.MAX_STEPS} 步` }); return; }
  state.plan = plan;
  view.renderPlan();
  save();
}

function removeStep(i) {
  if (planLocked() || !state.plan) return;
  const plan = lib.removeStep(state.plan, i);
  state.plan = plan.steps.length ? plan : null;
  view.renderPlan();
  save();
}

function clearPlan() {
  if (planLocked()) return;
  state.plan = null;
  view.renderPlan();
  save();
}

function clearAll() {
  if (state.running) return;
  sendToken++;                                             // 还在等的 AI 回复作废
  view.setBusy(false);
  state.messages = []; state.assets = []; state.plan = null; state.draft = []; state.chain = null;
  view.renderAll();
  save();
}

// ── 运行：后端提交到队列 → 等 /history 里出现结果（事件 + 轮询）→ 结果放进素材库 ───────────────────────
function tick() {
  clearInterval(tickTimer);
  if (!state.running) return;
  tickTimer = setInterval(() => { if (!state.running) clearInterval(tickTimer); else view.tickRunning(); }, 1000);
}

function waitFinished(promptId) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    let done = false;
    const finish = (fn, v) => { if (done) return; done = true; clearInterval(timer); api.removeEventListener("execution_success", onEv); api.removeEventListener("execution_error", onEv); fn(v); };
    let misses = 0;
    const poll = async () => {
      try {
        const r = await api.fetchApi(`/history/${encodeURIComponent(promptId)}`);
        const entry = (await r.json())[promptId];
        if (lib.isFinished(entry)) return finish(resolve, entry);
        if (Date.now() - t0 > RUN_TIMEOUT_MS) return finish(reject, new Error("等了 15 分钟还没出结果，可以到队列里看看"));
        if (!entry) {                                       // 还没有记录：要么还在队列里，要么在队列里被取消了
          const q = await (await api.fetchApi("/queue")).json();
          misses = lib.inQueue(q, promptId) ? 0 : misses + 1;
          if (misses >= 3) finish(reject, new Error("这次运行已经不在队列里了（可能被取消了）"));
        }
      } catch (e) { /* 网络抖一下：下一轮再试 */ }
    };
    const onEv = (e) => { if (!e.detail || e.detail.prompt_id === promptId) poll(); };
    api.addEventListener("execution_success", onEv);
    api.addEventListener("execution_error", onEv);
    const timer = setInterval(poll, HISTORY_POLL_MS);
    poll();
  });
}

function failStep(i, message) {
  const step = state.plan.steps[i];
  step.state = "failed";
  step.result = { items: [], error: message, warnings: (step.run && step.run.warnings) || [], elapsed: step.run ? Date.now() - step.run.started : 0, byKind: {} };
  delete step.run;
  state.running = null;
  addMessage({ role: "error", text: `步骤 ${i + 1}「${step.name}」没有成功：${message}` });
  view.renderPlan();
  view.refreshSend();
  save();
}

// 把这一步产出的图片 / 视频 / 音频从输出目录复制到素材库（input/助手/），返回 {image: [编号…], video: […], audio: […]}；放不进去的写进 warnings
async function stageResults(i, items, warnings) {
  const step = state.plan.steps[i];
  const byKind = { image: [], video: [], audio: [] };
  for (const it of lib.stageable(items)) {
    try {
      const d = await readJson(await post("/pro/stage", { filename: it.file.filename, subfolder: it.file.subfolder || "", type: it.file.type || "output" }));
      const k = byKind[d.kind].length + 1;
      const { n } = lib.addAsset(state.assets, { name: d.name, kind: d.kind, label: `步骤 ${i + 1}「${step.name}」的第 ${k} 个${lib.KIND_LABEL[d.kind]}` });
      if (!n) { warnings.push("素材库满了，有结果没放进素材库（后面的步骤用不了它）"); continue; }
      it.asset = n;
      byKind[d.kind].push(n);
    } catch (e) {
      warnings.push(`「${it.file.filename}」没能放进素材库（${e.message || e}），后面的步骤用不了它`);
    }
  }
  view.renderAssets();
  return byKind;
}

// 等这一步出结果，把结果（或停止 / 失败）记进方案。步骤里的 run 有 prompt_id、结果节点 id 和开始时的设置。返回 true = 成功完成。
async function finishStep(i) {
  const step = state.plan.steps[i];
  const run = step.run;
  let ok = false;
  try {
    const entry = await waitFinished(run.promptId);
    const elapsed = Date.now() - run.started;
    if (lib.wasInterrupted(entry)) {
      step.state = "stopped";
      addMessage({ role: "note", text: `已停止步骤 ${i + 1}「${step.name}」（用时 ${lib.elapsedText(elapsed)}）` });
    } else {
      const items = lib.historyItems(entry, run.outputs);
      const error = lib.historyError(entry, run.titles);
      const warnings = [...(run.warnings || [])];
      const byKind = await stageResults(i, items, warnings);
      step.result = { items, error, warnings, elapsed, byKind };
      if (lib.runOutcome(items, error) === "done") {
        step.state = "done";
        step.runs = (step.runs || 0) + 1;
        step.ran = run.snap ? run.snap.parts : undefined;       // 没有开始时的记录（老存档）：当作设置改过，下次会提示重跑
        step.usedRuns = run.snap ? run.snap.usedRuns : {};
        ok = true;
      } else {
        step.state = "failed";
        addMessage({ role: "error", text: `步骤 ${i + 1}「${step.name}」没有成功${error ? "：" + error : "（看右边的出错信息）"}` });
      }
    }
  } catch (e) {
    state.running = null;
    failStep(i, (e.message || String(e)) + `（用时 ${lib.elapsedText(Date.now() - run.started)}）`);
    return false;
  }
  delete step.run;
  state.running = null;
  view.renderPlan();
  view.refreshSend();
  save();
  return ok;
}

// 提交一步并等它跑完；返回 true = 成功完成。resubmit = 刷新网页时这一步提交到一半：用存档里的 run_id 再提交一次（后端同一个 run_id 只会真的提交一次，
// 上次已经提交成功的话拿到的就是上次的 prompt_id，不会重复花钱）
async function executeStep(i, resubmit = false) {
  const plan = state.plan, step = plan.steps[i];
  const { files, missing, deps } = lib.resolveStepFiles(plan, state.assets, i);
  if (missing.length) { failStep(i, missing.join("；")); return false; }
  if (!resubmit) {
    step.state = "running";
    step.run = { started: Date.now(), snap: lib.runSnapshot(plan, i, deps), runId: step.lostRunId || `${nextId()}-${Date.now().toString(36)}`.slice(-80) };
    delete step.lostRunId;
  }
  const run = step.run;
  state.running = { stepIdx: i, start: run.started, node: "", promptId: null };
  view.renderPlan();
  view.refreshSend();
  tick();
  saveNow();                                                // 先把 run_id 存下来，再去提交：提交到一半刷新了，才知道用哪个 run_id 接着来
  // 网络层失败（fetch 抛 TypeError：网页正在刷新 / 连接断了）时不能算这一步失败：服务器没回话，不知道有没有收到；带着同一个 run_id 再来是安全的（同一个 run_id 只会真的提交一次）。
  // 刷新网页时这个请求会被掐断，如果这里直接记失败，刷新后的页面读到的就是「没成功」，没法接着做了
  let d = null, lastErr = null;
  for (let attempt = 1; attempt <= 3 && !d; attempt++) {
    try {
      d = await readJson(await post("/pro/run", lib.runPayload(step, files, api.clientId, run.runId)));
    } catch (e) {
      lastErr = e;
      if (!(e instanceof TypeError) || attempt === 3) break;
      await new Promise((r) => setTimeout(r, 1500 * attempt));
    }
  }
  if (!d) {
    const noReply = lastErr instanceof TypeError;
    if (noReply) step.lostRunId = run.runId;               // 服务器一直没回话，不知道它到底收到没有：下次再运行这一步沿用这个 run_id，收到过的话拿到的就是上次那一次，不会再提交一遍
    failStep(i, (noReply ? "连不上服务器，不知道这次请求有没有送到（网络断了？）。再点一次会带着同一个编号重试：服务器已经收到的，不会重复提交" : (lastErr && lastErr.message) || String(lastErr)) + `（用时 ${lib.elapsedText(Date.now() - run.started)}）`);
    return false;
  }
  // 提交成功就把 prompt_id 记进步骤：这时刷新网页也能接着等（resumeRunning），长任务（视频要几分钟）不会因为刷新丢结果
  const warnings = [...(d.warnings || [])];
  if (d.repeated) warnings.push("服务器上已经有这一次提交的记录（上次网页没拿到回复），这次直接接上它，没有重复提交");
  Object.assign(run, { promptId: d.prompt_id, name: d.name, outputs: d.outputs, titles: d.titles, warnings });
  state.running.promptId = d.prompt_id;
  save();
  if (state.running.stopRequested) interrupt(d.prompt_id);     // 提交的时候点了「停止」：拿到 prompt_id 马上中断
  return finishStep(i);
}

// 用户点了运行：先拿运行锁（同一个浏览器里只有一个标签页能运行），拿不到说明另一个标签页（包括复制出来的）在跑；拿到了就开始
async function drive(i, mode) {
  if (driving) return;
  driving = true;
  let release = null;
  try {
    release = await takeLock();
    if (!release) {
      addMessage({ role: "note", text: "另一个标签页正在运行这个方案，等它跑完（或关掉它）再来" });
      return;
    }
    await runChain(i, mode, "new");
  } finally {
    if (release) release();                                // runChain 已经把最终状态存好了，再放锁：另一个标签页收到存档时锁还在，才会跟着更新
    driving = false;
    view.refreshSend();
  }
}

// 拿到运行锁之后，从第 i 步开始跑；mode "all" = 跑完这一步接着跑后面需要运行的（设置改过 / 前面重跑过 / 还没跑的），"one" = 只跑这一步。
// how: "new" 正常提交；"wait" 刷新网页后接着等已经提交的那一步；"resubmit" 刷新网页后这一步提交到一半，用同一个 run_id 再来一次。
// 中途失败 / 停止就停下，后面的步骤不动。
async function runChain(i, mode, how) {
  state.chain = { mode, cursor: i };
  saveNow();
  let cur = i;
  try {
    for (;;) {
      const ok = how === "wait" ? await finishStep(cur) : await executeStep(cur, how === "resubmit");
      how = "new";
      if (!ok || !state.chain || state.chain.mode !== "all" || state.chain.stopped) break;
      if (lostLock) { addMessage({ role: "note", text: "运行锁被另一个标签页拿走了，这里不再提交后面的步骤（让它接着做）" }); break; }
      const next = lib.nextToRun(state.plan, cur);
      if (next < 0) break;
      cur = next;
      state.chain.cursor = cur;
      save();
    }
  } catch (e) {                                            // 不该发生的意外：别把步骤卡在「运行中」、别让界面一直锁着
    const step = state.plan && state.plan.steps[cur];
    if (step && step.state === "running") failStep(cur, "意外出错：" + (e.message || e));
    else addMessage({ role: "error", text: "运行出错了：" + (e.message || e) });
    state.running = null;
  } finally {
    state.chain = null;
    view.renderPlan();
    view.refreshSend();
    saveNow();                                             // 最终状态存好；放锁在调用的地方（drive / resumeRunning）
  }
}

function runStep(i) {
  if (planLocked() || !state.plan) return;
  drive(i, "one");
}

function runAll() {
  if (planLocked() || !state.plan) return;
  const i = lib.nextToRun(state.plan, -1);
  if (i >= 0) drive(i, "all");
}

// 中断正在跑的这一步：ComfyUI 的 /interrupt 只对正在执行的有用，还在排队的要从队列里删掉
async function interrupt(promptId) {
  try { await post("/interrupt", { prompt_id: promptId }); } catch (e) { /* 已经结束了 */ }
  try { await post("/queue", { delete: [promptId] }); } catch (e) { /* 不在队列里了 */ }
}

async function stop() {
  const r = state.running;
  if (!r) return;
  if (state.chain) state.chain.stopped = true;
  if (!r.promptId) { r.stopRequested = true; return; }     // 还在提交：拿到 prompt_id 后马上中断（executeStep 里）
  await interrupt(r.promptId);
}

function onExecuting(e) {
  const r = state.running;
  if (!r) return;
  const d = e.detail;
  const nid = d && typeof d === "object" ? d.node : d;
  const pid = d && typeof d === "object" ? d.prompt_id : null;
  if (pid && r.promptId && pid !== r.promptId) return;
  const run = state.plan && state.plan.steps[r.stepIdx] && state.plan.steps[r.stepIdx].run;
  if (nid && run) r.node = (run.titles || {})[String(nid)] || "";
}

// 「在应用里打开」：把这一步的应用载入成一个新标签页，并把方案里的设置填进去，方便接着手动调
async function openInApp(i) {
  if (planLocked()) return;
  const step = state.plan.steps[i];
  try {
    const wf = await readJson(await api.fetchApi("/userdata/" + encodeURIComponent("workflows/" + step.path)));
    await app.loadGraphData(wf, true, true, `工作台 · ${step.name}`);
    const set = (key, value) => {
      const k = key.indexOf(":");
      const node = app.graph.getNodeById(Number(key.slice(0, k)));
      const w = node && node.widgets && node.widgets.find((x) => x.name === key.slice(k + 1));
      if (!w) return;
      const vals = w.options && w.options.values;
      if (Array.isArray(vals) && !vals.includes(value)) w.options.values = [...vals, value];   // 素材在 input/助手/ 下，不在下拉里；补进去，前端才不会提示「缺少媒体」
      w.value = value;
      if (w.callback) w.callback(value);
    };
    const sc = step.schema || {};
    if (step.mode && sc.mode) set(sc.mode.key, step.mode);
    Object.entries(step.fields || {}).forEach(([k, v]) => {
      const f = (sc.fields || {})[k];
      const cast = f && f.cast === "int" ? Number.parseInt(v, 10) : f && f.cast === "float" ? Number.parseFloat(v) : v;   // 数字下拉：节点里的选项是数字
      set(k, cast);
    });
    Object.entries(lib.resolveStepFiles(state.plan, state.assets, i).files).forEach(([k, v]) => set(k, v));
    app.graph.setDirtyCanvas(true, true);
    close();
  } catch (e) {
    addMessage({ role: "error", text: "打开失败：" + (e.message || e) });
  }
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => { btn.textContent = "已复制"; }, () => { btn.textContent = "复制失败"; }).finally(() => setTimeout(() => { btn.textContent = "复制"; }, 1500));
}

// 文字结果（AI 写的提示词 / 文案）在输出目录里是 .txt，要读出来显示
function loadText(it, src, done) {
  fetch(src).then((r) => r.text()).then((t) => { it.text = t; done(t); save(); }).catch(() => done("（读取失败）"));
}

// ── 打开 / 关闭 ──────────────────────────────────────────────────────────────────────────────────────
function persistUi() {
  try { localStorage.setItem(UI_KEY, JSON.stringify(ui)); } catch (e) { /* 不存就不存 */ }
}

function open() {
  ui.open = true;
  persistUi();
  view.setOpen(true);
  view.focusInput();                                       // 焦点放进工作台：打开后直接打字；不然焦点还停在刚点的图标 / 页面上，按键会落到 ComfyUI 的快捷键上
  if (!catalogApps) loadCatalog();
}

// 工作台开着、焦点却在工作台外面时按 Ctrl+Enter：那是 ComfyUI 的「排队运行」，会把后面那张节点图（可能是你自己的工作流，可能要花钱）跑起来。拦下来
function guardQueueKey(e) {
  if (!view || !view.isOpen() || e.key !== "Enter" || !(e.ctrlKey || e.metaKey) || (e.target instanceof Node && view.root.contains(e.target))) return;
  e.stopPropagation();
  e.preventDefault();
}

function close() {
  ui.open = false;
  persistUi();
  view.setOpen(false);
}

const toggleTab = () => app.extensionManager.sidebarTab.toggleSidebarTab(TAB_ID);

// 左边图标栏里的「AI 工作台」图标：点一下 = 打开大窗口（侧栏面板里只放一个说明，打开后立刻收起面板）
function launcher(el) {
  el.style.height = "100%";
  el.replaceChildren();
  const box = document.createElement("div");
  box.style.cssText = "padding:14px;display:flex;flex-direction:column;gap:10px;font-size:13px";
  const p = document.createElement("div");
  p.textContent = "AI 工作台已经改成大窗口：说一句话排好步骤，每步的设置都能改，一键跑完。";
  const b = document.createElement("button");
  b.textContent = "打开工作台";
  b.style.cssText = "padding:6px 14px;border-radius:6px;border:none;background:var(--p-primary-color,#3b82f6);color:#fff;cursor:pointer;font:inherit";
  const openAndCollapse = () => { open(); if (app.extensionManager.sidebarTab.activeSidebarTabId === TAB_ID) toggleTab(); view.focusInput(); };       // 收起侧栏面板会把焦点带走，最后再放回输入框
  b.onclick = openAndCollapse;
  box.append(p, b);
  el.append(box);
  setTimeout(openAndCollapse, 60);
}

// ── 注册 ─────────────────────────────────────────────────────────────────────────────────────────────
const actions = {
  examples: EXAMPLES,
  send, attach, onPaste, onDrop, uploadForField, changed, open, close, clearAll, clearPlan, runAll, runStep, stop, addStep, removeStep, openInApp, copyText, loadText,
  mention: (n) => { view.insertText(`素材 ${n} `); },
};

function buildView() {
  document.querySelectorAll(".pcw, .pcw-handle").forEach((el) => el.remove());      // 重试时别留下上一次建到一半的界面
  view = createView({ state, url, actions, catalog: () => catalogApps });
  view.renderAll();
}

app.registerExtension({
  name: "Pro.Chat",
  async setup() {
    load();
    try {
      buildView();
    } catch (e) {                                           // 存档里的数据不对头把界面画崩了：丢掉存档、从空白开始，别让工作台一直打不开
      console.error("[pro-chat] 存档数据有问题，已清掉重来：", e);
      try { localStorage.removeItem(STORE_KEY); } catch (e2) { /* 同上 */ }
      Object.assign(state, { messages: [], assets: [], plan: null, draft: [], chain: null, busy: false, running: null });
      buildView();
    }
    api.addEventListener("executing", onExecuting);
    window.addEventListener("storage", onStorage);
    window.addEventListener("keydown", guardQueueKey, true);
    window.addEventListener("pagehide", () => { saveNow(); releaseLock(); });            // 刚改完就刷新 / 关页面：存档有 300 毫秒的延迟，这里立刻补存；运行记录也马上放掉（Web Locks 的锁浏览器会自己放；没有它时，别的标签页不用干等记录过期）
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") saveNow(); });
    resumeRunning();
    setInterval(resumeRunning, 7000);                       // 另一个标签页在跑却突然没了（关了 / 崩了）：它的锁一放，这里接手，免得步骤一直卡在「运行中」
    app.extensionManager.registerSidebarTab({
      id: TAB_ID,
      icon: "pi pi-comments",
      title: "AI 工作台",
      tooltip: "AI 工作台：说一句话排好步骤，一键出图 / 视频 / 配音 / 成片",
      type: "custom",
      render: launcher,
    });
    view.setOpen(ui.open !== false);
    if (ui.open !== false && (!document.activeElement || document.activeElement === document.body)) view.focusInput();      // 默认打开：焦点还在空白处就放进输入框；已经在别处（比如 ComfyUI 的对话框）就不去抢
    loadCatalog();
  },
});

// 给测试 / 调试用：控制台里可以 window.__proChat.state 看状态
window.__proChat = { state, view: () => view, actions, lib };
