"""The download supervisor: a real child process, a mock hub and a real Redis (#1246).

Nothing here reaches the network. ``loopback_only`` blocks every non-loopback socket and DNS
lookup, and the supervisor is given the mock hub's endpoint explicitly (the child gets it as
``HF_ENDPOINT``), so a catalog id - a real repository id - cannot fall through to huggingface.co.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest
from fixtures.hf_cache import make_complete_repo
from fixtures.mock_hf_hub import default_repo

from app.core import local_whisper_catalog as catalog
from app.services.local_models import cache
from app.services.local_models import downloader
from app.services.local_models import supervisor
from app.services.local_models.state import DownloadStore

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]


def _entry(model_id: str) -> catalog.LocalWhisperModel:
    entry = catalog.get(model_id)
    assert entry is not None
    return entry


TINY = _entry("tiny")
BASE = _entry("base")
TOKEN = "hf_supervisor_secret_token"


@pytest.fixture(autouse=True)
def _never_reach_the_real_hub(loopback_only):
    """Every test here runs behind the loopback-only network guard."""


@pytest.fixture
def hub(mock_hf_hub):
    mock_hf_hub.add_repo(default_repo(TINY.repo_id))
    return mock_hf_hub


@pytest.fixture
def store(local_model_redis, lm_namespace):
    return DownloadStore(local_model_redis, lm_namespace)


@pytest.fixture
def children(monkeypatch):
    """Record every child the supervisor starts (argv, env, process) and still start it."""
    started: list[dict] = []

    class Recording(subprocess.Popen[bytes]):
        def __init__(self, cmd, *args, **kwargs):
            super().__init__(cmd, *args, **kwargs)
            started.append({"cmd": list(cmd), "env": kwargs.get("env") or {}, "proc": self})

    monkeypatch.setattr(supervisor.subprocess, "Popen", Recording)
    return started


class Run:
    """One supervised download, with the notifications it produced."""

    def __init__(self, store: DownloadStore, hub, task_id: str = "task-1", **kwargs):
        self.store, self.hub, self.task_id = store, hub, task_id
        self.events: list[dict] = []
        self.kwargs = {"token": None, "poll_seconds": 0.05, **kwargs}
        self.status: str | None = None
        self.error: BaseException | None = None

    def go(self, entry=TINY) -> str:
        self.store.try_acquire(entry.id, self.task_id, 7)
        self.status = supervisor.run_download(
            entry,
            task_id=self.task_id,
            user_id=7,
            store=self.store,
            notify=self.events.append,
            endpoint=self.hub.url,
            **self.kwargs,
        )
        return self.status

    def go_in_thread(self, entry=TINY) -> threading.Thread:
        def target() -> None:
            try:
                self.go(entry)
            except BaseException as exc:  # surfaced by the test, not swallowed
                self.error = exc

        thread = threading.Thread(target=target)
        thread.start()
        return thread


def _wait_for_partial(entry=TINY, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cache.has_partial(entry.repo_id):
            return
        time.sleep(0.02)
    pytest.fail("the download never produced a partial file")


def test_a_download_completes_with_monotonic_progress_ending_at_the_total(hub, store, children):
    run = Run(store, hub)

    assert run.go() == "completed"

    done = [e["bytes_done"] for e in run.events]
    assert done == sorted(done)
    assert run.events[-1]["status"] == "completed"
    assert run.events[-1]["bytes_done"] == run.events[-1]["bytes_total"] > 0
    assert run.events[-1]["percent"] == 100.0
    assert cache.is_complete(TINY.repo_id) is True
    assert cache.has_partial(TINY.repo_id) is False
    assert store.current_lock() is None
    final = store.read_state()
    assert final is not None
    assert final["status"] == "completed"
    assert len(children) == 1


def test_progress_payloads_carry_no_task_or_user_identity(hub, store, children):
    run = Run(store, hub)
    run.go()

    assert len(run.events) >= 2
    for event in run.events:
        assert set(event) == {
            "model_id",
            "status",
            "bytes_done",
            "bytes_total",
            "percent",
            "error_code",
        }


def test_the_token_reaches_the_child_only_through_its_environment(hub, store, children):
    run = Run(store, hub, token=TOKEN)

    assert run.go() == "completed"

    child = children[0]
    assert TOKEN not in " ".join(child["cmd"])
    assert child["env"]["HF_TOKEN"] == TOKEN
    assert f"Bearer {TOKEN}" in hub.authorizations()
    assert TOKEN not in repr(run.events)
    assert TOKEN not in repr(store.read_state())


def test_a_run_without_a_token_hides_an_ambient_one_from_the_child(
    hub, store, children, monkeypatch
):
    monkeypatch.setenv("HF_TOKEN", "hf_ambient_should_not_leak")
    run = Run(store, hub, token=None)

    run.go()

    assert "HF_TOKEN" not in children[0]["env"]


def test_cancel_mid_download_stops_the_child_and_removes_only_this_repos_partials(
    hub, store, children, hf_cache
):
    hub.mode = "slow"
    make_complete_repo(hf_cache, BASE.repo_id)
    neighbour_blobs = sorted(p.name for p in (cache.repo_dir(BASE.repo_id) / "blobs").iterdir())
    run = Run(store, hub)
    thread = run.go_in_thread()
    try:
        _wait_for_partial()
        store.request_cancel(run.task_id)
        thread.join(timeout=30)
    finally:
        hub.release.set()
        thread.join(timeout=30)

    assert run.error is None
    assert run.status == "cancelled"
    assert children[0]["proc"].poll() is not None
    assert cache.has_partial(TINY.repo_id) is False
    assert cache.is_complete(TINY.repo_id) is False
    assert store.current_lock() is None
    assert run.events[-1]["status"] == "cancelled"
    assert cache.is_complete(BASE.repo_id) is True
    assert sorted(p.name for p in (cache.repo_dir(BASE.repo_id) / "blobs").iterdir()) == (
        neighbour_blobs
    )


def test_a_cancel_requested_before_the_task_starts_spawns_nothing(hub, store, children):
    store.request_cancel("task-1")
    run = Run(store, hub)

    assert run.go() == "cancelled"

    assert children == []
    assert hub.requests == []
    assert store.current_lock() is None


@pytest.mark.parametrize(
    ("mode", "code"),
    [("401", "hf_auth_failed"), ("404", "hf_repo_not_found"), ("500", "hf_unreachable")],
)
def test_a_hub_refusal_fails_with_its_code_and_releases_the_lock(hub, store, children, mode, code):
    hub.mode = mode
    run = Run(store, hub, token=TOKEN)

    assert run.go() == "failed"

    assert run.events[-1]["error_code"] == code
    assert store.current_lock() is None
    state = store.read_state()
    assert state is not None
    assert (state["status"], state["error_code"]) == ("failed", code)
    assert TOKEN not in repr(run.events) + repr(state)


def test_offline_mode_fails_fast_without_a_child_or_a_request(hub, store, children, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    run = Run(store, hub)

    assert run.go() == "failed"

    assert run.events[-1]["error_code"] == "downloads_disabled_offline"
    assert children == []
    assert hub.requests == []
    assert store.current_lock() is None


def test_too_little_disk_fails_before_any_child_starts(hub, store, children, monkeypatch):
    monkeypatch.setattr(cache, "free_bytes", lambda: 1024)
    run = Run(store, hub)

    assert run.go() == "failed"

    assert run.events[-1]["error_code"] == "insufficient_disk_space"
    assert children == []
    assert store.current_lock() is None


def test_an_unwritable_cache_fails_before_any_request(hub, store, children, monkeypatch):
    monkeypatch.setattr(cache, "cache_writable", lambda: False)
    run = Run(store, hub)

    assert run.go() == "failed"

    assert run.events[-1]["error_code"] == "cache_not_writable"
    assert hub.requests == []


def test_a_download_that_outlives_its_deadline_is_stopped(hub, store, children):
    hub.mode = "slow"
    run = Run(store, hub, deadline_seconds=0.5)
    try:
        status = run.go()
    finally:
        hub.release.set()

    assert status == "failed"
    assert run.events[-1]["error_code"] == "download_timed_out"
    assert children[0]["proc"].poll() is not None
    assert store.current_lock() is None


def test_a_task_that_lost_its_lock_to_another_download_stands_down(hub, store, children):
    store.try_acquire(BASE.id, "someone-else", 9)
    run = Run(store, hub)

    status = supervisor.run_download(
        TINY,
        task_id=run.task_id,
        user_id=7,
        store=store,
        notify=run.events.append,
        endpoint=hub.url,
        token=None,
    )

    assert status == "failed"
    assert run.events[-1]["error_code"] == "model_download_in_progress"
    assert children == []
    held = store.current_lock()
    assert held is not None
    assert held["task_id"] == "someone-else"


def test_success_is_not_reported_while_expected_blobs_are_missing(
    hub, store, hf_cache, monkeypatch
):
    """``refs/main`` is written before the files on a branch download, so it proves nothing."""
    make_complete_repo(hf_cache, TINY.repo_id)  # complete by the cache's rule, wrong blobs
    assert cache.is_complete(TINY.repo_id) is True
    real_popen = subprocess.Popen

    def lying_child(cmd, *args, **kwargs):
        script = "import json; print(json.dumps({'ok': True}))"
        return real_popen([sys.executable, "-c", script], *args, **kwargs)

    monkeypatch.setattr(supervisor.subprocess, "Popen", lying_child)
    run = Run(store, hub)

    assert run.go() == "failed"

    assert run.events[-1]["error_code"] == "download_failed"
    assert not any(e["status"] == "completed" for e in run.events)


def test_a_child_that_reports_a_failure_code_has_it_passed_on(hub, store, hf_cache, monkeypatch):
    real_popen = subprocess.Popen

    def failing_child(cmd, *args, **kwargs):
        script = "import json; print(json.dumps({'ok': False, 'code': 'disk_full'}))"
        return real_popen([sys.executable, "-c", script], *args, **kwargs)

    monkeypatch.setattr(supervisor.subprocess, "Popen", failing_child)
    blobs = cache.repo_dir(TINY.repo_id) / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "stale.incomplete").write_bytes(b"x")
    run = Run(store, hub)

    assert run.go() == "failed"

    assert run.events[-1]["error_code"] == "disk_full"
    assert cache.has_partial(TINY.repo_id) is False  # a disk-full run cannot resume them


def test_every_failure_code_the_supervisor_emits_has_a_fixed_sentence():
    for code in (
        "model_download_in_progress",
        "insufficient_disk_space",
        "download_timed_out",
        "downloads_disabled_offline",
        "cache_not_writable",
    ):
        assert downloader.ERROR_MESSAGES[code].endswith(".")
