"""Unit tests for the watch-source FS-event plumbing (issue #294).

Covers the three pieces that decide *whether* and *when* a scan is dispatched:
mount classification, event filtering, and debouncing. The supervisor that ties
them together is exercised in ``test_watch_fs_event_supervisor.py``.
"""

from __future__ import annotations

import threading

import pytest

from app.services.watch_sources.fs_events import detection
from app.services.watch_sources.fs_events.dispatcher import ScanDispatcher
from app.services.watch_sources.fs_events.handler import WatchEventHandler

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Mount classification
# --------------------------------------------------------------------------- #
MOUNTINFO = """\
25 30 0:23 / /proc rw,nosuid - proc proc rw
30 0 8:1 / / rw,relatime - ext4 /dev/sda1 rw
101 30 0:57 / /watch rw,relatime - nfs4 nas:/media rw,vers=4.2
102 30 0:58 / /host/mac rw,relatime shared:1 master:2 - fuse.grpcfuse grpcfuse rw
103 30 0:59 / /win/c rw,relatime - 9p drvfs rw
104 30 0:60 / /my\\040files rw,relatime - ext4 /dev/sdb1 rw
"""


def test_parse_mountinfo_handles_optional_fields_and_escapes():
    mounts = dict(detection.parse_mountinfo(MOUNTINFO))
    # The line with optional fields ("shared:1 master:2") before " - " parses.
    assert mounts["/host/mac"] == "fuse.grpcfuse"
    assert mounts["/watch"] == "nfs4"
    # \040 is mountinfo's escape for a space.
    assert mounts["/my files"] == "ext4"


def test_filesystem_type_picks_the_longest_matching_mount(tmp_path, monkeypatch):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(MOUNTINFO, encoding="utf-8")
    monkeypatch.setattr(detection, "MOUNTINFO_PATH", str(mountinfo))
    # "/" also matches, but "/watch" is longer and therefore the real mount.
    assert detection.filesystem_type("/watch/session-a/take1.mp4") == "nfs4"
    assert detection.filesystem_type("/srv/media") == "ext4"


def test_filesystem_type_returns_none_without_procfs(monkeypatch):
    monkeypatch.setattr(detection, "MOUNTINFO_PATH", "/nonexistent/mountinfo")
    assert detection.filesystem_type("/watch") is None


@pytest.mark.parametrize(
    ("path", "expected_supported"),
    [
        ("/watch/x", False),  # nfs4 — a NAS writer never raises inotify
        ("/host/mac/x", False),  # Docker Desktop macOS bind mount (FUSE)
        ("/win/c/x", False),  # WSL2 Windows drive
        ("/srv/media/x", True),  # plain ext4
    ],
)
def test_classify_path_rejects_mounts_that_cannot_deliver(
    tmp_path, monkeypatch, path, expected_supported
):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(MOUNTINFO, encoding="utf-8")
    monkeypatch.setattr(detection, "MOUNTINFO_PATH", str(mountinfo))
    check = detection.classify_path(path)
    assert check.supported is expected_supported
    assert check.reason


def test_classify_path_defers_to_the_probe_when_the_mount_is_unknown(monkeypatch):
    monkeypatch.setattr(detection, "filesystem_type", lambda _p: None)
    check = detection.classify_path("/watch")
    assert check.supported is True
    assert "probe" in check.reason


# --------------------------------------------------------------------------- #
# Live delivery probe
# --------------------------------------------------------------------------- #
def test_probe_delivery_confirms_and_cleans_up(tmp_path):
    seen: list[str] = []

    def arm(name: str) -> threading.Event:
        seen.append(name)
        event = threading.Event()
        event.set()  # stand in for an observer that delivers immediately
        return event

    delivered, reason = detection.probe_delivery(tmp_path, arm, timeout=0.1)
    assert delivered is True
    assert "verified" in reason
    assert seen and seen[0].startswith(detection.PROBE_PREFIX)
    # The probe file must never be left behind for the scanner to find.
    assert list(tmp_path.iterdir()) == []


def test_probe_delivery_reports_no_events_and_still_cleans_up(tmp_path):
    delivered, reason = detection.probe_delivery(tmp_path, lambda _n: threading.Event(), 0.05)
    assert delivered is False
    assert "no native event" in reason
    assert list(tmp_path.iterdir()) == []


def test_probe_delivery_treats_an_unwritable_directory_as_unsupported(tmp_path):
    missing = tmp_path / "does-not-exist"
    delivered, reason = detection.probe_delivery(missing, lambda _n: threading.Event(), 0.05)
    assert delivered is False
    assert "probe" in reason


def test_sweep_stale_probes_removes_orphans(tmp_path):
    (tmp_path / f"{detection.PROBE_PREFIX}deadbeef").write_text("x", encoding="utf-8")
    (tmp_path / "recording.mp4").write_text("x", encoding="utf-8")
    assert detection.sweep_stale_probes(tmp_path) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["recording.mp4"]


# --------------------------------------------------------------------------- #
# Event filtering
# --------------------------------------------------------------------------- #
class FakeEvent:
    def __init__(self, src_path="", event_type="created", is_directory=False, dest_path=""):
        self.src_path = src_path
        self.dest_path = dest_path
        self.event_type = event_type
        self.is_directory = is_directory


def _handler(extensions=None):
    fired: list[int] = []
    return WatchEventHandler(7, extensions, fired.append), fired


def test_handler_dispatches_for_a_new_media_file():
    handler, fired = _handler()
    handler.dispatch(FakeEvent("/watch/a/session.mp4"))
    assert fired == [7]


