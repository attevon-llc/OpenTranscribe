"""Supervisor tests: mode selection, fallback, reconciliation, degradation (issue #294).

Every observer here is a fake — the point is the *decision* logic, which is what
silently did the wrong thing on macOS/Windows/NAS deployments before this
feature existed. The one invariant that outranks all the others: a broken
observer degrades to Celery polling and never raises into the beat process.
"""

from __future__ import annotations

import contextlib
from typing import cast

import pytest

from app.models.watch_source import WatchSource
from app.services.watch_sources.fs_events import detection
from app.services.watch_sources.fs_events import observers as observers_module
from app.services.watch_sources.fs_events import status as status_module
from app.services.watch_sources.fs_events import supervisor as supervisor_module
from app.services.watch_sources.fs_events.dispatcher import ScanDispatcher
from app.services.watch_sources.fs_events.supervisor import FsEventSupervisor
from app.services.watch_sources.fs_events.supervisor import WatchPlan
from app.services.watch_sources.fs_events.supervisor import build_plan

pytestmark = pytest.mark.unit


class FakeObserver:
    """Stands in for watchdog's Observer / PollingObserver."""

    def __init__(self, kind: str, fail_on_start: bool = False) -> None:
        self.kind = kind
        self.fail_on_start = fail_on_start
        self.scheduled: list[tuple[str, bool]] = []
        self.started = False
        self.stopped = False

    def schedule(self, handler, path, recursive=False):  # noqa: ARG002
        self.scheduled.append((path, recursive))

    def start(self):
        if self.fail_on_start:
            raise RuntimeError("inotify watch limit reached")
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):  # noqa: ARG002
        return None


@pytest.fixture
def env(monkeypatch):
    """Fake observers + an in-memory status store; returns the recorded state."""
    made: dict[str, list[FakeObserver]] = {"native": [], "polling": []}
    fail: dict[str, bool] = {"native": False, "polling": False}
    published: dict[int, dict] = {}

    def native():
        obs = FakeObserver("native", fail_on_start=fail["native"])
        made["native"].append(obs)
        return obs

    def polling(timeout):  # noqa: ARG001
        obs = FakeObserver("polling", fail_on_start=fail["polling"])
        made["polling"].append(obs)
        return obs

    monkeypatch.setattr(observers_module, "watchdog_available", lambda: True)
    monkeypatch.setattr(observers_module, "native_observer", native)
    monkeypatch.setattr(observers_module, "polling_observer", polling)
    monkeypatch.setattr(
        status_module,
        "publish",
        lambda sid, payload, ttl=0: published.update({sid: payload}) or True,
    )
    monkeypatch.setattr(status_module, "clear", lambda sid: published.pop(sid, None) is not None)
    return {"made": made, "fail": fail, "published": published}


def make_plan(source_id: int = 1, mode: str = "auto", path: str = "/watch/a") -> WatchPlan:
    return WatchPlan(
        source_id=source_id,
        path=path,
        recursive=True,
        extensions=(".mp4",),
        debounce_seconds=35.0,
        poll_seconds=15,
        mode=mode,
    )


def make_supervisor(plans: list[WatchPlan], monkeypatch, gate: str | None = None):
    sup = FsEventSupervisor(
        dispatcher=ScanDispatcher(dispatch=lambda _sid: None), probe_timeout=0.01
    )
    monkeypatch.setattr(
        sup, "load_plans", lambda: ({} if gate else {p.source_id: p for p in plans}, gate)
    )
    return sup


# --------------------------------------------------------------------------- #
# Mode selection and fallback
# --------------------------------------------------------------------------- #
def test_auto_keeps_the_native_observer_when_the_probe_confirms_delivery(env, monkeypatch):
    monkeypatch.setattr(
        detection, "classify_path", lambda _p: detection.DeliveryCheck(True, "ext4", "ext4")
    )
    monkeypatch.setattr(detection, "probe_delivery", lambda *a, **k: (True, "verified"))
    sup = make_supervisor([make_plan()], monkeypatch)

    assert sup.reconcile()["watching"] == 1
    assert env["made"]["native"][0].started is True
    assert env["made"]["polling"] == []
    assert env["published"][1]["mode"] == status_module.MODE_NATIVE
    sup.stop(0.1)


