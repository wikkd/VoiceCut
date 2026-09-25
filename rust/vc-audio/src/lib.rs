//! vc-audio: VoiceCut 音频热点函数的 Rust 实现（PyO3 扩展，abi3-py38）。
//!
//! 与 app/audio_ops.py 纯 Python 实现的接口约定：
//! - `compute_peaks(path, max_points=4000)` → `[[min, max], ...]`
//!   （逐帧声道下混 → 每 ceil(frames/max_points) 帧取 min/max，与 numpy 版一致）
//! - `audio_metrics_raw(path)` → 全精度聚合值 dict；
//!   round(x, 2/4) 等"收尾"留在 Python 侧（同一段代码服务两条实现路径，
//!   保证两条路径输出逐字节一致）。
//!
//! 实现：手写 RIFF/WAVE 头解析 + 整块字节流读取（v1 的 hound 逐样本迭代器
//! 比 soundfile 的块读慢 3 倍，已弃用）。支持 PCM 16/24/32 位整型与
//! 32 位浮点（VoiceCut 产物均为 pcm_s16le 单声道）。
use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};

struct WavInfo {
    data_off: u64,
    data_len: u64,
    format: u16, // 1=PCM int, 3=IEEE float
    channels: usize,
    sample_rate: u32,
    bits: u16,
}

fn perr(msg: &str) -> PyErr {
    PyErr::new::<PyIOError, _>(msg.to_string())
}

fn parse_wav(path: &str) -> PyResult<WavInfo> {
    let mut f = File::open(path).map_err(|e| perr(&format!("{}", e)))?;
    let mut hdr = [0u8; 12];
    f.read_exact(&mut hdr).map_err(|e| perr(&format!("{}", e)))?;
    if &hdr[0..4] != b"RIFF" || &hdr[8..12] != b"WAVE" {
        return Err(perr("not a RIFF/WAVE file"));
    }
    let mut fmt: Option<(u16, usize, u32, u16)> = None;
    let mut data: Option<(u64, u64)> = None;
    loop {
        let mut ch = [0u8; 8];
        if f.read_exact(&mut ch).is_err() {
            break;
        }
        let id = [ch[0], ch[1], ch[2], ch[3]];
        let size = u32::from_le_bytes([ch[4], ch[5], ch[6], ch[7]]) as u64;
        if id == *b"fmt " {
            let mut body = vec![0u8; size as usize];
            f.read_exact(&mut body).map_err(|e| perr(&format!("{}", e)))?;
            if body.len() < 16 {
                return Err(perr("fmt chunk too short"));
            }
            let format = u16::from_le_bytes([body[0], body[1]]);
            let channels = u16::from_le_bytes([body[2], body[3]]) as usize;
            let sample_rate = u32::from_le_bytes([body[4], body[5], body[6], body[7]]);
            let bits = u16::from_le_bytes([body[14], body[15]]);
            // WAVE_FORMAT_EXTENSIBLE：真实格式在扩展区前 2 字节
            let real = if format == 0xFFFE && body.len() >= 26 {
                u16::from_le_bytes([body[24], body[25]])
            } else {
                format
            };
            fmt = Some((real, channels, sample_rate, bits));
            if size & 1 == 1 {
                f.seek(SeekFrom::Current(1)).ok();
            }
        } else if id == *b"data" {
            data = Some((pos_of(&mut f)? + 0, size));
            f.seek(SeekFrom::Current(size as i64 + (size & 1) as i64))
                .ok();
        } else {
            f.seek(SeekFrom::Current(size as i64 + (size & 1) as i64)).ok();
        }
        if fmt.is_some() && data.is_some() {
            break;
        }
    }
    let (format, channels, sample_rate, bits) =
        fmt.ok_or_else(|| perr("fmt chunk missing"))?;
    let (data_off, data_len) = data.ok_or_else(|| perr("data chunk missing"))?;
    Ok(WavInfo { data_off, data_len, format, channels, sample_rate, bits })
}

/// 返回当前 seek 位置（读 8 字节块头前的位置需调用方自己记；这里用流位置回推）。
fn pos_of(f: &mut File) -> PyResult<u64> {
    f.stream_position().map_err(|e| perr(&format!("{}", e)))
}

