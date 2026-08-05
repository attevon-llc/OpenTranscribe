"""Coalesce bursts of filesystem events into one scan dispatch per source.

Writing a single 4 GB recording into a watched folder produces hundreds of
``modified`` events, and a copy of a session directory produces one burst per
file. Dispatching a scan per event would flood the download queue, so events are
folded into a per-source timer:

- **debounce** — dispatch only once the source has been *quiet* for N seconds.
  N defaults to ``watch.file_stability_seconds`` plus a margin, because
  ``LocalWatchClient.list_files`` deliberately skips files younger than that
  ("still being written"). Firing earlier would produce a scan that finds
  nothing and then no further trigger until the next poll.
- **max defer** — a folder under continuous churn never goes quiet, so a
  dispatch is forced once the oldest pending event reaches this age.
- **cooldown** — a floor on the interval between two event-driven dispatches
  for the same source.

The dispatch itself is taken under the shared Redis task lock so two supervisor
replicas cannot both enqueue the same scan; ``watch_source.scan_single`` holds
its own per-source lock as the second line of defence.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.utils.task_lock import task_lock_manager

logger = logging.getLogger(__name__)

LOCK_KEY_TEMPLATE = "watch_source:fs_dispatch:{source_id}"


@dataclass
class _Pending:
    first_event: float
    last_event: float
    debounce_seconds: float
    max_defer_seconds: float
    cooldown_seconds: float
    events: int = 1


def _default_dispatch(source_id: int) -> None:
    """Enqueue the real scan task (imported lazily to avoid an import cycle)."""
    from app.tasks.watch_source_tasks import scan_single

    scan_single.delay(source_id)


class ScanDispatcher:
    """Debounces FS events and enqueues ``watch_source.scan_single``."""

    def __init__(
        self,
        dispatch: Callable[[int], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        lock_timeout: int = 60,
    ) -> None:
        self._dispatch = dispatch or _default_dispatch
        self._clock = clock
        self._lock_timeout = lock_timeout
        self._mutex = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._last_dispatch: dict[int, float] = {}
        self.events_seen: dict[int, int] = {}
        self.scans_dispatched: dict[int, int] = {}
        self.last_event_at: dict[int, float] = {}

    # ----- event intake -------------------------------------------------- #
    def note_event(
        self,
        source_id: int,
        *,
        debounce_seconds: float,
        max_defer_seconds: float = 300.0,
        cooldown_seconds: float | None = None,
    ) -> None:
        """Record one filesystem event for ``source_id``."""
        now = self._clock()
        cooldown = debounce_seconds if cooldown_seconds is None else cooldown_seconds
        with self._mutex:
            self.events_seen[source_id] = self.events_seen.get(source_id, 0) + 1
            # Wall clock (not the injectable monotonic clock) — this one is only
            # ever displayed, while every window below is computed from ``now``.
            self.last_event_at[source_id] = time.time()
            current = self._pending.get(source_id)
            if current is None:
                self._pending[source_id] = _Pending(
                    first_event=now,
                    last_event=now,
                    debounce_seconds=debounce_seconds,
                    max_defer_seconds=max_defer_seconds,
                    cooldown_seconds=cooldown,
                )
            else:
                current.last_event = now
                current.events += 1
                current.debounce_seconds = debounce_seconds
                current.max_defer_seconds = max_defer_seconds
                current.cooldown_seconds = cooldown

    # ----- draining ------------------------------------------------------ #
    def due(self) -> list[int]:
        """Source ids whose debounce (or max-defer) window has elapsed."""
        now = self._clock()
        ready: list[int] = []
        with self._mutex:
            for source_id, pending in self._pending.items():
                quiet = (now - pending.last_event) >= pending.debounce_seconds
                deferred_too_long = (now - pending.first_event) >= pending.max_defer_seconds
                if not (quiet or deferred_too_long):
                    continue
                last = self._last_dispatch.get(source_id)
                if last is not None and (now - last) < pending.cooldown_seconds:
                    continue
                ready.append(source_id)
        return ready

    def flush(self) -> list[int]:
        """Dispatch every due source. Returns the ids actually enqueued."""
        dispatched: list[int] = []
        for source_id in self.due():
            with self._mutex:
                pending = self._pending.pop(source_id, None)
            if pending is None:
                continue
            if self._dispatch_locked(source_id, pending.events):
                dispatched.append(source_id)
        return dispatched

    def _dispatch_locked(self, source_id: int, event_count: int) -> bool:
        """Enqueue one scan, guarded by the shared Redis task lock."""
        lock_key = LOCK_KEY_TEMPLATE.format(source_id=source_id)
        try:
            with task_lock_manager.acquire_lock(lock_key, timeout=self._lock_timeout) as acquired:
                if not acquired:
                    logger.debug(
                        "FS-event scan for source %s already dispatched by another replica",
                        source_id,
                    )
                    return False
                self._dispatch(source_id)
        except Exception as e:  # noqa: BLE001 - never kill the supervisor thread
            logger.warning("FS-event scan dispatch failed for source %s: %s", source_id, e)
            return False

        with self._mutex:
            self._last_dispatch[source_id] = self._clock()
            self.scans_dispatched[source_id] = self.scans_dispatched.get(source_id, 0) + 1
        logger.info(
            "Dispatched watch_source.scan_single for source %s after %d filesystem event(s)",
            source_id,
            event_count,
        )
        return True

    # ----- lifecycle ----------------------------------------------------- #
    def forget(self, source_id: int) -> None:
        """Drop all state for a source that is no longer watched."""
        with self._mutex:
            self._pending.pop(source_id, None)
            self._last_dispatch.pop(source_id, None)
            self.events_seen.pop(source_id, None)
            self.scans_dispatched.pop(source_id, None)
            self.last_event_at.pop(source_id, None)

    def stats(self, source_id: int) -> dict[str, int | None]:
        """Counters for the status blob shown in the UI."""
        with self._mutex:
            return {
                "events_seen": self.events_seen.get(source_id, 0),
                "scans_dispatched": self.scans_dispatched.get(source_id, 0),
                "pending_events": self._pending[source_id].events
                if source_id in self._pending
                else 0,
            }
