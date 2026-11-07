"""Per-user revocation epochs that could not reach Redis, replayed when it is back.

A logout-all, password reset or admin termination stamps a per-user epoch in Redis
(``TokenService.stamp_user_revocation_epoch``). While Redis is unreachable the stamping
process falls back to the database for its own checks, but every OTHER replica still reads
Redis, finds no epoch and keeps accepting the user's access tokens until they expire. This
backlog keeps each epoch that never reached Redis and writes it once Redis answers again,
so those replicas start refusing the tokens too.

The backlog is per process and in memory: if the stamping process restarts before Redis
recovers, its backlog is lost and the remaining exposure is the access-token lifetime.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Raise the stored epoch to ARGV[1] with a TTL of ARGV[2] seconds, never lower it: a newer
#: epoch another replica stamped after the outage must win over the replayed one. Atomic
#: because Redis runs a script without interleaving other commands.
RAISE_EPOCH_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]))
if current and current >= tonumber(ARGV[1]) then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


class PendingEpochs:
    """Epochs waiting for Redis, keyed by their full Redis key."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        """Create an empty backlog.

        Args:
            clock: Wall-clock source, for when each epoch's key would have expired.
        """
        self._pending: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)

    def remember(self, key: str, epoch: int, ttl_seconds: int) -> None:
        """Keep *epoch* for *key* until it can be written; a newer epoch replaces an older one.

        Args:
            key: The Redis key the epoch belongs under.
            epoch: The revocation epoch (unix seconds).
            ttl_seconds: How long the key should have lived from now.
        """
        expires = self._clock() + ttl_seconds
        with self._lock:
            held = self._pending.get(key)
            if held is None or held[0] < epoch:
                self._pending[key] = (epoch, expires)

    def flush(self, store: Any) -> int:
        """Write every pending epoch that has not yet expired, through :data:`RAISE_EPOCH_LUA`.

        Stops at the first failure and keeps that entry and the rest for the next attempt.

        Args:
            store: A Redis client.

        Returns:
            Number of epochs written.
        """
        with self._lock:
            items = list(self._pending.items())
        written = 0
        for key, (epoch, expires) in items:
            ttl = int(expires - self._clock())
            if ttl > 0:
                try:
                    store.eval(RAISE_EPOCH_LUA, 1, key, epoch, ttl)
                except Exception:
                    logger.warning(
                        "Could not replay %d revocation epoch(s) into Redis; will retry",
                        len(items) - written,
                        exc_info=True,
                    )
                    return written
                written += 1
            with self._lock:
                if self._pending.get(key) == (epoch, expires):
                    del self._pending[key]
        if written:
            logger.info("Replayed %d revocation epoch(s) recorded while Redis was down", written)
        return written
