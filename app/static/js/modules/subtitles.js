// VoiceCut 前端 — 实时字幕区（SRT/ASS 加载 / Whisper 生成 / 播放高亮 / 选区·加片段）
// 由 createSubtitles(ctx) 创建；ctx 注入 util + state + 主流程依赖（分段/撤销/保存/渲染），
// 避免与主流程模块形成循环 import。
export function createSubtitles(ctx) {
  const { $, $$, fmtT, esc, api, toast, state, trackTask,
          speakerLabelAt, segsFor, newSegment, pushUndo,
          scheduleSaveProject, renderSegments } = ctx;

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
  // 字幕高亮块：与选区同色的蓝色 region，仅作视觉标记。
  // 必须拖/缩放全关 + 只保留一个（否则会堆出一堆可拖动的"假选区"）。
  let subRegion = null;
  function selectSubRange(i) {
    const s = state.subs[i];
    if (!state.ws || !s) return;
    if (subRegion) { try { subRegion.remove(); } catch (e) {} subRegion = null; }
    // 置守卫标记：程序创建的高亮块不能被波形拖选分支(region-created)当成新选区
    state._vcProgRegion = true;
    try {
      subRegion = state.regions.addRegion({ start: s.start, end: s.end, color: "rgba(108,156,255,0.25)", drag: false, resize: false });
    } finally { state._vcProgRegion = false; }
    state.ws.setTime(s.start);
  }
  function resetSubRegion() {   // 切换素材时由 waveform 调用：旧波形的 region 已随插件销毁
    subRegion = null;
  }
  function addSubToSegments(i) {
    if (!state.currentItem) return toast("请先选择素材");
    const s = state.subs[i];
    if (!s) return;
    const segs = segsFor(state.currentItem.id);
    pushUndo("字幕加片段");
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
      pushUndo("选区加片段");
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

  return { updateCurrentSub, renderSubs, selectSubRange, resetSubRegion, addSubToSegments,
           addCurrentSubToSegments, uploadSubFile, generateSubs };
}
