// 片段列表「点击不跳转」——按【列坐标】真实鼠标点击取证
// 用法：node scripts/diag_jump_click.js [项目id]
//
// 上一版探针用 tr.click() 直接命中 <tr>，绕过了「用户点在哪一列」这个变量。
// 真实鼠标点在行内不同列上，走的是完全不同的分支：
//   .col-text 里的 <input class="seg-text"> / .col-spk/.col-lang 里的 <select>
//   → 命中 app.js 的 `if (evtEl(e).closest("input, select")) return;` → **静默不跳转**
//   其余列（序号/来源/时间/时长/状态/试听/操作）才走跳转分支。
// 本脚本用 CDP 真实鼠标事件逐列点击，量化「哪一列点了不跳」。
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9415;
const PROF = process.env.TEMP + "\\vc-jumpclick-" + Date.now();
const BASE = process.env.VC_BASE || "http://127.0.0.1:8765";
const TARGET_PROJECT = process.argv[2] || null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--autoplay-policy=no-user-gesture-required", "--mute-audio",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, `${BASE}/`], { stdio: "ignore" });
  try {
    let target = null;
    for (let i = 0; i < 40 && !target; i++) {
      try { const res = await fetch(`http://127.0.0.1:${PORT}/json`); const l = await res.json(); target = l.find((t) => t.type === "page") || null; } catch (e) {}
      if (!target) await sleep(500);
    }
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let msgId = 0; const pending = new Map(); const errs = [];
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.method === "Runtime.exceptionThrown") errs.push((m.params.exceptionDetails.exception || {}).description || m.params.exceptionDetails.text);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    };
    const send = (method, params = {}) => new Promise((res) => { const id = ++msgId; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
    const ev = async (expr) => {
      const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.result.exceptionDetails) return { __exc: JSON.stringify(r.result.exceptionDetails).slice(0, 400) };
      return r.result.result.value;
    };
    await send("Runtime.enable");
    await sleep(3500);
    await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1100, deviceScaleFactor: 1, mobile: false });

    const boot = await ev(`(async () => {
      const sl = (ms) => new Promise((r) => setTimeout(r, ms));
      for (let i = 0; i < 250 && !window.__vc; i++) await sl(100);
      const vc = window.__vc;
      if (!vc) return { err: 'no __vc', href: location.href, boot: (document.getElementById('boot-state')||{}).textContent };
      const plist = await (await fetch('/api/projects')).json();
      const wanted = ${JSON.stringify(TARGET_PROJECT)};
      const proj = wanted ? plist.find(p => p.id === wanted) : plist.find(p => p.item_count > 5);
      await vc.selectProject(proj);
      for (let i = 0; i < 1200; i++) {
        if (vc.state._segRows && vc.state._segRows.length > 0) break;
        await sl(100);
      }
      document.getElementById('seg-scroll').scrollTop = 0;
      await sl(400);
      return { project: proj.name, rows: (vc.state._segRows || []).length,
               domRows: document.querySelectorAll('#seg-tbody tr.seg-row').length };
    })()`);
    if (boot.err) { console.error("FATAL:", JSON.stringify(boot)); return; }
    console.log("PROJECT:", boot.project, "| rows", boot.rows, "| domRows", boot.domRows);

    // 行几何 + 每列的点击坐标
    const geo = await ev(`(() => {
      const vc = window.__vc;
      const sc = document.getElementById('seg-scroll');
      sc.scrollTop = 0;
      const tr = document.querySelector('#seg-tbody tr.seg-row');
      if (!tr) return { err: 'no row' };
      const r = tr.getBoundingClientRect();
      const i = Number(tr.dataset.i), item = tr.dataset.item;
      const seg = (vc.state.segmentsByItem.get(item) || [])[i];
      const cells = Array.from(tr.children).map((td) => {
        const b = td.getBoundingClientRect();
        let inner = td.querySelector('input, select, button');
        return { cls: td.className, cx: Math.round(b.x + b.width / 2), w: Math.round(b.width),
                 inner: inner ? inner.tagName.toLowerCase() + '.' + String(inner.className).split(' ')[0] : null };
      });
      return { y: Math.round(r.y + r.height / 2), h: Math.round(r.height), cells, i, item, segStart: seg ? seg.start : null,
               tableX: Math.round(document.getElementById('seg-table').getBoundingClientRect().x) };
    })()`);
    if (geo.err) { console.error("FATAL geo:", JSON.stringify(geo)); return; }
    console.log("ROW y=" + geo.y + " h=" + geo.h + " segStart=" + geo.segStart + " i=" + geo.i);
    console.log("列: " + geo.cells.map(c => c.cls.split(" ").pop() + "(" + c.inner + "@x" + c.cx + ",w" + c.w + ")").join("  "));

    const clickAt = async (x, y) => {
      const base = { x, y, button: "left", clickCount: 1, buttons: 1 };
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", ...base, buttons: 0 });
      await send("Input.dispatchMouseEvent", { type: "mousePressed", ...base });
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", ...base });
    };

    console.log("\n=== 逐列真实鼠标点击（目标 seg.start=" + geo.segStart + "）===");
    const bad = [];
    // 安全护栏：含 input/select/button 的单元格一律不点 —— 点在「删除」按钮上会真的删片段
    // （2026-09-26 踩过：m-c2f20f0598 少了一段，靠 voicecut.db 主库快照才捞回）。
    for (const c of geo.cells) {
      if (c.inner) { console.log("  SKIP x=" + String(c.cx).padStart(4) + " " + c.cls.split(" ").pop() +
        " inner=" + c.inner + "  ← 含交互控件，探针不点"); continue; }
      // 每次先把播放头挪走 + 清状态
      await ev(`(() => { const vc = window.__vc;
        if (vc.state.ws) vc.state.ws.setTime(Math.max(0, ${geo.segStart} > 60 ? ${geo.segStart} - 55 : ${geo.segStart} + 55));
        vc.state.activeSeg = null; vc.state.selectedSegs = new Set(); vc.renderSegments();
        document.activeElement && document.activeElement.blur && document.activeElement.blur();
        return 1; })()`);
      await sleep(250);
      await clickAt(c.cx, geo.y);
      await sleep(500);
      const res = await ev(`(() => { const vc = window.__vc;
        const t = vc.state.ws ? vc.state.ws.getCurrentTime() : null;
        const seg = (vc.state.segmentsByItem.get(${JSON.stringify(geo.item)}) || [])[${geo.i}];
        return { t: t == null ? null : +t.toFixed(2),
                 near: t != null && seg ? Math.abs(t - seg.start) < 0.4 : false,
                 active: !!(vc.state.activeSeg && seg && vc.state.activeSeg.segId === seg.id),
                 selN: vc.state.selectedSegs.size,
                 focus: document.activeElement ? document.activeElement.tagName.toLowerCase() + '.' +
                        String(document.activeElement.className).split(' ')[0] : null }; })()`);
      const tag = res.near ? "跳到" : "未跳";
      if (!res.near) bad.push(c);
      console.log("  " + (res.near ? "OK  " : "FAIL") + " x=" + String(c.cx).padStart(4) +
        " " + c.cls.split(" ").pop().padEnd(10) + " inner=" + String(c.inner).padEnd(18) +
        " → t=" + String(res.t).padStart(9) + " active=" + res.active +
        " 选中=" + res.selN + " focus=" + res.focus + "   [" + tag + "]");
    }
    console.log("\n不跳转的列: " + (bad.length ? bad.map(b => b.cls.split(" ").pop() + "(" + b.inner + ")").join(", ") : "无"));
    console.log("CDP exceptions: " + JSON.stringify(errs.slice(0, 6)));
  } finally {
    child.kill();
    await sleep(400);
    try { fs.rmSync(PROF, { recursive: true, force: true }); } catch (e) {}
  }
})();
