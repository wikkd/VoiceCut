// 截图: 选中视频素材后的应用界面
const { spawn } = require("child_process");
const fs = require("fs");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9360;
const PROF = process.env.TEMP + "\\vc-cdp-shot-" + Date.now();
const SHOT = process.env.TEMP + "\\vc-app-shot.png";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, "--window-size=1400,1000",
    "http://127.0.0.1:8765/"], { stdio: "ignore" });
  try {
    let target = null;
    for (let i = 0; i < 30 && !target; i++) { try { const res = await fetch(`http://127.0.0.1:${PORT}/json`); const l = await res.json(); target = l.find(t => t.type === "page") || null; } catch (e) {} if (!target) await sleep(500); }
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let msgId = 0; const pending = new Map();
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
    const send = (method, params = {}) => new Promise((res) => { const id = ++msgId; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
    await send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 1000, deviceScaleFactor: 1, mobile: false });
    await sleep(2500);
    // 选中视频素材
    const sel = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const item = vc.state.items.find(i => i.kind === 'video');
      if (!item) return 'no video item';
      await vc.selectItem(item);
      await new Promise(r => setTimeout(r, 2500));
      return { id: item.id, video_url: item.video_url, panelHidden: document.querySelector('#video-panel').classList.contains('no-video'),
               workspace: (() => { const w = document.querySelector('#workspace'); return w ? { cols: getComputedStyle(w).gridTemplateColumns, rows: getComputedStyle(w).gridTemplateRows } : null; })(),
               videoRect: (() => { const v = document.querySelector('#video-preview'); const r = v.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height), vw: v.videoWidth, vh: v.videoHeight }; })() };
    })()`, awaitPromise: true, returnByValue: true });
    console.log("SELECT:", JSON.stringify(sel.result && sel.result.result && sel.result.result.value));
    // 再等一下视频出画面
    await sleep(2000);
    const shot = await send("Page.captureScreenshot", { format: "png" });
    const b64 = shot.result && shot.result.data;
    fs.writeFileSync(SHOT, Buffer.from(b64, "base64"));
    console.log("SHOT saved:", SHOT);
    ws.close();
  } finally { child.kill(); }
})();
