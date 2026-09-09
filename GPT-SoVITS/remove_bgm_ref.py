import numpy as np
import librosa
import soundfile as sf
from scipy.signal import correlate, fftconvolve

print("Loading original mixed audio...")
mixed, sr = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/input_0908.wav", sr=44100, mono=False)
if mixed.ndim == 1:
    mixed = np.stack([mixed, mixed], axis=0)
print(f"Mixed shape: {mixed.shape}")

print("Loading reference BGM...")
bgm_ref, _ = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/bgm_reference.mp3", sr=44100, mono=False)
if bgm_ref.ndim == 1:
    bgm_ref = np.stack([bgm_ref, bgm_ref], axis=0)
print(f"BGM ref shape: {bgm_ref.shape}")

# 用 demucs 分离出的伴奏作为"背景音乐估计"
print("Loading demucs accompaniment (no_vocals)...")
no_vocals, _ = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/separated_0908/htdemucs/input_0908/no_vocals.wav", sr=44100, mono=False)
if no_vocals.ndim == 1:
    no_vocals = np.stack([no_vocals, no_vocals], axis=0)
print(f"No vocals shape: {no_vocals.shape}")

target_len = mixed.shape[1]

def align_and_subtract(mixed_ch, bgm_ch, method="fft"):
    """对齐参考伴奏并从混合音频中减去"""
    # 归一化参考伴奏到混合音频的音量
    # 使用互相关找到最佳对齐偏移
    min_len = min(len(mixed_ch), len(bgm_ch))
    m = mixed_ch[:min_len]
    b = bgm_ch[:min_len]
    
    # 互相关找延迟
    correlation = fftconvolve(m, b[::-1], mode='full')
    lag = np.argmax(np.abs(correlation)) - min_len + 1
    
    print(f"  Detected lag: {lag} samples ({lag/sr*1000:.1f}ms)")
    
    # 对齐
    if lag > 0:
        bgm_aligned = np.pad(bgm_ch, (lag, 0))[:target_len]
    elif lag < 0:
        bgm_aligned = bgm_ch[-lag:][:target_len]
    else:
        bgm_aligned = bgm_ch[:target_len]
    
    # 补齐长度
    if len(bgm_aligned) < target_len:
        bgm_aligned = np.pad(bgm_aligned, (0, target_len - len(bgm_aligned)))
    
    # 缩放参考伴奏以匹配混合音频中的伴奏音量
    # 最小二乘法找到最佳缩放因子
    scale = np.dot(mixed_ch, bgm_aligned) / np.dot(bgm_aligned, bgm_aligned)
    print(f"  Optimal scale: {scale:.4f}")
    
    # 相减
    clean = mixed_ch - scale * bgm_aligned
    
    return clean, scale

print("\n=== Reference BGM subtraction ===")
clean_channels = []
for ch in range(mixed.shape[0]):
    print(f"Channel {ch+1}:")
    clean_ch, scale = align_and_subtract(mixed[ch], bgm_ref[ch])
    clean_channels.append(clean_ch)
    print(f"  RMS mixed: {np.sqrt(np.mean(mixed[ch]**2)):.6f}")
    print(f"  RMS clean: {np.sqrt(np.mean(clean_ch**2)):.6f}")

min_len = min(len(c) for c in clean_channels)
clean_stereo = np.stack([c[:min_len] for c in clean_channels], axis=0)

# 归一化
max_val = np.abs(clean_stereo).max()
if max_val > 0:
    clean_stereo = clean_stereo / max_val * 0.95

output_path = "D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/madoka_0908_ref_subtract.wav"
sf.write(output_path, clean_stereo.T, sr, subtype='PCM_16')
print(f"\nSaved: {output_path}")
print(f"Duration: {min_len/sr:.1f}s")
