"""Fixtures for the privacy-purge API tests.

The OpenSearch client is a small in-memory evaluator of the exact query shapes the purge sends,
so a scope bug shows up as a surviving or wrongly deleted DOCUMENT, not only as a different body.
The task body runs in process against the savepoint-isolated session (its own sessions could not
see the test's uncommitted rows), with the broker call recorded.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from app.auth.audit import audit_logger
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.services.privacy import purge
from app.services.privacy.policy import clear_write_backstop_cache
from app.tasks import privacy_purge_task as task_module
from app.tasks.privacy_purge_task import privacy_purge_task
from tests.fixtures.fake_speaker_index import FakeOpenSearch
from tests.fixtures.privacy_purge import REVERIFY_LEASE_KEY

# --------------------------------------------------------------------------- #
# Stand-ins
# --------------------------------------------------------------------------- #


class _KeepOpen:
    """A session the purge may 'close': the test's savepoint-isolated one must stay open."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def opensearch(monkeypatch) -> FakeOpenSearch:
    fake = FakeOpenSearch()
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    return fake


@pytest.fixture()
def events(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


@pytest.fixture()
def reindexed(monkeypatch) -> list[tuple[int, str, int]]:
    queued: list[tuple[int, str, int]] = []

    def record(files):
        queued.extend(files)
        return 0

    monkeypatch.setattr(purge, "_queue_reindexes", record)
    return queued


@pytest.fixture()
def broker(monkeypatch) -> list[dict[str, Any]]:
    """Every ``apply_async`` of the purge task, recorded instead of published."""
    sent: list[dict[str, Any]] = []

    def record(args=None, kwargs=None, **options):
        sent.append({"kwargs": dict(kwargs or {}), **options})

    monkeypatch.setattr(privacy_purge_task, "apply_async", record)
    return sent


@pytest.fixture()
def in_process_task(monkeypatch, db_session):
    """Make the task body use the test's session, and return a runner for a recorded dispatch."""

    @contextmanager
    def scope():
        yield db_session

    monkeypatch.setattr(task_module, "session_scope", scope)
    monkeypatch.setattr(task_module, "SessionLocal", lambda: _KeepOpen(db_session))

    def run(sent: dict[str, Any]) -> dict[str, Any]:
        result = privacy_purge_task.apply(kwargs=sent["kwargs"], task_id=sent["task_id"])
        outcome: dict[str, Any] = result.get(propagate=True)
        return outcome

    return run


@pytest.fixture(autouse=True)
def _capabilities():
    set_capability_resolver(
        lambda _r: {**COMMUNITY_CAPABILITIES, "organizations": True, "org.defaults": True}
    )
    clear_write_backstop_cache()
    yield
    reset_capability_resolver()
    clear_write_backstop_cache()


@pytest.fixture()
def reverify_lease(local_model_redis, monkeypatch):
    """The re-verify's lease on a throwaway Redis (``fixtures/throwaway_redis``), never the stack's.

    Returns the client, so a test can hold the lease the way an overlapping run would.
    """
    from app.utils import task_lock

    monkeypatch.setattr(task_lock, "get_request_redis", lambda: local_model_redis)
    local_model_redis.delete(REVERIFY_LEASE_KEY)
    yield local_model_redis
    local_model_redis.delete(REVERIFY_LEASE_KEY)
