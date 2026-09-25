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
    console.log("页面错误:", errors.length ? errors.join(" | ") : "（无）");
    console.log(ok ? "REAL-DRAG PASS" : "REAL-DRAG FAIL");
    process.exitCode = ok ? 0 : 1;
  } finally {
    try { child.kill(); } catch (e) {}
    try { fs.rmSync(PROF, { recursive: true, force: true }); } catch (e) {}
  }
})();
