"""Redis-backed engine metrics for capacity planning and CPU autoscaling.

Metrics are written under the key prefix `engine:metrics:<hostname>`
with a 60-second TTL. A single Engine instance (shared by all Celery
threads in a worker process) owns a single MetricsCollector.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from redis import Redis

logger = logging.getLogger(__name__)

_REDIS_PREFIX = "engine:metrics"
_TTL_SECONDS = 120
_HOSTNAME = socket.gethostname()


@dataclass
class EngineMetrics:
    """Snapshot of engine-wide counters for one worker."""

    gpu_ready_queue_depth: int = 0  # files preprocessed, waiting for GPU
    gpu_in_flight: int = 0  # files currently in Stage 2
    gpu_idle_seconds: float = 0.0  # cumulative GPU idle while queue non-empty
    last_stage_durations: dict[str, float] = field(default_factory=dict)


class MetricsCollector:
    """Write engine metrics to Redis with TTL.

    Thread-safe: each counter is written with a separate Redis HSET.
    The engine's GPU stage increments in_flight at entry and decrements
    at exit; preprocess increments ready_queue_depth, GPU stage decrements it.
    """

    def __init__(self, hostname: str = _HOSTNAME) -> None:
        self._key = f"{_REDIS_PREFIX}:{hostname}"
        self._redis: Redis[Any] | None = None
        self._stage_times: list[tuple[str, float]] = []

    def _get_redis(self):
        if self._redis is None:
            try:
                from app.core.redis import get_redis

                self._redis = get_redis()
            except Exception as e:
                logger.debug(f"MetricsCollector: Redis unavailable: {e}")
        return self._redis

    def _hset(self, field_name: str, value) -> None:
        r = self._get_redis()
        if r is None:
            return
        try:
            r.hset(self._key, field_name, str(value))
            r.expire(self._key, _TTL_SECONDS)
        except Exception as e:
            logger.debug(f"MetricsCollector: Redis write failed: {e}")

    def increment_ready_queue(self) -> None:
        r = self._get_redis()
        if r is None:
            return
        try:
            r.hincrby(self._key, "gpu_ready_queue_depth", 1)
            r.expire(self._key, _TTL_SECONDS)
        except Exception as e:
            logger.debug(f"MetricsCollector: hincrby failed: {e}")

    def decrement_ready_queue(self) -> None:
        r = self._get_redis()
        if r is None:
            return
        try:
            r.hincrby(self._key, "gpu_ready_queue_depth", -1)
            r.expire(self._key, _TTL_SECONDS)
        except Exception as e:
            logger.debug(f"MetricsCollector: hincrby failed: {e}")

    def increment_in_flight(self) -> None:
        r = self._get_redis()
        if r is None:
            return
        try:
            r.hincrby(self._key, "gpu_in_flight", 1)
            r.expire(self._key, _TTL_SECONDS)
        except Exception as e:
            logger.debug(f"MetricsCollector: hincrby failed: {e}")

    def decrement_in_flight(self) -> None:
        r = self._get_redis()
        if r is None:
            return
        try:
            r.hincrby(self._key, "gpu_in_flight", -1)
            r.expire(self._key, _TTL_SECONDS)
        except Exception as e:
            logger.debug(f"MetricsCollector: hincrby failed: {e}")

    def record_stage_duration(self, stage: str, duration_s: float) -> None:
        self._hset(f"last_{stage}_s", f"{duration_s:.3f}")
        self._stage_times.append((stage, duration_s))

    def get_snapshot(self) -> EngineMetrics:
        r = self._get_redis()
        if r is None:
            return EngineMetrics()
        try:
            raw = r.hgetall(self._key)
            return EngineMetrics(
                gpu_ready_queue_depth=int(raw.get(b"gpu_ready_queue_depth", 0) or 0),
                gpu_in_flight=int(raw.get(b"gpu_in_flight", 0) or 0),
                gpu_idle_seconds=float(raw.get(b"gpu_idle_seconds", 0.0) or 0.0),
                last_stage_durations={
                    k.decode().replace("last_", "").replace("_s", ""): float(v)
                    for k, v in raw.items()
                    if k.startswith(b"last_") and k.endswith(b"_s")
                },
            )
        except Exception as e:
            logger.debug(f"MetricsCollector: get_snapshot failed: {e}")
            return EngineMetrics()
