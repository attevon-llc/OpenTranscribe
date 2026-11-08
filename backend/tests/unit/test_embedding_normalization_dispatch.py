"""The one-time embedding normalization runs on a worker, not in the API (audit M12).

It used to run ``normalize_speaker_embeddings_task.apply()`` inline in the API lifespan:
a full scroll over the speaker indices on the event loop that serves every request and
socket, on every replica, with no election. The API also set the "done" flag from the
result, reading an ``already_running`` skip and an OpenSearch outage (``normalized: 0``)
as "every vector is already normalized", which marked the migration done without it ever
having run. Now the API only dispatches, and the task sets the flag after a clean run.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any

import pytest

from app.models.system_settings import SystemSettings
from app.startup import maintenance
from app.tasks import speaker_embedding_migration as migration
from tests.helpers import StoreBackedTaskLocks

FLAG = "embedding_normalization_done"


def _flag(db) -> str | None:
    row = db.query(SystemSettings).filter(SystemSettings.key == FLAG).first()
    return None if row is None else row.value


@pytest.fixture
def sessions(db_session, monkeypatch):
    """Every session the startup step and the task open is the test's own."""

    @contextmanager
    def scope():
        yield db_session
        db_session.commit()  # session_scope commits on a clean exit

    monkeypatch.setattr(migration, "session_scope", scope)
    monkeypatch.setattr("app.db.session_utils.session_scope", scope)
    monkeypatch.setattr("app.db.base.SessionLocal", lambda: _Unclosed(db_session))
    db_session.query(SystemSettings).filter(SystemSettings.key == FLAG).delete()
    db_session.commit()
    return db_session


class _Unclosed:
    def __init__(self, session: Any) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def close(self) -> None:
        pass


def test_startup_dispatches_to_a_worker_and_never_runs_it_inline(sessions, monkeypatch):
    calls: list[str] = []

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(maintenance.asyncio, "sleep", _no_wait)
    monkeypatch.setattr("app.utils.boot_once.run_once_per_boot", lambda step, ttl=300: True)
    monkeypatch.setattr(
        migration.normalize_speaker_embeddings_task, "apply", lambda *a, **k: calls.append("apply")
    )
    monkeypatch.setattr(
        migration.normalize_speaker_embeddings_task, "delay", lambda *a, **k: calls.append("delay")
    )

    asyncio.run(maintenance._run_one_time_embedding_normalization())

    assert calls == ["delay"]
    assert _flag(sessions) is None, "only the task, after a clean run, may set the flag"


def test_startup_dispatches_nothing_once_the_flag_is_set(sessions, monkeypatch):
    sessions.add(SystemSettings(key=FLAG, value="true"))
    sessions.commit()
    calls: list[str] = []

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(maintenance.asyncio, "sleep", _no_wait)
    monkeypatch.setattr("app.utils.boot_once.run_once_per_boot", lambda step, ttl=300: True)
    monkeypatch.setattr(
        migration.normalize_speaker_embeddings_task, "delay", lambda *a, **k: calls.append("delay")
    )

    asyncio.run(maintenance._run_one_time_embedding_normalization())

    assert calls == []


@pytest.mark.parametrize(
    ("summary", "flag"),
    [
        ({"total_found": 4, "normalized": 3, "already_normalized": 1, "failed": 0}, "true"),
        ({"total_found": 0, "normalized": 0, "already_normalized": 0, "failed": 0}, "true"),
        ({"normalized": 0, "failed": 0, "error": "OpenSearch client not available"}, None),
        ({"total_found": 4, "normalized": 2, "already_normalized": 0, "failed": 2}, None),
    ],
    ids=["normalized-some", "nothing-to-do", "opensearch-down", "partial-failure"],
)
def test_the_task_sets_the_flag_only_after_a_clean_run(sessions, monkeypatch, summary, flag):
    monkeypatch.setattr("app.utils.task_lock.task_lock_manager", StoreBackedTaskLocks({}))
    monkeypatch.setattr(migration, "_run_normalize_embeddings", lambda batch_size: dict(summary))

    migration.normalize_speaker_embeddings_task.apply(kwargs={}).get()

    assert _flag(sessions) == flag


def test_a_skipped_run_does_not_set_the_flag(sessions, monkeypatch):
    held = StoreBackedTaskLocks({migration.NORMALIZE_EMBEDDINGS_LOCK_KEY: "another run"})
    monkeypatch.setattr("app.utils.task_lock.task_lock_manager", held)

    result = migration.normalize_speaker_embeddings_task.apply(kwargs={}).get()

    assert result == {"status": "already_running"}
    assert _flag(sessions) is None
