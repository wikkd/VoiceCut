// 片段列表「点击不跳转」——机制类假设排查
// 用法：node scripts/diag_jump_modes.js [项目id]
//
// 上一轮结论：按列坐标真实点击时，.col-text / .col-lang / .col-spk（合计约 54% 行宽）
// 点了完全没反应（走 app.js 的 `closest("input, select") → return`）。
// 但那可能只是「设计如此」。本脚本继续排查**机制类**的不确定性：
//   P2 循环模式 ON 且有远处选区 → loopCheck 会不会把播放头拽回选区起点
//   P3 播放中点击
//   P4 state.auditioning（单段循环试听）残留时点击
//   P5 state.auditionSeq（多段序列试听）残留时点击
//   P6 跨素材快速连点（两次 selectItem 竞态 → currentItem 与 ws 错配）
//   P7 虚拟列表滚动后立刻点击（窗口重建与点击同帧）
// 点击统一落在 td.col-time（无交互控件），隔离「点在哪一列」这个变量。
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 9417;
const PROF = process.env.TEMP + "\\vc-jumpmodes-" + Date.now();
const BASE = process.env.VC_BASE || "http://127.0.0.1:8765";
const TARGET_PROJECT = process.argv[2] || null;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--autoplay-policy=no-user-gesture-required", "--mute-audio",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, `${BASE}/`], { stdio: "ignore" });
  try {
    let target = null;
    for (let i = 0; i < 40 && !target; i++) {
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
    await sleep(3500);
    await send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 1100, deviceScaleFactor: 1, mobile: false });

    // 探针代码不用反斜杠 / 反引号 / ${}
    const r = await send("Runtime.evaluate", { expression: `(async () => { try {
      const sl = (ms) => new Promise((r) => setTimeout(r, ms));
      for (let i = 0; i < 250 && !window.__vc; i++) await sl(100);
      const vc = window.__vc;
      if (!vc) return { err: 'no __vc' };
      const unhandled = [];
      window.addEventListener('unhandledrejection', (e) => {
        const rs = e.reason;
        unhandled.push(String((rs && rs.name ? rs.name + ': ' : '') + (rs && rs.message ? rs.message : rs)));
      });
      const plist = await (await fetch('/api/projects')).json();
      const wanted = ${JSON.stringify(TARGET_PROJECT)};
      const proj = wanted ? plist.find(p => p.id === wanted) : plist.find(p => p.item_count > 5);
      await vc.selectProject(proj);
      for (let i = 0; i < 1500; i++) { if (vc.state._segRows && vc.state._segRows.length > 0) break; await sl(100); }
      // 等首素材音频就绪
      await sl(2500);

      const sc = document.getElementById('seg-scroll');
      const dom = () => Array.from(document.querySelectorAll('#seg-tbody tr.seg-row'));
      const pitch = () => { const a = dom(); return a.length > 1 ? Math.round(a[1].getBoundingClientRect().top - a[0].getBoundingClientRect().top) : 36; };
      const segsOf = (id) => vc.state.segmentsByItem.get(id) || [];
      const curId = () => (vc.state.currentItem ? vc.state.currentItem.id : null);

      // 把 (itemId, i) 的行滚进视口，返回可点的 td.col-time 元素
      const reach = async (itemId, i) => {
        const sc2 = sc, p = pitch();
        const idx = (vc.state._segRows || []).findIndex((r) => r.item.id === itemId && segsOf(itemId).indexOf(r.seg) === i);
        if (idx < 0) return null;
        for (let a = 0; a < 8; a++) {
          sc2.scrollTop = Math.max(0, idx * p - 150 - a * p * 3);
          await sl(90);
          const tr = document.querySelector('#seg-tbody tr.seg-row[data-item="' + itemId + '"][data-i="' + i + '"]');
          if (tr) {
            const rr = tr.getBoundingClientRect();
            if (rr.top > 180 && rr.bottom < 1040) return tr.querySelector('td.col-time');
          }
        }
        return null;
      };

      const segAt = (itemId, i) => segsOf(itemId)[i];
      const pickI = (itemId, frac) => Math.max(1, Math.min(segsOf(itemId).length - 1, Math.floor(segsOf(itemId).length * frac)));

      const out = { project: proj.name, totalRows: (vc.state._segRows || []).length, cases: [] };

      // 通用：设置前置状态 → 点行 → 采样
      const run = async (label, itemId, i, prep) => {
        const seg = segAt(itemId, i);
        if (!seg) { out.cases.push({ label, err: 'no seg' }); return; }
        const td = await reach(itemId, i);
        if (!td) { out.cases.push({ label, err: 'row unreachable' }); return; }
        if (prep) await prep(itemId, seg);
        // 先把播放头挪开
        vc.state.ws.setTime(Math.max(0, seg.start > 40 ? seg.start - 30 : seg.start + 30));
        await sl(260);
        const before = vc.state.ws.getCurrentTime();
        td.click();
        await sl(60); const s60 = vc.state.ws.getCurrentTime();
        await sl(500); const s560 = vc.state.ws.getCurrentTime();
        await sl(1000); const s1560 = vc.state.ws.getCurrentTime();
        const near = (t) => Math.abs(t - seg.start) < 0.45;
        out.cases.push({ label, item: itemId, i, want: +seg.start.toFixed(2),
          before: +before.toFixed(2), s60: +s60.toFixed(2), s560: +s560.toFixed(2), s1560: +s1560.toFixed(2),
          near60: near(s60), nearFinal: near(s1560),
          cur: curId(), curOk: curId() === itemId,
          active: !!(vc.state.activeSeg && vc.state.activeSeg.segId === seg.id),
          loop: !!vc.state.loop, play: !!(vc.state.ws && vc.state.ws.isPlaying()),
          aud: !!vc.state.auditioning, seq: vc.state.auditionSeq ? vc.state.auditionSeq.length : 0 });
      };

      const A = curId();                       // 当前素材
      const others = vc.state.items.filter((it) => it.id !== A && segsOf(it.id).length > 20);
      const B = others.length ? others[0].id : A;

      // ── P1 基线 ──
      vc.state.loop = false; vc.state.auditioning = null; vc.state.auditionSeq = null;
      if (vc.state.ws.isPlaying()) vc.state.ws.pause();
      await run('P1 基线(空闲)', A, pickI(A, 0.5), null);

      // ── P2 循环模式 ON + 已有远处选区 ──
      await run('P2 循环ON+远处选区', A, pickI(A, 0.75), async (itemId, seg) => {
        vc.state.loop = true;
        const s0 = segsOf(itemId)[1];
        vc.state.ws.setTime(s0.start);
        await sl(200);
        // 用真实入口建一个在目标片段之前的选区
        vc.setSelection ? vc.setSelection(s0.start, s0.end) : (window.__vc.selectItem && 0);
        const w = vc.state;
        if (w.selection === null) { /* 无外部入口时退化：直接改 state */ }
        const r = await (async () => {
          // 借时间轴拖拽之外的最短路径：直接给区域插桩
          try {
            w.selection = { start: s0.start, end: s0.end };
            if (w.selectionRegion) w.selectionRegion.setOptions({ start: s0.start, end: s0.end });
          } catch (e) {}
          return 1;
        })();
        await sl(150);
      });

      // ── P3 播放中点击 ──
      vc.state.loop = false;
      await run('P3 播放中点击', A, pickI(A, 0.3), async () => {
        try { await vc.state.ws.play(); } catch (e) {}
        await sl(400);
      });
      try { vc.state.ws.pause(); } catch (e) {}
      await sl(200);

      // ── P4 残留单段循环试听 ──
      await run('P4 残留 auditioning', A, pickI(A, 0.6), async (itemId, seg) => {
        vc.state.auditioning = { start: seg.start + 12, end: seg.start + 20, loop: true };
        await sl(100);
      });
      vc.state.auditioning = null;

      // ── P5 残留多段序列试听 ──
      await run('P5 残留 auditionSeq', A, pickI(A, 0.65), async (itemId) => {
        const s = segsOf(itemId);
        vc.state.auditionSeq = [{ item: null, start: s[2].start, end: s[2].end }, { item: null, start: s[3].start, end: s[3].end }];
        vc.state.auditionIdx = 0;
        await sl(100);
      });
      vc.state.auditionSeq = null;

      // ── P6 跨素材快速连点（竞态）──
      {
        const iA = pickI(A, 0.4), iB = pickI(B, 0.4);
        const tdA = await reach(A, iA);
        if (tdA) {
          tdA.click();
          await sl(120);                       // 上一次 selectItem 还在飞
          const tdB = await reach(B, iB);
          if (tdB) {
            tdB.click();
            await sl(1800);
            const segB = segAt(B, iB);
            const g = vc.state.ws.getCurrentTime();
            out.cases.push({ label: 'P6 跨素材快速连点', item: B, i: iB, want: +segB.start.toFixed(2),
              s60: +g.toFixed(2), s560: +g.toFixed(2), s1560: +g.toFixed(2),
              near60: Math.abs(g - segB.start) < 0.45, nearFinal: Math.abs(g - segB.start) < 0.45,
              cur: curId(), curOk: curId() === B,
              wsDur: vc.state.ws ? vc.state.ws.getDuration() : null,
              itemDur: (vc.state.items.find(x => x.id === B) || {}).duration,
              active: !!(vc.state.activeSeg && vc.state.activeSeg.segId === segB.id) });
          }
        }
      }

      // ── P7 虚拟列表滚动后同帧点击 ──
      {
        const iA = pickI(A, 0.8);
        const idx = (vc.state._segRows || []).findIndex((r) => r.item.id === A && segsOf(A).indexOf(r.seg) === iA);
        sc.scrollTop = Math.max(0, idx * pitch() - 160);
        const tr = document.querySelector('#seg-tbody tr.seg-row[data-item="' + A + '"][data-i="' + iA + '"]');
        if (tr) {
          const seg = segAt(A, iA);
          vc.state.ws.setTime(Math.max(0, seg.start - 30));
          await sl(240);
          tr.querySelector('td.col-time').click();     // 不等待 rAF 重排
          await sl(56);
          const s = vc.state.ws.getCurrentTime();
          out.cases.push({ label: 'P7 滚动后同帧点击', item: A, i: iA, want: +seg.start.toFixed(2),
            s60: +s.toFixed(2), near60: Math.abs(s - seg.start) < 0.45, nearFinal: Math.abs(s - seg.start) < 0.45 });
        } else {
          out.cases.push({ label: 'P7 滚动后同帧点击', err: 'row gone after scroll' });
        }
      }

      // ── P8/P9/P10 快速连点两行（videoSync/videoToAudioSync 反向回写的竞态）──
      const rapid = async (label, gapMs) => {
        const i1 = pickI(A, 0.25), i2 = pickI(A, 0.55);
        const t1 = await reach(A, i1);
        if (!t1) { out.cases.push({ label, err: 'row1 unreachable' }); return; }
        vc.state.ws.setTime(Math.max(0, segAt(A, i1).start - 30));
        await sl(1500);                                   // 让上一次 seek 彻底落地
        t1.click();
        await sl(gapMs);
        const t2 = await reach(A, i2);                    // 第二次点击前需要重新取元素（行已重渲染）
        if (!t2) { out.cases.push({ label, err: 'row2 unreachable' }); return; }
        t2.click();
        await sl(1600);
        const want = segAt(A, i2).start, got = vc.state.ws.getCurrentTime();
        const want1 = segAt(A, i1).start;
        const v = document.getElementById('video-preview');
        out.cases.push({ label, item: A, i: i2, want: +want.toFixed(2),
          s60: +got.toFixed(2), s560: +got.toFixed(2), s1560: +got.toFixed(2),
          near60: Math.abs(got - want) < 0.45, nearFinal: Math.abs(got - want) < 0.45,
          cur: curId(), curOk: curId() === A,
          active: !!(vc.state.activeSeg && vc.state.activeSeg.segId === segAt(A, i2).id),
          hasVideo: !!(v && v.src), landedPrev: Math.abs(got - want1) < 0.45,
          note: 'prev=' + want1.toFixed(2) });
      };
      await rapid('P8 连点间隔120ms', 120);
      await rapid('P9 连点间隔400ms', 400);
      await rapid('P10 连点间隔900ms', 900);

      if (vc.state.loop) vc.state.loop = false;
      out.unhandled = unhandled.slice(0, 8); out.unhandledCount = unhandled.length;
      return out;
    } catch (e) { return { err: String((e && e.stack) || (e && e.message) || e) }; } })()`,
      awaitPromise: true, returnByValue: true });

    const v = r.result.result.value || {};
    if (r.result.exceptionDetails) console.log("EVAL-EXC:", JSON.stringify(r.result.exceptionDetails).slice(0, 900));
    if (v.err) { console.error("FATAL:", v.err); return; }
    console.log("PROJECT:", v.project, "| rows", v.totalRows);
    console.log("\n=== 机制类假设 ===");
    (v.cases || []).forEach((c) => {
      if (c.err) { console.log("  [" + c.label + "] ERR " + c.err); return; }
      const tag = c.nearFinal ? "跳到" : (c.near60 ? "先跳后拽回" : "未跳");
      console.log("  " + (c.nearFinal ? "OK  " : "FAIL") + " " + c.label.padEnd(22) +
        " | want=" + String(c.want).padStart(9) +
        " before=" + String(c.before).padStart(9) +
        " → 60ms=" + String(c.s60).padStart(9) + " 560ms=" + String(c.s560).padStart(9) +
        " 1560ms=" + String(c.s1560).padStart(9) +
        " | active=" + c.active + " curOk=" + c.curOk +
        (c.loop ? " loop=1" : "") + (c.play ? " playing=1" : "") +
        (c.aud ? " aud=1" : "") + (c.seq ? " seq=" + c.seq : "") +
        (c.wsDur !== undefined ? " wsDur=" + c.wsDur + " itemDur=" + c.itemDur : "") +
        "   [" + tag + "]");
    });
    console.log("\nunhandled(" + v.unhandledCount + "): " + JSON.stringify(v.unhandled));
    console.log("CDP exceptions: " + JSON.stringify(errs.slice(0, 6)));
  } finally {
    child.kill();
    await sleep(400);
    try { fs.rmSync(PROF, { recursive: true, force: true }); } catch (e) {}
  }
})();
