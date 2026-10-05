// AI 助手：ComfyUI 左侧栏的聊天标签页（后端见 ../__init__.py）。
// 说一句话（可以附商品图）→ AI 选应用、选模式、写好字段，给出「方案卡片」→ 你可以改 → 点「生成」→ 后端把工作流提交到队列，
// 结果显示在对话里。不碰你的画布；想接着手动调，点「在应用里打开」。
// 纯函数在 pro-chat-lib.js（node 里有测试）；这里只管界面和接 ComfyUI。所有文字都用 textContent 写进页面，模型的输出不会被当成 HTML。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import * as lib from "./pro-chat-lib.js";

const STORE_KEY = "proChat.v1";
const HISTORY_POLL_MS = 2000;
const RUN_TIMEOUT_MS = 15 * 60 * 1000;
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const EXAMPLES = [
  "帮我写一段红苹果的淘宝标题和卖点",
  "做一张夏日清凉节促销海报，满199减50，限时三天",
  "给我 30 秒舒缓钢琴配乐，适合产品视频",
  "把「夏日清凉，限时三天」念成语音，声音温暖一点",
  "一只橘猫在草地上奔跑的视频，配上配音和字幕",
  "（先点「附图」传一张商品图）把这张图的背景换成纯白色",
];

const state = { messages: [], images: [], plan: null, draft: [], busy: false, running: null };
let seq = 0;
const nextId = () => `${Date.now().toString(36)}-${++seq}`;
let root, listEl, inputEl, sendBtn, draftEl, statusEl, fileInput, tickTimer, saveTimer, busyTimer;
let sendToken = 0;

// ── 小工具 ───────────────────────────────────────────────────────────────────────────────────────────
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "value") el.value = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const url = (path) => api.apiURL(path);
const post = (route, body) => api.fetchApi(route, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

async function readJson(r) {
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `请求失败（HTTP ${r.status}）`);
  return d;
}

function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try { localStorage.setItem(STORE_KEY, lib.toStorage(state)); } catch (e) { /* 隐私模式 / 配额：不存就不存 */ }
  }, 300);
}

function load() {
  try {
    const d = lib.fromStorage(localStorage.getItem(STORE_KEY));
    if (d) Object.assign(state, d);
  } catch (e) { /* 读不了就从空白开始 */ }
}

