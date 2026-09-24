import WaveSurfer from "/static/vendor/wavesurfer.esm.js";
import Timeline from "/static/vendor/plugins/timeline.esm.js";
import Regions from "/static/vendor/plugins/regions.esm.js";
import Minimap from "/static/vendor/plugins/minimap.esm.js";

import { $, $$, esc, shortName, fmtT, fmtSel, fmtDur, api, clampN,
         SEG_MIN, SEG_MAX, SEEK_STEP, SEEK_FAST, VOL_STEP, CHAR_PALETTE } from "/static/js/util.js";
import { state, toast, layout, applyLayout, saveLayout, resetLayout,
         togglePanel, swapPanels, initWorkspace, PANELS } from "/static/js/state.js";
import { createTraining } from "/static/js/modules/training.js";

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
    li.addEventListener("click", () => selectItem(item));
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
      else selectItem(item);
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
    if (state.items.length) await selectItem(state.items[0]);
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
    renderSegments(); renderSubs(); renderPool(); updateTransport(); updateSelUI(); updatePlayUI();
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
        updatePlayUI();
        updateSelUI();
        updateTransport();
        renderSubs();
        renderSegments();
      }
      await refreshItems();
      toast("已删除素材");
    } catch (e) { toast("删除失败: " + e.message, 6000); }
  }

  // ── 选择素材 / 波形加载 ────────────────────────────────
  async function selectItem(item) {
    if ((state.dirtyItems.size || state.poolDirty) && state.currentItem && state.currentItem.id !== item.id) {
      await saveProjectNow();   // 切换素材前先浮存旧素材项目
      await savePoolNow();
    }
    state.currentItem = item;
    state.speakerSegs = state.speakerSegsByItem.get(item.id) || [];
    renderMediaList();

    // 视频预览
    const vp = $("#video-panel"), v = $("#video-preview");
    if (item.video_url) {
      vp.classList.remove("no-video");
      if (v.src !== location.origin + item.video_url) v.src = item.video_url;
      videoSeekByAudio = false;
      v.load();
    } else {
      vp.classList.add("no-video");
      v.removeAttribute("src");
    }

    let peaks = item.peaks;
    if (!peaks) {
      try { const pj = await api(item.peaks_url); peaks = pj.peaks; item.peaks = peaks; } catch (e) { peaks = []; }
    }
    await loadProject(item);
    loadWavesurfer(item, peaks);
    renderSegments();
    updateTransport();

    // 实时字幕
    state.subs = []; state.currentSubIdx = -1;
    renderSubs();
    if (item.subs_url) {
      try {
        const sj = await api(item.subs_url);
        state.subs = sj.subs || [];
      } catch (e) { state.subs = []; }
      renderSubs();
    }
  }

  function loadWavesurfer(item, peaks) {
    if (state.ws) { try { state.ws.destroy(); } catch (e) {} state.ws = null; }
    state.selection = null; state.selectionRegion = null;
    state.dragRegion = null;
    state.multiRegions = []; state.ctrlMarking = false;
    state.auditionSeq = null; state.auditionIdx = 0;
    $("#empty-state").classList.add("hidden");

    const timeline = Timeline.create({ container: "#timeline", height: 24 });
    state.regions = Regions.create({ color: "rgba(108,156,255,0.25)" });
    const minimap = Minimap.create({
      container: "#minimap", height: 44,
      waveColor: "#3a3a55", progressColor: "#6c9cff",
      interact: false,   // 总览条的点击/拖动由本页面操作（M6 播放头）
    });

    const ws = WaveSurfer.create({
      container: "#waveform",
      height: 150,
      waveColor: "#6a6a8c",
      progressColor: "#6c9cff",
      cursorColor: "#ffd166",
      cursorWidth: 1,
      backend: "MediaElement",
      url: item.audio_url,
      peaks: (peaks && peaks.length ? peaks.map((p) => Math.max(Math.abs(p[0]), Math.abs(p[1]))) : undefined),
      duration: item.duration,
      plugins: [timeline, state.regions, minimap],
    });
    state.ws = ws;

    // 时间轴与波形同步：放大后刻度按绝对坐标定位，需要让容器宽度跟随波形总宽度并随滚动偏移
    const syncTimeline = () => {
      const tl = document.querySelector("#timeline [part='timeline']");
      if (!tl || !ws.getWrapper()) return;
      tl.style.width = ws.getWrapper().scrollWidth + "px";
      tl.style.transform = "translateX(" + (-ws.getScroll()) + "px)";
    };
    ws.on("redraw", syncTimeline);
    ws.on("scroll", syncTimeline);

    // 选区
    state.regions.enableDragSelection({ color: "rgba(108,156,255,0.25)" });
    // 拖拽进行中（未松手）会先触发 region-initialized，记录以便右键取消
    state.regions.on("region-initialized", (region) => { state.dragRegion = region; });
    state.regions.on("region-created", (region) => {
      state.dragRegion = null;
      if (!state.ctrlMarking) {                       // Ctrl 快进多选：保留之前标记
        if (state.selectionRegion && state.selectionRegion !== region) {
          try { state.selectionRegion.remove(); } catch (e) {}
        }
        clearMultiRegions();                          // 普通拖选：清空多选标记
      }
      state.selectionRegion = region;
      state.selection = { start: region.start, end: region.end };
      updateSelUI();
    });
    state.regions.on("region-updated", (region) => {
      if (region === state.selectionRegion) {
        const m = state.multiRegions.find((x) => x.region === region);
        if (m) { m.start = region.start; m.end = region.end; }
        state.selection = { start: region.start, end: region.end };
        updateSelUI();
      }
    });
    state.regions.on("region-removed", (region) => {
      const i = state.multiRegions.findIndex((m) => m.region === region);
      if (i >= 0) { state.multiRegions.splice(i, 1); updateSelUI(); }
    });

    ws.on("play", () => { state.playing = true; updatePlayUI(); videoPlay(); });
    ws.on("pause", () => { state.playing = false; updatePlayUI(); videoPause(); });
    ws.on("finish", () => { state.playing = false; updatePlayUI(); });
    ws.on("timeupdate", (t) => {
      $("#cur-time").textContent = fmtT(t);
      videoSync(t);
      loopCheck(t);
      updateCurrentSub(t);
      auditionCheck(t);
      updateMMCursor(t);
    });
    ws.on("ready", () => { updateTransport(); updateMMCursor(0); });
    ws.on("error", (e) => toast("播放错误: " + (e && e.message ? e.message : e)));
  }

  // ── 播放控制 ───────────────────────────────────────────
  function togglePlay() { if (state.ws) { if (state.playing) state.ws.pause(); else state.ws.play(); } }
  function updatePlayUI() {
    const icon = state.playing ? "⏸" : "▶";
    $("#btn-play2").textContent = icon;
    $("#btn-loop").classList.toggle("primary", state.loop);
    $("#btn-loop").textContent = state.loop ? "循环中" : "循环";
  }
  function toggleLoop() { state.loop = !state.loop; updatePlayUI(); }
  function playSelection() {
    if (!state.ws || !state.selection) return toast("请先拖拽出选区");
    if (state.multiRegions.length >= 2) {       // 多选：顺序试听全部标记段
      state.auditionSeq = state.multiRegions.slice();
      state.auditionIdx = 0;
      playSeqItem();
      return;
    }
    if (!state.loop) state.auditioning = { start: state.selection.start, end: state.selection.end };
    state.ws.setTime(state.selection.start);
    state.ws.play();
  }
  function playSeqItem() {
    const m = state.auditionSeq && state.auditionSeq[state.auditionIdx];
    if (!m) { state.auditionSeq = null; return; }
    state.ws.setTime(m.start);
    state.ws.play();
    state.auditioning = { start: m.start, end: m.end };
  }
  function loopCheck(t) {
    if (state.loop && state.selection && state.selection.end - state.selection.start > 0.02
        && t >= state.selection.end - 0.03) {
      state.auditioning = null;
      state.ws.setTime(state.selection.start);
    }
  }
  function auditionCheck(t) {
    if (state.auditioning && t >= state.auditioning.end - 0.02) {
      if (state.auditionSeq && state.auditionIdx + 1 < state.auditionSeq.length) {
        state.auditionIdx++;
        playSeqItem();
      } else {
        state.ws.pause();
        state.auditioning = null;
        state.auditionSeq = null;
      }
    }
  }
  function updateTransport() {
    if (!state.currentItem) { $("#dur-info").textContent = "—"; return; }
    $("#dur-info").textContent = fmtDur(state.currentItem.duration);
  }

  // ── 总览条播放头（M6） ──
  function updateMMCursor(t) {
    const w = document.querySelector("#minimap-wrap");
    const c = document.querySelector("#mm-cursor");
    if (!w || !c) return;
    if (!state.ws || !state.currentItem) { c.classList.add("hidden"); return; }
    const cur = (t == null ? state.ws.getCurrentTime() : t);
    const dur = state.currentItem.duration || 1;
    const x = Math.max(0, Math.min(1, cur / dur));
    c.classList.remove("hidden");
    c.style.left = (x * 100) + "%";
    const tt = document.querySelector("#mm-time");
    if (tt) tt.textContent = fmtT(cur);
  }
  function mmSeekFromEvent(e) {
    if (!state.ws || !state.currentItem) return;
    const w = document.querySelector("#minimap-wrap");
    if (!w) return;
    const r = w.getBoundingClientRect();
    if (r.width <= 0) return;
    const f = clampN((e.clientX - r.left) / r.width, 0, 1);
    state.ws.setTime(f * state.currentItem.duration);
  }
  let mmDragging = false;
  function mmSeekDown(e) { mmDragging = true; mmSeekFromEvent(e); try { document.querySelector("#minimap-wrap").setPointerCapture(e.pointerId); } catch (err) {} }
  function mmSeekMove(e) { if (mmDragging) mmSeekFromEvent(e); }
  function mmSeekUp(e) { mmDragging = false; try { document.querySelector("#minimap-wrap").releasePointerCapture(e.pointerId); } catch (err) {} }
  function setupMMSeek() {
    const w = document.querySelector("#minimap-wrap");
    if (!w || w.dataset.mm) return;
    w.dataset.mm = "1";
    w.addEventListener("pointerdown", mmSeekDown);
    w.addEventListener("pointermove", mmSeekMove);
    w.addEventListener("pointerup", mmSeekUp);
    w.addEventListener("pointercancel", mmSeekUp);
    window.addEventListener("resize", () => updateMMCursor(state.ws ? state.ws.getCurrentTime() : 0));
  }
  function updateSelUI() {
    const n = state.multiRegions.length;
    $("#sel-info").textContent = (n >= 2 ? `多选 ${n} 段 · ` : "") + fmtSel(state.selection);
  }

  // 快退/快进：平移播放头（夹在 0 ~ 时长内），视频经 timeupdate 联动
  function seekBy(delta) {
    if (!state.ws) return toast("请先导入素材");
    const dur = state.currentItem ? state.currentItem.duration : state.ws.getDuration();
    state.ws.setTime(clampN(state.ws.getCurrentTime() + delta, 0, dur || 0));
  }
  // 音量 ±：波形与视频音量同步调整
  function adjVolume(delta) {
    if (!state.ws) return toast("请先导入素材");
    const v = clampN(state.ws.getVolume() + delta, 0, 1);
    state.ws.setVolume(v);
    toast("音量 " + Math.round(v * 100) + "%", 1200);
  }

  // ── Ctrl+→ 多选快进：每按一次标记一段并前进，可连续累积多段 ──
  const MULTI_COLOR = "rgba(255,170,80,0.4)"; // 多选标记色（橙），区别于普通选区（蓝）
  function markForward() {
    if (!state.ws) return toast("请先导入素材");
    const dur = state.currentItem ? state.currentItem.duration : state.ws.getDuration();
    const t = state.ws.getCurrentTime();
    const start = t, end = Math.min(dur, t + SEEK_STEP);
    if (end - start < 0.05) return toast("已到末尾");
    state.ctrlMarking = true;
    const region = state.regions.addRegion({ start, end, color: MULTI_COLOR, drag: true, resize: true });
    state.ctrlMarking = false;
    state.multiRegions.push({ start, end, region });
    state.selectionRegion = region;
    state.selection = { start, end };
    state.ws.setTime(end);
    updateSelUI();
  }
  function unmarkLast() {
    if (!state.multiRegions.length) { seekBy(-SEEK_STEP); return; }
    const last = state.multiRegions.pop();
    try { last.region.remove(); } catch (e) {}
    const prev = state.multiRegions[state.multiRegions.length - 1];
    state.selectionRegion = prev ? prev.region : null;
    state.selection = prev ? { start: prev.start, end: prev.end } : null;
    state.ws.setTime(prev ? prev.start : Math.max(0, (last ? last.start : 0) - SEEK_STEP));
    updateSelUI();
  }
  function clearMultiRegions() {
    state.multiRegions.slice().forEach((m) => { try { m.region.remove(); } catch (e) {} });
    state.multiRegions = [];
    updateSelUI();
  }

  function clearSelection() {
    if (state.selectionRegion) { try { state.selectionRegion.remove(); } catch (e) {} }
    state.selectionRegion = null;
    state.selection = null;
    state.dragRegion = null;
    state.auditionSeq = null; state.auditionIdx = 0;
    clearMultiRegions();
    updateSelUI();
  }

  // 视频同步
  let lastVidSync = 0;
  function videoPlay() {
    const v = $("#video-preview");
    if (v && v.src && v.paused) v.play().catch(() => {});
  }
  function videoPause() { const v = $("#video-preview"); if (v) v.pause(); }
  function videoSync(t) {
    const v = $("#video-preview");
    if (!v || !v.src) return;
    const now = performance.now();
    if (now - lastVidSync < 120) return;
    lastVidSync = now;
    if (Math.abs(v.currentTime - t) > 0.05) {
      videoSeekByAudio = true;
      v.currentTime = t;
    }
  }

  // 视频 → 音频/波形联动：拖动视频进度条 / 点击播放暂停时同步波形
  let videoSeekByAudio = false;   // 标记当前视频 seek 是否由音频同步触发
  function videoToAudioSync() {
    if (!state.ws) return;
    const v = $("#video-preview");
    if (!v || !v.src) return;
    if (Math.abs(v.currentTime - state.ws.getCurrentTime()) > 0.15) {
      state.ws.setTime(v.currentTime);
    }
  }

  // 缩放
  function zoomSet(lv) {
    if (!state.ws) return;
    state.zoomLevel = Math.max(0, Math.min(200, lv));
    state.ws.zoom(state.zoomLevel);
  }
  function zoomIn() { zoomSet(state.zoomLevel <= 0 ? 1 : state.zoomLevel * 1.5); }
  function zoomOut() { zoomSet(state.zoomLevel <= 1 ? 0 : state.zoomLevel / 1.5); }
  function nudgeSelection(delta, mode) {
    if (!state.ws || !state.selection || !state.selectionRegion) return toast("请先拖拽出选区");
    let { start, end } = state.selection;
    const dur = state.currentItem ? state.currentItem.duration : end;
    if (mode === "move") { start = Math.max(0, Math.min(dur, start + delta)); end = Math.max(0, Math.min(dur, end + delta)); }
    else { end = Math.max(start + 0.05, Math.min(dur, end + delta)); }
    state.selectionRegion.setExtent(start, end);
    state.selection = { start, end };
    updateSelUI();
  }

  // ── 片段列表 ───────────────────────────────────────────
  function segsFor(itemId) {
    if (!state.segmentsByItem.has(itemId)) state.segmentsByItem.set(itemId, []);
    return state.segmentsByItem.get(itemId);
  }
  function segIssues(seg) {
    const issues = [];
    if (seg.mixed) issues.push("混合");
    const dur = seg.end - seg.start;
    if (!seg.text.trim()) issues.push("空文本");
    if (dur < SEG_MIN) issues.push(`过短(<${SEG_MIN}s)`);
    if (dur > SEG_MAX) issues.push(`过长(>${SEG_MAX}s)`);
    return issues;
  }
  function allSegs() {
    const out = [];
    (state.items || []).forEach(item => {
      (segsFor(item.id) || []).forEach(seg => out.push({ item, seg }));
    });
    return out;
  }
  function charSegs(cid) {
    return allSegs().filter(r => (cid ? r.seg.characterId === cid : !r.seg.characterId));
  }
  function updateSegBadge(itemId, i) {
    const tr = document.querySelector(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (!tr) return;
    const segs = segsFor(itemId);
    const seg = segs[i];
    if (!seg) return;
    const issues = segIssues(seg);
    const tagCls = issues.length ? (issues.some(x => x === "空文本" || x === "混合") ? "warn" : "bad") : "ok";
    const tagTxt = issues.length ? issues.join("，") : "合规";
    const cell = tr.querySelector(".tag");
    if (cell) { cell.className = "tag " + tagCls; cell.textContent = tagTxt; }
    tr.classList.toggle("bad", issues.length > 0);
    const ch = charById(seg.characterId);
    tr.style.borderLeft = ch ? "4px solid " + ch.color : "";
  }

  function renderSegments() {
    const tb = $("#seg-tbody");
    const empty = $("#seg-empty");
    tb.innerHTML = "";
    const rows = allSegs();
    $("#seg-count").textContent = rows.length ? `(${rows.length})` : "";
    empty.classList.toggle("hidden", rows.length > 0);
    rows.forEach((row) => {
      const seg = row.seg, item = row.item;
      const i = segsFor(item.id).indexOf(seg);
      const issues = segIssues(seg);
      const cls = issues.length ? "bad" : "";
      const tagCls = issues.length ? (issues.some(x => x === "空文本" || x === "混合") ? "warn" : "bad") : "ok";
      const tagTxt = issues.length ? issues.join("，") : "合规";
      const ch = charById(seg.characterId);
      const tr = document.createElement("tr");
      tr.className = "seg-row" + (cls ? " " + cls : "") + (state.selectedSegs.has(seg.id) ? " sel" : "");
      tr.dataset.item = item.id;
      tr.dataset.i = i;
      if (ch) tr.style.borderLeft = "4px solid " + ch.color;
      tr.innerHTML = `
        <td class="seg-num">${String(i + 1).padStart(2, "0")}</td>
        <td class="seg-src" title="${esc(item.name)}">${esc(shortName(item.name))}</td>
        <td>${fmtT(seg.start)} ~ ${fmtT(seg.end)}</td>
        <td>${fmtDur(seg.end - seg.start)}</td>
        <td><span class="tag ${tagCls}">${tagTxt}</span></td>
        <td><button class="chip seg-aud" data-i="${i}">试听</button></td>
        <td><input type="text" class="seg-text" data-i="${i}" value="${esc(seg.text)}" placeholder="输入转写文本…"></td>
        <td><select class="seg-lang" data-i="${i}">
          ${["JP","ZH","EN"].map(l => `<option value="${l}" ${seg.language === l ? "selected" : ""}>${l}</option>`).join("")}
        </select></td>
        <td><select class="seg-speaker" data-i="${i}">
          <option value="">未分配</option>
          ${state.characters.map(c => `<option value="${esc(c.id)}" ${seg.characterId === c.id ? "selected" : ""} style="color:${esc(c.color)}">${esc(c.name)}</option>`).join("")}
        </select></td>
        <td class="row-actions">
          <button class="chip seg-jump" data-i="${i}">跳转</button>
          <button class="chip danger seg-del" data-i="${i}">删除</button>
        </td>`;
      tb.appendChild(tr);
    });
  }

  function addSegmentFromSelection() {
    if (!state.currentItem) return toast("请先导入素材");
    if (!state.selection) return toast("请先在波形上拖拽出选区");
    const segs = segsFor(state.currentItem.id);
    pushUndo();
    const list = state.multiRegions.length >= 2 ? state.multiRegions : [];
    if (list.length) {
      list.forEach((m) => segs.push(newSegment(m.start, m.end)));
      scheduleSaveProject();
      renderSegments();
      toast(`已加入片段 ${list.length} 条`);
      return;
    }
    segs.push(newSegment(state.selection.start, state.selection.end));
    scheduleSaveProject();
    renderSegments();
  }
  function deleteSegment(itemId, i) {
    const segs = segsFor(itemId);
    const s = segs[i];
    if (s) state.selectedSegs.delete(s.id);
    pushUndo();
    segs.splice(i, 1);
    scheduleSaveProject(itemId); renderSegments();
  }
  async function jumpToSegment(item, seg) {
    if (state.currentItem && state.currentItem.id !== item.id) await selectItem(item);
    if (!state.ws) return;
    state.ws.setTime(seg.start);
    $("#sel-info").textContent = fmtSel(seg);
  }
  async function auditionSegment(item, seg) {
    if (!state.currentItem || state.currentItem.id !== item.id) await selectItem(item);
    if (!state.ws) return;
    state.ws.setTime(seg.start);
    state.ws.play();
    state.auditioning = { start: seg.start, end: seg.end };
    scrollSegRow(item.id, seg.id);
  }
  // 从角色池/素材库等位置试听时：先回到剪辑页并选中对应素材再播放
  async function gotoEditAndPlay(item, seg) {
    closePool();
    setPage("edit");
    await auditionSegment(item, seg);
  }
  function scrollSegRow(itemId, segId) {
    const segs = segsFor(itemId);
    const i = segs.findIndex(s => s.id === segId);
    if (i < 0) return;
    const tr = document.querySelector(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (tr && tr.scrollIntoView) tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
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
    renderSegments();
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
      const segs = segsFor(item.id);
      let changed = false;
      segs.forEach(s => { if (segIds.includes(s.id)) { s.characterId = characterId || null; n++; changed = true; } });
      if (changed) scheduleSaveProject(item.id);
    });
    renderSegments(); renderPool();
    toast(`已重定向 ${n} 段片段`);
  }

  function poolCard(ch) {
    const isU = !ch;
    const mine = charSegs(ch ? ch.id : null);
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
    $("#pool-unassigned-count").textContent = charSegs(null).length;
    grid.appendChild(poolCard(null));
    state.characters.forEach(ch => grid.appendChild(poolCard(ch)));
    $("#pool-merge-count").textContent = (state.poolMerge || new Set()).size;
  }

  function poolAudition(cid) {
    const rows = charSegs(cid);
    if (!rows.length) return toast("该角色暂无片段");
    gotoEditAndPlay(rows[0].item, rows[0].seg);
  }
  function poolPlaySeg(segId, itemId) {
    const item = state.items.find(x => x.id === itemId);
    const seg = item ? segsFor(item.id).find(s => s.id === segId) : null;
    if (item && seg) gotoEditAndPlay(item, seg);
  }
  function poolRename(cid) {
    const ch = charById(cid); if (!ch) return;
    const name = prompt("角色名称：", ch.name);
    if (name == null || !name.trim()) return;
    pushUndo();
    ch.name = name.trim();
    scheduleSavePool(); renderPool(); renderSegments();
  }
  function poolDelete(cid) {
    const ch = charById(cid); if (!ch) return;
    const n = charSegs(cid).length;
    if (!confirm(`删除角色「${ch.name}」？其 ${n} 段片段将变为未分配`)) return;
    pushUndo();
    state.characters = state.characters.filter(c => c.id !== cid);
    (state.items || []).forEach(item => {
      let changed = false;
      segsFor(item.id).forEach(s => { if (s.characterId === cid) { s.characterId = null; changed = true; } });
      if (changed) scheduleSaveProject(item.id);
    });
    scheduleSavePool(); renderPool(); renderSegments();
  }
  function poolSetColor(cid, color) {
    const ch = charById(cid); if (!ch) return;
    pushUndo();
    ch.color = color;
    scheduleSavePool(); renderPool(); renderSegments();
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
      segsFor(item.id).forEach(s => { if (ids.includes(s.characterId) && s.characterId !== target.id) { s.characterId = target.id; changed = true; } });
      if (changed) scheduleSaveProject(item.id);
    });
    state.poolMerge = new Set();
    scheduleSavePool(); renderPool(); renderSegments();
    toast(`已合并为「${target.name}」`);
  }
  function createPoolCharacter() {
    const name = prompt("新角色名称：", "新角色");
    if (name == null || !name.trim()) return;
    pushUndo();
    state.characters.push({ id: uid("char"), name: name.trim(), color: paletteNext(), speakerLabels: [], created: Date.now() });
    scheduleSavePool(); renderPool(); renderSegments();
  }

  async function doIdentifySpeakers() {
    if (!state.currentProject) return toast("请先选择项目");
    toast("开始项目级说话人识别（ECAPA 声纹，将把项目内全部素材联合聚类，首次含模型加载）…");
    try {
      const j = await api(`/api/projects/${state.currentProject.id}/speakers/generate`, { method: "POST" });
      trackTask(j.task_id, async (result) => {
        if (Array.isArray(result.characters)) state.characters = result.characters;
        await loadAllItemData();
        renderPool(); renderSegments(); renderSubs();
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
    const seg = item ? segsFor(item.id).find(s => s.id === sid) : null;
    if (seg) { seg.characterId = card.dataset.poolChar || null; scheduleSaveProject(item.id); renderSegments(); renderPool(); }
  });

  // ── 实时字幕区 ────────────────────────────────────────
  function currentSubAt(t) {
    for (let i = 0; i < state.subs.length; i++) {
      const s = state.subs[i];
      if (t >= s.start - 0.05 && t < s.end + 0.05) return i;
    }
    return -1;
  }
  function updateCurrentSub(t) {
    const idx = currentSubAt(t);
    if (idx === state.currentSubIdx) { if (idx < 0) $("#sub-current").textContent = "—"; return; }
    state.currentSubIdx = idx;
    $$("#sub-tbody tr.sub-row").forEach((tr, i) => tr.classList.toggle("cur", i === idx));
    $("#sub-current").textContent = idx >= 0 ? state.subs[idx].text : "—";
    if (idx >= 0) { const row = $$("#sub-tbody tr.sub-row")[idx]; if (row) row.scrollIntoView({ block: "nearest" }); }
  }
  function renderSubs() {
    const tb = $("#sub-tbody"), empty = $("#sub-empty");
    tb.innerHTML = "";
    const subs = state.subs || [];
    $("#sub-count").textContent = subs.length ? `(${subs.length})` : "";
    empty.classList.toggle("hidden", subs.length > 0);
    subs.forEach((s, i) => {
      const lbl = speakerLabelAt((s.start + s.end) / 2);
      const key = state.currentItem ? state.currentItem.id + ":" + lbl : "";
      const ch = lbl ? state.characters.find(c => (c.speakerLabels || []).includes(key)) : null;
      const tr = document.createElement("tr");
      tr.className = "sub-row" + (i === state.currentSubIdx ? " cur" : "");
      tr.dataset.i = i;
      tr.innerHTML = `
        <td>${fmtT(s.start)} ~ ${fmtT(s.end)}</td>
        <td class="sub-text">${esc(s.text)}</td>
        <td class="sub-speaker">${ch ? `<span class="spk-badge" style="background:${esc(ch.color)}">${esc(ch.name)}</span>` : (lbl ? `<span class="spk-badge">${esc(lbl)}</span>` : "")}</td>
        <td class="sub-act">
          <button class="chip sub-sel" data-i="${i}">选区</button>
          <button class="chip primary sub-add" data-i="${i}">加片段</button>
        </td>`;
      tb.appendChild(tr);
    });
  }
  function selectSubRange(i) {
    const s = state.subs[i];
    if (!state.ws || !s) return;
    state.regions.addRegion({ start: s.start, end: s.end, color: "rgba(108,156,255,0.25)" });
    state.ws.setTime(s.start);
  }
  function addSubToSegments(i) {
    if (!state.currentItem) return toast("请先选择素材");
    const s = state.subs[i];
    if (!s) return;
    const segs = segsFor(state.currentItem.id);
    pushUndo();
    segs.push(newSegment(s.start, s.end, s.text || ""));
    scheduleSaveProject();
    renderSegments();
    toast("已加入片段：" + ((s.text || "").slice(0, 24) || "（空文本）"));
  }
  function addCurrentSubToSegments() {
    if (!state.currentItem) return toast("请先选择素材");
    if (state.currentSubIdx >= 0 && state.subs[state.currentSubIdx]) { addSubToSegments(state.currentSubIdx); return; }
    if (state.selection) {
      const segs = segsFor(state.currentItem.id);
      pushUndo();
      segs.push(newSegment(state.selection.start, state.selection.end));
      scheduleSaveProject();
      renderSegments();
      toast("已加入片段（选区）");
      return;
    }
    toast("请先播放到有字幕的位置");
  }
  async function uploadSubFile(file) {
    if (!state.currentItem) return toast("请先选择素材");
    const fd = new FormData();
    fd.append("file", file);
    toast("加载字幕: " + file.name);
    try {
      const j = await api(`/api/subtitles/${state.currentItem.id}`, { method: "POST", body: fd });
      state.subs = j.subs || [];
      renderSubs();
      toast("字幕已加载：" + j.count + " 条");
    } catch (e) { toast("字幕加载失败: " + e.message, 6000); }
  }
  async function generateSubs() {
    if (!state.currentItem) return toast("请先选择素材");
    const itemId = state.currentItem.id;
    toast("正在生成字幕（Whisper 识别）…");
    try {
      const j = await api(`/api/subtitles/${itemId}/generate`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: "medium" }) });
      trackTask(j.task_id, async (result) => {
        try {
          const sj = await api(`/api/subtitles/${itemId}`);
          state.subs = sj.subs || [];
        } catch (e) { state.subs = (result && result.subs) || []; }
        renderSubs();
        toast("字幕生成完成：" + ((result && result.count) || 0) + " 条，请人工校对");
      });
    } catch (e) { toast("生成失败: " + e.message); }
  }

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
    const segs = segsFor(itemId);
    const seg = segs[i];
    if (!seg) return;
    if (btn) {
      if (btn.classList.contains("seg-aud")) { auditionSegment(state.items.find(x => x.id === itemId), seg); return; }
      else if (btn.classList.contains("seg-jump")) { jumpToSegment(state.items.find(x => x.id === itemId), seg); return; }
      else if (btn.classList.contains("seg-del")) { deleteSegment(itemId, i); return; }
    }
    e.preventDefault();
    const rows = allSegs();
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
    renderSegments();
  });
  $("#seg-tbody").addEventListener("input", (e) => {
    const tr = e.target.closest("tr.seg-row");
    if (!tr) return;
    const itemId = tr.dataset.item;
    const i = Number(e.target.dataset.i);
    const segs = segsFor(itemId);
    if (!segs[i]) return;
    if (e.target.classList.contains("seg-text")) {
      if (!e.target.dataset.undoed) { e.target.dataset.undoed = "1"; pushUndo(); }
      segs[i].text = e.target.value; scheduleSaveProject(itemId);
    }
    updateSegBadge(itemId, i);
  });
  $("#seg-tbody").addEventListener("change", (e) => {
    const tr = e.target.closest("tr.seg-row");
    if (!tr) return;
    const itemId = tr.dataset.item;
    const i = Number(e.target.dataset.i);
    const segs = segsFor(itemId);
    if (!segs[i]) return;
    delete e.target.dataset.undoed;
    if (!e.target.classList.contains("seg-text")) pushUndo();
    if (e.target.classList.contains("seg-lang")) { segs[i].language = e.target.value; scheduleSaveProject(itemId); }
    if (e.target.classList.contains("seg-speaker")) {
      segs[i].characterId = e.target.value || null;
      scheduleSaveProject(itemId); renderSegments();
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
    const segs = segsFor(itemId);
    let ids = Array.from(state.selectedSegs).filter(id => allSegs().some(r => r.seg.id === id));
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
    renderPool(); renderSegments(); renderSubs();
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
    if (item) selectItem(item);
  }

  // ── 导入 ───────────────────────────────────────────────
  function importDialog() { $("#file-input").click(); }
  async function uploadFile(file) {
    const fd = new FormData();
    fd.append("file", file);
    if (state.currentProject) fd.append("project_id", state.currentProject.id);
    toast(`导入中: ${file.name}`);
    try {
      const j = await api("/api/import", { method: "POST", body: fd });
      trackTask(j.task_id, (result) => {
        selectResultItem(result);
        if (result && result.auto_task_id) trackTask(result.auto_task_id, autoAnalyzeDone);
      });
    } catch (e) { toast("导入失败: " + e.message); }
  }

  // ── 清洗动作 ───────────────────────────────────────────
  function needItem() { if (!state.currentItem) { toast("请先选择素材"); return false; } return true; }
  function doDenoise() {
    if (!needItem()) return;
    toast("开始降噪…");
    api("/api/denoise", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: state.currentItem.id }) })
      .then(j => trackTask(j.task_id, r => selectResultItem(r)))
      .catch(e => toast("降噪失败: " + e.message));
  }
  function doSeparate() {
    if (!needItem()) return;
    toast("开始人声分离（GPU，首次含模型加载）…");
    api("/api/separate", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: state.currentItem.id }) })
      .then(j => trackTask(j.task_id, r => selectResultItem(r)))
      .catch(e => toast("分离失败: " + e.message));
  }
  function doTrim() {
    if (!needItem()) return;
    api("/api/trim", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: state.currentItem.id }) })
      .then(j => trackTask(j.task_id, r => selectResultItem(r)))
      .catch(e => toast("去静音失败: " + e.message));
  }

  async function doAutosplit() {
    if (!needItem()) return;
    const item = state.currentItem;
    const threshold_db = Number($("#as-threshold").value);
    const min_silence = Number($("#as-min-silence").value);
    const min_len = Number($("#as-min-len").value);
    const max_len = Number($("#as-max-len").value);
    if (!(min_len > 0) || !(max_len >= min_len)) return toast("最短片段需 >0 且不能大于最长片段");
    hideModal("#modal-autosplit");
    if (!confirm("将替换当前素材「" + item.name + "」的全部片段，确定继续？")) return;
    await saveProjectNow();
    toast("开始按静音自动切分…");
    pushUndo();
    try {
      const j = await api(`/api/items/${item.id}/autosplit`, { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          threshold_db: threshold_db || -35,
          min_silence: min_silence || 0.5,
          min_len: min_len || 0.8,
          max_len: max_len || 15,
          language: $("#as-language").value || "JP",
        }) });
      trackTask(j.task_id, async (result) => {
        state.dirtyItems.delete(item.id);
        await loadProject(item, true);
        renderSegments();
        toast("自动切分完成：" + (result.count || 0) + " 段");
      });
    } catch (e) { toast("自动切分启动失败: " + e.message, 6000); }
  }

  // ── 导出选区 ───────────────────────────────────────────
  function openExportModal() {
    if (!needItem()) return;
    if (!state.selection) return toast("请先拖拽出选区");
    $("#ex-range").textContent = fmtSel(state.selection);
    showModal("#modal-export");
  }
  async function doExportSelection() {
    if (!state.currentItem || !state.selection) return;
    const fmt = $("#ex-fmt").value, sr = $("#ex-sr").value;
    hideModal("#modal-export");
    try {
      const j = await api("/api/export", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_id: state.currentItem.id,
          start: state.selection.start, end: state.selection.end,
          format: fmt, sample_rate: Number(sr) }) });
      showResult("导出成功", `文件：${j.name}\n位置：${j.path}\n\n[下载](${j.download_url})`,
        `<a class="btn primary" href="${j.download_url}" download>保存到浏览器</a>`);
    } catch (e) { toast("导出失败: " + e.message); }
  }

  // ── B 站 ───────────────────────────────────────────────
  // ── 网络 URL 导入（多平台 / 批量） ──
  // 导入后在后台自动完成：解析 → 下载音频（立刻可剪辑）→ 下载完整视频（本地预览）。
  async function doUrlOpen() {
    const raw = $("#bb-url").value.trim();
    const urls = raw.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    if (!urls.length) return toast("请输入至少一个视频链接");
    hideModal("#modal-bilibili");
    toast(`已提交 ${urls.length} 个链接，后台下载中…`);
    try {
      const j = await api("/api/url/open", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls, project_id: state.currentProject ? state.currentProject.id : undefined }) });
      const results = j.results || [];
      let ok = 0, fail = 0;
      results.forEach(r => {
        if (r.ok) {
          ok++;
          // 后台下载：完成音频即出素材，视频随后补齐本地预览
          trackTask(r.task_id, (res) => {
            selectResultItem(res);
            if (res && res.auto_task_id) trackTask(res.auto_task_id, autoAnalyzeDone);
          });
        } else { fail++; toast(`提交失败: ${r.url} — ${r.error}`, 6000); }
      });
      toast(`已提交 ${ok} 个链接后台下载${fail ? `，${fail} 个失败` : ""}`);
    } catch (e) { toast("URL 导入失败: " + e.message, 6000); }
  }

  // ── 转写 ───────────────────────────────────────────────
  function openTranscribeModal() {
    if (!state.currentProject) return toast("请先选择项目");
    const n = (state.items || []).reduce((a, item) => a + segsFor(item.id).filter(s => !s.text.trim()).length, 0);
    if (!n) return toast("片段列表为空或都已填写文本");
    showModal("#modal-transcribe");
  }
  async function doTranscribe() {
    if (!state.currentProject) return;
    const model = $("#tr-model").value;
    hideModal("#modal-transcribe");
    pushUndo();
    let total = 0;
    (state.items || []).forEach((item) => {
      const segs = segsFor(item.id);
      const todo = segs.map((s, i) => ({ ...s, idx: i })).filter(x => !x.text.trim());
      if (!todo.length) return;
      total += todo.length;
      api("/api/transcribe", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_id: item.id, segments: todo.map(s => ({ start: s.start, end: s.end })), model }) })
        .then(j => trackTask(j.task_id, (result) => {
          const cur = segsFor(item.id);
          (result.texts || []).forEach((text, k) => { cur[todo[k].idx].text = text; });
          scheduleSaveProject(item.id);
          renderSegments();
        }))
        .catch(e => toast("转写启动失败: " + e.message));
    });
    toast(`转写 ${total} 段（${model}）…`);
  }

  // ── 训练集导出 ─────────────────────────────────────────
  function openDatasetModal() {
    if (!state.currentProject) return toast("请先选择项目");
    const n = (state.items || []).reduce((a, item) => a + segsFor(item.id).length, 0);
    if (!n) return toast("片段列表为空");
    showModal("#modal-dataset");
  }
  async function doDatasetExport() {
    if (!state.currentProject) return;
    hideModal("#modal-dataset");
    const clips = [];
    (state.items || []).forEach(item => {
      segsFor(item.id).forEach(s => clips.push({
        item_id: item.id, start: s.start, end: s.end, text: s.text,
        language: s.language, speaker: (charById(s.characterId) || {}).name || "",
      }));
    });
    toast("导出训练集…");
    try {
      const j = await api("/api/dataset/export", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: state.currentProject.id,
          clips,
          speaker: $("#ds-speaker").value.trim() || "speaker",
          language: $("#ds-language").value,
          layout: $("#ds-per-speaker").checked ? "per_speaker" : "flat",
          val_ratio: Number($("#ds-val-ratio").value) || 0,
          out_dir: $("#ds-outdir").value.trim() || undefined,
        }) });
      trackTask(j.task_id, (result) => {
        const skipped = (result.skipped || []).map(s => `  - [跳过] ${s.reason}（${fmtT(s.seg.start)}~${fmtT(s.seg.end)}）`).join("\n") || "  （无跳过）";
        const spk = (result.speakers || []).map(s => `  - ${s.name}: train ${s.train} / val ${s.val}`).join("\n") || "  （无）";
        showResult("训练集导出完成",
          `输出目录：${result.out_dir}\n成功片段：${result.count}\n\n角色分布：\n${spk}\n\n${skipped}\n\nlist.txt：${result.list_file}`,
          null, 600);
      });
    } catch (e) { toast("导出训练集失败: " + e.message); }
  }

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
      "import": importDialog,
      "bilibili": () => showModal("#modal-bilibili"),
      "url-open": () => showModal("#modal-bilibili"),
      "project-new": createProject,
      "project-rename": renameProject,
      "project-delete": deleteProject,
      "pool": openPool,
      "identify-speakers": doIdentifySpeakers,
      "export-selection": openExportModal,
      "denoise": doDenoise,
      "separate": doSeparate,
      "trim": doTrim,
      "transcribe": openTranscribeModal,
      "dataset-export": openDatasetModal,
      "autosplit": () => showModal("#modal-autosplit"),
      "undo": undo,
      "redo": redo,
      "validate": renderSegments,
      "zoom-in": zoomIn,
      "zoom-out": zoomOut,
      "fit": () => zoomSet(0),
      "zoom-sel": () => { if (state.selection) { zoomSet(0); state.ws.setTime(state.selection.start); } },
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
        if (k === "+" || k === "=" || k === "Add" || k === "NumpadAdd") { e.preventDefault(); zoomIn(); return; }
        if (k === "-" || k === "Subtract" || k === "NumpadSubtract") { e.preventDefault(); zoomOut(); return; }
        if (k === "0") { e.preventDefault(); zoomSet(0); return; }
      }
      const tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (e.ctrlKey && (e.key === "o" || e.key === "O")) { e.preventDefault(); importDialog(); return; }
      if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        if (e.shiftKey) redo(); else undo();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === "y" || e.key === "Y")) { e.preventDefault(); redo(); return; }

      // 小键盘快进：−/+快退/快进 15 秒；数字区方向键(2/4/6/8)等效主方向键（兼容 NumLock 开关）
      const code = e.code || "";
      if (code === "NumpadSubtract") { e.preventDefault(); seekBy(-SEEK_FAST); return; }
      if (code === "NumpadAdd") { e.preventDefault(); seekBy(SEEK_FAST); return; }
      if (code === "Numpad4" || code === "Numpad6") { e.preventDefault(); seekBy(code === "Numpad6" ? SEEK_STEP : -SEEK_STEP); return; }
      if (code === "Numpad8" || code === "Numpad2") { e.preventDefault(); adjVolume(code === "Numpad8" ? VOL_STEP : -VOL_STEP); return; }

      switch (e.key) {
        case " ": e.preventDefault(); togglePlay(); break;
        case "l": case "L": toggleLoop(); break;
        case "e": case "E": openExportModal(); break;
        case "n": case "N": doDenoise(); break;
        case "v": case "V": doSeparate(); break;
        case "ArrowLeft": case "ArrowRight": {
          e.preventDefault();
          const d = e.key === "ArrowRight" ? SEEK_STEP : -SEEK_STEP;
          if (e.ctrlKey) { (e.key === "ArrowRight" ? markForward() : unmarkLast()); } // Ctrl+→ 快进多选 / Ctrl+← 撤销上一段
          else if (e.shiftKey) nudgeSelection(d, "end"); // Shift+←→ 微调选区终点边界
          else seekBy(d);                                // ←→ 快退 / 快进 5 秒
          break;
        }
        case "ArrowUp": case "ArrowDown": e.preventDefault(); adjVolume(e.key === "ArrowUp" ? VOL_STEP : -VOL_STEP); break;
        case "Delete":
          if (state.selectedSegs.size) {
            pushUndo();
            (state.items || []).forEach(item => {
              const segs = segsFor(item.id);
              const before = segs.length;
              const kept = segs.filter(s => !state.selectedSegs.has(s.id));
              if (kept.length !== before) { segs.splice(0, segs.length, ...kept); scheduleSaveProject(item.id); }
            });
            state.selectedSegs = new Set();
            renderSegments();
          } else if (focusedSeg) {
            deleteSegment(focusedSeg.item, focusedSeg.i);
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
      files.forEach(uploadFile);
    });
    $("#file-input").addEventListener("change", () => {
      Array.from($("#file-input").files).forEach(uploadFile);
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
    $("#btn-import").addEventListener("click", importDialog);
    $("#btn-bilibili").addEventListener("click", () => showModal("#modal-bilibili"));
    $("#btn-export-dataset").addEventListener("click", openDatasetModal);
    $("#btn-play2").addEventListener("click", togglePlay);
    $("#btn-prev").addEventListener("click", () => state.ws && state.ws.setTime(0));
    $("#btn-next").addEventListener("click", () => state.ws && state.ws.setTime(state.currentItem ? state.currentItem.duration : 0));
    $("#btn-loop").addEventListener("click", toggleLoop);
    $("#btn-play-selection").addEventListener("click", playSelection);
    $("#btn-export-selection").addEventListener("click", openExportModal);
    $("#minimap-toggle").addEventListener("change", (e) => $("#minimap-wrap").classList.toggle("hidden", !e.target.checked));
    $("#btn-add-seg").addEventListener("click", addSegmentFromSelection);
    $("#btn-clear-segs").addEventListener("click", () => {
      if (!state.currentItem) return;
      if (confirm("清空当前素材的全部片段？")) {
        pushUndo();
        segsFor(state.currentItem.id).length = 0;
        state.selectedSegs = new Set();
        scheduleSaveProject(); renderSegments();
      }
    });
    $("#btn-transcribe").addEventListener("click", openTranscribeModal);
    $("#btn-pool").addEventListener("click", openPool);
    $("#btn-identify-speakers").addEventListener("click", doIdentifySpeakers);
    $("#btn-auto-analyze").addEventListener("click", toggleAutoAnalyze);
    $("#btn-auto-analyze2").addEventListener("click", toggleAutoAnalyze);
    $("#pool-back").addEventListener("click", closePool);
    $("#pool-close").addEventListener("click", closePool);
    $("#pool-new").addEventListener("click", createPoolCharacter);
    $("#pool-merge").addEventListener("click", mergePoolSelected);
    $("#pool-identify").addEventListener("click", doIdentifySpeakers);

    $("#bb-open").addEventListener("click", doUrlOpen);
    $("#tr-start").addEventListener("click", doTranscribe);
    $("#ds-start").addEventListener("click", doDatasetExport);
    $("#as-start").addEventListener("click", doAutosplit);
    $("#ex-start").addEventListener("click", doExportSelection);
    // 视频 ↔ 音频双向联动
    const vp = $("#video-preview");
    vp.addEventListener("volumechange", () => { if (!vp.muted) vp.muted = true; });
    vp.addEventListener("seeked", () => {
      const drift = state.ws ? Math.abs(vp.currentTime - state.ws.getCurrentTime()) : 0;
      if (videoSeekByAudio && drift <= 0.3) { videoSeekByAudio = false; return; }
      videoSeekByAudio = false;
      videoToAudioSync();
    });
    vp.addEventListener("play", () => { videoToAudioSync(); if (state.ws) state.ws.play(); });
    vp.addEventListener("pause", () => { if (state.ws) state.ws.pause(); });
    // 屏蔽 Ctrl+滚轮 页面缩放，改为时间轴缩放
    window.addEventListener("wheel", (e) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      if (!state.ws) return;
      if (e.deltaY < 0) zoomIn(); else zoomOut();
    }, { passive: false });

    $$(".modal-mask").forEach((mask) => mask.addEventListener("click", (e) => {
      if (e.target === mask || e.target.closest("[data-close]")) mask.classList.add("hidden");
    }));
    $("#bb-url").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doUrlOpen(); } });
    // 实时字幕
    $("#btn-sub-open").addEventListener("click", () => $("#sub-file").click());
    $("#btn-sub-generate").addEventListener("click", generateSubs);
    $("#btn-sub-add").addEventListener("click", addCurrentSubToSegments);
    $("#sub-file").addEventListener("change", () => {
      const f = $("#sub-file").files[0];
      if (f) uploadSubFile(f);
      $("#sub-file").value = "";
    });
    $("#sub-tbody").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      const tr = e.target.closest("tr.sub-row");
      if (!tr) return;
      const i = Number(tr.dataset.i);
      if (btn && btn.classList.contains("sub-sel")) selectSubRange(i);
      else if (btn && btn.classList.contains("sub-add")) addSubToSegments(i);
      else selectSubRange(i);
    });
    // 波形区域内右键：不弹浏览器菜单，始终取消选区/本次拖拽
    $("#wave-box").addEventListener("contextmenu", (e) => {
      e.preventDefault();
      if (state.dragRegion) {
        try { state.dragRegion.remove(); } catch (err) {}
        if (state.selectionRegion) { try { state.selectionRegion.remove(); } catch (err) {} }
        state.dragRegion = null;
        state.selectionRegion = null;
        state.selection = null;
        updateSelUI();
        toast("已取消选区");
        return;
      }
      if (state.selection && state.selectionRegion) {
        clearSelection();
        toast("已取消选区");
      }
    });
  }


  // ── 页面切换（素材库 / 剪辑 / 训练交付） ──
  let training = null;   // 训练交付页模块实例（启动区由 createTraining 创建）
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
    const b2 = $("#btn-import2"); if (b2) b2.addEventListener("click", importDialog);
    const u2 = $("#btn-url2"); if (u2) u2.addEventListener("click", () => showModal("#modal-bilibili"));
    const p2 = $("#btn-pool2"); if (p2) p2.addEventListener("click", openPool);
    const rf = $("#btn-train-refresh"); if (rf) rf.addEventListener("click", () => training && training.loadTraining(true));
    setPage(currentPage);
  }

  // ── 启动 ───────────────────────────────────────────────
  setupMenus();
  setupShortcuts();
  setupDrop();
  bindUI();
  initWorkspace();
  setupMMSeek();
  training = createTraining({ $, esc, shortName, fmtDur, api, state, toast, trackTask });
  setupPagebar();
  setInterval(() => {
    if (currentPage === "train" && !document.getElementById("page-train").classList.contains("hidden")) {
      if (training) training.loadTraining(false);
    }
  }, 8000);
  function beaconSave() {
    try {
      Array.from(state.dirtyItems).forEach(id => {
        const payload = JSON.stringify({ segments: segsFor(id), speaker_segments: state.speakerSegsByItem.get(id) || [] });
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
  window.__vc = { state, selectItem, selectProject, renderSegments, WaveSurfer, Timeline, Regions, Minimap,
    markForward, unmarkLast, clearMultiRegions,
    loadProject, saveProjectNow, savePoolNow, openPool, closePool, renderPool,
    undo, redo, pushUndo, doAutosplit, uploadFile,
    createProject, renameProject, deleteProject, doIdentifySpeakers, newSegment,
    toggleAutoAnalyze, autoAnalyzeDone, attachActiveTasks,
    setPage, loadTraining: training.loadTraining, startTrain: training.startTrain,
    doInfer: training.doInfer, train: training.train,
    workspace: { layout, applyLayout, saveLayout, resetLayout, togglePanel, swapPanels, PANELS } };
})();
