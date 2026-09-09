import sys
import os

sys.argv = ["s2_train.py", "--config", "D:/projects/ai-agent-test/GPT-SoVITS/TEMP/tmp_s2.json"]

os.chdir("D:/projects/ai-agent-test/GPT-SoVITS/GPT_SoVITS")
sys.path.insert(0, "D:/projects/ai-agent-test/GPT-SoVITS/GPT_SoVITS")

import torch
import torch.distributed as dist
import torch.multiprocessing as mp

# Monkey-patch dist.init_process_group to no-op on Windows
_orig_init = dist.init_process_group
def _fake_init(*args, **kwargs):
    pass
dist.init_process_group = _fake_init

# Monkey-patch DDP to be a passthrough wrapper
from torch.nn.parallel import DistributedDataParallel as RealDDP
class FakeDDP(RealDDP):
    def __init__(self, module, **kwargs):
        # Skip RealDDP.__init__, just store the module
        torch.nn.Module.__init__(self)
        self.module = module
    def forward(self, *args, **kwargs):
        return self.module(*args, **kwargs)

import torch.nn.parallel
torch.nn.parallel.DistributedDataParallel = FakeDDP

from s2_train import main
if __name__ == "__main__":
    main()
