// 时间轴拖拽选区专项验证：普通拖选 / Ctrl 多选 / 原地点击跳转
// 用法: node scripts/diag_timeline_sel.js
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = Number(process.argv[2] || 9351);
const PROF = process.env.TEMP + "\\vc-cdp-" + Date.now();
const ROOT = path.join(__dirname, "..");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const SRV_PORT = Number(process.env.VC_PORT || 8899);
const BASE = `http://127.0.0.1:${SRV_PORT}`;
const WORKDIR = fs.mkdtempSync(path.join(os.tmpdir(), "vc-dts-"));
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
  const server = spawn(PY, ["voicecut.py", "--port", String(SRV_PORT), "--no-browser", "--workdir", WORKDIR],
    { cwd: ROOT, stdio: "ignore" });
  let ready = false;
  for (let i = 0; i < 60 && !ready; i++) {
    try { ready = (await fetch(`${BASE}/api/config`)).ok; } catch (e) {}
    if (!ready) await sleep(500);
  }
  if (!ready) throw new Error(`server not ready on ${BASE}`);
  const seedFd = new FormData();
  seedFd.append("file", new Blob([makeWav(20)], { type: "audio/wav" }), "seed.wav");
  await (await fetch(`${BASE}/api/import`, { method: "POST", body: seedFd })).json();
  // 等导入任务收尾（素材出现在库里即可）
  await sleep(2500);

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
    const send = (method, params) => new Promise((res) => { const id = ++msgId; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
    for (let i = 0; i < 60; i++) {
      const b = await send("Runtime.evaluate", { expression: `!!(window.__vc && window.__vc.state.ws)`, returnByValue: true });
      if (b.result.result.value) break;
      await sleep(500);
    }
    await sleep(1000); // 波形 ready

    const drag = (x1r, x2r, ctrl) => send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const tl = document.querySelector('#timeline');
      const wr = vc.state.ws.getWrapper().getBoundingClientRect();
      const dur = vc.state.ws.getDuration();
      const y = tl.getBoundingClientRect().top + 5;
      const x1 = wr.left + wr.width * ${x1r}, x2 = wr.left + wr.width * ${x2r};
      const opts = (x) => ({ bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0, ctrlKey: ${!!ctrl} });
      tl.dispatchEvent(new MouseEvent('mousedown', opts(x1)));
      await new Promise(r => setTimeout(r, 30));
      window.dispatchEvent(new MouseEvent('mousemove', opts(x2)));
      await new Promise(r => setTimeout(r, 30));
      window.dispatchEvent(new MouseEvent('mouseup', opts(x2)));
      await new Promise(r => setTimeout(r, 50));
      const s = vc.state.selection;
      return { sel: s ? { start: Math.round(s.start * 100) / 100, end: Math.round(s.end * 100) / 100 } : null,
               eA: Math.round(${x1r} * dur * 100) / 100, eB: Math.round(${x2r} * dur * 100) / 100,
               region: !!vc.state.selectionRegion, marks: vc.state.multiRegions.length };
    })()`, returnByValue: true, awaitPromise: true });

    const r1 = (await drag(0.2, 0.4, false)).result.result.value;
    console.log("普通拖选:", JSON.stringify(r1));
    const ok1 = r1 && r1.sel && Math.abs(r1.sel.start - r1.eA) < 0.3 && Math.abs(r1.sel.end - r1.eB) < 0.3 && r1.region;

    const r2 = (await drag(0.5, 0.6, true)).result.result.value;
    console.log("Ctrl 多选:", JSON.stringify(r2));
    const ok2 = r2 && r2.marks === 1 && r2.sel && Math.abs(r2.sel.start - r2.eA) < 0.3;

    const r3 = (await drag(0.6, 0.8, false)).result.result.value;
    console.log("再普通拖选(清多选):", JSON.stringify(r3));
    const ok3 = r3 && r3.marks === 0 && r3.sel && Math.abs(r3.sel.end - r3.eB) < 0.3;

    const r4 = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const tl = document.querySelector('#timeline');
      const wr = vc.state.ws.getWrapper().getBoundingClientRect();
      const y = tl.getBoundingClientRect().top + 5;
      const x = wr.left + wr.width * 0.7;
      const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 };
      tl.dispatchEvent(new MouseEvent('mousedown', opts));
      window.dispatchEvent(new MouseEvent('mouseup', opts));
      await new Promise(r => setTimeout(r, 50));
      return { t: Math.round(vc.state.ws.getCurrentTime() * 100) / 100, e: Math.round(vc.state.ws.getDuration() * 0.7 * 100) / 100 };
    })()`, returnByValue: true, awaitPromise: true });
    const r4v = r4.result.result.value;
    console.log("原地点击跳转:", JSON.stringify(r4v));
    const ok4 = r4v && Math.abs(r4v.t - r4v.e) < 0.3;

    const allOk = ok1 && ok2 && ok3 && ok4;
    console.log(allOk ? "TIMELINE-SEL ALL PASS" : "TIMELINE-SEL FAIL");
    process.exitCode = allOk ? 0 : 1;
  } finally {
    try { child.kill(); } catch (e) {}
    try { server.kill(); } catch (e) {}
    try { fs.rmSync(WORKDIR, { recursive: true, force: true }); } catch (e) {}
  }
})();
