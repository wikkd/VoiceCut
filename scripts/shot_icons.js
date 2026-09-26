// 图标系统体检：连真实服务（8765），在「剪辑 / 角色池 / 训练交付」三个页面上
// ① 全量审计每个 .ico 元素是否解析到真实 SVG（落到 .ico 的空遮罩兜底 = 类名漏定义 → 渲染成实心方块）
// ② 逐页截整窗图供目检
//
// 用法：先启动 voicecut.py（默认 8765），再 node scripts/shot_icons.js
//
// 坑（踩过，务必注意）：下面 Runtime.evaluate 的代码整体是模板字符串，模板会先吃掉
// 反斜杠转义。所以表达式里**不要写带反斜杠的正则**（形如 斜杠 a 反斜杠 斜杠 b 斜杠
// 会被提前闭合 → SyntaxError: Unexpected token '.'），也不要在注释里写反引号。
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9398;
const PROF = process.env.TEMP + "\\vc-icons-" + Date.now();
const ROOT = path.join(__dirname, "..");
const OUT = path.join(ROOT, "workdir");
const BASE = process.env.VC_BASE || "http://127.0.0.1:8765";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, "--window-size=1440,960",
    BASE + "/"], { stdio: "ignore" });
  let errs = [];
  try {
    let target = null;
    for (let i = 0; i < 40 && !target; i++) {
      try { const res = await fetch(`http://127.0.0.1:${PORT}/json`); const l = await res.json(); target = l.find(t => t.type === "page") || null; } catch (e) {}
      if (!target) await sleep(500);
    }
    if (!target) { console.error("FATAL: 无 CDP page target（Chrome 没起来？）"); return; }
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let msgId = 0; const pending = new Map();
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.method === "Runtime.exceptionThrown") errs.push((m.params.exceptionDetails.exception || {}).description || m.params.exceptionDetails.text);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    };
    const send = (method, params = {}) => new Promise((res) => { const id = ++msgId; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
    const evaluate = async (expr) => {
      const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.result && r.result.exceptionDetails) console.log("EVAL-EXC:", JSON.stringify(r.result.exceptionDetails).slice(0, 500));
      return (r.result && r.result.result && r.result.result.value);
    };
    const shot = async (name) => {
      const cap = await send("Page.captureScreenshot", { format: "png" });
      if (!cap.result || !cap.result.data) { console.log("SHOT-FAIL:", name); return; }
      const p = path.join(OUT, name);
      fs.writeFileSync(p, Buffer.from(cap.result.data, "base64"));
      console.log("SHOT:", p);
    };
    // 轮询等待页面进入某个状态。**必须这么做**：固定 sleep 会在启动慢时踩空
    // （踩过：等 2.6s 时项目还没加载完，state.items 为空 → 后续取值为 undefined 直接抛错）。
    const waitFor = async (expr, label, ms = 30000) => {
      const t0 = Date.now();
      while (Date.now() - t0 < ms) {
        if (await evaluate(expr)) return true;
        await sleep(400);
      }
      console.log("WAIT-TIMEOUT:", label, "(" + Math.round((Date.now() - t0) / 1000) + "s)");
      return false;
    };
    await send("Runtime.enable");
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 960, deviceScaleFactor: 1, mobile: false });
    await waitFor(`!!(window.__vc && window.__vc.state && window.__vc.state.items && window.__vc.state.items.length)`, "boot + items");

    // 审计器：注入一次，逐页调用
    await evaluate(`(() => {
      window.__icoAudit = () => {
        const out = { total: 0, ok: 0, missingClass: 0, bad: [] };
        const seen = new Set();
        document.querySelectorAll('i.ico, .sort-arrow, [class*="ico-"]').forEach((el) => {
          if (!el.classList.contains('ico') && !el.classList.contains('sort-arrow')
              && !/^ico-/.test([...el.classList].find(c => c.startsWith('ico-')) || '')) return;
          out.total++;
          const cs = getComputedStyle(el);
          const mask = cs.webkitMaskImage || cs.maskImage || 'none';
          const isFallback = mask.indexOf('data:image/svg') >= 0 || mask === 'none';
          // 只关心「语义图标类」——.ico 自身不算，必须带一个 ico-<name>
          const nameCls = [...el.classList].find(c => c.startsWith('ico-'));
          if (!nameCls && !el.classList.contains('sort-arrow')) { out.missingClass++; return; }
          const key = nameCls || 'sort-arrow';
          if (isFallback) out.bad.push({ cls: key, tag: el.tagName, parent: (el.parentElement && el.parentElement.className || '').slice(0, 40) });
          else out.ok++;
          seen.add(key);
        });
        out.classes = [...seen].sort();
        return out;
      };
      return 'injected';
    })()`);

    // ── 1) 剪辑页 ──
    const r1 = await evaluate(`(async () => {
      const vc = window.__vc;
      if (!vc || !vc.state.items || !vc.state.items.length) return { err: 'no items' };
      await vc.selectItem(vc.state.items[0]);
      await new Promise(r => setTimeout(r, 2600));
      return { items: vc.state.items.length, cur: vc.state.currentItem && vc.state.currentItem.name };
    })()`);
    console.log("EDIT:", JSON.stringify(r1));
    await shot("shot_icons_edit.png");
    const a1 = (await evaluate(`window.__icoAudit && window.__icoAudit()`)) || {};
    console.log("AUDIT-EDIT:", JSON.stringify({ total: a1.total, ok: a1.ok, missingClass: a1.missingClass, bad: a1.bad }));

    // ── 2) 角色池页 ──
    const r2 = await evaluate(`(async () => {
      const vc = window.__vc;
      try { vc.setPage('edit'); } catch (e) {}
      const btn = document.querySelector('#btn-pool2') || document.querySelector('#btn-pool');
      if (btn) btn.click();
      await new Promise(r => setTimeout(r, 1200));
      return { visible: !document.querySelector('#pool-view').classList.contains('hidden') };
    })()`);
    console.log("POOL:", JSON.stringify(r2));
    await shot("shot_icons_pool.png");
    const a2 = (await evaluate(`window.__icoAudit && window.__icoAudit()`)) || {};
    console.log("AUDIT-POOL:", JSON.stringify({ total: a2.total, ok: a2.ok, missingClass: a2.missingClass, bad: a2.bad }));

    // ── 3) 训练交付页 ──
    const r3 = await evaluate(`(async () => {
      const vc = window.__vc;
      const cb = document.querySelector('#pool-close');
      if (cb) cb.click();
      await new Promise(r => setTimeout(r, 500));
      const tb = document.querySelector('#pagebar .page-btn[data-page="train"]');
      if (tb) tb.click();
      await new Promise(r => setTimeout(r, 2200));
      return { poolClosed: document.querySelector('#pool-view').classList.contains('hidden'),
               trainShown: !document.querySelector('#page-train').classList.contains('hidden'),
               roles: document.querySelectorAll('#train-role-list .train-role').length };
    })()`);
    console.log("TRAIN:", JSON.stringify(r3));
    await shot("shot_icons_train.png");
    const a3 = (await evaluate(`window.__icoAudit && window.__icoAudit()`)) || {};
    console.log("AUDIT-TRAIN:", JSON.stringify({ total: a3.total, ok: a3.ok, missingClass: a3.missingClass, bad: a3.bad }));

    // ── 4) 素材库页 + 菜单展开（把 #media-menu / 下拉里的图标也扫进来） ──
    const r4 = await evaluate(`(async () => {
      const vc = window.__vc;
      const mb = document.querySelector('#pagebar .page-btn[data-page="media"]');
      if (mb) mb.click();
      await new Promise(r => setTimeout(r, 1200));
      // 展开「视图」菜单与素材右键菜单，让隐藏区里的图标也进入审计
      const vt = [...document.querySelectorAll('#menubar .menu-title')].find(b => b.textContent.indexOf('视图') >= 0);
      if (vt) vt.click();
      await new Promise(r => setTimeout(r, 300));
      const row = document.querySelector('#media-list li') || document.querySelector('#media-page-list li');
      if (row) row.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 300, clientY: 300 }));
      await new Promise(r => setTimeout(r, 400));
      return { mediaShown: !document.querySelector('#page-media').classList.contains('hidden'),
               menuOpen: !document.querySelector('#media-menu').classList.contains('hidden') };
    })()`);
    console.log("MEDIA:", JSON.stringify(r4));
    await shot("shot_icons_media.png");
    const a4 = (await evaluate(`window.__icoAudit && window.__icoAudit()`)) || {};
    console.log("AUDIT-MEDIA:", JSON.stringify({ total: a4.total, ok: a4.ok, missingClass: a4.missingClass, bad: a4.bad, classes: a4.classes }));

    // ── 5) 表头排序箭头：文本字形（⇅/▼/▲）已换成 mask 图标 + rotate，
    //    这里跑一遍 默认 → 降序 → 升序 三态，核对类切换并截表头特写。 ──
    const r5 = await evaluate(`(async () => {
      // 先切回「剪辑」页 —— 片段列表在别的页面上是 display:none，rect 全 0 会导致裁剪截图为空
      const eb = document.querySelector('#pagebar .page-btn[data-page="edit"]');
      if (eb) eb.click();
      await new Promise(r => setTimeout(r, 900));
      const snap = () => [...document.querySelectorAll('#seg-table thead [data-sort] .sort-arrow')]
        .map(a => (a.className.replace('sort-arrow', '').trim() || 'none'));
      const txt = () => [...document.querySelectorAll('#seg-table thead [data-sort] .sort-arrow')]
        .map(a => a.textContent);          // 必须为空 —— 还有文字就说明没换成图标
      const durTh = document.querySelector('#seg-table thead [data-sort="dur"]');
      const out = { states: [] };
      out.states.push({ seg: 'time', cls: snap(), text: txt() });
      durTh.click(); await new Promise(r => setTimeout(r, 350));
      out.states.push({ seg: 'dur-desc', cls: snap(), text: txt() });
      durTh.click(); await new Promise(r => setTimeout(r, 350));
      out.states.push({ seg: 'dur-asc', cls: snap(), text: txt() });
      durTh.click(); await new Promise(r => setTimeout(r, 350));
      out.states.push({ seg: 'back-to-time', cls: snap(), text: txt() });
      const th = document.querySelector('#seg-table thead tr');
      const r = th.getBoundingClientRect();
      out.clip = { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.min(60, Math.round(r.height) + 30) };
      out.anyText = out.states.some(s => s.text.some(t => t !== ''));
      return out;
    })()`);
    console.log("SORT-ARROWS:", JSON.stringify(r5 && r5.states));
    console.log("SORT-CHECK:", r5 && !r5.anyText ? "✓ 箭头已全部为图标（无文字残留）" : "✗ 仍有文字箭头残留");
    if (r5 && r5.clip) {
      const capS = await send("Page.captureScreenshot", { format: "png", clip: { ...r5.clip, scale: 3 } });
      if (capS.result && capS.result.data) {
        const ps = path.join(OUT, "shot_icons_sortarrow.png");
        fs.writeFileSync(ps, Buffer.from(capS.result.data, "base64"));
        console.log("SHOT:", ps);
      } else { console.log("SHOT-FAIL: sortarrow clip=" + JSON.stringify(r5.clip)); }
    }

    console.log("JS-ERRORS:", JSON.stringify(errs.slice(0, 6)));
  } finally {
    try { child.kill(); } catch (e) {}
  }
})();
