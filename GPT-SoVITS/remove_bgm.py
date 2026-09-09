import numpy as np
import librosa
import soundfile as sf

print("Loading original mixed audio...")
# 原始混合音频（有人声+背景音乐）
mixed, sr = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/input_0908.wav", sr=44100, mono=False)
if mixed.ndim == 1:
    mixed = np.stack([mixed, mixed], axis=0)
print(f"Mixed shape: {mixed.shape}, sr: {sr}")

print("Loading reference BGM...")
# 干净的背景音乐原曲
bgm, sr2 = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/bgm_reference.mp3", sr=44100, mono=False)
if bgm.ndim == 1:
    bgm = np.stack([bgm, bgm], axis=0)
print(f"BGM shape: {bgm.shape}, sr: {sr2}")

# 确保长度一致（截取或填充到混合音频的长度）
target_len = mixed.shape[1]
if bgm.shape[1] > target_len:
    bgm = bgm[:, :target_len]
elif bgm.shape[1] < target_len:
    pad = target_len - bgm.shape[1]
    bgm = np.pad(bgm, ((0, 0), (0, pad)), mode='constant')

print(f"Target length: {target_len} samples ({target_len/sr:.1f}s)")

# ===== 方法1: 简单频谱减法 =====
print("\n=== Method 1: Simple spectral subtraction ===")
# 转到频域
from scipy.signal import stft, istft

def spectral_subtract(mixed_audio, noise_audio, sr, alpha=2.0, beta=0.01):
    """频谱减法：从混合音频中减去噪声（背景音乐）"""
    nperseg = 2048
    noverlap = 1536
    
    # STFT
    f_mixed, t_mixed, Zxx_mixed = stft(mixed_audio, fs=sr, nperseg=nperseg, noverlap=noverlap)
    f_noise, t_noise, Zxx_noise = stft(noise_audio, fs=sr, nperseg=nperseg, noverlap=noverlap)
    
    # 确保时间帧数一致
    min_frames = min(Zxx_mixed.shape[1], Zxx_noise.shape[1])
    Zxx_mixed = Zxx_mixed[:, :min_frames]
    Zxx_noise = Zxx_noise[:, :min_frames]
    
    # 幅度谱
    mag_mixed = np.abs(Zxx_mixed)
    phase_mixed = np.angle(Zxx_mixed)
    mag_noise = np.abs(Zxx_noise)
    
    # 频谱减法：从混合音频的幅度谱中减去背景音乐的幅度谱
    # 使用 oversubtraction (alpha > 1) 和 spectral floor (beta)
    mag_clean = mag_mixed - alpha * mag_noise
    mag_clean = np.maximum(mag_clean, beta * mag_mixed)  # spectral floor
    
    # 用原始相位重建
    Zxx_clean = mag_clean * np.exp(1j * phase_mixed)
    
    # ISTFT
    _, clean = istft(Zxx_clean, fs=sr, nperseg=nperseg, noverlap=noverlap)
    return clean

# 对每个声道分别处理
clean_channels = []
for ch in range(mixed.shape[0]):
    print(f"Processing channel {ch+1}/{mixed.shape[0]}...")
    clean_ch = spectral_subtract(mixed[ch], bgm[ch], sr, alpha=2.5, beta=0.02)
    clean_channels.append(clean_ch)

# 合并声道
min_len = min(len(c) for c in clean_channels)
clean_stereo = np.stack([c[:min_len] for c in clean_channels], axis=0)

# 归一化
max_val = np.abs(clean_stereo).max()
if max_val > 0:
    clean_stereo = clean_stereo / max_val * 0.95

# 保存
output_path = "D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/madoka_0908_no_bgm.wav"
sf.write(output_path, clean_stereo.T, sr, subtype='PCM_16')
print(f"\nSaved: {output_path}")
print(f"Duration: {min_len/sr:.1f}s")
