// VoiceCut 前端 — 输入/输出区（本地导入 · 清洗动作 · 选区导出 · 网络 URL 导入 · 转写 · 训练集导出）
// 由 createIo(ctx) 创建；ctx 注入 util + state + 主流程依赖（任务跟踪/项目存取/渲染/弹窗），
// 避免与主流程模块形成循环 import。
export function createIo(ctx) {
  const { $, api, state, toast, trackTask, needItem, selectResultItem, autoAnalyzeDone,
          showModal, hideModal, showResult, saveProjectNow, pushUndo, loadProject,
          loadAllItemData,
          renderSegments, segsFor, charById, scheduleSaveProject, fmtSel, fmtT } = ctx;

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
    pushUndo("自动切分");
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
    if (!state.selection) return toast("请先在时间轴上拖拽出选区");
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

  // ── 网络 URL 导入（多平台 / 批量） ──────────────────────
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
    pushUndo("转写回填");
    let total = 0;
    (state.items || []).forEach((item) => {
      const segs = segsFor(item.id);
      const todo = segs.map((s, i) => ({ ...s, idx: i })).filter(x => !x.text.trim() && !x.locked);
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

  // ── 清晰度打分 ─────────────────────────────────────────
  // 对齐发音：把当前素材的片段窗口收缩到实际发音区间（字幕链式时间修复）
  async function doAlignSpeech() {
    if (!needItem()) return;
    const item = state.currentItem;
    try {
      const j = await api(`/api/items/${item.id}/align-speech`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: "{}" });
      trackTask(j.task_id, async (result) => {
        pushUndo("对齐发音");   // 后端已改窗口并落盘，应用前快照使本次收缩可撤销
        await loadAllItemData();
        renderSegments();
        const changed = (result && result.changed) || 0;
        const total = (result && result.total) || 0;
        toast(changed > 0
          ? `对齐发音完成：${changed}/${total} 段窗口已收缩到语音区间（锁定片段未动）`
          : `对齐发音完成：${total} 段均无需调整`);
      });
      toast("正在检测静音并对齐片段到发音区间…");
    } catch (e) { toast("对齐发音启动失败: " + e.message, 6000); }
  }

  async function doQualityScan() {
    if (!state.currentProject) return toast("请先选择项目");
    try {
      const j = await api("/api/quality/scan", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: state.currentProject.id }) });
      trackTask(j.task_id, async (result) => {
        pushUndo("清晰度打分");   // 打分会写回 s.q，纳入撤销范围
        await loadAllItemData();
        renderSegments();
        toast(`清晰度打分完成：${result.scored} 段，平均 ${result.avg != null ? result.avg : "—"} 分（≥80 优 / 60~79 良 / <60 差）`);
      });
      toast("清晰度打分中…");
    } catch (e) { toast("清晰度打分启动失败: " + e.message, 6000); }
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
    let nScored = 0;
    (state.items || []).forEach(item => {
      segsFor(item.id).forEach(s => {
        clips.push({
          item_id: item.id, start: s.start, end: s.end, text: s.text,
          language: s.language, speaker: (charById(s.characterId) || {}).name || "",
          q: s.q != null ? s.q : null,
        });
        if (s.q != null) nScored++;
      });
    });
    if (!nScored) {
      const go = confirm("片段尚未打清晰度分（将不做过滤/排序）。是否先去片段面板点「清晰度」打分？\n确定=继续导出，取消=中止");
      if (!go) return;
    }
    const minScore = Number($("#ds-min-score") && $("#ds-min-score").value) || 0;
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
          min_score: minScore,
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

  return { importDialog, uploadFile, doDenoise, doSeparate, doTrim, doAutosplit,
           openExportModal, doExportSelection, doUrlOpen,
           openTranscribeModal, doTranscribe, doQualityScan, doAlignSpeech,
           openDatasetModal, doDatasetExport };
}
