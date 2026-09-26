// VoiceCut 前端 — 后台任务跟踪（轮询 · 状态栏 · 取消 · 完成后刷新与选中）
// 由 createTasks(ctx) 创建；ctx 注入 util + state + 主流程依赖（素材/项目/渲染）。
// 本模块在其他功能模块之前创建，trackTask 等对外是真函数；
// 对 pool/segments/subtitles/waveform 的调用以闭包注入，规避模块创建顺序耦合。
export function createTasks(ctx) {
  const { $, api, esc, ico, toast, state, pushUndo,
          renderMediaList, refreshItems, loadAllItemData,
          renderPool, renderSegments, renderSubs, selectItem, afterAnalyze } = ctx;

  // opts.quiet：静默任务（如声纹重匹配）——仍走气泡进度与取消，但不锁界面
  function trackTask(taskId, doneCb, opts) {
    const existing = state.activeTasks.get(taskId);
    if (existing) { existing.doneCb = doneCb; return; }  // 同一后台任务去重
    state.activeTasks.set(taskId, { msg: "排队中", progress: 0, doneCb, quiet: !!(opts && opts.quiet),
      logs: [`[${new Date().toLocaleTimeString("zh-CN", { hour12: false })}] 任务已提交`] });
    ensurePolling();
    updateStatusbar();
  }

  // 后台自动分析完成：刷新角色池 / 素材 / 片段 / 字幕
  async function autoAnalyzeDone(result) {
    if (!result) return;
    pushUndo("说话人识别");   // 识别结果覆盖前快照（导入自动分析 / 刷新后重挂任务均覆盖）
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
    // 识别任务收尾时会提交自动训练任务（auto_training 开启时），重新挂接跟踪
    attachActiveTasks();
    if (afterAnalyze) setTimeout(afterAnalyze, 400);   // 自动补扫空白区（pool.scanGaps）
    return true;  // 已显示专属完成提示，抑制通用“任务完成”
  }

  // 自动训练管线（单角色）完成：刷新角色池展示试听音频入口
  async function autoTrainDone(result) {
    try {
      if (state.currentProject) {
        const j = await api(`/api/projects/${state.currentProject.id}`);
        if (Array.isArray(j.characters)) state.characters = j.characters;
      }
    } catch (e) { /* 网络抖动忽略，渲染旧池 */ }
    renderPool();
    const name = (result && (result.role || result.role_id)) || "角色";
    if (result && result.ok) {
      toast(`「${name}」自动管线完成（${result.trained ? "全阶段训练" : "zero-shot 试听"}），角色池可点「听声辨认」`, 6000);
    } else if (result && result.error) {
      toast(`「${name}」自动管线失败: ${result.error}`, 8000);
    }
    return true;
  }

  // 刷新页面后重新挂接仍在后台运行的任务（含导入后自动分析）
  async function attachActiveTasks() {
    try {
      const tasks = await api("/api/tasks/active");
      tasks.forEach((t) => {
        if (state.activeTasks.has(t.id)) return;
        let cb = null;
        if (t.kind === "speakers") cb = autoAnalyzeDone;
        else if (t.kind === "train") cb = autoTrainDone;
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
        // 消息变化 → 记入任务日志流（遮罩日志条展示，带时间戳，上限 60 条）
        if (t.message && t.message !== info.msg) {
          (info.logs = info.logs || []).push(`[${new Date().toLocaleTimeString("zh-CN", { hour12: false })}] ${t.message}`);
          if (info.logs.length > 60) info.logs.splice(0, info.logs.length - 60);
        }
        info.msg = t.message || info.msg;
        info.progress = t.progress || 0;
        if (t.status === "done") {
          state.activeTasks.delete(tid);
          await refreshItems();
          let customToast = false;
          if (info.doneCb) customToast = !!(await info.doneCb(t.result));
          if (!customToast) toast("任务完成");
          finalizeTaskBubble(tid, "done", "任务完成", 1200);
          state.identifying = false;  // 识别回调已完成，解冻角色池落盘
        } else if (t.status === "error") {
          state.activeTasks.delete(tid);
          state.identifying = false;
          finalizeTaskBubble(tid, "err", "任务失败: " + (t.message || "未知错误"), 5000);
        } else if (t.status === "cancelled") {
          state.activeTasks.delete(tid);
          state.identifying = false;
          finalizeTaskBubble(tid, "", "任务已取消", 1500);
        }
      } catch (e) { /* 网络抖动忽略 */ }
    }
    updateStatusbar();
  }
  // 遮罩中央：每任务一行（消息 + 进度条）+ 汇总日志条（各任务日志按任务顺序合并，显示最近 10 条，自动滚底）
  function renderBusyOverlay() {
    const box = $("#busy-overlay .busy-tasks");
    const logsEl = $("#busy-overlay .busy-logs");
    if (!box) return;
    box.innerHTML = "";
    const allLogs = [];
    for (const [, t] of state.activeTasks) {
      if (t.quiet) continue;   // 静默任务不进遮罩（气泡区仍有进度）
      (t.logs || []).forEach(l => allLogs.push(l));
      const row = document.createElement("div");
      row.className = "busy-task";
      const pct = ((t.progress || 0) * 100).toFixed(0) + "%";
      row.innerHTML = `<div class="bt-row"><span class="bt-msg"></span><span class="bt-pct"></span></div>` +
        `<div class="bt-bar"><div class="bt-fill"></div></div>`;
      row.querySelector(".bt-msg").textContent = t.msg;
      row.querySelector(".bt-pct").textContent = pct;
      row.querySelector(".bt-fill").style.width = pct;
      box.appendChild(row);
    }
    if (logsEl) {
      logsEl.innerHTML = "";
      allLogs.slice(-10).forEach(l => {
        const d = document.createElement("div");
        d.className = "busy-log-line";
        d.textContent = l;
        logsEl.appendChild(d);
      });
      logsEl.scrollTop = logsEl.scrollHeight;
    }
  }

  function updateStatusbar() {
    const info = $("#task-info");
    renderTaskBubbles();
    info.textContent = state.activeTasks.size
      ? `后台任务 ${state.activeTasks.size} 个（左下气泡可单独取消）` : "就绪";
    // 后台任务运行期锁定页面操作：有「非静默」任务 → 全屏遮罩，全清 → 解锁
    // （声纹重匹配等 quiet 任务仍显示气泡进度与取消，但不锁界面，用户可继续改下一段）
    const ov = $("#busy-overlay");
    if (ov) {
      const locking = [...state.activeTasks.values()].some(t => !t.quiet);
      ov.classList.toggle("hidden", !locking);
      renderBusyOverlay();
    }
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
          `<button class="tclose" title="取消任务">${ico("close")}</button></div>` +
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
    // done / err 前置一个状态图标（CSS mask，跟随气泡的状态色）；取消/其余为纯文字。
    // 图标走行内 inline-block（.ico 自带 vertical-align 补偿），所以这里不用改 .tmsg 的布局。
    const mark = cls === "done" ? ico("ok") : cls === "err" ? ico("fail") : "";
    const tm = b.querySelector(".tmsg");
    if (mark) tm.innerHTML = mark + esc(msg); else tm.textContent = msg;
    const bar = b.querySelector(".tbar"); if (bar) bar.remove();
    const x = b.querySelector(".tclose"); if (x) x.remove();
    setTimeout(() => { b.classList.add("out"); setTimeout(() => b.remove(), 280); }, ms);
  }
  function cancelAllTasks() {
    Array.from(state.activeTasks.keys()).forEach((tid) => {
      api(`/api/tasks/${tid}/cancel`, { method: "POST" }).catch(() => {});
    });
  }
  // 遮罩上的「取消全部任务」按钮（遮罩拦截其余一切交互，仅留取消出口）
  const _busyCancel = $("#busy-cancel");
  if (_busyCancel) _busyCancel.addEventListener("click", cancelAllTasks);
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
