const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
// 多分辨率排版体检：同一页面在 1100/1280/1440/1600 宽度下的溢出与关键控件可用性 + JS 异常
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9395;
const PROF = process.env.TEMP + "\\vc-size-" + Date.now();
const ROOT = path.join(__dirname, "..");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const SRV_PORT = 8907;
const BASE = `http://127.0.0.1:${SRV_PORT}`;
const WORKDIR = fs.mkdtempSync(path.join(os.tmpdir(), "vc-size-"));
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
  for (let i = 0; i < 90; i++) {
    await sleep(500);
    const t = await (await fetch(`${BASE}/api/tasks/${imp.task_id}`)).json();
    if (t && (t.status === "done" || t.status === "failed")) break;
  }

  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, `${BASE}/`], { stdio: "ignore" });
  try {
    let target = null;
    for (let i = 0; i < 30 && !target; i++) {
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
    await send("Runtime.enable");
    await sleep(2500);

    // 造数据（在最大的窗口下先做，之后只改视口尺寸）
    await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 900, deviceScaleFactor: 1, mobile: false });
    const seed = `(async () => {
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
        '食べるんだろ？', 'そっちの方がいいよ', '長い名前の話者が入っている行の見え方チェック用の長いテキスト', '短い',
        'もう少し長めのセリフを入れて折り返しと列幅のバランスを見る行', 'きみはどう思う？'];
      const segs = texts.map((t, k) => ({
        id: 's' + k, start: 320.8 + k * 8.6, end: 320.8 + k * 8.6 + 7.9, text: t, language: 'JP',
        speakerLabel: 'S' + k, characterId: k % 3 === 0 ? 'cA' : (k % 3 === 1 ? 'cB' : null),
        locked: k === 5, quality: k === 2 ? 31 : 88,
      }));
      vc.state.segmentsByItem.set(itemId, segs);
      vc.renderSegments();
      const pad2 = (n) => String(n).padStart(2, '0');
      const ts = (t) => { const m = Math.floor(t / 60); const s = (t - m * 60).toFixed(1);
        return '00:' + pad2(m) + ':' + (s.length < 4 ? '0' + s : s).replace('.', ','); };
      let srt = '';
      texts.forEach((t, k) => { const a = 320.8 + k * 8.6, b = a + 7.9;
        srt += (k + 1) + '\\n' + ts(a) + ' --> ' + ts(b) + '\\n' + t + '\\n\\n'; });
      const f = new FormData();
      f.append('file', new Blob([srt], { type: 'text/plain' }), 'demo.srt');
      const up = await (await fetch('/api/subtitles/' + itemId, { method: 'POST', body: f })).json();
      await vc.selectItem(vc.state.items[0]);
      await new Promise(r => setTimeout(r, 900));
      vc.renderSegments();
      return { subCount: (up && up.count) || 0 };
    })()`;
    const sd = await send("Runtime.evaluate", { expression: seed, awaitPromise: true, returnByValue: true });
    console.log("SEED:", JSON.stringify(sd.result.result.value));

    const probe = `(() => {
      const vc = window.__vc;
      const box = (sel) => { const el = document.querySelector(sel); if (!el) return null;
        const r = el.getBoundingClientRect(); return { x: Math.round(r.x), w: Math.round(r.width), h: Math.round(r.height), right: Math.round(r.right) }; };
      const segSc = document.getElementById('seg-scroll');
      const subSc = document.getElementById('sub-scroll');
      const segTbl = document.getElementById('seg-table');
      const subTbl = document.getElementById('sub-table');
      const ths = Array.from(segTbl.querySelectorAll('thead th')).map(th => Math.round(th.getBoundingClientRect().width));
      const subThs = Array.from(subTbl.querySelectorAll('thead th')).map(th => Math.round(th.getBoundingClientRect().width));
      const firstSegRow = segTbl.querySelector('tbody tr.seg-row');
      const textInput = segTbl.querySelector('.seg-text');
      const cells = firstSegRow ? Array.from(firstSegRow.children).map(td => Math.round(td.getBoundingClientRect().width)) : [];
      return {
        vw: window.innerWidth, vh: window.innerHeight,
        segOverflowX: segSc ? segSc.scrollWidth - segSc.clientWidth : null,
        subOverflowX: subSc ? subSc.scrollWidth - subSc.clientWidth : null,
        segPanel: box('#segments-panel'), segThs: ths, segCells: cells,
        segMismatch: cells.length === ths.length ? ths.map((t, i) => Math.abs(t - cells[i]) > 2 ? i : -1).filter((i) => i >= 0) : 'len-diff',
        textInputW: textInput ? Math.round(textInput.getBoundingClientRect().width) : null,
        subPanel: box('#subtitle-panel'), subThs,
        subRows: document.querySelectorAll('#sub-tbody tr.sub-row').length,
        segRows: document.querySelectorAll('#seg-tbody tr.seg-row').length,
        gripWrap: Array.from(document.querySelectorAll('#segments-panel .panel-grip > *')).map(el => Math.round(el.getBoundingClientRect().height)),
        subGripWrap: Array.from(document.querySelectorAll('#subtitle-panel .panel-grip > *')).map(el => Math.round(el.getBoundingClientRect().height)),
        docOverflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      };
    })()`;

    // 覆盖真实常见视口：1920 全屏 / 1536(1920@125%) / 1280(1920@150%) / 1100 / 1024 / 960 / 860
    const sizes = [[1600, 900], [1440, 810], [1280, 720], [1100, 640], [1024, 600], [960, 580], [860, 540]];
    for (const [w, h] of sizes) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
      await sleep(700);
      // 等后台任务排空（遮罩会挡住整页，截图无意义）
      for (let i = 0; i < 90; i++) {
        const busy = await send("Runtime.evaluate", { expression: "window.__vc.state.activeTasks.size", returnByValue: true });
        if ((busy.result.result.value || 0) === 0) break;
        await sleep(1000);
      }
      await send("Runtime.evaluate", { expression: "window.__vc.updateStatusbar()", returnByValue: true });
      await sleep(500);
      const r = await send("Runtime.evaluate", { expression: probe, returnByValue: true });
      console.log("SIZE " + w + "x" + h + ":", JSON.stringify(r.result.result.value));
      const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      fs.writeFileSync(path.join(ROOT, "workdir", `shot_${w}.png`), Buffer.from(shot.result.data, "base64"));
    }

    // ── 窄槽位：面板可拖拽换位，把「片段列表」拖进左/右窄列后是否仍可用 ──
    // 左列下限 140px、右列下限 170px（见 state.js clampN），任何固定宽表都会在那里横滚到不可用。
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 860, deviceScaleFactor: 1, mobile: false });
    for (const [slot, w] of [["media", 220], ["sub", 240]]) {
      const set = `(() => { const L = window.__vc.workspace.layout;
        L.area.seg = '${slot}'; window.__vc.workspace.applyLayout(); return L.area.seg; })()`;
      const sv = await send("Runtime.evaluate", { expression: set, returnByValue: true });
      await sleep(800);
      const r = await send("Runtime.evaluate", { expression: probe, returnByValue: true });
      console.log("SLOT seg->" + sv.result.result.value + " (col " + w + "):", JSON.stringify(r.result.result.value));
      const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      fs.writeFileSync(path.join(ROOT, "workdir", `shot_slot_${slot}.png`), Buffer.from(shot.result.data, "base64"));
    }
    await send("Runtime.evaluate", { expression: "(() => { const L = window.__vc.workspace.layout; L.area.seg='seg'; window.__vc.workspace.applyLayout(); })()", returnByValue: true });

    console.log("JS-ERRORS:", JSON.stringify(errs.slice(0, 5)));
  } finally {
    try { child.kill(); } catch (e) {}
    try { server.kill(); } catch (e) {}
  }
})().catch(e => { console.error("FATAL:", e.message); process.exit(1); });
