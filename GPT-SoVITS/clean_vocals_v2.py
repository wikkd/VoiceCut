import numpy as np
import librosa
import soundfile as sf
from scipy.signal import stft, istft, butter, sosfilt

print("Loading audio...")
audio, sr = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/sample_raw.wav", sr=48000, mono=False)
if audio.ndim == 1:
    audio = np.stack([audio, audio], axis=0)
print(f"Shape: {audio.shape}, sr: {sr}")

def aggressive_denoise(signal, sr):
    """强力降噪：谱减法 + 中值滤波"""
    nperseg = 4096
    noverlap = 3072
    
    f, t, Zxx = stft(signal, fs=sr, nperseg=nperseg, noverlap=noverlap)
    mag = np.abs(Zxx)
    phase = np.angle(Zxx)
    
    # 多次迭代降噪
    for iteration in range(3):
        noise_floor = np.percentile(mag, 8 + iteration * 3, axis=1, keepdims=True)
        alpha = 2.0 + iteration * 0.5
        mag = mag - alpha * noise_floor
        mag = np.maximum(mag, 0.05 * noise_floor)
    
    Zxx_clean = mag * np.exp(1j * phase)
    _, clean = istft(Zxx_clean, fs=sr, nperseg=nperseg, noverlap=noverlap)
    return clean

def heavy_de_ess(signal, sr):
    """强力齿音消除：多频段压缩"""
    nyq = sr / 2
    result = signal.copy()
    
    # 齿音频段：3kHz - 10kHz
    for low, high in [(3000, 6000), (6000, 10000)]:
        sos = butter(2, [low/nyq, min(high/nyq, 0.99)], btype='band', output='sos')
        band = sosfilt(sos, signal)
        
        # 计算包络
        envelope = np.abs(band)
        threshold = np.percentile(envelope, 85)
        
        # 超过阈值的部分衰减
        mask = envelope > threshold
        gain = np.ones_like(signal)
        gain[mask] = threshold / (envelope[mask] + 1e-8)
        gain = np.minimum(gain, 0.3)  # 最多衰减到30%
        
        # 平滑增益
        from scipy.ndimage import uniform_filter1d
        gain = uniform_filter1d(gain, size=int(sr * 0.005))
        
        result = result * gain
    
    return result

def soft_brighten(signal, sr, gain_db=3.0, freq_start=3000):
    """柔和提亮：只增强中高频，避免刺耳"""
    nyq = sr / 2
    
    # 高通
    sos_hp = butter(2, freq_start/nyq, btype='high', output='sos')
    high_freq = sosfilt(sos_hp, signal)
    
    # 限制高频能量
    high_env = np.abs(high_freq)
    max_high = np.percentile(high_env, 95)
    if max_high > 0:
        high_freq = high_freq / max_high * max_high
    
    gain_linear = 10 ** (gain_db / 20)
    brightened = signal + (gain_linear - 1) * high_freq
    return brightened

# ===== 主处理 =====
processed = np.zeros_like(audio)

for ch in range(audio.shape[0]):
    print(f"\nChannel {ch+1}:")
    sig = audio[ch].copy()
    
    # Step 1: 强力降噪
    print("  1. Aggressive denoise...")
    sig = aggressive_denoise(sig, sr)
    
    # Step 2: 强力齿音消除
    print("  2. Heavy de-ess...")
    sig = heavy_de_ess(sig, sr)
    
    # Step 3: 柔和提亮
    print("  3. Soft brighten...")
    sig = soft_brighten(sig, sr, gain_db=3.0, freq_start=3000)
    
    # Step 4: 高频低通（去掉超高频杂音 >12kHz）
    nyq = sr / 2
    sos_lp = butter(3, 12000/nyq, btype='low', output='sos')
    sig = sosfilt(sos_lp, sig)
    
    # Step 5: 低切
    sos_hp = butter(2, 100/nyq, btype='high', output='sos')
    sig = sosfilt(sos_hp, sig)
    
    processed[ch] = sig

# 归一化
max_val = np.abs(processed).max()
if max_val > 0:
    processed = processed / max_val * 0.90

output_path = "D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/sample_cleaned_v2.wav"
sf.write(output_path, processed.T, sr, subtype='PCM_16')
print(f"\nSaved: {output_path}")
