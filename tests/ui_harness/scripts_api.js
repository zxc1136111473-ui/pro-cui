// 假的 ComfyUI api 对象
const target = new EventTarget();
export const api = {
  clientId: "harness-client",
  apiURL: (path) => "/api" + path,
  fetchApi: (route, opts) => fetch("/api" + route, opts),
  addEventListener: (t, f) => target.addEventListener(t, f),
  removeEventListener: (t, f) => target.removeEventListener(t, f),
  dispatch: (t, detail) => target.dispatchEvent(new CustomEvent(t, { detail })),
};
window.__api = api;
