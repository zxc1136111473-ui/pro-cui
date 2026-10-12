// AI 工作台的界面（DOM）：只管把状态画出来、把操作交给 actions；不接 ComfyUI、不发请求，所以用一个假页面也能跑起来看。
// 布局：左边聊天（说一句话、附素材），右边方案（一步一张卡：设置 + 运行 + 结果）和素材库。
// 所有文字都用 textContent 写进页面，模型的输出不会被当成 HTML。
import * as lib from "./pro-chat-lib.js";

export function h(tag, props, ...kids) {
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

export const CSS = `
.pcw{position:fixed;inset:0;z-index:1400;display:none;flex-direction:column;background:var(--bg-color,#202020);color:var(--fg-color,#ddd);font-size:13px}
.pcw.pcw-open{display:flex}
.pcw *{box-sizing:border-box}
.pcw:focus{outline:none}
.pcw-top{display:flex;align-items:center;gap:12px;padding:8px 14px;border-bottom:1px solid var(--border-color,#4e4e4e);background:var(--comfy-menu-bg,#353535)}
.pcw-top b{font-size:15px}
.pcw-sub{opacity:.6;font-size:12px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pcw-main{flex:1;min-height:0;display:grid;grid-template-columns:minmax(300px,400px) minmax(0,1fr)}
.pcw-left{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--border-color,#4e4e4e)}
.pcw-right{display:flex;flex-direction:column;min-height:0;min-width:0}
@media (max-width:900px){.pcw-main{grid-template-columns:1fr;grid-template-rows:minmax(220px,42%) minmax(0,1fr)}.pcw-left{border-right:none;border-bottom:1px solid var(--border-color,#4e4e4e)}}
.pcw-link{background:none;border:none;color:inherit;opacity:.7;cursor:pointer;font:inherit;font-size:12px;padding:0 2px}
.pcw-link:hover{opacity:1;text-decoration:underline}
.pcw-link[disabled]{opacity:.35;cursor:not-allowed;text-decoration:none}
.pcw-btn{padding:5px 14px;border-radius:6px;border:1px solid var(--border-color,#4e4e4e);background:var(--comfy-input-bg,#2a2a2a);color:inherit;cursor:pointer;font:inherit;white-space:nowrap}
.pcw-btn:hover{filter:brightness(1.2)}
.pcw-btn[disabled]{opacity:.45;cursor:not-allowed}
.pcw-btn-primary{background:var(--p-primary-color,#3b82f6);border-color:transparent;color:#fff}
.pcw-btn-sm{padding:3px 10px;font-size:12px}
.pcw-list{flex:1;min-height:0;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:10px}
.pcw-msg{max-width:94%;padding:8px 10px;border-radius:10px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.pcw-user{align-self:flex-end;background:var(--p-primary-color,#3b82f6);color:#fff}
.pcw-ai{align-self:flex-start;background:var(--comfy-input-bg,#2a2a2a);border:1px solid var(--border-color,#4e4e4e)}
.pcw-err{color:#e0443e}
.pcw-muted{opacity:.65;font-size:12px}
.pcw-warn{opacity:.9;font-size:12px;color:#c77d00}
.pcw-planchip{margin-top:6px;padding:4px 8px;border-radius:6px;background:rgba(59,130,246,.15);font-size:12px;white-space:normal}
.pcw-atts{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.pcw-att{position:relative;display:inline-block}
.pcw-thumb{width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid var(--border-color,#4e4e4e)}
.pcw-filechip{display:inline-flex;align-items:center;gap:4px;max-width:180px;padding:6px 8px;border-radius:6px;border:1px solid var(--border-color,#4e4e4e);background:rgba(127,127,127,.15);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pcw-tag{position:absolute;left:2px;top:2px;padding:0 5px;border-radius:4px;background:rgba(0,0,0,.65);color:#fff;font-size:11px;line-height:16px}
.pcw-empty{opacity:.9;line-height:1.7}
.pcw-chips{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.pcw-chip{text-align:left;padding:6px 10px;border-radius:8px;border:1px dashed var(--border-color,#666);background:none;color:inherit;cursor:pointer;font:inherit}
.pcw-chip:hover{background:rgba(127,127,127,.15)}
.pcw-input{border-top:1px solid var(--border-color,#4e4e4e);padding:8px 10px}
.pcw-drafts{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.pcw-draft button{position:absolute;top:-6px;right:-6px;width:18px;height:18px;border-radius:9px;border:none;background:#555;color:#fff;cursor:pointer;line-height:16px;padding:0;font-size:12px}
.pcw-row{display:flex;gap:6px;align-items:flex-end}
.pcw-row textarea{flex:1;resize:none;max-height:140px;background:var(--comfy-input-bg,#222);color:var(--input-text,#ddd);border:1px solid var(--border-color,#4e4e4e);border-radius:8px;padding:7px 8px;font:inherit}
.pcw-modelrow{display:flex;align-items:center;gap:6px;margin-top:6px}
.pcw-modelrow select{flex:1;min-width:0;background:var(--comfy-input-bg,#222);color:var(--input-text,#ddd);border:1px solid var(--border-color,#4e4e4e);border-radius:6px;padding:3px 6px;font:inherit;font-size:12px}
.pcw-drop{outline:2px dashed var(--p-primary-color,#3b82f6);outline-offset:-4px}
.pcw-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid var(--border-color,#4e4e4e)}
.pcw-bar select{background:var(--comfy-input-bg,#222);color:var(--input-text,#ddd);border:1px solid var(--border-color,#4e4e4e);border-radius:6px;padding:4px 6px;font:inherit}
.pcw-bar .pcw-spacer{flex:1}
.pcw-notice{padding:6px 14px;font-size:12px;color:#c77d00;border-bottom:1px solid var(--border-color,#4e4e4e)}
.pcw-steps{flex:1;min-height:0;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:12px}
.pcw-step{border:1px solid var(--border-color,#4e4e4e);border-radius:10px;background:var(--comfy-menu-secondary-bg,#292929);padding:10px 12px}
.pcw-step-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.pcw-num{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;border-radius:11px;background:var(--p-primary-color,#3b82f6);color:#fff;font-size:12px}
.pcw-step-name{font-size:14px}
.pcw-fam{opacity:.7;font-size:12px;margin-right:6px}
.pcw-funcsel{font-size:14px;font-weight:600;max-width:240px}
.pcw-badge{padding:1px 8px;border-radius:10px;font-size:12px;background:rgba(127,127,127,.25)}
.pcw-b-running{background:rgba(59,130,246,.3)}
.pcw-b-done{background:rgba(34,197,94,.28)}
.pcw-b-stale{background:rgba(224,176,80,.3)}
.pcw-b-failed,.pcw-b-stopped,.pcw-b-interrupted{background:rgba(255,107,107,.28)}
.pcw-spacer{flex:1}
.pcw-note{margin:4px 0 0;opacity:.75;font-size:12px}
.pcw-settings{margin-top:8px}
.pcw-settings>summary{cursor:pointer;opacity:.8;font-size:12px}
.pcw-fields{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px 16px;margin-top:8px}
.pcw-field{display:flex;flex-direction:column;gap:3px;min-width:0}
.pcw-field.pcw-wide{grid-column:1 / -1}
.pcw-field.pcw-changed>label{color:#2b74e8}
.pcw-field>label{font-size:12px;opacity:.9;display:flex;gap:6px;align-items:center}
.pcw-hint{font-size:11px;opacity:.55;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.pcw-missing>label{color:#e0443e}
.pcw-missing select{border-color:#e0443e}
.pcw-field textarea,.pcw-field select,.pcw-field input[type=number],.pcw-field input[type=text]{width:100%;background:var(--comfy-input-bg,#222);color:var(--input-text,#ddd);border:1px solid var(--border-color,#4e4e4e);border-radius:6px;padding:5px 6px;font:inherit}
.pcw-field textarea{resize:vertical;min-height:44px}
.pcw-numrow{display:flex;gap:8px;align-items:center}
.pcw-numrow input[type=range]{flex:1;min-width:60px}
.pcw-numrow input[type=number]{width:84px;flex:none}
.pcw-check{display:flex;gap:6px;align-items:center;cursor:pointer}
.pcw-adv{margin-top:10px;grid-column:1 / -1}
.pcw-adv>summary{cursor:pointer;opacity:.75;font-size:12px}
.pcw-filerow{display:flex;gap:6px;align-items:center}
.pcw-filerow select{flex:1;min-width:0}
.pcw-filepic{margin-top:4px;align-self:flex-start;max-height:64px;max-width:100%;border-radius:6px;border:1px solid var(--border-color,#4e4e4e)}
.pcw-results{margin-top:10px;display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start}
.pcw-res{position:relative;max-width:100%}
.pcw-res img{display:block;max-width:100%;max-height:220px;border-radius:6px;cursor:zoom-in}
.pcw-res video{display:block;max-width:100%;max-height:360px;border-radius:6px;background:#000}
/* 抠图的结果是透明底：垫一层灰白棋盘格（常见的透明提示），不然透明的地方显示成黑底，看着像没抠干净 */
.pcw-thumb,.pcw-res img,.pcw-filepic{background:#e8e8e8 repeating-conic-gradient(#c6c6c6 0% 25%, #e8e8e8 0% 50%) 0 0 / 14px 14px}
.pcw-res audio{display:block;width:280px;max-width:100%}
.pcw-resbar{display:flex;gap:8px;align-items:center;margin-top:3px;font-size:12px;opacity:.85}
.pcw-text{flex:1 1 100%;padding:6px 8px;border-radius:6px;background:rgba(127,127,127,.12)}
.pcw-text .pcw-tl{display:flex;justify-content:space-between;align-items:center;font-size:12px;opacity:.8;margin-bottom:2px}
.pcw-text pre{margin:0;white-space:pre-wrap;word-break:break-word;font:inherit}
.pcw-assets{border-top:1px solid var(--border-color,#4e4e4e);padding:8px 14px}
.pcw-assets-head{font-size:12px;opacity:.75;margin-bottom:6px}
.pcw-assets-list{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px}
.pcw-asset{position:relative;flex:none;cursor:pointer}
.pcw-asset .pcw-thumb,.pcw-asset .pcw-filechip{width:72px;height:72px;margin:0}
.pcw-asset .pcw-filechip{display:flex;align-items:center;justify-content:center;white-space:normal;text-align:center}
.pcw-handle{position:fixed;left:56px;top:34%;z-index:1100;display:none;writing-mode:vertical-rl;letter-spacing:2px;padding:14px 7px;border:none;border-radius:0 8px 8px 0;background:var(--p-primary-color,#3b82f6);color:#fff;cursor:pointer;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,.4)}
.pcw-handle:hover{filter:brightness(1.15)}
.pcw-launch{padding:12px;display:flex;flex-direction:column;gap:10px;font-size:13px}
/* 紧凑：右栏够宽时，模式 / 文件这类控件占两列、下拉和数字并排，一屏摆下更多；文字框仍然整行 */
.pcw-right{container-type:inline-size}
.pcw-fields{grid-auto-flow:row dense}
@container (min-width:620px){.pcw-field.pcw-span2{grid-column:span 2}}
a.pcw-link{text-decoration:none}
.pcw-fold{background:none;border:none;color:inherit;opacity:.7;cursor:pointer;font:inherit;font-size:14px;padding:0;width:18px}
.pcw-fold:hover{opacity:1}
.pcw-step.pcw-collapsed{padding-top:8px;padding-bottom:8px}
.pcw-sumline{margin:3px 0 0 26px;font-size:12px}
.pcw-prog{height:3px;border-radius:2px;background:rgba(127,127,127,.25);margin-top:6px;overflow:hidden}
.pcw-prog>i{display:block;height:100%;width:0;background:var(--p-primary-color,#3b82f6);transition:width .9s linear}
.pcw-chain{font-size:12px}
.pcw-errbox{padding:6px 8px;border-radius:6px;background:rgba(224,68,62,.12)}
.pcw-errtitle{font-weight:600}
.pcw-errtip{margin:2px 0 4px;opacity:.9}
.pcw-errbox details{font-size:12px;opacity:.85}
.pcw-errbox pre{margin:4px 0 0;white-space:pre-wrap;word-break:break-word;font:inherit}
/* 大图预览（点结果图 / 素材库里的放大按钮） */
.pcw-lb{position:absolute;inset:0;z-index:30;background:rgba(0,0,0,.88);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:16px}
.pcw-lb:focus{outline:none}
.pcw-lb-top{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:center;color:#eee;font-size:13px}
.pcw-lb-stage{display:flex;gap:12px;align-items:center;justify-content:center;max-width:94%;min-height:0}
.pcw-lb-stage img,.pcw-lb-stage video{display:block;max-width:100%;max-height:calc(100vh - 140px);min-width:0;border-radius:6px}
.pcw-lb-stage img{background:#e8e8e8 repeating-conic-gradient(#c6c6c6 0% 25%, #e8e8e8 0% 50%) 0 0 / 14px 14px}
.pcw-lb-stage.pcw-lb-two img,.pcw-lb-stage.pcw-lb-two video{max-width:46vw}
.pcw-lb-nav{position:absolute;top:50%;transform:translateY(-50%);width:42px;height:64px;border:none;border-radius:8px;background:rgba(255,255,255,.12);color:#fff;font-size:22px;cursor:pointer}
.pcw-lb-nav:hover{background:rgba(255,255,255,.28)}
.pcw-lb-prev{left:12px}
.pcw-lb-next{right:12px}
.pcw-lb-info{color:#bbb;font-size:12px;min-height:16px}
.pcw-lb .pcw-btn{background:rgba(255,255,255,.14);color:#fff;border-color:transparent;text-decoration:none}
.pcw-lb .pcw-btn.pcw-on{background:var(--p-primary-color,#3b82f6)}
/* 素材库缩略图上的小工具：放大 / 去掉 */
.pcw-assettools{position:absolute;right:2px;top:2px;display:none;gap:2px}
.pcw-asset:hover .pcw-assettools,.pcw-asset:focus-within .pcw-assettools{display:flex}
@media (hover:none){.pcw-assettools{display:flex}}
.pcw-assettools button{width:20px;height:20px;border-radius:10px;border:none;background:rgba(0,0,0,.65);color:#fff;cursor:pointer;font-size:12px;line-height:18px;padding:0}
.pcw-assettools button:hover{background:rgba(0,0,0,.92)}
`;

const KIND_ICON = { video: "▶", audio: "♪", image: "▣", other: "▫" };

export function createView({ state, url, actions, catalog, catalogStatus }) {
  let root, listEl, inputEl, sendBtn, draftEl, statusEl, fileInput, stepsEl, barEl, noticeEl, assetsEl, handle, modelEl, modelRow;
  let busyTimer;
  const stepEls = [];

  // ── 小部件 ──────────────────────────────────────────────────────────────────────────────────────────
  const hasPlan = () => !!(state.plan && state.plan.steps.length);

  function thumb(a, n) {
    const src = url(lib.inputPath(a.name));
    const tag = n ? h("span", { class: "pcw-tag", text: "#" + n }) : null;
    if (a.kind === "image") return h("span", { class: "pcw-att" }, h("img", { class: "pcw-thumb", src, loading: "lazy", title: lib.assetLabel(a) }), tag);
    if (a.kind === "video") return h("span", { class: "pcw-att" }, h("video", { class: "pcw-thumb", src: src + "#t=0.1", preload: "metadata", muted: true, title: lib.assetLabel(a) }), tag);
    return h("span", { class: "pcw-att" }, h("span", { class: "pcw-filechip", title: lib.assetLabel(a), text: `${KIND_ICON[a.kind] || "▫"} ${lib.baseName(a.name)}` }), tag);
  }

  // ── 聊天 ────────────────────────────────────────────────────────────────────────────────────────────
  function stickToBottom(fn) {
    const near = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < 80;
    fn();
    if (near) listEl.scrollTop = listEl.scrollHeight;
  }

  function renderMessage(m) {
    if (m.role === "user") {
      const atts = (m.attachments || []).map((n) => [n, state.assets[n - 1]]).filter(([, a]) => a && a.name);
      return h("div", { class: "pcw-msg pcw-user" }, m.text, atts.length ? h("div", { class: "pcw-atts" }, atts.map(([n, a]) => thumb(a, n))) : null);
    }
    if (m.role === "error") return h("div", { class: "pcw-msg pcw-ai pcw-err" }, m.text);
    if (m.role === "note") return h("div", { class: "pcw-muted", style: "align-self:center", text: m.text });
    return h("div", { class: "pcw-msg pcw-ai" }, m.text, (m.warnings || []).map((w) => h("div", { class: "pcw-warn", text: "（" + w + "）" })),
      m.planText ? h("div", { class: "pcw-planchip", text: "方案已更新到右边：" + m.planText }) : null,
      m.model ? h("div", { class: "pcw-muted pcw-modelnote", text: "模型：" + m.model }) : null);
  }

  function renderChat() {
    stickToBottom(() => {
      listEl.replaceChildren();
      if (!state.messages.length) {
        listEl.append(h("div", { class: "pcw-empty" },
          h("div", { text: "你好，我是 AI 工作台。说一句话，我来排好步骤（出图 → 出视频 → 配音 → 成片……），右边每一步的设置都能改，改完一键跑完；前一步做出来的图 / 视频 / 音频会自动交给下一步。可以附商品图、视频、音频。" }),
          h("div", { class: "pcw-chips" }, actions.examples.map((t) => h("button", { class: "pcw-chip", text: t, onclick: () => { inputEl.value = t.replace(/^（先点「附件」传一张商品图）/, ""); autoGrow(); inputEl.focus(); } })))));
      }
      for (const m of state.messages) listEl.append(renderMessage(m));
    });
  }

  // 输入框下面的「助手模型」下拉：自动 + 后端给的清单（阿里一组、谷歌一组）；选的记在浏览器里（actions.setModel），清单读到之前只有「自动」
  function renderModels() {
    const list = actions.models();
    const cur = actions.getModel();
    modelEl.replaceChildren(h("option", { value: "", text: "自动（推荐）" }));
    for (const [prov, title] of [["ali", "阿里"], ["gateway", "谷歌（走网关，一般比阿里慢）"]]) {
      const ms = list.filter((m) => m.provider === prov);
      if (ms.length) modelEl.append(h("optgroup", { label: title }, ms.map((m) => h("option", { value: m.id, text: m.label }))));
    }
    modelEl.value = list.some((m) => m.id === cur) ? cur : "";
    modelRow.style.display = list.length ? "" : "none";                          // 后端是旧版（没给清单）：不显示，别摆一个选了也没用的下拉
  }

  function autoGrow() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(140, inputEl.scrollHeight) + "px";
  }

  // 等 AI 回复：显示已等了多久（到阿里的线路偶尔会卡十几秒，没有计时看起来像死了）
  function setBusy(on) {
    state.busy = on;
    refreshSend();
    renderPlan();                                                                 // 等 AI 回复时方案不让改：重画一遍把控件锁上 / 解开
    clearInterval(busyTimer);
    if (!on) { statusEl.textContent = ""; return; }
    const t0 = Date.now();
    const draw = () => { statusEl.textContent = `AI 正在想… ${lib.elapsedText(Date.now() - t0)}`; };
    draw();
    busyTimer = setInterval(draw, 1000);
  }

  function refreshSend() {
    const running = !!state.running;
    sendBtn.disabled = state.busy || running;
    sendBtn.title = running ? "步骤正在运行，跑完再继续聊（避免改乱正在跑的方案）" : "";
  }

  function setStatus(text) {
    if (!state.busy) statusEl.textContent = text || "";
  }

  function renderDrafts() {
    draftEl.replaceChildren(...state.draft.map((d, i) => h("div", { class: "pcw-att pcw-draft" }, thumb(d, 0), h("button", { title: "移除", text: "×", onclick: () => { state.draft.splice(i, 1); renderDrafts(); } }))));
  }

  // ── 方案：控件 ──────────────────────────────────────────────────────────────────────────────────────
  function fieldWrap(i, step, f, kids, wide, locked) {
    const changed = f.key in (step.fields || {});
    const reset = changed ? h("button", { class: "pcw-link", text: "↺ 恢复默认", title: "恢复默认值", disabled: locked, onclick: () => { delete step.fields[f.key]; actions.changed(); renderPlan(); } }) : null;
    const span2 = !wide && f.hint && f.hint.length > 50;                               // 说明很长的控件占两列（说明最多显示两行，完整的放在鼠标悬停提示里）
    return h("div", { class: "pcw-field" + (wide ? " pcw-wide" : span2 ? " pcw-span2" : "") + (changed ? " pcw-changed" : "") },
      h("label", null, h("span", { text: f.label }), reset), kids, f.hint ? h("div", { class: "pcw-hint", title: f.hint, text: f.hint }) : null);
  }

  function control(i, step, f, locked) {
    const pc = `${i}:${f.key}`;
    const edit = (value) => { lib.setField(step, f, value); actions.changed(); };
    if (f.kind === "text") {
      const dflt = String(f.default || "").trim();
      const rowsFor = (v) => String(Math.min(10, Math.max(2, String(v).split("\n").length)));                 // 按行数长高（对白脚本有好几行），最多 10 行再滚动
      const placeholder = dflt ? `留空就用默认：${dflt.length > 40 ? dflt.slice(0, 40) + "…" : dflt}` : "";
      if (f.multiline === false) {                                                                                // 角色名、颜色这类短文字：单行输入框，不占两行高
        const inp = h("input", { type: "text", "data-pc": pc, disabled: locked, placeholder, oninput: () => { edit(inp.value); markChanged(inp, step, f); }, value: lib.fieldValue(step, f) });
        return fieldWrap(i, step, f, inp, false, locked);
      }
      const ta = h("textarea", { rows: rowsFor(lib.fieldValue(step, f)), "data-pc": pc, disabled: locked, placeholder, oninput: () => { edit(ta.value); ta.rows = rowsFor(ta.value); markChanged(ta, step, f); }, value: lib.fieldValue(step, f) });
      return fieldWrap(i, step, f, ta, true, locked);
    }
    if (f.kind === "choice") {
      const cur = String(lib.fieldValue(step, f));
      const sel = h("select", { "data-pc": pc, disabled: locked, onchange: () => { edit(sel.value); markChanged(sel, step, f); } },
        f.options.map((o) => { const shown = (f.labels && f.labels[o]) || o; return h("option", { value: o, text: o === f.default ? `${shown}（默认）` : shown, selected: o === cur }); }));
      return fieldWrap(i, step, f, sel, false, locked);
    }
    if (f.kind === "bool") {
      const cb = h("input", { type: "checkbox", "data-pc": pc, disabled: locked, onchange: () => { edit(cb.checked); markChanged(cb, step, f); } });
      cb.checked = !!lib.fieldValue(step, f);
      return fieldWrap(i, step, f, h("label", { class: "pcw-check" }, cb, h("span", { text: cb.checked ? "开" : "关" })), false, locked);
    }
    // 整数 / 小数：滑块（范围不大时）+ 数字框
    const cur = lib.fieldValue(step, f);
    const stepAttr = f.step || (f.kind === "int" ? 1 : 0.01);
    const ranged = f.min != null && f.max != null && f.max - f.min <= 1e6 && (f.max - f.min) / stepAttr <= 5000;
    const num = h("input", { type: "number", "data-pc": pc, min: f.min, max: f.max, step: stepAttr, disabled: locked, value: String(cur) });
    const range = ranged ? h("input", { type: "range", min: f.min, max: f.max, step: stepAttr, disabled: locked, value: String(cur) }) : null;
    const apply = (raw, src) => {
      const x = lib.clampNumber(raw, f);
      if (x === null) return;
      edit(x);
      if (src !== num) num.value = String(x);
      if (range && src !== range) range.value = String(x);
      markChanged(num, step, f);
    };
    num.addEventListener("input", () => apply(num.value, num));
    num.addEventListener("change", () => { apply(num.value, null); });
    if (range) range.addEventListener("input", () => apply(range.value, range));
    return fieldWrap(i, step, f, h("div", { class: "pcw-numrow" }, range, num), false, locked);
  }

  // 改动后给这个控件加 / 去「已改」的高亮和「恢复默认」按钮（不重画整张卡，免得输入框丢焦点）
  function markChanged(el, step, f) {
    const wrap = el.closest(".pcw-field");
    if (!wrap) return;
    const changed = f.key in step.fields;
    wrap.classList.toggle("pcw-changed", changed);
    const label = wrap.querySelector("label");
    const old = label.querySelector("button");
    if (changed && !old) label.append(h("button", { class: "pcw-link", text: "↺ 恢复默认", title: "恢复默认值", disabled: el.disabled, onclick: () => { delete step.fields[f.key]; actions.changed(); renderPlan(); } }));
    if (!changed && old) old.remove();
  }

  function fileControl(i, step, f, locked) {
    const { steps, assets } = lib.fileChoices(state.plan, state.assets, i, f.kind);
    const ref = step.files[f.key];
    const cur = lib.refToValue(ref);
    const known = new Set([...steps, ...assets].map((c) => c.value));
    const opt = (c) => h("option", { value: c.value, text: c.label, selected: c.value === cur });
    const sel = h("select", { "data-pc": `${i}:${f.key}`, disabled: locked, onchange: () => {
      const r = lib.valueToRef(sel.value);
      if (r) step.files[f.key] = r; else delete step.files[f.key];
      actions.changed();
      renderPlan();
    } },
      h("option", { value: "", text: "（用应用里的默认文件）", selected: !cur }),
      cur && !known.has(cur) ? h("option", { value: cur, text: "（这个文件已经不可用）", selected: true }) : null,
      steps.length ? h("optgroup", { label: "前面步骤做出来的" }, steps.map(opt)) : null,
      assets.length ? h("optgroup", { label: "素材库" }, assets.map(opt)) : null);
    const up = h("input", { type: "file", accept: f.kind + "/*", style: "display:none", onchange: () => { if (up.files[0]) actions.uploadForField(i, f.key, f.kind, up.files[0]); up.value = ""; } });
    const row = h("div", { class: "pcw-filerow" }, sel, h("button", { class: "pcw-btn pcw-btn-sm", text: "上传…", disabled: locked, title: `上传一个${lib.KIND_LABEL[f.kind]}文件，放进素材库并用在这里`, onclick: () => up.click() }), up);
    let pic = null;
    if (ref && ref.asset && state.assets[ref.asset - 1]) {
      const a = state.assets[ref.asset - 1];
      const src = url(lib.inputPath(a.name));
      pic = f.kind === "image" ? h("img", { class: "pcw-filepic", src }) : f.kind === "video" ? h("video", { class: "pcw-filepic", src, controls: true, preload: "metadata" }) : h("audio", { src, controls: true, preload: "metadata" });
    } else if (ref && ref.step) {
      pic = h("div", { class: "pcw-hint", text: `运行步骤 ${ref.step} 之后，会自动用它做出来的${lib.KIND_LABEL[f.kind]}` });
    }
    const need = lib.isRequired(step, f);                   // 当前模式下必填（有的文件只在某个模式必填）
    return h("div", { class: "pcw-field pcw-span2" + (cur ? " pcw-changed" : "") + (need && !cur ? " pcw-missing" : "") },
      h("label", null, h("span", { text: f.label + (need ? "（必填）" : "") }),
        cur ? h("button", { class: "pcw-link", text: "↺ 用默认", disabled: locked, onclick: () => { delete step.files[f.key]; actions.changed(); renderPlan(); } }) : null), row, pic);
  }

  // ── 方案：结果 ──────────────────────────────────────────────────────────────────────────────────────
  // 报错：认得的错误先写一句人话 + 怎么办，原文收在「详细信息」里；认不出的原样显示
  function errorBox(text) {
    const ex = lib.explainError(text);
    if (!ex) return h("div", { class: "pcw-err", style: "flex:1 1 100%", text });
    return h("div", { class: "pcw-err pcw-errbox", style: "flex:1 1 100%" }, h("div", { class: "pcw-errtitle", text: ex.title }), h("div", { class: "pcw-errtip", text: ex.tip }),
      h("details", null, h("summary", { text: "详细信息" }), h("pre", { text })));
  }

  function resultView(step, stepIdx) {
    const r = step.result;
    if (!r) return null;
    const box = h("div", { class: "pcw-results" });
    const visual = (r.items || []).filter((it) => it.kind === "image" || it.kind === "video");
    const lbItems = visual.map((it) => ({ src: url(lib.viewPath(it.file)), kind: it.kind, name: it.file.filename, asset: it.asset || 0, label: it.label || "" }));
    for (const it of r.items || []) {
      const src = url(lib.viewPath(it.file));
      const bar = it.kind === "text" ? null : h("div", { class: "pcw-resbar" }, it.asset ? h("span", { text: "素材 #" + it.asset }) : null,
        it.asset ? h("button", { class: "pcw-link", text: "在聊天里引用", onclick: () => actions.mention(it.asset) }) : null,
        h("a", { class: "pcw-link", href: src, download: it.file.filename, title: "把这个文件下载到电脑上", text: "下载" }));
      if (it.kind === "image") box.append(h("div", { class: "pcw-res" }, h("img", { src, loading: "lazy", title: "点一下看大图（可以对比原图、下载）", onclick: () => openLightbox(lbItems, visual.indexOf(it), stepIdx) }), bar));
      else if (it.kind === "video") box.append(h("div", { class: "pcw-res" }, h("video", { src, controls: true, preload: "metadata" }), bar));
      else if (it.kind === "audio") box.append(h("div", { class: "pcw-res" }, h("audio", { src, controls: true, preload: "metadata" }), bar));
      else if (it.kind === "text") {
        const pre = h("pre", { text: it.text != null ? it.text : "加载中…" });
        const copy = navigator.clipboard && !it.isError ? h("button", { class: "pcw-link", text: "复制", onclick: (e) => actions.copyText(pre.textContent, e.target) }) : null;
        box.append(h("div", { class: "pcw-text" + (it.isError ? " pcw-err" : "") }, it.label || copy ? h("div", { class: "pcw-tl" }, h("span", { text: it.label || "" }), copy) : null, pre));
        if (it.text == null) actions.loadText(it, src, (t) => { pre.textContent = t; });
      }
    }
    if (r.error) box.append(errorBox(r.error));
    if (!(r.items || []).length && !r.error) box.append(h("div", { class: "pcw-muted", text: "没有产出结果（可能被跳过了）；可以点「在应用里打开」看看。" }));
    (r.warnings || []).forEach((w) => box.append(h("div", { class: "pcw-warn", style: "flex:1 1 100%", text: "（" + w + "）" })));
    return box;
  }

  // ── 方案：步骤卡 ────────────────────────────────────────────────────────────────────────────────────
  const STATUS_TEXT = { idle: "还没运行", running: "运行中", done: "已完成", stale: "要重跑", failed: "没成功", stopped: "已停止", interrupted: "页面刷新时中断了" };

  function badgeText(step, st) {
    const t = STATUS_TEXT[st.kind];
    if (st.kind === "running") {
      const r = state.running;
      const est = lib.estimateMs(actions.durations(), lib.durationKey(step));
      return `${t} · 已 ${lib.elapsedText(Date.now() - ((step.run && step.run.started) || Date.now()))}${est ? `（预计${lib.etaText(est)}）` : ""}${r && r.node ? " · " + lib.shortTitle(r.node) : ""}`;
    }
    if (st.kind === "done") return `${t}${step.result && step.result.elapsed ? " · 用时 " + lib.elapsedText(step.result.elapsed) : ""}`;
    if (st.kind === "stale") return `${t}（${st.reason}）`;
    return t;
  }

  // 步骤卡上的三个按钮只建一次，状态变了只原地改文字和可点状态：如果重画，用户正在点的按钮会被换掉，这一下点击就丢了
  // （比如刚在数字框里改完值，鼠标一按下输入框失焦、触发状态刷新，按钮被换，松开鼠标时点击落空）
  const MAIN_LABEL = { idle: "运行这一步", done: "重跑这一步", stale: "重跑这一步", failed: "再试一次", stopped: "再试一次", interrupted: "再试一次" };

  function stepButtons(i) {
    const stateOf = () => lib.stepStatus(state.plan, i).kind;
    const main = h("button", { class: "pcw-btn pcw-btn-sm", onclick: () => (stateOf() === "running" ? actions.stop() : actions.runStep(i)) });
    const open = h("button", { class: "pcw-btn pcw-btn-sm", text: "在应用里打开", title: "把这一步载入成一个应用页面，接着手动调", onclick: () => actions.openInApp(i) });
    const del = h("button", { class: "pcw-btn pcw-btn-sm", text: "删除", onclick: () => actions.removeStep(i) });
    return { main, open, del, index: i };
  }

  function updateButtons(b, st) {
    const running = st.kind === "running";
    b.main.textContent = running ? "停止" : MAIN_LABEL[st.kind];
    b.main.className = "pcw-btn pcw-btn-sm" + (!running && (st.kind === "idle" || st.kind === "stale") ? " pcw-btn-primary" : "");
    const missing = lib.missingRequired(state.plan.steps[b.index]);
    const frozen = !!(state.running || state.busy);
    b.main.disabled = !running && (frozen || missing.length > 0);
    b.main.title = missing.length ? `还缺必填的：${missing.map((f) => f.label).join("、")}` : state.busy ? "AI 正在想，等它回复后再运行" : "";
    b.open.disabled = frozen;
    b.del.disabled = frozen;
  }

  function stepCard(i) {
    const step = state.plan.steps[i];
    const sc = step.schema || {};
    const st = lib.stepStatus(state.plan, i);
    const locked = st.kind === "running" || !!state.busy;                          // 等 AI 回复的时候方案也不让改（回复一到整个方案会被换掉）
    const collapsed = lib.isCollapsed(state.plan, i);
    const badge = h("span", { class: `pcw-badge pcw-b-${st.kind}`, text: badgeText(step, st) });
    const b = stepButtons(i);
    updateButtons(b, st);
    const fold = h("button", { class: "pcw-fold", title: collapsed ? "展开这一步" : "折叠这一步（只留一行，步骤多的时候一屏看得下）", "aria-expanded": String(!collapsed), text: collapsed ? "▸" : "▾", onclick: () => actions.toggleStep(i) });
    const funcs = lib.siblings(catalog() || [], step.workflow);                  // 这一步所在的应用里的所有功能（合并应用）：标题上画一个「做什么」下拉；单独的应用就是一个名字
    let title = h("b", { class: "pcw-step-name", text: step.name });
    let funcSel = null;
    if (funcs.length > 1) {
      const sel = funcSel = h("select", { class: "pcw-funcsel", "data-pc": `${i}:func`, disabled: locked || !!state.running, title: `这个应用里的功能：${funcs.map((a) => a.name).join("、")}；换一个功能会清掉这一步现在的设置和结果`, onchange: () => actions.changeFunction(i, sel.value) },
        funcs.map((a) => h("option", { value: a.workflow, text: a.name, selected: a.workflow === step.workflow })));
      title = h("span", { class: "pcw-step-name" }, h("span", { class: "pcw-fam", text: ((funcs[0].family || {}).name || "") + " ›" }), sel);
    }
    const head = h("div", { class: "pcw-step-head" }, fold, h("span", { class: "pcw-num", text: String(i + 1) }), title, badge, h("span", { class: "pcw-spacer" }),
      h("span", { style: "display:inline-flex;gap:6px" }, b.main, b.open, b.del));
    const prog = h("div", { class: "pcw-prog", style: "display:none" }, h("i"));
    const box = h("div", { class: "pcw-step" + (collapsed ? " pcw-collapsed" : ""), "data-step": String(i) }, head, prog, step.note ? h("div", { class: "pcw-note", text: step.note }) : null);       // 费用 / 额度的提醒统一放在方案顶部（updateBar），卡片里不再重复
    box._badge = badge;
    box._funcSel = funcSel;
    box._buttons = b;
    box._prog = prog;
    updateProgress(box, step);
    if (collapsed) {                                                    // 折叠：只留一行 + 一句摘要（模式 / 改过几项 / 有什么结果）+ 还缺什么 / 没成功
      const line = [lib.stepSummary(step), lib.resultBrief(step.result)].filter(Boolean).join(" · ");
      if (line) box.append(h("div", { class: "pcw-muted pcw-sumline", text: line }));
      const missing = lib.missingRequired(step);
      if (missing.length) box.append(h("div", { class: "pcw-err pcw-sumline", text: `还缺：${missing.map((f) => f.label).join("、")}（展开后点「上传…」或选素材）` }));
      if (step.result && step.result.error) box.append(h("div", { class: "pcw-err pcw-sumline", text: "没成功，展开看原因" }));
      return box;
    }
    const common = Object.values(sc.fields || {}).filter((f) => !f.advanced);
    const advanced = Object.values(sc.fields || {}).filter((f) => f.advanced);
    const grid = h("div", { class: "pcw-fields" });
    if (sc.mode) {
      const cur = lib.currentMode(step);
      const sel = h("select", { "data-pc": `${i}:mode`, disabled: locked, onchange: () => { step.mode = sel.value === sc.mode.default ? null : sel.value; actions.changed(); renderPlan(); } },   // 重画：不同模式必填的文件不一样
        sc.mode.options.map((o) => h("option", { value: o, text: o, selected: o === cur })));
      grid.append(h("div", { class: "pcw-field pcw-span2" }, h("label", null, h("span", { text: sc.mode.label || "模式" })), sel));
    }
    const texts = common.filter((f) => f.kind === "text");
    const files = Object.values(sc.files || {});
    texts.forEach((f) => grid.append(control(i, step, f, locked)));
    files.forEach((f) => grid.append(fileControl(i, step, f, locked)));
    common.filter((f) => f.kind !== "text").forEach((f) => grid.append(control(i, step, f, locked)));
    if (advanced.length) {
      const adv = h("details", { class: "pcw-adv" }, h("summary", { text: `高级设置（${advanced.length} 项，不改就用默认）` }), h("div", { class: "pcw-fields" }, advanced.map((f) => control(i, step, f, locked))));
      if (advanced.some((f) => f.key in step.fields)) adv.open = true;
      grid.append(adv);
    }
    const open = st.kind !== "done" || !step.result;
    box.append(h("details", { class: "pcw-settings", open: open || undefined }, h("summary", { text: "设置" }), grid));
    const results = resultView(step, i);
    if (results) box.append(results);                       // 注意：append(null) 会写出文字 "null"
    return box;
  }

  // 运行中的步骤：有历史用时记录就显示一条进度条（已过去 / 预计，最多到 95%）；没有记录就不显示（不瞎猜）
  function updateProgress(box, step) {
    const bar = box._prog;
    if (!bar) return;
    const est = step.state === "running" ? lib.estimateMs(actions.durations(), lib.durationKey(step)) : 0;
    const pct = est ? lib.progressPercent(Date.now() - ((step.run && step.run.started) || Date.now()), est) : null;
    bar.style.display = pct == null ? "none" : "";
    if (pct != null) bar.firstChild.style.width = pct + "%";
  }

  // 只更新每张卡的状态标签和按钮（改设置时「要重跑」会变，不用重画控件）
  function refreshStatus() {
    if (hasPlan()) {
      stepEls.forEach((el, i) => {
        if (!el || !state.plan.steps[i]) return;
        const step = state.plan.steps[i];
        const st = lib.stepStatus(state.plan, i);
        el._badge.className = `pcw-badge pcw-b-${st.kind}`;
        el._badge.textContent = badgeText(step, st);
        updateButtons(el._buttons, st);
        if (el._funcSel) el._funcSel.disabled = !!(state.running || state.busy);                    // 换功能和删除一样：有步骤在运行 / 等 AI 回复时不让动
        updateProgress(el, step);
      });
    }
    if (runBtn) updateBar();                                                // 没有方案时也要更新：顶上「添加一步」的下拉写着应用目录是加载中 / 没读到
  }

  // 每秒更新一次：正在运行的那一步的「已 N 秒」
  function tickRunning() {
    if (!hasPlan()) return;
    stepEls.forEach((el, i) => {
      const step = state.plan.steps[i];
      if (el && step && step.state === "running") { el._badge.textContent = badgeText(step, { kind: "running", reason: "" }); updateProgress(el, step); }
    });
    if (chainEl) chainEl.textContent = chainText();
  }

  // 「运行全部」时顶上写：第几步在跑、后面还有几步、大概还要多久（每一步都有历史用时才给，不瞎猜）
  function chainText() {
    const c = state.chain, r = state.running;
    if (!c || c.mode !== "all" || !r || !hasPlan() || !state.plan.steps[r.stepIdx]) return "";
    const left = lib.pendingAfter(state.plan, r.stepIdx).length;
    const rest = lib.remainingMs(state.plan, actions.durations(), r.stepIdx, Date.now() - r.start);
    return `第 ${r.stepIdx + 1} 步运行中${left ? `，后面还有 ${left} 步` : "，这是最后一步"}${rest ? `，预计还要${lib.etaText(rest)}` : ""}`;
  }

  // 顶部工具条：运行 / 停止、添加一步、清空方案。元素只建一次（见上面的说明），状态变了原地更新
  let runBtn, addSel, clearBtn, presetSel, foldBtn, chainEl;

  function buildBar() {
    runBtn = h("button", { class: "pcw-btn pcw-btn-primary", onclick: () => (state.running ? actions.stop() : actions.runAll()) });
    chainEl = h("span", { class: "pcw-muted pcw-chain" });
    presetSel = h("select", { class: "pcw-presetsel", title: "一键方案：点一下排好一组常用的步骤（会换掉现在的方案）", onchange: () => { if (presetSel.value) actions.applyPreset(presetSel.value); presetSel.value = ""; } });
    foldBtn = h("button", { class: "pcw-link", onclick: () => actions.toggleAll() });
    addSel = h("select", { class: "pcw-addsel", title: "自己加一步（不用 AI 也能搭方案）", onchange: () => { if (addSel.value) actions.addStep(addSel.value); addSel.value = ""; } });
    const retryCatalog = () => { if (!(catalog() || []).length && catalogStatus && catalogStatus() === "failed") actions.reloadCatalog(); };      // 目录没读到时，点一下下拉就再试一次
    addSel.addEventListener("mousedown", retryCatalog);
    addSel.addEventListener("focus", retryCatalog);
    clearBtn = h("button", { class: "pcw-link", text: "清空方案", onclick: () => actions.clearPlan() });
    barEl.append(runBtn, chainEl, presetSel, addSel, h("span", { class: "pcw-spacer" }), foldBtn, clearBtn);
  }

  function updateBar() {
    const plan = state.plan;
    const n = hasPlan() ? lib.pendingCount(plan) : 0;
    const running = !!state.running;
    runBtn.style.display = hasPlan() ? "" : "none";
    runBtn.textContent = running ? "■ 停止" : n ? `▶ 运行全部（${n} 步待运行）` : "▶ 全部已完成";
    const blocked = hasPlan() ? plan.steps.map((s, i) => [i, s]).filter(([i, s]) => lib.needsRun(plan, i) && lib.missingRequired(s).length) : [];
    runBtn.disabled = !running && (!n || blocked.length > 0 || !!state.busy);
    runBtn.title = blocked.length ? "有步骤还缺必填的素材，先补上" : state.busy ? "AI 正在想，等它回复后再运行" : "";
    const apps = catalog() || [];
    const holder = apps.length ? "＋ 添加一步…" : catalogStatus && catalogStatus() === "failed" ? "＋ 添加一步（应用目录没加载出来，点这里重试）" : "＋ 添加一步（应用目录加载中…）";
    const fams = lib.families(apps);
    if (addSel.options.length !== fams.length + 1) {                      // 目录加载好了才补选项（下拉打开时不去动它）；一项 = 一个应用（做图 / 处理图 / …），里面的功能在步骤卡片标题上的「做什么」下拉里选
      addSel.replaceChildren(h("option", { value: "", text: holder }),
        ...fams.map((f) => h("option", { value: f.first.workflow, text: f.single ? `${f.first.workflow} ${f.first.name}` : f.name, title: f.single ? f.first.desc : `里面的功能：${f.apps.map((a) => a.name).join("、")}` })));
    } else if (addSel.options[0].text !== holder) {
      addSel.options[0].text = holder;
    }
    const pre = actions.presets();                                        // 一键方案：后端是旧版（没给清单）就不显示
    presetSel.style.display = pre.length ? "" : "none";
    if (presetSel.options.length !== pre.length + 1) presetSel.replaceChildren(h("option", { value: "", text: "⚡ 一键方案…" }), ...pre.map((p) => h("option", { value: p.id, text: p.name, title: p.desc })));
    presetSel.disabled = running || !!state.busy;
    foldBtn.style.display = hasPlan() && plan.steps.length >= 2 ? "" : "none";
    foldBtn.textContent = hasPlan() && plan.steps.every((_, i) => lib.isCollapsed(plan, i)) ? "展开全部" : "折叠全部";
    chainEl.textContent = chainText();
    addSel.disabled = running || !!state.busy || (!!plan && plan.steps.length >= lib.MAX_STEPS);
    clearBtn.style.display = hasPlan() ? "" : "none";
    clearBtn.disabled = running || !!state.busy;
    const notes = hasPlan() ? lib.costNotes(plan) : [];
    const lacks = blocked.map(([i, s]) => h("div", { class: "pcw-err", text: `⚠ 步骤 ${i + 1}「${s.name}」还缺：${lib.missingRequired(s).map((f) => f.label).join("、")}（必须有，点这一步里的「上传…」或选素材库里的）` }));
    noticeEl.replaceChildren(...lacks, ...notes.map((c) => h("div", { text: `⚠ 步骤 ${c.index}「${c.name}」：${c.text}` })));
    noticeEl.style.display = notes.length || lacks.length ? "" : "none";
  }

  function keepFocus(fn) {
    const a = document.activeElement;
    const key = a && a.dataset && a.dataset.pc && stepsEl.contains(a) ? a.dataset.pc : null;
    const sel = key && typeof a.selectionStart === "number" ? [a.selectionStart, a.selectionEnd] : null;
    const top = stepsEl.scrollTop;
    fn();
    stepsEl.scrollTop = top;
    if (!key) return;
    const el = [...stepsEl.querySelectorAll("[data-pc]")].find((x) => x.dataset.pc === key);
    if (el) { el.focus(); if (sel) { try { el.setSelectionRange(sel[0], sel[1]); } catch (e) { /* 数字框不支持 */ } } }
  }

  function renderPlan() {
    keepFocus(() => {
      stepsEl.replaceChildren();
      stepEls.length = 0;
      if (!hasPlan()) {
        const pre = actions.presets();
        stepsEl.append(h("div", { class: "pcw-empty" },
          h("div", { text: "还没有方案。在左边说一句话，我来排步骤；也可以用上面的「＋ 添加一步」自己搭。" }),
          h("div", { class: "pcw-muted", text: "每一步 = 一个应用（出图 / 出视频 / 配音 / 成片……）。前一步做出来的图、视频、音频，下一步可以直接用。" }),
          pre.length ? h("div", { class: "pcw-muted", style: "margin-top:12px", text: "或者点一个一键方案（排好一组常用的步骤，设置再自己改）：" }) : null,
          pre.length ? h("div", { class: "pcw-chips" }, pre.map((p) => h("button", { class: "pcw-chip", title: p.desc, text: "⚡ " + p.name + (p.image ? "（要先有商品图）" : ""), onclick: () => actions.applyPreset(p.id) }))) : null));
      } else {
        state.plan.steps.forEach((_, i) => { const c = stepCard(i); stepEls[i] = c; stepsEl.append(c); });
      }
      updateBar();
    });
  }

  // ── 大图预览 ────────────────────────────────────────────────────────────────────────────────────────
  let lbEl = null;

  function closeLightbox() {
    if (lbEl) { lbEl.remove(); lbEl = null; }
  }

  // items: [{src, kind, name, asset, label}]；idx：先看哪一个；stepIdx：这些结果属于第几步（下标，素材库里的写 -1）：
  // 这一步用了一张图当输入时，可以在「结果 / 原图 / 左右对比」之间切换；← → 翻页，Esc 或点空白处关闭
  function openLightbox(items, idx, stepIdx) {
    if (!items.length) return;
    closeLightbox();
    let cur = Math.max(0, Math.min(idx, items.length - 1)), mode = "result";
    const source = stepIdx >= 0 && hasPlan() ? lib.sourceImage(state.plan, state.assets, stepIdx) : null;
    const srcUrl = source ? url(lib.inputPath(source.name)) : "";
    const top = h("div", { class: "pcw-lb-top" });
    const stage = h("div", { class: "pcw-lb-stage" });
    const info = h("div", { class: "pcw-lb-info" });
    const prev = h("button", { class: "pcw-lb-nav pcw-lb-prev", title: "上一个（←）", text: "‹", onclick: () => go(-1) });
    const next = h("button", { class: "pcw-lb-nav pcw-lb-next", title: "下一个（→）", text: "›", onclick: () => go(1) });
    const media = (src, kind, label) => (kind === "video" ? h("video", { src, controls: true, preload: "metadata" })
      : h("img", { src, alt: label || "", onload: (e) => { info.textContent = `${label ? label + " · " : ""}${e.target.naturalWidth} × ${e.target.naturalHeight}`; } }));
    function go(d) {
      const n = cur + d;
      if (n < 0 || n >= items.length) return;
      cur = n;
      mode = "result";
      draw();
    }
    function draw() {
      const it = items[cur];
      const showSource = !!source && it.kind === "image";
      if (!showSource) mode = "result";
      stage.classList.toggle("pcw-lb-two", mode === "both");
      stage.replaceChildren(...(mode === "source" ? [media(srcUrl, "image", "原图")] : mode === "both" ? [media(srcUrl, "image", "原图"), media(it.src, it.kind, "结果")] : [media(it.src, it.kind, it.label || "结果")]));
      const seg = (m, text) => h("button", { class: "pcw-btn pcw-btn-sm" + (mode === m ? " pcw-on" : ""), "data-lb": m, text, onclick: () => { mode = m; draw(); } });
      top.replaceChildren(h("span", { text: `${cur + 1} / ${items.length}` }),
        showSource ? seg("result", "结果") : null, showSource ? seg("source", "原图") : null, showSource ? seg("both", "左右对比") : null,
        h("a", { class: "pcw-btn pcw-btn-sm", "data-lb": "download", href: mode === "source" ? srcUrl : it.src, download: mode === "source" ? lib.baseName(source.name) : it.name, text: "下载" }),
        it.asset ? h("button", { class: "pcw-btn pcw-btn-sm", text: `在聊天里引用 #${it.asset}`, onclick: () => { closeLightbox(); actions.mention(it.asset); } }) : null,
        h("button", { class: "pcw-btn pcw-btn-sm", "data-lb": "close", text: "关闭 ✕", onclick: closeLightbox }));
      prev.style.display = cur > 0 ? "" : "none";
      next.style.display = cur < items.length - 1 ? "" : "none";
      if (lbEl && !lbEl.contains(document.activeElement)) lbEl.focus();                 // 刚点的按钮被重画掉了，焦点要回到预览上，不然 Esc / ← → 就没用了
    }
    lbEl = h("div", { class: "pcw-lb", tabindex: "-1", role: "dialog", "aria-label": "大图预览",
      onclick: (e) => { if (e.target === lbEl || e.target === stage) closeLightbox(); },
      onkeydown: (e) => { if (e.key === "Escape") closeLightbox(); else if (e.key === "ArrowLeft") go(-1); else if (e.key === "ArrowRight") go(1); else return; e.preventDefault(); e.stopPropagation(); } },
      top, stage, info, prev, next);
    root.append(lbEl);
    draw();
    lbEl.focus();
  }

  // 素材库里放大看：所有图片 / 视频素材一起翻
  function previewAsset(n) {
    const list = [];
    state.assets.forEach((a, k) => { if (a.name && (a.kind === "image" || a.kind === "video")) list.push({ src: url(lib.inputPath(a.name)), kind: a.kind, name: lib.baseName(a.name), asset: k + 1, label: lib.assetLabel(a) }); });
    const at = list.findIndex((x) => x.asset === n);
    openLightbox(list, at < 0 ? 0 : at, -1);
  }

  // ── 素材库 ──────────────────────────────────────────────────────────────────────────────────────────
  function renderAssets() {
    assetsEl.replaceChildren();
    const have = state.assets.filter((a) => a.name).length;                          // 去掉的素材留着空位（编号不能变），不算数
    assetsEl.style.display = have ? "" : "none";
    assetsEl.append(h("div", { class: "pcw-assets-head", text: `素材库 · ${have} 个（点一下可以在聊天里引用，比如「用 #3 做视频」；鼠标放上去可以放大或去掉）` }),
      h("div", { class: "pcw-assets-list" }, state.assets.map((a, k) => {
        if (!a.name) return null;
        const t = thumb(a, k + 1);
        t.classList.add("pcw-asset");
        t.addEventListener("click", () => actions.mention(k + 1));
        t.append(h("span", { class: "pcw-assettools" },
          a.kind === "image" || a.kind === "video" ? h("button", { title: "放大预览", "aria-label": "放大预览", "data-tool": "zoom", text: "⤢", onclick: (e) => { e.stopPropagation(); previewAsset(k + 1); } }) : null,
          h("button", { title: "从素材库里去掉（别的素材的编号不会变）", "aria-label": "从素材库里去掉", "data-tool": "remove", text: "×", onclick: (e) => { e.stopPropagation(); actions.removeAsset(k + 1); } })));
        return t;
      })));
    const list = assetsEl.querySelector(".pcw-assets-list");
    if (list) list.scrollLeft = list.scrollWidth;                                  // 新的在最右边，滚到能看见
  }

  // ── 整体 ────────────────────────────────────────────────────────────────────────────────────────────
  function build() {
    if (!document.getElementById("pro-chat-style")) {
      const st = document.createElement("style");
      st.id = "pro-chat-style";
      st.textContent = CSS;
      document.head.append(st);
    }
    listEl = h("div", { class: "pcw-list" });
    draftEl = h("div", { class: "pcw-drafts" });
    statusEl = h("div", { class: "pcw-muted", style: "min-height:16px;margin:0 0 4px" });
    inputEl = h("textarea", { rows: "2", placeholder: "说你想做什么，比如：给红苹果做一张主图，再做一条视频并配音", onkeydown: (e) => { if (lib.isEnterToSend(e)) { e.preventDefault(); actions.send(); } }, oninput: autoGrow });
    sendBtn = h("button", { class: "pcw-btn pcw-btn-primary", text: "发送", onclick: () => actions.send() });
    fileInput = h("input", { type: "file", accept: "image/*,video/*,audio/*", multiple: true, style: "display:none", onchange: () => { actions.attach([...fileInput.files]); fileInput.value = ""; } });
    const attachBtn = h("button", { class: "pcw-btn", title: "附件：上传商品图、视频、音频（可多个，也可以直接粘贴 / 拖进来）", text: "附件", onclick: () => fileInput.click() });
    modelEl = h("select", { class: "pcw-modelsel", title: "排方案、回复你的 AI 用哪个模型。自动 = 系统挑（纯文字用稳定的阿里模型，带图片的那一轮用能看图的）；谷歌的走网关，一般比阿里慢（几秒到 80 秒，带图片的那一轮要 1 分钟左右）；"
      + "选的模型看不了图、而这一轮带了图片时，这一轮会自动换成能看图的。选了会记住", onchange: () => actions.setModel(modelEl.value) });
    modelRow = h("div", { class: "pcw-modelrow" }, h("span", { class: "pcw-muted", text: "助手模型" }), modelEl);
    barEl = h("div", { class: "pcw-bar" });
    noticeEl = h("div", { class: "pcw-notice", style: "display:none" });
    buildBar();
    stepsEl = h("div", { class: "pcw-steps" });
    assetsEl = h("div", { class: "pcw-assets" });
    const left = h("div", { class: "pcw-left" }, listEl, h("div", { class: "pcw-input" }, draftEl, statusEl, h("div", { class: "pcw-row" }, inputEl, h("div", { style: "display:flex;flex-direction:column;gap:6px" }, attachBtn, sendBtn)), modelRow, fileInput));
    const right = h("div", { class: "pcw-right" }, barEl, noticeEl, stepsEl, assetsEl);
    root = h("div", { class: "pcw", tabindex: "-1", onpaste: (e) => actions.onPaste(e), ondragover: (e) => { e.preventDefault(); e.stopPropagation(); root.classList.add("pcw-drop"); }, ondragleave: () => root.classList.remove("pcw-drop"), ondrop: (e) => { root.classList.remove("pcw-drop"); actions.onDrop(e); } },
      h("div", { class: "pcw-top" }, h("b", { text: "AI 工作台" }), h("span", { class: "pcw-sub", text: "说一句话 → 排好步骤 → 一键跑完。原来的节点图 / 应用在后面，点右上角按钮回去" }),
        h("button", { class: "pcw-link", text: "清空全部", onclick: () => actions.clearAll() }), h("button", { class: "pcw-btn pcw-btn-sm", text: "回到节点图 / 应用", title: "关掉工作台（左边的「AI 工作台」图标或左边缘的蓝色按钮可以再打开）", onclick: () => actions.close() })),
      h("div", { class: "pcw-main" }, left, right));
    // 键盘事件只给工作台用：不让 ComfyUI 的全局快捷键（Delete 删节点、Ctrl+Enter 排队……）在这里误触发
    for (const t of ["keydown", "keyup", "keypress"]) root.addEventListener(t, (e) => e.stopPropagation());
    handle = h("button", { class: "pcw-handle", text: "AI 工作台", title: "打开 AI 工作台", onclick: () => actions.open() });
    document.body.append(root, handle);
    renderAll();
  }

  function renderAll() {
    renderChat();
    renderPlan();
    renderAssets();
    renderDrafts();
    renderModels();
    refreshSend();
  }

  function setOpen(on) {
    root.classList.toggle("pcw-open", on);
    handle.style.display = on ? "none" : "block";
    if (on) { listEl.scrollTop = listEl.scrollHeight; }
  }

  build();
  return {
    get root() { return root; },
    get input() { return inputEl; },
    renderAll, renderChat, renderPlan, renderAssets, renderDrafts, renderModels, refreshStatus, refreshSend, tickRunning, setBusy, setStatus, setOpen, autoGrow,
    isOpen: () => root.classList.contains("pcw-open"),
    scrollChat: () => { listEl.scrollTop = listEl.scrollHeight; },
    insertText: (text) => { inputEl.value = (inputEl.value ? inputEl.value.replace(/\s*$/, " ") : "") + text; autoGrow(); inputEl.focus(); },
    clearInput: () => { inputEl.value = ""; autoGrow(); },
    focusInput: () => { if (!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches)) inputEl.focus({ preventScroll: true }); },       // 触屏设备上聚焦会弹出软键盘，不去抢（它们本来也没有物理键盘的快捷键问题）
    inputText: () => inputEl.value.trim(),
    dropEl: () => root,
  };
}