fn open_at(path: &str, info: &WavInfo) -> PyResult<(File, usize, f32, usize)> {
    let mut f = File::open(path).map_err(|e| perr(&format!("{}", e)))?;
    f.seek(SeekFrom::Start(info.data_off))
        .map_err(|e| perr(&format!("{}", e)))?;
    let bps = (info.bits / 8) as usize;
    let bytes_per_frame = bps * info.channels;
    let frames = (info.data_len / bytes_per_frame as u64) as usize;
    let scale: f32 = match (info.format, info.bits) {
        (3, _) => 1.0,
        (1, b) => 1.0 / 2f64.powi(b as i32 - 1) as f32,
        _ => return Err(perr("unsupported wav format (need PCM int or IEEE float)")),
    };
    Ok((f, frames, scale, bytes_per_frame))
}

/// 从一个原始字节块解析逐帧下混均值，写入 out（f32）。
fn decode_frames(buf: &[u8], format: u16, bits: u16, channels: usize, out: &mut Vec<f32>) {
    let bps = (bits / 8) as usize;
    let scale: f32 = match (format, bits) {
        (3, _) => 1.0,
        (1, b) => 1.0 / 2f64.powi(b as i32 - 1) as f32,
        _ => 1.0,
    };
    let inv_c = 1.0 / channels as f32;
    let mut i = 0usize;
    let bytes = buf.len() - buf.len() % (bps * channels);
    let mut acc;
    while i < bytes {
        acc = 0.0f32;
        for _ in 0..channels {
            let v: f32 = match (format, bits) {
                (3, 32) => f32::from_le_bytes([buf[i], buf[i + 1], buf[i + 2], buf[i + 3]]),
                (1, 16) => {
                    let v = i16::from_le_bytes([buf[i], buf[i + 1]]);
                    v as f32 * scale
                }
                (1, 24) => {
                    let mut v = (buf[i] as i32) | ((buf[i + 1] as i32) << 8) | ((buf[i + 2] as i32) << 16);
                    if v & 0x80_0000 != 0 {
                        v -= 1 << 24;
                    }
                    v as f32 * scale
                }
                (1, 32) => {
                    let v = i32::from_le_bytes([buf[i], buf[i + 1], buf[i + 2], buf[i + 3]]);
                    v as f32 * scale
                }
                _ => 0.0,
            };
            acc += v;
            i += bps;
        }
        out.push(acc * inv_c);
    }
}

#[pyfunction]
#[pyo3(signature = (path, max_points = 4000))]
fn compute_peaks(path: &str, max_points: usize) -> PyResult<Vec<Vec<f32>>> {
    let max_points = max_points.max(1);
    let info = parse_wav(path)?;
    let (mut f, frames, _scale, bpf) = open_at(path, &info)?;
    if frames == 0 {
        return Ok(Vec::new());
    }
    let block = (frames + max_points - 1) / max_points;
    let mut peaks: Vec<Vec<f32>> = Vec::with_capacity(frames / block + 1);
    let mut frames_left = frames;

    // 快路径：PCM16 单声道 —— 零拷贝转 &[i16]，整数域 min/max（自动向量化），
    // 峰值在最后一步乘 scale（线性，逐字节等价于先下混再缩放）。
    if info.format == 1 && info.bits == 16 && info.channels == 1 {
        let mut buf = vec![0u8; block * bpf];
        while frames_left > 0 {
            let take = block.min(frames_left);
            let want = take * bpf;
            f.read_exact(&mut buf[..want]).map_err(|e| perr(&format!("{}", e)))?;
            // buf 为独立分配（对齐 ≥ 8 字节），cast_slice 对 i16 恒成功
            let s: &[i16] = bytemuck::cast_slice(&buf[..want]);
            for chunk in s.chunks(block) {
                let mn = chunk.iter().min().copied().unwrap_or(0i16);
                let mx = chunk.iter().max().copied().unwrap_or(0i16);
                peaks.push(vec![mn as f32 * _scale, mx as f32 * _scale]);
            }
            frames_left -= take;
        }
        return Ok(peaks);
    }

    // 通用路径（多声道 / 24/32 位 / 浮点）
    let mut buf = vec![0u8; block * bpf];
    let mut decoded: Vec<f32> = Vec::with_capacity(block);
    while frames_left > 0 {
        let take = block.min(frames_left);
        let want = take * bpf;
        f.read_exact(&mut buf[..want]).map_err(|e| perr(&format!("{}", e)))?;
        decoded.clear();
        decode_frames(&buf[..want], info.format, info.bits, info.channels, &mut decoded);
        let (mut bmin, mut bmax) = (f32::INFINITY, f32::NEG_INFINITY);
        for &m in &decoded {
            if m < bmin { bmin = m; }
            if m > bmax { bmax = m; }
        }
        peaks.push(vec![bmin, bmax]);
        frames_left -= take;
    }
    Ok(peaks)
}