def test_auto_uses_polling_on_a_network_mount_without_even_trying_native(env, monkeypatch):
    """NFS/SMB/NAS: inotify is kernel-local and never sees a remote writer."""
    monkeypatch.setattr(
        detection,
        "classify_path",
        lambda _p: detection.DeliveryCheck(False, "nfs4 is a network mount", "nfs4"),
    )
    sup = make_supervisor([make_plan()], monkeypatch)

    assert sup.reconcile()["watching"] == 1
    assert env["made"]["native"] == []
    assert env["made"]["polling"][0].started is True
    published = env["published"][1]
    assert published["mode"] == status_module.MODE_POLLING
    assert published["fs_type"] == "nfs4"
    assert "network mount" in published["detail"]
    sup.stop(0.1)


def test_auto_falls_back_to_polling_when_the_probe_sees_no_event(env, monkeypatch):
    """macOS / Windows Docker bind mounts: the fs type looks fine, events never come."""
    monkeypatch.setattr(
        detection, "classify_path", lambda _p: detection.DeliveryCheck(True, "ext4", "ext4")
    )
    monkeypatch.setattr(detection, "probe_delivery", lambda *a, **k: (False, "no native event"))
    sup = make_supervisor([make_plan()], monkeypatch)

    assert sup.reconcile()["watching"] == 1
    # The speculative native observer must be torn down, not leaked.
    assert env["made"]["native"][0].stopped is True
    assert env["made"]["polling"][0].started is True
    assert env["published"][1]["mode"] == status_module.MODE_POLLING
    sup.stop(0.1)


def test_native_mode_skips_the_probe_entirely(env, monkeypatch):
    def explode(*_a, **_k):
        raise AssertionError("forced native mode must not probe")

    monkeypatch.setattr(detection, "probe_delivery", explode)
    sup = make_supervisor([make_plan(mode="native")], monkeypatch)

    sup.reconcile()
    assert env["made"]["native"][0].started is True
    assert env["published"][1]["mode"] == status_module.MODE_NATIVE
    sup.stop(0.1)


def test_polling_mode_never_starts_the_native_observer(env, monkeypatch):
    monkeypatch.setattr(detection, "filesystem_type", lambda _p: "ext4")
    sup = make_supervisor([make_plan(mode="polling")], monkeypatch)

    sup.reconcile()
    assert env["made"]["native"] == []
    assert env["published"][1]["mode"] == status_module.MODE_POLLING
    assert "15s" in env["published"][1]["detail"]
    sup.stop(0.1)


def test_a_native_observer_that_cannot_start_falls_back_to_polling(env, monkeypatch):
    monkeypatch.setattr(
        detection, "classify_path", lambda _p: detection.DeliveryCheck(True, "ext4", "ext4")
    )
    env["fail"]["native"] = True
    sup = make_supervisor([make_plan()], monkeypatch)

    assert sup.reconcile()["watching"] == 1
    assert env["published"][1]["mode"] == status_module.MODE_POLLING
    sup.stop(0.1)


# --------------------------------------------------------------------------- #
# Degradation — the invariant that outranks everything else
# --------------------------------------------------------------------------- #
def test_a_total_observer_failure_degrades_to_polling_instead_of_raising(env, monkeypatch):
    monkeypatch.setattr(
        detection, "classify_path", lambda _p: detection.DeliveryCheck(True, "ext4", "ext4")
    )
    env["fail"]["native"] = True
    env["fail"]["polling"] = True
    sup = make_supervisor([make_plan()], monkeypatch)

    result = sup.reconcile()  # must not raise
    assert result["watching"] == 0
    assert result["failed"] == 1
    assert env["published"][1]["mode"] == status_module.MODE_ERROR
    assert env["published"][1]["active"] is False


