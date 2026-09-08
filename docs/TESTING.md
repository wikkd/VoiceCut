# VoiceCut 测试文档

## 1. 环境冒烟测试（scripts/smoke_test.py）

独立于 pytest，验证"环境 + 核心管线"，共 6 阶段：

| 阶段 | 验证内容 | 首次耗时 | 说明 |
|---|---|---|---|
| 1 | 全部依赖可导入 | 秒级 | flask/numpy/scipy/soundfile/noisereduce/yt_dlp/faster_whisper/torch/torchaudio/demucs |
| 2 | CUDA / GPU | 秒级 | 校验 RTX 5060 Ti (Blackwell sm_120)，实际跑 matmul |
| 3 | ffmpeg 管线 | 秒级 | 合成测试视频 → 抽音频 → 波形峰值 → 导出选区 → 质量指标 → 去静音 |
| 4 | noisereduce 降噪 | 数秒 | 正弦+白噪声 → 降噪后 RMS 显著下降 |
| 5 | demucs GPU 分离 | 首次~3min | 首次自动下载 htdemucs (~80MB 缓存)，之后秒级 |
| 6 | faster-whisper 转写 | 首次~1min | 用 tiny 验证转写管线 |

用法：
```powershell
.\.venv\Scripts\python.exe scripts\smoke_test.py            # 全部
.\.venv\Scripts\python.exe scripts\smoke_test.py --stage 5  # 单阶段
```

## 2. 单元测试（pytest）

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -v
```

| 文件 | 覆盖 |
|---|---|
| tests/test_env.py | 依赖导入 / ffmpeg 探测 / CUDA Blackwell |
| tests/test_ffmpeg_util.py | probe / 时长 / 抽流 / 导出(WAV+MP3) / 无效选区 / 去静音 / 预览转封装 |
| tests/test_audio_ops.py | 读 wav / 波形峰值 / 质量指标 / 响度标准化 / 数据集片段校验 |

## 3. 手动回归清单（阶段 03+ 逐项执行）

- [ ] 导入 MP4/MKV/AVI/MOV/FLV 视频 → 波形出现、视频预览可播
- [ ] 导入 WAV/MP3/FLAC/AAC/OGG 音频 → 波形出现
- [ ] 拖拽导入 / Ctrl+O / 文件选择器三种途径
- [ ] 波形缩放、选区拖拽、选区手柄、总览条导航
- [ ] 空格播放/暂停、L 循环选区、←→ 微调边界
- [ ] 视频预览窗与波形/播放头/选区联动
- [ ] 导出 WAV(32k/44.1k/48k) 与 MP3，命名 `原文件名_开始-结束.ext`
- [ ] 降噪后试听对比、产物进素材列表
- [ ] 分离人声/伴奏、产物进素材列表
- [ ] 片段列表：加片段/试听/编辑文本/删除/跳转/自动切分
- [ ] 批量转写(JP) → 文本可校对 → 空文本标红
- [ ] 导出训练集目录结构 + list.txt 格式校验
- [ ] B 站链接打开 → 页面播放 → 边看边缓存 → 可剪辑

## 4. 阶段 02 测试记录（2026-09-08）

| 项 | 结果 | 备注 |
|---|---|---|
| 环境 | ✅ | D 盘 Python 3.12.14 + uv venv；FFmpeg 7.1 (D:\ffmpeg，无 ffprobe，走 stderr 回退) |
| 依赖安装 | ✅ | flask 3.1.3 / numpy 2.5.3 / scipy 1.18.1 / soundfile 0.14.0 / noisereduce 3.0.3 / demucs 4.1.0 / yt-dlp 2026.8.19 / faster-whisper 1.2.1 |
| GPU torch | ✅ | torch 2.11.0+cu128 (CUDA 12.8)，RTX 5060 Ti capability (12,0)=sm_120，matmul 实跑通过 |
| 冒烟测试 | ✅ | 6/6 阶段 PASS（含 demucs GPU 分离、whisper 转写） |
| 单元测试 | ✅ | 16/16 PASS |
| 服务启动 | ✅ | /api/health /api/config /api/items / 静态页 全部 200 |
| htdemucs 模型 | ✅ | 已缓存 ~/.cache/torch/hub/checkpoints (80MB) |
| whisper 模型 | ✅ | tiny + **medium**（默认档，~1.5GB）均已预下载；large-v3 按需下载 |
| 网络适配 | ✅ | huggingface.co 不可达 → 自动回退 hf-mirror.com + 禁用 Xet；torch/lib 加入 PATH 解决 cublas64_12.dll |

### 已知环境要点

- `D:\ffmpeg` 只有 ffmpeg.exe（无 ffprobe）→ `probe()` 已实现 stderr 回退，无需补装
- huggingface.co 在本机网络超时 → `app/transcribe.py` 已内置镜像回退（`VC_HF_ENDPOINT` 可覆盖）
- faster-whisper GPU 依赖 cublas → `app/transcribe.py` 启动时自动把 `torch/lib` 加入 PATH
- whisper **medium**（默认档，~1.5GB）已预下载并 GPU 实跑通过；large-v3 按需下载

## 5. 阶段 03~06 功能验证记录（2026-09-08）

**后端 E2E（scripts/e2e_test.py，对运行中的服务全管线）**
- ✅ 导入（multipart → 后台任务 → 素材注册）  ✅ peaks / audio-Range(206) / video-Range(206)
- ✅ 导出 wav + mp3（命名 `原文件名_MM-SS.s-MM-SS.s.ext`，重名自动加序号，/api/files 下载）
- ✅ trim / denoise 后台任务产出新素材
- ✅ 数据集导出：001.wav + 001.txt + list.txt（UTF-8，`路径|speaker|JP|文本`）；空文本片段正确跳过
- ✅ 转写（medium，返回文本数组）

**前端浏览器验证（scripts/browser_test.js，CDP 无头 Chrome）**
- ✅ 页面 boot（data-vc=ok）、后端连通
- ✅ wavesurfer v7.12.11 ESM 渲染：波形/进度/时间轴/总览条 **4 个 canvas 位于 shadow DOM**（用 `document.querySelector('canvas')` 查不到，须穿透 shadowRoot）
- ✅ 程序化选区 region → selection 状态同步；播放 1s 无错误；视频预览窗显示

**关键实现要点 / 坑**
- wavesurfer 必须用 **ESM 构建**（`wavesurfer.esm.js` + `plugins/*.esm.js`）；官方同时发布的 `*.min.js` 是 **UMD 且渲染失效**（decode 成功但 peaks 为空、不出 canvas），勿回退。
- 预计算 peaks 需传**平铺单声道数组**（`[v0,v1,...]`），min/max 对格式会被误当作 2 样本声道；后端 `compute_peaks` 仍返回 min/max 对，前端转换。
- 本机 `D:\ffmpeg` 无 ffprobe → probe 走 stderr 回退（已在阶段02固化）。

**待人工回归（真实场景）**
- 真实 B 站链接打开（yt-dlp 解析 + 代理播放 + 音频后台获取）
- 真实拖拽导入 / 鼠标拖选选区 / 手柄微调 / 快捷键手感
- 长视频（1h+）波形加载与拖动 seek
- 波形区域内右键取消选区/取消拖拽，且不弹浏览器菜单
