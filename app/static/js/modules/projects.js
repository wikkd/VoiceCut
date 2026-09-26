// VoiceCut 前端 — 素材库与项目（素材列表/右键菜单 · 项目切换与管理 · 工作台清理）
// 由 createProjects(ctx) 创建；ctx 注入 util + state + 数据层（store）+ 跨模块闭包，
// 避免与功能模块形成循环 import。素材列表与项目下拉的 DOM 事件在工厂内一次性绑定。
export function createProjects(ctx) {
  const { $, esc, ico, fmtDur, toast, api, state, LS_PROJECT,
          loadProject, fillItemStates, saveProjectNow, savePoolNow,
          selectItem, resetWaveUI, renderPool, renderAutoAnalyzeBtn,
          renderAutoTrainingBtn, renderAutoGapScanBtn,
          renderSegments, renderSubs, attachActiveTasks, setPage } = ctx;

  // ── 素材列表 ──
  function _mediaLi(item) {
    const li = document.createElement("li");
    li.dataset.id = item.id;
    if (state.currentItem && state.currentItem.id === item.id) li.classList.add("active");
    const kindMap = { video: "视频", audio: "音频", denoised: "降噪", vocal: "人声",
                      instrumental: "伴奏", trimmed: "去静音", bilibili: "B站", url: "网络" };
    li.innerHTML = `<div class="m-name">${esc(item.name)}</div>
      <div class="m-meta"><span class="m-badge">${kindMap[item.kind] || item.kind}</span>
      <span>${fmtDur(item.duration)}</span>
      <button class="m-del" title="删除素材">${ico("close")}</button></div>`;
    li.addEventListener("click", () => selectItem(item));
    li.addEventListener("contextmenu", (e) => { e.preventDefault(); showMediaMenu(e.clientX, e.clientY, item); });
    li.querySelector(".m-del").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteMediaItem(item);
    });
    return li;
  }
  function renderMediaList() {
    // 侧栏列表（工作区左侧）
    const ul = $("#media-list"), empty = $("#media-empty");
    if (ul) {
      ul.innerHTML = "";
      if (empty) empty.classList.toggle("hidden", state.items.length > 0);
      state.items.forEach((item) => ul.appendChild(_mediaLi(item)));
    }
    // 素材库页（卡片 + 搜索/排序/统计）
    const pul = $("#media-page-list"), pempty = $("#media-page-empty"), stats = $("#media-page-stats");
    if (pul) {
      pul.innerHTML = "";
      const items = _visibleItems();
      items.forEach((item) => pul.appendChild(_mediaCard(item)));
      if (pempty) {
        pempty.classList.toggle("hidden", state.items.length > 0);
        pempty.textContent = mediaFilter ? "没有匹配的素材" : "拖拽文件到窗口，或点「导入文件」";
      }
      if (stats) {
        const total = state.items.reduce((s, it) => s + (it.duration || 0), 0);
        const scope = mediaFilter ? `${items.length} / ${state.items.length} 个素材` : `${state.items.length} 个素材`;
        stats.textContent = `${scope} · 总时长 ${fmtDur(total)}`;
      }
    }
  }

  // ── 素材库页卡片 ──
  let mediaFilter = "";
  let mediaSort = "default";
  const peaksCache = new Map();   // item.id -> peaks [[min,max],...]

  function _visibleItems() {
    let items = state.items.slice();
    if (mediaFilter) {
      const q = mediaFilter.toLowerCase();
      items = items.filter((it) => (it.name || "").toLowerCase().includes(q));
    }
    if (mediaSort === "name") items.sort((a, b) => (a.name || "").localeCompare(b.name || "", "zh"));
    else if (mediaSort === "dur") items.sort((a, b) => (b.duration || 0) - (a.duration || 0));
    return items;
  }

  function _paintWave(canvas, peaks) {
    if (!canvas) return;
    const w = canvas.clientWidth || 220, h = canvas.clientHeight || 44;
    canvas.width = w; canvas.height = h;
    const g = canvas.getContext("2d");
    g.clearRect(0, 0, w, h);
    if (!peaks || !peaks.length) return;
    let max = 0.01;
    for (const p of peaks) max = Math.max(max, Math.abs(p[0]), Math.abs(p[1]));
    g.fillStyle = "rgba(108,156,255,0.55)";
    const cols = Math.min(peaks.length, Math.floor(w / 2));
    const step = peaks.length / cols;
    for (let i = 0; i < cols; i++) {
      const p = peaks[Math.floor(i * step)];
      const bh = Math.max(2, ((Math.abs(p[0]) + Math.abs(p[1])) / (2 * max)) * h);
      g.fillRect(i * 2, (h - bh) / 2, 1.4, bh);
    }
  }

  function _loadWave(canvas, item) {
    if (peaksCache.has(item.id)) { _paintWave(canvas, peaksCache.get(item.id)); return; }
    api(`/api/peaks/${item.id}`).then((j) => {
      peaksCache.set(item.id, j.peaks || []);
      _paintWave(canvas, j.peaks || []);
    }).catch(() => {});
  }

  function _mediaCard(item) {
    const li = document.createElement("li");
    li.dataset.id = item.id;
    if (state.currentItem && state.currentItem.id === item.id) li.classList.add("active");
    const kindMap = { video: "视频", audio: "音频", denoised: "降噪", vocal: "人声",
                      instrumental: "伴奏", trimmed: "去静音", bilibili: "B站", url: "网络" };
    const kind = kindMap[item.kind] || item.kind;
    const parent = item.derived_from ? state.items.find((x) => x.id === item.derived_from) : null;
    const derived = parent ? `派生自 ${esc(parent.name)}` : "";
    const srcTag = (item.kind === "bilibili" || item.kind === "url") ? "网络导入" : "";
    li.innerHTML = `
      <div class="m-wave-wrap"><canvas class="m-wave"></canvas></div>
      <div class="m-name" title="${esc(item.name)}">${esc(item.name)}</div>
      <div class="m-meta">
        <span class="m-badge k-${esc(item.kind)}">${kind}</span>
        <span class="m-dur">${fmtDur(item.duration)}</span>
        ${item.sample_rate ? `<span class="m-dim">${Math.round(item.sample_rate / 1000)} kHz</span>` : ""}
        <span class="m-grow"></span>
        <button class="m-del" title="删除素材">${ico("close")}</button>
      </div>
      ${derived || srcTag ? `<div class="m-sub">${derived}${derived && srcTag ? " · " : ""}${srcTag}</div>` : ""}`;
    li.addEventListener("click", () => selectItem(item));
    li.addEventListener("dblclick", () => { selectItem(item); if (setPage) setPage("edit"); });
    li.addEventListener("contextmenu", (e) => { e.preventDefault(); showMediaMenu(e.clientX, e.clientY, item); });
    li.querySelector(".m-del").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteMediaItem(item);
    });
    _loadWave(li.querySelector(".m-wave"), item);
    return li;
  }

  // 素材库页工具行（搜索 / 排序），一次性绑定
  const searchInput = $("#media-search");
  if (searchInput) searchInput.addEventListener("input", () => {
    mediaFilter = searchInput.value.trim();
    renderMediaList();
  });
  const sortSel = $("#media-sort");
  if (sortSel) sortSel.addEventListener("change", () => {
    mediaSort = sortSel.value;
    renderMediaList();
  });
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
    // 菜单项里含 <i class="ico">，只改文本节点所在的 span（写 textContent 会抹掉图标）
    const addLabel = btnAdd.querySelector(".btn-label");
    if (addLabel) addLabel.textContent = isCurrent ? "已在工作区" : "添加到工作区";
    else btnAdd.textContent = isCurrent ? "已在工作区" : "添加到工作区";
    menu.classList.remove("hidden");
  }
  function hideMediaMenu() { const m = $("#media-menu"); if (m) m.classList.add("hidden"); }
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
    state.selection = null; state.selectionRegion = null;
    state.multiRegions = [];
    state.selectedSegs = new Set(); state.poolMerge = new Set();
    renderProjectSelect();
    const j = await api(`/api/projects/${proj.id}`);
    state.items = j.items || [];
    state.characters = j.characters || [];
    state.autoAnalyze = (j.auto_analyze !== false);
    state.autoTraining = (j.auto_training !== false);
    state.autoGapScan = (j.auto_gapscan !== false);
    renderAutoAnalyzeBtn();
    renderAutoTrainingBtn();
    renderAutoGapScanBtn();
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
    if (!items.length) return;
    // 先把本地未落盘的改动写回服务端，再拉服务端状态：否则这次拉取拿到的是
    // 「用户还没保存」的旧版本，会盖掉内存里刚改好的说话人指派（并且随后那次
    // 保存会把旧版本当用户数据写回，永久固化）。配合 store.fillItemStates 的
    // dirty 保护，两道防线都要有：这里解决"还没保存"，那里兜住"正在保存"。
    if (state.dirtyItems && state.dirtyItems.size && saveProjectNow) {
      try { await saveProjectNow(); } catch (e) { /* 保存失败时下面的 dirty 保护兜底 */ }
    }
    // 批量拉取（1 个请求替代逐素材 N 连发）；失败回退逐个加载
    if (state.currentProject && fillItemStates) {
      try {
        const j = await api(`/api/projects/${state.currentProject.id}/items_state`);
        fillItemStates(j.states || {});
        return;
      } catch (e) { /* fallthrough */ }
    }
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
    renderSegments(); renderSubs(); renderPool();
    resetWaveUI();
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
        state.selection = null; state.selectionRegion = null;
        state.multiRegions = [];
        state.auditionSeq = null; state.auditionIdx = 0;
        state.subs = []; state.currentSubIdx = -1; state.auditioning = null;
        state.playing = false;
        if (state.ws) { try { state.ws.destroy(); } catch (e) {} state.ws = null; }
        $("#video-panel").classList.add("no-video");
        const v = $("#video-preview"); v.removeAttribute("src");
        $("#empty-state").classList.remove("hidden");
        $("#sub-current").textContent = "—";
        resetWaveUI();
        renderSubs();
        renderSegments();
      }
      await refreshItems();
      toast("已删除素材");
    } catch (e) { toast("删除失败: " + e.message, 6000); }
  }

  // ── 素材右键菜单的全局关闭 ──
  document.addEventListener("click", hideMediaMenu);
  document.addEventListener("contextmenu", (e) => { if (!(/** @type {HTMLElement} */ (e.target).closest("#media-list li"))) hideMediaMenu(); });

  return { renderMediaList, refreshItems, renderProjectSelect, selectProject,
           loadAllItemData, clearWorkbench, createProject, renameProject,
           deleteProject, deleteMediaItem, hideMediaMenu };
}
