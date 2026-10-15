"""Redis-backed lock, progress state and cancel flag for the one local-model download (#1246).

Only one model may download at a time, deployment-wide: two multi-GB downloads into one cache
fight for disk and bandwidth. The lock is a Redis ``SET NX EX`` taken by the API before the task
is dispatched, refreshed by the running task, and released by a compare-and-delete on the task
id so a stale task can never release a newer lock. A dead worker frees it by TTL.

All keys live under a namespace given to :class:`DownloadStore`, so tests never touch the live
keys. Nothing here is persistent state: the hub cache on disk is the source of truth for
"downloaded"; losing these keys only drops the progress display.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

PRODUCTION_NAMESPACE = "asr:local_model_download"
#: Lock lifetime at dispatch: the task may queue behind media downloads before it starts.
QUEUED_LOCK_TTL_SECONDS = 900
#: Lock lifetime while running; the supervisor refreshes it every tick.
RUNNING_LOCK_TTL_SECONDS = 120
#: How long a finished download's state stays readable.
STATE_TTL_SECONDS = 24 * 3600
CANCEL_TTL_SECONDS = 3600

ACTIVE_STATUSES = frozenset({"queued", "downloading", "verifying"})

# Both scripts compare the task id inside the lock's JSON, atomically.
_RELEASE_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
if cjson.decode(v).task_id == ARGV[1] then redis.call('DEL', KEYS[1]) return 1 end
return 0
"""
_REFRESH_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
if cjson.decode(v).task_id == ARGV[1] then redis.call('EXPIRE', KEYS[1], ARGV[2]) return 1 end
return 0
"""


def _decode(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class DownloadStore:
    """The lock, state and cancel flag of the single in-flight model download."""

    def __init__(self, redis_client: Any, namespace: str = PRODUCTION_NAMESPACE) -> None:
        self._redis = redis_client
        self._ns = namespace

    @property
    def _lock_key(self) -> str:
        return f"{self._ns}:lock"

    @property
    def _state_key(self) -> str:
        return f"{self._ns}:state"

    def _cancel_key(self, task_id: str) -> str:
        return f"{self._ns}:cancel:{task_id}"

    # -- lock ---------------------------------------------------------------------------

    def try_acquire(
        self, model_id: str, task_id: str, user_id: int, ttl: int = QUEUED_LOCK_TTL_SECONDS
    ) -> bool:
        """Take the lock if nobody holds it. Returns False when another download holds it."""
        payload = json.dumps(
            {
                "model_id": model_id,
                "task_id": task_id,
                "user_id": user_id,
                "started_at": time.time(),
            }
        )
        return bool(self._redis.set(self._lock_key, payload, nx=True, ex=ttl))

    def adopt(self, model_id: str, task_id: str, user_id: int) -> bool:
        """Confirm this task holds the lock, re-taking it if it expired while queued.

        Returns False when a different task holds it.
        """
        held = self.current_lock()
        if held is None:
            return self.try_acquire(model_id, task_id, user_id, RUNNING_LOCK_TTL_SECONDS)
        return held.get("task_id") == task_id and self.refresh(task_id)

    def refresh(self, task_id: str, ttl: int = RUNNING_LOCK_TTL_SECONDS) -> bool:
        """Extend the lock if ``task_id`` still holds it."""
        return bool(self._redis.eval(_REFRESH_LUA, 1, self._lock_key, task_id, ttl))

    def release(self, task_id: str) -> bool:
        """Delete the lock only if ``task_id`` holds it (never a bare DEL)."""
        return bool(self._redis.eval(_RELEASE_LUA, 1, self._lock_key, task_id))

    def current_lock(self) -> dict | None:
        """Return the lock holder's record, or None when no download is in flight."""
        return _decode(self._redis.get(self._lock_key))

    # -- state --------------------------------------------------------------------------

    def write_state(self, replace: bool = False, **fields: Any) -> dict:
        """Merge ``fields`` into the progress state (or replace it) and return the result."""
        state = {} if replace else (self.read_state() or {})
        state.update(fields)
        state["updated_at"] = time.time()
        self._redis.set(self._state_key, json.dumps(state), ex=STATE_TTL_SECONDS)
        return state

    def read_state(self) -> dict | None:
        """Return the latest progress state, or None when there is none."""
        return _decode(self._redis.get(self._state_key))

    # -- cancel -------------------------------------------------------------------------

    def request_cancel(self, task_id: str) -> None:
        """Ask the task to stop; the supervisor polls this every tick."""
        self._redis.set(self._cancel_key(task_id), "1", ex=CANCEL_TTL_SECONDS)

    def cancel_requested(self, task_id: str) -> bool:
        """Return True when a cancel was requested for ``task_id``."""
        return self._redis.get(self._cancel_key(task_id)) is not None
