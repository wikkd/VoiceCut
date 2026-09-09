"""gptsovits \u9a71\u52a8\u5c42\u5355\u5143\u6d4b\u8bd5\uff08\u7eaf\u51fd\u6570/\u914d\u7f6e\u66ff\u6362/\u8def\u5f84/\u63a5\u53e3\uff0c\u4e0d\u771f\u8dd1\u8bad\u7ec3\uff09\u3002"""
from __future__ import annotations

import json
from pathlib import Path

from app import gptsovits
from app.dataset import DatasetSegment


def _fake_root(tmp_path: Path) -> Path:
    root = tmp_path / "gpt"
    for d in [
        "GPT_SoVITS/prepare_datasets",
        "GPT_SoVITS/configs",
        "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        "GPT_SoVITS/pretrained_models/chinese-hubert-base",
        "GPT_SoVITS/pretrained_models/sv",
        "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained",
        "TEMP", "logs", "GPT_weights_v2", "SoVITS_weights_v2",
    ]:
        (root / d).mkdir(parents=True, exist_ok=True)
    return root


def _settings(root: Path) -> dict:
    return {"root": str(root), "python": "py", "exp_root": "logs",
            "api_port": 9880, "version": "v2"}


def test_sanitize_and_lang_map() -> None:
    assert gptsovits.sanitize("A/B*C?") == "A_B_C"
    assert gptsovits.sanitize("  ") == "speaker"
    assert gptsovits.lang_map("JP") == "ja"
    assert gptsovits.lang_map("ZH") == "zh"
    assert gptsovits.lang_map("EN") == "en"
    assert gptsovits.lang_map("ja") == "ja"
    assert gptsovits.lang_map("") == "ja"


def test_settings_roundtrip(tmp_path: Path) -> None:
    s = gptsovits.load_settings(tmp_path)
    assert s["exp_root"] == "logs" and s["api_port"] == 9880
    s2 = gptsovits.save_settings(tmp_path, {"api_port": 9999, "language": "zh"})
    assert s2["api_port"] == 9999 and s2["language"] == "zh"
    s3 = gptsovits.load_settings(tmp_path)
    assert s3["api_port"] == 9999 and s3["language"] == "zh"


def test_unique_exp(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    settings = _settings(root)
    assert gptsovits.unique_exp(settings, "\u89d2\u8272 A") == "\u89d2\u8272_A"
    gptsovits.exp_dir(settings, "\u89d2\u8272_A").mkdir(parents=True, exist_ok=True)
    assert gptsovits.unique_exp(settings, "\u89d2\u8272 A") == "\u89d2\u8272_A_2"


def test_preprocess_steps_env(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    settings = _settings(root)
    steps = gptsovits.preprocess_steps(settings, "exp1", version="v2")
    assert [s["name"] for s in steps] == [
        "1-\u6587\u672c/\u683c\u5f0f\u5316", "2-\u8bed\u4e49SSL\u7279\u5f81", "3-\u8bed\u4e49Token"]
    e0 = steps[0]["env"]
    assert e0["exp_name"] == "exp1" and e0["i_part"] == "0" and e0["all_parts"] == "1"
    assert e0["opt_dir"] == str(root / "logs" / "exp1")
    assert e0["bert_pretrained_dir"] == str(root / "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large")
    e2 = steps[2]["env"]
    assert e2["s2config_path"] == str(root / "GPT_SoVITS" / "configs" / "s2.json")


def test_write_training_configs(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    (root / "TEMP" / "tmp_s1.yaml").write_text(
        "train:\n  epochs: 15\n  exp_name: madoka\n  half_weights_save_dir: D:/old/GPT_weights_v2\n"
        "data:\n  training_files: D:/old/logs/madoka/list.txt\n  exp_dir: D:/old/logs/madoka\n"
        "output_dir: D:/old/logs/madoka/logs_s1_v2\n"
        "train_semantic_path: D:/old/logs/madoka/6-name2semantic.tsv\n"
        "train_phoneme_path: D:/old/logs/madoka/2-name2text.txt\n", encoding="utf-8")
    (root / "TEMP" / "tmp_s2.json").write_text(json.dumps({
        "name": "madoka",
        "train": {"name": "madoka", "exp_dir": "D:/old",
                  "save_weight_dir": "D:/old/SoVITS_weights_v2", "epochs": 8,
                  "pretrained_s2G": "D:/p/s2G.pth"},
    }), encoding="utf-8")
    settings = _settings(root)
    (root / "logs" / "exp1").mkdir(parents=True, exist_ok=True)
    gptsovits.write_training_configs(settings, "exp1", epochs_s1=5, epochs_s2=3)
    s1 = (root / "TEMP" / "tmp_s1.yaml").read_text(encoding="utf-8")
    assert "epochs: 5" in s1 and "exp_name: exp1" in s1
    assert str(root / "logs" / "exp1" / "list.txt").replace("\\", "/") in s1
    s2 = json.loads((root / "TEMP" / "tmp_s2.json").read_text(encoding="utf-8"))
    assert s2["name"] == "exp1" and s2["train"]["epochs"] == 3
    assert str(root / "SoVITS_weights_v2").replace("\\", "/") in s2["train"]["save_weight_dir"]


def test_discover_weights(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    settings = _settings(root)
    (root / "GPT_weights_v2" / "madoka-e5.ckpt").write_bytes(b"x")
    (root / "GPT_weights_v2" / "madoka-e10.ckpt").write_bytes(b"x")
    (root / "SoVITS_weights_v2" / "madoka_e8_s0_infer.pth").write_bytes(b"x")
    w = gptsovits.discover_weights(settings, "madoka")
    assert "madoka-e10.ckpt" in (w["gpt"] or "")
    assert "madoka_e8_s0_infer.pth" in (w["sovits"] or "")


def test_build_dataset_val_holdout(sample_wav: Path, tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    settings = _settings(root)
    segs = [DatasetSegment(start=0.1 + i * 0.05, end=1.2 + i * 0.05,
                           text=f"text{i}", item_id="i1", language="ja",
                           speaker="role1") for i in range(4)]
    out = gptsovits.build_dataset(settings, "exp1", segs, {"i1": str(sample_wav)},
                                  speaker="role1", language="ja", val_ratio=0.25)
    list_file = root / "logs" / "exp1" / "list.txt"
    assert list_file.exists()
    train_lines = [ln for ln in list_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    val_file = root / "logs" / "exp1" / "val_list.txt"
    assert val_file.exists()
    val_lines = [ln for ln in val_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(train_lines) + len(val_lines) == out["count"] == 4
    assert out["val_holdout"] == len(val_lines) == 1
    for ln in train_lines + val_lines:
        assert ln.split("|")[2] == "ja"


def test_training_api_endpoints(tmp_path: Path) -> None:
    from app.config import AppConfig
    from app.server import create_app

    cfg = AppConfig(workdir=tmp_path)
    app = create_app(cfg)
    c = app.test_client()
    r = c.get("/api/training/config")
    assert r.status_code == 200
    r = c.get("/api/training/status")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and j["roles"] == []
    r = c.post("/api/training/config", json={"settings": {"api_port": 9999}})
    assert r.status_code == 200
    assert r.get_json()["settings"]["api_port"] == 9999
    r = c.get("/api/training/weights")
    assert r.status_code == 200 and r.get_json() == []
