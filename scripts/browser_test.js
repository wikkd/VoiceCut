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
      if (!vc.state.items.length) return { ok: false, reason: 'no items' };
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
      return {
        ok: true,
        boot: document.body.dataset.vc,
        canvases,
        region: region ? { start: region.start, end: region.end } : null,
        selection: vc.state.selection,
        playErr, errs,
        duration: vc.state.currentItem.duration,
        videoVisible: !document.querySelector('#video-panel').classList.contains('hidden'),
        hasItems: vc.state.items.length,
      };
    })()`;

    const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    console.log(JSON.stringify(r.result && r.result.result && r.result.result.value, null, 2));
    if (r.result && r.result.exceptionDetails) console.log("EXC:", JSON.stringify(r.result.exceptionDetails));
    ws.close();
  } finally { child.kill(); }
})();
