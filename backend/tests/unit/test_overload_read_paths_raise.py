"""Read paths that degrade one part of a response re-raise a database overload.

Each of these caught every exception and answered a partial result: a migration status that
said "not stalled", a file with no collections, a redaction scan reported as not queued. For a
pool timeout, a cancelled statement or the request deadline that partial answer is a guess
presented as fact; the overload has to reach the API's handler and answer 503 with
``Retry-After`` (``app/core/db_overload.py``) so the client retries. Any other failure keeps
its degraded answer, which is each function's documented contract.

The speaker suggestion-load case is in ``tests/api/test_speaker_update.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from psycopg2 import errors as pg_errors
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.api.endpoints import embedding_migration
from app.api.endpoints.files import detail_response
from app.api.endpoints.files import redaction_gate
from app.db.request_deadline import RequestDeadlineExceededError

pytestmark = pytest.mark.unit

OVERLOADS = [
    PoolTimeoutError("QueuePool limit reached"),
    OperationalError("SELECT 1", {}, pg_errors.QueryCanceled("canceling statement")),
    RequestDeadlineExceededError("request deadline passed"),
]
OVERLOAD_IDS = ["pool_timeout", "statement_timeout", "deadline"]
ORDINARY = SQLAlchemyError("an ordinary failure")


class _Session:
    """Raises ``error`` from ``query``/``commit``; counts rollbacks."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.rollbacks = 0

    def query(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self.error

    def commit(self) -> None:
        raise self.error

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.parametrize("error", OVERLOADS, ids=OVERLOAD_IDS)
def test_the_migration_status_raises_an_overload(error):
    with pytest.raises(type(error)):
        embedding_migration._compute_ground_truth_status(cast(Any, _Session(error)))


def test_the_migration_status_ground_truth_raises_on_an_ordinary_failure():
    with pytest.raises(SQLAlchemyError):
        embedding_migration._compute_ground_truth_status(cast(Any, _Session(ORDINARY)))


def _collections_raise(monkeypatch, error: BaseException) -> None:
    import app.services.collection_visibility as visibility

    def _raises(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(visibility, "collections_on_files", _raises)


@pytest.mark.parametrize("error", OVERLOADS, ids=OVERLOAD_IDS)
def test_the_file_collections_raise_an_overload(monkeypatch, error):
    _collections_raise(monkeypatch, error)

    with pytest.raises(type(error)):
        detail_response.get_file_collections(cast(Any, _Session(error)), 1, 1)


def test_the_file_collections_still_degrade_on_an_ordinary_failure(monkeypatch):
    _collections_raise(monkeypatch, ORDINARY)
    db = _Session(ORDINARY)

    assert detail_response.get_file_collections(cast(Any, db), 1, 1) == []
    assert db.rollbacks == 1


def _file() -> Any:
    return SimpleNamespace(id=7, user_id=3, redaction_status=None)


@pytest.mark.parametrize("error", OVERLOADS, ids=OVERLOAD_IDS)
def test_the_redaction_claim_raises_an_overload(error):
    db = _Session(error)

    with pytest.raises(type(error)):
        redaction_gate.claim_and_dispatch_redaction_scan(cast(Any, db), _file(), surface="test")
    assert db.rollbacks == 1, "the failed claim is rolled back before the overload propagates"


def test_the_redaction_claim_still_reports_not_queued_on_an_ordinary_failure():
    db = _Session(ORDINARY)

    outcome = redaction_gate.claim_and_dispatch_redaction_scan(
        cast(Any, db), _file(), surface="test"
    )

    assert outcome.queued is False
    assert outcome.task_id is None
