import numpy as np
import librosa
import soundfile as sf
from scipy.signal import stft, istft

print("Loading mixed audio (original)...")
mixed, sr = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/input_0908.wav", sr=44100, mono=False)
if mixed.ndim == 1:
    mixed = np.stack([mixed, mixed], axis=0)

print("Loading reference BGM...")
bgm_ref, _ = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/bgm_reference.mp3", sr=44100, mono=False)
if bgm_ref.ndim == 1:
    bgm_ref = np.stack([bgm_ref, bgm_ref], axis=0)

print("Loading demucs vocals (best-effort vocal estimate)...")
vocals, _ = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/separated_0908/htdemucs/input_0908/vocals.wav", sr=44100, mono=False)
if vocals.ndim == 1:
    vocals = np.stack([vocals, vocals], axis=0)

target_len = mixed.shape[1]

# 对齐参考伴奏到混合音频
# 使用互相关找到最佳偏移
from scipy.signal import fftconvolve

def find_best_shift(mixed_ch, ref_ch):
    min_len = min(len(mixed_ch), len(ref_ch))
    m = mixed_ch[:min_len]
    r = ref_ch[:min_len]
    corr = fftconvolve(m, r[::-1], mode='full')
    shift = np.argmax(np.abs(corr)) - min_len + 1
    return shift

shift = find_best_shift(mixed[0], bgm_ref[0])
print(f"Best shift: {shift} samples ({shift/sr*1000:.1f}ms)")

# 对齐参考伴奏
if shift > 0:
    bgm_aligned = np.pad(bgm_ref, ((0, 0), (shift, 0)))[:, :target_len]
elif shift < 0:
    bgm_aligned = bgm_ref[:, -shift:][:, :target_len]
else:
    bgm_aligned = bgm_ref[:, :target_len]

# 补齐长度
if bgm_aligned.shape[1] < target_len:
    bgm_aligned = np.pad(bgm_aligned, ((0, 0), (0, target_len - bgm_aligned.shape[1])))

print("\n=== Spectral-guided BGM removal ===")

nperseg = 4096
noverlap = 3072
nfft = nperseg

for ch in range(mixed.shape[0]):
    print(f"\nProcessing channel {ch+1}...")
    
    # STFT
    f_m, t_m, Zxx_m = stft(mixed[ch], fs=sr, nperseg=nperseg, noverlap=noverlap, nfft=nfft)
    f_r, t_r, Zxx_r = stft(bgm_aligned[ch], fs=sr, nperseg=nperseg, noverlap=noverlap, nfft=nfft)
    
    min_frames = min(Zxx_m.shape[1], Zxx_r.shape[1])
    Zxx_m = Zxx_m[:, :min_frames]
    Zxx_r = Zxx_r[:, :min_frames]
    
    # 方法: 在每个时频点上，如果参考伴奏的能量较高，从混合音频中减去
    # 使用 Wiener-like 滤波
    mag_m = np.abs(Zxx_m)
    phase_m = np.angle(Zxx_m)
    mag_r = np.abs(Zxx_r)
    
    # 估计掩码：在伴奏能量高的地方减去更多
    # 使用 sigmoid 过渡避免突变
    ratio = mag_r / (mag_m + 1e-8)
    
    # 自适应减法强度
    # 在低频（伴奏主要能量区）减得更多，高频保留更多
    freq_bins = np.arange(mag_m.shape[0])
    low_freq_mask = np.exp(-freq_bins / (mag_m.shape[0] * 0.3))  # 低频权重高
    
    # 减法系数：低频减得多，高频减得少
    alpha_base = 1.5 + low_freq_mask * 1.0  # 1.5 ~ 2.5
    alpha = alpha_base[:, np.newaxis] * np.ones((1, min_frames))  # broadcast to (freq, time)
    
    # 频谱减法
    mag_clean = mag_m - alpha * ratio * mag_m
    mag_clean = np.maximum(mag_clean, 0.05 * mag_m)  # spectral floor
    
    # 重建
    Zxx_clean = mag_clean * np.exp(1j * phase_m)
    _, clean = istft(Zxx_clean, fs=sr, nperseg=nperseg, noverlap=noverlap, nfft=nfft)
    
    if ch == 0:
        clean_stereo = np.zeros((mixed.shape[0], len(clean)))
    clean_stereo[ch] = clean

# 归一化
min_len = min(clean_stereo.shape[1], target_len)
clean_stereo = clean_stereo[:, :min_len]
max_val = np.abs(clean_stereo).max()
if max_val > 0:
    clean_stereo = clean_stereo / max_val * 0.95

output_path = "D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/madoka_0908_spectral_guided.wav"
sf.write(output_path, clean_stereo.T, sr, subtype='PCM_16')
print(f"\nSaved: {output_path}")
print(f"Duration: {min_len/sr:.1f}s")

# 同时保存 demucs 的人声作为对比
print("\n=== Also comparing with demucs vocals ===")
vocals_out = vocals[:, :min_len]
max_v = np.abs(vocals_out).max()
if max_v > 0:
    vocals_out = vocals_out / max_v * 0.95
sf.write("D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/madoka_0908_demucs_vocals.wav", vocals_out.T, sr, subtype='PCM_16')
print(f"Demucs vocals saved (for comparison)")
