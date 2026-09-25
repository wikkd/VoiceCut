// 片段行点击 → 工作选区断言：state.selection 设为片段区间、region 可拉伸不可拖动
// 用法: node scripts/diag_seg_highlight.js [cdpPort] [base]
const { spawn } = require("child_process");
const fs = require("fs");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = Number(process.argv[2] || 9363);
const BASE = process.argv[3] || "http://127.0.0.1:8765";
const PROF = process.env.TEMP + "\\vc-shl-" + Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, `${BASE}/`], { stdio: "ignore" });
  try {
    let target = null;
    for (let i = 0; i < 60 && !target; i++) {
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
    await sleep(1200);
    const r = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const row = document.querySelector("#seg-tbody tr.seg-row");
      if (!row) return { skip: "no seg rows" };
      const segId = row.querySelector(".seg-text") ? row.querySelector(".seg-text").dataset.i : null;
      row.click();
      await new Promise(r2 => setTimeout(r2, 400));
      const rg = vc.state.selectionRegion;
      return {
        activeSeg: vc.state.activeSeg,
        sel: vc.state.selection ? { start: Math.round(vc.state.selection.start*100)/100, end: Math.round(vc.state.selection.end*100)/100 } : null,
        drag: rg ? rg.drag : null, resize: rg ? rg.resize : null,
        nSel: vc.state.selectedSegs.size,
      };
    })()`, returnByValue: true, awaitPromise: true });
    const v = r.result.result.value;
    console.log("结果:", JSON.stringify(v));
    if (v.skip) { console.log("SKIP"); return; }
    const ok = v.activeSeg && v.sel && v.sel.end > v.sel.start && v.drag === false && v.resize === true && v.nSel === 1;
    console.log(ok ? "SEG-SELECTION PASS" : "SEG-SELECTION FAIL");
    process.exitCode = ok ? 0 : 1;
  } finally {
    try { child.kill(); } catch (e) {}
    try { fs.rmSync(PROF, { recursive: true, force: true }); } catch (e) {}
  }
})();
