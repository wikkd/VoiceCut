import numpy as np
import librosa
import soundfile as sf
import noisereduce as nr

print("Loading audio...")
audio, sr = librosa.load("D:/projects/ai-agent-test/GPT-SoVITS/sample_raw.wav", sr=48000, mono=False)
if audio.ndim == 1:
    audio = np.stack([audio, audio], axis=0)
print(f"Shape: {audio.shape}, sr: {sr}")

processed = np.zeros_like(audio)

for ch in range(audio.shape[0]):
    print(f"\nChannel {ch+1}:")
    sig = audio[ch]
    
    # noisereduce: spectral gating 降噪
    # prop_decrease: 降噪强度 (0-1)，越大降噪越强
    # n_std_thresh_stationary: 静态噪声阈值
    print("  noisereduce: stationary noise reduction...")
    clean = nr.reduce_noise(
        y=sig,
        sr=sr,
        stationary=True,
        prop_decrease=0.95,       # 95%降噪强度
        n_std_thresh_stationary=1.5,
        freq_mask_smooth_hz=500,
        time_mask_smooth_ms=50,
    )
    
    # 第二遍：非静态降噪（处理残留噪声）
    print("  noisereduce: non-stationary noise reduction...")
    clean = nr.reduce_noise(
        y=clean,
        sr=sr,
        stationary=False,
        prop_decrease=0.8,
        thresh_n_mult_nonstationary=2,
        sigmoid_slope_nonstationary=10,
        freq_mask_smooth_hz=500,
        time_mask_smooth_ms=50,
    )
    
    processed[ch] = clean

# 归一化
max_val = np.abs(processed).max()
if max_val > 0:
    processed = processed / max_val * 0.90

output_path = "D:/projects/ai-agent-test/GPT-SoVITS/output/cleaned/sample_cleaned_v3.wav"
sf.write(output_path, processed.T, sr, subtype='PCM_16')
print(f"\nSaved: {output_path}")
print(f"Duration: {processed.shape[1]/sr:.1f}s")
