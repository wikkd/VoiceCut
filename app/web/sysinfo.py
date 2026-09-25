"""系统性能监控路由（CPU / 内存 / GPU，供顶部导航栏性能显示）。"""
from __future__ import annotations

import psutil
from flask import Blueprint, jsonify

bp = Blueprint("sysinfo", __name__)

# 首次 cpu_percent(interval=None) 恒为 0.0，导入时先采样一次打底
psutil.cpu_percent(interval=None)
_gpu = None  # None=未初始化；False=不可用（无 N 卡/驱动缺失，永久降级）；dict=就绪


def _gpu_init() -> None:
    global _gpu
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(h)
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        _gpu = {"nvml": pynvml, "handle": h, "name": name}
    except Exception:  # noqa: BLE001
        _gpu = False


def _gpu_info() -> dict | None:
    if _gpu is False:
        return None
    if _gpu is None:
        _gpu_init()
        if _gpu is False:
            return None
    try:
        nvml = _gpu["nvml"]
        h = _gpu["handle"]
        util = nvml.nvmlDeviceGetUtilizationRates(h)
        mem = nvml.nvmlDeviceGetMemoryInfo(h)
        return {"name": _gpu["name"], "util": int(util.gpu),
                "vram_used": int(mem.used), "vram_total": int(mem.total)}
    except Exception:  # noqa: BLE001  NVML 运行期异常（驱动重置等）→ 本轮跳过
        return None


@bp.get("/api/sysperf")
def api_sysperf() -> object:
    vm = psutil.virtual_memory()
    return jsonify({
        "cpu": psutil.cpu_percent(interval=None),
        "cpu_cores": psutil.cpu_count(),
        "mem": vm.percent,
        "mem_used": vm.used,
        "mem_total": vm.total,
        "gpu": _gpu_info(),
    })
