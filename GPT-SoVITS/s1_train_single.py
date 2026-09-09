import sys
import os

sys.argv = ["s1_train.py", "--config_file", "D:/projects/ai-agent-test/GPT-SoVITS/TEMP/tmp_s1.yaml"]

os.chdir("D:/projects/ai-agent-test/GPT-SoVITS/GPT_SoVITS")
sys.path.insert(0, "D:/projects/ai-agent-test/GPT-SoVITS/GPT_SoVITS")

import torch
import pathlib
torch.serialization.add_safe_globals([pathlib.WindowsPath])

import torch.distributed as dist
import torch

# Fake distributed
def _fake_init(*args, **kwargs):
    pass
dist.init_process_group = _fake_init
dist.get_world_size = lambda: 1
dist.get_rank = lambda: 0
dist.is_initialized = lambda: True
dist.barrier = lambda: None

class _FakeGroup:
    def monitored_barrier(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
dist.new_group = lambda *a, **kw: _FakeGroup()
dist.destroy_process_group = lambda *a, **kw: None

# Patch Trainer to force single-process
import pytorch_lightning
_orig_trainer_init = pytorch_lightning.Trainer.__init__
def _patched_trainer_init(self, *args, **kwargs):
    kwargs["strategy"] = "auto"
    kwargs["devices"] = 1
    _orig_trainer_init(self, *args, **kwargs)
pytorch_lightning.Trainer.__init__ = _patched_trainer_init

from s1_train import main
import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config_file", type=str, default="configs/s1longer.yaml")
    args = parser.parse_args()
    main(args)
