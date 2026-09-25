// 端到端验证：声纹反馈 → 角色质心吸收 → 静默重匹配其他片段
// 临时后端（MFCC 降级路径）；seg1 与 seg2 取相同音频区间 → embedding 相同 → cos=1 必绑定
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const ROOT = path.join(__dirname, "..");
const PORT = Number(process.argv[2] || 8897);
const CDP_PORT = 9361;
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const WORKDIR = fs.mkdtempSync(path.join(os.tmpdir(), "vc-fb-"));
const PROF = process.env.TEMP + "\\vc-fb-" + Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const makeWav = (seconds, sr = 16000) => {
  const n = seconds * sr, dataLen = n * 2;
  const b = Buffer.alloc(44 + dataLen);
  b.write("RIFF", 0); b.writeUInt32LE(36 + dataLen, 4); b.write("WAVE", 8);
  b.write("fmt ", 12); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(sr, 24); b.writeUInt32LE(sr * 2, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34);
  b.write("data", 36); b.writeUInt32LE(dataLen, 40);
  // 440Hz 正弦（非静音，避免 RMS 门限拒掉声纹嵌入）
  for (let i = 0; i < n; i++) {
    b.writeInt16LE(Math.round(Math.sin(2 * Math.PI * 440 * i / sr) * 8000), 44 + i * 2);
  }
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
    fd.append("file", new Blob([makeWav(20)], { type: "audio/wav" }), "fb.wav");
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

    // 准备：先等导入触发的后台自动分析结束（其 save_pool 会覆盖池），再造角色/片段
    for (let i = 0; i < 120; i++) {
      const act = await (await fetch(`${BASE}/api/tasks/active`)).json();
      if (!act.length) { await sleep(2000); const act2 = await (await fetch(`${BASE}/api/tasks/active`)).json(); if (!act2.length) break; }
      await sleep(1000);
    }
    const prep = await evalJs(`(async () => {
      const vc = window.__vc, st = vc.state;
      await vc.selectItem(st.items[0]);
      const segs = st.segmentsByItem.get(st.currentItem.id) || [];
      st.segmentsByItem.set(st.currentItem.id, segs);
      const s1 = vc.newSegment(2, 5); s1.locked = true; s1.characterId = "c1";   // 样本段（人工已修正）
      const s2 = vc.newSegment(2, 5);                                            // 同音频、未分配 → 应被静默绑定
      const s3 = vc.newSegment(2, 5); s3.locked = true; s3.characterId = null;   // 锁定但未分配 → 永不自动动
      segs.push(s1, s2, s3);
      vc.renderSegments();
      return { itemId: st.currentItem.id, s1: s1.id, s2: s2.id, s3: s3.id };
    })()`);
    console.log("prep:", JSON.stringify(prep));

    // 角色直接经 API 落盘（绕过前端 poolDirty 语义）
    const pid = await evalJs(`window.__vc.state.currentProject.id`);
    const poolNow = await (await fetch(`${BASE}/api/projects/${pid}/characters`)).json();
    const chars = (poolNow.characters || []).filter(c => c.id !== "c1");
    chars.push({ id: "c1", name: "鹿目まどか", color: "#ff77bb", speakerLabels: [], created: Date.now() });
    await fetch(`${BASE}/api/projects/${pid}/characters`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ characters: chars }) });
    // 同步片段到磁盘（locked/characterId 要让 worker 读得到）
    await evalJs(`(async () => {
      const st = window.__vc.state;
      const segs = st.segmentsByItem.get(st.items[0].id) || [];
      await fetch("/api/items/${prep.itemId}/project", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ segments: segs, speaker_segments: [] }) });
    })()`);
    await sleep(500);
    let poolOk = false;
    for (let i = 0; i < 5 && !poolOk; i++) {
      const pool = await (await fetch(`${BASE}/api/projects/${pid}/characters`)).json();
      poolOk = (pool.characters || []).some(c => c.id === "c1");
      if (!poolOk) { await sleep(1000); }
    }
    console.log("pool has c1:", poolOk);
    if (!poolOk) throw new Error("c1 not persisted to pool");

    // 触发声纹反馈（与 UI 下拉/reassign 相同的 api 链路）
    const fb = await evalJs(`(async () => {
      const st = window.__vc.state;
      const j = await fetch("/api/speakers/feedback", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: st.currentProject.id,
          samples: [{ item_id: "${prep.itemId}", seg_id: "${prep.s1}",
                      character_id: "c1", start: 2, end: 5 }] }) });
      return await j.json();
    })()`);
    console.log("feedback task:", JSON.stringify(fb));
    if (!fb.task_id) throw new Error("feedback submit failed");

    // 轮询任务完成
    let result = null;
    for (let i = 0; i < 120; i++) {
      await sleep(1000);
      const t = await (await fetch(`${BASE}/api/tasks/${fb.task_id}`)).json();
      if (t.status === "done") { result = t.result; break; }
      if (t.status === "error") throw new Error("task error: " + (t.message || ""));
    }
    if (!result) throw new Error("task timeout");
    console.log("task result:", JSON.stringify({ ...result, characters: (result.characters || []).map(c => c.id) }));

    // 断言：从后端重读（worker 直接写盘），前端重载后核对
    const check = await evalJs(`(async () => {
      const st = window.__vc.state;
      await window.__vc.loadProject(st.items[0], true);
      const segs = st.segmentsByItem.get(st.items[0].id) || [];
      const byId = {};
      segs.forEach(s => byId[s.id] = s);
      return { s1: byId["${prep.s1}"] && byId["${prep.s1}"].characterId,
               s2: byId["${prep.s2}"] && byId["${prep.s2}"].characterId,
               s3: byId["${prep.s3}"] && byId["${prep.s3}"].characterId,
               pool: st.characters.map(c => c.id) };
    })()`);
    console.log("check:", JSON.stringify(check));
    const ok = check.s1 === "c1" && check.s2 === "c1" && (check.s3 === null || check.s3 === undefined);
    console.log(ok ? "ALL PASS: 未分配段被静默绑定，锁定未分配段未被触碰" : "FAIL");
    if (!ok) throw new Error("verification failed");
  } finally {
    try { server.kill(); } catch (e) {}
    await sleep(300);
  }
  process.exit(0);
})().catch((e) => { console.error("FAIL:", e.message); process.exit(1); });
