// 功能验证：点击片段行定位 → 拖动边界优化 → 自动转写回填
// 自带临时 workdir 后端（与生产隔离）；/api/transcribe 与 /api/tasks/<id> 请求被前端拦截，不触发真实模型。
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const ROOT = path.join(__dirname, "..");
const PORT = Number(process.argv[2] || 9356);
const CDP_PORT = 9357;
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const WORKDIR = fs.mkdtempSync(path.join(os.tmpdir(), "vc-segf-"));
const PROF = process.env.TEMP + "\\vc-segf-" + Date.now();
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
  const server = spawn(PY, ["voicecut.py", "--port", String(PORT), "--no-browser", "--workdir", WORKDIR],
    { cwd: ROOT, stdio: "ignore" });
  try {
    let ready = false;
    for (let i = 0; i < 60 && !ready; i++) {
      try { ready = (await fetch(`${BASE}/api/config`)).ok; } catch (e) {}
      if (!ready) await sleep(500);
    }
    if (!ready) throw new Error("temp server not ready");
    const fd = new FormData();
    fd.append("file", new Blob([makeWav(20)], { type: "audio/wav" }), "seg.wav");
    const imp = await (await fetch(`${BASE}/api/import`, { method: "POST", body: fd })).json();
    for (let i = 0; i < 80; i++) {
      await sleep(500);
      const t = await (await fetch(`${BASE}/api/tasks/${imp.task_id}`)).json();
      if (t && (t.status === "done" || t.status === "failed")) break;
    }

    const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
      "--autoplay-policy=no-user-gesture-required",
      "--remote-debugging-port=" + CDP_PORT, "--user-data-dir=" + PROF, BASE + "/"], { stdio: "ignore" });
    let target = null;
    for (let i = 0; i < 30 && !target; i++) {
      try { const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json`); const l = await res.json(); target = l.find((t) => t.type === "page") || null; } catch (e) {}
      if (!target) await sleep(500);
    }
    if (!target) throw new Error("CDP target not found");
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let msgId = 0; const pending = new Map();
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
    const send = (method, params = {}) => new Promise((res) => { msgId++; pending.set(msgId, res); ws.send(JSON.stringify({ id: msgId, method, params })); });
    const evalJs = async (expr) => {
      const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 700));
      return r.result && r.result.result ? r.result.result.value : undefined;
    };

    await sleep(4500);
    console.log("boot:", JSON.stringify(await evalJs(`({ok: document.body.dataset.vc, items: window.__vc.state.items.length})`)));

    // 注入 fetch 拦截：转写请求返回假任务，轮询返回固定文本
    await evalJs(`(() => {
      const orig = window.fetch;
      window.__tcCalls = [];
      window.fetch = async (url, opts) => {
        const u = String(url);
        if (u.includes("/api/transcribe")) {
          window.__tcCalls.push(JSON.parse(opts.body));
          return new Response(JSON.stringify({ task_id: "fake-tc" }), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        if (u.includes("/api/tasks/fake-tc")) {
          return new Response(JSON.stringify({ status: "done", progress: 1, result: { texts: ["テスト文本"] } }),
            { status: 200, headers: { "Content-Type": "application/json" } });
        }
        return orig(url, opts);
      };
    })()`);

    // 准备：选中素材 + 造一个片段 2~8s
    await evalJs(`(async () => {
      const vc = window.__vc, st = vc.state;
      await vc.selectItem(st.items[0]);
      const segs = st.segmentsByItem.get(st.currentItem.id) || [];
      st.segmentsByItem.set(st.currentItem.id, segs);
      segs.push(vc.newSegment(2, 8));
      vc.renderSegments();
    })()`);
    await sleep(1500);

    // ① 点击片段行 → activeSeg + 绑定选区 + 播放头跳转 + 行高亮
    const step1 = await evalJs(`(() => {
      const st = window.__vc.state;
      const tr = document.querySelector("#seg-tbody tr.seg-row");
      tr.click();
      return {
        activeSeg: st.activeSeg,
        regionBounds: st.selectionRegion ? [+st.selectionRegion.start.toFixed(1), +st.selectionRegion.end.toFixed(1)] : null,
        playhead: +st.ws.getCurrentTime().toFixed(1),
        rowActive: !!document.querySelector("#seg-tbody tr.seg-row.active"),
        selInfo: document.querySelector("#sel-info").textContent
      };
    })()`);
    console.log("① click row:", JSON.stringify(step1));
    await sleep(800);

    // ② 模拟拖拽结束：setOptions 改边界 + 发射 region-updated（vendor 拖拽结束时发出的同一事件）
    const step2 = await evalJs(`(() => {
      const st = window.__vc.state;
      st.selectionRegion.setOptions({ start: 2, end: 10 });
      st.regions.emit("region-updated", st.selectionRegion, {});
      const seg = st.segmentsByItem.get(st.currentItem.id)[0];
      const tr = document.querySelector("#seg-tbody tr.seg-row");
      return {
        regionBounds: st.selectionRegion ? [+st.selectionRegion.start.toFixed(1), +st.selectionRegion.end.toFixed(1)] : null,
        segBounds: [+seg.start.toFixed(1), +seg.end.toFixed(1)],
        rowTime: tr.children[2].textContent
      };
    })()`);
    console.log("② drag bounds:", JSON.stringify(step2));

    // ③ 防抖 1.2s 后自动转写 → 假任务 → 回填文本
    await sleep(2500);
    const step3 = await evalJs(`(() => {
      const st = window.__vc.state;
      const seg = st.segmentsByItem.get(st.currentItem.id)[0];
      const tr = document.querySelector("#seg-tbody tr.seg-row");
      return {
        tcCalls: window.__tcCalls,
        segText: seg.text,
        inputVal: tr.querySelector(".seg-text").value,
        tag: tr.querySelector(".tag").textContent
      };
    })()`);
    console.log("③ autofill:", JSON.stringify(step3));

    // ④ 断言汇总
    const ok1 = step1.activeSeg && step1.regionBounds && step1.regionBounds[0] === 2 && step1.regionBounds[1] === 8
      && step1.playhead === 2 && step1.rowActive;
    const ok2 = step2.regionBounds && step2.regionBounds[0] === 2 && step2.regionBounds[1] === 10
      && step2.segBounds[0] === 2 && step2.segBounds[1] === 10 && /0:10/.test(step2.rowTime);
    const ok3 = step3.tcCalls.length === 1 && step3.tcCalls[0].segments[0].start === 2 && step3.tcCalls[0].segments[0].end === 10
      && step3.segText === "テスト文本" && step3.inputVal === "テスト文本";
    console.log("RESULT:", JSON.stringify({ step1: !!ok1, step2: !!ok2, step3: !!ok3 }));
    if (!(ok1 && ok2 && ok3)) throw new Error("verification failed");
    console.log("ALL PASS");
  } finally {
    try { server.kill(); } catch (e) {}
    await sleep(300);
  }
  process.exit(0);
})().catch((e) => { console.error("FAIL:", e.message); process.exit(1); });
