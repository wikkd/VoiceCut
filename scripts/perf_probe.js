// VoiceCut 剪辑页卡顿诊断探针（只读，不改数据）
// 用法: node scripts/perf_probe.js [目标URL] [CDP端口]
// 对真实运行中的页面测: 空闲/播放时的 FPS、长任务(>50ms)数量与归因、关键渲染函数调用频次
const { spawn } = require("child_process");
const path = require("path");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = Number(process.argv[3] || 9351);
const URL_ = process.argv[2] || "http://127.0.0.1:8765/";
const PROF = process.env.TEMP + "\\vc-probe-" + Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, URL_], { stdio: "ignore" });
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
    await sleep(3500); // 等引导 + 波形解码

    const probe = `(async () => {
      const out = {};
      const vc = window.__vc;
      out.boot = document.body.dataset.vc;
      out.hasWs = !!(vc && vc.state && vc.state.ws);
      // 1) 找到目标素材所在项目并切换（默认压测最重的 m-c2f20f0598）
      const TARGET = "m-c2f20f0598";
      try {
        if (!vc.state.currentItem || vc.state.currentItem.id !== TARGET) {
          const projs = await (await fetch("/api/projects")).json();
          for (const p of projs) {
            const full = await (await fetch("/api/projects/" + p.id)).json();
            if ((full.items || []).some(x => x.id === TARGET)) {
              const it = full.items.find(x => x.id === TARGET);
              await vc.selectProject({ id: p.id, name: p.name });
              await new Promise(r => setTimeout(r, 500));
              await vc.selectItem(it);
              break;
            }
          }
          await new Promise(r => setTimeout(r, 4000)); // 等波形解码 + 片段渲染
        }
      } catch (e) { out.pickErr = String(e); }
      out.item = vc.state.currentItem ? vc.state.currentItem.name : null;
      out.dur = vc.state.currentItem ? Math.round(vc.state.currentItem.duration) : null;
      out.segRows = document.querySelectorAll('#seg-tbody tr').length;
      out.subRows = document.querySelectorAll('#sub-tbody tr').length;
      out.regions = vc.state.ws ? (vc.state.ws.regions ? Object.keys(vc.state.ws.regions.list || {}).length : "n/a") : null;
      // 2) 统计渲染函数调用频次
      const counts = {};
      for (const name of ["renderSegments", "renderSubs", "renderPool", "renderMediaList"]) {
        if (vc && typeof vc[name] === "function") {
          const orig = vc[name].bind(vc);
          counts[name] = 0;
          vc[name] = (...a) => { counts[name]++; return orig(...a); };
        }
      }
      // 3) 长任务收集
      const longtasks = [];
      try {
        new PerformanceObserver((list) => {
          for (const e of list.getEntries()) longtasks.push({ dur: Math.round(e.duration), at: e.name });
        }).observe({ entryTypes: ["longtask"] });
      } catch (e) {}
      function fps(durationMs) {
        return new Promise((res) => {
          let frames = 0; const t0 = performance.now();
          function loop() {
            frames++;
            if (performance.now() - t0 >= durationMs) res(frames / (durationMs / 1000));
            else requestAnimationFrame(loop);
          }
          requestAnimationFrame(loop);
        });
      }
      const idleFps = await fps(4000);
      out.idle = { fps: Math.round(idleFps), longtasks: longtasks.splice(0).sort((a,b)=>b.dur-a.dur).slice(0, 8) };
      // 4) 播放 4 秒
      if (vc.state.ws) {
        try { vc.state.ws.play(); } catch (e) { out.playErr = String(e); }
        await new Promise(r => setTimeout(r, 300));
        const playFps = await fps(4000);
        try { vc.state.ws.pause(); } catch (e) {}
        out.playing = { fps: Math.round(playFps), longtasks: longtasks.splice(0).sort((a,b)=>b.dur-a.dur).slice(0, 8), renders: { ...counts } };
      }
      // 5) 交互压力：连打 12 次多选标记（每次都新增 region + 重渲染）再播 3 秒
      try {
        vc.state.ws.setTime(1);
        for (let i = 0; i < 12; i++) { vc.markForward(); await new Promise(r => setTimeout(r, 120)); }
        const markFps = await fps(2000);
        vc.state.ws.play();
        await new Promise(r => setTimeout(r, 200));
        const markedPlayFps = await fps(3000);
        vc.state.ws.pause();
        out.marked = {
          fpsDuringMark: Math.round(markFps),
          fpsPlaying: Math.round(markedPlayFps),
          longtasks: longtasks.splice(0).sort((a,b)=>b.dur-a.dur).slice(0, 10),
          renders: { ...counts },
        };
        vc.clearMultiRegions();
      } catch (e) { out.markErr = String(e); }
      return out;
    })()`;

    const r = await send("Runtime.evaluate", { expression: probe, awaitPromise: true, returnByValue: true });
    console.log(JSON.stringify(r.result.result.value, null, 1));
  } finally {
    child.kill();
    try { spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" }); } catch (e) {}
  }
})();
