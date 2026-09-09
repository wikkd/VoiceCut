import numpy as np
import soundfile as sf
import librosa
import os
import sys

def quick_slice(input_path, output_dir=None, segment_sec=5, threshold=-40, min_length=2000):
    """
    快速切片工具
    - 按静音自动切片
    - 或按固定时长切片
    """
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(input_path), "sliced")
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"Loading: {input_path}")
    audio, sr = librosa.load(input_path, sr=None, mono=False)
    if audio.ndim == 1:
        audio = audio.reshape(1, -1)
    
    duration = audio.shape[1] / sr
    print(f"Duration: {duration:.1f}s, Sample rate: {sr}Hz, Channels: {audio.shape[0]}")
    
    # 方法1: 按固定时长切片
    if segment_sec > 0:
        chunk_samples = int(segment_sec * sr)
        total_samples = audio.shape[1]
        
        slices = []
        for start in range(0, total_samples, chunk_samples):
            end = min(start + chunk_samples, total_samples)
            # 跳过太短的尾部
            if end - start < sr * 0.5:
                continue
            slices.append((start, end))
        
        print(f"\n按 {segment_sec}s 切片，共 {len(slices)} 段")
        
        for i, (start, end) in enumerate(slices):
            clip = audio[:, start:end]
            # 归一化
            max_val = np.abs(clip).max()
            if max_val > 0:
                clip = clip / max_val * 0.9
            
            out_path = os.path.join(output_dir, f"{i:03d}_{start/sr:.1f}s-{end/sr:.1f}s.wav")
            sf.write(out_path, clip.T, sr, subtype='PCM_16')
        
        print(f"Saved {len(slices)} clips to: {output_dir}")
        return slices
    
    # 方法2: 按静音自动切片
    from tools.slicer2 import Slicer
    slicer = Slicer(sr=sr, threshold=threshold, min_length=min_length, min_interval=300, hop_size=20, max_sil_kept=500)
    chunks = slicer.slice(audio[0] if audio.shape[0] == 1 else audio)
    
    print(f"\n按静音切片，共 {len(chunks)} 段")
    
    for i, chunk_data in enumerate(chunks):
        if isinstance(chunk_data, list):
            chunk = chunk_data[0]
        else:
            chunk = chunk_data
        
        if hasattr(chunk, 'shape'):
            if chunk.ndim == 1:
                chunk = chunk.reshape(1, -1)
            
            max_val = np.abs(chunk).max()
            if max_val > 0:
                chunk = chunk / max_val * 0.9
            
            duration_sec = chunk.shape[-1] / sr
            out_path = os.path.join(output_dir, f"{i:03d}_{duration_sec:.1f}s.wav")
            sf.write(out_path, chunk.T, sr, subtype='PCM_16')
    
    print(f"Saved {len(chunks)} clips to: {output_dir}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python quick_slice.py <audio_file> [segment_seconds] [threshold]")
        print("Example: python quick_slice.py input.wav 5")
        print("Example: python quick_slice.py input.wav 0 -40  (0=auto silence detection)")
        sys.exit(1)
    
    input_path = sys.argv[1]
    seg_sec = float(sys.argv[2]) if len(sys.argv) > 2 else 5
    threshold = float(sys.argv[3]) if len(sys.argv) > 3 else -40
    
    quick_slice(input_path, segment_sec=seg_sec, threshold=threshold)
