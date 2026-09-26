import WaveSurfer from "/static/vendor/wavesurfer.esm.js";
import Timeline from "/static/vendor/plugins/timeline.esm.js";
import Regions from "/static/vendor/plugins/regions.esm.js";
import Minimap from "/static/vendor/plugins/minimap.esm.js";

import { $, $$, esc, shortName, fmtT, fmtSel, fmtDur, api, clampN, evtEl, closestEl, LS_PROJECT, ico,
         SEG_MIN, SEG_MAX, SEEK_STEP, SEEK_FAST, NUDGE_STEP, VOL_STEP, CHAR_PALETTE } from "/static/js/util.js";
import { state, toast, layout, applyLayout, saveLayout, resetLayout,
         togglePanel, swapPanels, initWorkspace, PANELS } from "/static/js/state.js";
import { createTraining } from "/static/js/modules/training.js";
import { createSubtitles } from "/static/js/modules/subtitles.js";
import { createIo } from "/static/js/modules/io.js";
import { createWaveform } from "/static/js/modules/waveform.js";
import { createSegments } from "/static/js/modules/segments.js";
import { createPool } from "/static/js/modules/pool.js";
import { createTasks } from "/static/js/modules/tasks.js";
import { createStore } from "/static/js/modules/store.js";
import { createProjects } from "/static/js/modules/projects.js";

