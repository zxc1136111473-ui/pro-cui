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
.pcw-hint{font-size:11px;opacity:.55}
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
.pcw-filepic{margin-top:4px;align-self:flex-start;max-height:96px;max-width:100%;border-radius:6px;border:1px solid var(--border-color,#4e4e4e)}
.pcw-results{margin-top:10px;display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start}
.pcw-res{position:relative;max-width:100%}
.pcw-res img{display:block;max-width:100%;max-height:280px;border-radius:6px;cursor:zoom-in}
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
`;

const KIND_ICON = { video: "▶", audio: "♪", image: "▣", other: "▫" };

export function createView({ state, url, actions, catalog, catalogStatus }) {
  let root, listEl, inputEl, sendBtn, draftEl, statusEl, fileInput, stepsEl, barEl, noticeEl, assetsEl, handle;
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
      m.planText ? h("div", { class: "pcw-planchip", text: "方案已更新到右边：" + m.planText }) : null);
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
    return h("div", { class: "pcw-field" + (wide ? " pcw-wide" : "") + (changed ? " pcw-changed" : "") },
      h("label", null, h("span", { text: f.label }), reset), kids, f.hint ? h("div", { class: "pcw-hint", text: f.hint }) : null);
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
    return h("div", { class: "pcw-field pcw-wide" + (cur ? " pcw-changed" : "") + (need && !cur ? " pcw-missing" : "") },
      h("label", null, h("span", { text: f.label + (need ? "（必填）" : "") }),
        cur ? h("button", { class: "pcw-link", text: "↺ 用默认", disabled: locked, onclick: () => { delete step.files[f.key]; actions.changed(); renderPlan(); } }) : null), row, pic);
  }

  // ── 方案：结果 ──────────────────────────────────────────────────────────────────────────────────────
  function resultView(step) {
    const r = step.result;
    if (!r) return null;
    const box = h("div", { class: "pcw-results" });
    for (const it of r.items || []) {
      const src = url(lib.viewPath(it.file));
      const bar = it.asset ? h("div", { class: "pcw-resbar" }, h("span", { text: "素材 #" + it.asset }),
        h("button", { class: "pcw-link", text: "在聊天里引用", onclick: () => actions.mention(it.asset) })) : null;
      if (it.kind === "image") box.append(h("div", { class: "pcw-res" }, h("img", { src, loading: "lazy", onclick: () => window.open(src, "_blank") }), bar));
      else if (it.kind === "video") box.append(h("div", { class: "pcw-res" }, h("video", { src, controls: true, preload: "metadata" }), bar));
      else if (it.kind === "audio") box.append(h("div", { class: "pcw-res" }, h("audio", { src, controls: true, preload: "metadata" }), bar));
      else if (it.kind === "text") {
        const pre = h("pre", { text: it.text != null ? it.text : "加载中…" });
        const copy = navigator.clipboard && !it.isError ? h("button", { class: "pcw-link", text: "复制", onclick: (e) => actions.copyText(pre.textContent, e.target) }) : null;
        box.append(h("div", { class: "pcw-text" + (it.isError ? " pcw-err" : "") }, it.label || copy ? h("div", { class: "pcw-tl" }, h("span", { text: it.label || "" }), copy) : null, pre));
        if (it.text == null) actions.loadText(it, src, (t) => { pre.textContent = t; });
      }
    }
    if (r.error) box.append(h("div", { class: "pcw-err", style: "flex:1 1 100%", text: r.error }));
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
      return `${t} · 已 ${lib.elapsedText(Date.now() - ((step.run && step.run.started) || Date.now()))}${r && r.node ? " · " + lib.shortTitle(r.node) : ""}`;
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
    const badge = h("span", { class: `pcw-badge pcw-b-${st.kind}`, text: badgeText(step, st) });
    const b = stepButtons(i);
    updateButtons(b, st);
    const head = h("div", { class: "pcw-step-head" }, h("span", { class: "pcw-num", text: String(i + 1) }), h("b", { class: "pcw-step-name", text: step.name }), badge, h("span", { class: "pcw-spacer" }),
      h("span", { style: "display:inline-flex;gap:6px" }, b.main, b.open, b.del));
    const box = h("div", { class: "pcw-step", "data-step": String(i) }, head, step.note ? h("div", { class: "pcw-note", text: step.note }) : null);       // 费用 / 额度的提醒统一放在方案顶部（updateBar），卡片里不再重复
    const common = Object.values(sc.fields || {}).filter((f) => !f.advanced);
    const advanced = Object.values(sc.fields || {}).filter((f) => f.advanced);
    const grid = h("div", { class: "pcw-fields" });
    if (sc.mode) {
      const cur = lib.currentMode(step);
      const sel = h("select", { "data-pc": `${i}:mode`, disabled: locked, onchange: () => { step.mode = sel.value === sc.mode.default ? null : sel.value; actions.changed(); renderPlan(); } },   // 重画：不同模式必填的文件不一样
        sc.mode.options.map((o) => h("option", { value: o, text: o, selected: o === cur })));
      grid.append(h("div", { class: "pcw-field pcw-wide" }, h("label", null, h("span", { text: sc.mode.label || "模式" })), sel));
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
    const results = resultView(step);
    if (results) box.append(results);                       // 注意：append(null) 会写出文字 "null"
    box._badge = badge;
    box._buttons = b;
    return box;
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
      });
    }
    if (runBtn) updateBar();                                                // 没有方案时也要更新：顶上「添加一步」的下拉写着应用目录是加载中 / 没读到
  }

  // 每秒更新一次：正在运行的那一步的「已 N 秒」
  function tickRunning() {
    if (!hasPlan()) return;
    stepEls.forEach((el, i) => {
      const step = state.plan.steps[i];
      if (el && step && step.state === "running") el._badge.textContent = badgeText(step, { kind: "running", reason: "" });
    });
  }

  // 顶部工具条：运行 / 停止、添加一步、清空方案。元素只建一次（见上面的说明），状态变了原地更新
  let runBtn, addSel, clearBtn;

  function buildBar() {
    runBtn = h("button", { class: "pcw-btn pcw-btn-primary", onclick: () => (state.running ? actions.stop() : actions.runAll()) });
    addSel = h("select", { title: "自己加一步（不用 AI 也能搭方案）", onchange: () => { if (addSel.value) actions.addStep(addSel.value); addSel.value = ""; } });
    const retryCatalog = () => { if (!(catalog() || []).length && catalogStatus && catalogStatus() === "failed") actions.reloadCatalog(); };      // 目录没读到时，点一下下拉就再试一次
    addSel.addEventListener("mousedown", retryCatalog);
    addSel.addEventListener("focus", retryCatalog);
    clearBtn = h("button", { class: "pcw-link", text: "清空方案", onclick: () => actions.clearPlan() });
    barEl.append(runBtn, addSel, h("span", { class: "pcw-spacer" }), clearBtn);
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
    if (addSel.options.length !== apps.length + 1) {                      // 目录加载好了才补选项（下拉打开时不去动它）
      addSel.replaceChildren(h("option", { value: "", text: holder }),
        ...apps.map((a) => h("option", { value: a.workflow, text: `${a.workflow} ${a.name}`, title: a.desc })));
    } else if (addSel.options[0].text !== holder) {
      addSel.options[0].text = holder;
    }
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
        stepsEl.append(h("div", { class: "pcw-empty" },
          h("div", { text: "还没有方案。在左边说一句话，我来排步骤；也可以用上面的「＋ 添加一步」自己搭。" }),
          h("div", { class: "pcw-muted", text: "每一步 = 一个应用（出图 / 出视频 / 配音 / 成片……）。前一步做出来的图、视频、音频，下一步可以直接用。" })));
      } else {
        state.plan.steps.forEach((_, i) => { const c = stepCard(i); stepEls[i] = c; stepsEl.append(c); });
      }
      updateBar();
    });
  }

  // ── 素材库 ──────────────────────────────────────────────────────────────────────────────────────────
  function renderAssets() {
    assetsEl.replaceChildren();
    assetsEl.style.display = state.assets.length ? "" : "none";
    assetsEl.append(h("div", { class: "pcw-assets-head", text: `素材库 · ${state.assets.length} 个（点一下可以在聊天里引用，比如「用 #3 做视频」）` }),
      h("div", { class: "pcw-assets-list" }, state.assets.map((a, k) => { if (!a.name) return null; const t = thumb(a, k + 1); t.classList.add("pcw-asset"); t.addEventListener("click", () => actions.mention(k + 1)); return t; })));
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
    barEl = h("div", { class: "pcw-bar" });
    noticeEl = h("div", { class: "pcw-notice", style: "display:none" });
    buildBar();
    stepsEl = h("div", { class: "pcw-steps" });
    assetsEl = h("div", { class: "pcw-assets" });
    const left = h("div", { class: "pcw-left" }, listEl, h("div", { class: "pcw-input" }, draftEl, statusEl, h("div", { class: "pcw-row" }, inputEl, h("div", { style: "display:flex;flex-direction:column;gap:6px" }, attachBtn, sendBtn)), fileInput));
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
    renderAll, renderChat, renderPlan, renderAssets, renderDrafts, refreshStatus, refreshSend, tickRunning, setBusy, setStatus, setOpen, autoGrow,
    isOpen: () => root.classList.contains("pcw-open"),
    scrollChat: () => { listEl.scrollTop = listEl.scrollHeight; },
    insertText: (text) => { inputEl.value = (inputEl.value ? inputEl.value.replace(/\s*$/, " ") : "") + text; autoGrow(); inputEl.focus(); },
    clearInput: () => { inputEl.value = ""; autoGrow(); },
    focusInput: () => { if (!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches)) inputEl.focus({ preventScroll: true }); },       // 触屏设备上聚焦会弹出软键盘，不去抢（它们本来也没有物理键盘的快捷键问题）
    inputText: () => inputEl.value.trim(),
    dropEl: () => root,
  };
}