// ── 样式 ─────────────────────────────────────────────────────────────────────────────────────────────
const CSS = `
.pc-root{display:flex;flex-direction:column;height:100%;min-height:0;font-size:13px;color:var(--fg-color,#ddd)}
.pc-head{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border-bottom:1px solid var(--border-color,#4e4e4e)}
.pc-head b{font-size:14px}
.pc-link{background:none;border:none;color:var(--fg-color,#bbb);opacity:.7;cursor:pointer;font-size:12px}
.pc-link:hover{opacity:1;text-decoration:underline}
.pc-list{flex:1;min-height:0;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:10px}
.pc-msg{max-width:94%;padding:8px 10px;border-radius:10px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.pc-user{align-self:flex-end;background:var(--p-primary-color,#3b82f6);color:#fff}
.pc-ai{align-self:flex-start;background:var(--comfy-input-bg,#2a2a2a);border:1px solid var(--border-color,#4e4e4e)}
.pc-err{color:#ff6b6b}
.pc-muted{opacity:.65;font-size:12px}
.pc-warn{opacity:.7;font-size:12px;color:#e0b050}
.pc-imgs{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.pc-thumb{width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid var(--border-color,#4e4e4e)}
.pc-plan{margin-top:8px;padding:8px;border:1px solid var(--p-primary-color,#3b82f6);border-radius:8px;background:rgba(59,130,246,.07);white-space:normal}
.pc-plan-title{font-weight:600;margin-bottom:2px}
.pc-plan-note{opacity:.75;font-size:12px;margin-bottom:6px}
.pc-label{display:block;margin:8px 0 3px;font-size:12px;opacity:.8}
.pc-plan textarea,.pc-plan select{width:100%;box-sizing:border-box;background:var(--comfy-input-bg,#222);color:var(--input-text,#ddd);border:1px solid var(--border-color,#4e4e4e);border-radius:6px;padding:5px 6px;font:inherit}
.pc-plan textarea{resize:vertical;min-height:44px}
.pc-plan details{margin-top:8px}
.pc-plan summary{cursor:pointer;opacity:.75;font-size:12px}
.pc-btns{display:flex;gap:8px;margin-top:10px;align-items:center;flex-wrap:wrap}
.pc-btn{padding:5px 14px;border-radius:6px;border:1px solid var(--border-color,#4e4e4e);background:var(--comfy-input-bg,#2a2a2a);color:inherit;cursor:pointer;font:inherit}
.pc-btn:hover{filter:brightness(1.2)}
.pc-btn[disabled]{opacity:.45;cursor:not-allowed}
.pc-btn-primary{background:var(--p-primary-color,#3b82f6);border-color:transparent;color:#fff}
.pc-status{font-size:12px;opacity:.85}
.pc-result{align-self:stretch;padding:8px 10px;border:1px solid var(--border-color,#4e4e4e);border-radius:10px;background:var(--comfy-input-bg,#222)}
.pc-result h4{margin:0 0 6px;font-size:13px}
.pc-img{display:block;width:100%;max-height:480px;object-fit:contain;border-radius:6px;margin:6px 0;cursor:zoom-in}
.pc-media{display:block;width:100%;max-height:480px;margin:6px 0;border-radius:6px;background:#000}
.pc-text{margin:6px 0;padding:6px 8px;border-radius:6px;background:rgba(127,127,127,.12)}
.pc-text .pc-tl{display:flex;justify-content:space-between;align-items:center;font-size:12px;opacity:.8;margin-bottom:2px}
.pc-text pre{margin:0;white-space:pre-wrap;word-break:break-word;font:inherit}
.pc-empty{opacity:.85;line-height:1.7}
.pc-chips{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.pc-chip{text-align:left;padding:6px 10px;border-radius:8px;border:1px dashed var(--border-color,#666);background:none;color:inherit;cursor:pointer;font:inherit}
.pc-chip:hover{background:rgba(127,127,127,.15)}
.pc-input{border-top:1px solid var(--border-color,#4e4e4e);padding:8px 10px}
.pc-drafts{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.pc-draft{position:relative}
.pc-draft button{position:absolute;top:-6px;right:-6px;width:18px;height:18px;border-radius:9px;border:none;background:#555;color:#fff;cursor:pointer;line-height:16px;padding:0;font-size:12px}
.pc-row{display:flex;gap:6px;align-items:flex-end}
.pc-row textarea{flex:1;box-sizing:border-box;resize:none;max-height:140px;background:var(--comfy-input-bg,#222);color:var(--input-text,#ddd);border:1px solid var(--border-color,#4e4e4e);border-radius:8px;padding:7px 8px;font:inherit}
.pc-drop{outline:2px dashed var(--p-primary-color,#3b82f6);outline-offset:-4px}
.pc-handle{position:fixed;left:56px;top:34%;z-index:900;display:none;writing-mode:vertical-rl;letter-spacing:2px;padding:14px 7px;border:none;border-radius:0 8px 8px 0;background:var(--p-primary-color,#3b82f6);color:#fff;cursor:pointer;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,.4)}
.pc-handle:hover{filter:brightness(1.15)}
`;

// ── 界面 ─────────────────────────────────────────────────────────────────────────────────────────────
function build() {
  if (!document.getElementById("pro-chat-style")) {
    const st = document.createElement("style");
    st.id = "pro-chat-style";
    st.textContent = CSS;
    document.head.append(st);
  }
  listEl = h("div", { class: "pc-list" });
  draftEl = h("div", { class: "pc-drafts" });
  statusEl = h("div", { class: "pc-muted", style: "min-height:16px;margin:0 0 4px" });
  inputEl = h("textarea", { rows: "2", placeholder: "说你想做什么，比如：做一张夏日促销海报，满199减50", onkeydown: (e) => { if (lib.isEnterToSend(e)) { e.preventDefault(); send(); } }, oninput: autoGrow, onpaste: onPaste });
  sendBtn = h("button", { class: "pc-btn pc-btn-primary", text: "发送", onclick: () => send() });
  fileInput = h("input", { type: "file", accept: "image/*", multiple: true, style: "display:none", onchange: () => { attach([...fileInput.files]); fileInput.value = ""; } });
  const attachBtn = h("button", { class: "pc-btn", title: "附图：上传商品图等（可多张，也可以直接粘贴 / 拖进来）", text: "附图", onclick: () => fileInput.click() });
  root = h("div", { class: "pc-root", ondragover: (e) => { e.preventDefault(); e.stopPropagation(); root.classList.add("pc-drop"); }, ondragleave: () => root.classList.remove("pc-drop"), ondrop: onDrop },
    h("div", { class: "pc-head" }, h("b", { text: "AI 助手" }),
      h("span", null, h("button", { class: "pc-link", text: "清空对话", onclick: clearAll }), h("button", { class: "pc-link", style: "margin-left:10px", text: "收起", title: "关闭这个面板（应用模式下左边没有图标可点）", onclick: toggleTab }))),
    listEl,
    h("div", { class: "pc-input" }, draftEl, statusEl, h("div", { class: "pc-row" }, inputEl, h("div", { style: "display:flex;flex-direction:column;gap:6px" }, attachBtn, sendBtn)), fileInput));
  renderList();
}

