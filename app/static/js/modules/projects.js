// VoiceCut 前端 — 素材库与项目（素材列表/右键菜单 · 项目切换与管理 · 工作台清理）
// 由 createProjects(ctx) 创建；ctx 注入 util + state + 数据层（store）+ 跨模块闭包，
// 避免与功能模块形成循环 import。素材列表与项目下拉的 DOM 事件在工厂内一次性绑定。
export function createProjects(ctx) {
  const { $, esc, fmtDur, toast, api, state, LS_PROJECT,
          loadProject, saveProjectNow, savePoolNow,
          selectItem, resetWaveUI, renderPool, renderAutoAnalyzeBtn,
          renderSegments, renderSubs, attachActiveTasks } = ctx;

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
  document.addEventListener("contextmenu", (e) => { if (!e.target.closest("#media-list li")) hideMediaMenu(); });

  return { renderMediaList, refreshItems, renderProjectSelect, selectProject,
           loadAllItemData, clearWorkbench, createProject, renameProject,
           deleteProject, deleteMediaItem, hideMediaMenu };
}
