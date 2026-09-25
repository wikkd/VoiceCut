// 验证修复：连续试听序列中手动暂停后，播放必须保持停止（不再被 in-flight playSeqItem 拉起）
const { spawn } = require("child_process");
const PORT = Number(process.argv[2] || 9355);
const BASE = "http://127.0.0.1:8765";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PROF = process.env.TEMP + "\\vc-pause5-" + Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const child = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--autoplay-policy=no-user-gesture-required",
    "--remote-debugging-port=" + PORT, "--user-data-dir=" + PROF, BASE + "/"], { stdio: "ignore" });
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
    const send = (method, params = {}) => new Promise((res) => { msgId++; pending.set(msgId, res); ws.send(JSON.stringify({ id: msgId, method, params })); });
    const evalJs = async (expr) => {
      const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 600));
      return r.result && r.result.result ? r.result.result.value : undefined;
    };

    await sleep(4000);
    console.log("boot:", JSON.stringify(await evalJs(`({items: window.__vc.state.items.length})`)));

    const probe = (tag) => evalJs(`(() => {
      const st = window.__vc.state;
      return {
        tag: "${tag}",
        flag: st.playing,
        ws: st.ws ? st.ws.isPlaying() : null,
        t: st.ws ? +st.ws.getCurrentTime().toFixed(2) : null,
        item: st.currentItem ? st.currentItem.name : null,
        seq: st.auditionSeq ? st.auditionSeq.length : null,
        playing_rows: document.querySelectorAll("#seg-tbody tr.seg-row.playing").length
      };
    })()`);

    // ── 跨素材序列：item0 seg → item1 seg，中途手动暂停 ──
    console.log("--- cross-item seq + manual pause ---");
    const ok = await evalJs(`(async () => {
      const st = window.__vc.state;
      if (!st.currentItem) return "no-item";
      const a = st.currentItem, b = st.items.find(x => x.id !== a.id);
      if (!b) return "no-second";
      const seq = [
        { item: a, itemId: a.id, segId: "fake-a", start: 1, end: 4 },
        { item: b, itemId: b.id, segId: "fake-b", start: 1, end: 4 }
      ];
      window.__vc.playSequence(seq, 0);
      return "started";
    })()`);
    console.log("seq:", ok);
    await sleep(2000);
    console.log(JSON.stringify(await probe("seg0 playing")));
    // 手动暂停（用户按空格/点按钮）
    await evalJs(`document.querySelector("#btn-play2").click()`);
    await sleep(500);
    console.log(JSON.stringify(await probe("paused")));
    // 静置 5 秒：若 bug 未修，playSeqItem 的 selectItem 返回后会自动恢复播放并切到 item b
    await sleep(5000);
    const p2 = await probe("paused+5s");
    console.log(JSON.stringify(p2));
    const pass = p2.flag === false && p2.ws === false && p2.seq === null;
    console.log(pass ? "PASS: 手动暂停后序列已终止，无自动恢复" : "FAIL: 播放被自动恢复或序列未清");

    // ── 回归：暂停后再按播放仍正常 ──
    await evalJs(`document.querySelector("#btn-play2").click()`);
    await sleep(1200);
    const p3 = await probe("resume");
    console.log(JSON.stringify(p3));
    await evalJs(`document.querySelector("#btn-play2").click()`);
    await sleep(600);
    const p4 = await probe("pause-again");
    console.log(JSON.stringify(p4));
    console.log(p4.flag === false ? "PASS: 回归正常" : "FAIL: 回归异常");
    console.log("DONE");
  } finally {
    try { child.kill(); } catch (e) {}
    await sleep(300);
  }
  process.exit(0);
})().catch((e) => { console.error("FAIL:", e.message); process.exit(1); });