function autoGrow() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(140, inputEl.scrollHeight) + "px";
}

// 等 AI 回复：显示已等了多久（到阿里的线路偶尔会卡十几秒，没有计时看起来像死了）
function setBusy(on) {
  state.busy = on;
  sendBtn.disabled = on;
  clearInterval(busyTimer);
  if (!on) { statusEl.textContent = ""; return; }
  const t0 = Date.now();
  const draw = () => { statusEl.textContent = `AI 正在想… ${lib.elapsedText(Date.now() - t0)}`; };
  draw();
  busyTimer = setInterval(draw, 1000);
}

function stickToBottom(fn) {
  const near = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < 80;
  fn();
  if (near) listEl.scrollTop = listEl.scrollHeight;
}

function renderList() {
  stickToBottom(() => {
    listEl.replaceChildren();
    if (!state.messages.length) {
      listEl.append(h("div", { class: "pc-empty" },
        h("div", { text: "你好，我是 AI 助手。说一句话，我来选工作流、写好各项设置，你确认后一键生成，结果直接显示在这里。可以附商品图。" }),
        h("div", { class: "pc-chips" }, EXAMPLES.map((t) => h("button", { class: "pc-chip", text: t, onclick: () => { inputEl.value = t.replace(/^（先点「附图」传一张商品图）/, ""); autoGrow(); inputEl.focus(); } })))));
    }
    for (const m of state.messages) listEl.append(renderMessage(m));
  });
}

function renderMessage(m) {
  if (m.role === "user") {
    return h("div", { class: "pc-msg pc-user" }, m.text, (m.images || []).length ? h("div", { class: "pc-imgs" }, m.images.map((n) => h("img", { class: "pc-thumb", src: url(lib.inputImagePath(n)), loading: "lazy" }))) : null);
  }
  if (m.role === "error") return h("div", { class: "pc-msg pc-ai pc-err" }, m.text);
  if (m.role === "note") return h("div", { class: "pc-muted", style: "align-self:center", text: m.text });
  if (m.role === "result") return renderResult(m);
  return h("div", { class: "pc-msg pc-ai" }, m.text, (m.warnings || []).map((w) => h("div", { class: "pc-warn", text: "（" + w + "）" })), m.plan ? renderPlan(m) : null);
}

