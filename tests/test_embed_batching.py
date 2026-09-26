"""批量前向的等价性回归。

改造把「逐条 encode_batch」换成「按素材批量」（干净机器实测 20x），代价是批次内
归约顺序略变。这组用例把改造的**前提**锁死，避免以后悄悄退化：

1. 批量与逐条在**同一前向实现**下逐位等价（用桩模型排除随机性）；
2. 门限（过短 / 电平过低）在两条路径上判得完全一致；
3. 混长窗（尾窗短于 1s）能正确分组并回填顺序；
4. 不是本模块 ECAPA 实现时**绝不批量**（否则会打穿既有测试与 MFCC 降级）；
5. 批量整体失败时自动退回逐条；
6. Whisper 显式精度 + 批量管线默认关闭、且依赖逐段边界的路径永不批量。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app import speakers, transcribe

SR = 16000


def _tone(seconds: float, *, freq: float = 220.0, amp: float = 0.3) -> np.ndarray:
    n = int(SR * seconds)
    return (np.sin(2 * np.pi * freq * np.arange(n) / SR) * amp).astype(np.float32)


def _tone_wav(path: Path, *, seconds: float = 1.0, freq: float = 220.0) -> None:
    sf.write(str(path), _tone(seconds, freq=freq), SR)


class _StubModel:
    """按行独立计算的假 ECAPA，使「批量」与「逐条」的结果可逐位比较。

    ``fail_batch=True`` 时**只对 batch > 1 抛错**，用来验证「批量失败自动退回逐条」。
    """

    def __init__(self, *, fail_batch: bool = False) -> None:
        self.calls: list[int] = []
        self.fail_batch = fail_batch

    def encode_batch(self, x):
        import torch

        b = int(x.shape[0])
        self.calls.append(b)
        if self.fail_batch and b > 1:
            raise RuntimeError("simulated batched forward failure")
        x = x.float()
        return torch.stack([x.mean(dim=1), x.std(dim=1), x.amax(dim=1)], dim=1)


@pytest.fixture()
def stub_loader(monkeypatch):
    """返回一个工厂：装上桩模型并把它挂到 _load_embedder。"""

    def _mk(*, fail_batch: bool = False) -> _StubModel:
        m = _StubModel(fail_batch=fail_batch)
        monkeypatch.setattr(speakers, "_load_embedder", lambda: m)
        return m

    return _mk


# ── 1. 批量 == 逐条 ──────────────────────────────────────────

def test_batched_equals_single_for_equal_windows(stub_loader) -> None:
    m = stub_loader()
    mono = _tone(30.0)
    ranges = [(1.0 + i * 1.5, 2.4 + i * 1.5) for i in range(10)]   # 全 1.4s -> 中段均 1s
    batched = speakers.embed_ranges(speakers._ECAPA_EMBED_IMPL, mono, SR, ranges)
    batched_calls = list(m.calls)
    m.calls.clear()          # 后面的逐条基线也会打到同一个桩上，先隔离两次计数
    single = [speakers._ECAPA_EMBED_IMPL(mono, SR, a, b) for a, b in ranges]
    assert len(batched) == len(single) == 10
    for b, s in zip(batched, single, strict=True):
        assert b is not None and s is not None
        assert np.allclose(b, s, rtol=1e-9, atol=1e-9)
    # 真的批量了：10 条等长窗只发一次前向（逐条基线则是 10 次）
    assert batched_calls == [10]
    assert m.calls == [1] * 10


def test_batched_handles_mixed_window_lengths(stub_loader) -> None:
    m = stub_loader()
    mono = _tone(10.0)
    # 前三个是 1.4s 窗（中段截成 16000），最后一个是 0.6s 尾窗（<1s，原样 9600）
    ranges = [(0.0, 1.4), (1.5, 2.9), (3.0, 4.4), (9.4, 10.0)]
    batched = speakers.embed_ranges(speakers._ECAPA_EMBED_IMPL, mono, SR, ranges)
    batched_calls = list(m.calls)
    m.calls.clear()
    single = [speakers._ECAPA_EMBED_IMPL(mono, SR, a, b) for a, b in ranges]
    for b, s in zip(batched, single, strict=True):
        assert b is not None and s is not None
        assert np.allclose(b, s, rtol=1e-9, atol=1e-9)
    # 两种窗长 -> 分组后 2 次前向（3 条长窗 + 1 条短窗），且顺序不能混
    assert sorted(batched_calls) == [1, 3]


def test_gates_identical_between_paths(stub_loader) -> None:
    stub_loader()
    mono = np.zeros(SR * 5, dtype=np.float32)
    mono[: SR * 2] = _tone(2.0)          # 0~2s 有声，其余静音
    ranges = [
        (0.0, 1.4),      # 正常
        (2.0, 2.2),      # 仅 0.2s < 0.25s -> 无效
        (3.0, 4.4),      # 全静音 -> 电平门限挡掉
    ]
    batched = speakers.embed_ranges(speakers._ECAPA_EMBED_IMPL, mono, SR, ranges)
    single = [speakers._ECAPA_EMBED_IMPL(mono, SR, a, b) for a, b in ranges]
    assert [x is None for x in batched] == [x is None for x in single] == [False, True, True]


# ── 2. 不该批量的场景（反向保护）─────────────────────────────

def test_foreign_embed_fn_is_never_batched(monkeypatch) -> None:
    """非本模块 ECAPA 实现（测试替身 / MFCC）必须逐条走，且不得触达模型加载。"""
    calls: list = []

    def fake(mono, sr, a, b):
        calls.append((a, b))
        return np.array([1.0, 0.0])

    def _boom():
        raise AssertionError("不应调用 _load_embedder：非 ECAPA 实现必须逐条")

    monkeypatch.setattr(speakers, "_load_embedder", _boom)
    out = speakers.embed_ranges(fake, _tone(5.0), SR, [(0.0, 1.0), (1.0, 2.0)])
    assert len(calls) == 2 and len(out) == 2


def test_collect_source_still_per_window_for_patched_impl(monkeypatch) -> None:
    """既有测试的语义：monkeypatch 掉 _ecapa_embedding 后必须仍是逐窗调用。"""
    calls: list = []

    def fake(mono, sr, a, b):
        calls.append((a, b))
        return np.array([1.0, 0.0])

    monkeypatch.setattr(speakers, "_ecapa_embedding", fake)
    subs = [{"start": 0.0, "end": 1.0}]
    speakers._collect_source(fake, _tone(2.0), SR, subs, 0, 1)
    assert calls == list(speakers._window_ranges(0.0, 1.0))


def test_batched_forward_failure_falls_back_to_single(stub_loader) -> None:
    m = stub_loader(fail_batch=True)
    mono = _tone(10.0)
    ranges = [(0.0, 1.4), (1.5, 2.9), (3.0, 4.4)]
    out = speakers.embed_ranges(speakers._ECAPA_EMBED_IMPL, mono, SR, ranges)
    single = [speakers._ECAPA_EMBED_IMPL(mono, SR, a, b) for a, b in ranges]
    for o, s in zip(out, single, strict=True):
        assert o is not None and s is not None
        assert np.allclose(o, s, rtol=1e-9, atol=1e-9)
    assert 3 in m.calls              # 先试过批量
    assert m.calls.count(1) >= 3     # 再逐条补算


# ── 3. _collect_source 与逐条基线等价 ────────────────────────

def test_collect_source_matches_per_window_baseline(stub_loader) -> None:
    stub_loader()
    mono = _tone(5.0)
    subs = [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]
    acc, sub_embs = speakers._collect_source(speakers._ECAPA_EMBED_IMPL, mono, SR, subs, 0, 2)

    base_acc: dict = {}
    base_embs: list = [None] * len(subs)
    for i, s in enumerate(subs):
        vecs = []
        for ws, we in speakers._window_ranges(s["start"], s["end"]):
            e = speakers._ECAPA_EMBED_IMPL(mono, SR, ws, we)
            if e is not None:
                vecs.append((ws, we, e))
        if vecs:
            base_acc[i] = vecs
            base_embs[i] = speakers._aggregate_subtitle_embedding(vecs)

    assert set(acc) == set(base_acc)
    for i in acc:
        assert [v[:2] for v in acc[i]] == [v[:2] for v in base_acc[i]]   # 窗与顺序一致
        for a, b in zip(acc[i], base_acc[i], strict=True):
            assert np.allclose(a[2], b[2], rtol=1e-9, atol=1e-9)
    for a, b in zip(sub_embs, base_embs, strict=True):
        assert (a is None) == (b is None)
        if a is not None:
            assert np.allclose(a, b, rtol=1e-9, atol=1e-9)


# ── 4. rescan_assignments 真的批量了 ─────────────────────────

def test_rescan_batches_equal_length_segments(stub_loader, monkeypatch, tmp_path: Path) -> None:
    m = stub_loader()
    wav = tmp_path / "x.wav"
    _tone_wav(wav, seconds=1.0)
    monkeypatch.setattr(speakers, "read_mono16k", lambda p: (_tone(60.0), SR))
    chars = [{"id": "cA", "embedding": speakers.embedding_to_b64(np.array([1.0, 0.0, 0.0])),
              "emb_count": 3}]
    segs = [{"id": f"s{i}", "start": float(i), "end": float(i) + 0.9,
             "characterId": None} for i in range(12)]
    sources = [{"item_id": "it1", "wav_path": str(wav), "segments": segs}]
    speakers.rescan_assignments(sources, chars)
    # 12 个等长段 -> 一次前向；改造前这里是 12 次
    assert m.calls == [12]


# ── 5. read_mono16k 缓存 ─────────────────────────────────────

def test_read_mono16k_cache_hits_and_invalidates(monkeypatch, tmp_path: Path) -> None:
    from app import ffmpeg_util

    calls: list = []
    real = ffmpeg_util.run_ffmpeg

    def _counting(args):
        calls.append(list(args))
        return real(args)

    monkeypatch.setattr(ffmpeg_util, "run_ffmpeg", _counting)
    speakers.clear_mono_cache()
    try:
        wav = tmp_path / "c.wav"
        _tone_wav(wav, seconds=1.0)
        a1, _ = speakers.read_mono16k(wav)
        a2, _ = speakers.read_mono16k(wav)
        assert len(calls) == 1          # 第二次命中缓存，没再起 ffmpeg
        assert a1 is a2                 # 同一对象复用（零拷贝）
        assert a1.dtype == np.float32

        # 内容变化（时长不同 -> size 与 mtime 都变）-> 键失效，必须重新解码
        _tone_wav(wav, seconds=2.0, freq=330.0)
        a3, _ = speakers.read_mono16k(wav)
        assert len(calls) == 2
        assert a3.size != a1.size

        speakers.clear_mono_cache()
        speakers.read_mono16k(wav)
        assert len(calls) == 3
    finally:
        speakers.clear_mono_cache()


# ── 6. Whisper 显式精度 / 批量管线开关 ───────────────────────

def test_resolve_runtime_is_explicit(monkeypatch) -> None:
    monkeypatch.delenv("VC_WHISPER_DEVICE", raising=False)
    monkeypatch.delenv("VC_WHISPER_COMPUTE", raising=False)
    dev, ctype = transcribe._resolve_runtime()
    assert dev in ("cuda", "cpu")          # 绝不能是 "auto"
    assert ctype in ("float16", "int8")
    assert (ctype == "float16") == dev.startswith("cuda")

    monkeypatch.setenv("VC_WHISPER_DEVICE", "cpu")
    monkeypatch.setenv("VC_WHISPER_COMPUTE", "int8")
    assert transcribe._resolve_runtime() == ("cpu", "int8")


def test_batch_pipeline_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("VC_WHISPER_BATCH", raising=False)
    assert transcribe._batch_enabled() is False
    monkeypatch.setenv("VC_WHISPER_BATCH", "1")
    assert transcribe._batch_enabled() is True


class _RecordingWhisper:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    def transcribe(self, path, **kw):
        self.sink.append(("serial", kw))
        return iter(()), types.SimpleNamespace(duration=0.0)


def _install_fakes(monkeypatch, sink: list) -> None:
    monkeypatch.setattr(transcribe, "_load_model",
                        lambda model, task: _RecordingWhisper(sink))

    def _pipe(model, task):
        class _P:
            def transcribe(self, path, **kw):
                sink.append(("batched", kw))
                return iter(()), types.SimpleNamespace(duration=0.0)
        return _P()

    monkeypatch.setattr(transcribe, "_batched_pipeline", _pipe)


def test_whisper_call_serial_when_pipeline_disabled(monkeypatch) -> None:
    monkeypatch.delenv("VC_WHISPER_BATCH", raising=False)
    sink: list = []
    _install_fakes(monkeypatch, sink)
    transcribe._whisper_call("medium", "transcribe", "x.wav", language="ja",
                             word_timestamps=False, batched_ok=True)
    assert [k for k, _ in sink] == ["serial"]


def test_whisper_call_batched_only_when_allowed(monkeypatch) -> None:
    monkeypatch.setenv("VC_WHISPER_BATCH", "1")
    monkeypatch.setenv("VC_WHISPER_BATCH_SIZE", "5")
    sink: list = []
    _install_fakes(monkeypatch, sink)
    transcribe._whisper_call("medium", "transcribe", "x.wav", language="ja",
                             word_timestamps=False, batched_ok=True)
    assert [k for k, _ in sink] == ["batched"]
    assert sink[0][1]["batch_size"] == 5

    # 反向保护：不允许批量的路径（依赖逐段边界）即使开着开关也必须串行
    sink.clear()
    transcribe._whisper_call("medium", "transcribe", "x.wav", language="ja",
                             word_timestamps=False, batched_ok=False)
    assert [k for k, _ in sink] == ["serial"]


def test_transcribe_timed_never_uses_batched_pipeline(monkeypatch) -> None:
    """字幕面板依赖逐段 start/end，批量会把 15 段并成 2 段 —— 必须永不启用。"""
    monkeypatch.setenv("VC_WHISPER_BATCH", "1")
    sink: list = []
    _install_fakes(monkeypatch, sink)

    def _forbidden(model, task):
        raise AssertionError("transcribe_timed 不得走批量管线")

    monkeypatch.setattr(transcribe, "_batched_pipeline", _forbidden)
    out = transcribe.transcribe_timed("x.wav", model="medium")
    assert out == []
    assert [k for k, _ in sink] == ["serial"]


def test_load_model_caches_and_passes_explicit_runtime(monkeypatch) -> None:
    made: list = []
    fake_mod = types.ModuleType("faster_whisper")

    def _WhisperModel(name, device=None, compute_type=None):
        made.append((name, device, compute_type))
        return object()

    fake_mod.WhisperModel = _WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)
    saved = dict(transcribe._models)
    transcribe._models.clear()
    try:
        a = transcribe._load_model("medium", "transcribe")
        b = transcribe._load_model("medium", "transcribe")
        assert a is b and len(made) == 1        # 同配置只构造一次
        assert made[0][1] not in (None, "auto")
        assert made[0][2] not in (None, "auto")
    finally:
        transcribe._models.clear()
        transcribe._models.update(saved)
