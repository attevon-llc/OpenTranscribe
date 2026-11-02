"""GPU/VRAM instrumentation (NVML, torch, MPS polling) used around each diarization run."""

from __future__ import annotations

import threading
import time

from pyannote_direct.config import GPU_INDEX


def get_gpu_info() -> dict | None:
    """Get GPU info via pynvml; fall back to torch if pynvml is unavailable."""
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(GPU_INDEX)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode()
        result = {
            'gpu_name': name,
            'vram_total_mb': round(mem.total / (1024**2)),
            'vram_used_mb': round(mem.used / (1024**2)),
            'vram_free_mb': round(mem.free / (1024**2)),
        }
        pynvml.nvmlShutdown()
        return result
    except Exception:
        pass
    try:
        import torch

        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(GPU_INDEX)
            free, total = torch.cuda.mem_get_info(GPU_INDEX)
            return {
                'gpu_name': props.name,
                'vram_total_mb': round(total / (1024**2)),
                'vram_used_mb': round((total - free) / (1024**2)),
                'vram_free_mb': round(free / (1024**2)),
            }
    except Exception:
        pass
    return None


def get_vram_used_mb() -> int | None:
    """Current VRAM used via NVML (all processes; not per-process)."""
    info = get_gpu_info()
    return info['vram_used_mb'] if info else None


def get_mps_driver_allocated_mb() -> int | None:
    """Current MPS allocator high-water mark for this process."""
    try:
        import torch

        if torch.backends.mps.is_available():
            return int(torch.mps.driver_allocated_memory() / (1024**2))
    except Exception:
        pass
    return None


class MPSVRAMPoller:
    """Poll MPS allocator at fixed cadence; track peak.

    MPS lacks `max_memory_allocated()`; poll `driver_allocated_memory()` on a
    background thread and take the high-water mark. Cadence of 100 ms matches
    the pattern in `vram-probe-diarization-mps.py`.
    """

    def __init__(self, interval_s: float = 0.1):
        self.interval_s = interval_s
        self._peak_mb = 0
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._peak_mb = get_mps_driver_allocated_mb() or 0
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while self._running:
            v = get_mps_driver_allocated_mb()
            if v is not None and v > self._peak_mb:
                self._peak_mb = v
            time.sleep(self.interval_s)

    def peek(self) -> int:
        """Current peak without stopping the thread."""
        v = get_mps_driver_allocated_mb()
        if v is not None and v > self._peak_mb:
            self._peak_mb = v
        return self._peak_mb

    def reset(self) -> None:
        """Reset peak to current allocator value (stage-boundary reset)."""
        self._peak_mb = get_mps_driver_allocated_mb() or 0

    def stop(self) -> int:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        return self._peak_mb


def sync_device(device_str: str) -> None:
    """Flush async kernel queue so subsequent timestamps are accurate."""
    import torch

    if device_str.startswith('cuda'):
        torch.cuda.synchronize()
    elif device_str == 'mps':
        torch.mps.synchronize()


def device_peak_vram_mb(device_str: str, mps_poller: MPSVRAMPoller | None = None) -> int:
    """Device-agnostic peak VRAM (MB) since last reset."""
    import torch

    if device_str.startswith('cuda'):
        device_idx = int(device_str.split(':', 1)[1]) if ':' in device_str else 0
        return int(torch.cuda.max_memory_allocated(device=device_idx) / (1024**2))
    if device_str == 'mps' and mps_poller is not None:
        return mps_poller.peek()
    return 0


def reset_device_peak_vram(device_str: str, mps_poller: MPSVRAMPoller | None = None) -> None:
    """Reset peak counter at a stage boundary."""
    import torch

    if device_str.startswith('cuda'):
        device_idx = int(device_str.split(':', 1)[1]) if ':' in device_str else 0
        torch.cuda.reset_peak_memory_stats(device=device_idx)
    elif device_str == 'mps' and mps_poller is not None:
        mps_poller.reset()


def device_steady_state_vram_mb(
    device_str: str,
    mps_poller: MPSVRAMPoller | None = None,  # noqa: ARG001 (API symmetry)
) -> int:
    """Current (not peak) device-allocated VRAM — sampled after pipeline returns."""
    import torch

    if device_str.startswith('cuda'):
        device_idx = int(device_str.split(':', 1)[1]) if ':' in device_str else 0
        return int(torch.cuda.memory_allocated(device=device_idx) / (1024**2))
    if device_str == 'mps':
        v = get_mps_driver_allocated_mb()
        return v if v is not None else 0
    return 0
