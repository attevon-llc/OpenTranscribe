"""System endpoints accessible to all authenticated users."""

import logging
import os
import platform
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.admin import get_cpu_usage
from app.api.endpoints.admin import get_disk_usage
from app.api.endpoints.admin import get_gpu_usage
from app.api.endpoints.admin import get_memory_usage
from app.api.endpoints.admin import get_system_uptime
from app.api.endpoints.auth import get_current_user
from app.core.version import APP_VERSION
from app.db.base import get_db
from app.models.user import User
from app.services.protected_media_providers import get_protected_media_auth_config

logger = logging.getLogger(__name__)

router = APIRouter()


def _device_mode_info(gpu_stats: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the CPU/GPU device-mode summary surfaced by /system/stats.

    The frontend uses these fields to render the CPU-only mode advisory
    banner in Settings → System Statistics. Distinguishes "user opted out
    via FORCE_CPU_MODE" from "host has no working GPU" so the UI can
    show the right reason text.
    """
    force_cpu = os.getenv("FORCE_CPU_MODE", "false").strip().lower() == "true"
    first_gpu = gpu_stats[0] if gpu_stats else {}
    gpu_available = first_gpu.get("available") is True
    # On fresh GPU-host startup, get_gpu_usage() returns
    # {"available": False, "loading": True} until the worker pushes its
    # first stats. Treat that transient state as "unknown — assume GPU"
    # so the CPU banner doesn't flash on every cold start.
    gpu_loading = first_gpu.get("loading") is True

    if force_cpu:
        device_mode = "cpu"
    elif gpu_available or gpu_loading:
        device_mode = "cuda"
    else:
        # No FORCE_CPU_MODE flag and no usable GPU — auto-fallback CPU.
        # MPS isn't currently surfaced via get_gpu_usage(); leave that
        # to a follow-up if Apple-Silicon visibility becomes a need.
        device_mode = "cpu"

    return {
        "device_mode": device_mode,
        "force_cpu_mode": force_cpu,
        "whisper_model": os.getenv("WHISPER_MODEL", "large-v3-turbo"),
        "diarization_enabled": os.getenv("ENABLE_DIARIZATION", "true").strip().lower() == "true",
    }


@router.get("/stats", response_model=dict[str, Any])
async def get_system_stats(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """
    Get system statistics accessible to all authenticated users.

    Returns system health metrics (CPU, memory, disk, GPU) and aggregate
    statistics about files, tasks, and models.
    """
    logger.info(f"System stats requested by user {current_user.email}")

    try:
        # System statistics
        try:
            system_stats = {
                "cpu": get_cpu_usage(),
                "memory": get_memory_usage(),
                "disk": get_disk_usage(),
                "gpu": get_gpu_usage(),
                "uptime": get_system_uptime(),
            }
        except Exception as e:
            logger.error(f"Error getting system stats: {e}")
            system_stats = {
                "cpu": {
                    "total_percent": "Unknown",
                    "per_cpu": [],
                    "logical_cores": 0,
                    "physical_cores": 0,
                },
                "gpus": [
                    {
                        "available": False,
                        "name": "Error",
                        "memory_total": "Unknown",
                        "memory_used": "Unknown",
                        "memory_free": "Unknown",
                        "memory_percent": "Unknown",
                    }
                ],
                "memory": {
                    "total": "Unknown",
                    "available": "Unknown",
                    "used": "Unknown",
                    "percent": "Unknown",
                },
                "disk": {
                    "total": "Unknown",
                    "used": "Unknown",
                    "free": "Unknown",
                    "percent": "Unknown",
                },
                "uptime": "Unknown",
            }

        # Consolidated database statistics (efficient aggregate queries)
        from app.utils.stats_helpers import get_file_stats
        from app.utils.stats_helpers import get_file_timing_stats
        from app.utils.stats_helpers import get_models_info
        from app.utils.stats_helpers import get_processing_eta
        from app.utils.stats_helpers import get_queue_depths
        from app.utils.stats_helpers import get_recent_tasks
        from app.utils.stats_helpers import get_task_stats
        from app.utils.stats_helpers import get_throughput_stats
        from app.utils.stats_helpers import get_user_stats

        user_stats = get_user_stats(db)
        file_stats = get_file_stats(db)
        task_stats = get_task_stats(db)
        recent = get_recent_tasks(db, limit=10)
        throughput = get_throughput_stats(db)
        eta = get_processing_eta(db)
        file_timing = get_file_timing_stats(db)
        queue_depths = get_queue_depths()
        models_info = get_models_info()

        total_files = file_stats["total"]
        total_speakers = file_stats["speakers"]
        total_segments = file_stats["segments"]

        # Construct the response
        stats = {
            "users": user_stats,
            "files": {
                "total": total_files,
                "new": file_stats["new"],
                "total_duration": file_stats["total_duration"],
                "segments": total_segments,
            },
            "transcripts": {"total_segments": total_segments},
            "speakers": {
                "total": total_speakers,
                "avg_per_file": round(total_speakers / total_files, 2) if total_files > 0 else 0,
            },
            "models": models_info,
            "system": {
                "version": APP_VERSION,
                "uptime": system_stats["uptime"],
                "memory": system_stats["memory"],
                "cpu": system_stats["cpu"],
                "disk": system_stats["disk"],
                "gpus": system_stats["gpu"],  # list of GPU stat dicts
                "platform": platform.platform(),
                "python_version": platform.python_version(),
                **_device_mode_info(system_stats["gpu"]),
            },
            "tasks": {**task_stats, "recent": recent},
            "throughput": throughput,
            "eta": eta,
            "file_timing": file_timing,
            "queues": queue_depths,
        }

        return stats
    except Exception as e:
        logger.error("Error getting system stats: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.get("/config/protected-media-auth", response_model=list[dict[str, Any]])
async def get_protected_media_auth(current_user: User = Depends(get_current_user)):
    """Return public auth configuration for protected media providers.

    Used by the frontend to decide when to prompt for username/password
    (or other credentials) when processing media URLs.
    """
    try:
        return get_protected_media_auth_config()
    except Exception as e:
        logger.error(f"Error getting protected media auth config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving protected media configuration",
        ) from e
