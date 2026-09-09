// VoiceCut 前端 — 共享应用状态 + 工作区布局（3×3 网格 / 分隔条 / 面板换位 / localStorage 记忆）
import { $, $$, clampN } from "./util.js";

export const state = {
  items: [],
  projects: [],
  currentProject: null,
  currentItem: null,
  ws: null,
  regions: null,
  selection: null,          // {start, end}
  selectionRegion: null,
  loop: false,
  playing: false,
  segmentsByItem: new Map(), // itemId -> [{start,end,text,language,speaker}]
  activeTasks: new Map(),    // taskId -> {msg, progress}
  pollTimer: null,
  auditioning: null,         // {start, end, itemId}
  zoomLevel: 0,              // 0=fit, >=1 缩放级别
  dragRegion: null,          // 正在拖拽（未松手）的选区
  multiRegions: [],          // Ctrl+→ 累积的多选区 [{start,end,region}]
  ctrlMarking: false,        // 正在通过 Ctrl+→ 添加标记（不替换旧选区）
  auditionSeq: null,         // 多选顺序试听队列
  auditionIdx: 0,
  characters: [],            // 当前项目共享角色池 [{id,name,color,speakerLabels,created,embedding?}]
  speakerSegs: [],           // 当前素材说话人分段 [{start,end,label}]
  speakerSegsByItem: new Map(), // itemId -> 说话人分段 [{start,end,label}]
  dirtyItems: new Set(),     // 片段有改动待保存的素材 id
  poolDirty: false,          // 角色池有改动待保存
  selectedSegs: new Set(),   // 片段列表多选行索引
  poolMerge: new Set(),      // 角色池合并勾选集
  subs: [],                 // 实时字幕 [{start,end,text}]
  currentSubIdx: -1,        // 当前播放头命中的字幕行索引
  bootErr: null,
  autoAnalyze: true,         // 导入后后台自动生成字幕 + 识别说话人
};

let toastTimer = null;
export function toast(msg, ms = 4000) {
  const el = $("#task-info");
  el.textContent = msg;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { if (!state.activeTasks.size) el.textContent = "就绪"; }, ms);
}

// ── 工作区布局 ─────────────────────────────────────────────
export const LS_KEY = "vc.workspace.v1";
export const PANELS = ["media", "video", "wave", "sub", "seg"];
export const PANEL_IDS = {
  media: "#media-panel", video: "#video-panel", wave: "#wave-box",
  sub: "#subtitle-panel", seg: "#segments-panel",
};
const LAYOUT_DEFAULTS = {
  area: { media: "media", video: "video", wave: "wave", sub: "sub", seg: "seg" },
  cols: { media: 220, sub: 240 },
  rows: { video: 200, seg: 220 },
  hidden: [],
};
function cloneLayout() {
  return {
    area: { ...LAYOUT_DEFAULTS.area },
    cols: { ...LAYOUT_DEFAULTS.cols },
    rows: { ...LAYOUT_DEFAULTS.rows },
    hidden: [],
  };
}
function loadLayout() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return cloneLayout();
    const j = JSON.parse(raw);
    const L = cloneLayout();
    if (j && typeof j === "object") {
      if (j.area) PANELS.forEach((p) => { if (PANELS.includes(j.area[p])) L.area[p] = j.area[p]; });
      if (j.cols) { L.cols.media = Number(j.cols.media) || 220; L.cols.sub = Number(j.cols.sub) || 240; }
      if (j.rows) { L.rows.video = Number(j.rows.video) || 200; L.rows.seg = Number(j.rows.seg) || 220; }
      if (Array.isArray(j.hidden)) L.hidden = j.hidden.filter((p) => PANELS.includes(p));
    }
    return L;
  } catch (e) { return cloneLayout(); }
}
export const layout = loadLayout();
export function saveLayout() { try { localStorage.setItem(LS_KEY, JSON.stringify(layout)); } catch (e) {} }

