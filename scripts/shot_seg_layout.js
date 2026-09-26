const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
// 片段列表排版目检：造 8 段 + 2 角色，截图 #segments-panel 区域
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9393;
const PROF = process.env.TEMP + "\\vc-shot-" + Date.now();
const ROOT = path.join(__dirname, "..");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const SRV_PORT = 8905;
const BASE = `http://127.0.0.1:${SRV_PORT}`;
const WORKDIR = fs.mkdtempSync(path.join(os.tmpdir(), "vc-shot-"));
const OUT = path.join(ROOT, "workdir", "shot_seg_layout.png");
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
  const fd = new FormData();
  fd.append("file", new Blob([makeWav(200)], { type: "audio/wav" }), "1-初乙在咖啡厅看到了另一个世界.mp4");
  const imp = await (await fetch(`${BASE}/api/import`, { method: "POST", body: fd })).json();
  for (let i = 0; i < 80; i++) {
    await sleep(500);
    const t = await (await fetch(`${BASE}/api/tasks/${imp.task_id}`)).json();
    if (t && (t.status === "done" || t.status === "failed")) break;
  }

  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run", "--window-size=1400,420",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, `${BASE}/`], { stdio: "ignore" });
  try {
    let target = null;
    for (let i = 0; i < 30 && !target; i++) {
      try { const res = await fetch(`http://127.0.0.1:${PORT}/json`); const l = await res.json(); target = l.find((t) => t.type === "page") || null; } catch (e) {}
      if (!target) await sleep(500);
    }
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let msgId = 0; const pending = new Map();
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
    const send = (method, params = {}) => new Promise((res) => { const id = ++msgId; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
    await sleep(2500);

    const expr = `(async () => {
      const vc = window.__vc;
      if (!vc) return { err: 'no __vc' };
      await vc.selectItem(vc.state.items[0]);
      await new Promise(r => setTimeout(r, 2500));
      const itemId = vc.state.currentItem.id;
      vc.state.characters = [
        { id: 'cA', name: '初乙', color: '#e5484d', speakerLabels: [], emb_count: 12 },
        { id: 'cB', name: '咖啡厅老板', color: '#46a758', speakerLabels: [], emb_count: 3 },
      ];
      const texts = ['播磨なにこの橋を女那は渡すはずなのだろう？', '先生はまた飲んでるみたい',
        'ホームルームでどうするきらしいよ', '今週までに月曜日からに調査報告書だよな',
        '食べるんだろ？', 'そっちの方がいいよ', '長い名前の話者が入っている行の見え方チェック用の長いテキスト', '短い'];
      const segs = texts.map((t, k) => ({
        id: 's' + k, start: 320.8 + k * 8.6, end: 320.8 + k * 8.6 + 7.9, text: t, language: 'JP',
        speakerLabel: 'S' + k, characterId: k % 3 === 0 ? 'cA' : (k % 3 === 1 ? 'cB' : null),
        locked: k === 5, quality: k === 2 ? 31 : 88,
      }));
      vc.state.segmentsByItem.set(itemId, segs);
      vc.state.speakerSegs = [];
      vc.state.speakerSegsByItem.set(itemId, texts.map((_, k) => ({ label: k % 2 ? 'S1' : 'S0', start: 320.8 + k * 8.6, end: 320.8 + k * 8.6 + 7.9 })));
      vc.renderSegments();
      // 走真实字幕接口喂一批字幕（渲染路径与用户「加载字幕」一致）
      const pad2 = (n) => String(n).padStart(2, '0');
      const ts = (t) => { const m = Math.floor(t / 60); const s = (t - m * 60).toFixed(1);
        return '00:' + pad2(m) + ':' + (s.length < 4 ? '0' + s : s).replace('.', ','); };
      let srt = '';
      texts.forEach((t, k) => { const a = 320.8 + k * 8.6, b = a + 7.9;
        srt += (k + 1) + '\\n' + ts(a) + ' --> ' + ts(b) + '\\n' + t + '\\n\\n'; });
      const fd = new FormData();
      fd.append('file', new Blob([srt], { type: 'text/plain' }), 'demo.srt');
      const up = await (await fetch('/api/subtitles/' + itemId, { method: 'POST', body: fd })).json();
      await vc.selectItem(vc.state.items[0]);   // 服务端已有字幕 → 重渲染两个字幕视图
      await new Promise(r => setTimeout(r, 900));
      vc.renderSegments();
      await new Promise(r => setTimeout(r, 400));
      const subCount = (up && up.count) || 0;
      const panel = document.getElementById('segments-panel');
      const r = panel.getBoundingClientRect();
      const tbl = document.getElementById('seg-table');
      const lastTh = tbl.querySelector('thead th:last-child').getBoundingClientRect();
      const lastTd = tbl.querySelector('tbody td.row-actions');
      const subScroll = document.getElementById('sub-scroll');
      return { overflowX: document.getElementById('seg-scroll').scrollWidth - document.getElementById('seg-scroll').clientWidth,
               subOverflowX: subScroll.scrollWidth - subScroll.clientWidth, subCount,
               subRows: document.querySelectorAll('#sub-tbody tr.sub-row').length,
               subGripWrap: Array.from(document.querySelectorAll('#subtitle-panel .panel-grip > *'))
                 .map(el => Math.round(el.getBoundingClientRect().height)),
               bars: ['transport', 'pagebar', 'statusbar'].map(id => {
                 const el = document.getElementById(id);
                 return id + ':' + (el ? Math.round(el.getBoundingClientRect().height) : 'na');
               }).join(' '),
               panelW: Math.round(r.width), tableRight: Math.round(lastTh.right), panelRight: Math.round(r.right),
               actionsW: lastTd ? Math.round(lastTd.getBoundingClientRect().width) : 0,
               headerH: Math.round(tbl.querySelector('thead').getBoundingClientRect().height),
               rowH: Math.round(tbl.querySelector('tbody tr').getBoundingClientRect().height), rect: {x: r.x, y: r.y, w: r.width, h: r.height} };
    })()`;
    const r1 = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    const info = r1.result.result.value;
    console.log("LAYOUT:", JSON.stringify(info));
    const clip = info.rect ? { x: info.rect.x, y: info.rect.y, width: info.rect.w, height: Math.min(info.rect.h || 380, 380), scale: 1 } : undefined;
    const shot = await send("Page.captureScreenshot", { format: "png", clip, captureBeyondViewport: true });
    fs.writeFileSync(OUT, Buffer.from(shot.result.data, "base64"));
    console.log("SHOT:", OUT);
    // 整页（1600x900）另存，便于整体排版目检
    await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 900, deviceScaleFactor: 1, mobile: false });
    // 等后台任务清空（遮罩会挡住整页），再截图
    for (let i = 0; i < 60; i++) {
      const busy = await send("Runtime.evaluate", { expression: "window.__vc.state.activeTasks.size", returnByValue: true });
      if ((busy.result.result.value || 0) === 0) break;
      await sleep(1000);
    }
    await sleep(800);
    const full = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    const OUT2 = path.join(ROOT, "workdir", "shot_page_full.png");
    fs.writeFileSync(OUT2, Buffer.from(full.result.data, "base64"));
    console.log("SHOT2:", OUT2);
    // 2x 放大：顶栏 / 传输栏
    await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 900, deviceScaleFactor: 2, mobile: false });
    await sleep(500);
    const top = await send("Page.captureScreenshot", { format: "png", clip: { x: 0, y: 0, width: 1600, height: 110, scale: 2 } });
    fs.writeFileSync(path.join(ROOT, "workdir", "shot_top.png"), Buffer.from(top.result.data, "base64"));
    const tr = await send("Page.captureScreenshot", { format: "png", clip: { x: 900, y: 60, width: 700, height: 460, scale: 2 } });
    fs.writeFileSync(path.join(ROOT, "workdir", "shot_subpanel.png"), Buffer.from(tr.result.data, "base64"));
    console.log("SHOT3/4 done");
  } finally {
    try { child.kill(); } catch (e) {}
    try { server.kill(); } catch (e) {}
  }
})().catch(e => { console.error("FATAL:", e.message); process.exit(1); });
