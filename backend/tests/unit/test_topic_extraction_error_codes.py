"""Topic-extraction results carry a stable error code, never the exception text."""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy.exc import OperationalError

pytestmark = pytest.mark.unit


def test_batch_topic_results_carry_a_code_not_the_exception_text(monkeypatch):
    from app.tasks import topic_extraction as mod

    class _Boom:
        def get(self, timeout):
            raise RuntimeError("password=hunter2 at /srv/internal/path")

    monkeypatch.setattr(mod.extract_topics_task, "delay", lambda *a, **k: _Boom())

    results = mod.batch_extract_topics_task.run(["f1"])

    assert results["failed"] == 1
    assert results["details"][0]["error"] == "topic_extraction_failed"
    assert "hunter2" not in str(results)


def test_a_task_level_topic_failure_returns_a_code(monkeypatch):
    from app.tasks import topic_extraction as mod

    monkeypatch.setattr(mod, "session_scope", _null_scope)

    result = mod._handle_task_error(RuntimeError("password=hunter2"), "f1")

    assert result == {"status": "failed", "error": "topic_extraction_failed"}


@contextlib.contextmanager
def _null_scope():
    raise OperationalError("connect", {}, Exception("no database in this test"))
    yield
