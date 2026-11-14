"""In demo mode no Celery publish leaves the process, by any calling form (#628, E1).

The owner's acceptance test for the public demo is "a demo session cannot enqueue any Celery
task". ``app/core/demo_publish.py`` refuses at ``Celery.send_task``, the one funnel every
calling form reaches. Each form is driven here against the real ``celery_app`` and the real
``before_task_publish`` counter of #898: refused, and the counter does not move. With the
demo off the same call reaches ``Celery.send_task`` (stubbed, so no broker is needed).

``tests/conftest.py::_skip_celery_dispatch`` replaces ``Task.apply_async`` and the app's
``send_task`` for the whole session, which would make every assertion here vacuous. The real
``Task.apply_async`` is captured when this module is imported (collection runs before any
fixture) and put back, with the instance-level ``send_task`` stub removed, for each test.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from celery import Celery
from celery import chain
from celery.app.task import Task

from app.core import inert
from app.core.config import settings
from app.core.demo_publish import DemoAwareCelery
from app.core.demo_publish import DemoPublishRefusedError

_REAL_APPLY_ASYNC = Task.apply_async


@pytest.fixture
def celery_app(monkeypatch):
    app = importlib.import_module("app.core.celery").celery_app
    monkeypatch.setattr(Task, "apply_async", _REAL_APPLY_ASYNC)
    if "send_task" in vars(app):
        monkeypatch.delattr(app, "send_task")
    return app


@pytest.fixture
def a_task(celery_app, monkeypatch):
    from app.tasks.redaction_task import redaction_detect_task

    # An earlier test's ``monkeypatch.setattr(redaction_detect_task, "delay" | "apply_async")``
    # is undone by setting the OLD bound method back on the instance, and that bound method is
    # the session-wide ``_skip_celery_dispatch`` stub. Left in place it shadows the real
    # ``Task.apply_async`` restored above, so nothing would ever reach ``send_task``.
    for name in ("delay", "apply_async"):
        if name in vars(redaction_detect_task):
            monkeypatch.delattr(redaction_detect_task, name)
    return redaction_detect_task


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)


def _publishes() -> float:
    return sum(v for k, v in inert.published_counts().items() if k != inert.PROBE_TASK)


def test_the_real_app_is_demo_aware(celery_app):
    assert isinstance(celery_app, DemoAwareCelery)
    assert inert.probe_publish_hook(), "the #898 publish counter is not connected"


@pytest.mark.parametrize("form", ["delay", "apply_async", "send_task", "chain"])
def test_every_calling_form_is_refused_and_nothing_is_counted(celery_app, a_task, demo_on, form):
    before = _publishes()
    calls = {
        "delay": lambda: a_task.delay(file_id=1, user_id=1),
        "apply_async": lambda: a_task.apply_async(kwargs={"file_id": 1, "user_id": 1}),
        "send_task": lambda: celery_app.send_task("system.update_gpu_stats"),
        "chain": lambda: chain(a_task.si(file_id=1, user_id=1), a_task.si(2, 2)).apply_async(),
    }

    with pytest.raises(DemoPublishRefusedError):
        calls[form]()

    assert _publishes() == before


def test_outside_demo_mode_the_publish_reaches_celery(celery_app, a_task, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    sent: list[str] = []
    monkeypatch.setattr(Celery, "send_task", lambda self, name, *a, **k: sent.append(name))

    a_task.delay(file_id=1, user_id=1)

    assert sent == [a_task.name]


def test_a_get_that_would_scan_redaction_claims_nothing_in_demo(demo_on, monkeypatch):
    from app.api.endpoints.files import redaction_gate

    claims: list[str] = []
    monkeypatch.setattr(
        redaction_gate,
        "claim_and_dispatch_redaction_scan",
        lambda db, f, surface: claims.append(surface),
    )

    redaction_gate._lazy_dispatch_redaction(db=MagicMock(), db_file=MagicMock())

    assert claims == []


def test_refusal_series_exists_at_zero_and_counts_each_refusal(celery_app, demo_on):
    from prometheus_client import REGISTRY

    def value() -> float | None:
        return REGISTRY.get_sample_value("celery_publish_refused_total")

    before = value()
    assert before is not None, "series must be exported before the first refusal"
    with pytest.raises(DemoPublishRefusedError):
        celery_app.send_task("system.update_gpu_stats")
    assert value() == before + 1
