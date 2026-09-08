# VoiceCut 开发文档

## 1. 技术栈

| 层 | 方案 | 备注 |
|---|---|---|
| GUI | 纯浏览器 HTML + wavesurfer.js v7 | 本地 vendor，离线可用 |
| 后端 | Flask (Python 3.12.14) | 127.0.0.1:8765 |
| 解码/导出 | FFmpeg 7.1 (subprocess) | 自动探测：FFMPEG_PATH → PATH → D:\ffmpeg |
| 音频 I/O | soundfile + numpy | 波形 / 指标 |
| 降噪 | noisereduce | 保守参数 |
| 人声分离 | demucs htdemucs (GPU torch) | RTX 5060 Ti (Blackwell sm_120) |
| ASR | faster-whisper | 默认 medium，可切 large-v3 |
| B 站流 | yt-dlp 解析 + 后端 Range 代理 | 方案 A：免登录 480p 单文件 MP4 |

## 2. 目录结构

```
VoiceCut/
├── voicecut.py               # 启动入口 (argparse + Flask + 自动开浏览器)
├── requirements.txt
├── README.md
├── app/
│   ├── __init__.py
│   ├── config.py             # AppConfig: 路径 / ffmpeg 探测 / 采样率常量
│   ├── ffmpeg_util.py        # ffmpeg/ffprobe 封装: probe / extract / export / trim / remux
│   ├── audio_ops.py          # 音频分析: 波形峰值 / 质量指标 / 响度标准化 / 数据集校验
│   ├── media_store.py        # MediaStore: 内存素材注册表 (线程安全)
│   ├── tasks.py              # TaskManager: 后台任务 + 进度轮询
│   ├── denoise.py            # noisereduce 封装 (保守降噪)
│   ├── separate.py           # demucs CLI 封装 (two-stems=vocals + 进度解析)
│   ├── transcribe.py         # faster-whisper 封装 (日语, 批量转写, 模型缓存)
│   ├── dataset.py            # GPT-SoVITS 数据集导出 (001.wav+001.txt+list.txt)
│   ├── bilibili.py           # yt-dlp 解析 + 直链代理 (方案A)
│   ├── server.py             # Flask 路由 (create_app 工厂)
│   └── static/               # 前端 (index.html / css / js / vendor)
├── tests/                    # pytest 单元测试
├── scripts/
│   └── smoke_test.py         # 环境+管线冒烟测试 (6 阶段)
└── docs/
    ├── DESIGN.md
    ├── DEVELOPMENT.md        # 本文档
    └── TESTING.md
```

## 3. 模块职责与依赖方向

```
server.py ──► config / media_store / tasks / ffmpeg_util / 各处理模块
ffmpeg_util.py        (不依赖其他 app 模块)
audio_ops.py  ──►     ffmpeg_util (读 wav / 归一)
denoise.py    ──►     audio_ops (读 wav)
separate.py   ──►     tasks (进度上报)
transcribe.py ──►     tasks (进度上报)
dataset.py    ──►     ffmpeg_util + audio_ops (切分/去静音/校验)
bilibili.py   ──►     (yt-dlp / 网络代理)
```

- 处理模块均接收 **WAV 路径**作为输入，产出新 WAV / 新素材条目
- 长操作一律经 `TaskManager.submit()` 后台执行，前端轮询 `/api/tasks/<id>`
- 前端只与 `server.py` 的 REST API 交互

## 4. API 约定（REST，JSON）

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/health` | 健康检查 | ✅ |
| GET | `/api/config` | ffmpeg 路径 / 采样率等 | ✅ |
| GET | `/api/items` | 素材列表 | ✅（骨架） |
| GET | `/api/tasks` / `/api/tasks/<id>` | 任务列表 / 进度 | ✅（骨架） |
| POST | `/api/import` | 上传文件（multipart），返回素材 | ⏳ 阶段03 |
| GET | `/api/audio/<id>` | WAV Range 流（播放/seek） | ⏳ 阶段03 |
| GET | `/api/peaks/<id>` | 波形峰值 JSON | ⏳ 阶段03 |
| GET | `/api/video/<id>` | 预览 MP4 Range 流 | ⏳ 阶段03 |
| POST | `/api/export` | 导出选区 {start,end,format,sr} | ⏳ 阶段03 |
| POST | `/api/denoise` | 降噪（任务） | ⏳ 阶段04 |
| POST | `/api/separate` | 人声分离（任务） | ⏳ 阶段04 |
| POST | `/api/transcribe` | 批量转写（任务） | ⏳ 阶段05 |
| POST | `/api/dataset/export` | 导出训练集 | ⏳ 阶段05 |
| POST | `/api/bilibili/open` | B站解析+代理 | ⏳ 阶段06 |
| GET | `/api/bilibili/proxy/<job>` | 直链 Range 代理流 | ⏳ 阶段06 |

命名约定：导出文件 `原文件名_开始-结束.wav`（如 `anime_ep1_00-03.2-00-08.5.wav`）。

## 5. 环境准备

```powershell
uv venv .venv --python "D:\uv-python\cpython-3.12.14-windows-x86_64-none\python.exe"
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

- 依赖经 `requirements.txt` 管理；demucs 自动拉取 Windows PyPI 默认 **GPU 版 torch**
- 首次运行 `smoke_test.py` 阶段 5/6 会下载模型（htdemucs ~300MB / whisper tiny ~75MB）
- whisper 模型缓存于 HF 默认目录；demucs 模型缓存于 torch hub 目录

## 6. 构建与测试命令

```powershell
# 冒烟测试（全部 6 阶段）
.\.venv\Scripts\python.exe scripts\smoke_test.py

# 单元测试
.\.venv\Scripts\python.exe -m pytest tests\ -v

# 启动
.\.venv\Scripts\python.exe voicecut.py --port 8765
```

## 7. 阶段规划

| 阶段 | 内容 | 状态 |
|---|---|---|
| 01 | git 环境 / 项目结构 / 开发文档 | ✅ |
| 02 | 环境安装 + 冒烟测试 | 进行中 |
| 03 | 核心管线：导入 / 波形 / 选区 / 播放 / 导出 | ⏳ |
| 04 | 清洗：降噪 / 分离 / 去静音 / 响度标准化 | ⏳ |
| 05 | 片段列表 + ASR 转写 + 训练集导出 | ⏳ |
| 06 | B 站直链代理 | ⏳ |

## 8. 开发约束

- 处理模块（非 server.py）**不 import flask**，保持可单测
- 长任务必须后台执行并上报进度，禁止阻塞 HTTP 请求线程
- 导出/写文件统一 utf-8；路径一律 `Path` 处理
- 训练集导出遵守 GPT-SoVITS 规范（32k 单声道 / 1~15s / list.txt 格式）
- 降噪保持保守默认，绝不默认过度处理
