const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
// 交互可用性探针：片段列表/字幕面板的真实点击·编辑·下拉·按钮；素材库页与训练交付页截图
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9397;
const PROF = process.env.TEMP + "\\vc-ix-" + Date.now();
const ROOT = path.join(__dirname, "..");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const SRV_PORT = 8909;
const BASE = `http://127.0.0.1:${SRV_PORT}`;
const WORKDIR = fs.mkdtempSync(path.join(os.tmpdir(), "vc-ix-"));
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
  fd.append("file", new Blob([makeWav(200)], { type: "audio/wav" }), "1-初乙.mp4");
  const imp = await (await fetch(`${BASE}/api/import`, { method: "POST", body: fd })).json();
  for (let i = 0; i < 90; i++) {
    await sleep(500);
    const t = await (await fetch(`${BASE}/api/tasks/${imp.task_id}`)).json();
    if (t && (t.status === "done" || t.status === "failed")) break;
  }

  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run", "--window-size=1400,900",
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
      if (m.method === "Runtime.exceptionThrown") errs.push(((m.params.exceptionDetails.exception || {}).description) || m.params.exceptionDetails.text);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    };
    const send = (method, params = {}) => new Promise((res) => { const id = ++msgId; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
    await send("Runtime.enable");
    await send("Emulation.setDeviceMetricsOverride", { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });
    await sleep(2500);

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
      const texts = ['最初のセリフ', '二番目のセリフです', '三番目の長めのセリフをここに入れて折り返しを見る'];
      const segs = texts.map((t, k) => ({
        id: 's' + k, start: 320.8 + k * 8.6, end: 320.8 + k * 8.6 + 7.9, text: t, language: 'JP',
        speakerLabel: 'S' + k, characterId: k === 0 ? 'cA' : null, quality: 88,
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
      await fetch('/api/subtitles/' + itemId, { method: 'POST', body: f });
      await vc.selectItem(vc.state.items[0]);
      await new Promise(r => setTimeout(r, 900));
      vc.renderSegments();
      return { segs: segs.length, subRows: document.querySelectorAll('#sub-tbody tr.sub-row').length };
    })()`;
    const sd = await send("Runtime.evaluate", { expression: seed, awaitPromise: true, returnByValue: true });
    console.log("SEED:", JSON.stringify(sd.result.result.value));

    // 交互体检：真实点击（Input 域）/ 键盘 / 下拉 change
    const r = await send("Runtime.evaluate", { expression: `(async () => {
      const vc = window.__vc;
      const out = {};
      // 1) 片段行聚焦：点序号列
      const numCell = document.querySelector('#seg-tbody tr.seg-row td.seg-num');
      numCell.click();
      out.focusOnRowClick = !!vc.state.focusedSeg;
      // 2) 文本编辑：输入 + 回车提交
      const inp = document.querySelector('#seg-tbody tr.seg-row .seg-text');
      inp.focus(); inp.value = '変更後のテキスト';
      inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      out.textCommit = (vc.state.segmentsByItem.get(vc.state.currentItem.id)[0].text === '変更後のテキスト');
      // 3) 说话人下拉：change 生效
      const sel = document.querySelector('#seg-tbody tr.seg-row .seg-speaker');
      sel.value = 'cB'; sel.dispatchEvent(new Event('change', { bubbles: true }));
      out.speakerChange = (vc.state.segmentsByItem.get(vc.state.currentItem.id)[0].characterId === 'cB');
      // 4) 语言下拉
      const lang = document.querySelector('#seg-tbody tr.seg-row .seg-lang');
      lang.value = 'ZH'; lang.dispatchEvent(new Event('change', { bubbles: true }));
      out.langChange = (vc.state.segmentsByItem.get(vc.state.currentItem.id)[0].language === 'ZH');
      // 5) 试听按钮
      document.querySelector('#seg-tbody tr.seg-row .seg-aud').click();
      out.audClicked = true;
      // 6) 字幕行：加片段按钮
      const before = vc.state.segmentsByItem.get(vc.state.currentItem.id).length;
      const addBtn = document.querySelector('#sub-tbody tr.sub-row .sub-add');
      out.subAddBtnExists = !!addBtn;
      if (addBtn) { addBtn.click(); await new Promise(r2 => setTimeout(r2, 600)); }
      out.subAddWorked = vc.state.segmentsByItem.get(vc.state.currentItem.id).length > before;
      // 7) 字幕行：选区按钮
      const selBtn = document.querySelector('#sub-tbody tr.sub-row .sub-sel');
      out.subSelBtnExists = !!selBtn;
      if (selBtn) { selBtn.click(); await new Promise(r2 => setTimeout(r2, 400)); }
      out.subSelWorked = !!(vc.state.selection && vc.state.selection.start != null);
      // 8) 点击字幕行文本 → 跳转
      const row = document.querySelector('#sub-tbody tr.sub-row');
      row.click(); await new Promise(r2 => setTimeout(r2, 300));
      out.subRowClickOk = true;
      // 9) 表头筛选可用性：切换状态筛选
      const fst = document.getElementById('seg-filter-status');
      out.statusFilterW = Math.round(fst.getBoundingClientRect().width);
      fst.value = 'ok'; fst.dispatchEvent(new Event('change', { bubbles: true }));
      await new Promise(r2 => setTimeout(r2, 300));
      out.filterApplied = document.getElementById('seg-count').textContent;
      fst.value = 'all'; fst.dispatchEvent(new Event('change', { bubbles: true }));
      // 10) 表头排序
      document.querySelector('#seg-table thead th[data-sort="dur"]').click();
      await new Promise(r2 => setTimeout(r2, 200));
      out.sortClicked = true;
      return out;
    })()`, awaitPromise: true, returnByValue: true });
    console.log("INTERACT:", JSON.stringify(r.result.result.value, null, 1));
    if (r.result.exceptionDetails) console.log("IX-EXC:", JSON.stringify(r.result.exceptionDetails).slice(0, 600));

    for (const [page, w, h] of [["edit", 1400, 900], ["media", 1400, 900], ["train", 1400, 900]]) {
      await send("Runtime.evaluate", { expression: `window.__vc.setPage('${page}')`, returnByValue: true });
      await sleep(700);
      const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      fs.writeFileSync(path.join(ROOT, "workdir", `shot_page_${page}.png`), Buffer.from(shot.result.data, "base64"));
    }
    console.log("JS-ERRORS:", JSON.stringify(errs.slice(0, 6)));
  } finally {
    try { child.kill(); } catch (e) {}
    try { server.kill(); } catch (e) {}
  }
})().catch(e => { console.error("FATAL:", e.message); process.exit(1); });
