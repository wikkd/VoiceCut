import numpy as np
import librosa
import soundfile as sf
from scipy.signal import stft, istft, fftconvolve
from scipy.ndimage import uniform_filter1d

print("Loading sample...")
sample, sr = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/sample_raw.wav", sr=44100, mono=False)
if sample.ndim == 1:
    sample = np.stack([sample, sample], axis=0)
print(f"Sample: {sample.shape}")

print("Loading reference BGM...")
bgm, _ = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/bgm_reference.mp3", sr=44100, mono=False)
if bgm.ndim == 1:
    bgm = np.stack([bgm, bgm], axis=0)
print(f"BGM: {bgm.shape}")

# 找到最佳对齐偏移
def find_shift(sig, ref):
    min_len = min(len(sig), len(ref))
    corr = fftconvolve(sig[:min_len], ref[:min_len][::-1], mode='full')
    return np.argmax(np.abs(corr)) - min_len + 1

shift = find_shift(sample[0], bgm[0])
print(f"Shift: {shift} samples ({shift/sr*1000:.1f}ms)")

# 对齐 BGM
target_len = sample.shape[1]
if shift > 0:
    bgm_aligned = np.pad(bgm, ((0,0),(shift,0)))[:, :target_len]
elif shift < 0:
    bgm_aligned = bgm[:, -shift:][:, :target_len]
else:
    bgm_aligned = bgm[:, :target_len]

if bgm_aligned.shape[1] < target_len:
    bgm_aligned = np.pad(bgm_aligned, ((0,0),(0, target_len - bgm_aligned.shape[1])))

# ===== 频谱减法：用参考伴奏从原片中减去 =====
nperseg = 4096
noverlap = 3072

clean_channels = []
for ch in range(sample.shape[0]):
    print(f"\nChannel {ch+1}...")
    
    f_m, t_m, Zxx_m = stft(sample[ch], fs=sr, nperseg=nperseg, noverlap=noverlap)
    f_r, t_r, Zxx_r = stft(bgm_aligned[ch], fs=sr, nperseg=nperseg, noverlap=noverlap)
    
    min_frames = min(Zxx_m.shape[1], Zxx_r.shape[1])
    Zxx_m = Zxx_m[:, :min_frames]
    Zxx_r = Zxx_r[:, :min_frames]
    
    mag_m = np.abs(Zxx_m)
    phase_m = np.angle(Zxx_m)
    mag_r = np.abs(Zxx_r)
    
    # 自适应频谱减法
    # 低频减得多（伴奏主要在低频），高频保留人声
    freq_bins = np.arange(mag_m.shape[0])[:, np.newaxis]  # (freq, 1)
    time_axis = np.ones((1, min_frames))
    
    # 频率权重：低频（<2000Hz）减得多，高频（>4000Hz）保留
    freq_weight = np.where(freq_bins < mag_m.shape[0] * 0.1, 2.5,   # 低频: alpha=2.5
                          np.where(freq_bins < mag_m.shape[0] * 0.2, 2.0,  # 中频: alpha=2.0
                                   np.where(freq_bins < mag_m.shape[0] * 0.3, 1.5,  # 中高频: alpha=1.5
                                            0.8)))  # 高频: alpha=0.8（保留人声）
    
    # 伴奏能量占比
    ratio = mag_r / (mag_m + 1e-8)
    
    # 减法
    mag_clean = mag_m - freq_weight * ratio * mag_m
    mag_clean = np.maximum(mag_clean, 0.08 * mag_m)
    
    # 平滑时间轴（减少音乐噪声）
    mag_clean = uniform_filter1d(mag_clean, size=3, axis=1)
    
    Zxx_clean = mag_clean * np.exp(1j * phase_m)
    _, clean = istft(Zxx_clean, fs=sr, nperseg=nperseg, noverlap=noverlap)
    clean_channels.append(clean[:target_len])

min_len = min(len(c) for c in clean_channels)
clean_stereo = np.stack([c[:min_len] for c in clean_channels], axis=0)

# 后处理：降噪 + 提亮
for ch in range(clean_stereo.shape[0]):
    sig = clean_stereo[ch]
    
    # 谱减降噪
    f, t, Zxx = stft(sig, fs=sr, nperseg=2048, noverlap=1536)
    mag = np.abs(Zxx)
    phase = np.angle(Zxx)
    noise_floor = np.percentile(mag, 10, axis=1, keepdims=True)
    mag = np.maximum(mag - 1.5 * noise_floor, 0.1 * noise_floor)
    sig_clean = istft(mag * np.exp(1j * phase), fs=sr, nperseg=2048, noverlap=1536)[1]
    clean_stereo[ch] = sig_clean[:min_len]

# 归一化
max_val = np.abs(clean_stereo).max()
if max_val > 0:
    clean_stereo = clean_stereo / max_val * 0.90

output_path = "D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/sample_no_bgm.wav"
sf.write(output_path, clean_stereo.T, sr, subtype='PCM_16')
print(f"\nSaved: {output_path}")
print(f"Duration: {min_len/sr:.1f}s")
