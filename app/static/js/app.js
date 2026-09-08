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
    selectionCreatedAt: 0,     // 最近一次创建选区的时间(ms)，用于右键快速取消
    dragRegion: null,          // 正在拖拽（未松手）的选区
    bootErr: null,
  };

  const SEG_MIN = 1.0, SEG_MAX = 15.0;
  const SEL_CANCEL_MS = 3000;   // 左键选出选区后，此毫秒内右键可取消

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
                        instrumental: "伴奏", trimmed: "去静音", bilibili: "B站" };
      li.innerHTML = `<div class="m-name">${esc(item.name)}</div>
        <div class="m-meta"><span class="m-badge">${kindMap[item.kind] || item.kind}</span>
        <span>${fmtDur(item.duration)}</span></div>`;
      li.addEventListener("click", () => selectItem(item));
      ul.appendChild(li);
    });
  }
  function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

  async function refreshItems() {
    try { state.items = await api("/api/items"); renderMediaList(); }
    catch (e) { /* ignore */ }
  }

  // ── 选择素材 / 波形加载 ────────────────────────────────
  async function selectItem(item) {
    state.currentItem = item;
    renderMediaList();

    // 视频预览
    const vp = $("#video-panel"), v = $("#video-preview");
    if (item.video_url) {
      vp.classList.remove("hidden");
      if (v.src !== location.origin + item.video_url) v.src = item.video_url;
      v.load();
    } else {
      vp.classList.add("hidden");
      v.removeAttribute("src");
    }

    let peaks = item.peaks;
    if (!peaks) {
      try { const pj = await api(item.peaks_url); peaks = pj.peaks; item.peaks = peaks; } catch (e) { peaks = []; }
    }
    loadWavesurfer(item, peaks);
    renderSegments();
    updateTransport();
  }

  function loadWavesurfer(item, peaks) {
    if (state.ws) { try { state.ws.destroy(); } catch (e) {} state.ws = null; }
    state.selection = null; state.selectionRegion = null;
    state.selectionCreatedAt = 0; state.dragRegion = null;
    $("#empty-state").classList.add("hidden");

    const timeline = Timeline.create({ container: "#timeline", height: 24 });
    state.regions = Regions.create({ color: "rgba(108,156,255,0.25)" });
    const minimap = Minimap.create({
      container: "#minimap", height: 44,
      waveColor: "#3a3a55", progressColor: "#6c9cff",
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
      if (state.selectionRegion && state.selectionRegion !== region) {
        try { state.selectionRegion.remove(); } catch (e) {}
      }
      state.selectionRegion = region;
      state.selectionCreatedAt = Date.now();
      state.selection = { start: region.start, end: region.end };
      updateSelUI();
    });
    state.regions.on("region-updated", (region) => {
      if (region === state.selectionRegion) {
        state.selection = { start: region.start, end: region.end };
        updateSelUI();
      }
    });

    ws.on("play", () => { state.playing = true; updatePlayUI(); videoPlay(); });
    ws.on("pause", () => { state.playing = false; updatePlayUI(); videoPause(); });
    ws.on("finish", () => { state.playing = false; updatePlayUI(); });
    ws.on("timeupdate", (t) => {
      $("#cur-time").textContent = fmtT(t);
      videoSync(t);
      loopCheck(t);
      auditionCheck(t);
    });
    ws.on("ready", () => updateTransport());
    ws.on("error", (e) => toast("播放错误: " + (e && e.message ? e.message : e)));
  }

  // ── 播放控制 ───────────────────────────────────────────
  function togglePlay() { if (state.ws) { if (state.playing) state.ws.pause(); else state.ws.play(); } }
  function updatePlayUI() {
    const icon = state.playing ? "⏸" : "▶";
    $("#btn-play").textContent = icon;
    $("#btn-play2").textContent = icon;
    $("#btn-loop").classList.toggle("primary", state.loop);
    $("#btn-loop").textContent = state.loop ? "🔁 循环中" : "🔁 循环";
  }
  function toggleLoop() { state.loop = !state.loop; updatePlayUI(); }
  function playSelection() {
    if (!state.ws || !state.selection) return toast("请先拖拽出选区");
    state.ws.setTime(state.selection.start);
    state.ws.play();
  }
  function loopCheck(t) {
    if (state.loop && state.selection && state.selection.end - state.selection.start > 0.02
        && t >= state.selection.end - 0.03) {
      state.ws.setTime(state.selection.start);
    }
  }
  function auditionCheck(t) {
    if (state.auditioning && t >= state.auditioning.end - 0.02) {
      state.ws.pause();
      state.auditioning = null;
    }
  }
  function updateTransport() {
    if (!state.currentItem) { $("#dur-info").textContent = "—"; return; }
    $("#dur-info").textContent = fmtDur(state.currentItem.duration);
  }
  function updateSelUI() { $("#sel-info").textContent = fmtSel(state.selection); }

  function clearSelection() {
    if (state.selectionRegion) { try { state.selectionRegion.remove(); } catch (e) {} }
    state.selectionRegion = null;
    state.selection = null;
    state.selectionCreatedAt = 0;
    state.dragRegion = null;
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
    if (Math.abs(v.currentTime - t) > 0.05) v.currentTime = t;
  }

  // 缩放
  function zoomSet(lv) {
    if (!state.ws) return;
    state.zoomLevel = Math.max(0, Math.min(200, lv));
    state.ws.zoom(state.zoomLevel);
  }
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
      const tr = document.createElement("tr");
      tr.className = "seg-row" + (cls ? " " + cls : "");
      tr.dataset.i = i;
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
        <td><input type="text" class="seg-speaker" data-i="${i}" value="${esc(seg.speaker || "speaker")}"></td>
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
    segs.push({ start: state.selection.start, end: state.selection.end, text: "", language: "JP", speaker: "speaker" });
    renderSegments();
  }
  function deleteSegment(i) {
    const segs = segsFor(state.currentItem.id);
    segs.splice(i, 1); renderSegments();
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
  let focusedSeg = null;
  function setSegFocus(tr) {
    $$(".seg-row").forEach(r => r.style.outline = "");
    if (tr) { tr.style.outline = "1px solid var(--accent)"; focusedSeg = tr.dataset.i; }
    else focusedSeg = null;
  }

  $("#seg-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const i = Number(btn.dataset.i);
    const segs = segsFor(state.currentItem.id);
    if (btn.classList.contains("seg-aud")) auditionSegment(segs[i]);
    else if (btn.classList.contains("seg-jump")) jumpToSegment(segs[i]);
    else if (btn.classList.contains("seg-del")) deleteSegment(i);
  });
  $("#seg-tbody").addEventListener("input", (e) => {
    const i = Number(e.target.dataset.i);
    const segs = segsFor(state.currentItem.id);
    if (!segs[i]) return;
    if (e.target.classList.contains("seg-text")) segs[i].text = e.target.value;
    if (e.target.classList.contains("seg-speaker")) segs[i].speaker = e.target.value;
    renderSegments();   // 更新状态徽标
  });
  $("#seg-tbody").addEventListener("change", (e) => {
    const i = Number(e.target.dataset.i);
    const segs = segsFor(state.currentItem.id);
    if (e.target.classList.contains("seg-lang") && segs[i]) segs[i].language = e.target.value;
  });
  $("#seg-tbody").addEventListener("click", (e) => {
    const tr = e.target.closest("tr.seg-row");
    if (tr) setSegFocus(tr);
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
  async function doBilibiliOpen() {
    const url = $("#bb-url").value.trim();
    if (!url) return toast("请输入 B 站链接");
    hideModal("#modal-bilibili");
    toast("解析 B 站视频…");
    try {
      const j = await api("/api/bilibili/open", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
      // 立即用代理流播放视频
      const vp = $("#video-panel"), v = $("#video-preview");
      vp.classList.remove("hidden");
      v.src = j.video_proxy_url;
      v.load(); v.play().catch(() => {});
      $("#dur-info").textContent = "获取中…";
      trackTask(j.task_id, (r) => selectResultItem(r));
    } catch (e) { toast("B 站打开失败: " + e.message, 6000); }
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
            language: s.language, speaker: s.speaker })),
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
      "export-selection": openExportModal,
      "denoise": doDenoise,
      "separate": doSeparate,
      "trim": doTrim,
      "transcribe": openTranscribeModal,
      "dataset-export": openDatasetModal,
      "validate": renderSegments,
      "fit": () => zoomSet(0),
      "zoom-sel": () => { if (state.selection) { zoomSet(0); state.ws.setTime(state.selection.start); } },
      "toggle-minimap": () => $("#minimap").classList.toggle("hidden"),
      "help": () => showModal("#modal-help"),
    };
    document.querySelectorAll("[data-act]").forEach((b) => {
      b.addEventListener("click", () => { const f = actions[b.dataset.act]; if (f) f(); });
    });
  }

  // ── 快捷键 ─────────────────────────────────────────────
  function setupShortcuts() {
    window.addEventListener("keydown", (e) => {
      const tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (e.ctrlKey && (e.key === "o" || e.key === "O")) { e.preventDefault(); importDialog(); return; }
      switch (e.key) {
        case " ": e.preventDefault(); togglePlay(); break;
        case "l": case "L": toggleLoop(); break;
        case "e": case "E": openExportModal(); break;
        case "n": case "N": doDenoise(); break;
        case "v": case "V": doSeparate(); break;
        case "ArrowRight": e.preventDefault(); nudgeSelection(0.05, e.shiftKey ? "move" : "end"); break;
        case "ArrowLeft": e.preventDefault(); nudgeSelection(-0.05, e.shiftKey ? "move" : "end"); break;
        case "Delete":
          if (focusedSeg != null && state.currentItem) { deleteSegment(Number(focusedSeg)); setSegFocus(null); }
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
    $("#btn-play").addEventListener("click", togglePlay);
    $("#btn-play2").addEventListener("click", togglePlay);
    $("#btn-prev").addEventListener("click", () => state.ws && state.ws.setTime(0));
    $("#btn-next").addEventListener("click", () => state.ws && state.ws.setTime(state.currentItem ? state.currentItem.duration : 0));
    $("#btn-loop").addEventListener("click", toggleLoop);
    $("#btn-play-selection").addEventListener("click", playSelection);
    $("#btn-export-selection").addEventListener("click", openExportModal);
    $("#btn-zoom-in").addEventListener("click", () => zoomSet(state.zoomLevel <= 0 ? 1 : state.zoomLevel * 1.5));
    $("#btn-zoom-out").addEventListener("click", () => zoomSet(state.zoomLevel <= 1 ? 0 : state.zoomLevel / 1.5));
    $("#btn-fit").addEventListener("click", () => zoomSet(0));
    $("#minimap-toggle").addEventListener("change", (e) => $("#minimap").classList.toggle("hidden", !e.target.checked));
    $("#btn-add-seg").addEventListener("click", addSegmentFromSelection);
    $("#btn-clear-segs").addEventListener("click", () => {
      if (!state.currentItem) return;
      if (confirm("清空当前素材的全部片段？")) { segsFor(state.currentItem.id).length = 0; renderSegments(); }
    });
    $("#btn-transcribe").addEventListener("click", openTranscribeModal);
    $("#btn-dataset-export").addEventListener("click", openDatasetModal);

    $("#bb-open").addEventListener("click", doBilibiliOpen);
    $("#tr-start").addEventListener("click", doTranscribe);
    $("#ds-start").addEventListener("click", doDatasetExport);
    $("#ex-start").addEventListener("click", doExportSelection);

    $$(".modal-mask").forEach((mask) => mask.addEventListener("click", (e) => {
      if (e.target === mask || e.target.closest("[data-close]")) mask.classList.add("hidden");
    }));
    $("#bb-url").addEventListener("keydown", (e) => { if (e.key === "Enter") doBilibiliOpen(); });
    // 左键选区后短时间内右键可取消（含拖拽中右键取消本次拖拽）
    $("#waveform").addEventListener("contextmenu", (e) => {
      if (state.dragRegion) {
        e.preventDefault();
        try { state.dragRegion.remove(); } catch (err) {}
        if (state.selectionRegion) { try { state.selectionRegion.remove(); } catch (err) {} }
        state.dragRegion = null;
        state.selectionRegion = null;
        state.selection = null;
        state.selectionCreatedAt = 0;
        updateSelUI();
        toast("已取消选区");
        return;
      }
      if (!state.selection || !state.selectionRegion) return;
      if (!state.selectionCreatedAt || Date.now() - state.selectionCreatedAt > SEL_CANCEL_MS) return;
      e.preventDefault();
      clearSelection();
      toast("已取消选区");
    });
  }

  // ── 启动 ───────────────────────────────────────────────
  setupMenus();
  setupShortcuts();
  setupDrop();
  bindUI();
  boot();

  // 调试/自动化钩子
  window.__vc = { state, selectItem, renderSegments, WaveSurfer, Timeline, Regions, Minimap };
})();
