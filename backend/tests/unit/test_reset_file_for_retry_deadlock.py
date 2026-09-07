"""`reset_file_for_retry` must survive a transient Postgres deadlock (issue found live).

Found running the full E2E suite against a genuinely fresh stack: three concurrent xdist
workers, each reprocessing a different file, produced a real 3-way `DeadlockDetected` cycle
on `media_file` (SQLSTATE 40P01) — Postgres aborted one of the three transactions to break
the cycle, exactly as it is designed to. Before this fix, `reset_file_for_retry` caught that
abort with the same bare `except Exception` as any other failure, rolled back, and returned
`False` — surfacing a permanent-looking "Failed to reset file for reprocessing" /
`RESET_FAILED` to the caller for what is actually a one-shot, retry-safe timing artifact. The
test that hit it passed cleanly the moment it was re-run in isolation, which is exactly what
a deadlock looks like: real, but not reproducible by the same input twice.

Any admin bulk-reprocessing many files at once is running the same concurrent-UPDATE shape
against `media_file` production would eventually hit, not just this suite's xdist workers —
so this is a production reliability gap, not merely test flakiness.

Uses a fake ``Session`` stand-in (the ``_FakeDB`` pattern from ``test_account_lifecycle.py``)
rather than the real SAVEPOINT-backed ``db_session`` fixture: the function under test calls
``db.rollback()`` itself, and a real ``Session.rollback()`` inside that fixture's nested
transaction collapses the whole outer test transaction — including rows the fixture already
committed — which has nothing to do with the retry logic this file is verifying.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from typing import cast

from sqlalchemy.exc import OperationalError

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.utils.task_utils import reset_file_for_retry


class _FakeQuery:
    """Returns one canned row (or none) regardless of the filter — the filters are SQL."""

    def __init__(self, result=None):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._result

    def delete(self):
        return 0


class _FakeDB:
    """Minimal ``Session`` stand-in keyed by model class, tracking commit/rollback calls."""

    def __init__(self, media_file: MediaFile | None):
        self.media_file = media_file
        self.commit_calls = 0
        self.rollback_calls = 0
        self.commit_side_effect: Callable[[], None] | None = None

    def query(self, model):
        if model is MediaFile:
            return _FakeQuery(self.media_file)
        return _FakeQuery(None)

    def commit(self):
        self.commit_calls += 1
        if self.commit_side_effect is not None:
            self.commit_side_effect()

    def rollback(self):
        self.rollback_calls += 1

    def refresh(self, _instance):
        """No-op: the fake holds its state on the object already."""


def _deadlock_media_file() -> MediaFile:
    """A MediaFile stand-in shaped like reset_file_for_retry's normal input."""
    media_file = MediaFile()
    media_file.id = 272_759
    media_file.status = FileStatus.COMPLETED
    media_file.retry_count = 0
    media_file.active_task_id = None
    return media_file


def _deadlock_error() -> OperationalError:
    """A real-shaped OperationalError: SQLSTATE 40P01, same attribute path production reads."""
    orig = Exception("deadlock detected")
    orig.pgcode = "40P01"  # type: ignore[attr-defined]
    return OperationalError("UPDATE media_file ...", {}, orig)


def test_a_deadlock_on_the_first_attempt_is_retried_and_succeeds() -> None:
    """The exact shape hit live: attempt 1 deadlocks, attempt 2 (a real commit) succeeds."""
    media_file = _deadlock_media_file()
    db = _FakeDB(media_file)
    calls = {"n": 0}

    def _commit_first_call_deadlocks():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _deadlock_error()

    db.commit_side_effect = _commit_first_call_deadlocks

    result = reset_file_for_retry(cast(Any, db), media_file.id)

    assert result is True, "a retry-safe deadlock must not be reported as a permanent failure"
    assert calls["n"] == 2, "expected exactly one retry after the simulated deadlock"
    assert db.rollback_calls == 1, "the failed attempt must roll back before retrying"
    assert media_file.status == FileStatus.PENDING
    # The fake DB does not undo in-memory attribute mutations on a failed attempt (a real
    # Session.rollback() would expire/revert the ORM object) — attempt 1 increments once
    # before the simulated deadlock, attempt 2 increments again on retry, so the field this
    # test cares about (did the reset ultimately land as PENDING with SOME positive count)
    # ends at 2, not 1.
    assert media_file.retry_count == 2


def test_a_deadlock_on_every_attempt_gives_up_and_returns_false() -> None:
    """Guard on the fix: retrying must be BOUNDED, not an infinite/unbounded loop."""
    media_file = _deadlock_media_file()
    db = _FakeDB(media_file)
    calls = {"n": 0}

    def _always_deadlocks():
        calls["n"] += 1
        raise _deadlock_error()

    db.commit_side_effect = _always_deadlocks

    result = reset_file_for_retry(cast(Any, db), media_file.id)

    assert result is False
    assert calls["n"] == 3, "expected exactly the configured retry ceiling, not fewer or more"
    assert db.rollback_calls == 3


def test_a_non_deadlock_operational_error_is_not_retried() -> None:
    """Guard on the fix: only the deadlock SQLSTATE gets a retry — anything else fails once.

    Retrying an arbitrary OperationalError (a genuinely broken query, a lost connection)
    would mask a real bug behind three quiet attempts instead of surfacing it.
    """
    media_file = _deadlock_media_file()
    db = _FakeDB(media_file)
    calls = {"n": 0}

    def _connection_failure():
        calls["n"] += 1
        orig = Exception("connection lost")
        orig.pgcode = "08006"  # type: ignore[attr-defined]  # connection_failure SQLSTATE
        raise OperationalError("UPDATE media_file ...", {}, orig)

    db.commit_side_effect = _connection_failure

    result = reset_file_for_retry(cast(Any, db), media_file.id)

    assert result is False
    assert calls["n"] == 1, "a non-deadlock OperationalError must fail on the first try"
    assert db.rollback_calls == 1


def test_file_not_found_returns_false_without_retry() -> None:
    """A missing file is not a deadlock — no retry, no wasted attempts."""
    db = _FakeDB(None)

    result = reset_file_for_retry(cast(Any, db), 999_999)

    assert result is False
    assert db.commit_calls == 0
    assert db.rollback_calls == 0
