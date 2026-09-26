const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
// 诊断：角色池重命名 → 片段列表是否同步显示新名字（含刷新后持久化验证）
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9391;
const PROF = process.env.TEMP + "\\vc-ren-" + Date.now();
const ROOT = path.join(__dirname, "..");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const SRV_PORT = 8903;
const BASE = `http://127.0.0.1:${SRV_PORT}`;
const WORKDIR = fs.mkdtempSync(path.join(os.tmpdir(), "vc-ren-"));
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
  if (!ready) throw new Error("server not ready");
  const seedFd = new FormData();
  seedFd.append("file", new Blob([makeWav(20)], { type: "audio/wav" }), "seed.wav");
  const seedImp = await (await fetch(`${BASE}/api/import`, { method: "POST", body: seedFd })).json();
  for (let i = 0; i < 80; i++) {
    await sleep(500);
    const tsk = await (await fetch(`${BASE}/api/tasks/${seedImp.task_id}`)).json();
    if (tsk && (tsk.status === "done" || tsk.status === "failed")) break;
  }

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
    const send = (method, params = {}) => new Promise((res) => { const id = ++msgId; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
    await sleep(2500);

    // 阶段1：建角色 + 指派片段 + 通过真实 chip 点击重命名
    const expr1 = `(async () => {
      const vc = window.__vc;
      if (!vc) return { ok: false, reason: "no __vc" };
      await vc.selectItem(vc.state.items[0]);
      await new Promise(r => setTimeout(r, 3000));
      // 建角色（模拟池中已有角色）
      const ch = { id: "char_diag1", name: "旧名字", color: "#4a90d9", speakerLabels: [], created: Date.now() };
      vc.state.characters.push(ch);
      // 建一个片段并指派给该角色
      const seg = vc.newSegment(1.0, 3.0, "测试片段");
      seg.characterId = ch.id;
      const itemId = vc.state.currentItem.id;
      let arr = vc.state.segmentsByItem.get(itemId);
      if (!arr) { arr = []; vc.state.segmentsByItem.set(itemId, arr); }
      arr.push(seg);
      vc.renderSegments();
      const pickOpt = () => {
        const sel = document.querySelector(".seg-speaker");
        return sel ? sel.options[sel.selectedIndex] : null;
      };
      const before = { optText: pickOpt() ? pickOpt().textContent : null, poolName: ch.name };
      // 打开角色池，劫持 prompt，真实点击重命名 chip
      vc.openPool();
      window.prompt = () => "新名字ABC";
      const chip = document.querySelector('.pool-rename[data-char="char_diag1"]');
      if (!chip) return { ok: false, reason: "no rename chip", before };
      chip.click();
      await new Promise(r => setTimeout(r, 500));
      const after = {
        stateName: vc.state.characters.find(c => c.id === "char_diag1").name,
        poolCardName: (document.querySelector('.pool-card[data-pool-char="char_diag1"] .pool-name') || {}).textContent || null,
        segOptText: pickOpt() ? pickOpt().textContent : null,
        segOptCount: document.querySelectorAll(".seg-speaker").length,
      };
      return { ok: true, before, after };
    })()`;
    const r1 = await send("Runtime.evaluate", { expression: expr1, awaitPromise: true, returnByValue: true });
    console.log("STAGE1:", JSON.stringify(r1.result.result.value, null, 2));
    if (r1.result.exceptionDetails) console.log("EXC1:", JSON.stringify(r1.result.exceptionDetails).slice(0, 800));

    // 阶段2：等防抖落盘 → 刷新页面 → 验证角色名与片段列表
    await sleep(2500);
    await send("Page.enable");
    await send("Page.reload");
    await sleep(4500);
    const expr2 = `(async () => {
      const vc = window.__vc;
      if (!vc) return { ok: false, reason: "no __vc after reload" };
      await vc.selectItem(vc.state.items[0]);
      await new Promise(r => setTimeout(r, 3000));
      const ch = vc.state.characters.find(c => c.id === "char_diag1");
      const sel = document.querySelector(".seg-speaker");
      return {
        ok: true,
        chExists: !!ch,
        chName: ch ? ch.name : null,
        segOptText: sel ? sel.options[sel.selectedIndex].textContent : null,
        segCharId: sel ? sel.value : null,
      };
    })()`;
    const r2 = await send("Runtime.evaluate", { expression: expr2, awaitPromise: true, returnByValue: true });
    console.log("STAGE2:", JSON.stringify(r2.result.result.value, null, 2));
    if (r2.result.exceptionDetails) console.log("EXC2:", JSON.stringify(r2.result.exceptionDetails).slice(0, 800));
  } finally {
    try { child.kill(); } catch (e) {}
    try { server.kill(); } catch (e) {}
  }
})().catch(e => { console.error("FATAL:", e.message); process.exit(1); });
