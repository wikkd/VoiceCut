const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
// VoiceCut 前端综合验证 (Chrome DevTools Protocol, headless)
// 用法: node scripts/browser_test.js [cdp端口]
// 自带后端实例：以临时 workdir 启动 voicecut.py，测试数据与生产 workdir 完全隔离，用完即弃。
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = Number(process.argv[2] || 9347);
const PROF = process.env.TEMP + "\\vc-cdp-" + Date.now();
const ROOT = path.join(__dirname, "..");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const SRV_PORT = Number(process.env.VC_PORT || 8899);
const BASE = `http://127.0.0.1:${SRV_PORT}`;
const WORKDIR = fs.mkdtempSync(path.join(os.tmpdir(), "vc-bt-"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const makeWav = (seconds, sr = 16000) => {
  const n = seconds * sr, dataLen = n * 2;
  const b = Buffer.alloc(44 + dataLen);
  b.write("RIFF", 0); b.writeUInt32LE(36 + dataLen, 4); b.write("WAVE", 8);
  b.write("fmt ", 12); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(sr, 24); b.writeUInt32LE(sr * 2, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34);
  b.write("data", 36); b.writeUInt32LE(dataLen, 40);
  return b;
};

(async () => {
  // 1) 以临时 workdir 启动后端，等待就绪
  const server = spawn(PY, ["voicecut.py", "--port", String(SRV_PORT), "--no-browser", "--workdir", WORKDIR],
    { cwd: ROOT, stdio: "ignore" });
  let ready = false;
  for (let i = 0; i < 60 && !ready; i++) {
    try { ready = (await fetch(`${BASE}/api/config`)).ok; } catch (e) {}
    if (!ready) await sleep(500);
  }
  if (!ready) throw new Error(`server not ready on ${BASE}`);
  // 2) 预置一个素材（否则 MAIN 段 noItems，后续段全部失真）
  const seedFd = new FormData();
  seedFd.append("file", new Blob([makeWav(20)], { type: "audio/wav" }), "seed.wav");
  const seedImp = await (await fetch(`${BASE}/api/import`, { method: "POST", body: seedFd })).json();
  for (let i = 0; i < 80; i++) {
    await sleep(500);
    const tsk = await (await fetch(`${BASE}/api/tasks/${seedImp.task_id}`)).json();
    if (tsk && (tsk.status === "done" || tsk.status === "failed")) break;
  }

  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, `${BASE}/`], { stdio: "ignore" });
  try {
    let target = null;
    for (let i = 0; i < 30 && !target; i++) {
      try { const res = await fetch(`http://127.0.0.1:${PORT}/json`); const l = await res.json(); target = l.find((t) => t.type === "page") || null; } catch (e) {}
      if (!target) await sleep(500);
    }
    if (!target) throw new Error("CDP target not found");
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let msgId = 0; const pending = new Map();
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
    const send = (method, params = {}) => new Promise((res) => { const id = ++msgId; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
    await sleep(2500);

    const expr = `(async () => {
      const vc = window.__vc;
      const errs = [];
      window.addEventListener('error', (e) => errs.push(String(e.message)));
      if (!vc) return { ok: false, reason: 'no __vc' };
      // 工作区结构
      const wsEl = document.querySelector('#workspace');
      const panels = Array.from(document.querySelectorAll('#workspace .panel')).map(el => el.id);
      const winToggles = document.querySelectorAll("[data-act='panel-toggle']").length;
      const layoutReset = !!document.querySelector("[data-act='layout-reset']");
      const splitterCount = document.querySelectorAll('#workspace .splitter').length;
      const out = {
        ok: true,
        boot: document.body.dataset.vc,
        workspace: {
          hasWorkspace: !!wsEl,
          panels, winToggles, layoutReset, splitterCount,
          transport: !!document.querySelector('#transport'),
        },
        errs: [],
      };
      if (!vc.state.items.length) { out.noItems = true; return out; }
      await vc.selectItem(vc.state.items[0]);
      await new Promise(r => setTimeout(r, 5000));
      // 穿透 shadow root 统计 canvas
      let canvases = 0;
      const walk = (root) => {
        root.querySelectorAll('canvas').forEach(() => { canvases++; });
        root.querySelectorAll('*').forEach((el) => { if (el.shadowRoot) walk(el.shadowRoot); });
      };
      walk(document);
      let region = null;
      try { region = vc.state.regions.addRegion({ start: 0.5, end: 2.0, color: 'rgba(108,156,255,0.25)', drag: true, resize: true }); } catch (e) { errs.push('region:' + e.message); }
      let playErr = null;
      try { vc.state.ws.setTime(0); vc.state.ws.play(); await new Promise(r => setTimeout(r, 1000)); vc.state.ws.pause(); } catch (e) { playErr = String(e); }
      out.canvases = canvases;
      out.region = region ? { start: region.start, end: region.end } : null;
      out.selection = vc.state.selection;
      out.playErr = playErr;
      out.errs = errs;
      out.duration = vc.state.currentItem.duration;
      out.videoVisible = !document.querySelector('#video-panel').classList.contains('no-video') && !!document.querySelector('#video-preview').getAttribute('src');
      // A2: play-selection sets auditioning so playback stops at the selection end
      try { document.querySelector('#btn-play-selection').click(); out.auditioning = vc.state.auditioning; vc.state.auditioning = null; } catch (e) { out.auditioning = null; }
      // A4: video mute is locked (volumechange cannot unmute)
      try { const _vp = document.querySelector('#video-preview'); _vp.muted = false; _vp.dispatchEvent(new Event('volumechange')); out.mutedLocked = _vp.muted; } catch (e) { out.mutedLocked = null; }
      out.hasItems = vc.state.items.length;
      return out;
    })()`;

    const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    console.log("MAIN:", JSON.stringify(r.result && r.result.result && r.result.result.value, null, 2));
    if (r.result && r.result.exceptionDetails) console.log("EXC:", JSON.stringify(r.result.exceptionDetails));

    // 快捷键：有选区时 ←→ 平移选区(+0.5s)、播放头不动；↑↓ 音量；小键盘 −/+ 快进
    const kd = (key, code, vk, mods = 0) => send("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers: mods });
    const ku = (key, code, vk, mods = 0) => send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers: mods });
    const press = async (key, code, vk, mods = 0) => { await kd(key, code, vk, mods); await ku(key, code, vk, mods); };
    const rK0 = await send("Runtime.evaluate", { expression: `(() => { window.__vc.state.ws.setVolume(0.5); window.__vc.state.ws.setTime(1); return JSON.stringify({ vol: window.__vc.state.ws.getVolume(), sel: window.__vc.state.selection }); })()`, returnByValue: true });
    const base = JSON.parse(rK0.result.result.value);   // { vol, sel }
    const volBefore = base.vol;
    await press("ArrowRight", "ArrowRight", 39);       // 平移选区 +0.5s
    const rK1 = await send("Runtime.evaluate", { expression: `JSON.stringify({ t: window.__vc.state.ws.getCurrentTime(), sel: window.__vc.state.selection })`, returnByValue: true });
    await press("ArrowLeft", "ArrowLeft", 37);         // 平移选区 −0.5s（回原位）
    const rK2 = await send("Runtime.evaluate", { expression: `JSON.stringify({ t: window.__vc.state.ws.getCurrentTime(), sel: window.__vc.state.selection })`, returnByValue: true });
    await press("ArrowUp", "ArrowUp", 38);             // +5%
    const rK3 = await send("Runtime.evaluate", { expression: `window.__vc.state.ws.getVolume()`, returnByValue: true });
    await press("NumpadAdd", "NumpadAdd", 107);        // +15s（小键盘仍为跳转）
    const rK4 = await send("Runtime.evaluate", { expression: `window.__vc.state.ws.getCurrentTime()`, returnByValue: true });
    await press("NumpadSubtract", "NumpadSubtract", 109); // -15s
    const rK5 = await send("Runtime.evaluate", { expression: `window.__vc.state.ws.getCurrentTime()`, returnByValue: true });
    await press("ArrowDown", "ArrowDown", 40);         // -5%
    const rK6 = await send("Runtime.evaluate", { expression: `window.__vc.state.ws.getVolume()`, returnByValue: true });
    const v = (o) => o.result && o.result.result && o.result.result.value;
    const K1 = JSON.parse(v(rK1)), K2 = JSON.parse(v(rK2));
    const vol1 = v(rK3), t4 = v(rK4), t5 = v(rK5), vol2 = v(rK6);
    const near = (a, b, e) => Math.abs(a - b) < e;
    const keysOk = base.sel && K1.sel && K2.sel
      && near(K1.sel.start, base.sel.start + 0.5, 0.05) && near(K1.sel.end, base.sel.end + 0.5, 0.05)   // → 平移 +0.5
      && near(K1.t, 1, 0.05)                                                                             // 播放头不动
      && near(K2.sel.start, base.sel.start, 0.05) && near(K2.sel.end, base.sel.end, 0.05)                // ← 平移回原位
      && Math.abs(vol1 - (volBefore + 0.05)) < 0.001
      && t4 > 1 + 1 && t5 < t4 - 1 && Math.abs(vol2 - volBefore) < 0.001;                                // 小键盘跳转保留
    console.log("KEYS:", JSON.stringify({ base: base.sel, K1, K2, vol1, t4, t5, vol2, ok: keysOk }));

    // Ctrl+→ 多选快进：导入 40s 长素材 → 连续标记多段 → Ctrl+← 撤销
    const fd = new FormData();
    fd.append("file", new Blob([makeWav(40)], { type: "audio/wav" }), "long_test.wav");
    const impR = await fetch(`${BASE}/api/import`, { method: "POST", body: fd });
    const imp = await impR.json();
    let tsk = null;
    for (let i = 0; i < 80; i++) {
      await sleep(500);
      tsk = await (await fetch(`${BASE}/api/tasks/${imp.task_id}`)).json();
      if (tsk && (tsk.status === "done" || tsk.status === "failed")) break;
    }
    console.log("IMPORT:", JSON.stringify({ task: tsk && tsk.status, item: tsk && tsk.result && tsk.result.item_id }));
    await send("Page.reload", { ignoreCache: true });
    await sleep(3500);
    const rL = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const it = vc.state.items.find(x => x.name === 'long_test');
      if (!it) return { ok: false, reason: 'item not found' };
      await vc.selectItem(it);
      await new Promise(r => setTimeout(r, 4000));
      return { ok: true, dur: it.duration };
    })()`, awaitPromise: true, returnByValue: true });
    const longSel = v(rL);
    await send("Runtime.evaluate", { expression: `window.__vc.state.ws.setTime(0)` });
    await press("ArrowRight", "ArrowRight", 39, 2);      // Ctrl+→ 标记 0-5
    await press("ArrowRight", "ArrowRight", 39, 2);      // Ctrl+→ 标记 5-10
    const rM1 = await send("Runtime.evaluate", { expression: `(() => ({ n: window.__vc.state.multiRegions.length, sel: window.__vc.state.selection, t: window.__vc.state.ws.getCurrentTime() }))()`, returnByValue: true });
    await press("ArrowLeft", "ArrowLeft", 37, 2);        // Ctrl+← 撤销 → 1 段
    const rM2 = await send("Runtime.evaluate", { expression: `window.__vc.state.multiRegions.length`, returnByValue: true });
    const m1 = v(rM1), m2 = v(rM2);
    console.log("MULTI:", JSON.stringify({ selected: longSel, marks: m1, afterUndo: m2, ok: !!(longSel && longSel.ok && m1 && m1.n === 2 && m2 === 1) }));

    // M1-M6 前端功能：角色池、说话人下拉、总览条播放头、右键菜单、URL 弹窗
    const rF = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      vc.openPool();
      const poolVisible = !document.querySelector('#pool-view').classList.contains('hidden');
      const poolCards = document.querySelectorAll('#pool-grid .pool-card').length;
      const segs = vc.state.segmentsByItem.get(vc.state.currentItem.id);
      segs.push(vc.newSegment(0, 5, "test"));
      vc.renderSegments();
      const spkSel = document.querySelector('#seg-tbody .seg-speaker');
      const spkOptions = spkSel ? spkSel.options.length : 0;
      const segRow = document.querySelector('#seg-tbody tr.seg-row');
      segRow.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 150, clientY: 150 }));
      const redirVisible = !document.querySelector('#redirect-menu').classList.contains('hidden');
      document.querySelector('#redirect-menu').classList.add('hidden');
      const li = document.querySelector('#media-list li');
      li.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 80, clientY: 80 }));
      const mediaMenuVisible = !document.querySelector('#media-menu').classList.contains('hidden');
      document.querySelector('#media-menu').classList.add('hidden');
      vc.state.dirtyItems.add(vc.state.currentItem.id);
      await vc.saveProjectNow();
      vc.state.ws.setTime(10);
      await new Promise(r => setTimeout(r, 300));
      const mm = document.querySelector('#mm-cursor');
      const mmLeft = mm ? getComputedStyle(mm).left : null;
      const mmTime = document.querySelector('#mm-time').textContent;
      const bb = document.querySelector('#bb-url');
      const bbIsTextarea = !!bb && bb.tagName === 'TEXTAREA';
      const cancelBtn = !!document.querySelector('#toast-stack');
      const hasProjectSelect = !!document.querySelector('#project-select');
      const projectSelectOpts = document.querySelectorAll('#project-select option').length;
      const projectName = vc.state.currentProject ? vc.state.currentProject.name : null;
      const srcCells = document.querySelectorAll('#seg-tbody .seg-src').length;
      const poolTitle = document.querySelector('#pool-item-name').textContent;
      // A1: 草稿式文本编辑——input 只预览不落数据；未回车失焦=放弃；回车=确认写回+锁定
      let a1 = null;
      const segInput = document.querySelector('#seg-tbody .seg-text');
      if (segInput) {
        const origText = segInput.value;
        const a1ItemId = vc.state.currentItem.id;   // 注意：itId 在后面才定义，这里不能引用
        segInput.focus();
        segInput.value = "";
        segInput.dispatchEvent(new Event('input', { bubbles: true }));
        const focusKept = document.activeElement === segInput;
        const tagDraft = segInput.closest('tr').querySelector('.tag').textContent;   // 草稿实时校验预览
        const segMid = (vc.state.segmentsByItem.get(a1ItemId) || [])[0];
        const draftNotSaved = segMid.text === origText;                             // 草稿未落数据
        segInput.value = "draft-no-commit";
        segInput.dispatchEvent(new Event('input', { bubbles: true }));
        segInput.blur();                                                             // 未回车 → 放弃，恢复原文本
        await new Promise(r => setTimeout(r, 150));
        const segAfterDiscard = (vc.state.segmentsByItem.get(a1ItemId) || [])[0];
        const discarded = segAfterDiscard.text === origText && segInput.value === origText;
        segInput.focus();
        segInput.value = "confirmed-by-enter";
        segInput.dispatchEvent(new Event('input', { bubbles: true }));
        segInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));   // 回车确认
        await new Promise(r => setTimeout(r, 200));
        const segCommitted = (vc.state.segmentsByItem.get(a1ItemId) || [])[0];
        const committed = segCommitted.text === "confirmed-by-enter" && segCommitted.locked === true;
        a1 = { focusKept, tagDraft, draftNotSaved, discarded, committed };
      }
      // D1: per-speaker / val-ratio export fields
      const hasPerSpeaker = !!document.querySelector('#ds-per-speaker');
      const hasValRatio = !!document.querySelector('#ds-val-ratio');
      // D2: autosplit modal + menu
      const hasAutosplitModal = !!document.querySelector('#modal-autosplit');
      const hasAutosplitMenu = !!document.querySelector("[data-act='autosplit']");
      const hasAsStart = !!document.querySelector('#as-start');
      // D3: undo restores a deleted segment; undo/redo API + menu exposed
      const itId = vc.state.currentItem.id;
      const segsArr = vc.state.segmentsByItem.get(itId) || [];
      segsArr.push(vc.newSegment(6, 9, "undo-test"));
      vc.pushUndo();
      const nBefore = segsArr.length;
      segsArr.pop();
      vc.undo();
      const nAfter = (vc.state.segmentsByItem.get(itId) || []).length;
      const hasUndoApi = typeof vc.undo === 'function' && typeof vc.redo === 'function' && typeof vc.pushUndo === 'function';
      const hasUndoMenu = !!document.querySelector("[data-act='undo']");
      const _a = vc.state.segmentsByItem.get(itId) || [];
      const _i = _a.findIndex(s => s.text === 'undo-test');
      if (_i >= 0) { _a.splice(_i, 1); vc.state.dirtyItems.add(itId); }
      await vc.saveProjectNow();
      vc.renderSegments();
      // 角色池试听 → 跳转剪辑页并播放（回归：poolAudition 需先关掉角色池子页面、
      // 切回剪辑页、选中素材再 ws.play()）。用全新角色绑定当前素材的片段，
      // 保证 rows[0] 就在当前素材上；audSync 同步读可证明 auditionSegment 已执行
      // （headless 下无音频设备会让 auditionCheck 立即触发，故不依赖最终 auditioning 值）。
      const audCh = { id: "char_feat_aud_" + Date.now(), name: "回归角色", color: "#46a758",
                      speakerLabels: [], created: Date.now() };
      vc.state.characters.push(audCh);
      const audSeg = vc.newSegment(1, 4, "aud");
      audSeg.characterId = audCh.id;
      (vc.state.segmentsByItem.get(itId) || []).push(audSeg);
      vc.renderSegments(); vc.renderPool();
      await new Promise(r => setTimeout(r, 300));
      const audBtn = document.querySelector('#pool-grid .pool-card[data-pool-char="' + audCh.id + '"] .pool-aud');
      let poolAud = null;
      if (audBtn) {
        audBtn.click();
        const audSync = vc.state.auditioning ? { start: vc.state.auditioning.start, end: vc.state.auditioning.end } : null;
        await new Promise(r => setTimeout(r, 300));
        poolAud = {
          poolClosed: document.querySelector('#pool-view').classList.contains('hidden'),
          editVisible: !document.querySelector('#page-edit').classList.contains('hidden'),
          currentItemOk: vc.state.currentItem && vc.state.currentItem.id === itId,
          wsExists: !!vc.state.ws,
          audSync,
        };
      }
      // 清掉临时角色/片段（不落库）
      // 清掉临时角色/片段（不落库），避免影响后续 PERSIST 的 charCount 断言
      vc.state.characters = vc.state.characters.filter(c => c.id !== audCh.id);
      const _audList = vc.state.segmentsByItem.get(itId) || [];
      const _ai = _audList.findIndex(s => s.id === audSeg.id);
      if (_ai >= 0) _audList.splice(_ai, 1);
      // 把关键可见性并入 ok：此前只打印不判定，曾漏掉右键重定向菜单打不开的回归
      return { ok: poolVisible && redirVisible && mediaMenuVisible && bbIsTextarea && cancelBtn && hasAutosplitMenu
               && !!a1 && a1.focusKept && a1.draftNotSaved && a1.discarded && a1.committed,
               poolVisible, poolCards, spkOptions, redirVisible, mediaMenuVisible,
               mmLeft, mmTime, bbIsTextarea, cancelBtn, segCount: (vc.state.segmentsByItem.get(itId) || []).length,
               hasProjectSelect, projectSelectOpts, projectName, srcCells, poolTitle,
               a1, hasPerSpeaker, hasValRatio, hasAutosplitModal, hasAutosplitMenu, hasAsStart,
               hasUndoApi, hasUndoMenu, undoRestored: nAfter === nBefore, poolAud };
    })()`, awaitPromise: true, returnByValue: true });
    console.log("FEAT:", JSON.stringify(rF.result && rF.result.result && rF.result.result.value));

    // APPLY：选区写回聚焦片段——点击行聚焦 → 选区一致不修改 → 调整选区写回 → undo 恢复
    const rA = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const itId = vc.state.currentItem.id;
      const seg = (vc.state.segmentsByItem.get(itId) || [])[0];
      if (!seg) return { skip: 'no seg' };
      const row = document.querySelector('#seg-tbody tr.seg-row');
      row.click();
      await new Promise(r => setTimeout(r, 400));
      const focused = !!vc.state.activeSeg && vc.state.activeSeg.segId === seg.id
        && !!vc.state.selection && Math.abs(vc.state.selection.end - seg.end) < 0.05;
      const before = { start: seg.start, end: seg.end };
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true }));
      await new Promise(r => setTimeout(r, 200));
      const sameOk = seg.start === before.start && seg.end === before.end;   // 选区一致 → 不修改
      vc.state.selection = { start: before.start, end: before.end + 1 };     // 模拟手柄拉伸 +1s
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true }));
      await new Promise(r => setTimeout(r, 300));
      const segNow = (vc.state.segmentsByItem.get(itId) || []).find(s => s.id === seg.id);   // restoreSnapshot 会整体替换数组，undo 后必须重新取引用
      const applied = segNow && Math.abs(segNow.end - (before.end + 1)) < 0.01 && Math.abs(segNow.start - before.start) < 0.001;
      vc.undo();
      await new Promise(r => setTimeout(r, 300));
      const segUndo = (vc.state.segmentsByItem.get(itId) || []).find(s => s.id === seg.id);
      const undone = segUndo && Math.abs(segUndo.end - before.end) < 0.01;   // undo 恢复原区间
      return { focused, sameOk, applied, undone, segEnd: segNow ? segNow.end : null, before };
    })()`, awaitPromise: true, returnByValue: true });
    const av = rA.result && rA.result.result && rA.result.result.value;
    console.log("APPLY:", JSON.stringify(av));
    if (rA.result && rA.result.exceptionDetails) console.log("APPLY-EXC:", JSON.stringify(rA.result.exceptionDetails));
    console.log("APPLY:", JSON.stringify({ ok: !!(av && !av.skip && av.focused && av.sameOk && av.applied && av.undone) }));

    // SPEAKER：说话人下拉可更改——change 写回 characterId；点击下拉不再触发行重建（回归：行重建曾吞掉下拉交互）
    const rS = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const itId = vc.state.currentItem.id;
      const seg = (vc.state.segmentsByItem.get(itId) || [])[0];
      if (!seg) return { skip: 'no seg' };
      vc.state.characters.push({ id: 'c-t1', name: '测试角色', color: '#ff6600' });
      vc.renderSegments();
      const sel = document.querySelector('#seg-tbody .seg-speaker');
      if (!sel) return { skip: 'no speaker select' };
      const before = seg.characterId;
      // 点击下拉（行点击委托曾重建行 → 元素引用失效、下拉被关）
      sel.click();
      const sameEl = document.querySelector('#seg-tbody .seg-speaker') === sel;
      sel.value = 'c-t1';
      sel.dispatchEvent(new Event('change', { bubbles: true }));
      await new Promise(r => setTimeout(r, 300));
      const after = (vc.state.segmentsByItem.get(itId) || [])[0];
      const applied = after.characterId === 'c-t1';
      const selNow = document.querySelector('#seg-tbody .seg-speaker');
      const selKept = !!selNow && selNow.value === 'c-t1';           // 重渲染后仍显示新值
      vc.undo();
      await new Promise(r => setTimeout(r, 300));
      const segUndo = (vc.state.segmentsByItem.get(itId) || []).find(s => s.id === seg.id);
      const undone = segUndo && segUndo.characterId === before;      // undo 恢复
      return { sameEl, applied, selKept, undone, before: before || null };
    })()`, awaitPromise: true, returnByValue: true });
    const sv = rS.result && rS.result.result && rS.result.result.value;
    if (rS.result && rS.result.exceptionDetails) console.log("SPEAKER-EXC:", JSON.stringify(rS.result.exceptionDetails));
    console.log("SPEAKER:", JSON.stringify({ ...sv, ok: !!(sv && !sv.skip && sv.sameEl && sv.applied && sv.selKept && sv.undone) }));

    // 项目式：新建空项目 → 切换 → 素材/角色池隔离 → 删除
    const newProj = await (await fetch(`${BASE}/api/projects`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "test-proj" }) })).json();
    const rJ = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      await vc.selectProject(${JSON.stringify({ id: newProj.id, name: newProj.name })});
      await new Promise(r => setTimeout(r, 1200));
      const emptyItems = vc.state.items.length === 0;
      const emptyChars = vc.state.characters.length === 0;
      const selValue = document.querySelector('#project-select').value;
      return { ok: true, emptyItems, emptyChars, selValue };
    })()`, awaitPromise: true, returnByValue: true });
    console.log("PROJ:", JSON.stringify(rJ.result && rJ.result.result && rJ.result.result.value));
    await fetch(`${BASE}/api/projects/${newProj.id}`, { method: "DELETE" });

    // 项目持久化往返：刷新后片段从服务端恢复
    await send("Page.reload", { ignoreCache: true });
    await sleep(3500);
    const rP = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const it = vc.state.items.find(x => x.name === 'long_test');
      if (!it) return { ok: false };
      await vc.selectItem(it);
      await new Promise(r => setTimeout(r, 2500));
      const segs = vc.state.segmentsByItem.get(it.id) || [];
      return { ok: true, segCount: segs.length, charCount: vc.state.characters.length };
    })()`, awaitPromise: true, returnByValue: true });
    console.log("PERSIST:", JSON.stringify(rP.result && rP.result.result && rP.result.result.value));

    // 布局持久化往返：隐藏 字幕 → 刷新 → 仍隐藏 → 恢复默认
    const rH = await send("Runtime.evaluate", { expression: `(() => {
      const vc = window.__vc;
      vc.workspace.togglePanel('sub');
      return { hidden: vc.workspace.layout.hidden };
    })()`, returnByValue: true });
    console.log("HIDE:", JSON.stringify(rH.result && rH.result.result && rH.result.result.value));

    await send("Page.reload", { ignoreCache: true });
    await sleep(3500);
    const rR = await send("Runtime.evaluate", { expression: `(() => {
      const vc = window.__vc;
      const ws = document.querySelector('#workspace');
      return {
        boot: document.body.dataset.vc,
        hidden: vc.workspace.layout.hidden,
        subHiddenClass: document.querySelector('#subtitle-panel').classList.contains('panel-hidden'),
        subTrack: getComputedStyle(ws).getPropertyValue('--w-sub').trim(),
      };
    })()`, returnByValue: true });
    console.log("RELOAD:", JSON.stringify(rR.result && rR.result.result && rR.result.result.value));

    const rSpk = await send("Runtime.evaluate", { expression: `(() => {
      const vc = window.__vc;
      vc.workspace.resetLayout();
      return {
        hidden: vc.workspace.layout.hidden,
        subVisible: !document.querySelector('#subtitle-panel').classList.contains('panel-hidden'),
        subTrack: getComputedStyle(document.querySelector('#workspace')).getPropertyValue('--w-sub').trim(),
      };
    })()`, returnByValue: true });
    console.log("RESET:", JSON.stringify(rSpk.result && rSpk.result.result && rSpk.result.result.value));

    // A3: import through the UI auto-selects the new item (pollTasks refresh-then-doneCb)
    const rA3 = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const secs = 2, sr = 16000, dataLen = secs * sr * 2;
      const buf = new ArrayBuffer(44 + dataLen);
      const dv = new DataView(buf);
      const wr = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
      wr(0, 'RIFF'); dv.setUint32(4, 36 + dataLen, true); wr(8, 'WAVE'); wr(12, 'fmt ');
      dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
      dv.setUint32(24, sr, true); dv.setUint32(28, sr * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
      wr(36, 'data'); dv.setUint32(40, dataLen, true);
      const blob = new Blob([buf], { type: 'audio/wav' });
      vc.uploadFile(new File([blob], 'ui_import', { type: 'audio/wav' }));
      for (let i = 0; i < 80; i++) {
        await new Promise(r => setTimeout(r, 400));
        if (vc.state.currentItem && vc.state.currentItem.name === 'ui_import') break;
      }
      const ok = !!(vc.state.currentItem && vc.state.currentItem.name === 'ui_import');
      return { ok, name: vc.state.currentItem ? vc.state.currentItem.name : null };
    })()`, awaitPromise: true, returnByValue: true });
    console.log("A3:", JSON.stringify(v(rA3)));
    const _itemsList = await (await fetch(`${BASE}/api/items`)).json();
    const _uiItem = _itemsList.find(x => x.name === 'ui_import');
    if (_uiItem) await fetch(`${BASE}/api/items/${_uiItem.id}`, { method: "DELETE" });


    // DaVinci 式页面切换：素材库 / 剪辑 / 训练交付
    const rT = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const pageBtns = document.querySelectorAll('#pagebar .page-btn').length;
      vc.setPage('train');
      await new Promise(r => setTimeout(r, 900));
      const trainVisible = !document.querySelector('#page-train').classList.contains('hidden');
      const trainEls = !!document.querySelector('#train-role-list') && !!document.querySelector('#train-steps')
        && !!document.querySelector('#train-config-form') && !!document.querySelector('#train-infer-panel')
        && !!document.querySelector('#train-queue');
      const gpuState = document.querySelector('#train-gpu-state');
      const gpuTxt = gpuState ? gpuState.textContent : null;
      vc.setPage('media');
      await new Promise(r => setTimeout(r, 200));
      const mediaVisible = !document.querySelector('#page-media').classList.contains('hidden');
      const mediaList = !!document.querySelector('#media-page-list');
      const mediaItems = document.querySelectorAll('#media-page-list li').length;
      vc.setPage('edit');
      const editVisible = !document.querySelector('#page-edit').classList.contains('hidden');
      const hasApi = typeof vc.loadTraining === 'function' && typeof vc.startTrain === 'function'
        && typeof vc.setPage === 'function';
      return { ok: true, pageBtns, trainVisible, trainEls, gpuTxt, mediaVisible, mediaList,
               mediaItems, editVisible, hasApi };
    })()`, awaitPromise: true, returnByValue: true });
    console.log("TRAIN:", JSON.stringify(rT.result && rT.result.result && rT.result.result.value));

    // 恢复默认页面（剪辑），避免影响后续测试
    await send("Runtime.evaluate", { expression: `window.__vc.setPage('edit')`, returnByValue: true });
    ws.close();
  } finally {
    child.kill();
    try { server.kill(); } catch (e) {}
    try { fs.rmSync(WORKDIR, { recursive: true, force: true }); }
    catch (e) { console.log("[bt] 临时 workdir 清理失败（保留供排查）:", WORKDIR); }
  }
})();
