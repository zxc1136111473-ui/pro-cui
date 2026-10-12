// 假的 ComfyUI 页面对象（tests/ui_harness 用）：只实现工作台用到的那几个接口
const listeners = {};
export const app = {
  registerExtension(ext) { app._ext = ext; Promise.resolve().then(() => ext.setup()); },
  extensionManager: {
    registerSidebarTab(tab) { window.__sidebarTab = tab; },
    sidebarTab: { activeSidebarTabId: null, toggleSidebarTab(id) { this.activeSidebarTabId = this.activeSidebarTabId === id ? null : id; window.__toggled = (window.__toggled || 0) + 1; } },
  },
  graph: (window.__graph = { _nodes: {}, getNodeById(id) { return this._nodes[id] || null; }, setDirtyCanvas() {} }),         // window.__graph：检查脚本读「在应用里打开」往节点里填了什么
  async loadGraphData(wf, clean, restore, name) {
    window.__loaded = { name, nodes: wf.nodes.length };
    window.__graph._nodes = {};
    for (const n of wf.nodes) window.__graph._nodes[n.id] = { id: n.id, widgets: (n.inputs || []).filter((i) => i.widget).map((i) => ({ name: i.name, value: null, options: { values: [] } })) };
  },
};
