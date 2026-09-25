# VoiceCut → Rust 迁移重构：分析报告与分阶段计划

> 2026-09-25 · 基于实测数据（非拍脑袋）。结论先行：**全量 Rust 化不成立，也不必要**——本项目的重负载早已在原生层
> （ffmpeg / CTranslate2 / torch / numpy），纯 Python 热循环实测仅 7~125ms。真正的性能瓶颈在
> ①识别数据棘轮膨胀 ②前端 DOM 全量渲染 ③ffmpeg 进程编排。Rust 只在少数"逐采样点计算"场景有真实收益。

## 一、现有代码库热点分析（实测）

| 模块 | 实现 | 实测耗时 | Rust 可替代？ | 结论 |
|---|---|---|---|---|
| `audio_ops.compute_peaks` | soundfile 分块读 + numpy min/max | 168MB wav **125ms** | ✅ memmap2+rayon 预计 ~30ms | **试点候选**（收益真实但小） |
| `audio_ops.audio_metrics` | numpy 全量统计 | 5s 片段 **7ms**；千段导出约 1s | ✅ 但收益微小 | 可与 peaks 合并进同一 crate |
| 转写 `transcribe` | faster-whisper / **CTranslate2(C++)** | 分钟级（GPU） | ❌ 已是原生 | 不迁 |
| 说话人 `speakers` | speechbrain / **torch CUDA** | 分钟级（GPU） | ❌ 已是原生 | 不迁 |
| 分离 `separate` | **demucs / torch** | 分钟级（GPU） | ❌ 已是原生 | 不迁 |
| 抽音频/预览/去静音/切分 | **ffmpeg 子进程** | 子进程主导（50ms+ 启动×N） | ⚠️ Rust 只能省编排开销 | 不迁，改为**减少调用次数** |
| 数据集导出 `dataset` | 线程池 + 每片段 1~3 次 ffmpeg | 每片段 ~200ms | ⚠️ 同上 | 编排层保持 Python |
| 流式服务 `streaming` | Flask sendfile | IO 主导 | ❌ | 不迁 |
| 前端渲染 | 原生 JS + wavesurfer | 卡顿主源（5830 行 DOM） | ❌ Rust/WASM 不解决 DOM | 用虚拟滚动解决 |
| 识别管线数据棘轮 | `bind_segments` 每轮重切 | 199→2040 段 | ❌ 语义 bug，非性能 | **优先修这个** |

**工具链现状**：本机无 cargo/rustc；uv 管理的 .venv。引入 Rust 需安装工具链 + maturin（PyO3 构建后端）。

## 二、分阶段计划

### Phase 0（已完成，本次会话）
- 基线测量：peaks 125ms/168MB、metrics 7ms/片段、进程 CPU、前端 61fps 探针（`scripts/perf_probe.js`）。
- 卡顿根因定位：识别数据棘轮 + 5830 行表格全量重建。

### Phase 1（必做，与 Rust 无关的真瓶颈）
1. **识别管线防棘轮**：`bind_segments` 写回前先规范化去重（合并高重叠段）；mixed 只在首次识别时切分，重跑仅重绑不重切。
2. **存量数据清洗**：对 2040 段去重合并（先备份 `workdir/voicecut.db`，列影响行数后执行）。
3. **片段表虚拟滚动 / 增量渲染**：`renderSegments` 只渲染可视区 ± 缓冲行。
- 验收：browser_test 12 段全绿；5830 行场景交互 <50ms；段数不再随分析轮次增长。

### Phase 2（Rust 试点：`vc-audio` crate）
- 范围：`compute_peaks` + `audio_metrics` + `read_wav` 统计部分，合并为一个 PyO3 扩展模块 `vc_audio`。
- 接口兼容：**函数签名与返回值与 `app/audio_ops.py` 完全一致**（peaks: `list[[min,max]]`；metrics 同 dict）。
  Python 侧保留同名纯 Python 实现作为回退（import 失败自动降级），`audio_ops.py` 内按 `VC_AUDIO_DISABLE=1` 可切换。
- 前置：安装 rustup + maturin；`uv pip install maturin`；CI 无 Rust 时跳过构建走回退。
- 预期：peaks 125→~30ms（4×）；metrics 千段 1s→~0.2s。**收益封顶就在这个量级**，不扩大范围。

### Phase 3（可选，按 Phase 2 实测收益决定是否继续）
- 若 Phase 2 实测收益 <30% 或构建链复杂度不划算 → 停止扩张，冻结 crate。
- 若收益显著 → 评估把「自动切分静音扫描」（现为 ffmpeg silencedetect）改为 Rust 内嵌扫描，省一次子进程。

## 三、性能对比评估方法
- 统一基线脚本（pytest-benchmark 或 `timeit` 固定夹具）：168MB wav peaks、5s clip ×1000 metrics、
  12 集项目全量识别、5830 段渲染帧率（perf_probe）。
- 每阶段出对照表：迁移前 / 迁移后 / 回退路径开关（`VC_AUDIO_DISABLE`）三列。

## 四、风险与回归重点
| 风险 | 缓解 |
|---|---|
| PyO3 构建链引入，他人接手成本上升 | 回退实现永远保留；构建失败=自动纯 Python；README 写明 |
| peaks/metrics 浮点结果与 numpy 不一致（NaN/极值/声道下混顺序） | 差分测试：同一批文件两实现输出逐值比对（容差 1e-6）；回归夹具固化 3 个真实 wav |
| Windows 工具链/编码坑（本机已有多起先例） | maturin develop 全程 ASCII 路径；CI 优先 |
| 识别去重改坏了正确片段 | 清洗前自动备份；清洗规则只合并「起点差 <0.05s 或互相包含 >90%」的段；人工抽查 20 段 |
| 虚拟滚动破坏试听高亮/多选/右键 | browser_test 的 FEAT/PROJ/PERSIST 段 + 新增高亮滚动断言 |

**回归测试关键环节**：①导入→peaks/时长入库 ②自动分析→角色池绑定数不变 ③数据集导出 list.txt 逐字节一致
④141 项 pytest 全绿 ⑤browser_test 12 段 ⑥撤销/重做 ⑦大表格滚动下的试听跳转高亮。
