import os
import sys
import torch
import numpy as np
import librosa
import soundfile as sf

# 设置路径
uvr5_dir = os.path.join(os.path.dirname(__file__), 'tools', 'uvr5')
sys.path.insert(0, uvr5_dir)

from lib.lib_v5 import nets_61968KB as Nets
from lib.lib_v5.model_param_init import ModelParameters
from lib.lib_v5 import spec_utils
from lib.utils import inference as uvr5_inference

def separate_vocals(input_path, output_dir, device='cuda', is_half=True):
    """分离人声和背景音乐"""
    
    # 使用 HP5 模型（只保留主唱，去除其他人声）
    model_path = os.path.join(uvr5_dir, 'uvr5_weights', 'HP5_only_main_vocal.pth')
    param_path = os.path.join(uvr5_dir, 'lib', 'lib_v5', 'modelparams', '4band_v2.json')
    
    print(f"加载模型: {model_path}")
    mp = ModelParameters(param_path)
    model = Nets.CascadedASPPNet(mp.param['bins'] * 2)
    cpk = torch.load(model_path, map_location='cpu')
    model.load_state_dict(cpk)
    model.eval()
    
    if is_half:
        model = model.half().to(device)
    else:
        model = model.to(device)
    
    print(f"处理音频: {input_path}")
    
    # 读取音频
    X_wave = {}
    bands_n = len(mp.param["band"])
    
    for d in range(bands_n, 0, -1):
        bp = mp.param["band"][d]
        if d == bands_n:
            X_wave[d], _ = librosa.load(input_path, sr=bp["sr"], mono=False, dtype=np.float32)
            if X_wave[d].ndim == 1:
                X_wave[d] = np.asfortranarray([X_wave[d], X_wave[d]])
        else:
            X_wave[d] = librosa.resample(
                X_wave[d + 1],
                orig_sr=mp.param["band"][d + 1]["sr"],
                target_sr=bp["sr"],
                res_type=bp["res_type"]
            )
    
    # 转换为频谱图
    X_spec_s = {}
    for d in range(bands_n, 0, -1):
        X_spec_s[d] = spec_utils.wave_to_spectrogram_mt(
            X_wave[d],
            mp.param["band"][d]["fft_size"],
            mp.param["band"][d]["hop_length"],
            mp.param["band"][d]["n_tlabels"]
        )
    
    # 合并频谱图（取左声道）
    X_spec = X_spec_s[bands_n][:, :, :, 0] if X_spec_s[bands_n].ndim == 4 else X_spec_s[bands_n]
    
    # 推理
    print("分离人声...")
    instrumentals, X_mag, X_phase = uvr5_inference(
        X_spec,
        device, model, 0.75,
        {"window_size": 512, "tta": False}
    )
    
    # 转换为波形
    vocals = spec_utils.spectrogram_to_wave(X_phase)
    
    # 保存
    os.makedirs(output_dir, exist_ok=True)
    vocal_path = os.path.join(output_dir, 'vocals.wav')
    sf.write(vocal_path, vocals.T, mp.param["band"][bands_n]["sr"])
    
    print(f"完成！人声保存到: {vocal_path}")
    return vocal_path

if __name__ == "__main__":
    input_path = sys.argv[1] if len(sys.argv) > 1 else "input.wav"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "output"
    separate_vocals(input_path, output_dir)
