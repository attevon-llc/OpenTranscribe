"""Three integer knobs that always treated a too-small value as the smallest allowed one.

``GPU_POISONED_MAX_REQUEUES=-1`` means no requeues, ``REDACTION_PII_WORKERS=0`` means one
worker, ``TRANSCRIPTION_INFRA_REQUEUE_ALERT_THRESHOLD=0`` means a threshold of one.
Falling back to the default silently turned "none" into "two" and "one" into "auto"/"three".
"""

from __future__ import annotations

import contextlib

import pytest


def test_negative_poisoned_requeue_cap_means_no_requeue(monkeypatch):
    from app.tasks.transcription import context

    class _Redis:
        def incr(self, key: str) -> int:
            return 1

        def expire(self, key: str, ttl: int) -> None:
            return None

    monkeypatch.setattr(context.redis_core, "get_redis", lambda: _Redis())
    monkeypatch.setenv("GPU_POISONED_MAX_REQUEUES", "-1")

    assert context._poisoned_requeue_allowed("task-1") is False


def test_zero_pii_workers_means_one_worker(monkeypatch):
    from app.services.redaction import pii_pool

    monkeypatch.setenv("REDACTION_PII_WORKERS", "0")

    assert pii_pool._worker_count() == 1


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_requeue_alert_threshold_below_one_is_one(monkeypatch, raw):
    from app.core import celery_metrics
    from app.core import task_liveness
    from app.db import session_utils

    class _Query:
        def filter(self, *args, **kwargs):
            return []

    class _Db:
        def query(self, *args):
            return _Query()

    @contextlib.contextmanager
    def _scope():
        yield _Db()

    seen: list[int] = []
    monkeypatch.setenv("TRANSCRIPTION_INFRA_REQUEUE_ALERT_THRESHOLD", raw)
    monkeypatch.setattr(session_utils, "session_scope", _scope)
    monkeypatch.setattr(task_liveness, "probe_runs", lambda ids: {})
    monkeypatch.setattr(
        task_liveness, "files_requeued_at_least", lambda threshold: seen.append(threshold)
    )

    celery_metrics.update_transcription_lease_metrics()

    assert seen == [1]
