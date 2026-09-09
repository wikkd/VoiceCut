import numpy as np
import librosa
import soundfile as sf
from scipy.signal import butter, sosfilt, wiener

print("Loading audio...")
audio, sr = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/sample_raw.wav", sr=48000, mono=False)
if audio.ndim == 1:
    audio = np.stack([audio, audio], axis=0)
print(f"Shape: {audio.shape}, sr: {sr}")

# ===== 1. 降噪：谱减法去背景噪音 =====
def spectral_denoise(signal, sr, noise_thresh=0.02):
    """简单的谱减法降噪"""
    from scipy.signal import stft, istft
    nperseg = 2048
    noverlap = 1536
    
    f, t, Zxx = stft(signal, fs=sr, nperseg=nperseg, noverlap=noverlap)
    mag = np.abs(Zxx)
    phase = np.angle(Zxx)
    
    # 估计噪声底（取幅度谱的低百分位数作为噪声估计）
    noise_floor = np.percentile(mag, 10, axis=1, keepdims=True)
    
    # 谱减
    alpha = 1.5
    mag_clean = mag - alpha * noise_floor
    mag_clean = np.maximum(mag_clean, 0.1 * noise_floor)  # spectral floor
    
    Zxx_clean = mag_clean * np.exp(1j * phase)
    _, clean = istft(Zxx_clean, fs=sr, nperseg=nperseg, noverlap=noverlap)
    return clean

# ===== 2. 人声提亮：增强中高频 =====
def brighten_vocals(signal, sr, gain_db=6.0, freq_start=2000):
    """增强中高频使人声更明亮"""
    from scipy.signal import butter, sosfilt
    
    # 高通滤波器 + 增益
    nyq = sr / 2
    freq_start_norm = freq_start / nyq
    
    # 设计高通滤波器
    sos_hp = butter(4, freq_start_norm, btype='high', output='sos')
    high_freq = sosfilt(sos_hp, signal)
    
    # 增益系数
    gain_linear = 10 ** (gain_db / 20)
    
    # 混合：原始 + 增强的高频
    brightened = signal + (gain_linear - 1) * high_freq
    
    return brightened

# ===== 3. 动态压缩：使音量更均匀 =====
def dynamic_compress(signal, sr, threshold=0.3, ratio=4.0, attack_ms=5, release_ms=50):
    """简单的动态范围压缩"""
    attack_samples = int(sr * attack_ms / 1000)
    release_samples = int(sr * release_ms / 1000)
    
    envelope = np.abs(signal)
    compressed = np.copy(signal)
    
    gain = np.ones_like(signal)
    current_gain = 1.0
    
    for i in range(len(signal)):
        if envelope[i] > threshold:
            target_gain = threshold + (envelope[i] - threshold) / ratio
            target_gain = target_gain / envelope[i] if envelope[i] > 0 else 1.0
        else:
            target_gain = 1.0
        
        # 平滑攻击和释放
        if target_gain < current_gain:
            current_gain += (target_gain - current_gain) / attack_samples
        else:
            current_gain += (target_gain - current_gain) / release_samples
        
        gain[i] = current_gain
    
    compressed = signal * gain
    return compressed

# ===== 4. 齿音消除 =====
def de_ess(signal, sr, freq_start=5000, threshold=0.2):
    """简单齿音消除"""
    from scipy.signal import butter, sosfilt
    nyq = sr / 2
    
    # 提取高频齿音区域
    sos = butter(2, [freq_start/nyq, min(12000/nyq, 0.99)], btype='band', output='sos')
    sibilant = sosfilt(sos, signal)
    
    # 如果齿音能量过大，衰减
    sib_env = np.abs(sibilant)
    mask = sib_env > threshold
    reduction = np.ones_like(signal)
    reduction[mask] = threshold / sib_env[mask]
    
    return signal * reduction

# ===== 主处理流程 =====
processed = np.zeros_like(audio)

for ch in range(audio.shape[0]):
    print(f"\nProcessing channel {ch+1}...")
    sig = audio[ch].copy()
    
    # Step 1: 降噪
    print("  Step 1: Denoising...")
    sig = spectral_denoise(sig, sr)
    
    # Step 2: 齿音消除
    print("  Step 2: De-essing...")
    sig = de_ess(sig, sr)
    
    # Step 3: 动态压缩
    print("  Step 3: Dynamic compression...")
    sig = dynamic_compress(sig, sr)
    
    # Step 4: 人声提亮
    print("  Step 4: Brightening...")
    sig = brighten_vocals(sig, sr, gain_db=5.0, freq_start=2500)
    
    # Step 5: 低切（去掉低频隆隆声）
    from scipy.signal import butter, sosfilt
    sos_lowcut = butter(2, 80/sr*2, btype='high', output='sos')
    sig = sosfilt(sos_lowcut, sig)
    
    processed[ch] = sig

# 归一化
max_val = np.abs(processed).max()
if max_val > 0:
    processed = processed / max_val * 0.92

# 保存
output_path = "D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/sample_cleaned.wav"
sf.write(output_path, processed.T, sr, subtype='PCM_16')
print(f"\nSaved: {output_path}")
print(f"Duration: {processed.shape[1]/sr:.1f}s")

# 也保存一个对比版本（只降噪不提亮）
denoise_only = np.zeros_like(audio)
for ch in range(audio.shape[0]):
    denoise_only[ch] = spectral_denoise(audio[ch], sr)
    denoise_only[ch] = dynamic_compress(denoise_only[ch], sr)
max_val2 = np.abs(denoise_only).max()
if max_val2 > 0:
    denoise_only = denoise_only / max_val2 * 0.92
sf.write("D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/sample_denoised_only.wav", denoise_only.T, sr, subtype='PCM_16')
print("Denoised-only version also saved for comparison")