function renderPlan(m) {
  const p = m.plan, sc = p.schema || {};
  const live = lib.isLivePlanState(m.planState);           // 等确认的，以及上次没成功 / 被停止 / 刷新时没跟上的（可以改了再试）
  const retry = live && !!m.planState && m.planState !== "open";
  const frozen = !live;
  p.fields = p.fields || {};
  p.images = p.images || {};
  const box = h("div", { class: "pc-plan" }, h("div", { class: "pc-plan-title", text: `应用 ${p.workflow}「${p.name}」` }), p.note ? h("div", { class: "pc-plan-note", text: p.note }) : null);
  const body = h("div");                                   // 设置区；方案不再有效（已生成 / 放弃…）后折叠起来，免得对话越来越长
  if (sc.mode) {
    const sel = h("select", { disabled: frozen, onchange: () => { p.mode = sel.value; save(); } },
      sc.mode.options.map((o) => h("option", { value: o, text: o, selected: o === (p.mode || sc.mode.default) })));
    body.append(h("label", { class: "pc-label", text: sc.mode.label || "模式" }), sel);
  }
  const editor = (f) => {
    const wrap = document.createDocumentFragment();
    wrap.append(h("label", { class: "pc-label", text: f.label }));
    if (f.kind === "choice") {
      const sel = h("select", { disabled: frozen, onchange: () => { if (sel.value) p.fields[f.key] = sel.value; else delete p.fields[f.key]; save(); } },
        h("option", { value: "", text: "（默认）" }), f.options.map((o) => h("option", { value: o, text: o, selected: o === p.fields[f.key] })));
      wrap.append(sel);
    } else {
      const ta = h("textarea", { rows: "2", disabled: frozen, placeholder: "（用应用里的默认）", value: p.fields[f.key] || "", oninput: () => { if (ta.value.trim()) p.fields[f.key] = ta.value; else delete p.fields[f.key]; save(); } });
      wrap.append(ta);
    }
    return wrap;
  };
  const imageEditor = (im) => {
    const pic = h("img", { class: "pc-thumb", style: "margin-top:4px" });
    const show = () => { if (p.images[im.key]) { pic.src = url(lib.inputImagePath(p.images[im.key])); pic.style.display = ""; } else pic.style.display = "none"; };
    const sel = h("select", { disabled: frozen, onchange: () => { if (sel.value) p.images[im.key] = sel.value; else delete p.images[im.key]; show(); save(); } },
      h("option", { value: "", text: "（用默认演示图）" }), state.images.map((n) => h("option", { value: n, text: n.split("/").pop(), selected: n === p.images[im.key] })));
    show();
    const wrap = document.createDocumentFragment();
    wrap.append(h("label", { class: "pc-label", text: im.label }), sel, pic);
    return wrap;
  };
  // AI 填了的先摆出来；没填的（包括这次用不到的图片位）收进「更多设置」
  const fields = Object.values(sc.fields || {});
  const images = Object.values(sc.images || {});
  fields.filter((f) => f.key in p.fields).forEach((f) => body.append(editor(f)));
  images.filter((im) => im.key in p.images).forEach((im) => body.append(imageEditor(im)));
  const more = [...fields.filter((f) => !(f.key in p.fields)).map(editor), ...images.filter((im) => !(im.key in p.images)).map(imageEditor)];
  if (more.length) body.append(h("details", null, h("summary", { text: "更多设置（不改就用默认）" }), more));
  if ((sc.uploads || []).length) {
    body.append(h("div", { class: "pc-warn", style: "margin-top:8px", text: "聊天里传不了" + sc.uploads.map((u) => `「${u.label}」`).join("、") + "，用的是应用里的默认文件；要用自己的，请点「在应用里打开」上传。" }));
  }
  box.append(frozen ? h("details", null, h("summary", { text: "查看当时的设置" }), body) : body);
  const status = h("span", { class: "pc-status" });
  const btns = h("div", { class: "pc-btns" });
  if (live) {
    btns.append(h("button", { class: "pc-btn pc-btn-primary", text: retry ? "再试一次" : "生成", disabled: !!state.running, onclick: () => runPlan(m) }),
      h("button", { class: "pc-btn", text: "在应用里打开", onclick: () => openInApp(p) }),
      h("button", { class: "pc-btn", text: "放弃", onclick: () => dismiss(m) }));
    if (retry) { status.textContent = { failed: "上次没有成功", stopped: "上次已停止", interrupted: "页面刷新时没跟上" }[m.planState] || ""; btns.append(status); }
  } else if (m.planState === "running") {
    if (state.running && state.running.msgId === m.id) state.running.statusEl = status;
    btns.append(status, h("button", { class: "pc-btn", text: "停止", onclick: stopRun }));
    tick();
  } else {
    status.textContent = { done: "已生成", failed: "没有成功", stopped: "已停止", dismissed: "已放弃", superseded: "已被新方案替代", interrupted: "页面刷新时中断了" }[m.planState] || "";
    btns.append(status);
  }
  box.append(btns);
  return box;
}

