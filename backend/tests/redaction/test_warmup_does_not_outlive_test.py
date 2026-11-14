"""``join_inflight_pii_warmup`` waits for the warm-up thread the real module records.

Self-contained: each test sets ``warmup._warmup_thread`` the way ``start_pii_warmup`` does
(a plain module-global assignment, restored by hand after the join) rather than through
``monkeypatch``, whose teardown runs before an autouse fixture's and would hide the thread.
"""

from __future__ import annotations

import threading

import pytest

from app.services.redaction import warmup
from tests.warmup_join import join_inflight_pii_warmup


@pytest.fixture
def _restore_thread():
    saved = warmup._warmup_thread
    yield
    warmup._warmup_thread = saved


def test_a_live_slow_warmup_is_joined(_restore_thread):
    done = threading.Event()
    release = threading.Timer(0.2, done.set)
    thread = threading.Thread(target=lambda: done.wait(30), daemon=True)
    warmup._warmup_thread = thread
    thread.start()
    release.start()
    assert join_inflight_pii_warmup(timeout=10) is True
    assert not thread.is_alive()


def test_no_thread_or_a_finished_one_is_a_noop(_restore_thread):
    warmup._warmup_thread = None
    assert join_inflight_pii_warmup(timeout=0.1) is False
    finished = threading.Thread(target=lambda: None)
    finished.start()
    finished.join()
    warmup._warmup_thread = finished
    assert join_inflight_pii_warmup(timeout=0.1) is False
    assert not finished.is_alive()


def test_a_warmup_that_never_finishes_fails(_restore_thread):
    stop = threading.Event()
    thread = threading.Thread(target=lambda: stop.wait(30), daemon=True)
    warmup._warmup_thread = thread
    thread.start()
    try:
        with pytest.raises(AssertionError, match="still building"):
            join_inflight_pii_warmup(timeout=0.1)
    finally:
        stop.set()
        thread.join()
