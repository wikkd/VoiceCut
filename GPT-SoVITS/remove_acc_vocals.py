import numpy as np
import librosa
import soundfile as sf
from scipy.signal import stft, istft
from scipy.ndimage import uniform_filter1d

print("=" * 60)
print("Phase 1: 分析伴奏中残留的人声特征")
print("=" * 60)

# 加载伴奏（demucs 分离的 no_vocals）
print("\nLoading accompaniment (no_vocals)...")
accompaniment, sr = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/separated_0908/htdemucs/input_0908/no_vocals.wav", sr=44100, mono=False)
if accompaniment.ndim == 1:
    accompaniment = np.stack([accompaniment, accompaniment], axis=0)

# 加载需要处理的音频
print("Loading sample_feature_separated.wav...")
target, _ = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/sample_feature_separated.wav", sr=44100, mono=False)
if target.ndim == 1:
    target = np.stack([target, target], axis=0)

# 加载干净人声作为参考
print("Loading clean vocals reference...")
vocals_clean, _ = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/separated_0908/htdemucs/input_0908/vocals.wav", sr=44100, mono=False)
if vocals_clean.ndim == 1:
    vocals_clean = np.stack([vocals_clean, vocals_clean], axis=0)

min_len = min(accompaniment.shape[1], target.shape[1], vocals_clean.shape[1])
accompaniment = accompaniment[:, :min_len]
target = target[:, :min_len]
vocals_clean = vocals_clean[:, :min_len]
print(f"Length: {min_len/sr:.1f}s")

# ===== 分析伴奏中的人声残留 =====
print("\n--- 伴奏频谱分析 ---")
nperseg = 4096
noverlap = 3072

acc_mono = np.mean(accompaniment, axis=0)
voc_mono = np.mean(vocals_clean, axis=0)
target_mono = np.mean(target, axis=0)

# 伴奏的频谱
f, t, Zxx_acc = stft(acc_mono, fs=sr, nperseg=nperseg, noverlap=noverlap)
mag_acc = np.abs(Zxx_acc)

# 干净人声的频谱
_, _, Zxx_voc = stft(voc_mono, fs=sr, nperseg=nperseg, noverlap=noverlap)
mag_voc = np.abs(Zxx_voc)

# 伴奏中人声残留的频谱特征 = 伴奏频谱 - 伴奏中非人声部分
# 用干净人声的频谱包络作为人声模板
voc_envelope = np.mean(mag_voc, axis=1)  # 人声平均频谱包络
acc_envelope = np.mean(mag_acc, axis=1)  # 伴奏平均频谱包络

# 伴奏中人声残留 = 伴奏频谱中与人声模板相似的成分
# 归一化
voc_env_norm = voc_envelope / (np.max(voc_envelope) + 1e-8)
acc_env_norm = acc_envelope / (np.max(acc_envelope) + 1e-8)

# 残留人声 = 伴奏中与人声频谱形状匹配的部分
residual_vocal_ratio = voc_env_norm / (acc_env_norm + 1e-8)
residual_vocal_ratio = np.clip(residual_vocal_ratio, 0, 2)

print(f"Accompaniment vocal residual ratio (mean): {np.mean(residual_vocal_ratio):.3f}")
print(f"Accompaniment vocal residual ratio (max): {np.max(residual_vocal_ratio):.3f}")

# ===== 分析伴奏中残留人声的 F0 =====
print("\n--- 伴奏残留人声 F0 分析 ---")
f0_acc, voiced_acc, _ = librosa.pyin(acc_mono, fmin=80, fmax=600, sr=sr)
f0_acc_valid = f0_acc[~np.isnan(f0_acc)]
print(f"Accompaniment F0 range: {np.min(f0_acc_valid):.0f} - {np.max(f0_acc_valid):.0f} Hz")
print(f"Accompaniment F0 mean: {np.mean(f0_acc_valid):.0f} Hz")

f0_voc, _, _ = librosa.pyin(voc_mono, fmin=80, fmax=600, sr=sr)
f0_voc_valid = f0_voc[~np.isnan(f0_voc)]
print(f"Vocals F0 range: {np.min(f0_voc_valid):.0f} - {np.max(f0_voc_valid):.0f} Hz")
print(f"Vocals F0 mean: {np.mean(f0_voc_valid):.0f} Hz")

# ===== 伴奏的谐波/打击乐比例 =====
harmonic_acc, percussive_acc = librosa.effects.hpss(acc_mono)
harmonic_ratio_acc = np.sum(harmonic_acc**2) / (np.sum(harmonic_acc**2) + np.sum(percussive_acc**2))
print(f"\nAccompaniment harmonic ratio: {harmonic_ratio_acc:.1%}")

