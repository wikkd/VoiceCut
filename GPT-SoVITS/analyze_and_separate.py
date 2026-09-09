import numpy as np
import librosa
import soundfile as sf
from scipy.signal import stft, istft, butter, sosfilt
from scipy.ndimage import uniform_filter1d

print("=" * 60)
print("Phase 1: 分析目标人声特征")
print("=" * 60)

# 加载已分离的人声作为参考（demucs 分离的 clean vocals）
print("\nLoading reference vocals (from demucs separation)...")
vocals_ref, sr = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/separated_0908/htdemucs/input_0908/vocals.wav", sr=44100, mono=False)
if vocals_ref.ndim == 1:
    vocals_ref = np.stack([vocals_ref, vocals_ref], axis=0)

# 加载原始混合音频
print("Loading mixed audio...")
mixed, _ = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/input_0908.wav", sr=44100, mono=False)
if mixed.ndim == 1:
    mixed = np.stack([mixed, mixed], axis=0)

# 截取到相同长度
target_len = min(vocals_ref.shape[1], mixed.shape[1])
vocals_ref = vocals_ref[:, :target_len]
mixed = mixed[:, :target_len]
print(f"Length: {target_len/sr:.1f}s")

# ===== 1. 基频（F0）分析 =====
print("\n--- 分析 F0 (基频) ---")
vocals_mono = np.mean(vocals_ref, axis=0)
f0, voiced_flag, voiced_probs = librosa.pyin(vocals_mono, fmin=80, fmax=600, sr=sr)
f0_valid = f0[~np.isnan(f0)]
print(f"F0 range: {np.min(f0_valid):.1f} - {np.max(f0_valid):.1f} Hz")
print(f"F0 mean: {np.mean(f0_valid):.1f} Hz")
print(f"F0 median: {np.median(f0_valid):.1f} Hz")

# ===== 2. 共振峰（Formants）分析 =====
print("\n--- 分析共振峰 ---")
# 用 LPC 估计共振峰
from scipy.linalg import toeplitz, solve

