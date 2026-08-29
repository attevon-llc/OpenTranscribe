"""Real crash-simulation proof for ``scripts/testrun-registry.sh`` + the liveness probe
in ``scripts/cleanup-test-data.py`` (issue #629).

The whole safety argument for the flock-liveness marker scheme is "the kernel releases
the lock on SIGKILL, so a crashed run cannot be mistaken for a live one, without a
PID-reuse hazard." That is a claim about kernel behaviour, not application logic — it
is worthless unmocked. This file spawns a REAL subprocess holding the lock, asserts the
Python-side probe (``live_marker_start_times``) reports it live, ``kill -9``s it, and
asserts the probe now reports it stale.

No RUN_* gate (issue #431 precedent for a new safety proof): this needs nothing but
``bash`` and Python's ``fcntl``, both always available.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REGISTRY_SCRIPT = _REPO_ROOT / "scripts" / "testrun-registry.sh"
_CLEANUP_DATA_SCRIPT = _REPO_ROOT / "scripts" / "cleanup-test-data.py"


def _load_cleanup_data_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cleanup_test_data", _CLEANUP_DATA_SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {_CLEANUP_DATA_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanup_data = _load_cleanup_data_module()


def _spawn_holder(testruns_dir: Path) -> subprocess.Popen:
    """A subprocess that sources the registry, calls ``testrun_begin``, then sleeps —
    holding the marker's flock for as long as it lives.
    """
    script = f"""
        source "{_REGISTRY_SCRIPT}"
        testrun_begin
        echo "$TESTRUN_MARKER"
        sleep 300
    """
    # `sleep 300` runs as bash's CHILD (no `exec`), and a child forked after
    # `exec {fd}>marker` in `testrun_begin` inherits that open fd — so it keeps holding
    # the flock even after the top-level bash process is killed. `start_new_session`
    # puts the whole tree in its own process group so the kill below can take out
    # both, which is the only way this test can observe a real "crashed run" rather
    # than a merely-orphaned sleep still holding the lock.
    return subprocess.Popen(  # noqa: S603
        ["bash", "-c", script],
        cwd=str(testruns_dir.parent),
        env={**os.environ, "TESTRUN_REGISTRY_DIR": str(testruns_dir)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _wait_for_marker_file(marker: Path, timeout: float = 10.0) -> None:
    """Poll until *marker* exists, or give up — a bounded poll loop, not a fixed wait
    (kept as its own function so a `break` on success is possible without also exiting
    the caller's outer scope).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            break
        time.sleep(0.1)


@pytest.mark.unit
def test_a_live_holder_is_reported_live_and_a_killed_one_goes_stale(tmp_path: Path) -> None:
    testruns_dir = tmp_path / ".testruns"
    testruns_dir.mkdir()

    proc = _spawn_holder(testruns_dir)
    try:
        marker_path_line = proc.stdout.readline().strip() if proc.stdout else ""
        assert marker_path_line, (
            f"holder produced no marker path; stderr={proc.stderr.read() if proc.stderr else ''}"
        )
        marker = Path(marker_path_line)

        _wait_for_marker_file(marker)
        assert marker.exists(), "holder never created its marker file"

        live_starts = cleanup_data.live_marker_start_times(testruns_dir)
        assert live_starts, "a live holder's marker must be reported live"

        # kill -9 the whole process group — the scenario the whole scheme exists to
        # survive. Killing only proc.pid would leave the `sleep 300` CHILD (which
        # inherited the locked fd across fork) still holding the lock.
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=10)

        # The kernel releases the flock synchronously with process exit; no polling
        # loop is needed, but give the filesystem a moment on a loaded box.
        deadline = time.monotonic() + 5
        stale_now: list[int] = []
        while time.monotonic() < deadline:
            stale_now = cleanup_data.live_marker_start_times(testruns_dir)
            if not stale_now:
                break
            time.sleep(0.1)
        assert stale_now == [], "a SIGKILLed holder's marker must go stale, not stay live"
    finally:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=10)


@pytest.mark.unit
def test_an_empty_testruns_dir_reports_no_live_markers(tmp_path: Path) -> None:
    testruns_dir = tmp_path / ".testruns"
    testruns_dir.mkdir()
    assert cleanup_data.live_marker_start_times(testruns_dir) == []


@pytest.mark.unit
def test_a_missing_testruns_dir_reports_no_live_markers(tmp_path: Path) -> None:
    assert cleanup_data.live_marker_start_times(tmp_path / "does-not-exist") == []


@pytest.mark.unit
def test_a_marker_file_with_no_holder_at_all_is_stale(tmp_path: Path) -> None:
    """A marker written directly (never locked by anyone) must read as stale — this is
    the "died before ever taking the lock" edge, distinct from the SIGKILL scenario.
    """
    testruns_dir = tmp_path / ".testruns"
    testruns_dir.mkdir()
    (testruns_dir / "orphan.lock").write_text("started_at=123\n", encoding="utf-8")
    assert cleanup_data.live_marker_start_times(testruns_dir) == []
