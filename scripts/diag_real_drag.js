// 真实输入链复现：用 CDP Input.dispatchMouseEvent（可信输入，含 pointer 事件链）
// 在真实服务页面拖选时间轴，同时抓 console/页面异常。
// 用法: node scripts/diag_real_drag.js [port=9352] [base=http://127.0.0.1:8765]
const { spawn } = require("child_process");
const fs = require("fs");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = Number(process.argv[2] || 9352);
const BASE = process.argv[3] || "http://127.0.0.1:8765";
const PROF = process.env.TEMP + "\\vc-rdrag-" + Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
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
    let msgId = 0; const pending = new Map(); const errors = [];
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
      if (m.method === "Runtime.consoleAPICalled" && (m.params.type === "error" || m.params.type === "warning"))
        errors.push(m.params.args.map(a => a.value || a.description || "").join(" ").slice(0, 300));
      if (m.method === "Runtime.exceptionThrown")
        errors.push("EXC: " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text).slice(0, 300));
    };
    const send = (method, params) => new Promise((res) => { const id = ++msgId; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
    await send("Runtime.enable");
    for (let i = 0; i < 60; i++) {
      const b = await send("Runtime.evaluate", { expression: `!!(window.__vc && window.__vc.state.ws)`, returnByValue: true });
      if (b.result.result.value) break;
      await sleep(500);
    }
    await sleep(1200);
    // 选一个有波形的素材（若当前无波形则点第一个素材）
    await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      if (vc.state.ws && vc.state.ws.getDuration() > 0) return "has";
      const cell = document.querySelector('.media-cell, [class*="media"] li, .media-list li');
      if (cell) { cell.click(); await new Promise(r=>setTimeout(r,2500)); }
      return cell ? "clicked" : "none";
    })()`, returnByValue: true, awaitPromise: true });
    await sleep(1500);

    const rect = await send("Runtime.evaluate", { expression: `(() => {
      const tl = document.querySelector('#timeline');
      if (!tl) return null;
      const r = tl.getBoundingClientRect();
      return { x: r.left, y: r.top + r.height / 2, w: r.width, h: r.height, visible: r.width > 0 && r.height > 0 };
    })()`, returnByValue: true });
    const R = rect.result.result.value;
    console.log("timeline rect:", JSON.stringify(R));
    if (!R || !R.visible) { console.log("FAIL: timeline 不可见"); process.exitCode = 1; return; }

    // 真实可信输入：在时间轴上从 30% 拖到 45%
    const x1 = R.x + R.w * 0.30, x2 = R.x + R.w * 0.45, y = R.y;
    const btn = (type, x, pressed) => send("Input.dispatchMouseEvent", {
      type, x, y, button: "left", buttons: pressed ? 1 : 0, clickCount: type === "mousePressed" || type === "mouseReleased" ? 1 : 0 });
    await btn("mouseMoved", x1, false);
    await btn("mousePressed", x1, true);
    await sleep(60);
    for (let k = 1; k <= 5; k++) await btn("mouseMoved", x1 + (x2 - x1) * k / 5, true);
    await sleep(60);
    await btn("mouseReleased", x2, false);
    await sleep(300);

    const st = await send("Runtime.evaluate", { expression: `(() => {
      const vc = window.__vc;
      const s = vc.state.selection;
      const dur = vc.state.ws ? vc.state.ws.getDuration() : 0;
      return { sel: s ? { start: Math.round(s.start*100)/100, end: Math.round(s.end*100)/100 } : null,
               expect: { a: Math.round(dur*0.30*100)/100, b: Math.round(dur*0.45*100)/100 },
               region: !!vc.state.selectionRegion, dur };
    })()`, returnByValue: true });
    const v = st.result.result.value;
    console.log("真实拖选结果:", JSON.stringify(v));
    const ok = v && v.sel && Math.abs(v.sel.start - v.expect.a) < 0.4 && Math.abs(v.sel.end - v.expect.b) < 0.4 && v.region;

    // 波形拖选（恢复的旧交互）：在 #waveform 上真实拖拽 → 生成选区且 drag=false（不可再拖动）
    const wr = await send("Runtime.evaluate", { expression: `(() => {
      const r = window.__vc.state.ws.getWrapper().getBoundingClientRect();
      return { x: r.left, y: r.top + r.height * 0.5, w: r.width };
    })()`, returnByValue: true });
    const W = wr.result.result.value;
    const wx1 = W.x + W.w * 0.55, wx2 = W.x + W.w * 0.70;
    const wbtn = (type, x, pressed) => send("Input.dispatchMouseEvent", {
      type, x, y: W.y, button: "left", buttons: pressed ? 1 : 0, clickCount: (type === "mousePressed" || type === "mouseReleased") ? 1 : 0 });
    await wbtn("mousePressed", wx1, true); await sleep(60);
    for (let k = 1; k <= 5; k++) await wbtn("mouseMoved", wx1 + (wx2 - wx1) * k / 5, true);
    await sleep(60);
    await wbtn("mouseReleased", wx2, false);
    await sleep(300);
    const wv = (await send("Runtime.evaluate", { expression: `(() => {
      const vc = window.__vc;
      const s = vc.state.selection, r = vc.state.selectionRegion;
      const dur = vc.state.ws ? vc.state.ws.getDuration() : 0;
      return { sel: s ? { start: Math.round(s.start*100)/100, end: Math.round(s.end*100)/100 } : null,
               expect: { a: Math.round(dur*0.55*100)/100, b: Math.round(dur*0.70*100)/100 },
               drag: r ? r.drag : null, resize: r ? r.resize : null };
    })()`, returnByValue: true })).result.result.value;
    console.log("波形拖选结果:", JSON.stringify(wv));
    // 容差 1.0s：vendored enableDragSelection 预览 region 初始化固定 +5px（40s 素材≈0.72s），
    // 属插件固有偏差；关键断言是选区落在拖拽区间附近且 drag/resize 全关（不可再拖动）
    const okW = wv && wv.sel && Math.abs(wv.sel.start - wv.expect.a) < 1.0 && Math.abs(wv.sel.end - wv.expect.b) < 1.0
      && wv.drag === false && wv.resize === false;

    // 键盘微调（真实按键事件）：Shift+→ 调终点 / Alt+→ 调起点 / → 平移，各 +0.5s
    const keyEv = (keyName, vk, mods) => Promise.all([
      send("Input.dispatchKeyEvent", { type: "keyDown", key: keyName, code: keyName, windowsVirtualKeyCode: vk, modifiers: mods }),
      send("Input.dispatchKeyEvent", { type: "keyUp", key: keyName, code: keyName, windowsVirtualKeyCode: vk, modifiers: mods }),
    ]);
    const selJson = () => send("Runtime.evaluate", { expression: `JSON.stringify(window.__vc.state.selection)`, returnByValue: true })
      .then(r2 => JSON.parse(r2.result.result.value));
    const S0 = await selJson();
    await keyEv("ArrowRight", 39, 8);   // Shift → end +0.5
    await keyEv("ArrowRight", 39, 1);   // Alt → start +0.5
    await keyEv("ArrowRight", 39, 0);   // 平移 → 双边 +0.5
    const S1 = await selJson();
    const okK = S0 && S1 && Math.abs((S1.end - S0.end) - 1.0) < 0.26 && Math.abs((S1.start - S0.start) - 1.0) < 0.26;
    console.log("键盘微调:", JSON.stringify({ before: S0, after: S1 }), okK ? "KEY-NUDGE PASS" : "KEY-NUDGE FAIL");

    console.log("页面错误:", errors.length ? errors.join(" | ") : "（无）");
    console.log(ok ? "REAL-DRAG PASS" : "REAL-DRAG FAIL", "|", okW ? "WAVE-DRAG PASS" : "WAVE-DRAG FAIL");
    process.exitCode = (ok && okW && okK) ? 0 : 1;
  } finally {
    try { child.kill(); } catch (e) {}
    try { fs.rmSync(PROF, { recursive: true, force: true }); } catch (e) {}
  }
})();
