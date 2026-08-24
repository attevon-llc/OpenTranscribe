"""Redaction compute-device resolution — automatic, runtime, VRAM-aware.

Policy (``REDACTION_DEVICE``): ``auto`` (default) uses the GPU whenever it has enough free
VRAM and falls back to CPU otherwise; ``cpu`` forces CPU; ``cuda[:N]`` prefers that GPU.

The decision is made **at runtime per scan** (cheap ``mem_get_info`` probe), so the worker
adapts to GPU pressure from transcription/diarization **without any restart** — the
toxicity model is moved GPU↔CPU on demand. GPU inference is serialized so peak VRAM is
bounded to one inference even under concurrency.
"""

from __future__ import annotations

import contextlib
import logging
import threading

from app.core import constants as C  # noqa: N812

logger = logging.getLogger(__name__)

# Serializes model inference when running on GPU so only ONE inference is in flight at a
# time — peak VRAM stays bounded to a single inference even under concurrency. No-op on CPU.
_gpu_inference_lock = threading.Lock()


def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def _free_gb(idx: int) -> float:
    import torch

    free_bytes, _ = torch.cuda.mem_get_info(idx)
    return float(free_bytes) / 1e9


def _best_gpu() -> tuple[int, float] | None:
    """Return (index, free_gb) of the visible GPU with the most free VRAM, or None."""
    try:
        import torch

        n = torch.cuda.device_count()
        if n == 0:
            return None
        best = max(range(n), key=lambda i: _free_gb(i))
        return best, _free_gb(best)
    except Exception as exc:  # noqa: BLE001
        logger.debug("GPU probe failed: %s", exc)
        return None


def resolve_device() -> str:
    """Resolve the device to run redaction models on RIGHT NOW: 'cpu' or 'cuda:N'.

    Re-evaluated on every call (cheap), so the choice tracks live GPU availability.
    """
    policy = (C.REDACTION_DEVICE or "auto").lower()
    if policy == "cpu":
        return "cpu"
    if not _cuda_available():
        return "cpu"

    need = C.REDACTION_MIN_FREE_VRAM_GB
    try:
        if policy.startswith("cuda"):
            # Explicit device preference.
            idx = int(policy.split(":", 1)[1]) if ":" in policy else 0
            free = _free_gb(idx)
            if free >= need:
                return f"cuda:{idx}"
            logger.info(
                "Redaction: cuda:%d has %.2f GB free < %.2f GB — using CPU this scan",
                idx,
                free,
                need,
            )
            return "cpu"

        # auto: pick the GPU with the most free VRAM if it clears the bar.
        best = _best_gpu()
        if best and best[1] >= need:
            return f"cuda:{best[0]}"
        if best:
            logger.info(
                "Redaction(auto): best GPU has %.2f GB free < %.2f GB — using CPU this scan",
                best[1],
                need,
            )
        return "cpu"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redaction device probe failed (%s) — using CPU", exc)
        return "cpu"


def inference_guard():
    """Context manager: serialize on GPU (bounded VRAM), no-op on CPU."""
    if resolve_device().startswith("cuda"):
        return _gpu_inference_lock
    return contextlib.nullcontext()