// VoiceCut 前端 — wavesurfer v7 (UMD) + Flask REST
(() => {
  "use strict";



  // ── 引导 ───────────────────────────────────────────────
  function setBoot(color, msg) {
    const el = $("#boot-state");
    el.classList.remove("lamp-green", "lamp-red");
    if (color) el.classList.add("lamp-" + color);
    el.title = msg;
    el.querySelector(".lamp-msg").textContent = msg;
  }
  function fail(msg) {
    state.bootErr = msg;
    document.body.dataset.vc = "error";
    setBoot("red", msg);
  }
  async function boot() {
    if (typeof WaveSurfer === "undefined") return fail("wavesurfer 未加载");
    try {
      const cfg = await api("/api/config");
      const projList = await api("/api/projects");
      state.projects = projList;
      projects.renderProjectSelect();
      let pid = null;
      try { pid = localStorage.getItem(LS_PROJECT); } catch (e) {}
      const proj = projList.find(p => p.id === pid) || projList[0] || null;
      if (proj) await projects.selectProject(proj);
      setBoot("green", "后端 OK · ffmpeg: " + cfg.ffmpeg.split(/[\\/]/).pop());
      document.body.dataset.vc = "ok";
    } catch (e) { return fail("后端连接失败: " + e.message); }
  }

  let focusedSeg = null;
  function setSegFocus(tr) {
    $$(".seg-row").forEach(r => r.style.outline = "");
    if (tr) { tr.style.outline = "1px solid var(--accent)"; focusedSeg = { item: tr.dataset.item, i: Number(tr.dataset.i) }; }
    else focusedSeg = null;
  }

  // 片段列表：点击/多选（Shift 区间、Ctrl 追加），右键重定向角色
  // 筛选/排序控件（只影响展示顺序与可见性，行 data-i 仍是原始索引）
  let _segFilterTimer = 0;
  $("#seg-filter-text").addEventListener("input", (e) => {
    clearTimeout(_segFilterTimer);
    _segFilterTimer = setTimeout(() => {
      state.segFilter.text = evtEl(e).value || "";
      segments.renderSegments();
    }, 200);
  });
  $("#seg-filter-status").addEventListener("change", (e) => {
    state.segFilter.status = evtEl(e).value || "all";
    segments.renderSegments();
  });
  $("#seg-filter-item").addEventListener("change", (e) => {
    state.segFilter.item = evtEl(e).value || "all";
    segments.renderSegments();
  });
  // 表头排序箭头：点击列名循环 默认方向 → 升序 → 恢复时间序。
  // 箭头是 CSS mask 图标（.sort-arrow）：.on=当前排序列点亮，.desc=箭头翻转朝下。
  function updateSortArrows() {
    const cur = state.segSort || "time";
    const descDefault = { dur: 1, score: 1 };   // 时长/清晰度默认降序（最优在前），其余默认升序
    $$("#seg-table thead [data-sort]").forEach((el) => {
      const key = el.dataset.sort;
      const arrow = el.querySelector(".sort-arrow");
      const on = cur === key, asc = cur === key + "_asc";
      if (arrow) {
        const active = on || asc;
        const desc = on && !!descDefault[key];   // asc 分支恒为升序 → 朝上
        arrow.classList.toggle("on", active);
        arrow.classList.toggle("desc", active && desc);
      }
      el.classList.toggle("sort-on", on || asc);
    });
  }
  $("#seg-table thead").addEventListener("click", (e) => {
    if (e.target.closest("input, select")) return;   // 表头内筛选控件点击不触发排序
    const el = e.target.closest("[data-sort]");
    if (!el) return;
    const key = el.dataset.sort;
    const cur = state.segSort || "time";
    if (key === "time") state.segSort = "time";
    else if (cur === key) state.segSort = key + "_asc";
    else if (cur === key + "_asc") state.segSort = "time";
    else state.segSort = key;
    updateSortArrows();
    segments.renderSegments();
  });
  updateSortArrows();
  $("#seg-tbody").addEventListener("click", (e) => {
    const btn = evtEl(e).closest("button");
    const tr = closestEl(evtEl(e), "tr.seg-row");
    if (!tr) return;
    const itemId = tr.dataset.item;
    const i = Number(tr.dataset.i);
    const segs = segments.segsFor(itemId);
    const seg = segs[i];
    if (!seg) return;
    if (btn) {
      if (btn.classList.contains("seg-aud")) { segments.auditionSegment(state.items.find(x => x.id === itemId), seg); return; }
      else if (btn.classList.contains("seg-jump")) { waveform.focusSegment(state.items.find(x => x.id === itemId), seg); return; }
      else if (btn.classList.contains("seg-del")) { segments.deleteSegment(itemId, i); return; }
      // 锁定开关：多选时作用于整个选区（与右键批量一致），否则只作用于本行
      else if (btn.classList.contains("seg-lock")) {
        const sel = Array.from(state.selectedSegs);
        const ids = (sel.length > 1 && sel.includes(seg.id)) ? sel : [seg.id];
        segments.toggleLock(ids);
        return;
      }
    }
    // 行内编辑控件（文本框/语言/说话人下拉）：不触发行选中——否则 renderSegments 重建行，
    // 正在交互的控件被替换，下拉打不开/change 丢失（曾导致说话人无法更改）
    if (evtEl(e).closest("input, select")) {
      // 但点到**别的片段**的行内控件时必须解除残留的循环试听：试听循环
      // （state.auditioning.loop）会不停把播放头拽回试听片段，用户表现为
      // "点了其他片段却锁死在试听片段、退不出循环"。同一段的控件不动 ——
      // 保留"循环听这一段 + 顺手改它的文本"的工作流。
      if (state.auditioning && state.auditioning.loop && state.auditioning.segId !== seg.id) {
        waveform.stopAudition();
      }
      return;
    }
    e.preventDefault();
    const rows = segments.allSegs();
    const flatIdx = rows.findIndex(r => r.seg.id === seg.id);
    if (e.shiftKey && state.selectedSegs.size) {
      let first = 1e9;
      rows.forEach((r, k) => { if (state.selectedSegs.has(r.seg.id)) first = Math.min(first, k); });
      const lo = Math.min(first, flatIdx), hi = Math.max(first, flatIdx);
      state.selectedSegs = new Set();
      for (let k = lo; k <= hi; k++) state.selectedSegs.add(rows[k].seg.id);
    } else if (e.ctrlKey || e.metaKey) {
      if (state.selectedSegs.has(seg.id)) state.selectedSegs.delete(seg.id);
      else state.selectedSegs.add(seg.id);
    } else {
      state.selectedSegs = new Set([seg.id]);
      waveform.focusSegment(state.items.find(x => x.id === itemId), seg); // 点击行定位：跳转波形 + 行高亮
    }
    setSegFocus(tr);
    segments.renderSegments();
  });
  // 片段文本框：草稿式编辑——自动识别内容防误改，输入仅草稿，必须回车确认才落数据；
  // 未回车就失焦（点别处 / Esc）= 放弃修改，恢复原文本
  $("#seg-tbody").addEventListener("input", (e) => {
    const el = evtEl(e);
    const tr = closestEl(el, "tr.seg-row");
    if (!tr) return;
    if (!el.classList.contains("seg-text")) return;
    el.dataset.dirty = "1";
    segments.updateSegBadge(tr.dataset.item, Number(el.dataset.i), el.value);   // 草稿实时校验预览
  });
  // 片段文本框内 Enter = 确认草稿并退出编辑（焦点交还页面，快捷键恢复）
  $("#seg-tbody").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.classList && e.target.classList.contains("seg-text")) {
      e.preventDefault();
      e.target.dataset.commit = "1";
      e.target.blur();
    }
  });
  // focusout（blur 不冒泡）：有 commit 标记 = 回车确认 → 写入片段；否则放弃草稿恢复原文本
  $("#seg-tbody").addEventListener("focusout", (e) => {
    const el = evtEl(e);
    if (!el.classList || !el.classList.contains("seg-text") || !el.dataset.dirty) return;
    const tr = closestEl(el, "tr.seg-row");
    if (!tr) return;
    const itemId = tr.dataset.item, i = Number(el.dataset.i);
    const segs = segments.segsFor(itemId);
    if (!segs[i]) return;
    const commit = el.dataset.commit === "1";
    delete el.dataset.commit; delete el.dataset.dirty;
    if (commit) {
      const nv = el.value;
      if (nv !== segs[i].text) {
        store.pushUndo("编辑文本");
        segs[i].text = nv; segs[i].locked = true;   // 人工确认 → 锁定，自动识别不再覆盖
        store.scheduleSaveProject(itemId);
      }
    } else {
      el.value = segs[i].text;                       // 未回车 → 丢弃草稿
    }
    segments.updateSegBadge(itemId, i);
  });
  $("#seg-tbody").addEventListener("change", (e) => {
    const el = evtEl(e);
    const tr = closestEl(el, "tr.seg-row");
    if (!tr) return;
    const itemId = tr.dataset.item;
    const i = Number(el.dataset.i);
    const segs = segments.segsFor(itemId);
    if (!segs[i]) return;
    delete el.dataset.undoed;
    if (!el.classList.contains("seg-text")) store.pushUndo("修改片段属性");
    if (el.classList.contains("seg-lang")) { segs[i].language = el.value; store.scheduleSaveProject(itemId); }
    if (el.classList.contains("seg-speaker")) {
      segs[i].characterId = el.value || null;
      segs[i].locked = true;   // 人工指派 → 锁定
      store.scheduleSaveProject(itemId); segments.renderSegments();
      // 声纹反馈：修正样本并入角色质心并静默更新其他片段
      if (el.value) pool.sendCharacterFeedback([{ item_id: itemId, seg_id: segs[i].id,
        character_id: el.value, start: segs[i].start, end: segs[i].end }]);
    }
  });
  function showRedirectMenu(x, y, segIds) {
    const menu = $("#redirect-menu");
    menu.innerHTML = "";
    menu.style.left = x + "px"; menu.style.top = y + "px";
    const mk = (label, val, color) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (color) b.style.color = color;
      b.addEventListener("click", () => { hideRedirectMenu(); pool.reassignSegments(segIds, val); });
      return b;
    };
    // 锁定 / 解锁：批量作用于右键命中的选区（有未锁定的就给「锁定」，有已锁定的就给「解锁」）
    const lockRows = segments.allSegs().filter(r => segIds.includes(r.seg.id));
    if (lockRows.length) {
      const mkLock = (label, val, title) => {
        const b = document.createElement("button");
        b.className = "menu-lock";
        b.innerHTML = ico(val ? "lock" : "unlock") + label;
        b.title = title;
        b.addEventListener("click", () => { hideRedirectMenu(); segments.toggleLock(segIds, val); });
        return b;
      };
      if (lockRows.some(r => !r.seg.locked)) {
        menu.appendChild(mkLock(`锁定 ${lockRows.length} 段`, true,
          "标记为人工成果：自动切分 / 补扫 / 声纹识别改写都会跳过"));
      }
      if (lockRows.some(r => r.seg.locked)) {
        menu.appendChild(mkLock(`解锁 ${lockRows.length} 段`, false,
          "解除人工标记：自动补扫 / 切分 / 识别可以再改动它"));
      }
      const lsep = document.createElement("div");
      lsep.className = "menu-sep";
      menu.appendChild(lsep);
    }
    if (segIds.length >= 2) {
      // 多选合并：把被自动切分拆开的句子拼回一段（仅同素材内，按时间顺序拼接文本）
      const mb = document.createElement("button");
      mb.className = "menu-merge";
      mb.innerHTML = ico("merge") + `合并 ${segIds.length} 段`;
      mb.title = "把选中的片段拼成一段（起点取最早、终点取最晚，文本按时间顺序拼接）";
      mb.addEventListener("click", () => { hideRedirectMenu(); segments.mergeSegments(segIds); });
      menu.appendChild(mb);
      const sep = document.createElement("div");
      sep.className = "menu-sep";
      menu.appendChild(sep);
    }
    menu.appendChild(mk("未分配", "", ""));
    state.characters.forEach(c => menu.appendChild(mk(c.name, c.id, c.color)));
    menu.classList.remove("hidden");
  }
  function hideRedirectMenu() { const m = $("#redirect-menu"); if (m) m.classList.add("hidden"); }
  document.addEventListener("click", hideRedirectMenu);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideRedirectMenu(); });
  $("#segments-panel").addEventListener("contextmenu", (e) => {
    const tr = closestEl(evtEl(e), "tr.seg-row");
    if (!tr) return;
    e.preventDefault();
    const itemId = tr.dataset.item;
    const i = Number(tr.dataset.i);
    const segs = segments.segsFor(itemId);
    let ids = Array.from(state.selectedSegs).filter(id => segments.allSegs().some(r => r.seg.id === id));
    if (!ids.length && segs[i]) ids = [segs[i].id];
    showRedirectMenu(e.clientX, e.clientY, ids);
  });

  // ── 通用守卫 ───────────────────────────────────────────
  function needItem() { if (!state.currentItem) { toast("请先选择素材"); return false; } return true; }

  // ── 弹窗 ───────────────────────────────────────────────
  function showModal(sel) { $(sel).classList.remove("hidden"); }
  function hideModal(sel) { $(sel).classList.add("hidden"); }
  function showResult(title, text, html, width) {
    $("#result-title").textContent = title;
    const body = $("#result-body");
    body.textContent = text;
    if (html) {
      const div = document.createElement("div");
      div.className = "modal-actions";
      div.style.justifyContent = "flex-start";
      div.innerHTML = html;
      body.insertAdjacentElement("afterend", div);
      body.dataset.extra = "1";
    }
    showModal("#modal-result");
  }

  // 菜单
  function setupMenus() {
    $$(".menu-title").forEach((bt) => {
      bt.addEventListener("click", (e) => {
        e.stopPropagation();
        const m = bt.parentElement;
        const was = m.classList.contains("open");
        $$(".menu").forEach(x => x.classList.remove("open"));
        if (!was) m.classList.add("open");
      });
    });
    document.addEventListener("click", () => $$(".menu").forEach(x => x.classList.remove("open")));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") $$(".menu").forEach(x => x.classList.remove("open")); });

    const actions = {
      "import": () => io.importDialog(),
      "bilibili": () => showModal("#modal-bilibili"),
      "url-open": () => showModal("#modal-bilibili"),
      "project-new": () => projects.createProject(),
      "project-rename": () => projects.renameProject(),
      "project-delete": () => projects.deleteProject(),
      "pool": () => pool.openPool(),
      "identify-speakers": () => pool.doIdentifySpeakers(),
      "export-selection": () => io.openExportModal(),
      "denoise": () => io.doDenoise(),
      "separate": () => io.doSeparate(),
      "trim": () => io.doTrim(),
      "transcribe": () => io.openTranscribeModal(),
      "dataset-export": () => io.openDatasetModal(),
      "quality-scan": () => io.doQualityScan(),
      "align-speech": () => io.doAlignSpeech(),
      "autosplit": () => showModal("#modal-autosplit"),
      "undo": doUndo,
      "redo": doRedo,
      "validate": segments.renderSegments,
      "zoom-in": () => waveform.zoomIn(),
      "zoom-out": () => waveform.zoomOut(),
      "fit": () => waveform.zoomSet(0),
      "zoom-sel": () => { if (state.selection) { waveform.zoomSet(0); state.ws.setTime(state.selection.start); } },
      "toggle-minimap": () => $("#minimap-wrap").classList.toggle("hidden"),
      "panel-toggle": (b) => togglePanel(b.dataset.panel),
      "layout-reset": resetLayout,
      "help": () => showModal("#modal-help"),
    };
    /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll("[data-act]")).forEach((b) => {
      b.addEventListener("click", () => { const f = actions[b.dataset.act]; if (f) f(b); });
    });
  }

  // ── 撤销/重做包装 ───────────────────────────────────────
  // 撤销/重做会改变片段区间（如"修改片段选区"），选区与片段可能重新变得不一致
  // → 必须刷新选区颜色，否则仍显示蓝色但实际已偏离（黄/蓝与实际状态脱节）
  function doUndo() { store.undo(); if (waveform && waveform.refreshSelColor) waveform.refreshSelColor(); }
  function doRedo() { store.redo(); if (waveform && waveform.refreshSelColor) waveform.refreshSelColor(); }

  // ── 快捷键 ─────────────────────────────────────────────
  function setupShortcuts() {
    window.addEventListener("keydown", (e) => {
      // Ctrl+A 全选/取消全选片段：必须放在所有守卫（含 input/textarea 早退）之前，
      // 否则焦点在片段文本框里时会漏给浏览器原生全选。范围为当前筛选下可见的片段
      // （所见即所选，避免误删被过滤隐藏的行）；输入框内选文本请用双击/拖选。
      if ((e.ctrlKey || e.metaKey) && (e.key === "a" || e.key === "A")) {
        e.preventDefault();
        const ids = segments.viewSegIds();
        if (!ids.length) return toast("当前列表没有可选择的片段");
        const allIn = ids.every(id => state.selectedSegs.has(id));
        if (allIn && state.selectedSegs.size) {
          ids.forEach(id => state.selectedSegs.delete(id));
          toast("已取消全选（可见范围）");
        } else {
          ids.forEach(id => state.selectedSegs.add(id));
          toast(`已全选 ${ids.length} 段（可见范围）——Delete 批量删除 / K 锁定或解锁 / 右键批量操作`, 4000);
        }
        segments.renderSegments();
        return;
      }
      // Esc：输入框/下拉/复选框内 = 退出编辑（失焦，快捷键恢复）；页面空白处 = 取消当前选区
      if (e.key === "Escape") {
        const tg = (/** @type {HTMLElement} */ (e.target).tagName || "").toLowerCase();
        if (tg === "input" || tg === "textarea" || tg === "select") { e.target.blur(); return; }
        if (state.selection || state.multiRegions.length) { waveform.clearSelection(); toast("已取消选区"); }
        return;
      }
      // Ctrl/Cmd + +/-/0: 屏蔽浏览器页面缩放，改为时间轴缩放
      if (e.ctrlKey || e.metaKey) {
        const k = e.key;
        if (k === "+" || k === "=" || k === "Add" || k === "NumpadAdd") { e.preventDefault(); waveform.zoomIn(); return; }
        if (k === "-" || k === "Subtract" || k === "NumpadSubtract") { e.preventDefault(); waveform.zoomOut(); return; }
        if (k === "0") { e.preventDefault(); waveform.zoomSet(0); return; }
        // Ctrl+O 导入：不因焦点在输入框内而漏给浏览器（Chrome 会弹打开文件）
        if (k === "o" || k === "O") { e.preventDefault(); io.importDialog(); return; }
        // Ctrl+Enter：把当前选区写回聚焦片段（修改识别片段起止）
        if (k === "Enter") { e.preventDefault(); segments.applySelectionToActive(); return; }
      }
      const tag = (/** @type {HTMLElement} */ (e.target).tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        if (e.shiftKey) doRedo(); else doUndo();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === "y" || e.key === "Y")) { e.preventDefault(); doRedo(); return; }

      // 小键盘快进：−/+快退/快进 15 秒；数字区方向键(2/4/6/8)等效主方向键（兼容 NumLock 开关）
      const code = e.code || "";
      if (code === "NumpadSubtract") { e.preventDefault(); waveform.seekBy(-SEEK_FAST); return; }
      if (code === "NumpadAdd") { e.preventDefault(); waveform.seekBy(SEEK_FAST); return; }
      if (code === "Numpad4" || code === "Numpad6") { e.preventDefault(); waveform.seekBy(code === "Numpad6" ? SEEK_STEP : -SEEK_STEP); return; }
      if (code === "Numpad8" || code === "Numpad2") { e.preventDefault(); waveform.adjVolume(code === "Numpad8" ? VOL_STEP : -VOL_STEP); return; }

      switch (e.key) {
        case " ": e.preventDefault(); waveform.togglePlay(); break;
        // Enter：有"未确认"的黄色选区时直接确认写回（等价 Ctrl+Enter 与「应用选区」按钮）；
        // 焦点在文本框内时走上面的早退守卫，仍是"提交文本"语义，不冲突。
        case "Enter":
          if (waveform.selPending && waveform.selPending()) {
            e.preventDefault();
            segments.applySelectionToActive();
          }
          break;
        case "l": case "L": waveform.toggleLoop(); break;
        case "e": case "E": io.openExportModal(); break;
        case "n": case "N": io.doDenoise(); break;
        case "v": case "V": io.doSeparate(); break;
        // K：锁定 / 解锁片段。有选中 → 整个选区；否则 → 当前聚焦片段（点击行 / 跳转定位的那一段）
        case "k": case "K": {
          const sel = Array.from(state.selectedSegs);
          if (sel.length) segments.toggleLock(sel);
          else if (state.activeSeg && state.activeSeg.segId) segments.toggleLock([state.activeSeg.segId]);
          else toast("请先点击片段行（或 Ctrl 多选）再按 K");
          break;
        }
        case "ArrowLeft": case "ArrowRight": {
          e.preventDefault();
          const d = (e.key === "ArrowRight" ? 1 : -1) * NUDGE_STEP;
          if (e.ctrlKey) { (e.key === "ArrowRight" ? waveform.markForward() : waveform.unmarkLast()); } // Ctrl+→ 快进多选 / Ctrl+← 撤销上一段
          else if (e.shiftKey) waveform.nudgeSelection(d, "end");   // Shift+←→ 微调选区终点
          else if (e.altKey) waveform.nudgeSelection(d, "start");   // Alt+←→ 微调选区起点
          else if (state.selection && state.selectionRegion) waveform.nudgeSelection(d, "move"); // ←→ 平移整个选区
          else waveform.seekBy((e.key === "ArrowRight" ? 1 : -1) * SEEK_STEP);  // 无选区：快退 / 快进 5 秒
          break;
        }
        case "ArrowUp": case "ArrowDown": e.preventDefault(); waveform.adjVolume(e.key === "ArrowUp" ? VOL_STEP : -VOL_STEP); break;
        case "Delete":
          if (state.selectedSegs.size) {
            store.pushUndo("批量删除片段");
            (state.items || []).forEach(item => {
              const segs = segments.segsFor(item.id);
              const before = segs.length;
              const kept = segs.filter(s => !state.selectedSegs.has(s.id));
              if (kept.length !== before) { segs.splice(0, segs.length, ...kept); store.scheduleSaveProject(item.id); }
            });
            state.selectedSegs = new Set();
            segments.renderSegments();
          } else if (focusedSeg) {
            segments.deleteSegment(focusedSeg.item, focusedSeg.i);
            setSegFocus(null);
          }
          break;
      }
    });
  }

  // ── 拖拽导入 ───────────────────────────────────────────
  function setupDrop() {
    let dragCount = 0;
    window.addEventListener("dragenter", (e) => { e.preventDefault(); dragCount++; });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("dragleave", (e) => { e.preventDefault(); dragCount = Math.max(0, dragCount - 1); });
    window.addEventListener("drop", (e) => {
      e.preventDefault(); dragCount = 0;
      const files = Array.from(e.dataTransfer.files || []);
      files.forEach((f) => io.uploadFile(f));
    });
    $("#file-input").addEventListener("change", () => {
      const fi = /** @type {HTMLInputElement} */ ($("#file-input"));
      Array.from(fi.files || []).forEach((f) => io.uploadFile(f));
      fi.value = "";
    });
  }

  // ── 事件绑定 ───────────────────────────────────────────
  function bindUI() {
    // 下拉/复选框/取色器用完立即失焦：防止焦点滞留导致空格、单键快捷键被控件原生行为劫持
    document.addEventListener("change", (e) => {
      const t = e.target;
      if (!t || t.nodeType !== 1) return;
      const tag = t.tagName.toLowerCase();
      if (tag === "select" || t.type === "checkbox" || t.type === "color" || t.type === "radio") t.blur();
    }, true);
    ["#project-select", "#project-select2"].forEach((psSel) => {
      const ps = /** @type {HTMLSelectElement|null} */ ($(psSel));
      if (ps) ps.addEventListener("change", () => {
        const proj = state.projects.find(pp => pp.id === ps.value);
        if (proj && state.currentProject && proj.id !== state.currentProject.id) projects.selectProject(proj);
      });
    });
    $("#btn-import").addEventListener("click", () => io.importDialog());
    $("#btn-bilibili").addEventListener("click", () => showModal("#modal-bilibili"));
    $("#btn-export-dataset").addEventListener("click", () => io.openDatasetModal());
    $("#btn-play2").addEventListener("click", () => waveform.togglePlay());
    $("#btn-prev").addEventListener("click", () => state.ws && state.ws.setTime(0));
    $("#btn-next").addEventListener("click", () => state.ws && state.ws.setTime(state.currentItem ? state.currentItem.duration : 0));
    $("#btn-loop").addEventListener("click", () => waveform.toggleLoop());
    $("#btn-play-selection").addEventListener("click", () => waveform.playSelection());
    $("#btn-export-selection").addEventListener("click", () => io.openExportModal());
    $("#minimap-toggle").addEventListener("change", (e) => $("#minimap-wrap").classList.toggle("hidden", !evtEl(e).checked));
    $("#tr-auto").addEventListener("change", (e) => {   // 改选区/合并后自动重识别字幕（持久化开关）
      try { localStorage.setItem("vc.retranscribe.v1", evtEl(e).checked ? "1" : "0"); } catch (_) {}
    });
    $("#btn-add-seg").addEventListener("click", segments.addSegmentFromSelection);
    $("#btn-apply-seg").addEventListener("click", segments.applySelectionToActive);
    $("#btn-clear-segs").addEventListener("click", () => {
      if (!state.currentItem) return;
      if (confirm("清空当前素材的全部片段？")) {
        store.pushUndo("清空片段");
        segments.segsFor(state.currentItem.id).length = 0;
        state.selectedSegs = new Set();
        store.scheduleSaveProject(); segments.renderSegments();
      }
    });
    $("#btn-transcribe").addEventListener("click", () => io.openTranscribeModal());
    $("#btn-pool").addEventListener("click", () => pool.openPool());
    $("#btn-identify-speakers").addEventListener("click", () => pool.doIdentifySpeakers());
    $("#btn-reidentify-speakers").addEventListener("click", () => pool.doIdentifySpeakers(true));
    $("#btn-auto-analyze").addEventListener("click", () => pool.toggleAutoAnalyze());
    $("#btn-auto-analyze2").addEventListener("click", () => pool.toggleAutoAnalyze());
    $("#btn-auto-train").addEventListener("click", () => pool.toggleAutoTraining());
    $("#btn-auto-train2").addEventListener("click", () => pool.toggleAutoTraining());
    $("#pool-back").addEventListener("click", () => pool.closePool());
    $("#pool-close").addEventListener("click", () => pool.closePool());
    $("#pool-new").addEventListener("click", () => pool.createPoolCharacter());
    $("#pool-merge").addEventListener("click", () => pool.mergePoolSelected());
    $("#pool-identify").addEventListener("click", () => pool.doIdentifySpeakers());
    $("#pool-reidentify").addEventListener("click", () => pool.doIdentifySpeakers(true));
    $("#pool-gapscan").addEventListener("click", () => pool.scanGaps());
    $("#btn-auto-gapscan").addEventListener("click", () => pool.toggleAutoGapScan());
    $("#btn-auto-gapscan2").addEventListener("click", () => pool.toggleAutoGapScan());

    $("#bb-open").addEventListener("click", () => io.doUrlOpen());
    $("#tr-start").addEventListener("click", () => io.doTranscribe());
    $("#ds-start").addEventListener("click", () => io.doDatasetExport());
    $("#as-start").addEventListener("click", () => io.doAutosplit());
    $("#ex-start").addEventListener("click", () => io.doExportSelection());
    // 视频 ↔ 音频双向联动 / Ctrl+滚轮时间轴缩放（波形模块内绑定）
    waveform.bindVideoPreview();
    waveform.bindZoomWheel();

    $$(".modal-mask").forEach((mask) => mask.addEventListener("click", (e) => {
      if (e.target === mask || e.target.closest("[data-close]")) mask.classList.add("hidden");
    }));
    $("#bb-url").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); io.doUrlOpen(); } });
    // 实时字幕
    $("#btn-sub-open").addEventListener("click", () => $("#sub-file").click());
    $("#btn-sub-generate").addEventListener("click", subtitles.generateSubs);
    $("#btn-sub-add").addEventListener("click", subtitles.addCurrentSubToSegments);
    $("#sub-file").addEventListener("change", () => {
      const fi = /** @type {HTMLInputElement} */ ($("#sub-file"));
      const f = fi.files && fi.files[0];
      if (f) subtitles.uploadSubFile(f);
      fi.value = "";
    });
    $("#sub-tbody").addEventListener("click", (e) => {
      const btn = evtEl(e).closest("button");
      const tr = closestEl(evtEl(e), "tr.sub-row");
      if (!tr) return;
      const i = Number(tr.dataset.i);
      if (btn && btn.classList.contains("sub-sel")) subtitles.selectSubRange(i);
      else if (btn && btn.classList.contains("sub-add")) subtitles.addSubToSegments(i);
      else subtitles.selectSubRange(i);
    });
    // 波形区域内右键取消选区（波形模块内绑定）
    waveform.bindWaveBox();
    waveform.bindTimelineSelection();
  }


  // ── 页面切换（素材库 / 剪辑 / 训练交付） ──
  let training = null;   // 训练交付页模块实例（启动区由 createTraining 创建）
  let subtitles = null;  // 实时字幕模块实例（启动区由 createSubtitles 创建）
  let io = null;         // 输入/输出模块实例（启动区由 createIo 创建）
  let waveform = null;   // 波形/播放模块实例（启动区由 createWaveform 创建）
  let segments = null;   // 片段列表模块实例（启动区由 createSegments 创建）
  let store = null;      // 数据层模块实例（启动区由 createStore 创建，最先）
  let projects = null;   // 素材库/项目模块实例（启动区由 createProjects 创建）
  let tasks = null;      // 任务跟踪模块实例（启动区由 createTasks 创建）
  let pool = null;       // 角色池模块实例（启动区由 createPool 创建）
  const PAGE_KEY = "vc.page.v1";
  let currentPage = "edit";
  try { const saved = localStorage.getItem(PAGE_KEY); if (["edit", "media", "train"].includes(saved)) currentPage = saved; } catch (e) {}

  function setPage(name) {
    currentPage = name;
    document.body.dataset.page = name;   // 供 CSS 做页面上下文显隐（工具栏/菜单项）
    try { localStorage.setItem(PAGE_KEY, name); } catch (e) {}
    $$("#page-edit, #page-media, #page-train").forEach(pp => pp.classList.add("hidden"));
    const el = document.getElementById("page-" + name);
    if (el) el.classList.remove("hidden");
    $$("#pagebar .page-btn").forEach(b => b.classList.toggle("active", b.dataset.page === name));
    if (name === "media") { projects.renderMediaList(); projects.renderProjectSelect(); }
    if (name === "train" && training) training.loadTraining(true);
  }
  function setupPagebar() {
    $$("#pagebar .page-btn").forEach(b => b.addEventListener("click", () => setPage(b.dataset.page)));
    const b2 = $("#btn-import2"); if (b2) b2.addEventListener("click", () => io.importDialog());
    const u2 = $("#btn-url2"); if (u2) u2.addEventListener("click", () => showModal("#modal-bilibili"));
    const p2 = $("#btn-pool2"); if (p2) p2.addEventListener("click", () => pool.openPool());
    const rf = $("#btn-train-refresh"); if (rf) rf.addEventListener("click", () => training && training.loadTraining(true));
    setPage(currentPage);
  }

  // ── 启动 ───────────────────────────────────────────────
  store = createStore({ api, toast, state, CHAR_PALETTE,
    segsFor: (id) => segments.segsFor(id),
    renderSegments: () => segments.renderSegments(),
    renderPool: () => pool.renderPool() });
  projects = createProjects({ $, esc, ico, fmtDur, toast, api, state, LS_PROJECT,
    loadProject: store.loadProject, fillItemStates: store.fillItemStates, saveProjectNow: store.saveProjectNow,
    savePoolNow: store.savePoolNow, setPage,
    selectItem: (item) => waveform.selectItem(item),
    resetWaveUI: () => { waveform.updatePlayUI(); waveform.updateSelUI(); waveform.updateTransport(); },
    renderPool: () => pool.renderPool(),
    renderAutoAnalyzeBtn: () => pool.renderAutoAnalyzeBtn(),
    renderAutoTrainingBtn: () => pool.renderAutoTrainingBtn(),
    renderAutoGapScanBtn: () => pool.renderAutoGapScanBtn(),
    renderSegments: () => segments.renderSegments(), renderSubs: () => subtitles.renderSubs(),
    attachActiveTasks: () => tasks.attachActiveTasks() });
  tasks = createTasks({ $, api, esc, ico, toast, state, pushUndo: store.pushUndo,
    renderMediaList: projects.renderMediaList, refreshItems: projects.refreshItems,
    loadAllItemData: projects.loadAllItemData,
    renderPool: () => pool.renderPool(), renderSegments: () => segments.renderSegments(),
    renderSubs: () => subtitles.renderSubs(),
    // 后台自动识别完成后自动补扫空白区（pool 晚于 tasks 创建 → 闭包注入）
    afterAnalyze: () => pool.scanGaps({ auto: true }),
    selectItem: (item) => waveform.selectItem(item) });
  segments = createSegments({ $, esc, ico, shortName, fmtT, fmtDur, fmtSel, SEG_MIN, SEG_MAX,
    state, toast, api, trackTask: tasks.trackTask,
    charById: store.charById, newSegment: store.newSegment, pushUndo: store.pushUndo, scheduleSaveProject: store.scheduleSaveProject, setPage,
    closePool: () => pool.closePool(),
    // waveform 晚于 segments 创建：用闭包注入，写回片段后把选区从黄色刷回蓝色
    refreshSelColor: () => waveform.refreshSelColor(),
    selectItem: (item) => waveform.selectItem(item) });
  pool = createPool({ $, $$, esc, ico, shortName, fmtT, toast, state, api, trackTask: tasks.trackTask,
    attachActiveTasks: tasks.attachActiveTasks,
    needItem, charById: store.charById, uid: store.uid, paletteNext: store.paletteNext, pushUndo: store.pushUndo,
    scheduleSaveProject: store.scheduleSaveProject, scheduleSavePool: store.scheduleSavePool,
    loadAllItemData: projects.loadAllItemData, segments, setPage,
    playSequence: (seq, idx) => waveform.playSequence(seq, idx),
    renderSubs: () => subtitles.renderSubs() });
  subtitles = createSubtitles({ $, $$, fmtT, esc, ico, api, toast, state, trackTask: tasks.trackTask,
    speakerLabelAt: store.speakerLabelAt, segsFor: segments.segsFor, newSegment: store.newSegment, pushUndo: store.pushUndo, scheduleSaveProject: store.scheduleSaveProject,
    renderSegments: segments.renderSegments,
    setSelection: (s, e) => waveform.setSelection(s, e) });
  training = createTraining({ $, esc, ico, shortName, fmtDur, api, state, toast, trackTask: tasks.trackTask });
  io = createIo({ $, api, state, toast, trackTask: tasks.trackTask, needItem,
    selectResultItem: tasks.selectResultItem, autoAnalyzeDone: tasks.autoAnalyzeDone,
    showModal, hideModal, showResult, saveProjectNow: store.saveProjectNow, pushUndo: store.pushUndo, loadProject: store.loadProject,
    loadAllItemData: projects.loadAllItemData,
    renderSegments: segments.renderSegments, segsFor: segments.segsFor, charById: store.charById, scheduleSaveProject: store.scheduleSaveProject,
    fmtSel, fmtT });
  waveform = createWaveform({ $, api, ico, fmtT, fmtDur, fmtSel, clampN, SEEK_STEP, toast, state,
    WaveSurfer, Timeline, Regions, Minimap,
    renderMediaList: projects.renderMediaList, loadProject: store.loadProject,
    saveProjectNow: store.saveProjectNow, savePoolNow: store.savePoolNow,
    renderSegments: segments.renderSegments,
    auditionFocus: segments.auditionFocus,
    subtitles });
  setupMenus();
  setupShortcuts();
  setupDrop();
  bindUI();
  initWorkspace();
  waveform.setupMMSeek();
  setupPagebar();
  setInterval(() => {
    if (currentPage === "train" && !document.getElementById("page-train").classList.contains("hidden")) {
      if (training) training.loadTraining(false);
    }
  }, 8000);
  // ── 窗口聚焦静默刷新：后台任务可能在别处（其他页面/命令行）完成，
  //    防止本页面滞留旧的角色池/片段数据。10s 节流，识别运行中跳过。 ──
  let lastFocusRefresh = 0;
  window.addEventListener("focus", async () => {
    if (document.body.dataset.vc !== "ok" || state.identifying) return;
    const now = Date.now();
    if (now - lastFocusRefresh < 10000) return;
    lastFocusRefresh = now;
    try {
      if (!state.currentProject) return;
      const j = await api(`/api/projects/${state.currentProject.id}`);
      if (Array.isArray(j.characters)) state.characters = j.characters;
      state.items = j.items || [];
      projects.renderMediaList();
      await projects.loadAllItemData();
      renderPoolSafe();
    } catch (e) { /* 网络抖动忽略 */ }
  });
  function renderPoolSafe() {
    try { pool.renderPool(); segments.renderSegments(); subtitles.renderSubs(); }
    catch (e) { /* 模块未就绪时跳过 */ }
  }
  // ── 顶栏性能显示（CPU / 内存 / GPU，3s 轮询，后台标签页暂停） ──
  function perfLevel(v) { return v >= 85 ? "lv-bad" : v >= 60 ? "lv-warn" : ""; }
  function startPerfMonitor() {
    const elCpu = document.getElementById("perf-cpu");
    const elMem = document.getElementById("perf-mem");
    const elGpu = document.getElementById("perf-gpu");
    const chip = document.getElementById("perf-chip");
    if (!elCpu) return;
    const gb = (b) => (b / 1073741824).toFixed(1);
    async function tick() {
      if (document.hidden) return;
      try {
        const j = await api("/api/sysperf");
        const set = (el, v) => { el.textContent = Math.round(v) + "%"; el.className = perfLevel(v); };
        set(elCpu, j.cpu); set(elMem, j.mem);
        if (j.gpu) set(elGpu, j.gpu.util);
        else { elGpu.textContent = "N/A"; elGpu.className = "off"; }
        if (chip) chip.title =
          `CPU ${Math.round(j.cpu)}%（${j.cpu_cores} 核）\n` +
          `内存 ${Math.round(j.mem)}%（${gb(j.mem_used)} / ${gb(j.mem_total)} GB）\n` +
          (j.gpu ? `GPU ${j.gpu.name}：负载 ${Math.round(j.gpu.util)}%，显存 ${gb(j.gpu.vram_used)} / ${gb(j.gpu.vram_total)} GB`
                 : "GPU：未检测到 NVIDIA 显卡");
      } catch (e) { /* 后端未就绪，下轮重试 */ }
    }
    tick();
    setInterval(tick, 3000);
  }
  startPerfMonitor();
  function beaconSave() {
    try {
      Array.from(state.dirtyItems).forEach(id => {
        const payload = JSON.stringify({ segments: segments.segsFor(id), speaker_segments: state.speakerSegsByItem.get(id) || [] });
        navigator.sendBeacon(`/api/items/${id}/project`, new Blob([payload], { type: "application/json" }));
      });
      // 识别/反馈进行中不落盘（与 savePoolNow 同策略）：此刻的 characters 可能是旧快照
      if (state.poolDirty && state.currentProject && !state.identifying) {
        const payload = JSON.stringify({ characters: state.characters });
        navigator.sendBeacon(`/api/projects/${state.currentProject.id}/characters`, new Blob([payload], { type: "application/json" }));
      }
    } catch (e) { /* 尽力而为 */ }
  }
  window.addEventListener("beforeunload", () => {
    if (state.dirtyItems.size || state.poolDirty) beaconSave();
  });
  pool.renderAutoAnalyzeBtn();
  pool.renderAutoTrainingBtn();
  boot();

  // 调试/自动化钩子
  window.__vc = { state, selectItem: waveform.selectItem, selectProject: projects.selectProject,
    renderSegments: segments.renderSegments,
    playSequence: (seq, idx) => waveform.playSequence(seq, idx),
    updateStatusbar: tasks.updateStatusbar, attachActiveTasks: tasks.attachActiveTasks,
    WaveSurfer, Timeline, Regions, Minimap,
    markForward: waveform.markForward, unmarkLast: waveform.unmarkLast,
    clearMultiRegions: waveform.clearMultiRegions,
    stopAudition: waveform.stopAudition,
    loadProject: store.loadProject, saveProjectNow: store.saveProjectNow, savePoolNow: store.savePoolNow,
    openPool: pool.openPool, closePool: pool.closePool, renderPool: pool.renderPool,
    undo: doUndo, redo: doRedo, pushUndo: store.pushUndo, doAutosplit: io.doAutosplit, uploadFile: io.uploadFile,
    createProject: projects.createProject, renameProject: projects.renameProject,
    deleteProject: projects.deleteProject,
    doIdentifySpeakers: pool.doIdentifySpeakers, newSegment: store.newSegment,
    scanGaps: pool.scanGaps, toggleAutoGapScan: pool.toggleAutoGapScan,
    applySelectionToActive: segments.applySelectionToActive, mergeSegments: segments.mergeSegments,
    toggleSegLock: segments.toggleLock,
    queueRetranscribe: segments.queueRetranscribe,
    toggleAutoAnalyze: pool.toggleAutoAnalyze,
    toggleAutoTraining: pool.toggleAutoTraining,
    setPage, loadTraining: training.loadTraining, startTrain: training.startTrain,
    doInfer: training.doInfer, train: training.train,
    workspace: { layout, applyLayout, saveLayout, resetLayout, togglePanel, swapPanels, PANELS } };
})();
