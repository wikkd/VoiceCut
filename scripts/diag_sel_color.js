// 选区颜色真实交互探针：真实鼠标拖手柄 → 黄；真实 Enter → 蓝（与合成事件路径区分）
const { spawn } = require("child_process");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = Number(process.argv[2] || 9391);
const BASE = process.argv[3] || "http://127.0.0.1:8765";
const PROF = process.env.TEMP + "\\vc-selc-" + Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--window-size=1600,900",
    "--no-first-run", "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, `${BASE}/`], { stdio: "ignore" });
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
    const ev = async (expr) => {
      const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.result && r.result.exceptionDetails) return { __exc: JSON.stringify(r.result.exceptionDetails).slice(0, 300) };
      return r.result && r.result.result && r.result.result.value;
    };
    const mouse = (type, x, y, btn) => send("Input.dispatchMouseEvent",
      { type, x, y, button: btn || "left", buttons: type === "mouseReleased" ? 0 : 1, clickCount: 1 });
    const key = (t) => send("Input.dispatchKeyEvent", { type: t, key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
    for (let i = 0; i < 60; i++) { if (await ev(`!!(window.__vc && window.__vc.state.ws)`)) break; await sleep(500); }
    // 准备：选中素材 → 真实点击片段列表第一行（走 focusSegment 完整链路，region 才会创建）
    const prep0 = await ev(`(async () => {
      const vc = window.__vc;
      const item = (vc.state.items || [])[0];
      if (!item) return { err: 'no item' };
      await vc.selectItem(item);
      await new Promise(r => setTimeout(r, 800));
      const segs = vc.state.segmentsByItem.get(item.id) || [];
      if (!segs.length) return { err: 'no segs' };
      return { itemId: item.id, segId: segs[0].id, seg: { s: segs[0].start, e: segs[0].end }, dur: item.duration };
    })()`);
    console.log("PREP0:", JSON.stringify(prep0));
    // 真实点击片段行
    const rowBox = await ev(`(() => {
      const tr = document.querySelector('#seg-tbody tr.seg-row');
      if (!tr) return null;
      // 找行内第一个非控件 cell（时间/来源列），避免点到文本输入框被守卫早退
      const cells = [...tr.querySelectorAll('td')].filter(td => !td.querySelector('input, select'));
      const td = cells[0] || tr;
      const b = td.getBoundingClientRect();
      return { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) };
    })()`);
    if (!rowBox) { console.log("NO ROW"); process.exit(1); }
    const hit = await ev(`(() => { const el = document.elementFromPoint(${rowBox.x}, ${rowBox.y});
      return el ? { tag: el.tagName, cls: String(el.className).slice(0, 60), tr: !!el.closest('tr.seg-row') } : null; })()`);
    console.log("ROWBOX:", JSON.stringify(rowBox), "HIT:", JSON.stringify(hit));
    await mouse("mouseMoved", rowBox.x, rowBox.y); await sleep(80);
    await mouse("mousePressed", rowBox.x, rowBox.y); await sleep(60);
    await mouse("mouseReleased", rowBox.x, rowBox.y);
    await sleep(500);
    const prep = await ev(`(() => {
      const vc = window.__vc;
      const regs = document.querySelectorAll('.wavesurfer-region, region');
      const info = [...regs].map(el => ({ cls: String(el.className), tag: el.tagName,
        r: (() => { const b = el.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) }; })() }));
      return { activeSeg: vc.state.activeSeg, sel: vc.state.selection, selRegion: !!vc.state.selectionRegion,
        regionCount: regs.length, info, color: vc.state._selColor };
    })()`);
    console.log("PREP:", JSON.stringify(prep, null, 1));
    // 真实拖动：拖选区 region 右手柄（从 state.selectionRegion.element 取 DOM）
    const box = await ev(`(() => {
      const el = window.__vc.state.selectionRegion && window.__vc.state.selectionRegion.element;
      if (!el) return null;
      const b = el.getBoundingClientRect();
      return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) };
    })()`);
    if (box) {
      const y = box.y + Math.floor(box.h / 2);
      await mouse("mouseMoved", box.x + box.w - 3, y);
      await sleep(120);
      await mouse("mousePressed", box.x + box.w - 3, y);
      for (let i = 1; i <= 8; i++) { await mouse("mouseMoved", box.x + box.w - 3 + i * 5, y); await sleep(40); }
      const during = await ev(`({ color: window.__vc.state._selColor, sel: window.__vc.state.selection })`);
      await mouse("mouseReleased", box.x + box.w - 3 + 40, y);
      await sleep(400);
      const after = await ev(`({ color: window.__vc.state._selColor, sel: window.__vc.state.selection,
        pend: window.__vc.selPending ? window.__vc.selPending() : null })`);
      console.log("DURING:", JSON.stringify(during));
      console.log("AFTER:", JSON.stringify(after));
      // 真实 Enter 键
      await key("keyDown"); await key("char"); await key("keyUp");
      await sleep(500);
      const afterEnter = await ev(`({ color: window.__vc.state._selColor,
        seg: (() => { const a = window.__vc.state.activeSeg; const s = window.__vc.state.segmentsByItem.get(a.itemId).find(x => x.id === a.segId); return { s: s.start, e: s.end }; })(),
        sel: window.__vc.state.selection })`);
      console.log("AFTER_ENTER:", JSON.stringify(afterEnter));
    }
  } finally { child.kill(); }
})();
