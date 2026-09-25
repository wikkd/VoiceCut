// VoiceCut 前端 — 波形与播放（素材选择 · 波形/总览条 · 播放控制 · 选区与多选 · 视频联动 · 缩放）
// 由 createWaveform(ctx) 创建；ctx 注入 util + state + 主流程依赖（项目存取/素材列表/渲染/字幕），
// 避免与主流程模块形成循环 import。
export function createWaveform(ctx) {
  const { $, api, fmtT, fmtDur, fmtSel, clampN, SEEK_STEP, toast, state,
          WaveSurfer, Timeline, Regions, Minimap,
          renderMediaList, loadProject, saveProjectNow, savePoolNow,
          renderSegments, subtitles, auditionFocus } = ctx;

  // ── 素材选择 / 波形加载 ────────────────────────────────
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
    subtitles.renderSubs();
    if (item.subs_url) {
      try {
        const sj = await api(item.subs_url);
        state.subs = sj.subs || [];
      } catch (e) { state.subs = []; }
      subtitles.renderSubs();
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
      subtitles.updateCurrentSub(t);
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
  async function playSeqItem() {
    const m = state.auditionSeq && state.auditionSeq[state.auditionIdx];
    if (!m) { state.auditionSeq = null; return; }
    // 序列可跨素材（角色池连续试听）：先切素材再定位播放
    if (m.item && state.currentItem !== m.item) await selectItem(m.item);
    if (!state.ws) { state.auditionSeq = null; return; }
    state.ws.setTime(m.start);
    state.ws.play();
    state.auditioning = { start: m.start, end: m.end };
    if (m.segId && auditionFocus) auditionFocus(m.itemId, m.segId);
  }
  // 外部入口：按给定顺序连续试听（角色池 = 单一角色的全部片段）
  async function playSequence(seq, idx = 0) {
    if (!seq || !seq.length) return;
    state.auditionSeq = seq;
    state.auditionIdx = Math.max(0, Math.min(idx, seq.length - 1));
    await playSeqItem();
  }
  // 循环/试听到位检测的防重入守卫：timeupdate 可能在 seek 生效前连发旧位置，
  // 不设防会反复 setTime/pause，听感即"一帧一暂停一开始"的抖动。
  let lastWrap = null;   // 上次已回卷的选区对象（引用比较，重选/nudge 换新对象即重新武装）
  function loopCheck(t) {
    const sel = state.selection;
    if (!(state.loop && sel && sel.end - sel.start > 0.02)) return;
    if (t >= sel.end - 0.03) {
      if (lastWrap === sel) return;   // 同一选区已回卷，等播放头真正离开触发带
      lastWrap = sel;
      state.auditioning = null;
      state.ws.setTime(sel.start);
    } else if (t < sel.end - 0.3 || t <= sel.start + 0.05) {
      lastWrap = null;                // 已离开触发带或确认回卷到位，允许下一次回卷
    }
  }
  function auditionCheck(t) {
    const a = state.auditioning;
    if (!a || a._handled) return;
    if (t < a.end - 0.02) return;
    a._handled = true;   // 本段只处理一次，防 seek 未生效期间连跳/反复停
    if (state.auditionSeq && state.auditionIdx + 1 < state.auditionSeq.length) {
      state.auditionIdx++;
      playSeqItem();
    } else {
      state.ws.pause();
      state.auditioning = null;
      state.auditionSeq = null;
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
  let lastAudioSync = 0;   // 最近一次"音频→视频"同步时刻；其后的反向同步在窗口内一律抑制
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
      lastAudioSync = now;   // 抑制本次视频 seek 的 seeked 事件反向回写音频
      v.currentTime = t;
    }
  }

  // 视频 → 音频/波形联动：拖动视频进度条 / 点击播放暂停时同步波形
  let videoSeekByAudio = false;   // 标记当前视频 seek 是否由音频同步触发
  function videoToAudioSync() {
    if (!state.ws) return;
    const v = $("#video-preview");
    if (!v || !v.src) return;
    // 双向同步互斥：刚由音频驱动过视频 seek 就不再反向回写，
    // 否则两边阈值带(0.05~0.15s)交界处会互相刷 seek，播放反复中断抖动。
    if (performance.now() - lastAudioSync < 250) return;
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

  // ── DOM 事件（波形区 / 视频区 / 滚轮缩放） ─────────────
  // 视频 ↔ 音频双向联动
  function bindVideoPreview() {
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
  }
  // 屏蔽 Ctrl+滚轮 页面缩放，改为时间轴缩放
  function bindZoomWheel() {
    window.addEventListener("wheel", (e) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      if (!state.ws) return;
      if (e.deltaY < 0) zoomIn(); else zoomOut();
    }, { passive: false });
  }
  // 波形区域内右键：不弹浏览器菜单，始终取消选区/本次拖拽
  function bindWaveBox() {
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

  return { selectItem, togglePlay, toggleLoop, playSelection, playSequence,
           updatePlayUI, updateTransport, updateSelUI,
           setupMMSeek, seekBy, adjVolume,
           markForward, unmarkLast, clearMultiRegions, clearSelection,
           zoomSet, zoomIn, zoomOut, nudgeSelection,
           bindVideoPreview, bindZoomWheel, bindWaveBox };
}
