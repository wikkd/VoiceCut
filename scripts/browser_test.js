const { spawn } = require("child_process");
// VoiceCut 前端综合验证 (Chrome DevTools Protocol, headless)
// 用法: node scripts/browser_test.js [port]
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = Number(process.argv[2] || 9347);
const PROF = process.env.TEMP + "\\vc-cdp-" + Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, "http://127.0.0.1:8765/"], { stdio: "ignore" });
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
      out.hasItems = vc.state.items.length;
      return out;
    })()`;

    const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    console.log("MAIN:", JSON.stringify(r.result && r.result.result && r.result.result.value, null, 2));
    if (r.result && r.result.exceptionDetails) console.log("EXC:", JSON.stringify(r.result.exceptionDetails));

    // 快捷键：←→ 快退/快进、↑↓ 音量、小键盘 −/+ 快进
    const kd = (key, code, vk, mods = 0) => send("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers: mods });
    const ku = (key, code, vk, mods = 0) => send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers: mods });
    const press = async (key, code, vk, mods = 0) => { await kd(key, code, vk, mods); await ku(key, code, vk, mods); };
    const rK0 = await send("Runtime.evaluate", { expression: `(() => { window.__vc.state.ws.setVolume(0.5); window.__vc.state.ws.setTime(1); return window.__vc.state.ws.getVolume(); })()`, returnByValue: true });
    const volBefore = rK0.result && rK0.result.result && rK0.result.result.value;
    await press("ArrowRight", "ArrowRight", 39);       // +5s
    const rK1 = await send("Runtime.evaluate", { expression: `window.__vc.state.ws.getCurrentTime()`, returnByValue: true });
    await press("ArrowLeft", "ArrowLeft", 37);         // -5s
    const rK2 = await send("Runtime.evaluate", { expression: `window.__vc.state.ws.getCurrentTime()`, returnByValue: true });
    await press("ArrowUp", "ArrowUp", 38);             // +5%
    const rK3 = await send("Runtime.evaluate", { expression: `window.__vc.state.ws.getVolume()`, returnByValue: true });
    await press("NumpadAdd", "NumpadAdd", 107);        // +15s
    const rK4 = await send("Runtime.evaluate", { expression: `window.__vc.state.ws.getCurrentTime()`, returnByValue: true });
    await press("NumpadSubtract", "NumpadSubtract", 109); // -15s
    const rK5 = await send("Runtime.evaluate", { expression: `window.__vc.state.ws.getCurrentTime()`, returnByValue: true });
    await press("ArrowDown", "ArrowDown", 40);         // -5%
    const rK6 = await send("Runtime.evaluate", { expression: `window.__vc.state.ws.getVolume()`, returnByValue: true });
    const v = (o) => o.result && o.result.result && o.result.result.value;
    const t1 = 1, t2 = v(rK1), t3 = v(rK2), vol1 = v(rK3), t4 = v(rK4), t5 = v(rK5), vol2 = v(rK6);
    const keysOk = t2 > t1 + 1 && t3 < t2 - 1 && Math.abs(vol1 - (volBefore + 0.05)) < 0.001
      && t4 > t3 + 1 && t5 < t4 - 1 && Math.abs(vol2 - volBefore) < 0.001;
    console.log("KEYS:", JSON.stringify({ t1, t2, t3, volBefore, vol1, t4, t5, vol2, ok: keysOk }));

    // Ctrl+→ 多选快进：导入 40s 长素材 → 连续标记多段 → Ctrl+← 撤销
    const makeWav = (seconds, sr = 16000) => {
      const n = seconds * sr, dataLen = n * 2;
      const b = Buffer.alloc(44 + dataLen);
      b.write("RIFF", 0); b.writeUInt32LE(36 + dataLen, 4); b.write("WAVE", 8);
      b.write("fmt ", 12); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
      b.writeUInt32LE(sr, 24); b.writeUInt32LE(sr * 2, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34);
      b.write("data", 36); b.writeUInt32LE(dataLen, 40);
      return b;
    };
    const fd = new FormData();
    fd.append("file", new Blob([makeWav(40)], { type: "audio/wav" }), "long_test.wav");
    const impR = await fetch("http://127.0.0.1:8765/api/import", { method: "POST", body: fd });
    const imp = await impR.json();
    let tsk = null;
    for (let i = 0; i < 80; i++) {
      await sleep(500);
      tsk = await (await fetch(`http://127.0.0.1:8765/api/tasks/${imp.task_id}`)).json();
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

    const rS = await send("Runtime.evaluate", { expression: `(() => {
      const vc = window.__vc;
      vc.workspace.resetLayout();
      return {
        hidden: vc.workspace.layout.hidden,
        subVisible: !document.querySelector('#subtitle-panel').classList.contains('panel-hidden'),
        subTrack: getComputedStyle(document.querySelector('#workspace')).getPropertyValue('--w-sub').trim(),
      };
    })()`, returnByValue: true });
    console.log("RESET:", JSON.stringify(rS.result && rS.result.result && rS.result.result.value));

    ws.close();
  } finally { child.kill(); }
})();
