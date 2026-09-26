// 「点击试听后再点其他片段 → 锁死在试听片段、退不出循环」——退出路径矩阵排查
// 用法：node scripts/diag_audition_exit.js [项目id]
//
// 机制：`auditionSegment()` 把 state.auditioning 设为 {start, end, loop:true}，
// 之后由 waveform.js 的 auditionCheck(t) 在每次 timeupdate 越过 end 时把播放头
// setTime(start) 拉回来 —— 一个**粘性状态**。问题在于它只被两条路径解除：
//   ① focusSegment()（点片段行 / 跳转按钮）  ② togglePlay()（空格 / 暂停）
// 其余任何「移动播放头」的入口都不会清它，于是播放头被反复拽回试听片段。
//
// 本探针逐条枚举退出路径，判据不依赖真实播放 tick（headless 下不可靠）：
//   手动 emit('timeupdate', end+0.4) → 若发生 setTime(start) 即为「回卷=没退出」。
// E0 是**阳性对照**（什么都不做必须能检出回卷），否则后面的 OK 都没有意义。
//
// 安全护栏：只点 .seg-aud / .seg-jump / td.col-time / input.seg-text，
// 绝不点 .seg-del 与 td.col-act（上一轮误删过真实片段）；片段表用内存副本 + finally 还原。
const { spawn } = require("child_process");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9418;
const PROF = process.env.TEMP + "\\vc-audexit-" + Date.now();
const BASE = process.env.VC_BASE || "http://127.0.0.1:8765";
const TARGET_PROJECT = process.argv[2] || null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--autoplay-policy=no-user-gesture-required", "--mute-audio",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, BASE + "/"], { stdio: "ignore" });
  try {
    let target = null;
    for (let i = 0; i < 40 && !target; i++) {
      try {
        const res = await fetch("http://127.0.0.1:" + PORT + "/json");
        const l = await res.json();
        target = l.find((t) => t.type === "page") || null;
      } catch (e) {}
      if (!target) await sleep(500);
    }
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let msgId = 0;
    const pending = new Map();
    const errs = [];
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.method === "Runtime.exceptionThrown") errs.push((m.params.exceptionDetails.exception || {}).description || m.params.exceptionDetails.text);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    };
    const send = (method, params = {}) => new Promise((res) => { const id = ++msgId; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
    await send("Runtime.enable");
    await sleep(3500);
    await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1100, deviceScaleFactor: 1, mobile: false });

    // 探针代码不用反斜杠 / 反引号 / ${}
    const r = await send("Runtime.evaluate", { expression: `(async () => { try {
      const sl = (ms) => new Promise((r) => setTimeout(r, ms));
      for (let i = 0; i < 250 && !window.__vc; i++) await sl(100);
      const vc = window.__vc;
      if (!vc) return { err: 'no __vc', href: location.href };
      const unhandled = [];
      window.addEventListener('unhandledrejection', (e) => {
        const rs = e.reason;
        unhandled.push(String((rs && rs.name ? rs.name + ': ' : '') + (rs && rs.message ? rs.message : rs)));
      });

      const plist = await (await fetch('/api/projects')).json();
      const wanted = ${JSON.stringify(TARGET_PROJECT)};
      const proj = wanted ? plist.find(p => p.id === wanted) : plist.find(p => p.item_count > 5);
      if (!proj) return { err: 'no project' };
      await vc.selectProject(proj);
      for (let i = 0; i < 1500 && !(vc.state.currentItem && vc.state.ws); i++) await sl(100);
      for (let i = 0; i < 200 && !(vc.state.ws && vc.state.ws.getDuration() > 0); i++) await sl(150);

      const item = vc.state.currentItem;
      const segs = vc.state.segmentsByItem.get(item.id);
      if (!item || !segs) return { err: 'no current item/segs', item: !!item, segs: !!segs };
      const backup = segs.slice();
      const backupIds = backup.map(s => s.id).join(',');
      const D = Math.max(30, Math.round(vc.state.ws.getDuration() || item.duration || 60));
      const LEN = Math.min(1.5, D * 0.02);
      const starts = [0.02, 0.25, 0.5, 0.75].map(f => +(D * f).toFixed(2));

      const out = { project: proj.name, item: item.name, dur: D, segBackup: backup.length, cases: [] };

      const row = (i) => document.querySelector('#seg-tbody tr.seg-row[data-item="' + item.id + '"][data-i="' + i + '"]');

      try {
        segs.length = 0;
        starts.forEach((s, k) => segs.push(vc.newSegment(s, +(s + LEN).toFixed(2), 'ax' + k)));
        vc.state.segFilter.item = item.id;
        vc.state.segFilter.status = 'all';
        vc.state.segFilter.text = '';
        vc.renderSegments();
        for (let i = 0; i < 60 && !row(3); i++) await sl(100);
        if (!row(3)) { out.cases.push({ label: 'setup', err: 'rows not rendered' }); }
        else {
          // ── 单条用例 ─────────────────────────────────────────
          const runCase = async (label, action, opts) => {
            const o = opts || {};
            // 复位：loop 关 + 无选区（隔离 auditionCheck，确保只有它能回卷）+ 无序列
            vc.state.loop = false;
            vc.state.auditioning = null; vc.state.auditionSeq = null; vc.state.auditionIdx = 0;
            vc.state.selection = null; vc.state.activeSeg = null;
            try { vc.clearSelection(); } catch (e) {}
            document.querySelectorAll('#seg-tbody tr.seg-row.playing').forEach(el => el.classList.remove('playing'));
            try { vc.state.ws.pause(); } catch (e) {}
            vc.state.ws.setTime(starts[0]);
            await sl(320);
            // 武装：点第 1 行的「试听」按钮（真实入口）
            const b = row(1) && row(1).querySelector('button.seg-aud');
            if (!b) { out.cases.push({ label, err: 'no audition button' }); return; }
            b.click();
            for (let k = 0; k < 25 && !vc.state.auditioning; k++) await sl(60);
            const arm = vc.state.auditioning;
            if (!arm) { out.cases.push({ label, err: 'arm failed' }); return; }
            const a = { start: arm.start, end: arm.end };
            // 观察 setTime：raw 即「模块包装后的 setTime」，行为（回声窗口）不受影响
            let raw = vc.state.ws.setTime.bind(vc.state.ws);
            let calls = [];
            const observe = () => { vc.state.ws.setTime = (t) => { calls.push(+Number(t).toFixed(2)); return raw(t); }; };
            observe();
            // 执行退出动作
            await action(a);
            await sl(420);
            const armed = !!vc.state.auditioning;
            const actCalls = calls.slice(0, 10);
            // 切素材后 ws 被重建：重新挂观察器（否则量不到新实例）
            if (o.rebind && vc.state.ws) { calls = []; raw = vc.state.ws.setTime.bind(vc.state.ws); observe(); }
            // 回卷探测：手动 timeupdate 越过试听窗口终点
            calls = [];
            vc.state.ws.emit('timeupdate', a.end + 0.4);
            await sl(150);
            const wrapCalls = calls.slice(0, 6);
            vc.state.ws.setTime = raw;
            const playing = !!vc.state.playing;
            try { vc.state.ws.pause(); } catch (e) {}
            // 两种"被拽回"：① 探测用的 timeupdate 触发回卷  ② 动作后的等待期内已回卷
            // （② 表现为 actCalls 首项是目标位置、其后又出现 a.start —— E5/E6/E8/E9 即此种）
            const wrapped = wrapCalls.some(c => Math.abs(c - a.start) < 0.05);
            const pulled = actCalls.slice(1).some(c => Math.abs(c - a.start) < 0.05);
            const looped = wrapped || pulled;
            const ok = o.expectArm ? (armed && looped) : (!armed && !looped);
            out.cases.push({ label, aStart: +a.start.toFixed(2), aEnd: +a.end.toFixed(2),
              armed, looped, pulled, wrapped, playing, seeked: actCalls.length > 0,
              actCalls, wrapCalls, ok });
          };

          // E0 阳性对照：不操作 → 必须仍处于循环（否则判据失灵，后面全不可信）
          await runCase('E0 阳性对照：不操作', async () => {}, { expectArm: true });
          // E1 点另一行的时间列（常规行点击）
          await runCase('E1 行点击 td.col-time', async () => { const td = row(3) && row(3).querySelector('td.col-time'); if (td) td.click(); });
          // E2 点另一行的文本框列（app.js 有 closest("input,select") 早退）
          await runCase('E2 行内文本框 input.seg-text', async () => { const el = row(3) && row(3).querySelector('input.seg-text'); if (el) el.click(); });
          // E3 点另一行的「跳转」按钮（走 focusSegment）
          await runCase('E3 跳转按钮 .seg-jump', async () => { const el = row(3) && row(3).querySelector('button.seg-jump'); if (el) el.click(); });
          // E5 时间轴原地点击 = 跳转播放头
          await runCase('E5 时间轴原地点击', async () => {
            const tl = document.getElementById('timeline');
            if (!tl) return;
            const wr = vc.state.ws.getWrapper().getBoundingClientRect();
            const x = wr.left + Math.min(0.95, starts[2] / (vc.state.ws.getDuration() || 1)) * wr.width;
            tl.dispatchEvent(new MouseEvent('mousedown', { button: 0, clientX: x, clientY: tl.getBoundingClientRect().top + 5, bubbles: true }));
            window.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
          });
          // E6 总览条（minimap）点击
          await runCase('E6 总览条 pointerdown', async () => {
            const mw = document.getElementById('minimap-wrap');
            if (!mw) return;
            const r2 = mw.getBoundingClientRect();
            const x = r2.left + r2.width * 0.8, y = r2.top + r2.height / 2;
            mw.dispatchEvent(new PointerEvent('pointerdown', { clientX: x, clientY: y, bubbles: true }));
            mw.dispatchEvent(new PointerEvent('pointerup', { clientX: x, clientY: y, bubbles: true }));
          });
          // E7 跳到末尾按钮（setTime(duration)，必然越过试听窗口终点）
          await runCase('E7 按钮 #btn-next', async () => { document.getElementById('btn-next').click(); });
          // E7b 回到开头按钮
          await runCase('E7b 按钮 #btn-prev', async () => { document.getElementById('btn-prev').click(); });
          // E8 方向键快进（seekBy）
          await runCase('E8 方向键 ArrowRight', async () => { document.dispatchEvent(new KeyboardEvent('keydown', { code: 'ArrowRight', key: 'ArrowRight', bubbles: true })); });
          // E9 点击波形区（wavesurfer 交互插件 seek）
          await runCase('E9 波形区点击', async () => {
            const w = vc.state.ws.getWrapper();
            const r3 = w.getBoundingClientRect();
            w.dispatchEvent(new MouseEvent('click', { clientX: r3.left + r3.width * 0.7, clientY: r3.top + r3.height / 2, bubbles: true }));
          });
          // E10 切换素材（侧栏点另一个素材 → selectItem，ws 被重建）
          await runCase('E10 切换素材', async () => {
            const other = vc.state.items.find(x => x.id !== item.id);
            if (!other) return;
            const li = document.querySelector('#media-list li[data-id="' + other.id + '"]');
            if (li) li.click(); else await vc.selectItem(other);
            await sl(1500);
          }, { rebind: true });
          // E11 关闭循环开关（用户最直观的「退出循环」手势：再点一次「循环中」按钮）
          await runCase('E11 点 #btn-loop 开→关', async () => {
            const lb = document.getElementById('btn-loop');
            lb.click(); await sl(120); lb.click();
          });
          // E12 控制组：暂停按钮（已知可退出）
          await runCase('E12 控制组：暂停 #btn-play2', async () => { document.getElementById('btn-play2').click(); });
        }
      } finally {
        segs.length = 0;
        backup.forEach(s => segs.push(s));
        vc.state.segFilter.item = 'all';
        vc.state.loop = false;
        vc.state.auditioning = null; vc.state.auditionSeq = null; vc.state.auditionIdx = 0;
        try { if (vc.state.ws) vc.state.ws.pause(); } catch (e) {}
        try { vc.clearSelection(); } catch (e) {}
        vc.renderSegments();
        await sl(200);
        out.restored = segs.map(s => s.id).join(',') === backupIds;
        out.dirty = Array.from(vc.state.dirtyItems || []).length;
      }

      out.unhandled = unhandled.slice(0, 6);
      return out;
    } catch (e) { return { err: String(e && e.stack ? e.stack : e) }; } })()`, awaitPromise: true, returnByValue: true });

    const v = r.result && r.result.result && r.result.result.value;
    if (r.error) console.log("CDP-ERROR:", JSON.stringify(r.error));
    if (r.result && r.result.exceptionDetails) console.log("EXC:", JSON.stringify(r.result.exceptionDetails));
    if (!v) { console.log("NO VALUE"); console.log("RAW:", JSON.stringify(r).slice(0, 800)); }
    else if (v.err) { console.log("ERR:", v.err); }
    else {
      console.log("项目=" + v.project + "  素材=" + v.item + "  时长=" + v.dur + "s  原片段=" + v.segBackup +
        "  还原=" + v.restored + "  dirty=" + v.dirty);
      console.log("");
      console.log("结果         退出路径                                      armed(残留循环)  被拽回  playing  setTime 序列");
      for (const c of v.cases) {
        if (c.err) { console.log("ERR          " + c.label + "  " + c.err); continue; }
        const tag = c.ok ? "OK  " : "FAIL";
        console.log(tag + "         " + String(c.label).padEnd(44, " ") +
          " " + String(c.armed).padEnd(15, " ") + " " + String(c.looped).padEnd(6, " ") +
          " " + String(c.playing).padEnd(7, " ") +
          JSON.stringify(c.actCalls) + (c.wrapped ? "  wrap=" + JSON.stringify(c.wrapCalls) : ""));
      }
      console.log("");
      const fails = v.cases.filter(c => c.ok === false).map(c => c.label);
      console.log(fails.length ? "FAIL 计数=" + fails.length + " → " + fails.join(" | ") : "全部通过");
      if (v.unhandled && v.unhandled.length) console.log("unhandledrejection: " + JSON.stringify(v.unhandled));
    }
    if (errs.length) console.log("页面异常:", JSON.stringify(errs.slice(0, 5)));
  } finally {
    try { child.kill(); } catch (e) {}
  }
})();
