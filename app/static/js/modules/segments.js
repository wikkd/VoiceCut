// VoiceCut 前端 — 片段列表（数据访问 · 合规校验 · 渲染 · 增删 · 跳转试听）
// 由 createSegments(ctx) 创建；ctx 注入 util + state + 主流程依赖（角色/撤销/保存/页面），
// 避免与主流程模块形成循环 import。selectItem 以闭包形式注入（waveform 实例晚于本模块创建）。
export function createSegments(ctx) {
  const { $, esc, shortName, fmtT, fmtDur, fmtSel, ico, SEG_MIN, SEG_MAX,
          state, toast, api, trackTask, charById, newSegment, pushUndo,
          scheduleSaveProject, closePool, setPage, selectItem, refreshSelColor } = ctx;

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
  function updateSegBadge(itemId, i, draftText) {   // draftText：文本框草稿预览（未回车确认前的实时校验）
    const tr = $(`#seg-tbody tr.seg-row[data-item="${itemId}"][data-i="${i}"]`);
    if (!tr) return;
    const segs = segsFor(itemId);
    const seg = segs[i];
    if (!seg) return;
    const eff = draftText === undefined ? seg : { ...seg, text: draftText };
    const issues = segIssues(eff);
    const tagCls = _qCls(eff, issues.length ? (issues.some(x => x === "空文本" || x === "混合") ? "warn" : "bad") : "ok");
    const cell = tr.querySelector(".tag");
    if (cell) { cell.className = "tag " + tagCls; cell.textContent = _tagText(eff, issues); }
    tr.classList.toggle("bad", issues.length > 0);
    const ch = charById(seg.characterId);
    tr.style.borderLeft = ch ? "4px solid " + ch.color : "";
  }

  // ── 片段列表筛选/排序（只影响展示，data-i 始终是原始索引，编辑/删除不受影响） ──
  function _viewRows(rows) {
    const f = state.segFilter || { text: "", status: "all", item: "all" };
    const kw = (f.text || "").trim().toLowerCase();
    let out = rows;
    if (kw) out = out.filter(r => (r.seg.text || "").toLowerCase().includes(kw));
    if (f.item && f.item !== "all") out = out.filter(r => r.item.id === f.item);
    const st = f.status || "all";
    if (st === "ok") out = out.filter(r => segIssues(r.seg).length === 0);
    else if (st === "bad") out = out.filter(r => segIssues(r.seg).length > 0);
    else if (st === "locked") out = out.filter(r => r.seg.locked);
    else if (st === "unassigned") out = out.filter(r => !r.seg.characterId);
    // 排序：支持方向后缀（"dur"=降序 / "dur_asc"=升序；char 无方向两态）
    const sort = state.segSort || "time";
    const us = sort.indexOf("_");
    const skey = us < 0 ? sort : sort.slice(0, us);
    const sdir = us < 0 ? "" : sort.slice(us + 1);
    if (skey === "dur") {
      const d = sdir === "asc" ? 1 : -1;
      out = out.slice().sort((a, b) => d * ((b.seg.end - b.seg.start) - (a.seg.end - a.seg.start)));
    } else if (skey === "score") {
      const d = sdir === "asc" ? 1 : -1;
      out = out.slice().sort((a, b) => d * ((b.seg.q ?? -1) - (a.seg.q ?? -1)));   // 未打分排后（降序时）
    } else if (skey === "char") {
      const named = [], un = [];
      out.forEach(r => (r.seg.characterId ? named : un).push(r));
      named.sort((a, b) => {
        const na = (charById(a.seg.characterId) || {}).name || "";
        const nb = (charById(b.seg.characterId) || {}).name || "";
        return na === nb ? a.seg.start - b.seg.start : na.localeCompare(nb, "zh");
      });
      un.sort((a, b) => a.seg.start - b.seg.start);   // 未分配组排最后
      out = named.concat(un);
    }
    return out;
  }

  // 来源筛选下拉：选项随素材列表同步（签名比对，避免交互中重建 select）；素材被删时重置为全部
  let _itemFilterSig = "";
  function _syncItemFilter() {
    const sel = $("#seg-filter-item");
    if (!sel) return;
    const items = state.items || [];
    if (state.segFilter.item && state.segFilter.item !== "all"
      && !items.some(it => it.id === state.segFilter.item)) state.segFilter.item = "all";
    const sig = items.map(it => it.id).join(",");
    if (sig !== _itemFilterSig) {
      _itemFilterSig = sig;
      sel.innerHTML = '<option value="all">全部</option>'
        + items.map(it => `<option value="${esc(it.id)}">${esc(shortName(it.name, 10))}</option>`).join("");
    }
    sel.value = state.segFilter.item || "all";
  }

  function renderSegments() {
    _syncItemFilter();
    const tb = $("#seg-tbody");
    const sc = $("#seg-scroll");
    // 重建 tbody 会瞬间清空内容 → 浏览器把 scrollTop 归零（点击行/编辑等重渲染会莫名跳回第一条）。
    // 渲染前后保持滚动位置；行数变少时由浏览器自动 clamp 到最大滚动值。
    const keepTop = sc ? sc.scrollTop : 0;
    const empty = $("#seg-empty");
    const all = allSegs();
    const rows = _viewRows(all);
    state._segRows = rows;                       // 虚拟渲染的数据源
    const cnt = $("#seg-count");
    const f = state.segFilter || {};
    const filtered = ((f.text || "").trim()) || (f.status && f.status !== "all") || (f.item && f.item !== "all");
    cnt.textContent = !all.length ? ""
      : filtered ? `(${rows.length}/${all.length})` : `(${all.length})`;
    empty.classList.toggle("hidden", rows.length > 0);
    if (rows.length <= VIRTUAL_THRESHOLD) {
      tb.innerHTML = "";
      rows.forEach((row) => {
        const i = segsFor(row.item.id).indexOf(row.seg);
        tb.appendChild(_buildRow(row.item, row.seg, i));
      });
      if (tb.firstChild) _measureRowH(tb.firstChild);
    } else {
      _renderWindow();
    }
    if (sc && sc.scrollTop !== keepTop) {
      sc.scrollTop = keepTop;                                     // 保持滚动位置（跳转类调用会在其后自行滚动）
      if (rows.length > VIRTUAL_THRESHOLD) _renderWindow();        // 大表：按恢复后的位置重建可视窗口
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
      <td class="seg-num col-num">${String(i + 1).padStart(2, "0")}</td>
      <td class="seg-src col-src" title="${esc(item.name)}">${esc(shortName(item.name))}</td>
      <td class="seg-time col-time">${fmtT(seg.start)}<span class="sep">~</span>${fmtT(seg.end)}</td>
      <td class="seg-dur col-dur">${fmtDur(seg.end - seg.start)}</td>
      <td class="col-status"><span class="tag ${tagCls}">${tagTxt}</span><button class="chip seg-lock${seg.locked ? " on" : ""}" data-i="${i}" title="${seg.locked ? "已锁定（人工成果）：对齐发音 / 空白区补扫 / 声纹识别都会跳过它。按静音自动切分是整表替换（有确认、可撤销）。点击解锁" : "未锁定：点击锁定，防止这一段被自动流程（对齐发音 / 补扫 / 声纹识别）覆盖"}">${ico(seg.locked ? "lock" : "unlock")}</button></td>
      <td class="col-aud"><button class="chip seg-aud" data-i="${i}">${ico("audition")}试听</button></td>
      <td class="col-text"><input type="text" class="seg-text" data-i="${i}" value="${esc(seg.text)}" placeholder="输入转写文本…" title="修改后按回车确认生效；未回车失焦将放弃修改"></td>
      <td class="col-lang"><select class="seg-lang" data-i="${i}">
        ${["JP","ZH","EN"].map(l => `<option value="${l}" ${seg.language === l ? "selected" : ""}>${l}</option>`).join("")}
      </select></td>
      <td class="col-spk"><select class="seg-speaker" data-i="${i}">
        <option value="">未分配</option>
        ${state.characters.map(c => `<option value="${esc(c.id)}" ${seg.characterId === c.id ? "selected" : ""} style="color:${esc(c.color)}">${esc(c.name)}</option>`).join("")}
      </select></td>
      <td class="row-actions col-act">
        <button class="chip seg-jump" data-i="${i}">${ico("jump")}跳转</button>
        <button class="chip danger seg-del" data-i="${i}">${ico("delete")}删除</button>
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
    pushUndo("加入片段");
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
  // ── 区间变更后自动重识别字幕 ──────────────────────────────────────────
  // 改选区 / 合并片段后，旧文本与新区间不再对应 → 后台重跑 ASR 覆盖文本。
  // 连续改动合并成一次请求（1.2s 防抖）；quiet 任务，不锁界面（可继续操作）。
  const RETR_KEY = "vc.retranscribe.v1";
  let reTrTimer = null;
  const reTrQueue = new Map();   // "itemId|segId" -> { itemId, segId }：去重 + 保序
  function retranscribeEnabled() {
    try { return localStorage.getItem(RETR_KEY) !== "0"; } catch (e) { return true; }
  }
  function queueRetranscribe(pairs) {
    if (!retranscribeEnabled() || !pairs || !pairs.length) return;
    pairs.forEach(p => { if (p && p.itemId && p.segId) reTrQueue.set(p.itemId + "|" + p.segId, p); });
    if (reTrTimer) clearTimeout(reTrTimer);
    reTrTimer = setTimeout(flushRetranscribe, 1200);
  }
  async function flushRetranscribe() {
    reTrTimer = null;
    if (!reTrQueue.size) return;
    const byItem = new Map();
    reTrQueue.forEach(v => {
      if (!byItem.has(v.itemId)) byItem.set(v.itemId, []);
      byItem.get(v.itemId).push(v.segId);
    });
    reTrQueue.clear();
    const sel = $("#tr-model");
    const model = (sel && sel.value) || "medium";
    let n = 0;
    for (const [itemId, segIds] of byItem) {
      const segs = segsFor(itemId);
      // 以 segId 重新定位索引：防抖期间片段可能已被删除/重排，找不到就跳过
      const targets = segIds.map(id => ({ id, i: segs.findIndex(s => s.id === id) })).filter(t => t.i >= 0);
      if (!targets.length) continue;
      n += targets.length;
      pushUndo("重新识别字幕");   // 文本将被覆盖，可撤销回旧文本
      try {
        const j = await api("/api/transcribe", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ item_id: itemId, model,
            segments: targets.map(t => ({ start: segs[t.i].start, end: segs[t.i].end })) }) });
        trackTask(j.task_id, (result) => {
          const cur = segsFor(itemId);
          const texts = (result && result.texts) || [];
          let hit = 0;
          targets.forEach((t, k) => {
            const seg = cur.find(s => s.id === t.id);
            if (seg && texts[k] != null && String(texts[k]).trim()) { seg.text = String(texts[k]).trim(); hit++; }
          });
          if (hit) { scheduleSaveProject(itemId); renderSegments(); }
          toast(hit ? `已重新识别 ${hit} 段字幕` : "字幕识别未返回文本");
          return true;   // 已自定义提示，抑制默认「任务完成」
        }, { quiet: true });
      } catch (e) {
        toast("字幕重新识别启动失败: " + e.message, 5000);
      }
    }
    if (n) toast(`正在重新识别 ${n} 段字幕…`, 2500);
  }

  // 把当前工作选区写回聚焦片段（activeSeg）——修改识别片段的起止：
  // 点击片段行聚焦 → 波形上拖手柄/键盘微调选区 → 此处写回
  function applySelectionToActive() {
    const a = state.activeSeg;
    if (!a) return toast("请先点击片段行聚焦要修改的片段");
    if (!state.selection) return toast("请先在波形上调整出目标区间");
    const seg = segsFor(a.itemId).find(s => s.id === a.segId);
    if (!seg) return toast("聚焦片段不存在（可能已被删除）");
    const item = (state.items || []).find(x => x.id === a.itemId);
    let { start, end } = state.selection;
    const dur = item ? (item.duration || 0) : 0;   // clamp 到素材时长
    if (dur > 0) { start = Math.max(0, Math.min(start, dur)); end = Math.max(0, Math.min(end, dur)); }
    if (end - start < 0.01) return toast("选区过短，无法应用");
    if (Math.abs(start - seg.start) < 0.005 && Math.abs(end - seg.end) < 0.005)
      return toast("选区与片段区间一致，无需修改");
    pushUndo("修改片段选区");
    seg.start = +start.toFixed(3);
    seg.end = +end.toFixed(3);
    scheduleSaveProject(a.itemId);
    renderSegments();
    if (refreshSelColor) refreshSelColor();   // 写回后选区与片段一致 → 黄色回蓝
    queueRetranscribe([{ itemId: a.itemId, segId: seg.id }]);   // 区间变了 → 重识别文本
    toast(`已更新片段区间 ${fmtT(seg.start)} ~ ${fmtT(seg.end)}`);
  }

  // 合并选中的多个片段 → 一段（把被自动切分拆开的句子拼回完整句子）
  // 规则：仅同素材内可合并；起点取最早、终点取最晚；文本按时间顺序拼接；人工合并 → 锁定
  function mergeSegments(segIds) {
    const ids = Array.from(segIds || []);
    if (ids.length < 2) return toast("请先多选至少 2 个片段再右键合并（Ctrl/Shift 点选，或 Ctrl+A 全选）");
    const idSet = new Set(ids);
    let found = [];
    (state.items || []).forEach(item => {
      (segsFor(item.id) || []).forEach(seg => { if (idSet.has(seg.id)) found.push({ item, seg }); });
    });
    if (found.length < 2) return toast("选中的片段不足 2 段（可能已被删除）");
    const items = new Set(found.map(f => f.item.id));
    if (items.size > 1) return toast("只能合并同一素材内的片段（跨素材时间轴不同）");
    const item = found[0].item;
    const segs = segsFor(item.id);
    found.sort((a, b) => a.seg.start - b.seg.start);
    const start = Math.min(...found.map(f => f.seg.start));
    const end = Math.max(...found.map(f => f.seg.end));
    const first = found[0].seg;
    const joiner = (first.language === "EN") ? " " : "";   // 英文加空格，中日文直接相连
    const text = found.map(f => (f.seg.text || "")).filter(Boolean).join(joiner);
    pushUndo("合并片段");
    first.start = +start.toFixed(3);
    first.end = +end.toFixed(3);
    first.text = text;
    first.q = null;          // 区间变了，旧清晰度分数失效
    first.locked = true;     // 人工合并 → 锁定，自动切分/识别不再拆回
    const delIds = new Set(found.slice(1).map(f => f.seg.id));
    const kept = segs.filter(s => !delIds.has(s.id));
    segs.splice(0, segs.length, ...kept);
    delIds.forEach(id => state.selectedSegs.delete(id));
    if (state.activeSeg && delIds.has(state.activeSeg.segId)) state.activeSeg = { itemId: item.id, segId: first.id };
    scheduleSaveProject(item.id);
    renderSegments();
    queueRetranscribe([{ itemId: item.id, segId: first.id }]);   // 合并成长句 → 整句重识别
    toast(`已合并 ${found.length} 段 → ${fmtT(start)} ~ ${fmtT(end)}（${(end - start).toFixed(1)}s）`);
  }

  // 手动锁定 / 解锁片段：locked 是「人工成果」标记。后端尊重它的路径（已逐条核对）：
  //   align_segments_to_speech 跳过 locked（对齐发音）
  //   speakers.bind_segments / rescan 不重绑不拆分 locked（声纹反馈 / 识别改写 / 空白区补扫）
  //   _reset_project_pool 重新识别时保留文本与 locked
  // 例外：按静音自动切分（/autosplit）是整表替换，不读 locked —— 但它前端有 confirm
  // 与 pushUndo 兜底，属显式破坏性操作。
  // 此前 locked 只在人工编辑文本、指派说话人、重定向、合并时自动置位，
  // **而且没有任何办法解除**：用户既不能主动保护一个还没被人碰过的片段，也不能解锁交给自动流程重跑。
  // segIds: 单个 id 或数组；force: true=全部锁定 / false=全部解锁 / 省略=按当前状态取反
  // （有任一未锁定 → 全锁；全部已锁定 → 全解）。返回 { count, locked }。
  function toggleLock(segIds, force) {
    const ids = new Set(Array.isArray(segIds) ? segIds : [segIds]);
    if (!ids.size || (ids.size === 1 && ![...ids][0])) { toast("请先选择片段（点击行 / Ctrl 多选 / Ctrl+A 全选）"); return { count: 0, locked: null }; }
    const rows = allSegs().filter(r => ids.has(r.seg.id));
    if (!rows.length) { toast("选中的片段不存在（可能已被删除）"); return { count: 0, locked: null }; }
    const target = (force === true || force === false) ? force : !rows.every(r => r.seg.locked);
    const hit = rows.filter(r => !!r.seg.locked !== target);
    if (!hit.length) {
      toast(target ? "所选片段已全部锁定" : "所选片段已全部解锁");
      return { count: 0, locked: target };
    }
    pushUndo(target ? "锁定片段" : "解锁片段");
    const touched = new Set();
    hit.forEach(({ item, seg }) => { seg.locked = target; if (item) touched.add(item.id); });
    touched.forEach(id => scheduleSaveProject(id));
    renderSegments();
    toast(`${target ? "已锁定" : "已解锁"} ${hit.length} 段` +
      (target ? "（对齐 / 补扫 / 识别将跳过它）" : "（自动流程可以再改动了）"), 3500);
    return { count: hit.length, locked: target };
  }

  function deleteSegment(itemId, i) {    const segs = segsFor(itemId);
    const s = segs[i];
    if (s) state.selectedSegs.delete(s.id);
    pushUndo("删除片段");
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
    // 循环试听：到终点自动回卷重播。带上 segId/itemId —— app.js 判断"点的是不是
    // 正在试听的那一段"（点别的片段的行内控件要解除试听，点同一段则保留
    // "循环听 + 顺手改文本"的工作流）。
    state.auditioning = { start: seg.start, end: seg.end, itemId: item.id, segId: seg.id, loop: true };
    scrollSegRow(item.id, seg.id);
    toast("循环试听该片段（空格 / 暂停键停止，或再点一次「循环」退出）", 2500);
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

  // 当前筛选/排序下可见片段的 id 列表（Ctrl+A 全选范围 = 所见即所选）
  function viewSegIds() {
    return _viewRows(allSegs()).map(r => r.seg.id);
  }

  return { segsFor, segIssues, allSegs, charSegs, updateSegBadge, renderSegments, viewSegIds,
           addSegmentFromSelection, applySelectionToActive, mergeSegments, deleteSegment, jumpToSegment, auditionSegment,
           toggleLock,
           gotoEditAndPlay, scrollSegRow, auditionFocus,
           queueRetranscribe, retranscribeEnabled };
}
