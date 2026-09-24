// VoiceCut 前端 — 训练交付页（GPT-SoVITS 管线：导出/预处理/S2/S1/试听）
// 由 createTraining(ctx) 创建；ctx 注入共享依赖（util + state + 主流程 trackTask），
// 避免与主流程模块形成循环 import。返回的 API 供 setPage / window.__vc 使用。
export function createTraining(ctx) {
  const { $, esc, shortName, fmtDur, api, state, toast, trackTask } = ctx;

  // ── 训练交付页 ──
  const train = { roles: [], selected: null, settings: null, logTimer: null };
  function trainRoleSel() { return train.roles.find(r => r.id === train.selected) || null; }

  async function loadTraining(full) {
    try {
      const j = await api("/api/training/status");
      train.roles = j.roles || [];
      train.settings = j.settings || {};
      const gpuEl = $("#train-gpu-state");
      if (gpuEl) gpuEl.textContent = j.api && j.api.running ? "推理服务运行中 (端口 " + j.api.port + ")" : "推理服务未启动";
      renderTrainRoles();
      renderTrainQueue();
      if (!train.selected || !train.roles.find(rr => rr.id === train.selected)) {
        train.selected = train.roles.length ? train.roles[0].id : null;
        full = true;
      }
      renderTrainPipeline();
      if (full) renderTrainInspector();
    } catch (e) {
      const box = $("#train-role-list");
      if (box) box.innerHTML = '<div class="muted pad">训练状态加载失败: ' + esc(e.message) + '</div>';
    }
  }

  function renderTrainRoles() {
    const box = $("#train-role-list");
    if (!box) return;
    box.innerHTML = "";
    if (!train.roles.length) {
      box.innerHTML = '<div class="muted pad">项目里还没有角色。先导入素材并在「剪辑」页执行「识别说话人」（项目级分析），再回到这里训练。</div>';
      return;
    }
    train.roles.forEach(r => {
      const card = document.createElement("div");
      card.className = "train-role-card" + (r.id === train.selected ? " active" : "");
      const w = r.weights || {};
      const tags = [];
      tags.push(`<span class="tag">${r.clips} 片段 · ${fmtDur(r.duration)}</span>`);
      if (r.dataset && r.dataset.exists) tags.push(`<span class="tag ${r.dataset.preprocessed ? "ok" : "warn"}">${r.dataset.preprocessed ? "已预处理" : "已导出"}</span>`);
      if (w.gpt && w.sovits) tags.push('<span class="tag ok">已训练</span>');
      if (r.pipeline && r.pipeline.running) tags.push('<span class="tag run">训练中</span>');
      card.innerHTML = `<div class="train-role-name"><span class="train-role-dot" style="background:${esc(r.color)}"></span>${esc(r.name)}</div>
        <div class="train-role-meta">${tags.join("")}</div>`;
      card.addEventListener("click", () => {
        train.selected = r.id;
        renderTrainRoles(); renderTrainPipeline(); renderTrainInspector();
      });
      box.appendChild(card);
    });
  }

  function renderTrainQueue() {
    const box = $("#train-queue-items");
    if (!box) return;
    const running = train.roles.filter(r => r.pipeline && r.pipeline.running);
    if (!running.length) { box.textContent = "暂无排队任务"; box.className = "muted"; return; }
    box.className = "";
    box.innerHTML = "";
    running.forEach(r => {
      const chip = document.createElement("span");
      chip.className = "queue-chip";
      chip.innerHTML = `<span class="qc-dot" style="background:${esc(r.color)}"></span>${esc(r.name)} 训练中`;
      box.appendChild(chip);
    });
  }

  function renderTrainPipeline() {
    const title = $("#train-pipeline-title");
    const stepsBox = $("#train-steps");
    const infoBox = $("#train-dataset-info");
    const logBox = $("#train-log");
    if (!title || !stepsBox || !infoBox || !logBox) return;
    const r = trainRoleSel();
    if (!r) {
      title.textContent = "— 选择左侧角色 —";
      stepsBox.innerHTML = ""; infoBox.innerHTML = "";
      logBox.textContent = "（选择角色后显示）";
      stopTrainLogPoll();
      return;
    }
    const w = r.weights || {};
    const ds = r.dataset || {};
    const running = !!(r.pipeline && r.pipeline.running);
    title.textContent = "管线 · " + r.name + (running ? "（训练中）" : "");
    infoBox.innerHTML = [
      `角色 <b>${esc(r.name)}</b>`,
      `片段 <b>${r.clips}</b>`,
      `总时长 <b>${fmtDur(r.duration)}</b>`,
      `语言 <b>${esc(r.language)}</b>`,
      `exp <b>${esc(r.exp)}</b>`,
      ds.dir ? `数据集 <b title="${esc(ds.dir)}">${esc(shortName(ds.dir, 46))}</b>` : "数据集 未导出",
    ].join(" · ");
    const steps = [
      { name: "① 导出数据集", state: ds.exists ? (ds.list_lines ? "ok" : "warn") : "idle",
        desc: ds.exists ? `已导出 ${ds.wavs} wav / ${ds.list_lines} 条 list` : "未导出", act: "export" },
      { name: "② 预处理(文本/SSL/语义)", state: ds.preprocessed ? "ok" : "idle",
        desc: ds.preprocessed ? "2-name2text/3-bert/4-cnhubert/5-wav32k/6-name2semantic 已生成" : "未预处理", act: "preprocess" },
      { name: "③ SoVITS 训练 (S2)", state: w.sovits ? "ok" : "idle",
        desc: w.sovits ? shortName(w.sovits, 56) : "未训练", act: "train-s2" },
      { name: "④ GPT 训练 (S1)", state: w.gpt ? "ok" : "idle",
        desc: w.gpt ? shortName(w.gpt, 56) : "未训练", act: "train-s1" },
    ];
    stepsBox.innerHTML = "";
    steps.forEach(st => {
      const card = document.createElement("div");
      card.className = "step-card";
      card.innerHTML = `<span class="st-name">${st.name}</span>
        <span class="st-state ${st.state}">${st.state === "ok" ? "✓ " + st.desc : st.desc}</span>
        <span class="st-actions"><button class="btn primary" data-step="${st.act}" ${running ? "disabled" : ""}>${st.state === "ok" ? "重跑" : "执行"}</button></span>`;
      card.querySelector("button").addEventListener("click", () => startTrain(st.act));
      stepsBox.appendChild(card);
    });
    const runCard = document.createElement("div");
    runCard.className = "step-card";
    runCard.style.borderColor = "var(--accent)";
    runCard.innerHTML = `<span class="st-name" style="font-weight:700">一键全链</span>
      <span class="st-state">导出 → 预处理 → S2 → S1（串行）</span>
      <span class="st-actions"><button id="btn-train-run" class="btn primary" ${running ? "disabled" : ""}>${running ? "训练中" : "开始训练"}</button></span>`;
    runCard.querySelector("button").addEventListener("click", () => startTrain("pipeline"));
    stepsBox.appendChild(runCard);
    if (running) { pollTrainLog(r.pipeline.task_id, r.exp); logBox.textContent = "（训练中，日志加载中…）"; }
    else { stopTrainLogPoll(); loadTrainLog(r.exp); }
  }

  function stopTrainLogPoll() { if (train.logTimer) { clearInterval(train.logTimer); train.logTimer = null; } }
  function loadTrainLog(exp) {
    api(`/api/training/roles/x/logs?exp=${encodeURIComponent(exp)}`).then(j => {
      const logBox = $("#train-log");
      if (logBox) logBox.textContent = j.logs || "（暂无日志）";
    }).catch(() => {});
  }
  function pollTrainLog(taskId, exp) {
    stopTrainLogPoll();
    loadTrainLog(exp);
    train.logTimer = setInterval(() => {
      loadTrainLog(exp);
      const r = trainRoleSel();
      if (!r || !(r.pipeline && r.pipeline.running)) stopTrainLogPoll();
    }, 2500);
  }

  function trainBody() {
    const r = trainRoleSel();
    const pick = (s) => { const el = $(s); return el ? el.value : null; };
    return {
      project_id: state.currentProject ? state.currentProject.id : null,
      language: pick("#tf-language") || "ja",
      epochs_s1: pick("#tf-epochs-s1") ? Number(pick("#tf-epochs-s1")) : null,
      epochs_s2: pick("#tf-epochs-s2") ? Number(pick("#tf-epochs-s2")) : null,
      val_ratio: Number(pick("#tf-val") || 0) || 0,
      exp_name: (pick("#tf-exp") || "").trim() || (r ? r.exp : ""),
    };
  }

  async function startTrain(stage) {
    const r = trainRoleSel();
    if (!r) return toast("请先选择角色");
    const body = trainBody();
    let url = `/api/training/roles/${r.id}/pipeline`;
    if (stage === "export") Object.assign(body, { export: true, preprocess: false, train_s2: false, train_s1: false });
    else if (stage === "preprocess") { url = `/api/training/roles/${r.id}/preprocess`; Object.assign(body, { export: true }); }
    else if (stage === "train-s2") url = `/api/training/roles/${r.id}/train-s2`;
    else if (stage === "train-s1") url = `/api/training/roles/${r.id}/train-s1`;
    toast("提交训练任务…");
    try {
      const j = await api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (j.task_id) {
        trackTask(j.task_id, async (result) => {
          toast("训练管线完成：" + ((result && result.exp) || ""));
          stopTrainLogPoll();
          await loadTraining(true);
        });
        pollTrainLog(j.task_id, j.exp || r.exp);
        await loadTraining();
      } else toast("已提交");
    } catch (e) { toast("提交训练失败: " + e.message, 6000); }
  }

  function renderTrainInspector() {
    const r = trainRoleSel();
    const cfgBox = $("#train-config-form");
    const infBox = $("#train-infer-panel");
    if (!cfgBox || !infBox) return;
    const s = train.settings || {};
    cfgBox.innerHTML = `
      <label>语言
        <select id="tf-language">
          <option value="ja" ${s.language === "ja" ? "selected" : ""}>ja（日语）</option>
          <option value="zh" ${s.language === "zh" ? "selected" : ""}>zh（中文）</option>
          <option value="en" ${s.language === "en" ? "selected" : ""}>en（英语）</option>
        </select>
      </label>
      <label>GPT-SoVITS 根目录 <input id="tf-root" type="text" value="${esc(s.root || "")}"></label>
      <label>解释器 <input id="tf-python" type="text" value="${esc(s.python || "")}" title="留空=自动探测"></label>
      <label>推理端口 <input id="tf-port" type="number" value="${s.api_port || 9880}"></label>
      <label>S1 训练轮数 <input id="tf-epochs-s1" type="number" value="${s.epochs_s1 || ""}" placeholder="默认用模板"></label>
      <label>S2 训练轮数 <input id="tf-epochs-s2" type="number" value="${s.epochs_s2 || ""}" placeholder="默认用模板"></label>
      <label>验证集
        <select id="tf-val">
          <option value="0" ${!s.val_ratio ? "selected" : ""}>不分</option>
          <option value="0.1" ${s.val_ratio === 0.1 ? "selected" : ""}>10%</option>
          <option value="0.2" ${s.val_ratio === 0.2 ? "selected" : ""}>20%</option>
        </select>
      </label>
      <label>exp 名（可选覆盖） <input id="tf-exp" type="text" value="" placeholder="${r ? esc(r.exp) : ""}"></label>
      <button id="btn-train-save-cfg" class="btn">保存配置</button>
    `;
    const saveBtn = $("#btn-train-save-cfg");
    if (saveBtn) saveBtn.addEventListener("click", saveTrainConfig);

    if (!r) { infBox.innerHTML = '<div class="muted pad">先选择角色</div>'; return; }
    const refs = (r.dataset && r.dataset.clips) || [];
    const trained = !!(r.weights && r.weights.gpt && r.weights.sovits);
    infBox.innerHTML = `
      <label>参考片段
        <select id="tf-ref">${refs.length ? refs.map((c, i) => `<option value="${i}">${esc(c.wav)}</option>`).join("") : '<option value="">（无导出片段）</option>'}</select>
      </label>
      <label>合成文本 <textarea id="tf-text" placeholder="输入要合成的文本…"></textarea></label>
      <button id="btn-train-infer" class="btn primary" ${trained ? "" : "disabled"}>合成试听</button>
      <audio id="train-infer-audio" class="train-infer-audio" controls hidden></audio>
      <div class="infer-ref">${trained ? "" : "需要先训练出 GPT + SoVITS 权重才能试听"}</div>
    `;
    const inferBtn = $("#btn-train-infer");
    if (inferBtn) inferBtn.addEventListener("click", doInfer);
  }

  async function saveTrainConfig() {
    const pick = (s, d) => { const el = $(s); return el ? el.value : d; };
    const s = {
      root: pick("#tf-root", "").trim() || undefined,
      python: pick("#tf-python", "").trim() || undefined,
      api_port: Number(pick("#tf-port", "9880")) || 9880,
      language: pick("#tf-language", "ja"),
      epochs_s1: pick("#tf-epochs-s1", "") ? Number(pick("#tf-epochs-s1")) : null,
      epochs_s2: pick("#tf-epochs-s2", "") ? Number(pick("#tf-epochs-s2")) : null,
      val_ratio: Number(pick("#tf-val", "0")) || 0,
    };
    try {
      const j = await api("/api/training/config", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings: s }) });
      train.settings = j.settings;
      toast("配置已保存");
    } catch (e) { toast("保存配置失败: " + e.message, 6000); }
  }

  async function doInfer() {
    const r = trainRoleSel();
    if (!r) return;
    const textEl = $("#tf-text");
    const text = textEl ? textEl.value.trim() : "";
    if (!text) return toast("请输入合成文本");
    const refs = (r.dataset && r.dataset.clips) || [];
    const refIdx = Number($("#tf-ref") ? $("#tf-ref").value : 0) || 0;
    const refClip = refs[refIdx] || null;
    const btn = $("#btn-train-infer");
    if (btn) { btn.disabled = true; btn.textContent = "合成中…（首次加载模型较慢）"; }
    try {
      const j = await api(`/api/training/roles/${r.id}/infer`, { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: state.currentProject ? state.currentProject.id : null,
          exp_name: r.exp,
          text,
          ref_name: refClip ? refClip.wav : null,
          language: $("#tf-language") ? $("#tf-language").value : "ja",
        }) });
      const audio = $("#train-infer-audio");
      if (audio) { audio.src = j.audio_url; audio.hidden = false; audio.play().catch(() => {}); }
      toast("合成完成");
    } catch (e) { toast("合成失败: " + e.message, 8000); }
    if (btn) { btn.disabled = false; btn.textContent = "合成试听"; }
  }

  return { loadTraining, startTrain, doInfer, train };
}
