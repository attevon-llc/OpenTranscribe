"""Long-lived supervisor that watches local sources for filesystem events.

Runs in the **celery-beat** process (a single instance by design) and never in
the API or the workers. Its whole job is latency: dispatch
``watch_source.scan_single`` seconds after a file lands instead of waiting for
the source's ``polling_interval_minutes`` (15 by default).

Design constraints, in order of importance:

1. **Never break imports.** The Celery poll is untouched and remains the safety
   net; every failure here degrades to "polling only", is recorded in the status
   blob the UI reads, and is never raised into the beat process.
2. **Be honest about the mode.** ``auto`` verifies that native events actually
   arrive (see ``detection``) and falls back to ``PollingObserver`` when they do
   not — which is the normal outcome on macOS/Windows Docker bind mounts and on
   any NAS/SMB/NFS mount, regardless of host OS.
3. **Stay live.** Sources are added, edited, disabled, and deleted at runtime,
   so the watched set is reconciled against the database on a timer rather than
   being read once at startup.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.db.session_utils import session_scope
from app.models.watch_source import WatchSource
from app.services import watch_settings_service
from app.services.watch_sources.base import parse_extensions
from app.services.watch_sources.fs_events import detection
from app.services.watch_sources.fs_events import observers
from app.services.watch_sources.fs_events import status
from app.services.watch_sources.fs_events.dispatcher import ScanDispatcher
from app.services.watch_sources.fs_events.handler import WatchEventHandler

logger = logging.getLogger(__name__)

# A scan is only useful once the file has been quiet long enough for
# LocalWatchClient.list_files to stop treating it as "still being written",
# so the debounce window is the stability window plus a margin.
DEBOUNCE_MARGIN_SECONDS = 5.0
MIN_DEBOUNCE_SECONDS = 2.0
# Force a dispatch when a folder never goes quiet (a long continuous copy).
MAX_DEFER_SECONDS = 300.0


@dataclass(frozen=True)
class WatchPlan:
    """Everything needed to watch one source. Equality drives restart-on-change."""

    source_id: int
    path: str
    recursive: bool
    extensions: tuple[str, ...] | None
    debounce_seconds: float
    poll_seconds: int
    mode: str


@dataclass
class ActiveWatch:
    """A running observer plus how it was chosen."""

    plan: WatchPlan
    observer: Any
    handler: WatchEventHandler
    mode: str
    detail: str
    fs_type: str | None
    started_at: datetime


def build_plan(
    source: WatchSource,
    *,
    stability_seconds: int,
    mode: str,
    poll_seconds: int,
) -> WatchPlan:
    """Derive a :class:`WatchPlan` from a source row and the global settings."""
    extensions = parse_extensions(source.file_extensions)
    return WatchPlan(
        source_id=int(source.id),
        path=str(source.resolved_local_path),
        recursive=bool(source.recursive),
        extensions=tuple(extensions) if extensions else None,
        debounce_seconds=max(MIN_DEBOUNCE_SECONDS, stability_seconds + DEBOUNCE_MARGIN_SECONDS),
        poll_seconds=max(1, poll_seconds),
        mode=mode,
    )


class FsEventSupervisor:
    """Reconciles watched local sources and turns FS events into scan dispatches."""

    def __init__(
        self,
        *,
        reconcile_interval: float = 30.0,
        tick_interval: float = 1.0,
        probe_timeout: float = 5.0,
        dispatcher: ScanDispatcher | None = None,
        status_ttl: int = status.DEFAULT_TTL_SECONDS,
    ) -> None:
        self._reconcile_interval = reconcile_interval
        self._tick_interval = tick_interval
        self._probe_timeout = probe_timeout
        self._status_ttl = status_ttl
        self.dispatcher = dispatcher or ScanDispatcher()
        self._watches: dict[int, ActiveWatch] = {}
        # source_id → last reported failure detail, so a permanent misconfiguration
        # logs once instead of on every reconcile pass for the life of the process.
        self._reported_failures: dict[int, str] = {}
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None

    # ----- lifecycle ----------------------------------------------------- #
    def start(self) -> bool:
        """Spawn the daemon thread. Idempotent; returns False if already running."""
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._run, name="watch-source-fs-events", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the loop to exit and tear every observer down."""
        self._stop_flag.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._stop_all()
        self._thread = None

    @property
    def watched_source_ids(self) -> set[int]:
        return set(self._watches)

    def _run(self) -> None:
        logger.info(
            "Watch-source FS-event supervisor running (reconcile every %.0fs)",
            self._reconcile_interval,
        )
        next_reconcile = 0.0
        while not self._stop_flag.is_set():
            if time.monotonic() >= next_reconcile:
                try:
                    self.reconcile()
                except Exception as e:  # noqa: BLE001 - the loop must survive anything
                    logger.error("FS-event reconcile failed: %s", e, exc_info=True)
                next_reconcile = time.monotonic() + self._reconcile_interval
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                logger.error("FS-event dispatch tick failed: %s", e, exc_info=True)
            self._stop_flag.wait(self._tick_interval)
        logger.info("Watch-source FS-event supervisor stopped")

    def tick(self) -> list[int]:
        """Drain any debounced sources whose quiet window has elapsed."""
        return self.dispatcher.flush()

    # ----- reconciliation ------------------------------------------------ #
    def reconcile(self) -> dict[str, Any]:
        """Align the running observers with what the database currently asks for."""
        plans, gate = self.load_plans()
        if gate is not None:
            stopped = self._stop_all()
            return {"gated": gate, "watching": 0, "stopped": stopped}

        stopped = 0
        for source_id, active in list(self._watches.items()):
            if source_id not in plans or active.plan != plans[source_id]:
                self._stop_watch(source_id)
                stopped += 1

        started = failed = 0
        for source_id, plan in plans.items():
            if source_id in self._watches:
                continue
            if self._start_watch(plan):
                started += 1
            else:
                failed += 1

        self._publish_all()
        return {
            "gated": None,
            "watching": len(self._watches),
            "started": started,
            "stopped": stopped,
            "failed": failed,
        }

    def load_plans(self) -> tuple[dict[int, WatchPlan], str | None]:
        """Read the desired watch set from the DB.

        Returns ``(plans, gate)`` where a non-None ``gate`` explains why the
        whole layer is off (in which case ``plans`` is empty).
        """
        if not settings.WATCH_FOLDER_PATH:
            return {}, "WATCH_FOLDER_PATH is not mounted in this container"

        plans: dict[int, WatchPlan] = {}
        with session_scope() as db:
            if not watch_settings_service.is_enabled(db):
                return {}, "watch sources are disabled (watch.enabled)"
            if not watch_settings_service.fs_events_enabled(db):
                return {}, "FS events are disabled (watch.fs_events_enabled)"
            mode = watch_settings_service.fs_events_mode(db)
            if mode == "off":
                return {}, "FS events are forced off (watch.fs_events_mode=off)"
            stability = watch_settings_service.file_stability_seconds(db)
            poll_seconds = watch_settings_service.fs_events_poll_seconds(db)

            sources = (
                db.query(WatchSource)
                .filter(
                    WatchSource.is_enabled.is_(True),
                    WatchSource.source_type == "local",
                    WatchSource.use_fs_events.is_(True),
                )
                .all()
            )
            for source in sources:
                try:
                    plans[int(source.id)] = build_plan(
                        source, stability_seconds=stability, mode=mode, poll_seconds=poll_seconds
                    )
                except (ValueError, OSError) as e:
                    self._report_failure(int(source.id), status.MODE_ERROR, str(e))
        return plans, None

    # ----- per-source watch lifecycle ------------------------------------ #
    def _start_watch(self, plan: WatchPlan) -> bool:
        """Start the best available observer for one source. Never raises."""
        if not observers.watchdog_available():
            self._report_failure(
                plan.source_id,
                status.MODE_UNAVAILABLE,
                "the watchdog package is not installed in this image",
                plan,
            )
            return False

        handler = WatchEventHandler(plan.source_id, plan.extensions, self._on_event)
        detection.sweep_stale_probes(plan.path)
        try:
            observer, mode, detail, fs_type = self._select_observer(plan, handler)
        except Exception as e:  # noqa: BLE001 - fall back to Celery polling
            logger.debug("Observer selection failed for source %s", plan.source_id, exc_info=True)
            self._report_failure(
                plan.source_id, status.MODE_ERROR, f"{type(e).__name__}: {e}", plan
            )
            return False

        handler.disarm_probe()
        self._reported_failures.pop(plan.source_id, None)
        self._watches[plan.source_id] = ActiveWatch(
            plan=plan,
            observer=observer,
            handler=handler,
            mode=mode,
            detail=detail,
            fs_type=fs_type,
            started_at=datetime.now(UTC),
        )
        logger.info(
            "Watching source %s via %s observer (%s) at %s",
            plan.source_id,
            mode,
            detail,
            plan.path,
        )
        return True

    def _select_observer(
        self, plan: WatchPlan, handler: WatchEventHandler
    ) -> tuple[Any, str, str, str | None]:
        """Pick native vs polling, verifying native delivery in ``auto`` mode."""
        if plan.mode == "polling":
            observer, mode, detail = self._start_polling(
                plan, handler, "forced by watch.fs_events_mode"
            )
            return observer, mode, detail, detection.filesystem_type(plan.path)

        if plan.mode == "native":
            check = detection.DeliveryCheck(True, "forced by watch.fs_events_mode", None)
        else:
            check = detection.classify_path(plan.path)

        if not check.supported:
            observer, mode, detail = self._start_polling(plan, handler, check.reason)
            return observer, mode, detail, check.fs_type

        try:
            observer = observers.native_observer()
            observer.schedule(handler, plan.path, recursive=plan.recursive)
            observer.start()
        except Exception as e:  # noqa: BLE001 - any native failure means polling
            logger.warning(
                "Native observer unavailable for source %s (%s) — using the polling observer",
                plan.source_id,
                e,
            )
            reason = f"native observer failed to start ({type(e).__name__})"
            fallback, mode, detail = self._start_polling(plan, handler, reason)
            return fallback, mode, detail, check.fs_type

        if plan.mode == "native":
            return observer, status.MODE_NATIVE, check.reason, check.fs_type

        delivered, reason = detection.probe_delivery(
            plan.path, handler.arm_probe, self._probe_timeout
        )
        if delivered:
            return observer, status.MODE_NATIVE, reason, check.fs_type

        logger.info(
            "Native events are not delivered for source %s (%s) — using the polling observer",
            plan.source_id,
            reason,
        )
        observers.stop_observer(observer)
        fallback, mode, detail = self._start_polling(plan, handler, reason)
        return fallback, mode, detail, check.fs_type

    def _start_polling(
        self, plan: WatchPlan, handler: WatchEventHandler, reason: str
    ) -> tuple[Any, str, str]:
        """Start the cross-platform stat-sweep observer."""
        observer = observers.polling_observer(float(plan.poll_seconds))
        observer.schedule(handler, plan.path, recursive=plan.recursive)
        observer.start()
        return observer, status.MODE_POLLING, f"{reason}; sweeping every {plan.poll_seconds}s"

    def _report_failure(
        self, source_id: int, mode: str, detail: str, plan: WatchPlan | None = None
    ) -> None:
        """Record a start failure: publish it for the UI and log it once."""
        if self._reported_failures.get(source_id) != detail:
            logger.warning(
                "Watch source %s falls back to Celery polling only — %s", source_id, detail
            )
            self._reported_failures[source_id] = detail
        self._publish(source_id, mode, detail, None, plan)

    def _stop_watch(self, source_id: int) -> None:
        active = self._watches.pop(source_id, None)
        self._reported_failures.pop(source_id, None)
        if active is None:
            return
        observers.stop_observer(active.observer)
        self.dispatcher.forget(source_id)
        status.clear(source_id)

    def _stop_all(self) -> int:
        count = len(self._watches)
        for source_id in list(self._watches):
            self._stop_watch(source_id)
        # Sources that never started (error / unavailable) still have a status
        # blob claiming this layer knows about them — drop those too.
        for source_id in list(self._reported_failures):
            self._reported_failures.pop(source_id, None)
            status.clear(source_id)
        return count

    # ----- events + status ----------------------------------------------- #
    def _on_event(self, source_id: int) -> None:
        """Called from an observer thread for every event that survives filtering."""
        active = self._watches.get(source_id)
        debounce = active.plan.debounce_seconds if active else MIN_DEBOUNCE_SECONDS
        self.dispatcher.note_event(
            source_id, debounce_seconds=debounce, max_defer_seconds=MAX_DEFER_SECONDS
        )

    def _publish_all(self) -> None:
        for source_id, active in self._watches.items():
            self._publish(
                source_id,
                active.mode,
                active.detail,
                active.fs_type,
                active.plan,
                since=active.started_at,
            )

    def _publish(
        self,
        source_id: int,
        mode: str,
        detail: str,
        fs_type: str | None,
        plan: WatchPlan | None,
        since: datetime | None = None,
    ) -> None:
        stats = self.dispatcher.stats(source_id)
        last_event = self.dispatcher.last_event_at.get(source_id)
        status.publish(
            source_id,
            {
                "source_id": source_id,
                "mode": mode,
                "active": mode in (status.MODE_NATIVE, status.MODE_POLLING),
                "detail": detail,
                "fs_type": fs_type,
                "path": plan.path if plan else None,
                "debounce_seconds": plan.debounce_seconds if plan else None,
                "poll_seconds": plan.poll_seconds if plan and mode == status.MODE_POLLING else None,
                "since": since.isoformat() if since else None,
                "last_event_at": datetime.fromtimestamp(last_event, tz=UTC).isoformat()
                if last_event
                else None,
                "events_seen": stats["events_seen"],
                "scans_dispatched": stats["scans_dispatched"],
                "updated_at": datetime.now(UTC).isoformat(),
            },
            ttl=self._status_ttl,
        )


