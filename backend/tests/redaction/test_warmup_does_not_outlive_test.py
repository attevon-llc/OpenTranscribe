"""A background PII warm-up started inside one test is finished before the next begins.

The two tests are order-dependent on purpose: the first leaves a slow warm-up running, the
second observes that the suite-wide teardown fixture waited for it.
"""

from __future__ import annotations

import threading

from app.services.redaction import warmup

_finished = threading.Event()


_release = threading.Event()


def _slow_warm() -> None:
    _release.wait(timeout=30)
    _finished.set()


def test_a_leaves_a_warmup_running(monkeypatch):
    thread = threading.Thread(target=_slow_warm, daemon=True)
    monkeypatch.setattr(warmup, "_warmup_thread", thread)
    thread.start()
    assert not _finished.is_set()
    # Let it finish only once the test body is over, so the teardown has to wait on it.
    threading.Timer(0.2, _release.set).start()


def test_b_sees_it_already_finished():
    assert _finished.is_set()
