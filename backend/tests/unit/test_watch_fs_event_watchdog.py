"""End-to-end checks against **real** watchdog observers (issue #294).

The other two FS-event suites fake the observer to test decisions. This one
wires the actual thing up, because two claims cannot be verified any other way:

1. ``WatchEventHandler`` is duck-typed (a bare ``dispatch(event)``, not a
   ``FileSystemEventHandler`` subclass) so the module stays importable without
   watchdog — that only holds if watchdog really does call it.
2. The probe → filter → debounce → dispatch chain works on live events.

The polling half is deterministic everywhere. The native half **skips with the
reason** when the filesystem does not deliver events, which is the documented,
expected outcome on macOS/Windows Docker mounts and on network mounts.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.services.watch_sources.fs_events import detection
from app.services.watch_sources.fs_events import observers
from app.services.watch_sources.fs_events.dispatcher import ScanDispatcher
from app.services.watch_sources.fs_events.handler import WatchEventHandler

pytestmark = pytest.mark.unit

pytest.importorskip("watchdog", reason="watchdog is not installed in this image")

POLL_INTERVAL = 0.2
DELIVERY_TIMEOUT = 10.0


def _wait_for(predicate, timeout: float = DELIVERY_TIMEOUT, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _run_chain(observer, tmp_path, dispatcher: ScanDispatcher, handler: WatchEventHandler):
    """Schedule ``handler`` on ``tmp_path`` and drain the dispatcher until it fires."""
    observer.schedule(handler, str(tmp_path), recursive=True)
    observer.start()
    try:
        (tmp_path / "session.mp4").write_bytes(b"\x00" * 1024)
        assert _wait_for(lambda: dispatcher.events_seen.get(42, 0) > 0), (
            "no filesystem event reached the handler"
        )
        assert _wait_for(lambda: dispatcher.flush() == [42]), (
            "the debounced scan was never dispatched"
        )
    finally:
        observers.stop_observer(observer, timeout=2.0)


@pytest.fixture
def chain(monkeypatch):
    """A dispatcher whose Redis lock is granted, plus a real handler."""
    import contextlib

    from app.services.watch_sources.fs_events import dispatcher as dispatcher_module

    @contextlib.contextmanager
    def always_acquire(_key, timeout=0, blocking_timeout=0):
        yield True

    monkeypatch.setattr(
        dispatcher_module.task_lock_manager, "acquire_lock", always_acquire, raising=False
    )
    calls: list[int] = []
    disp = ScanDispatcher(dispatch=calls.append)
    handler = WatchEventHandler(
        42,
        (".mp4",),
        lambda sid: disp.note_event(sid, debounce_seconds=0.2, max_defer_seconds=5.0),
    )
    return disp, handler, calls


def test_polling_observer_drives_the_whole_chain(tmp_path, chain):
    """The cross-platform fallback must work on any filesystem, unconditionally."""
    disp, handler, calls = chain
    _run_chain(observers.polling_observer(POLL_INTERVAL), tmp_path, disp, handler)
    assert calls == [42]


def test_native_observer_drives_the_whole_chain_where_events_are_delivered(tmp_path, chain):
    disp, handler, calls = chain
    observer = observers.native_observer()
    observer.schedule(handler, str(tmp_path), recursive=True)
    observer.start()
    try:
        delivered, reason = detection.probe_delivery(tmp_path, handler.arm_probe, timeout=3.0)
        if not delivered:
            pytest.skip(f"native events are not delivered on this filesystem: {reason}")
        handler.disarm_probe()
        (tmp_path / "session.mp4").write_bytes(b"\x00" * 1024)
        assert _wait_for(lambda: disp.events_seen.get(42, 0) > 0)
        assert _wait_for(lambda: disp.flush() == [42])
    finally:
        observers.stop_observer(observer, timeout=2.0)
    assert calls == [42]


def test_the_probe_answers_through_a_real_observer(tmp_path):
    """``arm_probe`` must be reachable from watchdog's own dispatch path."""
    handler = WatchEventHandler(1, (".mp4",), lambda _sid: None)
    observer = observers.polling_observer(POLL_INTERVAL)
    observer.schedule(handler, str(tmp_path), recursive=False)
    observer.start()
    try:
        delivered, reason = detection.probe_delivery(tmp_path, handler.arm_probe, timeout=5.0)
    finally:
        observers.stop_observer(observer, timeout=2.0)
    assert delivered is True, reason
    assert list(tmp_path.iterdir()) == []  # the probe file cleaned itself up


def test_filtered_events_never_reach_the_dispatcher(tmp_path, chain):
    """A partial download must not trigger a scan through a real observer."""
    disp, handler, calls = chain
    observer = observers.polling_observer(POLL_INTERVAL)
    observer.schedule(handler, str(tmp_path), recursive=True)
    observer.start()
    try:
        (tmp_path / "session.mp4.part").write_bytes(b"\x00" * 512)
        (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
        time.sleep(POLL_INTERVAL * 6)
        assert disp.events_seen.get(42, 0) == 0
        assert disp.flush() == []
    finally:
        observers.stop_observer(observer, timeout=2.0)
    assert calls == []


def test_stopping_an_observer_is_idempotent_and_quiet():
    observer = observers.polling_observer(POLL_INTERVAL)
    observer.start()
    observers.stop_observer(observer, timeout=2.0)
    observers.stop_observer(observer, timeout=2.0)  # must not raise
    observers.stop_observer(None)


def test_watchdog_available_reports_true_when_installed():
    assert observers.watchdog_available() is True


def test_the_handler_is_accepted_by_watchdog_without_subclassing(tmp_path):
    """Regression guard for the duck-typed ``dispatch`` contract."""
    seen = threading.Event()
    handler = WatchEventHandler(5, None, lambda _sid: seen.set())
    observer = observers.polling_observer(POLL_INTERVAL)
    observer.schedule(handler, str(tmp_path), recursive=False)
    observer.start()
    try:
        (tmp_path / "clip.wav").write_bytes(b"RIFF")
        assert seen.wait(DELIVERY_TIMEOUT), "watchdog never called handler.dispatch()"
    finally:
        observers.stop_observer(observer, timeout=2.0)
