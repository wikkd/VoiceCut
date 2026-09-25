// VoiceCut 前端 — 角色池页（角色卡片 · 拖拽重定向 · 合并/重命名/配色 · 项目级说话人识别 · 自动分析开关）
// 由 createPool(ctx) 创建；ctx 注入 util + state + 主流程依赖（片段/字幕/保存/撤销），
// 避免与主流程模块形成循环 import。角色池自身的 DOM 事件在工厂内一次性绑定。
export function createPool(ctx) {
  const { $, $$, esc, shortName, fmtT, toast, state, api, trackTask,
          needItem, charById, uid, paletteNext, pushUndo,
          scheduleSaveProject, scheduleSavePool, loadAllItemData,
          segments, renderSubs } = ctx;

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
    state.identifying = true;  // 识别期间冻结角色池落盘，防止旧快照覆盖结果
    toast("开始项目级说话人识别（ECAPA 声纹，将把项目内全部素材联合聚类，首次含模型加载）…");
    try {
      const j = await api(`/api/projects/${state.currentProject.id}/speakers/generate`, { method: "POST" });
      trackTask(j.task_id, async (result) => {
        state.identifying = false;
        if (Array.isArray(result.characters)) state.characters = result.characters;
        await loadAllItemData();
        renderPool(); segments.renderSegments(); renderSubs();
        const created = (result.created || []).length;
        const merged = (result.merged || 0);
        const mixed = result.mixed || 0;
        const cleaned = result.cleaned || 0;
        const itemN = (result.items || []).length;
        let msg = `项目说话人识别完成：${result.n_speakers} 人（${result.quality === "ecapa" ? "ECAPA" : "MFCC 降级"}），跨 ${itemN} 个素材 ${result.labeled}/${result.total} 段已标记，其中 ${mixed} 段为多人混合(未绑定)；新增 ${created} 角色，跨素材归并 ${merged} 段`;
        if (cleaned > 0) msg += `；已清理 ${cleaned} 个旧版本残留角色`;
        toast(msg);
      });
    } catch (e) { state.identifying = false; toast("项目说话人识别启动失败: " + e.message, 6000); }
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

  // ── 角色池页面事件 ──
  function bindPoolUI() {
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
  }

  bindPoolUI();

  return { openPool, closePool, renderPool, doIdentifySpeakers, toggleAutoAnalyze,
           renderAutoAnalyzeBtn, reassignSegments, createPoolCharacter,
           mergePoolSelected };
}