function renderResult(m) {
  const box = h("div", { class: "pc-result" }, h("h4", { text: `结果 · ${m.name}${m.elapsed ? "（用时 " + lib.elapsedText(m.elapsed) + "）" : ""}` }));
  for (const it of m.items || []) {
    const src = url(lib.viewPath(it.file));
    if (it.kind === "image") box.append(h("img", { class: "pc-img", src, loading: "lazy", onclick: () => window.open(src, "_blank") }));
    else if (it.kind === "video") box.append(h("video", { class: "pc-media", src, controls: true, preload: "metadata" }));
    else if (it.kind === "audio") box.append(h("audio", { class: "pc-media", src, controls: true, preload: "metadata" }));
    else if (it.kind === "text") {
      const pre = h("pre", { text: it.text != null ? it.text : "加载中…" });
      const copy = navigator.clipboard && !it.isError ? h("button", { class: "pc-link", text: "复制", onclick: (e) => copyText(pre.textContent, e.target) }) : null;
      box.append(h("div", { class: "pc-text" + (it.isError ? " pc-err" : "") }, it.label || copy ? h("div", { class: "pc-tl" }, h("span", { text: it.label || "" }), copy) : null, pre));
      if (it.text == null) {
        fetch(src).then((r) => r.text()).then((t) => { it.text = t; pre.textContent = t; save(); }).catch(() => { pre.textContent = "（读取失败）"; });
      }
    }
  }
  if (m.error) box.append(h("div", { class: "pc-err", text: m.error }));
  if (!(m.items || []).length && !m.error) box.append(h("div", { class: "pc-muted", text: "没有产出结果（可能被跳过了）；可以在「应用里打开」看看。" }));
  (m.warnings || []).forEach((w) => box.append(h("div", { class: "pc-warn", text: "（" + w + "）" })));
  const src = state.messages.find((x) => x.id === m.planMsgId);
  if (src && src.plan) {
    box.append(h("div", { class: "pc-btns" },
      h("button", { class: "pc-btn", text: "再来一次", disabled: !!state.running, onclick: () => runPlan(src) }),
      h("button", { class: "pc-btn", text: "在应用里打开", onclick: () => openInApp(src.plan) })));
  }
  return box;
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => { btn.textContent = "已复制"; }, () => { btn.textContent = "复制失败"; }).finally(() => setTimeout(() => { btn.textContent = "复制"; }, 1500));
}

// ── 动作 ─────────────────────────────────────────────────────────────────────────────────────────────
function addMessage(m) {
  m.id = m.id || nextId();
  state.messages.push(m);
  renderList();
  listEl.scrollTop = listEl.scrollHeight;
  save();
  return m;
}

function clearAll() {
  if (state.running) return;
  sendToken++;                                             // 还在等的 AI 回复作废
  setBusy(false);
  state.messages = []; state.images = []; state.plan = null; state.draft = [];
  renderList(); renderDrafts(); save();
}

function dismiss(m) {
  m.planState = "dismissed";
  if (state.plan === m.plan) state.plan = null;
  renderList(); save();
}

async function send() {
  const text = inputEl.value.trim();
  if ((!text && !state.draft.length) || state.busy) return;
  const attached = state.draft.splice(0);
  const { images, newIdx } = lib.addImages(state.images, attached.map((a) => a.name));
  state.images = images;
  inputEl.value = ""; autoGrow(); renderDrafts();
  addMessage({ role: "user", text: text || "（附了图片）", images: attached.map((a) => a.name) });
  const token = ++sendToken;
  setBusy(true);
  try {
    const d = await readJson(await post("/pro/chat", { messages: lib.apiMessages(state.messages, state.images), images: state.images, new_images: newIdx, plan: lib.serverPlan(state.plan) }));
    if (token !== sendToken) return;                       // 等回复的时候点了「清空对话」：这条回复不要了
    const m = { role: "assistant", text: d.reply, plan: d.plan || null, planState: d.plan ? "open" : undefined, warnings: d.warnings || [] };
    if (d.plan) {
      for (const o of state.messages) if (o.plan && lib.isLivePlanState(o.planState)) o.planState = "superseded";
      state.plan = d.plan;
    }
    addMessage(m);
  } catch (e) {
    if (token === sendToken) addMessage({ role: "error", text: "出错了：" + (e.message || e) });
  } finally {
    if (token === sendToken) setBusy(false);
  }
}

// 附图：先上传到 input/助手/，成功后才出现在输入框上方
async function attach(files) {
  for (const f of files) {
    if (!f.type.startsWith("image/")) { statusEl.textContent = `「${f.name}」不是图片，已跳过`; continue; }
    if (f.size > MAX_IMAGE_BYTES) { statusEl.textContent = `「${f.name}」超过 20MB，已跳过`; continue; }
    statusEl.textContent = `正在上传 ${f.name || "图片"} …`;
    try {
      const body = new FormData();
      body.append("image", f, f.name || "image.png");
      body.append("type", "input");
      body.append("subfolder", "助手");
      body.append("overwrite", "false");
      const d = await readJson(await api.fetchApi("/upload/image", { method: "POST", body }));
      state.draft.push({ name: d.subfolder ? `${d.subfolder}/${d.name}` : d.name });
      statusEl.textContent = "";
    } catch (e) {
      statusEl.textContent = "上传失败：" + (e.message || e);
    }
  }
  renderDrafts();
}

