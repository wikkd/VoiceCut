// VoiceCut 前端 — 片段列表（数据访问 · 合规校验 · 渲染 · 增删 · 跳转试听）
// 由 createSegments(ctx) 创建；ctx 注入 util + state + 主流程依赖（角色/撤销/保存/页面），
// 避免与主流程模块形成循环 import。selectItem 以闭包形式注入（waveform 实例晚于本模块创建）。
export function createSegments(ctx) {
  const { $, esc, shortName, fmtT, fmtDur, fmtSel, SEG_MIN, SEG_MAX,
          state, toast, charById, newSegment, pushUndo,
          scheduleSaveProject, closePool, setPage, selectItem } = ctx;

  function segsFor(itemId) {
    if (!state.segmentsByItem.has(itemId)) state.segmentsByItem.set(itemId, []);
    return state.segmentsByItem.get(itemId);
  }
  function segIssues(seg) {
    const issues = [];
    if (seg.mixed) issues.push("混合");
    const dur = seg.end - seg.start;
    if (!seg.text.trim()) issues.push("空文本");
    if (dur < SEG_MIN) issues.push(`过短(<${SEG_MIN}s)`);
    if (dur > SEG_MAX) issues.push(`过长(>${SEG_MAX}s)`);
    return issues;
  }
  function allSegs() {
    const out = [];
    (state.items || []).forEach(item => {
      (segsFor(item.id) || []).forEach(seg => out.push({ item, seg }));
    });
    return out;
  }
  function charSegs(cid) {
    return allSegs().filter(r => (cid ? r.seg.characterId === cid : !r.seg.characterId));
  }
  function updateSegBadge(itemId, i) {
    const tr = document.querySelector(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (!tr) return;
    const segs = segsFor(itemId);
    const seg = segs[i];
    if (!seg) return;
    const issues = segIssues(seg);
    const tagCls = issues.length ? (issues.some(x => x === "空文本" || x === "混合") ? "warn" : "bad") : "ok";
    const tagTxt = issues.length ? issues.join("，") : "合规";
    const cell = tr.querySelector(".tag");
    if (cell) { cell.className = "tag " + tagCls; cell.textContent = tagTxt; }
    tr.classList.toggle("bad", issues.length > 0);
    const ch = charById(seg.characterId);
    tr.style.borderLeft = ch ? "4px solid " + ch.color : "";
  }

  function renderSegments() {
    const tb = $("#seg-tbody");
    const empty = $("#seg-empty");
    tb.innerHTML = "";
    const rows = allSegs();
    $("#seg-count").textContent = rows.length ? `(${rows.length})` : "";
    empty.classList.toggle("hidden", rows.length > 0);
    rows.forEach((row) => {
      const seg = row.seg, item = row.item;
      const i = segsFor(item.id).indexOf(seg);
      const issues = segIssues(seg);
      const cls = issues.length ? "bad" : "";
      const tagCls = issues.length ? (issues.some(x => x === "空文本" || x === "混合") ? "warn" : "bad") : "ok";
      const tagTxt = issues.length ? issues.join("，") : "合规";
      const ch = charById(seg.characterId);
      const tr = document.createElement("tr");
      tr.className = "seg-row" + (cls ? " " + cls : "") + (state.selectedSegs.has(seg.id) ? " sel" : "");
      tr.dataset.item = item.id;
      tr.dataset.i = i;
      if (ch) tr.style.borderLeft = "4px solid " + ch.color;
      tr.innerHTML = `
        <td class="seg-num">${String(i + 1).padStart(2, "0")}</td>
        <td class="seg-src" title="${esc(item.name)}">${esc(shortName(item.name))}</td>
        <td>${fmtT(seg.start)} ~ ${fmtT(seg.end)}</td>
        <td>${fmtDur(seg.end - seg.start)}</td>
        <td><span class="tag ${tagCls}">${tagTxt}</span></td>
        <td><button class="chip seg-aud" data-i="${i}">试听</button></td>
        <td><input type="text" class="seg-text" data-i="${i}" value="${esc(seg.text)}" placeholder="输入转写文本…"></td>
        <td><select class="seg-lang" data-i="${i}">
          ${["JP","ZH","EN"].map(l => `<option value="${l}" ${seg.language === l ? "selected" : ""}>${l}</option>`).join("")}
        </select></td>
        <td><select class="seg-speaker" data-i="${i}">
          <option value="">未分配</option>
          ${state.characters.map(c => `<option value="${esc(c.id)}" ${seg.characterId === c.id ? "selected" : ""} style="color:${esc(c.color)}">${esc(c.name)}</option>`).join("")}
        </select></td>
        <td class="row-actions">
          <button class="chip seg-jump" data-i="${i}">跳转</button>
          <button class="chip danger seg-del" data-i="${i}">删除</button>
        </td>`;
      tb.appendChild(tr);
    });
    // 重渲染后恢复试听高亮（不滚动，避免打扰）
    if (state.auditionFocus) auditionFocus(state.auditionFocus.itemId, state.auditionFocus.segId, false);
  }

  function addSegmentFromSelection() {
    if (!state.currentItem) return toast("请先导入素材");
    if (!state.selection) return toast("请先在波形上拖拽出选区");
    const segs = segsFor(state.currentItem.id);
    pushUndo();
    const list = state.multiRegions.length >= 2 ? state.multiRegions : [];
    if (list.length) {
      list.forEach((m) => segs.push(newSegment(m.start, m.end)));
      scheduleSaveProject();
      renderSegments();
      toast(`已加入片段 ${list.length} 条`);
      return;
    }
    segs.push(newSegment(state.selection.start, state.selection.end));
    scheduleSaveProject();
    renderSegments();
  }
  function deleteSegment(itemId, i) {
    const segs = segsFor(itemId);
    const s = segs[i];
    if (s) state.selectedSegs.delete(s.id);
    pushUndo();
    segs.splice(i, 1);
    scheduleSaveProject(itemId); renderSegments();
  }
  async function jumpToSegment(item, seg) {
    if (state.currentItem && state.currentItem.id !== item.id) await selectItem(item);
    if (!state.ws) return;
    state.ws.setTime(seg.start);
    $("#sel-info").textContent = fmtSel(seg);
  }
  async function auditionSegment(item, seg) {
    if (!state.currentItem || state.currentItem.id !== item.id) await selectItem(item);
    if (!state.ws) return;
    state.ws.setTime(seg.start);
    state.ws.play();
    state.auditioning = { start: seg.start, end: seg.end };
    scrollSegRow(item.id, seg.id);
  }
  // 从角色池/素材库等位置试听时：先回到剪辑页并选中对应素材再播放
  async function gotoEditAndPlay(item, seg) {
    closePool();
    setPage("edit");
    await auditionSegment(item, seg);
  }
  function scrollSegRow(itemId, segId) {
    const segs = segsFor(itemId);
    const i = segs.findIndex(s => s.id === segId);
    if (i < 0) return;
    const tr = document.querySelector(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (tr && tr.scrollIntoView) tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  // 试听跳转高亮：给当前正在试听的片段行加 .playing（角色池连续试听时逐段跟随）
  function auditionFocus(itemId, segId, scroll = true) {
    state.auditionFocus = { itemId, segId };
    document.querySelectorAll("#seg-tbody tr.seg-row.playing")
      .forEach(el => el.classList.remove("playing"));
    const segs = segsFor(itemId);
    const i = segs.findIndex(s => s.id === segId);
    const tr = document.querySelector(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (!tr) return;
    tr.classList.add("playing");
    if (scroll && tr.scrollIntoView) tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  return { segsFor, segIssues, allSegs, charSegs, updateSegBadge, renderSegments,
           addSegmentFromSelection, deleteSegment, jumpToSegment, auditionSegment,
           gotoEditAndPlay, scrollSegRow, auditionFocus };
}
