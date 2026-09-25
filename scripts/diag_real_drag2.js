// 插桩诊断：真实输入下 #timeline 与 window 收到哪些事件 + _tlTime 映射值
const { spawn } = require("child_process");
const fs = require("fs");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = Number(process.argv[2] || 9353);
const BASE = process.argv[3] || "http://127.0.0.1:8765";
const PROF = process.env.TEMP + "\\vc-rdiag-" + Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF,
    "--window-size=" + (process.env.VC_WIN || "800,600"), `${BASE}/`], { stdio: "ignore" });
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
    await sleep(1200);
    // 插桩：记录事件流 + 命中元素
    await send("Runtime.evaluate", { expression: `(() => {
      window.__evts = [];
      const rec = (tag) => (e) => window.__evts.push(tag + ":" + e.type + "@x" + Math.round(e.clientX) + (e.defaultPrevented ? ":prev" : ""));
      const tl = document.querySelector('#timeline');
      ["pointerdown","mousedown","pointermove","mousemove","pointerup","mouseup"].forEach(t => {
        tl.addEventListener(t, rec("TL"), true);
        window.addEventListener(t, rec("WIN"), true);
      });
      return "ok";
    })()`, returnByValue: true });
    const geom = (await send("Runtime.evaluate", { expression: `(() => {
      const tl = document.querySelector('#timeline'), wr = window.__vc.state.ws.getWrapper();
      const tb = tl.getBoundingClientRect(), wb = wr.getBoundingClientRect();
      return { tlW: tb.width, tlX: tb.left, tlY: tb.top, tlH: tb.height,
               wrW: wb.width, wrX: wb.left, wrScrollW: wr.scrollWidth, dur: window.__vc.state.ws.getDuration() };
    })()`, returnByValue: true })).result.result.value;
    console.log("geom:", JSON.stringify(geom));

    const x1 = geom.tlX + geom.tlW * 0.30, x2 = geom.tlX + geom.tlW * 0.45, y = geom.tlY + geom.tlH / 2;
    const btn = (type, x, pressed) => send("Input.dispatchMouseEvent", {
      type, x, y, button: "left", buttons: pressed ? 1 : 0, clickCount: (type === "mousePressed" || type === "mouseReleased") ? 1 : 0 });
    await btn("mousePressed", x1, true); await sleep(60);
    for (let k = 1; k <= 5; k++) await btn("mouseMoved", x1 + (x2 - x1) * k / 5, true);
    await btn("mouseReleased", x2, false); await sleep(200);

    const out = await send("Runtime.evaluate", { expression: `(() => {
      const tl = document.querySelector('#timeline');
      const tb = tl.getBoundingClientRect();
      const cx = tb.left + tb.width * 0.35, cy = tb.top + tb.height / 2;
      const el = document.elementFromPoint(cx, cy);
      const chain = [];
      let n = el;
      while (n && n !== document.body) {
        chain.push(n.tagName + (n.id ? "#" + n.id : "") + (n.className && typeof n.className === "string" ? "." + n.className.split(" ").join(".") : "") + (n.part ? "[part=" + n.part + "]" : ""));
        n = n.parentElement;
      }
      return { evts: window.__evts.slice(0, 30), sel: (window.__vc.state.selection || null),
               hit: el ? el.tagName + (el.id ? "#" + el.id : "") : null, chain };
    })()`, returnByValue: true });
    console.log("事件流:", JSON.stringify(out.result.result.value.evts));
    console.log("命中元素:", out.result.result.value.hit, "祖先链:", JSON.stringify(out.result.result.value.chain));
    console.log("selection:", JSON.stringify(out.result.result.value.sel));
  } finally {
    try { child.kill(); } catch (e) {}
    try { fs.rmSync(PROF, { recursive: true, force: true }); } catch (e) {}
  }
})();
