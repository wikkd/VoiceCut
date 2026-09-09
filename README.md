# VoiceCut — GPT-SoVITS 训练集制作工具

面向 **GPT-SoVITS 日文训练**的音频采集 / 清洗 / 精切 / 转写 / 导出工具。
素材来源：本地视频音频文件、B 站直链流（免登录 480p 代理播放）、动漫 / 歌曲 / 广播剧。

## 核心工作流

```
素材(动漫/歌曲/广播剧)
  → ① 导入: 本地拖拽 / B站直链代理（后台自动生成字幕 + 识别说话人）
  → ② 清洗: 人声分离(demucs htdemucs) / 保守降噪(noisereduce) / 去头尾静音+响度标准化
  → ③ 精切: 波形+视频同步, 选区→片段列表, 可自动按静音切分
  → ④ 转写: faster-whisper 批量转写(JP), 文本人工校对
  → ⑤ 校验: 时长1~15s / 静音率 / 爆音 / 空文本 → 标红
  → ⑥ 导出: GPT-SoVITS 标准目录 (001.wav + 001.txt + list.txt, 32kHz 单声道, 语言 JP, 多线程并行)
```

## 导入后后台自动分析

项目默认开启「⚡ 自动分析」：导入素材后后端自动在后台完成
1. **字幕加载**：优先提取视频内嵌字幕；没有则用 Whisper 自动生成；
2. **人物识别**：项目级说话人识别（ECAPA 声纹联合聚类，跨素材同一角色保持同一标签），
   自动创建 / 并入角色池并绑定片段。

工具栏与素材库页的「⚡ 自动分析」按钮可随时开关（按项目记忆）。同一项目同时只跑一轮分析，
批量导入时后完成的素材会在本轮结束后自动再排一轮；刷新页面后会自动重新挂接仍在运行的后台任务。

## 快速开始

```powershell
# 1. 创建虚拟环境（指向 D 盘 Python 3.12.14）
uv venv .venv --python "D:\uv-python\cpython-3.12.14-windows-x86_64-none\python.exe"

# 2. 安装依赖（demucs 含 GPU torch，体积大，耗时数分钟）
uv pip install --python .venv\Scripts\python.exe -r requirements.txt

# 3. 环境冒烟测试（校验 ffmpeg / CUDA / demucs / whisper / 音频管线）
.\.venv\Scripts\python.exe scripts\smoke_test.py

# 4. 启动
.\.venv\Scripts\python.exe voicecut.py
```

## 技术栈

| 层 | 方案 |
|---|---|
| GUI | 纯浏览器 (HTML + wavesurfer.js v7, 本地 vendor, 离线可用) |
| 后端 | Flask (Python 3.12.14) |
| 解码/导出 | FFmpeg 7.1 (D:\ffmpeg, 自动探测) |
| 降噪 | noisereduce |
| 人声分离 | demucs htdemucs (GPU: RTX 5060 Ti, Blackwell sm_120) |
| 转写 | faster-whisper (默认 medium, 可选 large-v3) |
| B站流 | yt-dlp 解析 + 后端 Range 代理 |

## 文档

- `docs/DESIGN.md` — 设计文档（界面布局 / 工作流 / 数据流）
- `docs/DEVELOPMENT.md` — 开发文档（架构 / 模块职责 / API / 快捷键）
- `docs/TESTING.md` — 测试文档（环境 / 单元 / 回归）

## 注意：GPT-SoVITS 词典

仓库内的 GPT-SoVITS/ 只跟踪源码，**日语/英语词典与缓存已从版本库移除**
（	ext/ja_userdic/、cmudict*.rep、ngdict*、g2pw/polyphonic*、

amedict_cache.pickle）。首次在新克隆上运行 GPT-SoVITS 文本处理前，
请运行其下载/生成脚本补全词典（本地 junction 目录不受影响）。

## 当前阶段状态

- [x] 01 阶段：git 环境 / 项目结构 / 开发文档
- [x] 02 阶段：环境安装 + 冒烟测试（6/6 冒烟 + 16/16 单测全过）
- [x] 03 阶段：核心管线（导入 / 波形 / 选区 / 播放 / 导出）
- [x] 04 阶段：清洗（降噪 / 分离 / 去静音）
- [x] 05 阶段：片段列表 + ASR 转写 + 训练集导出
- [x] 06 阶段：B 站直链代理（方案A，免登录 480p）

后端 E2E 15 项 + 前端浏览器渲染/选区/播放验证均通过；真实 B 站链接与手感回归待用户实测。
