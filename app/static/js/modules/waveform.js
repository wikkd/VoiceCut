// VoiceCut 前端 — 波形与播放（素材选择 · 波形/总览条 · 播放控制 · 选区与多选 · 视频联动 · 缩放）
// 由 createWaveform(ctx) 创建；ctx 注入 util + state + 主流程依赖（项目存取/素材列表/渲染/字幕），
// 避免与主流程模块形成循环 import。
export function createWaveform(ctx) {
  const { $, api, fmtT, fmtDur, fmtSel, clampN, SEEK_STEP, toast, state,
          WaveSurfer, Timeline, Regions, Minimap,
          renderMediaList, loadProject, saveProjectNow, savePoolNow,
          renderSegments, subtitles, auditionFocus } = ctx;

  // ── Region 创建守卫 ──────────────────────────────────
  // 程序创建的 region（时间轴拖选 / Ctrl 多选 / 键盘快记 / 字幕高亮）统一走 progAddRegion，
  // 创建期间置 state._vcProgRegion=true；"波形拖拽选区"的 region-created 分支据此跳过程序创建。
  function progAddRegion(opts) {
    state._vcProgRegion = true;
    try { return state.regions.addRegion(opts); } finally { state._vcProgRegion = false; }
  }
  // 记录波形拖选按下瞬间的修饰键：波形 Ctrl+拖选 = 追加多选标记（与时间轴 Ctrl+拖选一致）
  let lastDragCtrl = false;
  window.addEventListener("pointerdown", (e) => { lastDragCtrl = !!(e.ctrlKey || e.metaKey); }, true);
  let dragSelUnbind = null;   // enableDragSelection 的解绑函数（换素材时清理）

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
    state.multiRegions = [];
    state.auditionSeq = null; state.auditionIdx = 0;
    state.activeSeg = null;
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
      const tl = $("#timeline [part='timeline']");
      if (!tl || !ws.getWrapper()) return;
      tl.style.width = ws.getWrapper().scrollWidth + "px";
      tl.style.transform = "translateX(" + (-ws.getScroll()) + "px)";
    };
    ws.on("redraw", syncTimeline);
    ws.on("scroll", syncTimeline);

    // 选区显示：regions 插件仅用于"渲染"选区/多选标记（全部程序创建，
    // drag/resize 关闭）；创建选区的交互入口：波形拖拽 + 时间轴拖拽（bindTimelineSelection）
    state.regions.on("region-removed", (region) => {
      const i = state.multiRegions.findIndex((m) => m.region === region);
      if (i >= 0) { state.multiRegions.splice(i, 1); updateSelUI(); }
    });

    // 恢复"在波形上直接拖拽选区"的交互（此前 042f674 移除，现应需求回归）：
    // enableDragSelection 的参数原样传给 region 构造器，drag/resize 全关 ——
    // 拖出选区后即固定，不能再拖动/缩放。松开鼠标时插件 saveRegion → region-created。
    state.regions.on("region-created", (region) => {
      if (state._vcProgRegion) return;   // 程序创建的时间轴选区/多选标记/字幕高亮不走此分支
      if (lastDragCtrl) {
        // 波形 Ctrl+拖选：追加橙色多选标记（与时间轴 Ctrl+拖选一致）
        state.multiRegions.push({ start: region.start, end: region.end, region });
      } else {
        // 普通波形拖选：替换为单一蓝色选区
        clearMultiRegions();
        if (state.selectionRegion && state.selectionRegion !== region) {
          try { state.selectionRegion.remove(); } catch (e) {}
        }
      }
      state.selectionRegion = region;
      state.selection = { start: region.start, end: region.end };
      updateSelUI();
    });
    if (dragSelUnbind) { try { dragSelUnbind(); } catch (e) {} }
    dragSelUnbind = state.regions.enableDragSelection({ color: SEL_COLOR, drag: false, resize: true, minLength: 0.05 });

    // 鼠标拖选区两端 ↔ 手柄调整范围：把 region 的最新 start/end 同步回 state.selection
    const syncSelFromRegion = (region) => {
      if (region === state.selectionRegion && state.selection) {
        state.selection = { start: region.start, end: region.end };
        updateSelUI();
      }
    };
    state.regions.on("region-update", syncSelFromRegion);    // 拉伸过程中实时跟随
    state.regions.on("region-updated", syncSelFromRegion);   // 松开鼠标终值

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
  // 手动暂停（按钮/空格）＝终止试听序列：否则跨素材切换中 in-flight 的
  // playSeqItem 会在 selectItem 返回后把播放重新拉起，表现为"无法暂停"。
  function togglePlay() {
    if (!state.ws) return;
    if (state.playing) {
      state.auditionSeq = null;
      state.auditioning = null;
      document.querySelectorAll("#seg-tbody tr.seg-row.playing")
        .forEach((el) => el.classList.remove("playing"));
      state.ws.pause();
    } else {
      state.ws.play();
    }
  }

  // ── 片段定位 ───────────────────────────────────────────
  // 点击片段行/跳转：高亮行 + 播放头跳到片段起点 + 把工作选区设为该片段区间
  // （蓝色可拉伸选区，导出/试听/加片段直接可用）。
  async function focusSegment(item, seg) {
    if (!state.ws || !state.currentItem || state.currentItem.id !== item.id) await selectItem(item);
    if (!state.ws) return;
    state.ws.setTime(seg.start);
    state.activeSeg = { itemId: item.id, segId: seg.id };
    setSelection(seg.start, seg.end);
    renderSegments();   // 刷新行高亮（seg-jump 按钮路径不经过列表点击渲染）
  }

  function updatePlayUI() {
    const icon = state.playing ? "⏸" : "▶";
    $("#btn-play2").textContent = icon;
    $("#btn-loop").classList.toggle("primary", state.loop);
    $("#btn-loop").textContent = state.loop ? "循环中" : "循环";
  }
  function toggleLoop() { state.loop = !state.loop; updatePlayUI(); }
  function playSelection() {
    if (!state.ws || !state.selection) return toast("请先在时间轴上拖拽出选区");
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
    // 换素材切换（selectItem 耗秒级）前先停旧素材尾音；同素材时紧接 setTime+play 无感
    if (state.ws && state.ws.isPlaying()) state.ws.pause();
    const m = state.auditionSeq && state.auditionSeq[state.auditionIdx];
    if (!m) { state.auditionSeq = null; return; }
    // 序列可跨素材（角色池连续试听）：先切素材再定位播放
    if (m.item && state.currentItem !== m.item) await selectItem(m.item);
    if (!state.ws) { state.auditionSeq = null; return; }
    // await selectItem 期间用户可能已手动暂停（togglePlay 清空序列）——此时不得继续播放
    if (!state.auditionSeq || state.auditionSeq[state.auditionIdx] !== m) return;
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
    $("#dur-info").textContent = fmtDur(state.currentItem.duration || 0);
  }

  // ── 总览条播放头（M6） ──
  function updateMMCursor(t) {
    const w = document.querySelector("#minimap-wrap");
    const c = $("#mm-cursor");
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
    const w = $("#minimap-wrap");
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
    const region = progAddRegion({ start, end, color: MULTI_COLOR, drag: false, resize: false });
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
    state.multiRegions.slice().forEach((m) => {
      if (state.selectionRegion === m.region) state.selectionRegion = null;   // 被清的是多选标记则弃用引用
      try { m.region.remove(); } catch (e) {}
    });
    state.multiRegions = [];
    updateSelUI();
  }

  function clearSelection() {
    if (state.selectionRegion) { try { state.selectionRegion.remove(); } catch (e) {} }
    state.selectionRegion = null;
    state.selection = null;
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
  // 把工作选区设为指定区间（片段行点击 / 字幕「选区」按钮共用入口）：
  // 单一蓝色选区（整体不可拖走、两端可 ↔ 拉伸），替换旧选区与多选标记。
  function setSelection(start, end) {
    if (!state.ws) return;
    const wasMark = state.multiRegions.some(m => m.region === state.selectionRegion);
    clearMultiRegions();
    if (wasMark) state.selectionRegion = null;   // 旧选区是多选标记，已随 clear 移除，弃用引用
    const region = _ensureSelRegion(start, end, SEL_COLOR);
    state.selectionRegion = region;
    state.selection = { start, end };
    updateSelUI();
  }

  function nudgeSelection(delta, mode) {
    if (!state.ws || !state.selection || !state.selectionRegion) return toast("请先在波形或时间轴上拖拽出选区");
    let { start, end } = state.selection;
    const dur = state.currentItem ? state.currentItem.duration : end;
    if (mode === "move") { start = Math.max(0, Math.min(dur, start + delta)); end = Math.max(0, Math.min(dur, end + delta)); }
    else if (mode === "start") { start = Math.max(0, Math.min(end - 0.05, start + delta)); }
    else { end = Math.max(start + 0.05, Math.min(dur, end + delta)); }
    state.selectionRegion.setOptions({ start, end });  // vendored Region 无 setExtent（此前 Shift 微调静默抛错）
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
      if (state.selection && state.selectionRegion) {
        clearSelection();
        toast("已取消选区");
      }
    });
  }

  // ── 时间轴拖拽选区：创建选区的唯一交互入口（波形上不再有拖拽能力） ──
  // 左键在 #timeline 上按下并拖动 → 创建/更新选区；Ctrl+拖动 → 追加橙色
  // 多选标记；原地点击（位移 < 0.05s）= 跳转播放头；拖选中右键 = 取消本次。
  const SEL_COLOR = "rgba(108,156,255,0.25)";
  let tlDrag = null;   // {startT, moved, ctrl, region, cleared}
  function _tlTime(e) {
    const wr = state.ws.getWrapper().getBoundingClientRect();
    const dur = state.ws.getDuration() || 0;
    return clampN((e.clientX - wr.left) / (wr.width || 1) * dur, 0, dur);
  }
  function _ensureSelRegion(start, end, color) {
    if (state.selectionRegion) {
      state.selectionRegion.setOptions({ start, end, color, drag: false, resize: true });
      return state.selectionRegion;
    }
    // drag:false=整体不可拖走；resize:true=两端 ↔ 手柄可拉伸改范围（minLength 防止缩成 0）
    return progAddRegion({ start, end, color, drag: false, resize: true, minLength: 0.05 });
  }
  function bindTimelineSelection() {
    const tl = $("#timeline");
    if (!tl) return;
    tl.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || !state.ws) return;
      e.preventDefault();
      tlDrag = { startT: _tlTime(e), moved: false,
                 ctrl: e.ctrlKey || e.metaKey, region: null, cleared: false };
    });
    window.addEventListener("mousemove", (e) => {
      if (!tlDrag || !state.ws) return;
      const t = _tlTime(e);
      if (!tlDrag.moved && Math.abs(t - tlDrag.startT) < 0.05) return;
      tlDrag.moved = true;
      const a = Math.min(tlDrag.startT, t), b = Math.max(tlDrag.startT, t);
      if (tlDrag.ctrl) {
        // Ctrl 拖选：追加一个橙色标记，保留已有标记
        if (!tlDrag.region) {
          tlDrag.region = progAddRegion({ start: a, end: b, color: MULTI_COLOR, drag: false, resize: false });
        } else {
          tlDrag.region.setOptions({ start: a, end: b });
        }
        state.selectionRegion = tlDrag.region;
      } else {
        // 普通拖选：清掉旧选区与多选标记，单一蓝色选区
        if (!tlDrag.cleared) {
          tlDrag.cleared = true;
          if (state.selectionRegion) { try { state.selectionRegion.remove(); } catch (err) {} state.selectionRegion = null; }
          clearMultiRegions();
        }
        state.selectionRegion = _ensureSelRegion(a, b, SEL_COLOR);
      }
      state.selection = { start: a, end: b };
      updateSelUI();
    });
    window.addEventListener("mouseup", () => {
      if (!tlDrag) return;
      const d = tlDrag; tlDrag = null;
      if (!state.ws) return;
      if (!d.moved) { state.ws.setTime(d.startT); return; }   // 原地点击 = 跳转播放头
      if (d.ctrl && d.region) {
        state.multiRegions.push({ start: state.selection.start, end: state.selection.end, region: d.region });
      }
      updateSelUI();
    });
    tl.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      if (tlDrag) {   // 拖选中右键：取消本次拖拽
        if (tlDrag.region) { try { tlDrag.region.remove(); } catch (err) {} }
        tlDrag = null;
        toast("已取消本次拖选");
      } else if (state.selection || state.multiRegions.length) {
        clearSelection();
        toast("已取消选区");
      }
    });
  }

  return { selectItem, togglePlay, toggleLoop, playSelection, playSequence,
           focusSegment, setSelection,
           updatePlayUI, updateTransport, updateSelUI,
           setupMMSeek, seekBy, adjVolume,
           markForward, unmarkLast, clearMultiRegions, clearSelection,
           zoomSet, zoomIn, zoomOut, nudgeSelection,
           bindVideoPreview, bindZoomWheel, bindWaveBox, bindTimelineSelection };
}
