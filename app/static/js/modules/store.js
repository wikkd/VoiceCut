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
    // 有未落盘改动的素材不做覆盖式重载：否则刚改好的说话人指派被服务端旧版本
    // 抹掉，且紧接着那次保存会把旧版本写回服务端（永久固化）。见 fillItemStates。
    if (!(state.dirtyItems && state.dirtyItems.has(item.id))) {
      try {
        const proj = await api(`/api/items/${item.id}/project`);
        state.segmentsByItem.set(item.id, proj.segments || []);
        state.speakerSegsByItem.set(item.id, proj.speaker_segments || []);
      } catch (e) {
        state.segmentsByItem.set(item.id, []);
        state.speakerSegsByItem.set(item.id, []);
      }
    }
    state.speakerSegs = state.speakerSegsByItem.get(item.id) || [];
  }

  let saveTimer = null;
  let saveInFlight = false;
  let savePending = false;             // 保存进行中又来的请求：不能静默丢弃
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
    if (!ids.length) return;
    if (saveInFlight) { savePending = true; return; }   // 排队补跑，见 finally
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
      // 此前在飞期间到来的请求是**直接 return**：调用方（如 loadAllItemData 拉取前的
      // 落盘、selectProject、beforeunload 兜底）以为已经保存，实际改动还留在 dirtyItems
      // 里；随后服务端旧版本一旦回填内存，这份改动就被当成"用户数据"写回并永久固化。
      // 改为标记 pending + 收尾补跑一次（dirty 已清则立即返回，不会自旋）。
      if (savePending) { savePending = false; saveProjectNow(); }
    }
  }

  let poolTimer = null;
  let poolInFlight = false;
  let poolVer = 0;
  let poolRetry = null;
  let poolFails = 0;      // 连续失败次数：网络持续异常时停止自动重试，避免请求风暴
  let poolStuck = 0;      // 连续「因 identifying 被挡」次数：用于识别标志泄漏自检
  // 识别/反馈进行中无法落盘、或保存期间又有新改动 → 轮询补存，避免改名等改动被静默丢弃
  function schedulePoolRetry(pid) {   // pid：绑定发起时的项目，切项目后旧重试自动作废
    if (poolRetry || !state.poolDirty || poolFails >= 3) return;
    poolRetry = setTimeout(() => {
      poolRetry = null;
      if (!state.currentProject || state.currentProject.id !== pid) return;
      // 标志泄漏自检：identifying 仍为 true 但已无任何活跃任务 → 守卫失效（任务异常/取消未复位），
      // 此时继续等待会永久丢弃改动。连续 3 次（约 2.7s）确认后强制落盘，并在 toast 中明示。
      if (state.identifying && (!state.activeTasks || state.activeTasks.size === 0)) {
        poolStuck++;
        if (poolStuck >= 3) {
          poolStuck = 0;
          toast("识别状态已结束但标记未复位，已强制保存角色池改动", 5000);
          savePoolNow(true);
          return;
        }
      } else {
        poolStuck = 0;
      }
      savePoolNow();
    }, 900);
  }
  function scheduleSavePool() {
    state.poolDirty = true;
    poolVer++;
    if (poolTimer) clearTimeout(poolTimer);
    poolTimer = setTimeout(savePoolNow, 400);
  }
  async function savePoolNow(force) {
    // 识别(speakers 任务)运行期间禁止落盘：此刻的 state.characters 可能是
    // 旧快照，落盘会把后端刚写好的识别结果整池回滚（真实发生过）。
    // 但此前是直接 return —— 识别期间/标志未复位时的改名会被永久丢弃，之后无人重试。
    // 改为延后重试：等 identifying 解除后自动补存（force 用于确认标志泄漏后强制落盘）。
    if (state.identifying && !force) { schedulePoolRetry(state.currentProject ? state.currentProject.id : null); return; }
    if (!state.poolDirty || !state.currentProject || poolInFlight) return;
    const projectId = state.currentProject.id;
    const v = poolVer;
    poolInFlight = true;
    try {
      await api(`/api/projects/${projectId}/characters`, { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ characters: state.characters }) });
      if (poolVer === v) state.poolDirty = false;
      poolFails = 0;
    } catch (e) {
      poolFails++;
      toast("角色池保存失败，改动已保留待重试: " + e.message, 6000);
    } finally {
      poolInFlight = false;
      if (state.poolDirty) schedulePoolRetry(projectId);   // 保存期间又有新改动 → 再补存一次
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
    // 批量端点结果填充（项目切换一次性拉全量，替代逐素材请求）。
    // 但**有未落盘改动的素材不覆盖**：识别/声纹反馈任务结束时 pool.js 会
    // loadAllItemData() 重拉服务端状态，此刻用户的说话人改动可能还在 400ms
    // 防抖里（或保存请求被 saveInFlight 挡下仍在队列）。若用服务端旧版本覆盖
    // 内存，紧接着那次保存就把旧版本当"用户数据"写回，人工改的说话人被永久
    // 抹掉 —— 用户现象「改完说话人又被识别改回去」。dirty 素材以本地为准。
    for (const [id, st] of Object.entries(states || {})) {
      if (state.dirtyItems && state.dirtyItems.has(id)) continue;
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