def test_watchdog_missing_reports_unavailable_rather_than_pretending(env, monkeypatch):
    monkeypatch.setattr(observers_module, "watchdog_available", lambda: False)
    sup = make_supervisor([make_plan()], monkeypatch)

    assert sup.reconcile()["failed"] == 1
    assert env["published"][1]["mode"] == status_module.MODE_UNAVAILABLE


def test_a_reconcile_exception_never_escapes_the_supervisor_loop(env, monkeypatch):
    sup = FsEventSupervisor(dispatcher=ScanDispatcher(dispatch=lambda _sid: None))

    def boom():
        raise RuntimeError("database is down")

    monkeypatch.setattr(sup, "load_plans", boom)
    sup.start()
    try:
        # The thread runs reconcile immediately; it must still be alive after.
        sup._stop_flag.wait(0.2)
        assert sup._thread is not None and sup._thread.is_alive()
    finally:
        sup.stop(1.0)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def test_reconcile_starts_new_sources_and_stops_removed_ones(env, monkeypatch):
    monkeypatch.setattr(
        detection, "classify_path", lambda _p: detection.DeliveryCheck(False, "polling", "nfs4")
    )
    plans = [make_plan(1), make_plan(2, path="/watch/b")]
    state = {"plans": plans}
    sup = FsEventSupervisor(dispatcher=ScanDispatcher(dispatch=lambda _sid: None))
    monkeypatch.setattr(sup, "load_plans", lambda: ({p.source_id: p for p in state["plans"]}, None))

    sup.reconcile()
    assert sup.watched_source_ids == {1, 2}

    state["plans"] = [plans[0]]  # source 2 disabled / deleted at runtime
    sup.reconcile()
    assert sup.watched_source_ids == {1}
    assert env["made"]["polling"][1].stopped is True
    assert 2 not in env["published"]
    sup.stop(0.1)


def test_reconcile_restarts_a_source_whose_config_changed(env, monkeypatch):
    monkeypatch.setattr(
        detection, "classify_path", lambda _p: detection.DeliveryCheck(False, "polling", "nfs4")
    )
    state = {"plan": make_plan(1, path="/watch/a")}
    sup = FsEventSupervisor(dispatcher=ScanDispatcher(dispatch=lambda _sid: None))
    monkeypatch.setattr(sup, "load_plans", lambda: ({1: state["plan"]}, None))

    sup.reconcile()
    first = env["made"]["polling"][0]
    assert first.scheduled == [("/watch/a", True)]

    state["plan"] = make_plan(1, path="/watch/moved")  # the admin edited the folder
    sup.reconcile()
    assert first.stopped is True
    assert env["made"]["polling"][1].scheduled == [("/watch/moved", True)]
    assert sup.watched_source_ids == {1}
    sup.stop(0.1)


def test_a_gate_stops_every_watch_and_clears_its_status(env, monkeypatch):
    monkeypatch.setattr(
        detection, "classify_path", lambda _p: detection.DeliveryCheck(False, "polling", "nfs4")
    )
    state: dict[str, str | None] = {"gate": None}
    plan = make_plan(1)
    sup = FsEventSupervisor(dispatcher=ScanDispatcher(dispatch=lambda _sid: None))
    monkeypatch.setattr(
        sup, "load_plans", lambda: (({} if state["gate"] else {1: plan}), state["gate"])
    )

    sup.reconcile()
    assert sup.watched_source_ids == {1}

    state["gate"] = "FS events are disabled (watch.fs_events_enabled)"
    result = sup.reconcile()
    assert result["gated"] == state["gate"]
    assert sup.watched_source_ids == set()
    assert env["published"] == {}  # the UI must stop claiming a live watcher


