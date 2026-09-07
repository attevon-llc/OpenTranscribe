"""
Utility tasks for maintenance and system health.

This module contains general utility tasks. Recovery-specific tasks
have been moved to app.tasks.recovery for better organization.
"""

import contextlib
import json
import logging
import os

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.core.redis import get_redis
from app.utils.stats_helpers import format_bytes

logger = logging.getLogger(__name__)


def _env_flag(name: str, *, default: bool = False) -> bool:
    """Read a boolean environment variable, one way for every caller.

    `GPU_SCALE_ENABLED` used `.lower() == "true"` while `GPU_SCALE_DEFAULT_WORKER`
    three lines later required the literal `"1"` — two booleans from the same
    `.env`, parsed differently (issue #458). `GPU_SCALE_DEFAULT_WORKER=true` was
    silently False, and the two documented sources already disagree on the
    spelling: docker-compose.yml sets `0`, `.env.example` sets `1`.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _safe_float(value: str | None) -> float | None:
    """Parse a numeric string to float, returning None for non-numeric values.

    Handles nvidia-smi reporting '[N/A]' or 'N/A' on systems with unified
    memory (e.g. NVIDIA DGX Spark / Blackwell GB10).

    Args:
        value: String to parse, or None.

    Returns:
        Parsed float, or None if the value is not a valid number.
    """
    if value is None:
        return None
    value = value.strip()
    if not value or value.upper() in ("[N/A]", "N/A", "N/A%"):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _query_gpu_memory_torch(device_id: int) -> tuple[float, float] | None:
    """Query GPU memory via torch.cuda.mem_get_info() as a fallback.

    This is needed for systems with unified CPU+GPU memory (e.g. DGX Spark)
    where nvidia-smi reports memory stats as '[N/A]'.

    Args:
        device_id: CUDA device index.

    Returns:
        Tuple of (free_bytes, total_bytes), or None if torch is unavailable.
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        free, total = torch.cuda.mem_get_info(device_id)
        return (float(free), float(total))
    except Exception as e:
        logger.debug(f"torch.cuda.mem_get_info({device_id}) failed: {e}")
        return None


