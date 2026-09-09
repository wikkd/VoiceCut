import WaveSurfer from "/static/vendor/wavesurfer.esm.js";
import Timeline from "/static/vendor/plugins/timeline.esm.js";
import Regions from "/static/vendor/plugins/regions.esm.js";
import Minimap from "/static/vendor/plugins/minimap.esm.js";

// VoiceCut 前端 — wavesurfer v7 (UMD) + Flask REST
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const state = {
    items: [],
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
    characters: [],            // 当前素材角色池 [{id,name,color,speakerLabels,created}]
    speakerSegs: [],           // 当前素材说话人分段 [{start,end,label}]
    selectedSegs: new Set(),   // 片段列表多选行索引
    poolMerge: new Set(),      // 角色池合并勾选集
    projectDirty: false,       // 片段/角色有改动待保存
    subs: [],                 // 实时字幕 [{start,end,text}]
    currentSubIdx: -1,        // 当前播放头命中的字幕行索引
    bootErr: null,
  };

  const SEG_MIN = 1.0, SEG_MAX = 15.0;
  const SEEK_STEP = 5, SEEK_FAST = 15, VOL_STEP = 0.05; // 快退快进秒数 / 音量步进(5%)

  // ── 小工具 ─────────────────────────────────────────────
  const fmtT = (t) => {
    t = Math.max(0, t || 0);
    const m = Math.floor(t / 60), s = t - m * 60;
    return `${m}:${s.toFixed(1).padStart(4, "0")}`;
  };
  const fmtSel = (sel) => sel ? `${fmtT(sel.start)} ~ ${fmtT(sel.end)}` : "—";
  const fmtDur = (d) => `${d.toFixed(1)}s`;

  let toastTimer = null;
  function toast(msg, ms = 4000) {
    const el = $("#task-info");
    el.textContent = msg;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { if (!state.activeTasks.size) el.textContent = "就绪"; }, ms);
  }

  async function api(url, opts) {
    const r = await fetch(url, opts);
    let j = null;
    try { j = await r.json(); } catch (e) { /* ignore */ }
    if (!r.ok) throw new Error((j && j.error) || `HTTP ${r.status}`);
    return j;
  }

  // ── 工作区布局（3×3 网格 / 分隔条 / 面板换位 / localStorage 记忆） ──
  const LS_KEY = "vc.workspace.v1";
  const PANELS = ["media", "video", "wave", "sub", "seg"];
  const PANEL_IDS = {
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
  const layout = loadLayout();
  function saveLayout() { try { localStorage.setItem(LS_KEY, JSON.stringify(layout)); } catch (e) {} }
  function clampN(v, a, b) { return Math.max(a, Math.min(b, v)); }

  function applyLayout() {
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
  function updatePanelMenu() {
    $$("[data-act='panel-toggle']").forEach((b) => {
      const on = !layout.hidden.includes(b.dataset.panel);
      b.classList.toggle("panel-on", on);
      b.classList.toggle("panel-off", !on);
    });
  }
  function togglePanel(p) {
    if (!PANELS.includes(p)) return;
    const i = layout.hidden.indexOf(p);
    if (i >= 0) layout.hidden.splice(i, 1); else layout.hidden.push(p);
    applyLayout(); saveLayout();
  }
  function resetLayout() {
    try { localStorage.removeItem(LS_KEY); } catch (e) {}
    const d = cloneLayout();
    layout.area = d.area; layout.cols = d.cols; layout.rows = d.rows; layout.hidden = d.hidden;
    applyLayout(); saveLayout();
    toast("已恢复默认布局");
  }
  function swapPanels(a, b) {
    if (!PANELS.includes(a) || !PANELS.includes(b) || a === b) return;
    const ta = layout.area[a], tb = layout.area[b];
    layout.area[a] = tb; layout.area[b] = ta;
    applyLayout(); saveLayout();
  }
  function setupSplitters() {
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
  function setupGripDrag() {
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
  function setupPanelClose() {
    $$("[data-close-panel]").forEach((b) => {
      b.addEventListener("click", () => togglePanel(b.dataset.closePanel));
    });
  }
  function initWorkspace() {
    applyLayout();
    setupSplitters();
    setupGripDrag();
    setupPanelClose();
  }

  // ── 引导 ───────────────────────────────────────────────
  function fail(msg) {
    state.bootErr = msg;
    document.body.dataset.vc = "error";
    $("#boot-state").textContent = "❌ " + msg;
  }
  async function boot() {
    if (typeof WaveSurfer === "undefined") return fail("wavesurfer 未加载");
    try {
      const cfg = await api("/api/config");
      const items = await api("/api/items");
      state.items = items;
      $("#boot-state").textContent = "后端 OK · ffmpeg: " + cfg.ffmpeg.split(/[\\/]/).pop();
      document.body.dataset.vc = "ok";
      renderMediaList();
    } catch (e) { return fail("后端连接失败: " + e.message); }
  }

  // ── 素材列表 ───────────────────────────────────────────
  function renderMediaList() {
    const ul = $("#media-list");
    const empty = $("#media-empty");
    ul.innerHTML = "";
    empty.classList.toggle("hidden", state.items.length > 0);
    state.items.forEach((item) => {
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
      ul.appendChild(li);
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
  function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

  async function refreshItems() {
    try { state.items = await api("/api/items"); renderMediaList(); }
    catch (e) { /* ignore */ }
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
    if (state.projectDirty && state.currentItem && state.currentItem.id !== item.id) {
      await saveProjectNow();   // 切换素材前先浮存旧素材项目
    }
    state.currentItem = item;
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
    $("#btn-loop").textContent = state.loop ? "🔁 循环中" : "🔁 循环";
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
    const vid = $("#video-preview");
    if (vid) vid.volume = v;
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
    const dur = seg.end - seg.start;
    if (!seg.text.trim()) issues.push("空文本");
    if (dur < SEG_MIN) issues.push(`过短(<${SEG_MIN}s)`);
    if (dur > SEG_MAX) issues.push(`过长(>${SEG_MAX}s)`);
    return issues;
  }
  function renderSegments() {
    const tb = $("#seg-tbody");
    const empty = $("#seg-empty");
    tb.innerHTML = "";
    const segs = state.currentItem ? segsFor(state.currentItem.id) : [];
    $("#seg-count").textContent = segs.length ? `(${segs.length})` : "";
    empty.classList.toggle("hidden", segs.length > 0);
    segs.forEach((seg, i) => {
      const issues = segIssues(seg);
      const cls = issues.length ? "bad" : "";
      const tagCls = issues.length ? (issues.some(x => x.includes("空文本")) ? "warn" : "bad") : "ok";
      const tagTxt = issues.length ? issues.join("，") : "合规";
      const ch = charById(seg.characterId);
      const tr = document.createElement("tr");
      tr.className = "seg-row" + (cls ? " " + cls : "") + (state.selectedSegs.has(seg.id) ? " sel" : "");
      tr.dataset.i = i;
      if (ch) tr.style.borderLeft = "4px solid " + ch.color;
      tr.innerHTML = `
        <td class="seg-num">${String(i + 1).padStart(2, "0")}</td>
        <td>${fmtT(seg.start)} ~ ${fmtT(seg.end)}</td>
        <td>${fmtDur(seg.end - seg.start)}</td>
        <td><span class="tag ${tagCls}">${tagTxt}</span></td>
        <td><button class="chip seg-aud" data-i="${i}">▶ 试听</button></td>
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
  function deleteSegment(i) {
    const segs = segsFor(state.currentItem.id);
    const s = segs[i];
    if (s) state.selectedSegs.delete(s.id);
    segs.splice(i, 1);
    scheduleSaveProject(); renderSegments();
  }
  function jumpToSegment(seg) {
    if (!state.ws) return;
    state.ws.setTime(seg.start);
    $("#sel-info").textContent = fmtSel(seg);
  }
  function auditionSegment(seg) {
    if (!state.ws) return;
    state.ws.setTime(seg.start);
    state.ws.play();
    state.auditioning = { start: seg.start, end: seg.end };
  }

  // ── 角色池 / 说话人自动匹配 ──
  let segCounter = 0;
  function uid(prefix) { return prefix + "_" + Date.now().toString(36) + "_" + (++segCounter).toString(36); }

  const CHAR_PALETTE = ["#e5484d", "#f76808", "#f5d90a", "#46a758", "#3e63dd", "#8e4ec6", "#12a594", "#e93d82", "#00a2c7", "#ffb224"];
  function paletteNext() { return CHAR_PALETTE[state.characters.length % CHAR_PALETTE.length]; }
  function charById(id) { return state.characters.find(c => c.id === id) || null; }

  function newSegment(start, end, text, language) {
    const sp = autoCharacterFor(start, end);
    return { id: uid("s"), start, end, text: text || "", language: language || "JP",
             speakerLabel: sp.speakerLabel, characterId: sp.characterId };
  }

  function speakerLabelAt(t) {
    for (const s of state.speakerSegs) if (s.label && t >= s.start && t < s.end) return s.label;
    return null;
  }
  function autoCharacterFor(start, end) {
    let best = null, bestOv = 0;
    for (const s of state.speakerSegs) {
      if (!s.label) continue;
      const ov = Math.min(end, s.end) - Math.max(start, s.start);
      if (ov > bestOv) { bestOv = ov; best = s.label; }
    }
    if (!best) return { characterId: null, speakerLabel: null };
    const ch = state.characters.find(c => (c.speakerLabels || []).includes(best));
    return { characterId: ch ? ch.id : null, speakerLabel: best };
  }

  async function loadProject(item) {
    state.characters = []; state.speakerSegs = []; state.selectedSegs = new Set(); state.poolMerge = new Set();
    try {
      const proj = await api(`/api/items/${item.id}/project`);
      state.characters = proj.characters || [];
      state.speakerSegs = proj.speaker_segments || [];
      state.segmentsByItem.set(item.id, proj.segments || []);
    } catch (e) {
      state.segmentsByItem.set(item.id, []);
    }
  }

  let saveTimer = null;
  function scheduleSaveProject() {
    state.projectDirty = true;
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveProjectNow, 400);
  }
  async function saveProjectNow() {
    if (!state.currentItem || !state.projectDirty) return;
    state.projectDirty = false;
    try {
      await api(`/api/items/${state.currentItem.id}/project`, { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ characters: state.characters, segments: segsFor(state.currentItem.id) }) });
    } catch (e) { /* 静默忽略 */ }
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
    const segs = segsFor(state.currentItem.id);
    let n = 0;
    segs.forEach(s => { if (segIds.includes(s.id)) { s.characterId = characterId || null; n++; } });
    scheduleSaveProject();
    renderSegments(); renderPool();
    toast(`已重定向 ${n} 段片段`);
  }

  function poolCard(ch) {
    const isU = !ch;
    const segs = state.currentItem ? segsFor(state.currentItem.id) : [];
    const mine = isU ? segs.filter(s => !s.characterId) : segs.filter(s => s.characterId === ch.id);
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
        ${isU ? "" : `<button class="chip pool-aud" data-char="${ch.id}">▶ 试听</button>
          <button class="chip pool-rename" data-char="${ch.id}">重命名</button>
          <button class="chip pool-del danger" data-char="${ch.id}">删除</button>`}
      </div>
      <details class="pool-segs">
        <summary>片段（${mine.length}）</summary>
        <div class="pool-seg-list">
          ${mine.slice(0, 300).map(s => `
            <div class="pool-seg" draggable="true" data-seg="${s.id}">
              <span class="ps-time">${fmtT(s.start)}~${fmtT(s.end)}</span>
              <span class="ps-text">${esc(s.text.slice(0, 30) || "（空）")}</span>
              <button class="chip ps-play" data-seg="${s.id}" title="试听">▶</button>
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
    $("#pool-item-name").textContent = state.currentItem ? state.currentItem.name : "";
    const segs = state.currentItem ? segsFor(state.currentItem.id) : [];
    $("#pool-unassigned-count").textContent = segs.filter(s => !s.characterId).length;
    grid.appendChild(poolCard(null));
    state.characters.forEach(ch => grid.appendChild(poolCard(ch)));
    $("#pool-merge-count").textContent = (state.poolMerge || new Set()).size;
  }

  function poolAudition(cid) {
    const segs = segsFor(state.currentItem.id).filter(s => s.characterId === cid);
    if (!segs.length) return toast("该角色暂无片段");
    auditionSegment(segs[0]);
  }
  function poolPlaySeg(segId) {
    const seg = segsFor(state.currentItem.id).find(s => s.id === segId);
    if (seg) auditionSegment(seg);
  }
  function poolRename(cid) {
    const ch = charById(cid); if (!ch) return;
    const name = prompt("角色名称：", ch.name);
    if (name == null || !name.trim()) return;
    ch.name = name.trim();
    scheduleSaveProject(); renderPool(); renderSegments();
  }
  function poolDelete(cid) {
    const ch = charById(cid); if (!ch) return;
    const n = segsFor(state.currentItem.id).filter(s => s.characterId === cid).length;
    if (!confirm(`删除角色「${ch.name}」？其 ${n} 段片段将变为未分配`)) return;
    state.characters = state.characters.filter(c => c.id !== cid);
    segsFor(state.currentItem.id).forEach(s => { if (s.characterId === cid) s.characterId = null; });
    scheduleSaveProject(); renderPool(); renderSegments();
  }
  function poolSetColor(cid, color) {
    const ch = charById(cid); if (!ch) return;
    ch.color = color;
    scheduleSaveProject(); renderPool(); renderSegments();
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
    target.name = name.trim();
    target.speakerLabels = Array.from(new Set(chs.flatMap(c => c.speakerLabels || [])));
    state.characters = state.characters.filter(c => !ids.includes(c.id) || c.id === target.id);
    segsFor(state.currentItem.id).forEach(s => { if (ids.includes(s.characterId) && s.characterId !== target.id) s.characterId = target.id; });
    state.poolMerge = new Set();
    scheduleSaveProject(); renderPool(); renderSegments();
    toast(`已合并为「${target.name}」`);
  }
  function createPoolCharacter() {
    const name = prompt("新角色名称：", "新角色");
    if (name == null || !name.trim()) return;
    state.characters.push({ id: uid("char"), name: name.trim(), color: paletteNext(), speakerLabels: [], created: Date.now() });
    scheduleSaveProject(); renderPool(); renderSegments();
  }

  async function doIdentifySpeakers() {
    if (!needItem()) return;
    toast("开始说话人识别（ECAPA 声纹，首次含模型加载）…");
    try {
      const j = await api(`/api/items/${state.currentItem.id}/speakers/generate`, { method: "POST" });
      trackTask(j.task_id, (result) => {
        state.speakerSegs = result.speaker_segments || state.speakerSegs;
        (result.characters || []).forEach(c => { if (!charById(c.id)) state.characters.push(c); });
        const labelChar = {};
        state.characters.forEach(c => (c.speakerLabels || []).forEach(lb => labelChar[lb] = c.id));
        segsFor(state.currentItem.id).forEach(s => {
          if (s.speakerLabel && labelChar[s.speakerLabel] && !s.characterId) s.characterId = labelChar[s.speakerLabel];
        });
        scheduleSaveProject();
        renderPool(); renderSegments(); renderSubs();
        toast(`说话人识别完成：${result.n_speakers} 人（${result.quality === "ecapa" ? "ECAPA" : "MFCC 降级"}），${result.labeled}/${result.total} 段已标记`);
      });
    } catch (e) { toast("说话人识别启动失败: " + e.message, 6000); }
  }

  // 角色池页面事件
  $("#pool-grid").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const cid = btn.dataset.char, segId = btn.dataset.seg;
    if (btn.classList.contains("pool-aud")) poolAudition(cid);
    else if (btn.classList.contains("pool-rename")) poolRename(cid);
    else if (btn.classList.contains("pool-del")) poolDelete(cid);
    else if (btn.classList.contains("ps-play")) poolPlaySeg(segId);
  });
  $("#pool-grid").addEventListener("dblclick", (e) => {
    const nm = e.target.closest(".pool-name");
    if (nm) poolRename(nm.closest(".pool-card").dataset.poolChar);
  });
  $("#pool-grid").addEventListener("change", (e) => {
    if (e.target.classList.contains("pool-color")) poolSetColor(e.target.dataset.char, e.target.value);
    else if (e.target.classList.contains("pool-merge-cb")) togglePoolMerge(e.target.dataset.char, e.target.checked);
  });
  let dragSegId = null;
  $("#pool-grid").addEventListener("dragstart", (e) => {
    const seg = e.target.closest(".pool-seg");
    if (!seg) return;
    dragSegId = seg.dataset.seg;
    e.dataTransfer.setData("text/plain", dragSegId);
  });
  $("#pool-grid").addEventListener("dragover", (e) => { if (e.target.closest(".pool-card")) e.preventDefault(); });
  $("#pool-grid").addEventListener("drop", (e) => {
    const card = e.target.closest(".pool-card");
    if (!card || !state.currentItem) return;
    e.preventDefault();
    const sid = dragSegId || e.dataTransfer.getData("text/plain");
    const seg = segsFor(state.currentItem.id).find(s => s.id === sid);
    if (seg) { seg.characterId = card.dataset.poolChar || null; scheduleSaveProject(); renderSegments(); renderPool(); }
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
      const ch = lbl ? state.characters.find(c => (c.speakerLabels || []).includes(lbl)) : null;
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
    if (tr) { tr.style.outline = "1px solid var(--accent)"; focusedSeg = tr.dataset.i; }
    else focusedSeg = null;
  }

  // 片段列表：点击/多选（Shift 区间、Ctrl 追加），右键重定向角色
  $("#seg-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    const tr = e.target.closest("tr.seg-row");
    if (!tr || !state.currentItem) return;
    const i = Number(tr.dataset.i);
    const segs = segsFor(state.currentItem.id);
    const seg = segs[i];
    if (!seg) return;
    if (btn) {
      if (btn.classList.contains("seg-aud")) auditionSegment(seg);
      else if (btn.classList.contains("seg-jump")) jumpToSegment(seg);
      else if (btn.classList.contains("seg-del")) { deleteSegment(i); return; }
    }
    e.preventDefault();
    if (e.shiftKey && state.selectedSegs.size) {
      let first = 1e9;
      segs.forEach((s, k) => { if (state.selectedSegs.has(s.id)) first = Math.min(first, k); });
      const lo = Math.min(first, i), hi = Math.max(first, i);
      state.selectedSegs = new Set();
      for (let k = lo; k <= hi; k++) state.selectedSegs.add(segs[k].id);
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
    const i = Number(e.target.dataset.i);
    const segs = segsFor(state.currentItem.id);
    if (!segs[i]) return;
    if (e.target.classList.contains("seg-text")) { segs[i].text = e.target.value; scheduleSaveProject(); }
    renderSegments();   // 更新状态徽标
  });
  $("#seg-tbody").addEventListener("change", (e) => {
    const i = Number(e.target.dataset.i);
    const segs = segsFor(state.currentItem.id);
    if (!segs[i]) return;
    if (e.target.classList.contains("seg-lang")) { segs[i].language = e.target.value; scheduleSaveProject(); }
    if (e.target.classList.contains("seg-speaker")) {
      segs[i].characterId = e.target.value || null;
      scheduleSaveProject(); renderSegments();
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
    if (!tr || !state.currentItem) return;
    e.preventDefault();
    const i = Number(tr.dataset.i);
    const segs = segsFor(state.currentItem.id);
    let ids = Array.from(state.selectedSegs).filter(id => segs.some(s => s.id === id));
    if (!ids.length && segs[i]) ids = [segs[i].id];
    showRedirectMenu(e.clientX, e.clientY, ids);
  });

  // ── 任务跟踪 ───────────────────────────────────────────
  function trackTask(taskId, doneCb) {
    state.activeTasks.set(taskId, { msg: "排队中", progress: 0, doneCb });
    ensurePolling();
    updateStatusbar();
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
          if (info.doneCb) info.doneCb(t.result);
          await refreshItems();
          toast("任务完成");
        } else if (t.status === "error") {
          state.activeTasks.delete(tid);
          toast("任务失败: " + (t.message || "未知错误"), 6000);
        }
      } catch (e) { /* 网络抖动忽略 */ }
    }
    updateStatusbar();
  }
  function updateStatusbar() {
    const wrap = $("#task-bar-wrap"), bar = $("#task-bar"), info = $("#task-info");
    if (!state.activeTasks.size) {
      wrap.classList.add("hidden");
      if (!toastTimer) info.textContent = "就绪";
      return;
    }
    wrap.classList.remove("hidden");
    const arr = Array.from(state.activeTasks.values());
    const avg = arr.reduce((a, b) => a + b.progress, 0) / arr.length;
    bar.style.width = (avg * 100).toFixed(0) + "%";
    info.textContent = arr.map(a => a.msg).join(" · ");
  }
  function selectResultItem(result) {
    if (!result) return;
    const id = result.item ? result.item.id : (result.item_ids && result.item_ids[0]);
    if (!id) return;
    const item = state.items.find(x => x.id === id);
    if (item) selectItem(item);
  }

  // ── 导入 ───────────────────────────────────────────────
  function importDialog() { $("#file-input").click(); }
  async function uploadFile(file) {
    const fd = new FormData();
    fd.append("file", file);
    toast(`导入中: ${file.name}`);
    try {
      const j = await api("/api/import", { method: "POST", body: fd });
      trackTask(j.task_id, (result) => selectResultItem(result));
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
  async function doUrlOpen() {
    const raw = $("#bb-url").value.trim();
    const urls = raw.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    if (!urls.length) return toast("请输入至少一个视频链接");
    hideModal("#modal-bilibili");
    toast(`解析 ${urls.length} 个链接…`);
    try {
      const j = await api("/api/url/open", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ urls }) });
      const results = j.results || [];
      const firstOk = results.find(r => r.ok);
      if (firstOk && firstOk.video_proxy_url) {
        // 立即用代理流播放第一个视频预览
        const v = $("#video-preview");
        $("#video-panel").classList.remove("no-video");
        v.src = firstOk.video_proxy_url;
        v.load(); v.play().catch(() => {});
        $("#dur-info").textContent = "获取中…";
      }
      let ok = 0, fail = 0;
      results.forEach(r => {
        if (r.ok) { ok++; trackTask(r.task_id, (res) => selectResultItem(res)); }
        else { fail++; toast(`解析失败: ${r.url} — ${r.error}`, 6000); }
      });
      toast(`已提交 ${ok} 个链接，${fail} 个失败`);
    } catch (e) { toast("URL 导入失败: " + e.message, 6000); }
  }

  // ── 转写 ───────────────────────────────────────────────
  function openTranscribeModal() {
    if (!needItem()) return;
    const segs = segsFor(state.currentItem.id).filter(s => !s.text.trim());
    if (!segs.length) return toast("片段列表为空或都已填写文本");
    showModal("#modal-transcribe");
  }
  async function doTranscribe() {
    if (!state.currentItem) return;
    const segs = segsFor(state.currentItem.id);
    const todo = segs.map((s, i) => ({ ...s, idx: i })).filter(x => !x.text.trim());
    if (!todo.length) { hideModal("#modal-transcribe"); return toast("没有需要转写的片段"); }
    const model = $("#tr-model").value;
    hideModal("#modal-transcribe");
    toast(`转写 ${todo.length} 段（${model}）…`);
    try {
      const j = await api("/api/transcribe", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_id: state.currentItem.id,
          segments: todo.map(s => ({ start: s.start, end: s.end })), model }) });
      trackTask(j.task_id, (result) => {
        (result.texts || []).forEach((text, k) => { segs[todo[k].idx].text = text; });
        scheduleSaveProject();
        renderSegments();
        toast("转写完成，请人工校对文本");
      });
    } catch (e) { toast("转写启动失败: " + e.message); }
  }

  // ── 训练集导出 ─────────────────────────────────────────
  function openDatasetModal() {
    if (!needItem()) return;
    if (!segsFor(state.currentItem.id).length) return toast("片段列表为空");
    showModal("#modal-dataset");
  }
  async function doDatasetExport() {
    if (!state.currentItem) return;
    const segs = segsFor(state.currentItem.id);
    hideModal("#modal-dataset");
    toast("导出训练集…");
    try {
      const j = await api("/api/dataset/export", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          item_id: state.currentItem.id,
          speaker: $("#ds-speaker").value.trim() || "speaker",
          language: $("#ds-language").value,
          out_dir: $("#ds-outdir").value.trim() || undefined,
          segments: segs.map(s => ({ start: s.start, end: s.end, text: s.text,
            language: s.language, speaker: (charById(s.characterId) || {}).name || "" })),
        }) });
      trackTask(j.task_id, (result) => {
        const skipped = (result.skipped || []).map(s => `  - [跳过] ${s.reason}（${fmtT(s.seg.start)}~${fmtT(s.seg.end)}）`).join("\n") || "  （无跳过）";
        showResult("训练集导出完成",
          `输出目录：${result.out_dir}\n成功片段：${result.count}\n\n${skipped}\n\nlist.txt：${result.list_file}`,
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
      "pool": openPool,
      "identify-speakers": doIdentifySpeakers,
      "export-selection": openExportModal,
      "denoise": doDenoise,
      "separate": doSeparate,
      "trim": doTrim,
      "transcribe": openTranscribeModal,
      "dataset-export": openDatasetModal,
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
          if (state.currentItem) {
            const segs = segsFor(state.currentItem.id);
            if (state.selectedSegs.size) {
              segs.splice(0, segs.length, ...segs.filter(s => !state.selectedSegs.has(s.id)));
              state.selectedSegs = new Set();
              scheduleSaveProject(); renderSegments();
            } else if (focusedSeg != null) { deleteSegment(Number(focusedSeg)); setSegFocus(null); }
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
        segsFor(state.currentItem.id).length = 0;
        state.selectedSegs = new Set();
        scheduleSaveProject(); renderSegments();
      }
    });
    $("#btn-transcribe").addEventListener("click", openTranscribeModal);
    $("#btn-pool").addEventListener("click", openPool);
    $("#btn-identify-speakers").addEventListener("click", doIdentifySpeakers);
    $("#pool-back").addEventListener("click", closePool);
    $("#pool-close").addEventListener("click", closePool);
    $("#pool-new").addEventListener("click", createPoolCharacter);
    $("#pool-merge").addEventListener("click", mergePoolSelected);
    $("#pool-identify").addEventListener("click", doIdentifySpeakers);

    $("#bb-open").addEventListener("click", doUrlOpen);
    $("#tr-start").addEventListener("click", doTranscribe);
    $("#ds-start").addEventListener("click", doDatasetExport);
    $("#ex-start").addEventListener("click", doExportSelection);
    // 视频 ↔ 音频双向联动
    const vp = $("#video-preview");
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

  // ── 启动 ───────────────────────────────────────────────
  setupMenus();
  setupShortcuts();
  setupDrop();
  bindUI();
  initWorkspace();
  setupMMSeek();
  window.addEventListener("beforeunload", () => { if (state.projectDirty) saveProjectNow(); });
  boot();

  // 调试/自动化钩子
  window.__vc = { state, selectItem, renderSegments, WaveSurfer, Timeline, Regions, Minimap,
    markForward, unmarkLast, clearMultiRegions,
    loadProject, saveProjectNow, openPool, closePool, renderPool, doIdentifySpeakers, newSegment,
    workspace: { layout, applyLayout, saveLayout, resetLayout, togglePanel, swapPanels, PANELS } };
})();
