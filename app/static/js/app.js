import WaveSurfer from "/static/vendor/wavesurfer.esm.js";
import Timeline from "/static/vendor/plugins/timeline.esm.js";
import Regions from "/static/vendor/plugins/regions.esm.js";
import Minimap from "/static/vendor/plugins/minimap.esm.js";

import { $, $$, esc, shortName, fmtT, fmtSel, fmtDur, api, clampN,
         SEG_MIN, SEG_MAX, SEEK_STEP, SEEK_FAST, VOL_STEP, CHAR_PALETTE } from "/static/js/util.js";
import { state, toast, layout, applyLayout, saveLayout, resetLayout,
         togglePanel, swapPanels, initWorkspace, PANELS } from "/static/js/state.js";
import { createTraining } from "/static/js/modules/training.js";
import { createSubtitles } from "/static/js/modules/subtitles.js";
import { createIo } from "/static/js/modules/io.js";
import { createWaveform } from "/static/js/modules/waveform.js";
import { createSegments } from "/static/js/modules/segments.js";

// VoiceCut 前端 — wavesurfer v7 (UMD) + Flask REST
(() => {
  "use strict";



  // ── 引导 ───────────────────────────────────────────────
  function fail(msg) {
    state.bootErr = msg;
    document.body.dataset.vc = "error";
    $("#boot-state").textContent = "❌ " + msg;
  }
  const LS_PROJECT = "vc.project.v1";
  async function boot() {
    if (typeof WaveSurfer === "undefined") return fail("wavesurfer 未加载");
    try {
      const cfg = await api("/api/config");
      const projects = await api("/api/projects");
      state.projects = projects;
      renderProjectSelect();
      let pid = null;
      try { pid = localStorage.getItem(LS_PROJECT); } catch (e) {}
      const proj = projects.find(p => p.id === pid) || projects[0] || null;
      if (proj) await selectProject(proj);
      $("#boot-state").textContent = "后端 OK · ffmpeg: " + cfg.ffmpeg.split(/[\\/]/).pop();
      document.body.dataset.vc = "ok";
    } catch (e) { return fail("后端连接失败: " + e.message); }
  }

  // ── 素材列表 ───────────────────────────────────────────
  function _mediaLi(item) {
    const li = document.createElement("li");
    li.dataset.id = item.id;
    if (state.currentItem && state.currentItem.id === item.id) li.classList.add("active");
    const kindMap = { video: "视频", audio: "音频", denoised: "降噪", vocal: "人声",
                      instrumental: "伴奏", trimmed: "去静音", bilibili: "B站", url: "网络" };
    li.innerHTML = `<div class="m-name">${esc(item.name)}</div>
      <div class="m-meta"><span class="m-badge">${kindMap[item.kind] || item.kind}</span>
      <span>${fmtDur(item.duration)}</span>
      <button class="m-del" title="删除素材">✕</button></div>`;
    li.addEventListener("click", () => waveform.selectItem(item));
    li.addEventListener("contextmenu", (e) => { e.preventDefault(); showMediaMenu(e.clientX, e.clientY, item); });
    li.querySelector(".m-del").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteMediaItem(item);
    });
    return li;
  }
  function renderMediaList() {
    const pairs = [["#media-list", "#media-empty"], ["#media-page-list", "#media-page-empty"]];
    pairs.forEach(([ulSel, emptySel]) => {
      const ul = $(ulSel), empty = $(emptySel);
      if (!ul) return;
      ul.innerHTML = "";
      if (empty) empty.classList.toggle("hidden", state.items.length > 0);
      state.items.forEach((item) => ul.appendChild(_mediaLi(item)));
    });
  }
  // 素材右键菜单：删除 / 重命名 / 添加到工作区
  function showMediaMenu(x, y, item) {
    const menu = $("#media-menu");
    menu.style.left = x + "px"; menu.style.top = y + "px";
    const btnDel = menu.querySelector(".mm-del");
    const btnRen = menu.querySelector(".mm-rename");
    const btnAdd = menu.querySelector(".mm-add");
    btnDel.onclick = () => { hideMediaMenu(); deleteMediaItem(item); };
    btnRen.onclick = async () => { hideMediaMenu(); await renameMediaItem(item); };
    btnAdd.onclick = () => {
      hideMediaMenu();
      if (state.currentItem && state.currentItem.id === item.id) toast("该素材已在当前工作区");
      else waveform.selectItem(item);
    };
    const isCurrent = !!(state.currentItem && state.currentItem.id === item.id);
    btnAdd.disabled = isCurrent;
    btnAdd.textContent = isCurrent ? "已在工作区" : "添加到工作区";
    menu.classList.remove("hidden");
  }
  function hideMediaMenu() { const m = $("#media-menu"); if (m) m.classList.add("hidden"); }
  document.addEventListener("click", hideMediaMenu);
  document.addEventListener("contextmenu", (e) => { if (!e.target.closest("#media-list li")) hideMediaMenu(); });
  async function renameMediaItem(item) {
    const name = prompt("重命名素材：", item.name);
    if (name == null || !name.trim()) return;
    try {
      await api(`/api/items/${item.id}/rename`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
    } catch (e) { toast("重命名失败: " + e.message, 6000); }
    await refreshItems();
  }

  async function refreshItems() {
    try {
      const q = state.currentProject ? `?project_id=${encodeURIComponent(state.currentProject.id)}` : "";
      state.items = await api("/api/items" + q);
      renderMediaList();
    } catch (e) { /* ignore */ }
  }

  // ---- 项目切换 / 管理 ----
  function renderProjectSelect() {
    ["#project-select", "#project-select2"].forEach((selSel) => {
      const sel = $(selSel);
      if (!sel) return;
      sel.innerHTML = "";
      state.projects.forEach(proj => {
        const opt = document.createElement("option");
        opt.value = proj.id;
        opt.textContent = proj.name;
        sel.appendChild(opt);
      });
      if (state.currentProject) sel.value = state.currentProject.id;
    });
  }

  async function selectProject(proj) {
    if (state.dirtyItems.size) await saveProjectNow();
    if (state.poolDirty) await savePoolNow();
    state.currentProject = proj;
    try { localStorage.setItem(LS_PROJECT, proj.id); } catch (e) {}
    state.currentItem = null;
    state.selection = null; state.selectionRegion = null; state.dragRegion = null;
    state.multiRegions = []; state.ctrlMarking = false;
    state.selectedSegs = new Set(); state.poolMerge = new Set();
    renderProjectSelect();
    const j = await api(`/api/projects/${proj.id}`);
    state.items = j.items || [];
    state.characters = j.characters || [];
    state.autoAnalyze = (j.auto_analyze !== false);
    renderAutoAnalyzeBtn();
    state.segmentsByItem = new Map();
    state.speakerSegsByItem = new Map();
    renderMediaList();
    await loadAllItemData();
    if (state.items.length) await waveform.selectItem(state.items[0]);
    else clearWorkbench();
    attachActiveTasks();
  }

  async function loadAllItemData() {
    const items = state.items || [];
    await Promise.all(items.map(item => loadProject(item, true)));
  }

  function clearWorkbench() {
    state.currentItem = null;
    state.subs = []; state.currentSubIdx = -1; state.auditioning = null; state.playing = false;
    if (state.ws) { try { state.ws.destroy(); } catch (e) {} state.ws = null; }
    $("#video-panel").classList.add("no-video");
    const v = $("#video-preview"); if (v) v.removeAttribute("src");
    $("#empty-state").classList.remove("hidden");
    $("#sub-current").textContent = "—";
    segments.renderSegments(); subtitles.renderSubs(); renderPool();
    waveform.updateTransport(); waveform.updateSelUI(); waveform.updatePlayUI();
  }

  async function createProject() {
    const name = prompt("新项目名称：", "新项目");
    if (name == null || !name.trim()) return;
    try {
      const j = await api("/api/projects", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
      state.projects.push(j);
      renderProjectSelect();
      await selectProject(j);
      toast("项目已创建");
    } catch (e) { toast("创建项目失败: " + e.message, 6000); }
  }
  async function renameProject() {
    if (!state.currentProject) return;
    const name = prompt("重命名项目：", state.currentProject.name);
    if (name == null || !name.trim()) return;
    try {
      await api(`/api/projects/${state.currentProject.id}/rename`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
      state.currentProject.name = name.trim();
      renderProjectSelect();
      toast("项目已重命名");
    } catch (e) { toast("重命名失败: " + e.message, 6000); }
  }
  async function deleteProject() {
    if (!state.currentProject) return;
    if (state.items && state.items.length) return toast("项目非空，请先移除素材");
    if (!confirm(`删除项目「${state.currentProject.name}」？角色池将一并删除。`)) return;
    try {
      await api(`/api/projects/${state.currentProject.id}`, { method: "DELETE" });
      state.projects = state.projects.filter(pp => pp.id !== state.currentProject.id);
      if (state.projects.length) await selectProject(state.projects[0]);
      else clearWorkbench();
      toast("项目已删除");
    } catch (e) { toast("删除失败: " + e.message, 6000); }
  }
  async function deleteMediaItem(item) {
    if (!confirm("删除素材「" + item.name + "」？")) return;
    try {
      await api(`/api/items/${item.id}`, { method: "DELETE" });
      if (state.currentItem && state.currentItem.id === item.id) {
        state.currentItem = null;
        state.selection = null; state.selectionRegion = null; state.dragRegion = null;
        state.multiRegions = []; state.ctrlMarking = false;
        state.auditionSeq = null; state.auditionIdx = 0;
        state.subs = []; state.currentSubIdx = -1; state.auditioning = null;
        state.playing = false;
        if (state.ws) { try { state.ws.destroy(); } catch (e) {} state.ws = null; }
        $("#video-panel").classList.add("no-video");
        const v = $("#video-preview"); v.removeAttribute("src");
        $("#empty-state").classList.remove("hidden");
        $("#sub-current").textContent = "—";
        waveform.updatePlayUI();
        waveform.updateSelUI();
        waveform.updateTransport();
        subtitles.renderSubs();
        segments.renderSegments();
      }
      await refreshItems();
      toast("已删除素材");
    } catch (e) { toast("删除失败: " + e.message, 6000); }
  }

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
        body: JSON.stringify({ segments: segments.segsFor(id), speaker_segments: state.speakerSegsByItem.get(id) || [] }) })));
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
  const UNDO_MAX = 50;
  const undoStack = [];
  const redoStack = [];
  function workspaceSnapshot() {
    const segs = {};
    state.segmentsByItem.forEach((list, id) => { segs[id] = JSON.parse(JSON.stringify(list || [])); });
    return { segs, characters: JSON.parse(JSON.stringify(state.characters || [])) };
  }
  function pushUndo() {
    undoStack.push(workspaceSnapshot());
    if (undoStack.length > UNDO_MAX) undoStack.shift();
    redoStack.length = 0;
  }
  function restoreSnapshot(snap) {
    const segs = new Map();
    Object.keys(snap.segs || {}).forEach(id => segs.set(id, snap.segs[id]));
    state.segmentsByItem = segs;
    state.characters = snap.characters || [];
    state.selectedSegs = new Set();
    state.poolMerge = new Set();
    state.segmentsByItem.forEach((_, id) => markDirty(id));
    state.poolDirty = true;
    poolVer++;
    segments.renderSegments();
    renderPool();
    saveProjectNow();
    savePoolNow();
  }
  function undo() {
    if (!undoStack.length) return toast("没有可撤销的操作");
    redoStack.push(workspaceSnapshot());
    restoreSnapshot(undoStack.pop());
    toast("已撤销");
  }
  function redo() {
    if (!redoStack.length) return toast("没有可重做的操作");
    undoStack.push(workspaceSnapshot());
    restoreSnapshot(redoStack.pop());
    toast("已重做");
  }

  // ── 角色池子页面 ──
  function openPool() {
    if (!needItem()) return;
    state.poolOpen = true;
    $("#pool-view").classList.remove("hidden");
    renderPool();
  }
  function closePool() {
    state.poolOpen = false;
    $("#pool-view").classList.add("hidden");
  }

  function reassignSegments(segIds, characterId) {
    pushUndo();
    let n = 0;
    (state.items || []).forEach(item => {
      const segs = segments.segsFor(item.id);
      let changed = false;
      segs.forEach(s => { if (segIds.includes(s.id)) { s.characterId = characterId || null; n++; changed = true; } });
      if (changed) scheduleSaveProject(item.id);
    });
    segments.renderSegments(); renderPool();
    toast(`已重定向 ${n} 段片段`);
  }

  function poolCard(ch) {
    const isU = !ch;
    const mine = segments.charSegs(ch ? ch.id : null);
    const el = document.createElement("div");
    el.className = "pool-card" + (isU ? " unassigned" : "");
    el.dataset.poolChar = ch ? ch.id : "";
    el.style.setProperty("--pc", ch ? ch.color : "var(--muted)");
    el.innerHTML = `
      <div class="pool-head">
        ${ch ? `<input type="checkbox" class="pool-merge-cb" data-char="${ch.id}" title="勾选后可「合并选中角色」">` : "<span class='pool-nb'></span>"}
        ${ch ? `<input type="color" class="pool-color" data-char="${ch.id}" value="${esc(ch.color)}" title="修改颜色">` : ""}
        <span class="pool-name">${isU ? "未分配" : esc(ch.name)}</span>
        <span class="pool-count">${mine.length} 段</span>
      </div>
      ${ch && (ch.speakerLabels || []).length ? `<div class="pool-labels">自动标签: ${ch.speakerLabels.map(esc).join("、")}</div>` : ""}
      <div class="pool-actions">
        ${isU ? "" : `<button class="chip pool-aud" data-char="${ch.id}">试听</button>
          <button class="chip pool-rename" data-char="${ch.id}">重命名</button>
          <button class="chip pool-del danger" data-char="${ch.id}">删除</button>`}
      </div>
      <details class="pool-segs">
        <summary>片段（${mine.length}）</summary>
        <div class="pool-seg-list">
          ${mine.slice(0, 300).map(({ seg, item }) => `
            <div class="pool-seg" draggable="true" data-seg="${seg.id}" data-item="${item.id}">
              <span class="ps-time">${fmtT(seg.start)}~${fmtT(seg.end)}</span>
              <span class="ps-src" title="${esc(item.name)}">${esc(shortName(item.name, 6))}</span>
              <span class="ps-text">${esc(seg.text.slice(0, 24) || "（空）")}</span>
              <button class="chip ps-play" data-seg="${seg.id}" data-item="${item.id}" title="试听">▶</button>
            </div>`).join("")}
          ${mine.length > 300 ? `<div class="muted">… 其余 ${mine.length - 300} 段未列出</div>` : ""}
        </div>
      </details>`;
    return el;
  }

  function renderPool() {
    const grid = $("#pool-grid");
    if (!grid) return;
    grid.innerHTML = "";
    $("#pool-item-name").textContent = state.currentProject ? state.currentProject.name : "";
    $("#pool-unassigned-count").textContent = segments.charSegs(null).length;
    grid.appendChild(poolCard(null));
    state.characters.forEach(ch => grid.appendChild(poolCard(ch)));
    $("#pool-merge-count").textContent = (state.poolMerge || new Set()).size;
  }

  function poolAudition(cid) {
    const rows = segments.charSegs(cid);
    if (!rows.length) return toast("该角色暂无片段");
    segments.gotoEditAndPlay(rows[0].item, rows[0].seg);
  }
  function poolPlaySeg(segId, itemId) {
    const item = state.items.find(x => x.id === itemId);
    const seg = item ? segments.segsFor(item.id).find(s => s.id === segId) : null;
    if (item && seg) segments.gotoEditAndPlay(item, seg);
  }
  function poolRename(cid) {
    const ch = charById(cid); if (!ch) return;
    const name = prompt("角色名称：", ch.name);
    if (name == null || !name.trim()) return;
    pushUndo();
    ch.name = name.trim();
    scheduleSavePool(); renderPool(); segments.renderSegments();
  }
  function poolDelete(cid) {
    const ch = charById(cid); if (!ch) return;
    const n = segments.charSegs(cid).length;
    if (!confirm(`删除角色「${ch.name}」？其 ${n} 段片段将变为未分配`)) return;
    pushUndo();
    state.characters = state.characters.filter(c => c.id !== cid);
    (state.items || []).forEach(item => {
      let changed = false;
      segments.segsFor(item.id).forEach(s => { if (s.characterId === cid) { s.characterId = null; changed = true; } });
      if (changed) scheduleSaveProject(item.id);
    });
    scheduleSavePool(); renderPool(); segments.renderSegments();
  }
  function poolSetColor(cid, color) {
    const ch = charById(cid); if (!ch) return;
    pushUndo();
    ch.color = color;
    scheduleSavePool(); renderPool(); segments.renderSegments();
  }
  function togglePoolMerge(cid, on) {
    state.poolMerge = state.poolMerge || new Set();
    if (on) state.poolMerge.add(cid); else state.poolMerge.delete(cid);
    $("#pool-merge-count").textContent = state.poolMerge.size;
  }
  function mergePoolSelected() {
    const ids = Array.from(state.poolMerge || []);
    const chs = ids.map(charById).filter(Boolean);
    if (chs.length < 2) return toast("请至少勾选 2 个角色");
    const name = prompt("合并后角色名：", chs.map(c => c.name).join("+"));
    if (name == null || !name.trim()) return;
    const target = chs[0];
    pushUndo();
    target.name = name.trim();
    target.speakerLabels = Array.from(new Set(chs.flatMap(c => c.speakerLabels || [])));
    state.characters = state.characters.filter(c => !ids.includes(c.id) || c.id === target.id);
    (state.items || []).forEach(item => {
      let changed = false;
      segments.segsFor(item.id).forEach(s => { if (ids.includes(s.characterId) && s.characterId !== target.id) { s.characterId = target.id; changed = true; } });
      if (changed) scheduleSaveProject(item.id);
    });
    state.poolMerge = new Set();
    scheduleSavePool(); renderPool(); segments.renderSegments();
    toast(`已合并为「${target.name}」`);
  }
  function createPoolCharacter() {
    const name = prompt("新角色名称：", "新角色");
    if (name == null || !name.trim()) return;
    pushUndo();
    state.characters.push({ id: uid("char"), name: name.trim(), color: paletteNext(), speakerLabels: [], created: Date.now() });
    scheduleSavePool(); renderPool(); segments.renderSegments();
  }

  async function doIdentifySpeakers() {
    if (!state.currentProject) return toast("请先选择项目");
    toast("开始项目级说话人识别（ECAPA 声纹，将把项目内全部素材联合聚类，首次含模型加载）…");
    try {
      const j = await api(`/api/projects/${state.currentProject.id}/speakers/generate`, { method: "POST" });
      trackTask(j.task_id, async (result) => {
        if (Array.isArray(result.characters)) state.characters = result.characters;
        await loadAllItemData();
        renderPool(); segments.renderSegments(); subtitles.renderSubs();
        const created = (result.created || []).length;
        const merged = (result.merged || 0);
        const mixed = result.mixed || 0;
        const cleaned = result.cleaned || 0;
        const itemN = (result.items || []).length;
        let msg = `项目说话人识别完成：${result.n_speakers} 人（${result.quality === "ecapa" ? "ECAPA" : "MFCC 降级"}），跨 ${itemN} 个素材 ${result.labeled}/${result.total} 段已标记，其中 ${mixed} 段为多人混合(未绑定)；新增 ${created} 角色，跨素材归并 ${merged} 段`;
        if (cleaned > 0) msg += `；已清理 ${cleaned} 个旧版本残留角色`;
        toast(msg);
      });
    } catch (e) { toast("项目说话人识别启动失败: " + e.message, 6000); }
  }

  function renderAutoAnalyzeBtn() {
    const on = !!state.autoAnalyze;
    ["#btn-auto-analyze", "#btn-auto-analyze2"].forEach((sel) => {
      const b = $(sel);
      if (b) {
        b.classList.toggle("btn-auto-on", on);
        b.textContent = on ? "自动分析·开" : "自动分析·关";
      }
    });
  }
  async function toggleAutoAnalyze() {
    if (!state.currentProject) return toast("请先选择项目");
    const on = !state.autoAnalyze;
    try {
      await api(`/api/projects/${state.currentProject.id}/settings`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto_analyze: on }) });
      state.autoAnalyze = on;
      renderAutoAnalyzeBtn();
      toast(on ? "已开启：导入后自动生成字幕 + 识别说话人" : "已关闭后台自动分析", 4000);
    } catch (e) { toast("设置保存失败: " + e.message, 6000); }
  }

  // 角色池页面事件
  $("#pool-grid").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const cid = btn.dataset.char, segId = btn.dataset.seg, itemId = btn.dataset.item;
    if (btn.classList.contains("pool-aud")) poolAudition(cid);
    else if (btn.classList.contains("pool-rename")) poolRename(cid);
    else if (btn.classList.contains("pool-del")) poolDelete(cid);
    else if (btn.classList.contains("ps-play")) poolPlaySeg(segId, itemId);
  });
  $("#pool-grid").addEventListener("dblclick", (e) => {
    const nm = e.target.closest(".pool-name");
    if (nm) poolRename(nm.closest(".pool-card").dataset.poolChar);
  });
  $("#pool-grid").addEventListener("change", (e) => {
    if (e.target.classList.contains("pool-color")) poolSetColor(e.target.dataset.char, e.target.value);
    else if (e.target.classList.contains("pool-merge-cb")) togglePoolMerge(e.target.dataset.char, e.target.checked);
  });
  let dragSegId = null, dragSegItem = null;
  $("#pool-grid").addEventListener("dragstart", (e) => {
    const seg = e.target.closest(".pool-seg");
    if (!seg) return;
    dragSegId = seg.dataset.seg;
    dragSegItem = seg.dataset.item;
    e.dataTransfer.setData("text/plain", dragSegId);
  });
  $("#pool-grid").addEventListener("dragover", (e) => { if (e.target.closest(".pool-card")) e.preventDefault(); });
  $("#pool-grid").addEventListener("drop", (e) => {
    const card = e.target.closest(".pool-card");
    if (!card) return;
    e.preventDefault();
    const sid = dragSegId || e.dataTransfer.getData("text/plain");
    const item = state.items.find(x => x.id === dragSegItem);
    const seg = item ? segments.segsFor(item.id).find(s => s.id === sid) : null;
    if (seg) { seg.characterId = card.dataset.poolChar || null; scheduleSaveProject(item.id); segments.renderSegments(); renderPool(); }
  });

  let focusedSeg = null;
  function setSegFocus(tr) {
    $$(".seg-row").forEach(r => r.style.outline = "");
    if (tr) { tr.style.outline = "1px solid var(--accent)"; focusedSeg = { item: tr.dataset.item, i: Number(tr.dataset.i) }; }
    else focusedSeg = null;
  }

  // 片段列表：点击/多选（Shift 区间、Ctrl 追加），右键重定向角色
  $("#seg-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    const tr = e.target.closest("tr.seg-row");
    if (!tr) return;
    const itemId = tr.dataset.item;
    const i = Number(tr.dataset.i);
    const segs = segments.segsFor(itemId);
    const seg = segs[i];
    if (!seg) return;
    if (btn) {
      if (btn.classList.contains("seg-aud")) { segments.auditionSegment(state.items.find(x => x.id === itemId), seg); return; }
      else if (btn.classList.contains("seg-jump")) { segments.jumpToSegment(state.items.find(x => x.id === itemId), seg); return; }
      else if (btn.classList.contains("seg-del")) { segments.deleteSegment(itemId, i); return; }
    }
    e.preventDefault();
    const rows = segments.allSegs();
    const flatIdx = rows.findIndex(r => r.seg.id === seg.id);
    if (e.shiftKey && state.selectedSegs.size) {
      let first = 1e9;
      rows.forEach((r, k) => { if (state.selectedSegs.has(r.seg.id)) first = Math.min(first, k); });
      const lo = Math.min(first, flatIdx), hi = Math.max(first, flatIdx);
      state.selectedSegs = new Set();
      for (let k = lo; k <= hi; k++) state.selectedSegs.add(rows[k].seg.id);
    } else if (e.ctrlKey || e.metaKey) {
      if (state.selectedSegs.has(seg.id)) state.selectedSegs.delete(seg.id);
      else state.selectedSegs.add(seg.id);
    } else {
      state.selectedSegs = new Set([seg.id]);
    }
    setSegFocus(tr);
    segments.renderSegments();
  });
  $("#seg-tbody").addEventListener("input", (e) => {
    const tr = e.target.closest("tr.seg-row");
    if (!tr) return;
    const itemId = tr.dataset.item;
    const i = Number(e.target.dataset.i);
    const segs = segments.segsFor(itemId);
    if (!segs[i]) return;
    if (e.target.classList.contains("seg-text")) {
      if (!e.target.dataset.undoed) { e.target.dataset.undoed = "1"; pushUndo(); }
      segs[i].text = e.target.value; scheduleSaveProject(itemId);
    }
    segments.updateSegBadge(itemId, i);
  });
  $("#seg-tbody").addEventListener("change", (e) => {
    const tr = e.target.closest("tr.seg-row");
    if (!tr) return;
    const itemId = tr.dataset.item;
    const i = Number(e.target.dataset.i);
    const segs = segments.segsFor(itemId);
    if (!segs[i]) return;
    delete e.target.dataset.undoed;
    if (!e.target.classList.contains("seg-text")) pushUndo();
    if (e.target.classList.contains("seg-lang")) { segs[i].language = e.target.value; scheduleSaveProject(itemId); }
    if (e.target.classList.contains("seg-speaker")) {
      segs[i].characterId = e.target.value || null;
      scheduleSaveProject(itemId); segments.renderSegments();
    }
  });
  function showRedirectMenu(x, y, segIds) {
    const menu = $("#redirect-menu");
    menu.innerHTML = "";
    menu.style.left = x + "px"; menu.style.top = y + "px";
    const mk = (label, val, color) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (color) b.style.color = color;
      b.addEventListener("click", () => { hideRedirectMenu(); reassignSegments(segIds, val); });
      return b;
    };
    menu.appendChild(mk("未分配", "", ""));
    state.characters.forEach(c => menu.appendChild(mk(c.name, c.id, c.color)));
    menu.classList.remove("hidden");
  }
  function hideRedirectMenu() { const m = $("#redirect-menu"); if (m) m.classList.add("hidden"); }
  document.addEventListener("click", hideRedirectMenu);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideRedirectMenu(); });
  $("#segments-panel").addEventListener("contextmenu", (e) => {
    const tr = e.target.closest("tr.seg-row");
    if (!tr) return;
    e.preventDefault();
    const itemId = tr.dataset.item;
    const i = Number(tr.dataset.i);
    const segs = segments.segsFor(itemId);
    let ids = Array.from(state.selectedSegs).filter(id => segments.allSegs().some(r => r.seg.id === id));
    if (!ids.length && segs[i]) ids = [segs[i].id];
    showRedirectMenu(e.clientX, e.clientY, ids);
  });

  // ── 任务跟踪 ───────────────────────────────────────────
  function trackTask(taskId, doneCb) {
    const existing = state.activeTasks.get(taskId);
    if (existing) { existing.doneCb = doneCb; return; }  // 同一后台任务去重
    state.activeTasks.set(taskId, { msg: "排队中", progress: 0, doneCb });
    ensurePolling();
    updateStatusbar();
  }

  // 后台自动分析完成：刷新角色池 / 素材 / 片段 / 字幕
  async function autoAnalyzeDone(result) {
    if (!result) return;
    if (Array.isArray(result.characters)) state.characters = result.characters;
    try {
      if (state.currentProject) {
        const j = await api(`/api/projects/${state.currentProject.id}`);
        if (Array.isArray(j.characters)) state.characters = j.characters;
        state.items = j.items || [];
        renderMediaList();
      }
    } catch (e) { /* 网络抖动忽略，用任务结果兜底 */ }
    await loadAllItemData();
    renderPool(); segments.renderSegments(); subtitles.renderSubs();
    const created = (result.created || []).length, merged = (result.merged || 0);
    const mixed = result.mixed || 0, cleaned = result.cleaned || 0;
    const itemN = (result.items || []).length;
    let msg = `后台分析完成：${result.n_speakers} 人（${result.quality === "ecapa" ? "ECAPA" : "MFCC 降级"}），跨 ${itemN} 个素材 ${result.labeled}/${result.total} 段已标记，其中 ${mixed} 段为多人混合(未绑定)；新增 ${created} 角色，跨素材归并 ${merged} 段`;
    if (cleaned > 0) msg += `；已清理 ${cleaned} 个旧版本残留角色`;
    toast(msg, 6000);
    return true;  // 已显示专属完成提示，抑制通用“任务完成”
  }

  // 刷新页面后重新挂接仍在后台运行的任务（含导入后自动分析）
  async function attachActiveTasks() {
    try {
      const tasks = await api("/api/tasks/active");
      tasks.forEach((t) => {
        if (state.activeTasks.has(t.id)) return;
        let cb = null;
        if (t.kind === "speakers") cb = autoAnalyzeDone;
        else if (t.kind === "import") cb = (r) => { if (r) refreshItems(); };
        if (cb) trackTask(t.id, cb);
      });
    } catch (e) { /* 忽略 */ }
  }
  function ensurePolling() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(pollTasks, 800);
  }
  async function pollTasks() {
    if (!state.activeTasks.size) {
      clearInterval(state.pollTimer); state.pollTimer = null; updateStatusbar(); return;
    }
    for (const [tid, info] of state.activeTasks) {
      try {
        const t = await api(`/api/tasks/${tid}`);
        info.msg = t.message || info.msg;
        info.progress = t.progress || 0;
        if (t.status === "done") {
          state.activeTasks.delete(tid);
          await refreshItems();
          let customToast = false;
          if (info.doneCb) customToast = !!(await info.doneCb(t.result));
          if (!customToast) toast("任务完成");
        } else if (t.status === "error") {
          state.activeTasks.delete(tid);
          toast("任务失败: " + (t.message || "未知错误"), 6000);
        } else if (t.status === "cancelled") {
          state.activeTasks.delete(tid);
          toast("任务已取消");
        }
      } catch (e) { /* 网络抖动忽略 */ }
    }
    updateStatusbar();
  }
  function updateStatusbar() {
    const wrap = $("#task-bar-wrap"), bar = $("#task-bar"), info = $("#task-info");
    const cancelBtn = $("#btn-cancel-task");
    if (!state.activeTasks.size) {
      wrap.classList.add("hidden");
      if (cancelBtn) cancelBtn.classList.add("hidden");
      if (!toastTimer) info.textContent = "就绪";
      return;
    }
    wrap.classList.remove("hidden");
    if (cancelBtn) cancelBtn.classList.remove("hidden");
    const arr = Array.from(state.activeTasks.values());
    const avg = arr.reduce((a, b) => a + b.progress, 0) / arr.length;
    bar.style.width = (avg * 100).toFixed(0) + "%";
    info.textContent = arr.map(a => a.msg).join(" · ");
  }
  function cancelAllTasks() {
    Array.from(state.activeTasks.keys()).forEach((tid) => {
      api(`/api/tasks/${tid}/cancel`, { method: "POST" }).catch(() => {});
    });
  }
  function selectResultItem(result) {
    if (!result) return;
    const id = result.item ? result.item.id : (result.item_ids && result.item_ids[0]);
    if (!id) return;
    let item = state.items.find(x => x.id === id);
    if (!item && result.item) {
      item = result.item;
      state.items.push(item);
      renderMediaList();
    }
    if (item) waveform.selectItem(item);
  }

  // ── 通用守卫 ───────────────────────────────────────────
  function needItem() { if (!state.currentItem) { toast("请先选择素材"); return false; } return true; }

  // ── 弹窗 ───────────────────────────────────────────────
  function showModal(sel) { $(sel).classList.remove("hidden"); }
  function hideModal(sel) { $(sel).classList.add("hidden"); }
  function showResult(title, text, html, width) {
    $("#result-title").textContent = title;
    const body = $("#result-body");
    body.textContent = text;
    if (html) {
      const div = document.createElement("div");
      div.className = "modal-actions";
      div.style.justifyContent = "flex-start";
      div.innerHTML = html;
      body.insertAdjacentElement("afterend", div);
      body.dataset.extra = "1";
    }
    showModal("#modal-result");
  }

  // 菜单
  function setupMenus() {
    $$(".menu-title").forEach((bt) => {
      bt.addEventListener("click", (e) => {
        e.stopPropagation();
        const m = bt.parentElement;
        const was = m.classList.contains("open");
        $$(".menu").forEach(x => x.classList.remove("open"));
        if (!was) m.classList.add("open");
      });
    });
    document.addEventListener("click", () => $$(".menu").forEach(x => x.classList.remove("open")));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") $$(".menu").forEach(x => x.classList.remove("open")); });

    const actions = {
      "import": () => io.importDialog(),
      "bilibili": () => showModal("#modal-bilibili"),
      "url-open": () => showModal("#modal-bilibili"),
      "project-new": createProject,
      "project-rename": renameProject,
      "project-delete": deleteProject,
      "pool": openPool,
      "identify-speakers": doIdentifySpeakers,
      "export-selection": () => io.openExportModal(),
      "denoise": () => io.doDenoise(),
      "separate": () => io.doSeparate(),
      "trim": () => io.doTrim(),
      "transcribe": () => io.openTranscribeModal(),
      "dataset-export": () => io.openDatasetModal(),
      "autosplit": () => showModal("#modal-autosplit"),
      "undo": undo,
      "redo": redo,
      "validate": segments.renderSegments,
      "zoom-in": () => waveform.zoomIn(),
      "zoom-out": () => waveform.zoomOut(),
      "fit": () => waveform.zoomSet(0),
      "zoom-sel": () => { if (state.selection) { waveform.zoomSet(0); state.ws.setTime(state.selection.start); } },
      "toggle-minimap": () => $("#minimap-wrap").classList.toggle("hidden"),
      "panel-toggle": (b) => togglePanel(b.dataset.panel),
      "layout-reset": resetLayout,
      "help": () => showModal("#modal-help"),
    };
    document.querySelectorAll("[data-act]").forEach((b) => {
      b.addEventListener("click", () => { const f = actions[b.dataset.act]; if (f) f(b); });
    });
  }

  // ── 快捷键 ─────────────────────────────────────────────
  function setupShortcuts() {
    window.addEventListener("keydown", (e) => {
      // Ctrl/Cmd + +/-/0: 屏蔽浏览器页面缩放，改为时间轴缩放
      if (e.ctrlKey || e.metaKey) {
        const k = e.key;
        if (k === "+" || k === "=" || k === "Add" || k === "NumpadAdd") { e.preventDefault(); waveform.zoomIn(); return; }
        if (k === "-" || k === "Subtract" || k === "NumpadSubtract") { e.preventDefault(); waveform.zoomOut(); return; }
        if (k === "0") { e.preventDefault(); waveform.zoomSet(0); return; }
      }
      const tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (e.ctrlKey && (e.key === "o" || e.key === "O")) { e.preventDefault(); io.importDialog(); return; }
      if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        if (e.shiftKey) redo(); else undo();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === "y" || e.key === "Y")) { e.preventDefault(); redo(); return; }

      // 小键盘快进：−/+快退/快进 15 秒；数字区方向键(2/4/6/8)等效主方向键（兼容 NumLock 开关）
      const code = e.code || "";
      if (code === "NumpadSubtract") { e.preventDefault(); waveform.seekBy(-SEEK_FAST); return; }
      if (code === "NumpadAdd") { e.preventDefault(); waveform.seekBy(SEEK_FAST); return; }
      if (code === "Numpad4" || code === "Numpad6") { e.preventDefault(); waveform.seekBy(code === "Numpad6" ? SEEK_STEP : -SEEK_STEP); return; }
      if (code === "Numpad8" || code === "Numpad2") { e.preventDefault(); waveform.adjVolume(code === "Numpad8" ? VOL_STEP : -VOL_STEP); return; }

      switch (e.key) {
        case " ": e.preventDefault(); waveform.togglePlay(); break;
        case "l": case "L": waveform.toggleLoop(); break;
        case "e": case "E": io.openExportModal(); break;
        case "n": case "N": io.doDenoise(); break;
        case "v": case "V": io.doSeparate(); break;
        case "ArrowLeft": case "ArrowRight": {
          e.preventDefault();
          const d = e.key === "ArrowRight" ? SEEK_STEP : -SEEK_STEP;
          if (e.ctrlKey) { (e.key === "ArrowRight" ? waveform.markForward() : waveform.unmarkLast()); } // Ctrl+→ 快进多选 / Ctrl+← 撤销上一段
          else if (e.shiftKey) waveform.nudgeSelection(d, "end"); // Shift+←→ 微调选区终点边界
          else waveform.seekBy(d);                                // ←→ 快退 / 快进 5 秒
          break;
        }
        case "ArrowUp": case "ArrowDown": e.preventDefault(); waveform.adjVolume(e.key === "ArrowUp" ? VOL_STEP : -VOL_STEP); break;
        case "Delete":
          if (state.selectedSegs.size) {
            pushUndo();
            (state.items || []).forEach(item => {
              const segs = segments.segsFor(item.id);
              const before = segs.length;
              const kept = segs.filter(s => !state.selectedSegs.has(s.id));
              if (kept.length !== before) { segs.splice(0, segs.length, ...kept); scheduleSaveProject(item.id); }
            });
            state.selectedSegs = new Set();
            segments.renderSegments();
          } else if (focusedSeg) {
            segments.deleteSegment(focusedSeg.item, focusedSeg.i);
            setSegFocus(null);
          }
          break;
      }
    });
  }

  // ── 拖拽导入 ───────────────────────────────────────────
  function setupDrop() {
    let dragCount = 0;
    window.addEventListener("dragenter", (e) => { e.preventDefault(); dragCount++; });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("dragleave", (e) => { e.preventDefault(); dragCount = Math.max(0, dragCount - 1); });
    window.addEventListener("drop", (e) => {
      e.preventDefault(); dragCount = 0;
      const files = Array.from(e.dataTransfer.files || []);
      files.forEach((f) => io.uploadFile(f));
    });
    $("#file-input").addEventListener("change", () => {
      Array.from($("#file-input").files).forEach((f) => io.uploadFile(f));
      $("#file-input").value = "";
    });
  }

  // ── 事件绑定 ───────────────────────────────────────────
  function bindUI() {
    ["#project-select", "#project-select2"].forEach((psSel) => {
      const ps = $(psSel);
      if (ps) ps.addEventListener("change", () => {
        const proj = state.projects.find(pp => pp.id === ps.value);
        if (proj && state.currentProject && proj.id !== state.currentProject.id) selectProject(proj);
      });
    });
    $("#btn-cancel-task").addEventListener("click", cancelAllTasks);
    $("#btn-import").addEventListener("click", () => io.importDialog());
    $("#btn-bilibili").addEventListener("click", () => showModal("#modal-bilibili"));
    $("#btn-export-dataset").addEventListener("click", () => io.openDatasetModal());
    $("#btn-play2").addEventListener("click", () => waveform.togglePlay());
    $("#btn-prev").addEventListener("click", () => state.ws && state.ws.setTime(0));
    $("#btn-next").addEventListener("click", () => state.ws && state.ws.setTime(state.currentItem ? state.currentItem.duration : 0));
    $("#btn-loop").addEventListener("click", () => waveform.toggleLoop());
    $("#btn-play-selection").addEventListener("click", () => waveform.playSelection());
    $("#btn-export-selection").addEventListener("click", () => io.openExportModal());
    $("#minimap-toggle").addEventListener("change", (e) => $("#minimap-wrap").classList.toggle("hidden", !e.target.checked));
    $("#btn-add-seg").addEventListener("click", segments.addSegmentFromSelection);
    $("#btn-clear-segs").addEventListener("click", () => {
      if (!state.currentItem) return;
      if (confirm("清空当前素材的全部片段？")) {
        pushUndo();
        segments.segsFor(state.currentItem.id).length = 0;
        state.selectedSegs = new Set();
        scheduleSaveProject(); segments.renderSegments();
      }
    });
    $("#btn-transcribe").addEventListener("click", () => io.openTranscribeModal());
    $("#btn-pool").addEventListener("click", openPool);
    $("#btn-identify-speakers").addEventListener("click", doIdentifySpeakers);
    $("#btn-auto-analyze").addEventListener("click", toggleAutoAnalyze);
    $("#btn-auto-analyze2").addEventListener("click", toggleAutoAnalyze);
    $("#pool-back").addEventListener("click", closePool);
    $("#pool-close").addEventListener("click", closePool);
    $("#pool-new").addEventListener("click", createPoolCharacter);
    $("#pool-merge").addEventListener("click", mergePoolSelected);
    $("#pool-identify").addEventListener("click", doIdentifySpeakers);

    $("#bb-open").addEventListener("click", () => io.doUrlOpen());
    $("#tr-start").addEventListener("click", () => io.doTranscribe());
    $("#ds-start").addEventListener("click", () => io.doDatasetExport());
    $("#as-start").addEventListener("click", () => io.doAutosplit());
    $("#ex-start").addEventListener("click", () => io.doExportSelection());
    // 视频 ↔ 音频双向联动 / Ctrl+滚轮时间轴缩放（波形模块内绑定）
    waveform.bindVideoPreview();
    waveform.bindZoomWheel();

    $$(".modal-mask").forEach((mask) => mask.addEventListener("click", (e) => {
      if (e.target === mask || e.target.closest("[data-close]")) mask.classList.add("hidden");
    }));
    $("#bb-url").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); io.doUrlOpen(); } });
    // 实时字幕
    $("#btn-sub-open").addEventListener("click", () => $("#sub-file").click());
    $("#btn-sub-generate").addEventListener("click", subtitles.generateSubs);
    $("#btn-sub-add").addEventListener("click", subtitles.addCurrentSubToSegments);
    $("#sub-file").addEventListener("change", () => {
      const f = $("#sub-file").files[0];
      if (f) subtitles.uploadSubFile(f);
      $("#sub-file").value = "";
    });
    $("#sub-tbody").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      const tr = e.target.closest("tr.sub-row");
      if (!tr) return;
      const i = Number(tr.dataset.i);
      if (btn && btn.classList.contains("sub-sel")) subtitles.selectSubRange(i);
      else if (btn && btn.classList.contains("sub-add")) subtitles.addSubToSegments(i);
      else subtitles.selectSubRange(i);
    });
    // 波形区域内右键取消选区（波形模块内绑定）
    waveform.bindWaveBox();
  }


  // ── 页面切换（素材库 / 剪辑 / 训练交付） ──
  let training = null;   // 训练交付页模块实例（启动区由 createTraining 创建）
  let subtitles = null;  // 实时字幕模块实例（启动区由 createSubtitles 创建）
  let io = null;         // 输入/输出模块实例（启动区由 createIo 创建）
  let waveform = null;   // 波形/播放模块实例（启动区由 createWaveform 创建）
  let segments = null;   // 片段列表模块实例（启动区由 createSegments 创建）
  const PAGE_KEY = "vc.page.v1";
  let currentPage = "edit";
  try { const saved = localStorage.getItem(PAGE_KEY); if (["edit", "media", "train"].includes(saved)) currentPage = saved; } catch (e) {}

  function setPage(name) {
    currentPage = name;
    try { localStorage.setItem(PAGE_KEY, name); } catch (e) {}
    $$("#page-edit, #page-media, #page-train").forEach(pp => pp.classList.add("hidden"));
    const el = document.getElementById("page-" + name);
    if (el) el.classList.remove("hidden");
    $$("#pagebar .page-btn").forEach(b => b.classList.toggle("active", b.dataset.page === name));
    if (name === "media") { renderMediaList(); renderProjectSelect(); }
    if (name === "train" && training) training.loadTraining(true);
  }
  function setupPagebar() {
    $$("#pagebar .page-btn").forEach(b => b.addEventListener("click", () => setPage(b.dataset.page)));
    const b2 = $("#btn-import2"); if (b2) b2.addEventListener("click", () => io.importDialog());
    const u2 = $("#btn-url2"); if (u2) u2.addEventListener("click", () => showModal("#modal-bilibili"));
    const p2 = $("#btn-pool2"); if (p2) p2.addEventListener("click", openPool);
    const rf = $("#btn-train-refresh"); if (rf) rf.addEventListener("click", () => training && training.loadTraining(true));
    setPage(currentPage);
  }

  // ── 启动 ───────────────────────────────────────────────
  segments = createSegments({ $, esc, shortName, fmtT, fmtDur, fmtSel, SEG_MIN, SEG_MAX,
    state, toast, charById, newSegment, pushUndo, scheduleSaveProject, closePool, setPage,
    selectItem: (item) => waveform.selectItem(item) });
  subtitles = createSubtitles({ $, $$, fmtT, esc, api, toast, state, trackTask,
    speakerLabelAt, segsFor: segments.segsFor, newSegment, pushUndo, scheduleSaveProject,
    renderSegments: segments.renderSegments });
  training = createTraining({ $, esc, shortName, fmtDur, api, state, toast, trackTask });
  io = createIo({ $, api, state, toast, trackTask, needItem, selectResultItem, autoAnalyzeDone,
    showModal, hideModal, showResult, saveProjectNow, pushUndo, loadProject,
    renderSegments: segments.renderSegments, segsFor: segments.segsFor, charById, scheduleSaveProject,
    fmtSel, fmtT });
  waveform = createWaveform({ $, api, fmtT, fmtDur, fmtSel, clampN, SEEK_STEP, toast, state,
    WaveSurfer, Timeline, Regions, Minimap,
    renderMediaList, loadProject, saveProjectNow, savePoolNow, renderSegments: segments.renderSegments,
    subtitles });
  setupMenus();
  setupShortcuts();
  setupDrop();
  bindUI();
  initWorkspace();
  waveform.setupMMSeek();
  setupPagebar();
  setInterval(() => {
    if (currentPage === "train" && !document.getElementById("page-train").classList.contains("hidden")) {
      if (training) training.loadTraining(false);
    }
  }, 8000);
  function beaconSave() {
    try {
      Array.from(state.dirtyItems).forEach(id => {
        const payload = JSON.stringify({ segments: segments.segsFor(id), speaker_segments: state.speakerSegsByItem.get(id) || [] });
        navigator.sendBeacon(`/api/items/${id}/project`, new Blob([payload], { type: "application/json" }));
      });
      if (state.poolDirty && state.currentProject) {
        const payload = JSON.stringify({ characters: state.characters });
        navigator.sendBeacon(`/api/projects/${state.currentProject.id}/characters`, new Blob([payload], { type: "application/json" }));
      }
    } catch (e) { /* 尽力而为 */ }
  }
  window.addEventListener("beforeunload", () => {
    if (state.dirtyItems.size || state.poolDirty) beaconSave();
  });
  renderAutoAnalyzeBtn();
  boot();

  // 调试/自动化钩子
  window.__vc = { state, selectItem: waveform.selectItem, selectProject,
    renderSegments: segments.renderSegments,
    WaveSurfer, Timeline, Regions, Minimap,
    markForward: waveform.markForward, unmarkLast: waveform.unmarkLast,
    clearMultiRegions: waveform.clearMultiRegions,
    loadProject, saveProjectNow, savePoolNow, openPool, closePool, renderPool,
    undo, redo, pushUndo, doAutosplit: io.doAutosplit, uploadFile: io.uploadFile,
    createProject, renameProject, deleteProject, doIdentifySpeakers, newSegment,
    toggleAutoAnalyze, autoAnalyzeDone, attachActiveTasks,
    setPage, loadTraining: training.loadTraining, startTrain: training.startTrain,
    doInfer: training.doInfer, train: training.train,
    workspace: { layout, applyLayout, saveLayout, resetLayout, togglePanel, swapPanels, PANELS } };
})();