def _query_single_gpu(device_id: int, subprocess_mod, format_bytes) -> dict | None:
    """Query GPU stats for one device, with unified-memory fallback.

    First tries nvidia-smi for full stats. If memory values come back as
    '[N/A]' (unified memory systems like DGX Spark), falls back to
    torch.cuda.mem_get_info() for memory data.

    Args:
        device_id: NVIDIA device index to query.
        subprocess_mod: The subprocess module (passed in to avoid re-import).
        format_bytes: Byte-formatting helper (passed in to avoid closure issues).

    Returns:
        Dict of GPU stats, or None if the query fails.
    """
    try:
        result = subprocess_mod.run(  # noqa: S603 # nosec B603 B607
            [  # noqa: S607
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,memory.free,"
                "utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
                f"--id={device_id}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        parts = result.stdout.strip().split(", ")
        gpu_name = parts[0]
        memory_used_mib = _safe_float(parts[1])
        memory_total_mib = _safe_float(parts[2])
        memory_free_mib = _safe_float(parts[3])
        # int(float(...)), never int(raw) — issue #458. `_safe_float` was used as the
        # GUARD while `int()` took the raw string, so nvidia-smi emitting "37.5" (which
        # it does on some driver/board combinations) passed the guard and then raised
        # ValueError inside int(). That was swallowed by the broad except below and the
        # task reported "No GPU Available" for a perfectly healthy GPU.
        _utilization = _safe_float(parts[4]) if len(parts) > 4 else None
        utilization_percent = int(_utilization) if _utilization is not None else None
        _temperature = _safe_float(parts[5]) if len(parts) > 5 else None
        temperature_celsius = int(_temperature) if _temperature is not None else None

        memory_source = "nvidia-smi"

        # If nvidia-smi returned N/A for memory (unified memory systems),
        # fall back to torch.cuda.mem_get_info()
        if memory_total_mib is None or memory_used_mib is None:
            torch_mem = _query_gpu_memory_torch(device_id)
            if torch_mem is not None:
                free_bytes, total_bytes = torch_mem
                used_bytes = total_bytes - free_bytes
                memory_total = total_bytes
                memory_used = used_bytes
                memory_free = free_bytes
                memory_source = "torch.cuda.mem_get_info"
            else:
                # Both nvidia-smi and torch failed — report what we can
                return {
                    "available": True,
                    "device_id": device_id,
                    "name": gpu_name,
                    "memory_total": "N/A (unified memory)",
                    "memory_used": "N/A",
                    "memory_free": "N/A",
                    "memory_percent": "N/A",
                    "utilization_percent": f"{utilization_percent}%"
                    if utilization_percent is not None
                    else "N/A",
                    "temperature_celsius": temperature_celsius,
                    "memory_source": "unavailable",
                    "memory_note": "Unified memory system — install PyTorch "
                    "for memory stats via torch.cuda.mem_get_info()",
                }
        else:
            memory_used = memory_used_mib * 1024 * 1024
            memory_total = memory_total_mib * 1024 * 1024
            memory_free = (
                memory_free_mib * 1024 * 1024
                if memory_free_mib is not None
                else memory_total - memory_used
            )

        memory_percent = (memory_used / memory_total * 100) if memory_total > 0 else 0

        return {
            "available": True,
            "device_id": device_id,
            "name": gpu_name,
            "memory_total": format_bytes(memory_total),
            "memory_used": format_bytes(memory_used),
            "memory_free": format_bytes(memory_free),
            "memory_percent": f"{memory_percent:.1f}%",
            "utilization_percent": f"{utilization_percent}%"
            if utilization_percent is not None
            else "N/A",
            "temperature_celsius": temperature_celsius,
            "memory_source": memory_source,
        }
    except Exception as e:
        logger.warning(f"nvidia-smi query for device {device_id} failed: {e}")
        return None


@celery_app.task(name="system.update_gpu_stats", bind=True, priority=CPUPriority.SYSTEM)
def update_gpu_stats(self):
    """Periodic task to update GPU statistics in Redis.

    Runs on the cpu worker (which has 'count: all' NVIDIA device access via
    docker-compose.gpu.yml) so it fires independently of long-running GPU
    transcription tasks that hold the gpu queue at concurrency=1.

    Collects stats for every GPU the app is actively using:
      - Normal mode:      GPU_DEVICE_ID only
      - GPU-scale mode:   GPU_SCALE_DEVICE_ID (scaled worker) +
                          GPU_DEVICE_ID (default worker, when GPU_SCALE_DEFAULT_WORKER=1)

    Stores a JSON array in Redis key "gpu_stats" and broadcasts it via WebSocket
    so the frontend can cycle through all active GPUs.

    Returns:
        List of GPU stat dicts (one per active device).
    """
    try:
        import os
        import subprocess

        # Determine which GPU devices the app workers are using.
        # All values come from .env via env_file on the cpu-worker service.
        gpu_scale_enabled = _env_flag("GPU_SCALE_ENABLED", default=False)
        scale_device_id = int(os.environ.get("GPU_SCALE_DEVICE_ID", "2"))
        default_device_id = int(os.environ.get("GPU_DEVICE_ID", "0"))
        scale_default_worker = _env_flag("GPU_SCALE_DEFAULT_WORKER", default=False)

        if gpu_scale_enabled:
            # Scaled worker is always the primary; add default worker if also active.
            device_ids = [scale_device_id]
            if scale_default_worker and default_device_id != scale_device_id:
                device_ids.append(default_device_id)
        else:
            device_ids = [default_device_id]

        gpu_stats_list = []
        for device_id in device_ids:
            stats = _query_single_gpu(device_id, subprocess, format_bytes)
            if stats:
                gpu_stats_list.append(stats)

        if not gpu_stats_list:
            gpu_stats_list = [
                {
                    "available": False,
                    "device_id": device_ids[0] if device_ids else 0,
                    "name": "No GPU Available",
                    "memory_total": "N/A",
                    "memory_used": "N/A",
                    "memory_free": "N/A",
                    "memory_percent": "N/A",
                }
            ]

        # Store array in Redis (10-min TTL; beat runs every 5 min)
        redis_client = celery_app.backend.client
        redis_client.setex("gpu_stats", 600, json.dumps(gpu_stats_list))

        # Broadcast array to all connected WebSocket clients
        try:
            get_redis().publish(
                "websocket_notifications",
                json.dumps(
                    {
                        "type": "gpu_stats_update",
                        "broadcast": True,
                        "data": gpu_stats_list,
                    }
                ),
            )
            logger.debug(f"Broadcast GPU stats for {len(gpu_stats_list)} device(s)")
        except Exception as broadcast_err:
            logger.warning(f"Failed to broadcast GPU stats: {broadcast_err}")

        # Clear debounce lock (best-effort, non-critical)
        with contextlib.suppress(Exception):  # noqa: S110
            redis_client.delete("gpu_stats_pending")

        logger.debug(f"Updated GPU stats in Redis: {gpu_stats_list}")
        return gpu_stats_list

    # There is no `except FileNotFoundError` arm: `_query_single_gpu` catches
    # Exception one frame down, so it was unreachable (issue #458). It also reported
    # `device_id: 0` where the live path reports `device_ids[0]`, so it documented
    # behaviour the product does not have — and its log line is exactly what an
    # operator would grep for after a failure and never find.
    except Exception as e:
        logger.error(f"Error updating GPU stats: {str(e)}")
        fallback = [
            {
                "available": False,
                "device_id": 0,
                "name": "Error",
                "memory_total": "Unknown",
                "memory_used": "Unknown",
                "memory_free": "Unknown",
                "memory_percent": "Unknown",
                "error": str(e),
            }
        ]
        with contextlib.suppress(Exception):
            celery_app.backend.client.setex("gpu_stats", 600, json.dumps(fallback))
        return fallback


# All recovery tasks have been moved to app.tasks.recovery