@pytest.mark.parametrize(
    "event",
    [
        FakeEvent("/watch/a", is_directory=True),
        FakeEvent("/watch/a/session.mp4", event_type="deleted"),
        FakeEvent("/watch/a/session.mp4.part"),
        FakeEvent("/watch/a/.hidden.mp4"),
    ],
)
def test_handler_ignores_noise(event):
    handler, fired = _handler()
    handler.dispatch(event)
    assert fired == []


def test_handler_applies_the_source_extension_filter():
    handler, fired = _handler((".mp4", ".mp3"))
    handler.dispatch(FakeEvent("/watch/a/notes.txt"))
    assert fired == []
    handler.dispatch(FakeEvent("/watch/a/SESSION.MP4"))
    assert fired == [7]


def test_handler_uses_the_destination_of_a_rename():
    """rsync/browsers write ``foo.mp4.part`` then rename — the rename is the signal."""
    handler, fired = _handler((".mp4",))
    handler.dispatch(
        FakeEvent("/watch/a/x.mp4.part", event_type="moved", dest_path="/watch/a/x.mp4")
    )
    assert fired == [7]


def test_handler_answers_the_probe_before_any_filtering():
    handler, fired = _handler((".mp4",))
    name = f"{detection.PROBE_PREFIX}abc123"
    event = handler.arm_probe(name)
    handler.dispatch(FakeEvent(f"/watch/a/{name}"))
    assert event.is_set()
    assert fired == []  # the probe is never mistaken for content


def test_handler_never_raises_on_a_malformed_event():
    handler, fired = _handler()
    handler.dispatch(object())
    assert fired == []


# --------------------------------------------------------------------------- #
# Debounce / dispatch
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def dispatcher(monkeypatch):
    """A dispatcher with a fake clock, a recording sink, and the lock granted."""
    import contextlib

    from app.services.watch_sources.fs_events import dispatcher as dispatcher_module

    @contextlib.contextmanager
    def always_acquire(_key, timeout=0, blocking_timeout=0):
        yield True

    monkeypatch.setattr(
        dispatcher_module.task_lock_manager, "acquire_lock", always_acquire, raising=False
    )
    clock = FakeClock()
    calls: list[int] = []
    return ScanDispatcher(dispatch=calls.append, clock=clock), clock, calls


def test_a_burst_of_events_produces_exactly_one_scan(dispatcher):
    disp, clock, calls = dispatcher
    for _ in range(200):
        disp.note_event(3, debounce_seconds=10)
        clock.advance(0.01)

    assert disp.flush() == []  # still inside the quiet window
    clock.advance(10)
    assert disp.flush() == [3]
    assert calls == [3]

    clock.advance(60)
    assert disp.flush() == []  # nothing pending — no repeat dispatch


def test_the_debounce_window_covers_the_file_stability_check(dispatcher):
    """Dispatching early would scan a file list_files still calls "still writing"."""
    disp, clock, calls = dispatcher
    disp.note_event(3, debounce_seconds=35)
    clock.advance(34)
    assert disp.flush() == []
    clock.advance(1)
    assert disp.flush() == [3]


def test_continuous_churn_still_dispatches_via_max_defer(dispatcher):
    disp, clock, _calls = dispatcher
    for _ in range(30):
        disp.note_event(3, debounce_seconds=30, max_defer_seconds=60)
        clock.advance(5)
        if disp.due():
            break
    assert disp.flush() == [3]


def test_cooldown_suppresses_a_second_dispatch(dispatcher):
    disp, clock, calls = dispatcher
    disp.note_event(3, debounce_seconds=5, cooldown_seconds=100)
    clock.advance(5)
    assert disp.flush() == [3]

    disp.note_event(3, debounce_seconds=5, cooldown_seconds=100)
    clock.advance(5)
    assert disp.flush() == []  # inside the cooldown
    clock.advance(100)
    assert disp.flush() == [3]
    assert calls == [3, 3]


def test_events_for_different_sources_are_independent(dispatcher):
    disp, clock, calls = dispatcher
    disp.note_event(1, debounce_seconds=5)
    disp.note_event(2, debounce_seconds=5)
    clock.advance(5)
    assert sorted(disp.flush()) == [1, 2]
    assert sorted(calls) == [1, 2]


def test_a_lock_held_elsewhere_suppresses_the_duplicate_dispatch(monkeypatch):
    """Two supervisor replicas must not both enqueue the same scan."""
    import contextlib

    from app.services.watch_sources.fs_events import dispatcher as dispatcher_module

    @contextlib.contextmanager
    def never_acquire(_key, timeout=0, blocking_timeout=0):
        yield False

    monkeypatch.setattr(
        dispatcher_module.task_lock_manager, "acquire_lock", never_acquire, raising=False
    )
    clock = FakeClock()
    calls: list[int] = []
    disp = ScanDispatcher(dispatch=calls.append, clock=clock)
    disp.note_event(3, debounce_seconds=1)
    clock.advance(2)
    assert disp.flush() == []
    assert calls == []


def test_a_dispatch_failure_is_swallowed(dispatcher, monkeypatch):
    disp, clock, _calls = dispatcher

    def boom(_source_id):
        raise RuntimeError("broker down")

    monkeypatch.setattr(disp, "_dispatch", boom)
    disp.note_event(3, debounce_seconds=1)
    clock.advance(2)
    assert disp.flush() == []  # reported as "not dispatched", never raised


def test_forget_drops_all_state_for_a_removed_source(dispatcher):
    disp, clock, _calls = dispatcher
    disp.note_event(3, debounce_seconds=1)
    disp.forget(3)
    clock.advance(10)
    assert disp.flush() == []
    assert disp.stats(3)["events_seen"] == 0
