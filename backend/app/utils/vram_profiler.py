"""
VRAM Profiling Utility

Context-manager-based profiler that captures per-step VRAM snapshots during
the transcription pipeline. Uses NVML (via ctypes) for accurate device-level
memory tracking that includes non-PyTorch allocations (CTranslate2, CUDA
contexts, etc.).

Enable with ENABLE_VRAM_PROFILING=true environment variable.
"""

import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


def is_profiling_enabled() -> bool:
    """Check if VRAM profiling is enabled via environment variable."""
    return os.getenv("ENABLE_VRAM_PROFILING", "false").lower() == "true"


class VRAMProfiler:
    """Collects per-step VRAM usage and timing data during pipeline execution.

    Uses NVML for device-level memory (captures CTranslate2 etc.) plus
    PyTorch memory stats for framework-level detail (peak, reserved).
    """

    def __init__(self, enabled: bool | None = None):
        self.enabled = enabled if enabled is not None else is_profiling_enabled()
        self.steps: list[dict[str, Any]] = []
        self._cuda_available = False

        if self.enabled:
            try:
                import torch

                self._cuda_available = torch.cuda.is_available()
                if not self._cuda_available:
                    logger.info("VRAM profiling enabled but CUDA not available, timing-only mode")
            except ImportError:
                logger.warning("VRAM profiling enabled but PyTorch not installed")

    def _get_vram_mb(self) -> dict[str, float]:
        """Get current VRAM stats in MB from both NVML and PyTorch."""
        if not self._cuda_available:
            return {}
        import torch

        from app.utils.nvml_monitor import get_gpu_memory

        # NVML: true device-level memory (sees CTranslate2, CUDA contexts, everything)
        nvml = get_gpu_memory()
        device_used_mb = nvml.used_mb if nvml else 0.0
        device_free_mb = nvml.free_mb if nvml else 0.0
        device_total_mb = nvml.total_mb if nvml else 0.0

        # PyTorch: framework-level stats (only sees PyTorch allocations)
        pt_allocated = torch.cuda.memory_allocated(0) / (1024**2)
        pt_reserved = torch.cuda.memory_reserved(0) / (1024**2)
        pt_peak = torch.cuda.max_memory_allocated(0) / (1024**2)

        return {
            "device_used_mb": device_used_mb,
            "device_free_mb": device_free_mb,
            "device_total_mb": device_total_mb,
            "pt_allocated_mb": pt_allocated,
            "pt_reserved_mb": pt_reserved,
            "pt_peak_mb": pt_peak,
        }

    def _reset_peak(self) -> None:
        """Reset PyTorch peak memory stats for next measurement."""
        if self._cuda_available:
            import torch

            torch.cuda.reset_peak_memory_stats(0)

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """Profile a pipeline step.

        Captures NVML device_used (total GPU memory including CTranslate2)
        and PyTorch peak allocation around the step.
        """
        if not self.enabled:
            yield
            return

        self._reset_peak()
        before = self._get_vram_mb()
        start_time = time.perf_counter()

        yield

        elapsed = time.perf_counter() - start_time
        after = self._get_vram_mb()

        device_before = round(before.get("device_used_mb", 0), 1)
        device_after = round(after.get("device_used_mb", 0), 1)
        device_delta = round(device_after - device_before, 1)

        step_data = {
            "name": name,
            "duration_s": round(elapsed, 3),
            # NVML device-level (captures everything)
            "device_used_before_mb": device_before,
            "device_used_after_mb": device_after,
            "device_delta_mb": device_delta,
            "device_free_mb": round(after.get("device_free_mb", 0), 1),
            # PyTorch-level (only torch allocations)
            "pt_allocated_mb": round(after.get("pt_allocated_mb", 0), 1),
            "pt_peak_mb": round(after.get("pt_peak_mb", 0), 1),
            "pt_reserved_mb": round(after.get("pt_reserved_mb", 0), 1),
        }
        self.steps.append(step_data)

        logger.info(
            f"VRAM_PROFILE [{name}]: {elapsed:.1f}s | "
            f"device: {device_before:.0f}→{device_after:.0f}MB "
            f"(Δ{device_delta:+.0f}MB) free={step_data['device_free_mb']:.0f}MB | "
            f"pytorch: alloc={step_data['pt_allocated_mb']:.0f}MB "
            f"peak={step_data['pt_peak_mb']:.0f}MB"
        )

    def snapshot(self, label: str) -> None:
        """Capture a point-in-time VRAM snapshot without timing."""
        if not self.enabled:
            return

        vram = self._get_vram_mb()
        device_used = round(vram.get("device_used_mb", 0), 1)

        snap = {
            "name": f"snapshot:{label}",
            "duration_s": 0,
            "device_used_before_mb": device_used,
            "device_used_after_mb": device_used,
            "device_delta_mb": 0,
            "device_free_mb": round(vram.get("device_free_mb", 0), 1),
            "pt_allocated_mb": round(vram.get("pt_allocated_mb", 0), 1),
            "pt_peak_mb": round(vram.get("pt_peak_mb", 0), 1),
            "pt_reserved_mb": round(vram.get("pt_reserved_mb", 0), 1),
        }
        self.steps.append(snap)
        logger.info(
            f"VRAM_PROFILE [snapshot:{label}]: "
            f"device_used={device_used:.0f}MB "
            f"free={snap['device_free_mb']:.0f}MB "
            f"pt_alloc={snap['pt_allocated_mb']:.0f}MB"
        )

    def get_report(self) -> dict[str, Any]:
        """Get full profiling report."""
        total_duration = sum(s["duration_s"] for s in self.steps)
        peak_device = max((s["device_used_after_mb"] for s in self.steps), default=0)
        peak_pytorch = max((s["pt_peak_mb"] for s in self.steps), default=0)

        return {
            "profiling_enabled": self.enabled,
            "cuda_available": self._cuda_available,
            "total_duration_s": round(total_duration, 3),
            "peak_device_used_mb": round(peak_device, 1),
            "peak_pytorch_mb": round(peak_pytorch, 1),
            "steps": self.steps,
        }

    def save_to_redis(
        self,
        task_id: str,
        audio_duration: float = 0.0,
        num_speakers: int = 0,
    ) -> None:
        """Save profile to Redis for the admin GPU profiles endpoint.

        Stores profile as `gpu:profile:{task_id}` with 24hr TTL,
        and appends the task_id to a capped list `gpu:profile:history`.
        """
        if not self.enabled or not self.steps:
            return

        try:
            from app.core.redis import get_redis

            r = get_redis()
            report = self.get_report()
            report["task_id"] = task_id
            report["audio_duration_s"] = round(audio_duration, 1)
            report["num_speakers"] = num_speakers
            report["timestamp"] = time.time()

            key = f"gpu:profile:{task_id}"
            r.set(key, json.dumps(report), ex=86400)

            history_key = "gpu:profile:history"
            r.lpush(history_key, task_id)
            r.ltrim(history_key, 0, 99)
            r.expire(history_key, 86400)

            logger.info(f"VRAM profile saved to Redis: {key}")
        except Exception as e:
            logger.warning(f"Failed to save VRAM profile to Redis: {e}")

    def save_report(self, output_path: str) -> None:
        """Save profiling report to JSON file."""
        report = self.get_report()
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info(f"VRAM profile saved to {output_path}")

    def log_report(self) -> None:
        """Log the full profiling report at INFO level."""
        if not self.enabled or not self.steps:
            return
        report = self.get_report()
        logger.info(f"VRAM PROFILE REPORT: {json.dumps(report, indent=2)}")