#[pyfunction]
fn audio_metrics_raw<'py>(py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyDict>> {
    let info = parse_wav(path)?;
    let (mut f, frames, _scale, bpf) = open_at(path, &info)?;
    if frames == 0 {
        // 与 numpy 路径对齐：np.max(空数组) 抛 ValueError
        return Err(PyErr::new::<PyValueError, _>("zero-size audio"));
    }
    let silence_thr: f64 = 10f64.powf(-50.0 / 20.0);
    let mut sum_sq = 0f64;
    let mut peak = 0f64;
    let mut silence = 0u64;
    let mut clipping = false;
    let mut buf = vec![0u8; 1 << 20]; // 1MiB 块
    let mut decoded: Vec<f32> = Vec::with_capacity(1 << 18);
    let mut frames_left = frames;

    // 快路径：PCM16 单声道 —— 整数域精确累加（i64 平方和），阈值换算成整数
    if info.format == 1 && info.bits == 16 && info.channels == 1 {
        let scale_inv = 2f64.powi(15); // 32768
        let thr_i = silence_thr * scale_inv;               // 103.62…
        let sil_lim = if thr_i.fract() == 0.0 { thr_i as i64 - 1 } else { thr_i.floor() as i64 };
        let clip_lim = (0.999f64 * scale_inv).ceil() as i64; // 32736
        let mut peak16: u16 = 0;
        let mut sum_sq_i: i64 = 0;
        let mut sum_sq_lanes = [0i64; 16]; // 16 条独立车道，消除循环依赖便于向量化
        while frames_left > 0 {
            let take = (buf.len() / bpf).min(frames_left);
            let want = take * bpf;
            f.read_exact(&mut buf[..want]).map_err(|e| perr(&format!("{}", e)))?;
            let s: &[i16] = bytemuck::cast_slice(&buf[..want]);
            let mut chunks = s.chunks_exact(16);
            for c in &mut chunks {
                for k in 0..16 {
                    let a = c[k].unsigned_abs() as i64;
                    sum_sq_lanes[k] += a * a;
                    let pa = a as u16;
                    if pa > peak16 { peak16 = pa; }
                    if a <= sil_lim { silence += 1; }
                    if a >= clip_lim { clipping = true; }
                }
            }
            for &v in chunks.remainder() {
                let a = v.unsigned_abs() as i64;
                sum_sq_lanes[0] += a * a;
                let pa = a as u16;
                if pa > peak16 { peak16 = pa; }
                if a <= sil_lim { silence += 1; }
                if a >= clip_lim { clipping = true; }
            }
            frames_left -= take;
        }
        for lane in sum_sq_lanes {
            sum_sq_i += lane;
        }
        let scale2 = scale_inv * scale_inv;
        let d = PyDict::new_bound(py);
        d.set_item("rms", ((sum_sq_i as f64 / frames as f64) / scale2 + 1e-9).sqrt())?;
        d.set_item("peak", peak16 as f64 / scale_inv + 1e-9)?;
        d.set_item("silence_ratio", silence as f64 / frames as f64)?;
        d.set_item("clipping", clipping)?;
        d.set_item("duration", frames as f64 / info.sample_rate as f64)?;
        d.set_item("sample_rate", info.sample_rate)?;
        d.set_item("samples", frames)?;
        return Ok(d.into());
    }

    while frames_left > 0 {
        let take = (buf.len() / bpf).min(frames_left);
        let want = take * bpf;
        f.read_exact(&mut buf[..want]).map_err(|e| perr(&format!("{}", e)))?;
        decoded.clear();
        decode_frames(&buf[..want], info.format, info.bits, info.channels, &mut decoded);
        for &m in &decoded {
            let a = (m as f64).abs();
            sum_sq += a * a;
            if a > peak {
                peak = a;
            }
            if a < silence_thr {
                silence += 1;
            }
            if a > 0.999 {
                clipping = true;
            }
        }
        frames_left -= take;
    }
    let d = PyDict::new_bound(py);
    d.set_item("rms", (sum_sq / frames as f64 + 1e-9).sqrt())?;
    d.set_item("peak", peak + 1e-9)?;
    d.set_item("silence_ratio", silence as f64 / frames as f64)?;
    d.set_item("clipping", clipping)?;
    d.set_item("duration", frames as f64 / info.sample_rate as f64)?;
    d.set_item("sample_rate", info.sample_rate)?;
    d.set_item("samples", frames)?;
    Ok(d)
}

#[pymodule]
fn vc_audio(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute_peaks, m)?)?;
    m.add_function(wrap_pyfunction!(audio_metrics_raw, m)?)?;
    Ok(())
}
