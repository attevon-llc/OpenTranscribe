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
# Handles are cached PER DEVICE. A single global handle meant the first caller's index
# won every later query, so `update_gpu_stats` — the one caller that legitimately reads
# more than one card (GPU_DEVICE_ID and GPU_SCALE_DEVICE_ID under --gpu-scale) — silently
# reported device 0's memory for every device it asked about.
_handles: dict[int, ctypes.c_void_p] = {}


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
    """Initialize NVML and cache a handle for `device`. Returns True on success.

    NVML indices are PCI/nvidia-smi ordered and are NOT affected by
    CUDA_VISIBLE_DEVICES, so `device` here is the same index Docker's `device_ids` and
    `GPU_DEVICE_ID` use. With CUDA_DEVICE_ORDER=PCI_BUS_ID pinned in the image, it is
    also the same number as the CUDA ordinal, which is what lets callers pass
    `HardwareConfig.device_index` straight through.
    """
    global _nvml
    if _nvml is False:
        return False

    try:
        if _nvml is None:
            _nvml = ctypes.CDLL("libnvidia-ml.so.1")
            _nvml.nvmlInit_v2()
        if device not in _handles:
            handle = ctypes.c_void_p()
            rc = _nvml.nvmlDeviceGetHandleByIndex(device, ctypes.byref(handle))
            if rc != 0:
                logger.debug(f"NVML handle for device {device} unavailable (rc={rc})")
                return False
            _handles[device] = handle
        return True
    except Exception as e:
        logger.debug(f"NVML init failed: {e}")
        _nvml = False  # type: ignore[assignment]
        _handles.clear()
        return False


def get_gpu_memory(device: int = 0) -> GpuMemory | None:
    """Get current GPU memory usage via NVML.

    Returns GpuMemory with total/used/free in MB, or None if unavailable.
    """
    if not _ensure_init(device):
        return None

    try:
        mem = _NvmlMemory()
        _nvml.nvmlDeviceGetMemoryInfo(_handles[device], ctypes.byref(mem))  # type: ignore[union-attr]
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
