"""Owner-refreshed Redis leases: a redis-py ``Lock`` kept alive by its holder.

A lock with a fixed TTL has to choose between two failures. Long enough to outlive the
slowest run, and a crashed holder blocks everyone for that long (which is why API startup
used to delete every lock it could find, including ones live workers held). Short enough
to recover quickly, and a slow live run loses the lock and a second copy starts.

A lease removes the choice. The TTL is short; a daemon thread in the holder calls
``Lock.reacquire()`` every third of it, so the key lives exactly as long as the holding
process does. A holder that dies stops renewing and the key expires within one TTL.

redis-py documents both pieces this relies on (``redis/lock.py``):

* ``reacquire()`` resets the TTL of a lock this client still owns, atomically (Lua), and
  raises ``LockNotOwnedError`` once the token no longer matches.
* ``thread_local=False`` is required when another thread renews the lock: the default
  stores the token in ``threading.local``, so the heartbeat thread would see no token.
"""

from __future__ import annotations

import logging
import threading
import time

import redis
from redis.lock import Lock

logger = logging.getLogger(__name__)

#: Renew this many times per lease TTL, so one missed beat never loses the lease.
BEATS_PER_TTL = 3


def make_lease_lock(client: redis.Redis, key: str, ttl: float, blocking_timeout: float = 0) -> Lock:
    """Build a redis-py lock whose token is visible to a heartbeat thread."""
    return Lock(client, key, timeout=ttl, blocking_timeout=blocking_timeout, thread_local=False)


class LeaseHeartbeat:
    """Renew a held lock from a daemon thread until stopped.

    ``max_hold`` caps the total time the lease is renewed for. It is a backstop against a
    holder that is alive but wedged (a threads-pool Celery worker cannot be time-limited);
    after it, renewal stops and the lease expires one TTL later.
    """

    def __init__(self, lock: Lock, *, ttl: float, max_hold: float | None, name: str) -> None:
        self._lock = lock
        self._interval = max(ttl / BEATS_PER_TTL, 0.05)
        self._max_hold = max_hold
        self._name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Set when renewal failed because another owner holds the key, or Redis refused.
        self.lost = False

    def start(self) -> LeaseHeartbeat:
        """Start renewing; returns self for chaining."""
        self._thread = threading.Thread(
            target=self._run, name=f"lease-heartbeat:{self._name}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop renewing and wait for the thread to finish its current beat."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def _run(self) -> None:
        started = time.monotonic()
        while not self._stop.wait(self._interval):
            if self._max_hold is not None and time.monotonic() - started >= self._max_hold:
                logger.error(
                    "Lease %s held past its %.0fs cap; no longer renewing it",
                    self._name,
                    self._max_hold,
                )
                return
            try:
                self._lock.reacquire()
            except redis.exceptions.LockNotOwnedError:
                self.lost = True
                logger.error("Lease %s was lost: another owner holds the key", self._name)
                return
            except redis.RedisError as exc:
                # Transient: keep trying. The lease survives as long as one beat in
                # BEATS_PER_TTL lands.
                logger.warning("Lease %s renewal failed (%s); retrying", self._name, exc)