function renderDrafts() {
  draftEl.replaceChildren(...state.draft.map((d, i) => h("div", { class: "pc-draft" }, h("img", { class: "pc-thumb", src: url(lib.inputImagePath(d.name)) }),
    h("button", { title: "移除", text: "×", onclick: () => { state.draft.splice(i, 1); renderDrafts(); } }))));
}

function onPaste(e) {
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (files.length) { e.preventDefault(); e.stopPropagation(); attach(files); }
}

function onDrop(e) {
  e.preventDefault();
  e.stopPropagation();                      // 拖进聊天面板的文件只给聊天用，不让别的拖入处理（ComfyUI / 其他扩展）再处理一遍
  root.classList.remove("pc-drop");
  attach([...(e.dataTransfer?.files || [])]);
}

// 运行：后端提交到队列 → 等 /history 里出现结果（事件 + 轮询）→ 结果卡片
function tick() {
  clearInterval(tickTimer);
  const r = state.running;
  if (!r) return;
  const draw = () => {
    if (!state.running) { clearInterval(tickTimer); return; }
    if (r.statusEl && r.statusEl.isConnected) r.statusEl.textContent = `生成中… 已 ${lib.elapsedText(Date.now() - r.start)}${r.node ? " · " + lib.shortTitle(r.node) : ""}`;
  };
  draw();
  tickTimer = setInterval(draw, 1000);
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

async function runPlan(m) {
  if (state.running) return;
  const started = Date.now();
  state.running = { msgId: m.id, start: started, node: "", promptId: null, info: null, statusEl: null };
  m.planState = "running";
  renderList();
  try {
    const d = await readJson(await post("/pro/run", lib.runPayload(m.plan, api.clientId)));
    // 提交成功就把 prompt_id 记进消息：这时刷新网页也能接着等（resumeRun），长任务（视频要几分钟）不会因为刷新丢结果
    m.run = { promptId: d.prompt_id, name: d.name, outputs: d.outputs, titles: d.titles, warnings: d.warnings || [], started };
    Object.assign(state.running, { promptId: d.prompt_id, info: m.run });
    save();
  } catch (e) {
    state.running = null;
    m.planState = "failed";
    addMessage({ role: "error", text: "运行失败：" + (e.message || e) + `（用时 ${lib.elapsedText(Date.now() - started)}）` });
    renderList();
    save();
    return;
  }
  await finishRun(m);
}

// 等这次运行出结果，把结果（或停止 / 失败）记进对话。m.run 里有 prompt_id 和结果节点 id。
async function finishRun(m) {
  const run = m.run;
  try {
    const entry = await waitFinished(run.promptId);
    state.running = null;
    if (lib.wasInterrupted(entry)) {
      m.planState = "stopped";
      addMessage({ role: "note", text: `已停止「${run.name}」（用时 ${lib.elapsedText(Date.now() - run.started)}）` });
    } else {
      const items = lib.historyItems(entry, run.outputs);
      const error = lib.historyError(entry, run.titles);
      m.planState = lib.runOutcome(items, error);
      addMessage({ role: "result", planMsgId: m.id, name: run.name, items, error, warnings: run.warnings || [], elapsed: Date.now() - run.started });
    }
  } catch (e) {
    state.running = null;
    m.planState = "failed";
    addMessage({ role: "error", text: "运行失败：" + (e.message || e) + `（用时 ${lib.elapsedText(Date.now() - run.started)}）` });
  }
  delete m.run;
  renderList();
  save();
}

// 刷新网页时还在跑的方案（已经拿到 prompt_id）：接着等结果；没拿到的（提交那一刻刷新了）标成已中断
function resumeRun() {
  const running = state.messages.filter((m) => m.planState === "running");
  const keep = running.filter((m) => m.run && m.run.promptId).pop();
  for (const m of running) if (m !== keep) { m.planState = "interrupted"; delete m.run; }
  if (!keep) return;
  state.running = { msgId: keep.id, start: keep.run.started, node: "", promptId: keep.run.promptId, info: keep.run, statusEl: null };
  renderList();
  finishRun(keep);
}

async function stopRun() {
  const r = state.running;
  if (!r || !r.promptId) return;
  try { await post("/interrupt", { prompt_id: r.promptId }); } catch (e) { /* 已经结束了 */ }
}

// 「在应用里打开」：把这个应用载入成一个新标签页，并把方案里的设置填进去，方便接着手动调 / 上传自己的视频音频
async function openInApp(p) {
  try {
    const wf = await readJson(await api.fetchApi("/userdata/" + encodeURIComponent("workflows/" + p.path)));
    await app.loadGraphData(wf, true, true, `助手 · ${p.name}`);
    const set = (key, value) => {
      const i = key.indexOf(":");
      const node = app.graph.getNodeById(Number(key.slice(0, i)));
      const w = node && node.widgets && node.widgets.find((x) => x.name === key.slice(i + 1));
      if (!w) return;
      const vals = w.options && w.options.values;
      if (Array.isArray(vals) && !vals.includes(value)) w.options.values = [...vals, value];   // 聊天里传的图在 input/助手/ 下，不在下拉里；补进去，前端才不会提示「缺少媒体」
      w.value = value;
      if (w.callback) w.callback(value);
    };
    if (p.mode && p.schema && p.schema.mode) set(p.schema.mode.key, p.mode);
    Object.entries(p.fields || {}).forEach(([k, v]) => set(k, v));
    Object.entries(p.images || {}).forEach(([k, v]) => set(k, v));
    app.graph.setDirtyCanvas(true, true);
  } catch (e) {
    addMessage({ role: "error", text: "打开失败：" + (e.message || e) });
  }
}

function onExecuting(e) {
  const r = state.running;
  if (!r) return;
  const d = e.detail;
  const nid = d && typeof d === "object" ? d.node : d;
  const pid = d && typeof d === "object" ? d.prompt_id : null;
  if (pid && r.promptId && pid !== r.promptId) return;
  if (nid && r.info) r.node = (r.info.titles || {})[String(nid)] || "";
}

// ── 挂载位置 ─────────────────────────────────────────────────────────────────────────────────────────
// 根节点只建一次，但 ComfyUI 会先后（有时同时）给我们几个容器：画布模式的侧栏、应用模式的左栏。
// 记下所有容器，哪个看得见就放进哪个；切换模式后旧容器被销毁，自动换到还在的那个。
const hosts = new Set();
const visible = (el) => el.isConnected && el.getClientRects().length > 0;
const TAB_ID = "pro-chat";
const toggleTab = () => app.extensionManager.sidebarTab.toggleSidebarTab(TAB_ID);
let handle;

function place() {
  if (visible(root)) return;
  for (const el of [...hosts].reverse()) {
    if (!el.isConnected) hosts.delete(el);
    else if (visible(el)) { el.append(root); listEl.scrollTop = listEl.scrollHeight; return; }
  }
}

// 应用模式下前端把左侧图标栏写死成只有「素材」「应用」，看不到我们的标签页，所以那时在左边缘放一个按钮；
// 画布模式左栏里有我们的图标，不需要。应用模式的判断：画布容器被隐藏了。
function syncHandle() {
  const canvas = document.querySelector(".graph-canvas-container");
  const appMode = !!canvas && canvas.getClientRects().length === 0;
  const open = !!app.extensionManager.sidebarTab.activeSidebarTabId;
  handle.style.display = appMode && !open ? "block" : "none";
}

// ── 注册侧栏标签页 ───────────────────────────────────────────────────────────────────────────────────
app.registerExtension({
  name: "Pro.Chat",
  async setup() {
    load();
    build();
    api.addEventListener("executing", onExecuting);
    resumeRun();
    app.extensionManager.registerSidebarTab({
      id: TAB_ID,
      icon: "pi pi-comments",
      title: "AI 助手",
      tooltip: "AI 助手：说一句话，自动选工作流并运行",
      type: "custom",
      render: (el) => {
        el.style.height = "100%";
        hosts.add(el);
        el.append(root);
        listEl.scrollTop = listEl.scrollHeight;
        setTimeout(place, 300);                // 同时有两个容器时，最后一个可能是看不见的那个
      },
    });
    handle = h("button", { class: "pc-handle", text: "AI 助手", title: "AI 助手：说一句话，自动选工作流并运行", onclick: toggleTab });
    document.body.append(handle);
    setInterval(() => { place(); syncHandle(); }, 500);
  },
});

// 给测试 / 调试用：控制台里可以 window.__proChat.state 看状态
window.__proChat = { state, send, attach, runPlan, openInApp, lib };
