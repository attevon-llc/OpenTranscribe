"""Host metrics helpers (uptime, memory, CPU, disk, GPU) for the admin and system routes."""

import logging
from datetime import UTC
from datetime import datetime

import psutil

from app.core.config import settings
from app.core.queues import CeleryQueues
from app.utils.stats_helpers import format_bytes

# No basicConfig here — this module is imported via the API router before
# configure_logging() runs; a default root handler would double every log line.
logger = logging.getLogger(__name__)


# System statistics utility functions
def get_system_uptime():
    """Get system uptime in a readable format"""
    try:
        # Get boot time and calculate uptime
        boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
        uptime = datetime.now(UTC) - boot_time

        # Format as days, hours, minutes, seconds
        days, remainder = divmod(uptime.total_seconds(), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        if days > 0:
            return f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"
        else:
            return f"{int(hours)}h {int(minutes)}m {int(seconds)}s"
    except Exception as e:
        logger.exception(f"Error getting system uptime: {e}")
        return "Unknown"


def get_memory_usage():
    """Get system memory usage"""
    try:
        # Get virtual memory statistics
        memory = psutil.virtual_memory()

        # Return a dictionary with detailed information
        return {
            "total": format_bytes(memory.total),
            "available": format_bytes(memory.available),
            "used": format_bytes(memory.used),
            "percent": f"{memory.percent}%",
        }
    except Exception as e:
        logger.exception(f"Error getting memory usage: {e}")
        return {
            "total": "Unknown",
            "available": "Unknown",
            "used": "Unknown",
            "percent": "Unknown",
        }


def get_cpu_usage():
    """Get CPU usage information.

    Uses interval=None (non-blocking) which returns CPU usage since the last call.
    The first call after import returns 0.0; subsequent calls return meaningful values.
    This avoids blocking the async event loop for 1+ second.
    """
    try:
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        cpu_percent = sum(per_cpu) / len(per_cpu) if per_cpu else 0.0
        cpu_count = psutil.cpu_count(logical=True)
        physical_cores = psutil.cpu_count(logical=False) or 1

        return {
            "total_percent": f"{cpu_percent:.1f}%",
            "per_cpu": [f"{p:.1f}%" for p in per_cpu],
            "logical_cores": cpu_count,
            "physical_cores": physical_cores,
        }
    except Exception as e:
        logger.exception(f"Error getting CPU usage: {e}")
        return {
            "total_percent": "Unknown",
            "per_cpu": [],
            "logical_cores": 0,
            "physical_cores": 0,
        }


# Prime the CPU percent counter on module load so the first API call returns real data.
# This call is instant (interval=None) and runs once at import time.
psutil.cpu_percent(interval=None)


def get_disk_usage():
    """Get disk usage information"""
    try:
        # Get disk usage for the root directory
        disk = psutil.disk_usage("/")

        return {
            "total": format_bytes(disk.total),
            "used": format_bytes(disk.used),
            "free": format_bytes(disk.free),
            "percent": f"{disk.percent}%",
        }
    except Exception as e:
        logger.exception(f"Error getting disk usage: {e}")
        return {
            "total": "Unknown",
            "used": "Unknown",
            "free": "Unknown",
            "percent": "Unknown",
        }


def _query_gpu_via_smi() -> dict | None:
    """Run nvidia-smi directly to get GPU stats. Returns None if unavailable."""
    import subprocess

    try:
        device_id = settings.GPU_DEVICE_ID
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input  # nosec B603
            [  # noqa: S607 - nvidia-smi resolved from PATH on the GPU host  # nosec B607
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,memory.free,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
                f"--id={device_id}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        parts = result.stdout.strip().split(", ")
        used_mib, total_mib, free_mib = float(parts[1]), float(parts[2]), float(parts[3])
        used_bytes = used_mib * 1024 * 1024
        total_bytes = total_mib * 1024 * 1024
        free_bytes = free_mib * 1024 * 1024
        pct = (used_bytes / total_bytes * 100) if total_bytes > 0 else 0
        util = int(parts[4]) if len(parts) > 4 else None
        temp = int(parts[5]) if len(parts) > 5 else None
        return {
            "available": True,
            "name": parts[0],
            "memory_total": format_bytes(total_bytes),
            "memory_used": format_bytes(used_bytes),
            "memory_free": format_bytes(free_bytes),
            "memory_percent": f"{pct:.1f}%",
            "utilization_percent": f"{util}%" if util is not None else "N/A",
            "temperature_celsius": temp,
        }
    except (FileNotFoundError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def get_gpu_usage():
    """Get GPU usage array from Redis cache, falling back to direct nvidia-smi query.

    Returns a list of GPU stat dicts — one per active GPU device.  In normal
    mode this is a single-element list; in --gpu-scale dual-worker mode it
    contains one entry per active GPU worker.
    """
    try:
        import json

        from app.core.celery import celery_app

        redis_client = celery_app.backend.client
        gpu_stats_json = redis_client.get("gpu_stats")

        if gpu_stats_json:
            data = json.loads(gpu_stats_json)
            # Migrate legacy single-dict format to list
            return data if isinstance(data, list) else [data]

        # Redis empty — try nvidia-smi directly (works if backend host has NVIDIA drivers)
        direct_stats = _query_gpu_via_smi()
        if direct_stats:
            result = [direct_stats]
            redis_client.setex("gpu_stats", 600, json.dumps(result))
            return result

        # nvidia-smi not available — dispatch to cpu worker (debounced)
        lock_acquired = redis_client.set("gpu_stats_pending", "1", nx=True, ex=30)
        if lock_acquired:
            celery_app.send_task("system.update_gpu_stats", queue=CeleryQueues.CPU)
            logger.info("Dispatched on-demand GPU stats collection")

        return [
            {
                "available": False,
                "loading": True,
                "name": "Loading GPU stats...",
                "memory_total": "N/A",
                "memory_used": "N/A",
                "memory_free": "N/A",
                "memory_percent": "N/A",
            }
        ]
    except Exception as e:
        logger.exception(f"Error getting GPU usage from Redis: {e}")
        return [
            {
                "available": False,
                "name": "Error",
                "memory_total": "Unknown",
                "memory_used": "Unknown",
                "memory_free": "Unknown",
                "memory_percent": "Unknown",
            }
        ]
