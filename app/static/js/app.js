import WaveSurfer from "/static/vendor/wavesurfer.esm.js";
import Timeline from "/static/vendor/plugins/timeline.esm.js";
import Regions from "/static/vendor/plugins/regions.esm.js";
import Minimap from "/static/vendor/plugins/minimap.esm.js";

import { $, $$, esc, shortName, fmtT, fmtSel, fmtDur, api, clampN,
         SEG_MIN, SEG_MAX, SEEK_STEP, SEEK_FAST, VOL_STEP, CHAR_PALETTE } from "/static/js/util.js";
import { state, toast, toastBusy, layout, applyLayout, saveLayout, resetLayout,
         togglePanel, swapPanels, initWorkspace, PANELS } from "/static/js/state.js";
import { createTraining } from "/static/js/modules/training.js";
import { createSubtitles } from "/static/js/modules/subtitles.js";
import { createIo } from "/static/js/modules/io.js";
import { createWaveform } from "/static/js/modules/waveform.js";
import { createSegments } from "/static/js/modules/segments.js";
import { createPool } from "/static/js/modules/pool.js";
import { createTasks } from "/static/js/modules/tasks.js";
import { createStore } from "/static/js/modules/store.js";

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
    if (state.dirtyItems.size) await store.saveProjectNow();
    if (state.poolDirty) await store.savePoolNow();
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
    pool.renderAutoAnalyzeBtn();
    state.segmentsByItem = new Map();
    state.speakerSegsByItem = new Map();
    renderMediaList();
    await loadAllItemData();
    if (state.items.length) await waveform.selectItem(state.items[0]);
    else clearWorkbench();
    tasks.attachActiveTasks();
  }

  async function loadAllItemData() {
    const items = state.items || [];
    await Promise.all(items.map(item => store.loadProject(item, true)));
  }

  function clearWorkbench() {
    state.currentItem = null;
    state.subs = []; state.currentSubIdx = -1; state.auditioning = null; state.playing = false;
    if (state.ws) { try { state.ws.destroy(); } catch (e) {} state.ws = null; }
    $("#video-panel").classList.add("no-video");
    const v = $("#video-preview"); if (v) v.removeAttribute("src");
    $("#empty-state").classList.remove("hidden");
    $("#sub-current").textContent = "—";
    segments.renderSegments(); subtitles.renderSubs(); pool.renderPool();
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
      if (!e.target.dataset.undoed) { e.target.dataset.undoed = "1"; store.pushUndo(); }
      segs[i].text = e.target.value; store.scheduleSaveProject(itemId);
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
    if (!e.target.classList.contains("seg-text")) store.pushUndo();
    if (e.target.classList.contains("seg-lang")) { segs[i].language = e.target.value; store.scheduleSaveProject(itemId); }
    if (e.target.classList.contains("seg-speaker")) {
      segs[i].characterId = e.target.value || null;
      store.scheduleSaveProject(itemId); segments.renderSegments();
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
      b.addEventListener("click", () => { hideRedirectMenu(); pool.reassignSegments(segIds, val); });
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
      "pool": () => pool.openPool(),
      "identify-speakers": () => pool.doIdentifySpeakers(),
      "export-selection": () => io.openExportModal(),
      "denoise": () => io.doDenoise(),
      "separate": () => io.doSeparate(),
      "trim": () => io.doTrim(),
      "transcribe": () => io.openTranscribeModal(),
      "dataset-export": () => io.openDatasetModal(),
      "autosplit": () => showModal("#modal-autosplit"),
      "undo": store.undo,
      "redo": store.redo,
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
        if (e.shiftKey) store.redo(); else store.undo();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === "y" || e.key === "Y")) { e.preventDefault(); store.redo(); return; }

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
            store.pushUndo();
            (state.items || []).forEach(item => {
              const segs = segments.segsFor(item.id);
              const before = segs.length;
              const kept = segs.filter(s => !state.selectedSegs.has(s.id));
              if (kept.length !== before) { segs.splice(0, segs.length, ...kept); store.scheduleSaveProject(item.id); }
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
    $("#btn-cancel-task").addEventListener("click", () => tasks.cancelAllTasks());
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
        store.pushUndo();
        segments.segsFor(state.currentItem.id).length = 0;
        state.selectedSegs = new Set();
        store.scheduleSaveProject(); segments.renderSegments();
      }
    });
    $("#btn-transcribe").addEventListener("click", () => io.openTranscribeModal());
    $("#btn-pool").addEventListener("click", () => pool.openPool());
    $("#btn-identify-speakers").addEventListener("click", () => pool.doIdentifySpeakers());
    $("#btn-auto-analyze").addEventListener("click", () => pool.toggleAutoAnalyze());
    $("#btn-auto-analyze2").addEventListener("click", () => pool.toggleAutoAnalyze());
    $("#pool-back").addEventListener("click", () => pool.closePool());
    $("#pool-close").addEventListener("click", () => pool.closePool());
    $("#pool-new").addEventListener("click", () => pool.createPoolCharacter());
    $("#pool-merge").addEventListener("click", () => pool.mergePoolSelected());
    $("#pool-identify").addEventListener("click", () => pool.doIdentifySpeakers());

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
  let store = null;      // 数据层模块实例（启动区由 createStore 创建，最先）
  let tasks = null;      // 任务跟踪模块实例（启动区由 createTasks 创建）
  let pool = null;       // 角色池模块实例（启动区由 createPool 创建）
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
    const p2 = $("#btn-pool2"); if (p2) p2.addEventListener("click", () => pool.openPool());
    const rf = $("#btn-train-refresh"); if (rf) rf.addEventListener("click", () => training && training.loadTraining(true));
    setPage(currentPage);
  }

  // ── 启动 ───────────────────────────────────────────────
  store = createStore({ api, toast, state, CHAR_PALETTE,
    segsFor: (id) => segments.segsFor(id),
    renderSegments: () => segments.renderSegments(),
    renderPool: () => pool.renderPool() });
  tasks = createTasks({ $, api, toast, toastBusy, state,
    renderMediaList, refreshItems, loadAllItemData,
    renderPool: () => pool.renderPool(), renderSegments: () => segments.renderSegments(),
    renderSubs: () => subtitles.renderSubs(),
    selectItem: (item) => waveform.selectItem(item) });
  segments = createSegments({ $, esc, shortName, fmtT, fmtDur, fmtSel, SEG_MIN, SEG_MAX,
    state, toast, charById: store.charById, newSegment: store.newSegment, pushUndo: store.pushUndo, scheduleSaveProject: store.scheduleSaveProject, setPage,
    closePool: () => pool.closePool(),
    selectItem: (item) => waveform.selectItem(item) });
  pool = createPool({ $, $$, esc, shortName, fmtT, toast, state, api, trackTask: tasks.trackTask,
    needItem, charById: store.charById, uid: store.uid, paletteNext: store.paletteNext, pushUndo: store.pushUndo,
    scheduleSaveProject: store.scheduleSaveProject, scheduleSavePool: store.scheduleSavePool, loadAllItemData, segments, subtitles });
  subtitles = createSubtitles({ $, $$, fmtT, esc, api, toast, state, trackTask: tasks.trackTask,
    speakerLabelAt: store.speakerLabelAt, segsFor: segments.segsFor, newSegment: store.newSegment, pushUndo: store.pushUndo, scheduleSaveProject: store.scheduleSaveProject,
    renderSegments: segments.renderSegments });
  training = createTraining({ $, esc, shortName, fmtDur, api, state, toast, trackTask: tasks.trackTask });
  io = createIo({ $, api, state, toast, trackTask: tasks.trackTask, needItem,
    selectResultItem: tasks.selectResultItem, autoAnalyzeDone: tasks.autoAnalyzeDone,
    showModal, hideModal, showResult, saveProjectNow: store.saveProjectNow, pushUndo: store.pushUndo, loadProject: store.loadProject,
    renderSegments: segments.renderSegments, segsFor: segments.segsFor, charById: store.charById, scheduleSaveProject: store.scheduleSaveProject,
    fmtSel, fmtT });
  waveform = createWaveform({ $, api, fmtT, fmtDur, fmtSel, clampN, SEEK_STEP, toast, state,
    WaveSurfer, Timeline, Regions, Minimap,
    renderMediaList, loadProject: store.loadProject, saveProjectNow: store.saveProjectNow, savePoolNow: store.savePoolNow, renderSegments: segments.renderSegments,
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
  pool.renderAutoAnalyzeBtn();
  boot();

  // 调试/自动化钩子
  window.__vc = { state, selectItem: waveform.selectItem, selectProject,
    renderSegments: segments.renderSegments,
    WaveSurfer, Timeline, Regions, Minimap,
    markForward: waveform.markForward, unmarkLast: waveform.unmarkLast,
    clearMultiRegions: waveform.clearMultiRegions,
    loadProject: store.loadProject, saveProjectNow: store.saveProjectNow, savePoolNow: store.savePoolNow,
    openPool: pool.openPool, closePool: pool.closePool, renderPool: pool.renderPool,
    undo: store.undo, redo: store.redo, pushUndo: store.pushUndo, doAutosplit: io.doAutosplit, uploadFile: io.uploadFile,
    createProject, renameProject, deleteProject,
    doIdentifySpeakers: pool.doIdentifySpeakers, newSegment: store.newSegment,
    toggleAutoAnalyze: pool.toggleAutoAnalyze,
    autoAnalyzeDone: tasks.autoAnalyzeDone, attachActiveTasks: tasks.attachActiveTasks,
    setPage, loadTraining: training.loadTraining, startTrain: training.startTrain,
    doInfer: training.doInfer, train: training.train,
    workspace: { layout, applyLayout, saveLayout, resetLayout, togglePanel, swapPanels, PANELS } };
})();
