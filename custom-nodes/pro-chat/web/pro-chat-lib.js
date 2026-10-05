// AI 助手前端的纯函数：不依赖 ComfyUI 的页面对象，tests/test_chat_js.py 用 node 直接测。
// ComfyUI 会把 web/ 下的每个 .js 都当扩展加载；这个文件只导出函数，加载它没有任何副作用。

const EXT = {
  image: ["png", "jpg", "jpeg", "webp", "gif", "bmp"],
  video: ["mp4", "webm", "mov", "mkv"],
  audio: ["mp3", "wav", "flac", "ogg", "m4a", "aac"],
  text: ["txt", "md", "json", "csv"],
};

export const ERROR_LABEL = "出错信息";   // 应用里「只在出错时出现」的文字结果，标题都以它开头
export const MAX_STORED_MESSAGES = 60;
export const MAX_IMAGES = 12;           // 一次对话里模型最多看到的图片数，和后端 chat.MAX_IMAGES 一致（tests 里核对）

export function fileKind(name) {
  const m = /\.([A-Za-z0-9]+)$/.exec(String(name || ""));
  const ext = m ? m[1].toLowerCase() : "";
  for (const [kind, list] of Object.entries(EXT)) if (list.includes(ext)) return kind;
  return "other";
}

// /view 的相对地址（外面再用 api.apiURL 加上前缀）
export function viewPath(file) {
  const q = new URLSearchParams({ filename: file.filename, type: file.type || "output", subfolder: file.subfolder || "" });
  return "/view?" + q.toString();
}

// 上传的图片 "助手/a.png" → /view 的相对地址
export function inputImagePath(name) {
  const i = String(name).lastIndexOf("/");
  return viewPath({ filename: i < 0 ? name : name.slice(i + 1), subfolder: i < 0 ? "" : name.slice(0, i), type: "input" });
}

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
// （比如音乐的 Suno 渠道出错：工作流本身「成功」跑完了，只是产出里只有一段出错文字）；其余算成功（部分出错的会在结果卡片里用红字列出）
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

// 方案卡片还能不能操作（改设置 / 点生成）：没有状态、等着确认的，以及上次没成功 / 被停止 / 刷新时没跟上的（可以改了再试）
export function isLivePlanState(st) {
  return !st || st === "open" || st === "failed" || st === "stopped" || st === "interrupted";
}

// 把这轮新附的图加进对话的图片列表：同名的不重复占编号（内容相同的文件上传后 ComfyUI 会返回同一个名字），超过 max 张丢最早的；
// 返回新列表和这轮新图在新列表里的编号（从 1 起，给后端 new_images 用）。名字和编号只在这一轮请求里对应，历史消息每次都按名字重新换算。
export function addImages(images, added, max = MAX_IMAGES) {
  const list = (images || []).filter((n) => !added.includes(n));
  for (const n of added) if (!list.includes(n)) list.push(n);
  const kept = list.slice(-max);
  const newIdx = [...new Set(added)].map((n) => kept.indexOf(n) + 1).filter((i) => i > 0);
  return { images: kept, newIdx };
}

// 「当前方案」= 最近一条带方案的消息的方案（被放弃的不算）。刷新后用它还原，保证 state.plan 和卡片是同一个对象
// （分开存再分开还原，卡片里手动改的内容就不会传给模型，「放弃」也清不掉当前方案）
export function currentPlan(messages) {
  for (let i = (messages || []).length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.plan) return m.planState === "dismissed" ? null : m.plan;
  }
  return null;
}

// 方案 → /pro/run 的请求体（后端会再按目录校验一遍）
export function runPayload(plan, clientId) {
  return { workflow: plan.workflow, mode: plan.mode || null, fields: { ...(plan.fields || {}) }, images: { ...(plan.images || {}) }, client_id: clientId || "" };
}

// 方案 → /pro/chat 的「当前方案」
export function serverPlan(plan) {
  if (!plan) return null;
  return { workflow: plan.workflow, mode: plan.mode || null, fields: { ...(plan.fields || {}) }, images: { ...(plan.images || {}) } };
}

// 界面里的消息 → 发给模型的对话。助手那几条还原成它自己该输出的 JSON 格式（只带文字回复和方案），
// 这样它多轮之后仍然按格式回复，也记得自己上一次给的方案。
export function apiMessages(messages, imageNames) {
  const out = [];
  for (const m of messages) {
    if (m.role === "user" && String(m.text || "").trim()) {
      out.push({ role: "user", content: m.text });
    } else if (m.role === "assistant" && String(m.text || "").trim()) {
      out.push({ role: "assistant", content: JSON.stringify({ reply: m.text, plan: compactPlan(m.plan, imageNames) }) });
    }
  }
  return out;
}

function compactPlan(plan, imageNames) {
  if (!plan) return null;
  const opts = (plan.schema && plan.schema.mode && plan.schema.mode.options) || [];
  const mi = plan.mode ? opts.indexOf(plan.mode) + 1 : 0;
  const images = {};
  for (const [k, name] of Object.entries(plan.images || {})) {
    const i = (imageNames || []).indexOf(name);
    if (i >= 0) images[k] = i + 1;
  }
  return { workflow: plan.workflow, mode: mi > 0 ? mi : null, fields: plan.fields || {}, images, note: plan.note || "" };
}

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

// 存进 localStorage 的内容：只留最近的消息。正在运行、而且已经拿到 prompt_id 的方案（m.run）刷新后接着等结果；
// 还没拿到 prompt_id（提交的那一刻刷新了）的跟不上了，标成已中断
export function toStorage(state) {
  const messages = state.messages.slice(-MAX_STORED_MESSAGES).map((m) => (m.planState === "running" && !(m.run && m.run.promptId) ? { ...m, planState: "interrupted" } : m));
  const names = new Set();
  for (const m of messages) {
    for (const n of m.images || []) names.add(n);
    if (m.plan) for (const n of Object.values(m.plan.images || {})) names.add(n);
  }
  const images = state.images.filter((n) => names.has(n));
  return JSON.stringify({ v: 1, messages, images });
}

export function fromStorage(text) {
  try {
    const d = JSON.parse(text);
    if (!d || d.v !== 1 || !Array.isArray(d.messages)) return null;
    return { messages: d.messages, images: Array.isArray(d.images) ? d.images : [], plan: currentPlan(d.messages) };
  } catch (e) {
    return null;
  }
}
