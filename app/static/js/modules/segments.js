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
  // 状态标签文本：合规/问题 + 清晰度分数（有分时附后）
  function _tagText(seg, issues) {
    const base = issues.length ? issues.join("，") : "合规";
    return seg.q != null ? `${base} · ${Math.round(seg.q)}分` : base;
  }
  // 分数档位类：>=80 ok（绿）、60~79 warn（黄）、<60 bad（红）；无分数不染色
  function _qCls(seg, tagCls) {
    if (seg.q == null || tagCls === "bad") return tagCls;
    if (seg.q < 60) return "bad";
    if (seg.q < 80 && tagCls === "ok") return "warn";
    return tagCls;
  }
  function updateSegBadge(itemId, i) {
    const tr = $(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (!tr) return;
    const segs = segsFor(itemId);
    const seg = segs[i];
    if (!seg) return;
    const issues = segIssues(seg);
    const tagCls = _qCls(seg, issues.length ? (issues.some(x => x === "空文本" || x === "混合") ? "warn" : "bad") : "ok");
    const cell = tr.querySelector(".tag");
    if (cell) { cell.className = "tag " + tagCls; cell.textContent = _tagText(seg, issues); }
    tr.classList.toggle("bad", issues.length > 0);
    const ch = charById(seg.characterId);
    tr.style.borderLeft = ch ? "4px solid " + ch.color : "";
  }

  function renderSegments() {
    const tb = $("#seg-tbody");
    const empty = $("#seg-empty");
    const rows = allSegs();
    state._segRows = rows;                       // 虚拟渲染的数据源
    $("#seg-count").textContent = rows.length ? `(${rows.length})` : "";
    empty.classList.toggle("hidden", rows.length > 0);
    if (rows.length <= VIRTUAL_THRESHOLD) {
      tb.innerHTML = "";
      rows.forEach((row) => {
        const i = segsFor(row.item.id).indexOf(row.seg);
        tb.appendChild(_buildRow(row.item, row.seg, i));
      });
      if (tb.firstChild) _measureRowH(tb.firstChild);
    } else {
      const sc = $("#seg-scroll");
      if (sc) sc.scrollTop = 0;
      _renderWindow();
    }
    // 重渲染后恢复试听高亮（不滚动，避免打扰）
    if (state.auditionFocus) auditionFocus(state.auditionFocus.itemId, state.auditionFocus.segId, false);
  }

  // ── 大表虚拟渲染：只构建可视区 ± 缓冲行，上下用占位行撑高度 ──
  const VIRTUAL_THRESHOLD = 400;
  let _rowH = 0;        // 实测行高（首行 offsetHeight），0=未测
  let _scrollRaf = 0;

  function _measureRowH(tr) {
    if (_rowH > 0) return _rowH;
    const h = tr ? tr.getBoundingClientRect().height : 0;
    if (h > 10) { _rowH = h; }
    return _rowH || 33;
  }

  function _buildRow(item, seg, i) {
    const issues = segIssues(seg);
    const cls = issues.length ? "bad" : "";
    const tagCls = _qCls(seg, issues.length ? (issues.some(x => x === "空文本" || x === "混合") ? "warn" : "bad") : "ok");
    const tagTxt = _tagText(seg, issues);
    const ch = charById(seg.characterId);
    const tr = document.createElement("tr");
    tr.className = "seg-row" + (cls ? " " + cls : "") + (state.selectedSegs.has(seg.id) ? " sel" : "")
      + (state.activeSeg && state.activeSeg.segId === seg.id ? " active" : "")
      + (seg.locked ? " locked" : "");
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
    return tr;
  }

  function _renderWindow() {
    const tb = $("#seg-tbody");
    const rows = state._segRows || [];
    const sc = $("#seg-scroll");
    if (!tb || !sc) return;
    const rowH = _measureRowH(tb.querySelector("tr.seg-row"));
    // 页面隐藏时 clientHeight 为 0，用默认视口高度兜底
    const viewH = sc.clientHeight || 480;
    const first = Math.max(0, Math.floor(sc.scrollTop / rowH) - 5);
    const last = Math.min(rows.length, first + Math.ceil(viewH / rowH) + 10);
    tb.innerHTML = "";
    if (first > 0) {
      const sp = document.createElement("tr");
      sp.innerHTML = `<td colspan="10" style="height:${first * rowH}px;padding:0;border:none"></td>`;
      tb.appendChild(sp);
    }
    for (let k = first; k < last; k++) {
      const r = rows[k];
      tb.appendChild(_buildRow(r.item, r.seg, segsFor(r.item.id).indexOf(r.seg)));
    }
    if (last < rows.length) {
      const sp = document.createElement("tr");
      sp.innerHTML = `<td colspan="10" style="height:${(rows.length - last) * rowH}px;padding:0;border:none"></td>`;
      tb.appendChild(sp);
    }
  }

  const _sc = $("#seg-scroll");
  if (_sc) _sc.addEventListener("scroll", () => {
    if ((state._segRows || []).length <= VIRTUAL_THRESHOLD) return;
    if (_scrollRaf) return;
    _scrollRaf = requestAnimationFrame(() => { _scrollRaf = 0; _renderWindow(); });
  }, { passive: true });

  // 虚拟模式下确保目标行在可视区内（先滚动再重建窗口）
  function _ensureRowVisible(itemId, segId) {
    const rows = state._segRows || [];
    if (rows.length <= VIRTUAL_THRESHOLD) return;
    const idx = rows.findIndex(r => r.item.id === itemId && r.seg.id === segId);
    if (idx < 0) return;
    const sc = $("#seg-scroll");
    const rowH = _rowH || 33;
    const top = idx * rowH;
    if (top < sc.scrollTop || top > sc.scrollTop + sc.clientHeight - rowH) {
      sc.scrollTop = Math.max(0, top - sc.clientHeight / 2);
      _renderWindow();
    }
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
    _ensureRowVisible(itemId, segId);
    const segs = segsFor(itemId);
    const i = segs.findIndex(s => s.id === segId);
    if (i < 0) return;
    const tr = $(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (tr && tr.scrollIntoView) tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  // 活动片段边界同步：波形上拖动绑定选区结束后回写起止（只更新行内单元格，不整表重渲染）
  function syncSegBounds(itemId, segId, start, end) {
    const segs = segsFor(itemId);
    const seg = segs.find(s => s.id === segId);
    if (!seg) return;
    seg.start = start; seg.end = end;
    seg.locked = true;   // 人工调整边界 → 锁定（自动转写回填仍会执行，但批量转写/识别不再覆盖）
    scheduleSaveProject(itemId);
    const i = segs.indexOf(seg);
    const tr = $(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (tr) {
      tr.children[2].textContent = fmtT(seg.start) + " ~ " + fmtT(seg.end);
      tr.children[3].textContent = fmtDur(seg.end - seg.start);
    }
    updateSegBadge(itemId, i);
  }
  // 自动转写回填：识别完成后写入片段文本并刷新行内输入框（用户正在输入则不打扰）
  function applySegText(itemId, segId, text) {
    const segs = segsFor(itemId);
    const seg = segs.find(s => s.id === segId);
    if (!seg || !text || !text.trim()) return;
    seg.text = text.trim();
    scheduleSaveProject(itemId);
    const i = segs.indexOf(seg);
    const tr = $(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (tr) {
      const inp = tr.querySelector(".seg-text");
      if (inp && document.activeElement !== inp) inp.value = seg.text;
    }
    updateSegBadge(itemId, i);
  }

  // 试听跳转高亮：给当前正在试听的片段行加 .playing（角色池连续试听时逐段跟随）
  function auditionFocus(itemId, segId, scroll = true) {
    state.auditionFocus = { itemId, segId };
    document.querySelectorAll("#seg-tbody tr.seg-row.playing")
      .forEach(el => el.classList.remove("playing"));
    if (scroll) _ensureRowVisible(itemId, segId);
    const segs = segsFor(itemId);
    const i = segs.findIndex(s => s.id === segId);
    const tr = $(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (!tr) return;
    tr.classList.add("playing");
    if (scroll && tr.scrollIntoView) tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  return { segsFor, segIssues, allSegs, charSegs, updateSegBadge, renderSegments,
           addSegmentFromSelection, deleteSegment, jumpToSegment, auditionSegment,
           gotoEditAndPlay, scrollSegRow, auditionFocus, syncSegBounds, applySegText };
}
