"""GPT-SoVITS 训练管线驱动（集成在 VoiceCut 仓库内，经 junction 指向 D 盘本体）。

只以子进程方式驱动 GPT-SoVITS 的脚本/服务，不改其源码：
- 数据集导出：按角色复用 app.dataset.export_dataset → <root>/logs/<exp>/ + list.txt
- 预处理    ：复刻 webui open1abc 的环境变量与命令（1/2/3-get-*.py）
- 训练      ：以 TEMP/tmp_s1.yaml、tmp_s2.json 为模板替换后写回 → s1/s2_train_single.py
- 试听      ：生成 tts_infer.yaml → 懒启动 api_v2.py → POST /tts

约定：
- settings 存于 workdir/settings.json（root/python/exp_root/api_port/version/language/epochs/val_ratio）
- 所有 GPT-SoVITS 子进程 cwd = GPT-SoVITS 根目录
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.log import get_logger

_log = get_logger()

# 语言标签：VoiceCut(JP/ZH/EN) -> GPT-SoVITS(ja/zh/en)
LANG_MAP = {"JP": "ja", "ZH": "zh", "EN": "en", "JA": "ja", "JA-JP": "ja"}


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_root() -> Path:
    return repo_root() / "GPT-SoVITS"


def default_settings(root: str | Path | None = None) -> dict:
    root = Path(root) if root else default_root()
    return {
        "root": str(root),
        "python": detect_python(root),
        "exp_root": "logs",
        "api_port": 9880,
        "version": "v2",
        "language": "ja",
        "epochs_s1": None,
        "epochs_s2": None,
        "val_ratio": 0.0,
    }


def detect_python(root: str | Path) -> str | None:
    root = Path(root)
    for cand in (root / ".venv" / "Scripts" / "python.exe",
                 root / "runtime" / "python.exe",
                 root / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


# ── 设置读写 ────────────────────────────────────────────
def settings_path(workdir: str | Path) -> Path:
    return Path(workdir) / "settings.json"


def load_settings(workdir: str | Path) -> dict:
    p = settings_path(workdir)
    data: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
    base = default_settings()
    base.update({k: v for k, v in (data or {}).items() if v is not None})
    if not base.get("python"):
        base["python"] = detect_python(base.get("root") or default_root())
    return base


def save_settings(workdir: str | Path, data: dict) -> dict:
    p = settings_path(workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    merged = load_settings(workdir)
    for k in ("root", "python", "exp_root", "api_port", "version",
              "language", "epochs_s1", "epochs_s2", "val_ratio"):
        if k in data and data[k] is not None:
            merged[k] = data[k]
    p.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def require_root(settings: dict) -> Path:
    root = Path(settings.get("root") or default_root())
    if not root.exists():
        raise RuntimeError(
            f"GPT-SoVITS 根目录不存在: {root}\n"
            "请确认 junction（VoiceCut\\GPT-SoVITS → D:\\projects\\ai-agent-test\\GPT-SoVITS）已建立，"
            "或在「训练交付 → 配置」里修改 GPT-SoVITS 根目录。")
    return root


def resolve(settings: dict, *parts: str) -> str:
    return str(Path(require_root(settings), *parts))


def exp_dir(settings: dict, exp: str) -> Path:
    root = require_root(settings)
    return root / (settings.get("exp_root") or "logs") / exp


# ── 名称 / 语言 ─────────────────────────────────────────
def sanitize(name: str) -> str:
    out = []
    for ch in str(name or ""):
        out.append("_" if (ch in '/\\:*?"<>|\x00' or ch.isspace()) else ch)
    s = "".join(out).strip("._ ") or "speaker"
    return s[:48]


def unique_exp(settings: dict, name: str) -> str:
    base = sanitize(name)
    cand = base
    i = 2
    while exp_dir(settings, cand).exists():
        cand = f"{base}_{i}"
        i += 1
    return cand


def lang_map(code: str | None) -> str:
    return LANG_MAP.get((code or "").upper(), (code or "ja").lower())


# ── 数据集导出（按角色） ─────────────────────────────────
def build_dataset(
    settings: dict,
    exp: str,
    segments: list,
    sources: dict,
    *,
    speaker: str = "",
    language: str = "ja",
    val_ratio: float = 0.0,
    tasks=None,
    task_id: str | None = None,
) -> dict:
    from app.dataset import export_dataset  # 延迟导入（避免循环依赖）

    out_dir = exp_dir(settings, exp)
    out = export_dataset(
        sources, segments, out_dir,
        speaker=speaker or exp, language=language,
        sample_rate=32000, trim=True, normalize=True,
        layout="flat", tasks=tasks, task_id=task_id,
    )
    # 预留验证集：每 N 条抽 1 条进 val_list.txt（确定性，与现有导出语义一致）
    if val_ratio and val_ratio > 0:
        list_file = Path(out["list_file"])
        lines = [ln for ln in list_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        n = max(1, int(round(1.0 / val_ratio)))
        val_lines = [ln for i, ln in enumerate(lines) if (i + 1) % n == 0]
        train_lines = [ln for i, ln in enumerate(lines) if (i + 1) % n != 0]
        list_file.write_text("\n".join(train_lines) + ("\n" if train_lines else ""), encoding="utf-8")
        val_list = list_file.with_name("val_list.txt")
        val_list.write_text("\n".join(val_lines) + ("\n" if val_lines else ""), encoding="utf-8")
        out["val_holdout"] = len(val_lines)
        out["train_after_holdout"] = len(train_lines)
    return out


# ── 预处理（复刻 webui open1abc） ────────────────────────
def _s2_pretrained_s2g(settings: dict, version: str = "v2") -> str:
    root = require_root(settings)
    cands = [
        root / "TEMP" / "tmp_s2.json",
        root / "GPT_SoVITS" / "configs" / "s2.json",
    ]
    for p in cands:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            v = data.get("train", {}).get("pretrained_s2G")
            if v:
                return str(v)
        except Exception:  # noqa: BLE001
            continue
    # 兜底：官方 v2 预训练路径
    return str(root / "GPT_SoVITS" / "pretrained_models" /
               "gsv-v2final-pretrained" / "s2G2333k.pth")


def preprocess_steps(settings: dict, exp: str, version: str = "v2") -> list[dict]:
    root = require_root(settings)
    expd = exp_dir(settings, exp)
    inp_text = str(expd / "list.txt")
    gpu = "0"
    common = {"i_part": "0", "all_parts": "1", "_CUDA_VISIBLE_DEVICES": gpu}
    s2config = str(root / "GPT_SoVITS" / "configs" / "s2.json")
    if version in ("v2Pro", "v2ProPlus"):
        s2config = str(root / "GPT_SoVITS" / "configs" / f"s2{version}.json")
    steps = [
        {
            "name": "1-文本/格式化",
            "script": "GPT_SoVITS/prepare_datasets/1-get-text.py",
            "env": dict(common,
                        inp_text=inp_text, inp_wav_dir=str(expd), exp_name=exp,
                        opt_dir=str(expd),
                        bert_pretrained_dir=str(root / "GPT_SoVITS" / "pretrained_models" /
                                                "chinese-roberta-wwm-ext-large"),
                        is_half="True"),
            "merge": ("2-name2text", [0], ".txt", "2-name2text.txt"),
        },
        {
            "name": "2-语义SSL特征",
            "script": "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py",
            "env": dict(common,
                        inp_text=inp_text, inp_wav_dir=str(expd), exp_name=exp,
                        opt_dir=str(expd),
                        cnhubert_base_dir=str(root / "GPT_SoVITS" / "pretrained_models" /
                                              "chinese-hubert-base"),
                        sv_path=str(root / "GPT_SoVITS" / "pretrained_models" / "sv" /
                                    "pretrained_eres2netv2w24s4ep4.ckpt"),
                        is_half="True"),
            "merge": None,
        },
        {
            "name": "3-语义Token",
            "script": "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
            "env": dict(common,
                        inp_text=inp_text, exp_name=exp, opt_dir=str(expd),
                        pretrained_s2G=_s2_pretrained_s2g(settings, version),
                        s2config_path=s2config, is_half="True"),
            "merge": ("6-name2semantic", [0], ".tsv", "6-name2semantic.tsv"),
        },
    ]
    return steps


def _merge_parts(expd: Path, prefix: str, parts: list[int], suffix: str, outname: str) -> None:
    lines: list[str] = []
    for p in parts:
        f = expd / f"{prefix}-{p}{suffix}"
        if f.exists():
            lines += [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
            try:
                f.unlink()
            except OSError:
                pass
    (expd / outname).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def run_preprocess(settings: dict, exp: str, *, version: str = "v2",
                   log_cb=None, cancelled_cb=None, progress_cb=None,
                   start_progress: float = 0.0, span: float = 1.0) -> None:
    steps = preprocess_steps(settings, exp, version=version)
    n = max(1, len(steps))
    for i, step in enumerate(steps):
        if cancelled_cb and cancelled_cb():
            from app.tasks import TaskCancelled
            raise TaskCancelled()
        if progress_cb:
            progress_cb(start_progress + span * (i / n))
        run_step(settings, step, log_cb=log_cb, cancelled_cb=cancelled_cb)
        merge = step.get("merge")
        if merge:
            _merge_parts(exp_dir(settings, exp), *merge)
    if progress_cb:
        progress_cb(start_progress + span)


# ── 子进程执行 ──────────────────────────────────────────
def run_step(settings: dict, step: dict, *, log_cb=None, cancelled_cb=None) -> None:
    root = require_root(settings)
    env = dict(os.environ)
    env.update({k: str(v) for k, v in step["env"].items()})
    cmd = [settings["python"], step["script"]]
    _run_proc(cmd, cwd=root, env=env, label=step["name"],
              log_cb=log_cb, cancelled_cb=cancelled_cb)


def run_training(settings: dict, stage: str, *, log_cb=None, cancelled_cb=None) -> None:
    root = require_root(settings)
    script = "s2_train_single.py" if stage == "s2" else "s1_train_single.py"
    _run_proc([settings["python"], script], cwd=root, env=dict(os.environ),
              label="SoVITS(S2)" if stage == "s2" else "GPT(S1)",
              log_cb=log_cb, cancelled_cb=cancelled_cb)


def _run_proc(cmd, cwd, env, *, label: str, log_cb=None, cancelled_cb=None) -> None:
    from app.tasks import TaskCancelled

    log_cb = log_cb or (lambda s: None)
    log_cb(f"== {label} 开始 ==")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=flags,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if cancelled_cb and cancelled_cb():
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            proc.wait()
            raise TaskCancelled()
        line = line.rstrip("\n")
        if line.strip():
            log_cb(line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"{label} 失败（exit={proc.returncode}）")
    log_cb(f"== {label} 完成 ==")


# ── 训练配置（模板替换） ─────────────────────────────────
def write_training_configs(settings: dict, exp: str, *,
                           epochs_s1: int | None = None,
                           epochs_s2: int | None = None) -> tuple[str, str]:
    root = require_root(settings)
    temp = root / "TEMP"
    expd = exp_dir(settings, exp)
    s1p = temp / "tmp_s1.yaml"
    s2p = temp / "tmp_s2.json"
    if not s1p.exists():
        raise RuntimeError(f"缺少 S1 配置模板: {s1p}")
    if not s2p.exists():
        raise RuntimeError(f"缺少 S2 配置模板: {s2p}")

    s1_text = s1p.read_text(encoding="utf-8")
    repl = {
        "exp_name": exp,
        "training_files": str(expd / "list.txt").replace("\\", "/"),
        "exp_dir": str(expd).replace("\\", "/"),
        "output_dir": str(expd / "logs_s1_v2").replace("\\", "/"),
        "train_semantic_path": str(expd / "6-name2semantic.tsv").replace("\\", "/"),
        "train_phoneme_path": str(expd / "2-name2text.txt").replace("\\", "/"),
        "half_weights_save_dir": str(root / "GPT_weights_v2").replace("\\", "/"),
    }
    if epochs_s1:
        repl["epochs"] = str(int(epochs_s1))
    for key, val in repl.items():
        s1_text = re.sub(
            r"(?m)^(\s*%s\s*:\s*).*$" % re.escape(key),
            lambda m: m.group(1) + str(val), s1_text)
    s1p.write_text(s1_text, encoding="utf-8")

    s2p_text = s2p.read_text(encoding="utf-8")
    data = json.loads(s2p_text)
    data["name"] = exp
    tr = data.setdefault("train", {})
    tr["name"] = exp
    tr["exp_dir"] = str(expd).replace("\\", "/")
    tr["save_weight_dir"] = str(root / "SoVITS_weights_v2").replace("\\", "/")
    if epochs_s2:
        tr["epochs"] = int(epochs_s2)
    s2p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(s1p), str(s2p)


# ── 权重发现 ────────────────────────────────────────────
def _latest(files, pat: str):
    best = None
    best_epoch = -1
    for f in files:
        m = re.search(pat, f.name)
        ep = int(m.group(1)) if m else -1
        if best is None or ep > best_epoch or (ep == best_epoch and f.stat().st_mtime > best.stat().st_mtime):
            best, best_epoch = f, ep
    return best


def discover_weights(settings: dict, exp: str) -> dict:
    root = require_root(settings)
    gpt_dir = root / "GPT_weights_v2"
    sov_dir = root / "SoVITS_weights_v2"
    gpt = _latest(gpt_dir.glob(f"*{exp}*.ckpt"), r"e(\d+)") if gpt_dir.exists() else None
    sov = _latest(sov_dir.glob(f"*{exp}*infer*.pth"), r"_e(\d+)_") if sov_dir.exists() else None
    return {"gpt": str(gpt) if gpt else None, "sovits": str(sov) if sov else None}


# ── 试听（api_v2 服务管理 + POST /tts） ──────────────────
_api_proc: subprocess.Popen | None = None
_api_sig: str | None = None


def write_tts_infer(settings: dict, exp: str, weights: dict | None = None) -> str:
    root = require_root(settings)
    w = weights or discover_weights(settings, exp)
    if not w.get("gpt") or not w.get("sovits"):
        raise RuntimeError(f"角色「{exp}」还没有训练好的权重（GPT/SSoVITS 需都存在）")
    version = settings.get("version") or "v2"
    yaml_text = (
        "custom:\n"
        f"  bert_base_path: {str(root / 'GPT_SoVITS' / 'pretrained_models' / 'chinese-roberta-wwm-ext-large').replace(chr(92), '/')}\n"
        f"  cnhuhbert_base_path: {str(root / 'GPT_SoVITS' / 'pretrained_models' / 'chinese-hubert-base').replace(chr(92), '/')}\n"
        "  device: cuda\n"
        "  is_half: true\n"
        f"  t2s_weights_path: {w['gpt'].replace(chr(92), '/')}\n"
        f"  version: {version}\n"
        f"  vits_weights_path: {w['sovits'].replace(chr(92), '/')}\n"
    )
    temp = root / "TEMP"
    temp.mkdir(parents=True, exist_ok=True)
    yaml_path = temp / f"tts_infer_{exp}.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")
    return str(yaml_path)


def api_running(settings: dict) -> bool:
    return _api_proc is not None and _api_proc.poll() is None


def stop_api() -> None:
    global _api_proc, _api_sig
    if _api_proc is not None and _api_proc.poll() is None:
        try:
            _api_proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            _api_proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            try:
                _api_proc.kill()
            except Exception:  # noqa: BLE001
                pass
    _api_proc = None
    _api_sig = None


def start_api(settings: dict, exp: str, *, log_cb=None) -> bool:
    global _api_proc, _api_sig
    root = require_root(settings)
    sig = exp
    if api_running(settings) and _api_sig == sig:
        return True
    stop_api()
    log_cb = log_cb or (lambda s: None)
    yaml = write_tts_infer(settings, exp)
    port = int(settings.get("api_port") or 9880)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    log_cb("启动 GPT-SoVITS 推理服务（首次加载模型较慢）…")
    _api_proc = subprocess.Popen(
        [settings["python"], "api_v2.py", "-a", "127.0.0.1",
         "-p", str(port), "-c", yaml],
        cwd=str(root), env=dict(os.environ),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=flags,
    )
    _api_sig = sig
    url = f"http://127.0.0.1:{port}/"
    waited = 0.0
    while waited < 240:
        if _api_proc.poll() is not None:
            out = ""
            try:
                if _api_proc.stdout:
                    out = _api_proc.stdout.read()[-2000:]
            except Exception:  # noqa: BLE001
                pass
            _api_proc = None
            _api_sig = None
            raise RuntimeError("GPT-SoVITS 推理服务启动失败:\n" + out)
        try:
            urllib.request.urlopen(url, timeout=1)
            log_cb("推理服务就绪")
            return True
        except urllib.error.HTTPError:
            log_cb("推理服务就绪")
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
            waited += 0.5
            if int(waited) % 15 == 0:
                log_cb(f"等待推理服务就绪… {int(waited)}s")
    raise RuntimeError("GPT-SoVITS 推理服务启动超时（120s）")


def infer(settings: dict, exp: str, *, text: str, ref_wav: str,
          prompt_text: str, text_lang: str = "ja", prompt_lang: str = "ja",
          log_cb=None) -> bytes:
    start_api(settings, exp, log_cb=log_cb)
    port = int(settings.get("api_port") or 9880)
    body = {
        "text": text,
        "text_lang": text_lang,
        "ref_audio_path": str(ref_wav),
        "prompt_text": prompt_text,
        "prompt_lang": prompt_lang,
        "top_k": 15, "top_p": 1.0, "temperature": 1.0,
        "text_split_method": "cut5", "batch_size": 1,
        "batch_threshold": 0.75, "split_bucket": True,
        "speed_factor": 1.0, "fragment_interval": 0.3,
        "seed": -1, "parallel_infer": True,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/tts",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"合成失败（HTTP {exc.code}）: {detail}") from exc
