"""Unified progress tracking service with ETA calculation.

Provides Redis-backed progress tracking for long-running tasks
(reindex, migration, clustering, auto-label) with EWMA-based ETA
estimation and rate-limited Redis writes.
"""

import json
import logging
import threading
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field

from redis.exceptions import RedisError

from app.core.redis import get_request_redis

logger = logging.getLogger(__name__)

# Redis key pattern: task_progress:{task_type}:{user_id}
_KEY_PREFIX = "task_progress"
# Per-user index of this user's progress keys, so listing needs no keyspace SCAN.
_INDEX_PREFIX = "task_progress_index"
_TTL_ACTIVE = 86400  # 24h while running
_TTL_COMPLETED = 3600  # 1h after completion

# Rate limiting: max 1 Redis write per this many seconds (except first/last)
_MIN_WRITE_INTERVAL = 0.5

# EWMA smoothing factor for rate estimation (lower = smoother, less jumpy)
_EWMA_ALPHA = 0.15

# Suppress ETA until this many updates have occurred
_MIN_UPDATES_FOR_ETA = 3


@dataclass
class ProgressState:
    """Serializable progress state stored in Redis."""

    task_type: str
    user_id: int
    total: int
    processed: int = 0
    status: str = "running"  # running | completed | failed
    message: str = "Starting..."
    eta_seconds: float | None = None
    ewma_rate: float | None = None  # items/sec, persisted for cross-batch continuity
    failed_items: list[str] = field(default_factory=list)
    started_at: float = 0.0  # time.time()
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProgressState":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class ProgressTracker:
    """Track progress of a long-running task with ETA calculation.

    Uses EWMA (Exponentially Weighted Moving Average) for rate smoothing
    to avoid jumpy ETAs. Rate-limits Redis writes to avoid flooding.
    """

    def __init__(self, task_type: str, user_id: int, total: int):
        self.task_type = task_type
        self.user_id = user_id
        self.total = total

        self._lock = threading.Lock()
        self._update_count = 0
        self._ewma_rate: float | None = None  # items/sec
        self._started_at: float = 0.0
        self._last_update_time: float = 0.0
        self._last_write_time: float = 0.0
        self._last_processed: int = 0

    @property
    def _key(self) -> str:
        return f"{_KEY_PREFIX}:{self.task_type}:{self.user_id}"

    def resume_from_state(self, state: ProgressState) -> None:
        """Resume tracker from existing Redis state, restoring EWMA continuity.

        Must be called instead of start() when a batch task picks up
        progress that was already started by a previous batch.
        """
        with self._lock:
            self._last_processed = state.processed
            self._update_count = max(state.processed, 1)
            self._started_at = state.started_at if state.started_at > 0 else time.time()
            self._last_update_time = state.updated_at if state.updated_at > 0 else time.time()

            # Restore persisted EWMA rate for smooth cross-batch ETA
            if state.ewma_rate is not None and state.ewma_rate > 0:
                self._ewma_rate = state.ewma_rate
            elif (
                state.started_at > 0 and state.updated_at > state.started_at and state.processed > 0
            ):
                # Approximate rate from overall elapsed time
                elapsed = state.updated_at - state.started_at
                self._ewma_rate = state.processed / elapsed

    def start(self, message: str = "Starting...") -> None:
        """Mark task as started and write initial state to Redis."""
        now = time.time()
        self._started_at = now
        self._last_update_time = now
        state = ProgressState(
            task_type=self.task_type,
            user_id=self.user_id,
            total=self.total,
            processed=0,
            status="running",
            message=message,
            started_at=now,
            updated_at=now,
        )
        self._write_state(state, force=True)

    def update(
        self,
        processed: int,
        message: str = "",
        failed_item: str | None = None,
    ) -> ProgressState | None:
        """Update progress and compute ETA. Thread-safe.

        Returns ProgressState if a Redis write occurred (rate-limited),
        None if the update was throttled.
        """
        with self._lock:
            now = time.time()
            self._update_count += 1

            # Update EWMA rate
            elapsed = now - self._last_update_time
            if elapsed > 0 and processed > self._last_processed:
                items_delta = processed - self._last_processed
                instant_rate = items_delta / elapsed
                if self._ewma_rate is None:
                    self._ewma_rate = instant_rate
                else:
                    self._ewma_rate = (
                        _EWMA_ALPHA * instant_rate + (1 - _EWMA_ALPHA) * self._ewma_rate
                    )

            self._last_update_time = now
            self._last_processed = processed

            # Compute ETA
            eta: float | None = None
            remaining = self.total - processed
            if (
                self._update_count >= _MIN_UPDATES_FOR_ETA
                and self._ewma_rate
                and self._ewma_rate > 0
                and remaining > 0
            ):
                eta = remaining / self._ewma_rate

            # Rate-limit Redis writes (except first update and final)
            is_final = processed >= self.total
            is_first = self._update_count <= 1
            time_since_write = now - self._last_write_time

            if not is_first and not is_final and time_since_write < _MIN_WRITE_INTERVAL:
                return None

            state = ProgressState(
                task_type=self.task_type,
                user_id=self.user_id,
                total=self.total,
                processed=processed,
                status="running",
                message=message or f"Processing {processed}/{self.total}",
                eta_seconds=round(eta, 1) if eta is not None else None,
                ewma_rate=round(self._ewma_rate, 6) if self._ewma_rate else None,
                started_at=self._started_at,
                updated_at=now,
            )

        # Redis operations outside the lock (they have their own atomicity)
        if failed_item:
            existing = self.get_state(self.task_type, self.user_id)
            if existing:
                state.failed_items = existing.failed_items
            if failed_item not in state.failed_items:
                state.failed_items.append(failed_item)

        self._write_state(state)
        return state

    def complete(self, message: str = "Complete") -> None:
        """Mark task as completed."""
        state = ProgressState(
            task_type=self.task_type,
            user_id=self.user_id,
            total=self.total,
            processed=self.total,
            status="completed",
            message=message,
            eta_seconds=None,
            updated_at=time.time(),
        )
        self._write_state(state, force=True, ttl=_TTL_COMPLETED)

    def fail(self, message: str = "Failed") -> None:
        """Mark task as failed."""
        existing = self.get_state(self.task_type, self.user_id)
        state = ProgressState(
            task_type=self.task_type,
            user_id=self.user_id,
            total=self.total,
            processed=existing.processed if existing else 0,
            status="failed",
            message=message,
            eta_seconds=None,
            failed_items=existing.failed_items if existing else [],
            updated_at=time.time(),
        )
        self._write_state(state, force=True, ttl=_TTL_COMPLETED)

    def _write_state(
        self,
        state: ProgressState,
        force: bool = False,
        ttl: int = _TTL_ACTIVE,
    ) -> None:
        """Write state to Redis with optional force (bypass rate limit)."""
        try:
            r = get_request_redis()
            # One round trip: the state key plus its membership in the user's index
            # (redis-py "Pipelines" batch commands into a single request).
            index_key = f"{_INDEX_PREFIX}:{self.user_id}"
            pipe = r.pipeline()
            pipe.set(self._key, json.dumps(state.to_dict()), ex=ttl)
            pipe.sadd(index_key, self._key)
            pipe.expire(index_key, _TTL_ACTIVE)
            pipe.execute()
            self._last_write_time = time.time()
        except (RedisError, TypeError, ValueError) as e:
            logger.debug(f"Failed to write progress state: {e}", exc_info=True)

    @classmethod
    def get_state(cls, task_type: str, user_id: int) -> ProgressState | None:
        """Read current state from Redis."""
        try:
            r = get_request_redis()
            raw = r.get(f"{_KEY_PREFIX}:{task_type}:{user_id}")
            if raw:
                return ProgressState.from_dict(json.loads(raw))
        except (RedisError, ValueError, TypeError) as e:
            logger.debug(f"Failed to read progress state: {e}", exc_info=True)
        return None

    @classmethod
    def get_active_tasks(cls, user_id: int) -> list[dict]:
        """Get all active (running) tasks for a user.

        Reads the per-user index set, then every state with one MGET (two round
        trips total, independent of keyspace size). Index members whose key has
        expired are pruned.
        """
        results: list[dict] = []
        try:
            r = get_request_redis()
            index_key = f"{_INDEX_PREFIX}:{user_id}"
            keys = sorted(k.decode() if isinstance(k, bytes) else k for k in r.smembers(index_key))
            if not keys:
                return results
            stale = []
            for key, raw in zip(keys, r.mget(keys), strict=True):
                if not raw:
                    stale.append(key)
                    continue
                state = ProgressState.from_dict(json.loads(raw))
                if state.status == "running":
                    results.append(state.to_dict())
            if stale:
                r.srem(index_key, *stale)
        except (RedisError, ValueError, TypeError) as e:
            logger.debug(f"Failed to read active tasks: {e}", exc_info=True)
        return results


def emit_progress_notification(
    tracker: ProgressTracker,
    processed: int,
    user_id: int,
    notification_type: str,
    extra_data: dict | None = None,
    message: str = "",
    failed_item: str | None = None,
) -> None:
    """Update tracker + publish WebSocket notification in one call.

    Respects tracker rate limiting — if the tracker throttles
    the update, no notification is published either.
    """
    state = tracker.update(processed, message=message, failed_item=failed_item)
    if state is None:
        return  # Throttled

    try:
        r = get_request_redis()
        data: dict = {
            "progress": round(processed / tracker.total, 4) if tracker.total > 0 else 0,
            "message": state.message,
            "eta_seconds": state.eta_seconds,
            **(extra_data or {}),
        }
        notification = {
            "user_id": user_id,
            "type": notification_type,
            "data": data,
        }
        r.publish("websocket_notifications", json.dumps(notification))
    except (RedisError, TypeError, ValueError) as e:
        logger.debug(f"Failed to emit progress notification: {e}", exc_info=True)