harmonic_voc, percussive_voc = librosa.effects.hpss(voc_mono)
harmonic_ratio_voc = np.sum(harmonic_voc**2) / (np.sum(harmonic_voc**2) + np.sum(percussive_voc**2))
print(f"Vocals harmonic ratio: {harmonic_ratio_voc:.1%}")

print("\n" + "=" * 60)
print("Phase 2: 从目标音频中去除伴奏残留人声")
print("=" * 60)

# ===== 策略：从 target 中减去伴奏中的人声成分 =====
# 1. 找到伴奏和 target 之间的延迟
from scipy.signal import fftconvolve

def find_shift(a, b):
    min_l = min(len(a), len(b))
    corr = fftconvolve(a[:min_l], b[:min_l][::-1], mode='full')
    return np.argmax(np.abs(corr)) - min_l + 1

shift = find_shift(target_mono, acc_mono)
print(f"Target-Accompaniment shift: {shift} samples ({shift/sr*1000:.1f}ms)")

# 对齐伴奏
if shift > 0:
    acc_aligned = np.pad(accompaniment, ((0,0),(shift,0)))[:, :min_len]
elif shift < 0:
    acc_aligned = accompaniment[:, -shift:][:, :min_len]
else:
    acc_aligned = accompaniment.copy()

if acc_aligned.shape[1] < min_len:
    acc_aligned = np.pad(acc_aligned, ((0,0),(0, min_len - acc_aligned.shape[1])))

# ===== 频谱减法：基于伴奏人声特征掩码 =====
result = np.zeros_like(target)

for ch in range(target.shape[0]):
    print(f"\nChannel {ch+1}:")
    
    # STFT
    f, t, Zxx_tgt = stft(target[ch], fs=sr, nperseg=nperseg, noverlap=noverlap)
    mag_tgt = np.abs(Zxx_tgt)
    phase_tgt = np.angle(Zxx_tgt)
    
    _, _, Zxx_acc_ch = stft(acc_aligned[ch], fs=sr, nperseg=nperseg, noverlap=noverlap)
    mag_acc_ch = np.abs(Zxx_acc_ch)
    
    _, _, Zxx_voc_ch = stft(vocals_clean[ch], fs=sr, nperseg=nperseg, noverlap=noverlap)
    mag_voc_ch = np.abs(Zxx_voc_ch)
    
    min_frames = min(mag_tgt.shape[1], mag_acc_ch.shape[1], mag_voc_ch.shape[1])
    mag_tgt = mag_tgt[:, :min_frames]
    phase_tgt = phase_tgt[:, :min_frames]
    mag_acc_ch = mag_acc_ch[:, :min_frames]
    mag_voc_ch = mag_voc_ch[:, :min_frames]
    
    # 构建"伴奏中人声残留"的频谱模板
    # = 伴奏频谱 × 人声/伴奏频谱比
    voc_acc_ratio = mag_voc_ch / (mag_acc_ch + 1e-8)
    voc_acc_ratio = np.clip(voc_acc_ratio, 0, 3)
    
    # 伴奏中的人声残留估计
    acc_vocal_residual = mag_acc_ch * voc_acc_ratio
    
    # 平滑
    acc_vocal_residual = uniform_filter1d(acc_vocal_residual, size=3, axis=1)
    
    # 自适应减法：在伴奏人声残留强的地方减得多
    # 计算残留强度
    residual_strength = acc_vocal_residual / (mag_tgt + 1e-8)
    residual_strength = np.clip(residual_strength, 0, 1.5)
    
    # 减法系数：根据残留强度自适应
    alpha = 0.8 + residual_strength * 0.7  # 0.8 ~ 1.5
    
    # 从目标中减去伴奏人声残留
    mag_clean = mag_tgt - alpha * acc_vocal_residual
    mag_clean = np.maximum(mag_clean, 0.05 * mag_tgt)  # spectral floor
    
    # 重建
    Zxx_clean = mag_clean * np.exp(1j * phase_tgt)
    _, clean = istft(Zxx_clean, fs=sr, nperseg=nperseg, noverlap=noverlap)
    result[ch] = clean[:min_len]

# 归一化
max_val = np.abs(result).max()
if max_val > 0:
    result = result / max_val * 0.90

output_path = "D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/sample_no_accompaniment_vocals.wav"
sf.write(output_path, result.T, sr, subtype='PCM_16')
print(f"\nSaved: {output_path}")
print(f"Duration: {min_len/sr:.1f}s")
