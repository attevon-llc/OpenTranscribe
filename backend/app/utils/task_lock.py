"""Distributed task locks: owner-refreshed leases that fail closed.

Every lock here is a lease (``app/utils/redis_lease.py``): a short Redis TTL that the
holder renews from a heartbeat thread. A live run keeps its lock however long it takes; a
dead one's lock expires within ``TASK_LOCK_LEASE_SECONDS``. The ``timeout`` a caller passes
is the longest the lease will be renewed for, a backstop against a holder that is alive
but wedged, not the lock's TTL.

**Redis unavailable means the task does not run** (it is reported as skipped), unless its
key is in ``FAIL_OPEN_LOCKS`` below. A lock exists because two copies of the task at once
do damage; running without it during an outage is the one moment that damage is most
likely, since every replica sees the same outage. The classification of every current lock
key:

Fail closed (a concurrent duplicate destroys, duplicates or corrupts state):
    ``system_startup_recovery``, ``system.reclaim_lost_tasks``,
    ``system.reclaim_orphaned_deliveries``, ``system.health_check``: reset file state and
    re-dispatch work, so a duplicate runs a job twice.
    ``search_index_maintenance``, ``search_index_sweep``, ``search_reembed_degraded``,
    ``opensearch_index_structure_lock:*``: reindex / delete / recreate the shared index; two
    overlapping passes have already deleted most of an index (root CLAUDE.md).
    ``neural_search_bootstrap``: a concurrent deploy deletes an in-flight registration.
    ``backup_run``, ``media_mirror_run``: write and prune backup sets.
    ``watch_source:scan_all``, ``watch_source:scan:*``: a duplicate imports files twice.
    ``recluster_speakers_user_*``: rewrites a user's clusters.
    ``session_cap_sweep``, ``account_inactivity_sweep``, ``directory_sync*``: revoke
    sessions, disable accounts.

Fail open (``FAIL_OPEN_LOCKS``, with the reason a duplicate is harmless).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from typing import ParamSpec
from typing import TypeVar

import redis

from app.core.config import settings
from app.core.redis import get_request_redis
from app.utils.redis_lease import LeaseHeartbeat
from app.utils.redis_lease import make_lease_lock

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

#: Lock keys (exact, or a prefix ending in ``*``) that still run when Redis is down.
FAIL_OPEN_LOCKS: dict[str, str] = {
    "speaker_profile_opensearch_sync": (
        "copies profile fields into OpenSearch idempotently; a duplicate writes the same "
        "values and the sync converges over repeated runs"
    ),
    "watch_source:fs_dispatch:*": (
        "only de-duplicates the DISPATCH of a scan; the scan itself takes "
        "watch_source:scan:<id>, which fails closed"
    ),
}


def fails_open(lock_key: str) -> bool:
    """True when *lock_key* is classified as safe to run without its lock."""
    for pattern in FAIL_OPEN_LOCKS:
        if pattern.endswith("*") and lock_key.startswith(pattern[:-1]):
            return True
        if pattern == lock_key:
            return True
    return False


class TaskLockManager:
    """Manages distributed locks for task execution."""

    def __init__(self, client_factory: Callable[[], redis.Redis] | None = None) -> None:
        """Use *client_factory* for Redis; default is ``get_request_redis()``."""
        self._client_factory = client_factory

    def _client(self) -> redis.Redis | None:
        """The bounded request-path client if Redis answers now, else None.

        Every lock command (SET NX, the Lua release/extend scripts, and a blocking
        acquire, which redis-py implements as a client-side retry loop) is short, so the
        bounded socket timeout fits; a heartbeat that times out just retries next beat.

        Checked on every call rather than latched: one failed ping at process start used to
        disable locking for the life of the worker.
        """
        try:
            client = (self._client_factory or get_request_redis)()
            client.ping()
            return client
        except redis.RedisError as exc:
            logger.error("Redis unavailable for task locks: %s", exc)
            return None

    @contextmanager
    def acquire_lock(
        self, lock_key: str, timeout: int = 300, blocking_timeout: int = 0
    ) -> Iterator[bool]:
        """Hold a renewed lease on *lock_key* for the duration of the block.

        Args:
            lock_key: Unique identifier for the lock.
            timeout: Longest the lease is renewed for, in seconds (a wedge backstop).
            blocking_timeout: How long to wait for the lock (0 = non-blocking).

        Yields:
            True if the lock is held, False if another holder has it, or if Redis is
            unavailable and the key fails closed.
        """
        lease_ttl = min(settings.TASK_LOCK_LEASE_SECONDS, timeout)
        client = self._client()
        lock = None
        acquired = False
        if client is None:
            acquired = fails_open(lock_key)
            logger.warning(
                "Redis unavailable: %s %s without its lock",
                "running" if acquired else "refusing to run",
                lock_key,
            )
        else:
            lock = make_lease_lock(client, lock_key, lease_ttl, blocking_timeout)
            try:
                acquired = bool(lock.acquire(blocking=blocking_timeout > 0))
            except redis.RedisError as exc:
                acquired = fails_open(lock_key)
                lock = None
                logger.error("Redis error acquiring %s (%s); held=%s", lock_key, exc, acquired)
            if lock is not None and not acquired:
                logger.info("Could not acquire lock for %s - task may already be running", lock_key)

        heartbeat = None
        if acquired and lock is not None:
            logger.info("Acquired lock for %s", lock_key)
            heartbeat = LeaseHeartbeat(lock, ttl=lease_ttl, max_hold=timeout, name=lock_key).start()
        # The yield sits outside every except clause on purpose: an exception raised by
        # the body (including Celery's Retry) must reach the caller unchanged.
        try:
            yield acquired
        finally:
            if heartbeat is not None:
                heartbeat.stop()
            if acquired and lock is not None:
                self._release(lock, lock_key)

    @staticmethod
    def _release(lock: Any, lock_key: str) -> None:
        try:
            lock.release()
            logger.info("Released lock for %s", lock_key)
        except redis.exceptions.LockError as exc:
            logger.error("Lock %s was no longer ours at release: %s", lock_key, exc)
        except redis.RedisError as exc:
            logger.error("Error releasing lock for %s (it will expire): %s", lock_key, exc)

    def is_locked(self, lock_key: str) -> bool:
        """True if *lock_key* is currently held (False when Redis cannot say)."""
        client = self._client()
        if client is None:
            return False
        try:
            return bool(client.exists(lock_key) > 0)
        except redis.RedisError as exc:
            logger.error("Error checking lock status for %s: %s", lock_key, exc)
            return False


# Global lock manager instance
task_lock_manager = TaskLockManager()


def with_task_lock(
    lock_key: str, timeout: int = 300
) -> Callable[[Callable[P, R]], Callable[P, R | dict[str, Any]]]:
    """Decorator to ensure only one instance of a task runs at a time.

    Args:
        lock_key: Unique identifier for the lock.
        timeout: Longest the lease is renewed for, in seconds.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R | dict[str, Any]]:
        # functools.wraps keeps __name__/__doc__/__wrapped__ intact. Without it
        # every decorated task presents as "wrapper", which breaks Celery's
        # implicit task naming for any caller that does not pass name= and hides
        # the real docstring from introspection.
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | dict[str, Any]:
            with task_lock_manager.acquire_lock(lock_key, timeout=timeout) as acquired:
                if acquired:
                    return func(*args, **kwargs)
                logger.info(f"Skipping {func.__name__} - lock not acquired")
                return {
                    "skipped": True,
                    "reason": "Task already running or its lock is unavailable",
                    "function": func.__name__,
                }

        return wrapper

    return decorator