# --------------------------------------------------------------------------- #
# Plan construction + DB gating
# --------------------------------------------------------------------------- #
class FakeSource:
    """The handful of ``WatchSource`` attributes ``build_plan`` actually reads."""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.name = kwargs.get("name", "src")
        self.file_extensions = kwargs.get("file_extensions")
        self.recursive = kwargs.get("recursive", True)
        self._path = kwargs.get("path", "/watch/a")

    @property
    def resolved_local_path(self):
        if self._path is None:
            raise ValueError("Resolved path escapes watch root")
        return self._path


def fake_source(**kwargs) -> WatchSource:
    """Typed alias so mypy accepts the stand-in where a real row is declared."""
    return cast("WatchSource", FakeSource(**kwargs))


def test_build_plan_debounce_outlasts_the_file_stability_window():
    """A scan fired before the stability window would find nothing to import."""
    plan = build_plan(fake_source(), stability_seconds=30, mode="auto", poll_seconds=15)
    assert plan.debounce_seconds > 30
    assert plan.extensions is None  # no filter = all media

    zero = build_plan(fake_source(), stability_seconds=0, mode="auto", poll_seconds=15)
    assert zero.debounce_seconds >= supervisor_module.MIN_DEBOUNCE_SECONDS


def test_build_plan_normalizes_the_extension_filter():
    plan = build_plan(
        fake_source(file_extensions="MP4, .mp3"), stability_seconds=5, mode="auto", poll_seconds=1
    )
    assert plan.extensions == (".mp4", ".mp3")


@pytest.mark.parametrize(
    ("settings_values", "expected_gate_fragment"),
    [
        ({"enabled": False}, "watch.enabled"),
        ({"fs_events": False}, "watch.fs_events_enabled"),
        ({"mode": "off"}, "watch.fs_events_mode=off"),
    ],
)
def test_load_plans_gates_on_the_db_settings(monkeypatch, settings_values, expected_gate_fragment):
    monkeypatch.setattr(supervisor_module.settings, "WATCH_FOLDER_PATH", "/watch")
    monkeypatch.setattr(
        supervisor_module, "session_scope", lambda: contextlib.nullcontext(object())
    )
    svc = supervisor_module.watch_settings_service
    monkeypatch.setattr(svc, "is_enabled", lambda _db: settings_values.get("enabled", True))
    monkeypatch.setattr(
        svc, "fs_events_enabled", lambda _db: settings_values.get("fs_events", True)
    )
    monkeypatch.setattr(svc, "fs_events_mode", lambda _db: settings_values.get("mode", "auto"))

    plans, gate = FsEventSupervisor().load_plans()
    assert plans == {}
    assert gate is not None and expected_gate_fragment in gate


def test_load_plans_gates_when_the_watch_folder_is_not_mounted(monkeypatch):
    monkeypatch.setattr(supervisor_module.settings, "WATCH_FOLDER_PATH", "")
    plans, gate = FsEventSupervisor().load_plans()
    assert plans == {}
    assert gate is not None and "WATCH_FOLDER_PATH" in gate


def test_load_plans_records_an_error_for_an_unresolvable_path(env, monkeypatch):
    monkeypatch.setattr(supervisor_module.settings, "WATCH_FOLDER_PATH", "/watch")
    svc = supervisor_module.watch_settings_service
    monkeypatch.setattr(svc, "is_enabled", lambda _db: True)
    monkeypatch.setattr(svc, "fs_events_enabled", lambda _db: True)
    monkeypatch.setattr(svc, "fs_events_mode", lambda _db: "auto")
    monkeypatch.setattr(svc, "file_stability_seconds", lambda _db: 30)
    monkeypatch.setattr(svc, "fs_events_poll_seconds", lambda _db: 15)

    class FakeQuery:
        def filter(self, *_a):
            return self

        def all(self):
            return [fake_source(id=9, path=None)]

    class FakeSession:
        def query(self, *_a):
            return FakeQuery()

    monkeypatch.setattr(
        supervisor_module, "session_scope", lambda: contextlib.nullcontext(FakeSession())
    )

    plans, gate = FsEventSupervisor().load_plans()
    assert gate is None
    assert plans == {}
    assert env["published"][9]["mode"] == status_module.MODE_ERROR
