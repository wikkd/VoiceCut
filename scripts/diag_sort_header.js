// 表头排序/筛选探针：点击列头箭头循环排序、行序随 segSort 变化、表头内筛选生效
// 用法: node scripts/diag_sort_header.js [cdpPort] [base]
const { spawn } = require("child_process");
const fs = require("fs");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = Number(process.argv[2] || 9379);
const BASE = process.argv[3] || "http://127.0.0.1:8765";
const PROF = process.env.TEMP + "\\vc-srt-" + Date.now();
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
      const thDur = document.querySelector('#seg-table thead th[data-sort="dur"]');
      const thTime = document.querySelector('#seg-table thead th[data-sort="time"]');
      const arrow = () => thDur.querySelector(".sort-arrow").textContent;
      const durations = () => [...document.querySelectorAll("#seg-tbody tr.seg-row")]
        .map(tr => { const seg = [...vc.state.segmentsByItem.get(tr.dataset.item) || []][Number(tr.dataset.i)]; return seg ? +(seg.end - seg.start).toFixed(2) : null; });
      const click = (el) => el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      const out = { hasArrow: !!arrow() };
      click(thDur);  await new Promise(r2 => setTimeout(r2, 120));
      out.afterClick1 = { sort: vc.state.segSort, arrow: arrow(), ok: vc.state.segSort === "dur" && arrow() === "▼" };
      out.durDesc = durations();
      click(thDur);  await new Promise(r2 => setTimeout(r2, 120));
      out.afterClick2 = { sort: vc.state.segSort, arrow: arrow(), ok: vc.state.segSort === "dur_asc" && arrow() === "▲" };
      click(thTime); await new Promise(r2 => setTimeout(r2, 120));
      // 恢复默认 time 排序后：时长列箭头回未激活 ⇅；起止列为激活升序 ▲
      out.afterReset = { sort: vc.state.segSort, arrow: arrow(),
        arrowTime: thTime.querySelector(".sort-arrow").textContent,
        ok: vc.state.segSort === "time" && arrow() === "⇅" && thTime.querySelector(".sort-arrow").textContent === "▲" };
      // 表头内筛选
      const inp = document.querySelector("#seg-filter-text");
      out.filterInThead = !!inp && !!inp.closest("thead");
      inp.value = "ゼロ"; inp.dispatchEvent(new Event("input", { bubbles: true }));
      await new Promise(r2 => setTimeout(r2, 500));
      out.rowsAfterFilter = document.querySelectorAll("#seg-tbody tr.seg-row").length;
      inp.value = ""; inp.dispatchEvent(new Event("input", { bubbles: true }));
      await new Promise(r2 => setTimeout(r2, 500));
      out.rowsRestored = document.querySelectorAll("#seg-tbody tr.seg-row").length;
      // 来源素材筛选：选项随素材列表填充；选中某素材后行数 = 该素材片段数
      const sel = document.querySelector("#seg-filter-item");
      out.itemSelInThead = !!sel && !!sel.closest("thead");
      const items = vc.state.items || [];
      out.itemOptCount = sel.options.length;
      const countSegsOf = (id) => (vc.state.segmentsByItem.get(id) || []).length;
      if (items.length >= 1) {
        sel.value = items[0].id; sel.dispatchEvent(new Event("change", { bubbles: true }));
        await new Promise(r2 => setTimeout(r2, 300));
        out.rowsFirstItem = document.querySelectorAll("#seg-tbody tr.seg-row").length;
        out.expectFirstItem = countSegsOf(items[0].id);
        out.counterText = document.querySelector("#seg-count").textContent;
        sel.value = "all"; sel.dispatchEvent(new Event("change", { bubbles: true }));
        await new Promise(r2 => setTimeout(r2, 300));
        out.rowsAfterItemReset = document.querySelectorAll("#seg-tbody tr.seg-row").length;
      }
      out.ok = out.itemSelInThead && out.itemOptCount === items.length + 1
        && (items.length < 1 || (out.rowsFirstItem === out.expectFirstItem && out.rowsAfterItemReset === out.rowsRestored));
      return out;
    })()`, returnByValue: true, awaitPromise: true });
    const v = r.result.result.value;
    console.log("结果:", JSON.stringify(v));
    const ok = v.hasArrow && v.afterClick1.ok && v.afterClick2.ok && v.afterReset.ok
      && v.filterInThead && v.rowsAfterFilter >= 0 && v.rowsAfterFilter <= v.rowsRestored
      && v.ok;
    console.log(ok ? "SORT-HEADER PASS" : "SORT-HEADER FAIL");
    process.exitCode = ok ? 0 : 1;
  } finally {
    try { child.kill(); } catch (e) {}
    try { fs.rmSync(PROF, { recursive: true, force: true }); } catch (e) {}
  }
})();