export function applyLayout() {
  const ws = $("#workspace");
  if (!ws) return;
  const eff = (px, hid) => hid ? 0 : px;
  ws.style.setProperty("--w-media", eff(layout.cols.media, layout.hidden.includes("media")) + "px");
  ws.style.setProperty("--w-sub", eff(layout.cols.sub, layout.hidden.includes("sub")) + "px");
  ws.style.setProperty("--h-video", eff(layout.rows.video, layout.hidden.includes("video")) + "px");
  ws.style.setProperty("--h-wave", layout.hidden.includes("wave") ? "0px" : "1fr");
  ws.style.setProperty("--h-seg", eff(layout.rows.seg, layout.hidden.includes("seg")) + "px");
  PANELS.forEach((p) => {
    const el = $(PANEL_IDS[p]);
    if (!el) return;
    el.dataset.slot = layout.area[p];
    el.style.gridArea = layout.area[p];
    el.classList.toggle("panel-hidden", layout.hidden.includes(p));
  });
  $$(".splitter").forEach((sp) => {
    const k = sp.dataset.split;
    sp.classList.toggle("hidden",
      (k === "col-media" && layout.hidden.includes("media")) ||
      (k === "col-sub" && layout.hidden.includes("sub")) ||
      (k === "row-video" && layout.hidden.includes("video")) ||
      (k === "row-seg" && layout.hidden.includes("seg")));
  });
  updatePanelMenu();
}
export function updatePanelMenu() {
  $$("[data-act='panel-toggle']").forEach((b) => {
    const on = !layout.hidden.includes(b.dataset.panel);
    b.classList.toggle("panel-on", on);
    b.classList.toggle("panel-off", !on);
  });
}
export function togglePanel(p) {
  if (!PANELS.includes(p)) return;
  const i = layout.hidden.indexOf(p);
  if (i >= 0) layout.hidden.splice(i, 1); else layout.hidden.push(p);
  applyLayout(); saveLayout();
}
export function resetLayout() {
  try { localStorage.removeItem(LS_KEY); } catch (e) {}
  const d = cloneLayout();
  layout.area = d.area; layout.cols = d.cols; layout.rows = d.rows; layout.hidden = d.hidden;
  applyLayout(); saveLayout();
  toast("已恢复默认布局");
}
export function swapPanels(a, b) {
  if (!PANELS.includes(a) || !PANELS.includes(b) || a === b) return;
  const ta = layout.area[a], tb = layout.area[b];
  layout.area[a] = tb; layout.area[b] = ta;
  applyLayout(); saveLayout();
}
export function setupSplitters() {
  $$(".splitter").forEach((sp) => {
    sp.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const kind = sp.dataset.split;
      sp.setPointerCapture(e.pointerId);
      sp.classList.add("active");
      const ws = $("#workspace");
      const rect = ws.getBoundingClientRect();
      const c0 = { ...layout.cols }, r0 = { ...layout.rows };
      const onMove = (ev) => {
        const dx = ev.clientX - e.clientX, dy = ev.clientY - e.clientY;
        if (kind === "col-media") layout.cols.media = clampN(c0.media + dx, 140, rect.width - layout.cols.sub - 200);
        else if (kind === "col-sub") layout.cols.sub = clampN(c0.sub - dx, 170, rect.width - layout.cols.media - 200);
        else if (kind === "row-video") layout.rows.video = clampN(r0.video + dy, 100, rect.height - layout.rows.seg - 120);
        else if (kind === "row-seg") layout.rows.seg = clampN(r0.seg - dy, 120, rect.height - layout.rows.video - 100);
        applyLayout();
      };
      const onUp = () => {
        sp.removeEventListener("pointermove", onMove);
        sp.removeEventListener("pointerup", onUp);
        sp.classList.remove("active");
        saveLayout();
      };
      sp.addEventListener("pointermove", onMove);
      sp.addEventListener("pointerup", onUp);
    });
  });
}
let gripDrag = null;
function hitPanel(x, y, except) {
  return PANELS.find((k) => {
    if (k === except || layout.hidden.includes(k)) return false;
    const r = $(PANEL_IDS[k]).getBoundingClientRect();
    return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  });
}
export function setupGripDrag() {
  $$(".panel-grip").forEach((grip) => {
    grip.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      if (e.target.closest("button, input, label, select, .menu")) return;
      const panelEl = grip.closest(".panel");
      const p = panelEl && PANELS.find((k) => $(PANEL_IDS[k]) === panelEl);
      if (!p) return;
      gripDrag = { p, x: e.clientX, y: e.clientY, moved: false };
      grip.setPointerCapture(e.pointerId);
    });
    grip.addEventListener("pointermove", (e) => {
      if (!gripDrag) return;
      const dx = e.clientX - gripDrag.x, dy = e.clientY - gripDrag.y;
      if (!gripDrag.moved && Math.hypot(dx, dy) < 5) return;
      gripDrag.moved = true;
      document.body.classList.add("panel-dragging");
      const target = hitPanel(e.clientX, e.clientY, gripDrag.p);
      $$(".panel.drop-target").forEach((el) => el.classList.remove("drop-target"));
      if (target) $(PANEL_IDS[target]).classList.add("drop-target");
    });
    const endDrag = (e) => {
      if (!gripDrag) return;
      const p0 = gripDrag.p;
      const target = gripDrag.moved ? hitPanel(e.clientX, e.clientY, p0) : null;
      gripDrag = null;
      document.body.classList.remove("panel-dragging");
      $$(".panel.drop-target").forEach((el) => el.classList.remove("drop-target"));
      if (target) { swapPanels(p0, target); toast("已交换面板（可再拖标题栏换回）"); }
    };
    grip.addEventListener("pointerup", endDrag);
    grip.addEventListener("pointercancel", endDrag);
  });
}
export function setupPanelClose() {
  $$("[data-close-panel]").forEach((b) => {
    b.addEventListener("click", () => togglePanel(b.dataset.closePanel));
  });
}
export function initWorkspace() {
  applyLayout();
  setupSplitters();
  setupGripDrag();
  setupPanelClose();
}
