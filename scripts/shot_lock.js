// 锁定开关目检：造数据 → 锁定第 1/3/5/7 段 → 裁剪片段面板截图
// 用法：node scripts/shot_lock.js
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9397;
const PROF = process.env.TEMP + "\\vc-lock-" + Date.now();
const ROOT = path.join(__dirname, "..");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");
const SRV_PORT = 8909;
const BASE = `http://127.0.0.1:${SRV_PORT}`;
const WORKDIR = fs.mkdtempSync(path.join(os.tmpdir(), "vc-lock-"));
const OUT = path.join(ROOT, "workdir");
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
  if (!ready) { server.kill(); throw new Error("server not ready"); }
  const fd = new FormData();
  fd.append("file", new Blob([makeWav(200)], { type: "audio/wav" }), "lock-demo.mp4");
  const imp = await (await fetch(`${BASE}/api/import`, { method: "POST", body: fd })).json();
  let st = "timeout";
  for (let i = 0; i < 90; i++) {
    await sleep(500);
    const t = await (await fetch(`${BASE}/api/tasks/${imp.task_id}`)).json();
    if (t && (t.status === "done" || t.status === "failed")) { st = t.status; break; }
  }
  if (st !== "done") { console.error("FATAL: 导入未完成 " + st); server.kill(); process.exit(1); }

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
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

    const r = await send("Runtime.evaluate", { expression: `(async () => { try {
      const vc = window.__vc;
      if (!vc || !vc.state.items.length) return { err: 'no __vc/items' };
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
        quality: k === 2 ? 31 : 88,
      }));
      vc.state.segmentsByItem.set(itemId, segs);
      vc.renderSegments();
      // 用真实入口（点开关）锁 1/3/5/7 段，顺便验证入口本身。
      // 注意：toggleLock 会 renderSegments() 重建整表 → 必须每次重新查 chips，
      // 否则拿的是已脱离文档的旧节点（点了没反应）。
      for (const i of [0, 2, 4, 6]) {
        const c = document.querySelectorAll('#seg-table .seg-lock')[i];
        if (c) c.click();
        await new Promise(r => setTimeout(r, 150));
      }
      await new Promise(r => setTimeout(r, 300));
      const live = vc.state.segmentsByItem.get(itemId);
      // 把第 1 行设为「聚焦编辑」态，演示 locked 与 active 的视觉叠加
      vc.state.activeSeg = { itemId, segId: live[0].id };
      vc.renderSegments();
      await new Promise(r => setTimeout(r, 300));
      const panel = document.getElementById('segments-panel');
      const pr = panel.getBoundingClientRect();
      // 图标探针（替代此前的 emoji 字形探针）：.seg-lock 里的 <i class="ico"> 是 CSS mask 图标。
      // 必须验证 mask-image 解析到**真实的 lock/unlock SVG**，而不是落到 .ico 的「空遮罩兜底」
      // —— 落到兜底说明类名没定义，页面上只会渲染成 1em 实心方块（肉眼极易漏掉）。
      const probeIco = (root, name) => {
        const i = root ? root.querySelector('i.ico') : null;
        if (!i) return { name, hasIcon: false };
        const cs = getComputedStyle(i);
        const mask = cs.webkitMaskImage || cs.maskImage || 'none';
        const r = i.getBoundingClientRect();
        return { name, hasIcon: true, cls: i.className,
                 fallback: mask.indexOf('data:image/svg') >= 0,   // 兜底空遮罩 → 没画出来
                 // 坑：这段代码整体走「模板字符串 → Runtime.evaluate」，模板会先吃掉
                 // 反斜杠转义 —— 写成斜杠转义的正则（形如 斜杠 a 反斜杠 斜杠 b 斜杠）
                 // 到浏览器就退化成提前闭合，报 SyntaxError: Unexpected token '.'。
                 // 所以下面一律用 [.] 代替点转义，整段不含反斜杠。
                 svg: (mask.match(/([A-Za-z0-9_.-]+[.]svg)/) || ['', ''])[1],
                 size: [Math.round(r.width), Math.round(r.height)] };
      };
      const rows = [...document.querySelectorAll('#seg-tbody tr.seg-row')];
      const rowLocked = rows.find(tr => tr.classList.contains('locked'));
      const rowPlain = rows.find(tr => !tr.classList.contains('locked'));
      const q = (tr, sel) => (tr ? tr.querySelector(sel) : null);
      const icons = {
        lock: probeIco(q(rowLocked, '.seg-lock'), 'lock'),
        unlock: probeIco(q(rowPlain, '.seg-lock'), 'unlock'),
        aud: probeIco(q(rows[0], '.seg-aud'), 'aud'),
        jump: probeIco(q(rows[0], '.seg-jump'), 'jump'),
        del: probeIco(q(rows[0], '.seg-del'), 'del'),
      };
      return {
        lockedFlags: live.map(s => !!s.locked),
        segRows: live.length,
        icons,
        clip: { x: Math.round(pr.x), y: Math.round(pr.y), width: Math.round(pr.width), height: Math.round(pr.height) },
      };
    } catch (e) { return { err: String((e && e.message) || e) }; } })()`, awaitPromise: true, returnByValue: true });

    const v = r.result.result.value || {};
    if (r.result.exceptionDetails) {
      console.log("EVAL-EXC:", JSON.stringify(r.result.exceptionDetails).slice(0, 700));
    }
    if (v.err) { console.error("FATAL: " + v.err); return; }
    console.log("SEED:", JSON.stringify({ segRows: v.segRows, lockedFlags: v.lockedFlags }));
    const ic = v.icons || {};
    const bad = Object.entries(ic).filter(([, x]) => !x.hasIcon || x.fallback);
    const distinct = !!(ic.lock && ic.unlock && ic.lock.svg && ic.lock.svg !== ic.unlock.svg);
    console.log("ICONS:", JSON.stringify(ic));
    console.log("ICON-CHECK:", bad.length ? "✗ 未解析到真实 SVG: " + bad.map(([k]) => k).join(",")
      : "✓ 5 个图标全部解析到真实 SVG", "| lock ≠ unlock:", distinct);
    await sleep(400);
    const cap = await send("Page.captureScreenshot", { format: "png", clip: { ...v.clip, scale: 2 } });
    const p = path.join(OUT, "shot_lock_panel.png");
    fs.writeFileSync(p, Buffer.from(cap.result.data, "base64"));
    console.log("SHOT:", p, "clip=" + JSON.stringify(v.clip));
    // 状态列特写（放大 4x，看清锁图标与琥珀实心/灰虚线两种态）
    const zoom = await send("Runtime.evaluate", { expression: `(() => {
      const c = document.querySelectorAll('#seg-table thead th')[4];
      const p = document.getElementById('segments-panel');
      const cr = c.getBoundingClientRect(), pr = p.getBoundingClientRect();
      return { x: Math.round(cr.x) - 60, y: Math.round(pr.y), width: 300, height: Math.round(Math.min(pr.height, 300)) };
    })()`, returnByValue: true });
    const cap2 = await send("Page.captureScreenshot", { format: "png", clip: { ...zoom.result.result.value, scale: 4 } });
    const p2 = path.join(OUT, "shot_lock_zoom.png");
    fs.writeFileSync(p2, Buffer.from(cap2.result.data, "base64"));
    console.log("SHOT:", p2, "clip=" + JSON.stringify(zoom.result.result.value));
    console.log("JS-ERRORS:", JSON.stringify(errs));
  } finally {
    child.kill();
    server.kill();
    await sleep(600);
    try { fs.rmSync(PROF, { recursive: true, force: true }); } catch (e) {}
    try { fs.rmSync(WORKDIR, { recursive: true, force: true }); } catch (e) { console.log("[shot_lock] 临时 workdir 未清理:", WORKDIR); }
  }
})();
