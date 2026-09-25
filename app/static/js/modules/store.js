// VoiceCut 前端 — 数据层：角色/片段模型 · 项目持久化（防抖 + 版本号防覆盖）· 撤销/重做
// 由 createStore(ctx) 创建；ctx 注入 util + state + 跨模块回调用闭包（规避循环依赖）。
// 本模块在功能模块之前创建，charById/newSegment/pushUndo 等对其余模块是真函数。
export function createStore(ctx) {
  const { api, toast, state, CHAR_PALETTE, segsFor, renderSegments, renderPool } = ctx;

  // ── 角色池 / 说话人自动匹配 ──
  let segCounter = 0;
  function uid(prefix) { return prefix + "_" + Date.now().toString(36) + "_" + (++segCounter).toString(36); }

  function paletteNext() { return CHAR_PALETTE[state.characters.length % CHAR_PALETTE.length]; }
  function charById(id) { return state.characters.find(c => c.id === id) || null; }

  function newSegment(start, end, text, language) {
    const sp = autoCharacterFor(start, end);
    return { id: uid("s"), start, end, text: text || "", language: language || "JP",
             speakerLabel: sp.speakerLabel, characterId: sp.characterId, mixed: !!sp.mixed };
  }

  function speakerLabelAt(t) {
    for (const s of state.speakerSegs) if (s.label && t >= s.start && t < s.end) return s.label;
    return null;
  }
  function mixedAtRange(start, end) {
    const cover = new Map();
    for (const s of state.speakerSegs) {
      if (!s.label) continue;
      const ov = Math.min(end, s.end) - Math.max(start, s.start);
      if (ov > 0) cover.set(s.label, (cover.get(s.label) || 0) + ov);
    }
    if (cover.size < 2) return false;
    const items = Array.from(cover.entries()).sort((a, b) => b[1] - a[1]);
    const labeled = items.reduce((a, x) => a + x[1], 0);
    const second = items[1][1];
    return second / labeled >= 0.35 && second >= 0.25;
  }
  function autoCharacterFor(start, end) {
    let best = null, bestOv = 0;
    for (const s of state.speakerSegs) {
      if (!s.label) continue;
      const ov = Math.min(end, s.end) - Math.max(start, s.start);
      if (ov > bestOv) { bestOv = ov; best = s.label; }
    }
    const mixed = mixedAtRange(start, end);
    if (!best) return { characterId: null, speakerLabel: null, mixed };
    const key = (state.currentItem ? state.currentItem.id : "") + ":" + best;
    const ch = state.characters.find(c => (c.speakerLabels || []).includes(key));
    return { characterId: mixed ? null : (ch ? ch.id : null), speakerLabel: best, mixed };
  }

  async function loadProject(item, force) {
    if (!force && state.segmentsByItem.has(item.id)) {
      state.speakerSegs = state.speakerSegsByItem.get(item.id) || [];
      return;
    }
    try {
      const proj = await api(`/api/items/${item.id}/project`);
      state.segmentsByItem.set(item.id, proj.segments || []);
      state.speakerSegsByItem.set(item.id, proj.speaker_segments || []);
    } catch (e) {
      state.segmentsByItem.set(item.id, []);
      state.speakerSegsByItem.set(item.id, []);
    }
    state.speakerSegs = state.speakerSegsByItem.get(item.id) || [];
  }

  let saveTimer = null;
  let saveInFlight = false;
  const dirtyVer = new Map();          // itemId -> 自增版本：避免覆盖保存期间产生的新改动
  function markDirty(id) {
    state.dirtyItems.add(id);
    dirtyVer.set(id, (dirtyVer.get(id) || 0) + 1);
  }
  function scheduleSaveProject(itemId) {
    const id = itemId || (state.currentItem ? state.currentItem.id : null);
    if (id) markDirty(id);
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveProjectNow, 400);
  }
  async function saveProjectNow() {
    const ids = Array.from(state.dirtyItems);
    if (!ids.length || saveInFlight) return;
    const verAt = new Map(ids.map(id => [id, dirtyVer.get(id) || 0]));
    saveInFlight = true;
    try {
      await Promise.all(ids.map(id => api(`/api/items/${id}/project`, { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ segments: segsFor(id), speaker_segments: state.speakerSegsByItem.get(id) || [] }) })));
      // 仅当保存期间没有新的改动时才清除脏标记，避免静默丢改动
      ids.forEach(id => {
        if ((dirtyVer.get(id) || 0) === verAt.get(id)) {
          state.dirtyItems.delete(id);
          dirtyVer.delete(id);
        }
      });
    } catch (e) {
      toast("保存失败，改动已保留待重试: " + e.message, 6000);
    } finally {
      saveInFlight = false;
    }
  }

  let poolTimer = null;
  let poolInFlight = false;
  let poolVer = 0;
  function scheduleSavePool() {
    state.poolDirty = true;
    poolVer++;
    if (poolTimer) clearTimeout(poolTimer);
    poolTimer = setTimeout(savePoolNow, 400);
  }
  async function savePoolNow() {
    // 识别(speakers 任务)运行期间禁止落盘：此刻的 state.characters 可能是
    // 旧快照，落盘会把后端刚写好的识别结果整池回滚（真实发生过）。
    if (state.identifying) return;
    if (!state.poolDirty || !state.currentProject || poolInFlight) return;
    const projectId = state.currentProject.id;
    const v = poolVer;
    poolInFlight = true;
    try {
      await api(`/api/projects/${projectId}/characters`, { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ characters: state.characters }) });
      if (poolVer === v) state.poolDirty = false;
    } catch (e) {
      toast("角色池保存失败，改动已保留待重试: " + e.message, 6000);
    } finally {
      poolInFlight = false;
    }
  }

  // ── 撤销 / 重做（片段 + 角色池，快照式，刷新即清空） ──
  // pushUndo(label)：label 用于撤销/重做时在 toast 中显示是哪一步操作。
  const UNDO_MAX = 50;
  const undoStack = [];   // 元素 { snap, label }
  const redoStack = [];
  function workspaceSnapshot() {
    const segs = {};
    state.segmentsByItem.forEach((list, id) => { segs[id] = JSON.parse(JSON.stringify(list || [])); });
    return { segs, characters: JSON.parse(JSON.stringify(state.characters || [])) };
  }
  function pushUndo(label) {
    undoStack.push({ snap: workspaceSnapshot(), label: label || "操作" });
    if (undoStack.length > UNDO_MAX) undoStack.shift();
    redoStack.length = 0;
  }
  function restoreSnapshot(snap) {
    // 合并式恢复：仅覆盖快照中存在的素材，保留快照之后新加载/新导入素材的数据，
    // 避免 undo 把新素材的片段从内存里整个抹掉。
    const segs = new Map(state.segmentsByItem);
    Object.keys(snap.segs || {}).forEach(id => segs.set(id, snap.segs[id]));
    state.segmentsByItem = segs;
    state.characters = snap.characters || [];
    state.selectedSegs = new Set();
    state.poolMerge = new Set();
    state.segmentsByItem.forEach((_, id) => markDirty(id));
    state.poolDirty = true;
    poolVer++;
    renderSegments();
    renderPool();
    saveProjectNow();
    savePoolNow();
  }
  function undo() {
    if (!undoStack.length) return toast("没有可撤销的操作");
    const entry = undoStack.pop();
    redoStack.push({ snap: workspaceSnapshot(), label: entry.label });
    restoreSnapshot(entry.snap);
    toast("已撤销：" + entry.label);
  }
  function redo() {
    if (!redoStack.length) return toast("没有可重做的操作");
    const entry = redoStack.pop();
    undoStack.push({ snap: workspaceSnapshot(), label: entry.label });
    restoreSnapshot(entry.snap);
    toast("已重做：" + entry.label);
  }

  function fillItemStates(states) {
    // 批量端点结果填充（项目切换一次性拉全量，替代逐素材请求）
    for (const [id, st] of Object.entries(states || {})) {
      state.segmentsByItem.set(id, st.segments || []);
      state.speakerSegsByItem.set(id, st.speaker_segments || []);
    }
    if (state.currentItem) {
      state.speakerSegs = state.speakerSegsByItem.get(state.currentItem.id) || [];
    }
  }

  return { uid, paletteNext, charById, newSegment, speakerLabelAt, mixedAtRange,
           autoCharacterFor, loadProject, fillItemStates, markDirty, scheduleSaveProject, saveProjectNow,
           scheduleSavePool, savePoolNow, pushUndo, undo, redo };
}
