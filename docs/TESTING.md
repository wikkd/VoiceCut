# VoiceCut 测试文档

## 1. 环境冒烟测试（scripts/smoke_test.py）

独立于 pytest，验证"环境 + 核心管线"，共 6 阶段：

| 阶段 | 验证内容 | 首次耗时 | 说明 |
|---|---|---|---|
| 1 | 全部依赖可导入 | 秒级 | flask/numpy/scipy/soundfile/noisereduce/yt_dlp/faster_whisper/torch/torchaudio/demucs |
| 2 | CUDA / GPU | 秒级 | 校验 RTX 5060 Ti (Blackwell)，实际跑 matmul |
| 3 | ffmpeg 管线 | 秒级 | 合成测试视频 → 抽音频 → 波形峰值 → 导出选区 → 质量指标 → 去静音 |
| 4 | noisereduce 降噪 | 数秒 | 正弦+白噪声 → 降噪后 RMS 显著下降 |
| 5 | demucs GPU 分离 | 首次~10min | 首次自动下载 htdemucs (~300MB)，之后秒级 |
| 6 | faster-whisper 转写 | 首次~2min | 用 tiny (~75MB) 验证转写管线 |

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

## 4. 当前阶段测试记录

（阶段 02 环境安装与冒烟测试结果，执行后在此填写。）
