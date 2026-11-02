"""
Utility tasks for maintenance and system health.

This module contains general utility tasks. Recovery-specific tasks
have been moved to app.tasks.recovery for better organization.
"""

import contextlib
import json
import logging

from app.core.celery import celery_app
from app.core.config import settings
from app.core.env import env_bool
from app.core.queues import CPUPriority
from app.core.redis import get_redis
from app.utils.gpu_stats import query_gpu_stats

logger = logging.getLogger(__name__)


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

        # Determine which GPU devices the app workers are using.
        # All values come from .env via env_file on the cpu-worker service.
        gpu_scale_enabled = env_bool("GPU_SCALE_ENABLED", default=False)
        scale_device_id = int(os.environ.get("GPU_SCALE_DEVICE_ID", "2"))
        default_device_id = settings.GPU_DEVICE_ID
        scale_default_worker = env_bool("GPU_SCALE_DEFAULT_WORKER", default=False)

        if gpu_scale_enabled:
            # Scaled worker is always the primary; add default worker if also active.
            device_ids = [scale_device_id]
            if scale_default_worker and default_device_id != scale_device_id:
                device_ids.append(default_device_id)
        else:
            device_ids = [default_device_id]

        gpu_stats_list = []
        for device_id in device_ids:
            stats = query_gpu_stats(device_id)
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

        # Broadcast array to connected administrator WebSocket clients only
        try:
            get_redis().publish(
                "websocket_notifications",
                json.dumps(
                    {
                        "type": "gpu_stats_update",
                        "broadcast": True,
                        "admin_only": True,
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

    # There is no `except FileNotFoundError` arm: `query_gpu_stats` catches
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
