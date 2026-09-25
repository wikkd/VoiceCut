// VoiceCut 前端 — 后台任务跟踪（轮询 · 状态栏 · 取消 · 完成后刷新与选中）
// 由 createTasks(ctx) 创建；ctx 注入 util + state + 主流程依赖（素材/项目/渲染）。
// 本模块在其他功能模块之前创建，trackTask 等对外是真函数；
// 对 pool/segments/subtitles/waveform 的调用以闭包注入，规避模块创建顺序耦合。
export function createTasks(ctx) {
  const { $, api, toast, state,
          renderMediaList, refreshItems, loadAllItemData,
          renderPool, renderSegments, renderSubs, selectItem } = ctx;

  function trackTask(taskId, doneCb) {
    const existing = state.activeTasks.get(taskId);
    if (existing) { existing.doneCb = doneCb; return; }  // 同一后台任务去重
    state.activeTasks.set(taskId, { msg: "排队中", progress: 0, doneCb });
    ensurePolling();
    updateStatusbar();
  }

  // 后台自动分析完成：刷新角色池 / 素材 / 片段 / 字幕
  async function autoAnalyzeDone(result) {
    if (!result) return;
    if (Array.isArray(result.characters)) state.characters = result.characters;
    try {
      if (state.currentProject) {
        const j = await api(`/api/projects/${state.currentProject.id}`);
        if (Array.isArray(j.characters)) state.characters = j.characters;
        state.items = j.items || [];
        renderMediaList();
      }
    } catch (e) { /* 网络抖动忽略，用任务结果兜底 */ }
    await loadAllItemData();
    renderPool(); renderSegments(); renderSubs();
    const created = (result.created || []).length, merged = (result.merged || 0);
    const mixed = result.mixed || 0, cleaned = result.cleaned || 0;
    const itemN = (result.items || []).length;
    let msg = `后台分析完成：${result.n_speakers} 人（${result.quality === "ecapa" ? "ECAPA" : "MFCC 降级"}），跨 ${itemN} 个素材 ${result.labeled}/${result.total} 段已标记，其中 ${mixed} 段为多人混合(未绑定)；新增 ${created} 角色，跨素材归并 ${merged} 段`;
    if (cleaned > 0) msg += `；已清理 ${cleaned} 个旧版本残留角色`;
    toast(msg, 6000);
    return true;  // 已显示专属完成提示，抑制通用“任务完成”
  }

  // 刷新页面后重新挂接仍在后台运行的任务（含导入后自动分析）
  async function attachActiveTasks() {
    try {
      const tasks = await api("/api/tasks/active");
      tasks.forEach((t) => {
        if (state.activeTasks.has(t.id)) return;
        let cb = null;
        if (t.kind === "speakers") cb = autoAnalyzeDone;
        else if (t.kind === "import") cb = (r) => { if (r) refreshItems(); };
        if (cb) trackTask(t.id, cb);
      });
    } catch (e) { /* 忽略 */ }
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
          await refreshItems();
          let customToast = false;
          if (info.doneCb) customToast = !!(await info.doneCb(t.result));
          if (!customToast) toast("任务完成");
          finalizeTaskBubble(tid, "done", "✓ 任务完成", 1200);
          state.identifying = false;  // 识别回调已完成，解冻角色池落盘
        } else if (t.status === "error") {
          state.activeTasks.delete(tid);
          state.identifying = false;
          finalizeTaskBubble(tid, "err", "✗ 任务失败: " + (t.message || "未知错误"), 5000);
        } else if (t.status === "cancelled") {
          state.activeTasks.delete(tid);
          state.identifying = false;
          finalizeTaskBubble(tid, "", "任务已取消", 1500);
        }
      } catch (e) { /* 网络抖动忽略 */ }
    }
    updateStatusbar();
  }
  function updateStatusbar() {
    const info = $("#task-info");
    renderTaskBubbles();
    info.textContent = state.activeTasks.size
      ? `后台任务 ${state.activeTasks.size} 个（左下气泡可单独取消）` : "就绪";
  }
  // 任务气泡：每个 activeTask 一个持久气泡，向上堆叠；终态时变色短暂停留
  function renderTaskBubbles() {
    const stack = $("#toast-stack");
    if (!stack) return;
    [...stack.querySelectorAll(".toast-bubble.task")].forEach(b => {
      if (b.dataset.taskId && !state.activeTasks.has(b.dataset.taskId)) b.remove();
    });
    for (const [tid, t] of state.activeTasks) {
      let b = stack.querySelector(`.toast-bubble.task[data-task-id="${tid}"]`);
      if (!b) {
        b = document.createElement("div");
        b.className = "toast-bubble task";
        b.dataset.taskId = tid;
        b.innerHTML = `<div class="trow"><span class="tmsg"></span>` +
          `<button class="tclose" title="取消任务">✕</button></div>` +
          `<div class="tbar"><div class="tfill"></div></div>`;
        b.querySelector(".tclose").addEventListener("click", () => {
          api(`/api/tasks/${tid}/cancel`, { method: "POST" }).catch(() => {});
        });
        stack.appendChild(b);
      }
      b.querySelector(".tmsg").textContent = t.msg;
      b.querySelector(".tfill").style.width = ((t.progress || 0) * 100).toFixed(0) + "%";
    }
  }
  // 任务终态：气泡转终态样式短暂停留后淡出
  function finalizeTaskBubble(tid, cls, msg, ms = 1800) {
    const b = $(`#toast-stack .toast-bubble.task[data-task-id="${tid}"]`);
    if (!b) { if (cls === "err") toast(msg, 5000); return; }
    b.dataset.taskId = "";   // 脱离 activeTasks 追踪，避免被 renderTaskBubbles 清除
    b.classList.add(cls);
    b.querySelector(".tmsg").textContent = msg;
    const bar = b.querySelector(".tbar"); if (bar) bar.remove();
    const x = b.querySelector(".tclose"); if (x) x.remove();
    setTimeout(() => { b.classList.add("out"); setTimeout(() => b.remove(), 280); }, ms);
  }
  function cancelAllTasks() {
    Array.from(state.activeTasks.keys()).forEach((tid) => {
      api(`/api/tasks/${tid}/cancel`, { method: "POST" }).catch(() => {});
    });
  }
  function selectResultItem(result) {
    if (!result) return;
    const id = result.item ? result.item.id : (result.item_ids && result.item_ids[0]);
    if (!id) return;
    let item = state.items.find(x => x.id === id);
    if (!item && result.item) {
      item = result.item;
      state.items.push(item);
      renderMediaList();
    }
    if (item) selectItem(item);
  }

  return { trackTask, autoAnalyzeDone, attachActiveTasks, updateStatusbar,
           cancelAllTasks, selectResultItem };
}