# --------------------------------------------------------------------------- #
# Process-wide singleton (started from the celery beat_init signal)
# --------------------------------------------------------------------------- #
_supervisor: FsEventSupervisor | None = None
_supervisor_mutex = threading.Lock()


def start_supervisor(**kwargs: Any) -> FsEventSupervisor | None:
    """Start the process-wide supervisor. Logs and returns None on any failure."""
    global _supervisor
    try:
        with _supervisor_mutex:
            if _supervisor is None:
                _supervisor = FsEventSupervisor(**kwargs)
                atexit.register(stop_supervisor)
            _supervisor.start()
        return _supervisor
    except Exception as e:  # noqa: BLE001 - must never take down the host process
        logger.error("Watch-source FS-event supervisor failed to start: %s", e, exc_info=True)
        return None


def stop_supervisor(timeout: float = 10.0) -> None:
    """Stop the process-wide supervisor if one is running."""
    global _supervisor
    with _supervisor_mutex:
        current, _supervisor = _supervisor, None
    if current is not None:
        try:
            current.stop(timeout)
        except Exception as e:  # noqa: BLE001 - shutdown is best-effort
            logger.debug("Error stopping the FS-event supervisor: %s", e)


def get_supervisor() -> FsEventSupervisor | None:
    """Return the running supervisor, if any (used by tests and diagnostics)."""
    return _supervisor
