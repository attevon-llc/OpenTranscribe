"""Lightweight NVML GPU memory monitor.

Uses libnvidia-ml.so directly via ctypes — no pip dependency needed.
Captures true device-level memory including non-PyTorch allocations
(CTranslate2, CUDA contexts, etc.) that torch.cuda.memory_allocated()
misses entirely.
"""

import ctypes
import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)

_nvml = None
_handle = None


class GpuMemory(NamedTuple):
    """GPU memory snapshot in MB."""

    total_mb: float
    used_mb: float
    free_mb: float


class _NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


def _ensure_init(device: int = 0) -> bool:
    """Initialize NVML library and get device handle. Returns True on success."""
    global _nvml, _handle
    if _nvml is not None:
        return _handle is not None

    try:
        _nvml = ctypes.CDLL("libnvidia-ml.so.1")
        _nvml.nvmlInit_v2()
        _handle = ctypes.c_void_p()
        _nvml.nvmlDeviceGetHandleByIndex(device, ctypes.byref(_handle))
        return True
    except Exception as e:
        logger.debug(f"NVML init failed: {e}")
        _nvml = False  # type: ignore[assignment]
        _handle = None
        return False


def get_gpu_memory(device: int = 0) -> GpuMemory | None:
    """Get current GPU memory usage via NVML.

    Returns GpuMemory with total/used/free in MB, or None if unavailable.
    """
    if not _ensure_init(device):
        return None

    try:
        mem = _NvmlMemory()
        _nvml.nvmlDeviceGetMemoryInfo(_handle, ctypes.byref(mem))  # type: ignore[union-attr]
        return GpuMemory(
            total_mb=mem.total / (1024**2),
            used_mb=mem.used / (1024**2),
            free_mb=mem.free / (1024**2),
        )
    except Exception as e:
        logger.debug(f"NVML memory query failed: {e}")
        return None


def get_used_mb(device: int = 0) -> float:
    """Convenience: get GPU used memory in MB, or 0.0 if unavailable."""
    mem = get_gpu_memory(device)
    return mem.used_mb if mem else 0.0