def estimate_formants(signal, sr, order=16):
    """用 LPC 估计共振峰"""
    # 预加重
    pre = np.append(signal[0], signal[1:] - 0.97 * signal[:-1])
    # 加窗
    windowed = pre * np.hamming(len(pre))
    # 自相关
    r = np.correlate(windowed, windowed, mode='full')
    r = r[len(r)//2:]
    r = r[:order + 1]
    # Levinson-Durbin
    a = np.zeros(order + 1)
    a[0] = 1.0
    for i in range(1, order + 1):
        acc = r[i]
        for j in range(1, i):
            acc += a[j] * r[i - j]
        a[i] = -acc / r[0] if r[0] != 0 else 0
    
    # 求根
    roots = np.roots(a)
    roots = roots[np.imag(roots) >= 0]
    
    # 转为频率
    freqs = np.angle(roots) * sr / (2 * np.pi)
    # 取有意义的共振峰（300-5000Hz）
    formants = sorted(freqs[(freqs > 300) & (freqs < 5000)])
    return formants

# 分帧估计共振峰
frame_len = int(sr * 0.03)  # 30ms
hop = int(sr * 0.01)  # 10ms
n_frames = (target_len - frame_len) // hop

all_formants = []
for i in range(min(n_frames, 500)):  # 取前500帧
    start = i * hop
    frame = vocals_mono[start:start + frame_len]
    formants = estimate_formants(frame, sr, order=12)
    if len(formants) >= 2:
        all_formants.append(formants[:4])  # F1-F4

all_formants = np.array(all_formants)
print(f"Estimated {len(all_formants)} frames")
print(f"F1: {np.mean(all_formants[:, 0]):.0f} Hz (±{np.std(all_formants[:, 0]):.0f})")
if all_formants.shape[1] > 1:
    print(f"F2: {np.mean(all_formants[:, 1]):.0f} Hz (±{np.std(all_formants[:, 1]):.0f})")
if all_formants.shape[1] > 2:
    print(f"F3: {np.mean(all_formants[:, 2]):.0f} Hz (±{np.std(all_formants[:, 2]):.0f})")
if all_formants.shape[1] > 3:
    print(f"F4: {np.mean(all_formants[:, 3]):.0f} Hz (±{np.std(all_formants[:, 3]):.0f})")

# ===== 3. 频谱包络（音色指纹） =====
print("\n--- 分析频谱包络 ---")
# 计算平均频谱包络
nperseg = 4096
noverlap = 3072
f, t, Zxx_ref = stft(vocals_mono, fs=sr, nperseg=nperseg, noverlap=noverlap)
mag_ref = np.abs(Zxx_ref)
# 平均频谱包络
spectral_envelope = np.mean(mag_ref, axis=1)
spectral_envelope_db = 20 * np.log10(spectral_envelope + 1e-8)
print(f"Spectral envelope computed: {len(spectral_envelope)} bins")

# ===== 4. 谐波-打击乐分离分析 =====
print("\n--- 谐波/打击乐分析 ---")
harmonic, percussive = librosa.effects.hpss(vocals_mono)
harmonic_energy = np.sum(harmonic**2)
percussive_energy = np.sum(percussive**2)
total_energy = harmonic_energy + percussive_energy
print(f"Harmonic ratio: {harmonic_energy/total_energy:.1%}")
print(f"Percussive ratio: {percussive_energy/total_energy:.1%}")

print("\n" + "=" * 60)
print("Phase 2: 基于特征的人声分离")
print("=" * 60)

# ===== 构建人声掩码 =====
nperseg = 4096
noverlap = 3072

for ch in range(mixed.shape[0]):
    print(f"\nChannel {ch+1}:")
    sig = mixed[ch]
    
    # STFT
    f, t, Zxx = stft(sig, fs=sr, nperseg=nperseg, noverlap=noverlap)
    mag = np.abs(Zxx)
    phase = np.angle(Zxx)
    min_frames = mag.shape[1]
    
    # --- 掩码1: 基频谐波掩码 ---
    # 在 F0 及其谐波位置增强
    freq_bins = f  # Hz
    harmonic_mask = np.ones(len(freq_bins))
    
    for h in range(1, 8):  # 基频 + 7个谐波
        center = np.mean(f0_valid) * h
        if center > sr / 2:
            break
        # 在每个谐波位置放一个高斯窗
        sigma = 50 + h * 20  # 谐波带宽随阶数增加
        harmonic_mask += 1.5 * np.exp(-0.5 * ((freq_bins - center) / sigma) ** 2)
    
    harmonic_mask = harmonic_mask[:, np.newaxis] * np.ones((1, min_frames))
    
    # --- 掩码2: 共振峰掩码 ---
    formant_mask = np.ones(len(freq_bins))
    for i in range(min(all_formants.shape[1], 4)):
        center = np.mean(all_formants[:, i])
        sigma = np.std(all_formants[:, i]) + 100
        formant_mask += 1.0 * np.exp(-0.5 * ((freq_bins - center) / sigma) ** 2)
    
    formant_mask = formant_mask[:, np.newaxis] * np.ones((1, min_frames))
    
    # --- 掩码3: 频谱包络匹配 ---
    # 计算混合音频的频谱包络
    envelope_mix = np.mean(mag, axis=1)
    # 匹配度：相关系数
    from scipy.stats import pearsonr
    # 逐帧计算与参考人声的频谱包络相似度
    envelope_similarity = np.zeros(min_frames)
    for i_frame in range(min_frames):
        frame_env = mag[:, i_frame]
        # 用滑动窗口比较
        if np.std(frame_env) > 0 and np.std(spectral_envelope[:len(frame_env)]) > 0:
            # 对齐长度
            min_f = min(len(frame_env), len(spectral_envelope))
            corr = np.corrcoef(frame_env[:min_f], spectral_envelope[:min_f])[0, 1]
            envelope_similarity[i_frame] = max(0, corr)
    
    envelope_mask = 0.5 + envelope_similarity[np.newaxis, :] * 1.5
    
    # --- 组合掩码 ---
    combined_mask = harmonic_mask * formant_mask * envelope_mask
    
    # 谐波区域权重更高
    harmonic_ratio = harmonic_energy / (total_energy + 1e-8)
    combined_mask *= (0.7 + 0.6 * harmonic_ratio)
    
    # 平滑掩码
    combined_mask = uniform_filter1d(combined_mask, size=5, axis=1)
    
    # 归一化掩码
    combined_mask = np.clip(combined_mask, 0, 3)
    combined_mask = combined_mask / combined_mask.max()
    
    # 应用掩码
    vocal_mag = mag * combined_mask
    
    # 背景噪声估计（用混合音频减去增强的人声）
    noise_mag = np.maximum(mag - vocal_mag, 0)
    noise_mag = noise_mag * 0.3  # 降低噪声贡献
    
    # 重建
    Zxx_vocal = vocal_mag * np.exp(1j * phase)
    _, vocal_clean = istft(Zxx_vocal, fs=sr, nperseg=nperseg, noverlap=noverlap)
    
    if ch == 0:
        result = np.zeros((mixed.shape[0], len(vocal_clean)))
    result[ch] = vocal_clean

# 归一化
result = result[:, :target_len]
max_val = np.abs(result).max()
if max_val > 0:
    result = result / max_val * 0.90

output_path = "D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/sample_feature_separated.wav"
sf.write(output_path, result.T, sr, subtype='PCM_16')
print(f"\nSaved: {output_path}")
print(f"Duration: {target_len/sr:.1f}s")

# 保存分析结果
print("\n" + "=" * 60)
print("分析总结")
print("=" * 60)
print(f"基频范围: {np.min(f0_valid):.0f} - {np.max(f0_valid):.0f} Hz")
print(f"基频均值: {np.mean(f0_valid):.0f} Hz")
print(f"F1 均值: {np.mean(all_formants[:, 0]):.0f} Hz")
if all_formants.shape[1] > 1:
    print(f"F2 均值: {np.mean(all_formants[:, 1]):.0f} Hz")
if all_formants.shape[1] > 2:
    print(f"F3 均值: {np.mean(all_formants[:, 2]):.0f} Hz")
print(f"谐波占比: {harmonic_energy/total_energy:.1%}")
