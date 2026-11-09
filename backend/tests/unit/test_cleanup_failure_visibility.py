"""``tasks.cleanup`` failure handlers must leave a traceback and a visible count.

Each handler used to log only ``str(exc)`` (or nothing at ERROR level), so an
operator saw "failed" with no stack. The per-item isolation loops still count the
failure and keep going; the tests prove both halves.
"""

import contextlib
import logging
from unittest.mock import MagicMock

import pytest

from app.tasks import cleanup


def _has_traceback(caplog, fragment: str) -> bool:
    return any(fragment in r.getMessage() and r.exc_info for r in caplog.records)


@pytest.fixture
def fake_session(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(cleanup, "session_scope", lambda: contextlib.nullcontext(db), raising=True)
    return db


def test_emergency_recovery_per_file_failure_is_counted_with_traceback(
    fake_session, monkeypatch, caplog
):
    from app.services import uuid_helpers

    def _boom(db, file_uuid):
        raise ValueError("bad uuid row")

    monkeypatch.setattr(uuid_helpers, "get_file_by_uuid", _boom, raising=True)
    with caplog.at_level(logging.ERROR, logger=cleanup.logger.name):
        result = cleanup.emergency_file_recovery.run(["f-1", "f-2"])

    assert result["failed"] == 2
    assert result["recovered"] == 0
    assert len(result["errors"]) == 2
    assert _has_traceback(caplog, "Error recovering file f-1")


def test_emergency_recovery_outer_failure_logs_traceback_and_raises(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(cleanup, "session_scope", _boom, raising=True)
    with caplog.at_level(logging.ERROR, logger=cleanup.logger.name), pytest.raises(RuntimeError):
        cleanup.emergency_file_recovery.run(["f-1"])

    assert _has_traceback(caplog, "Critical error in emergency recovery")


def test_purge_loop_counts_a_failed_file_and_logs_traceback(fake_session, caplog):
    def _boom(db, media_file, defer_citation_scrub):
        raise OSError("storage unreachable")

    with caplog.at_level(logging.ERROR, logger=cleanup.logger.name):
        deleted, failed = cleanup._purge_expired_loop([(1, "u-1"), (2, "u-2")], _boom, [])

    assert (deleted, failed) == (0, 2)
    assert _has_traceback(caplog, "failed to delete file id=1")


def test_sweeper_outer_failure_is_counted_with_traceback(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(cleanup, "session_scope", _boom, raising=True)
    with caplog.at_level(logging.ERROR, logger=cleanup.logger.name):
        result = cleanup.orphan_upload_sweeper.run(max_age_minutes=30)

    assert result["errors"] == 1
    assert _has_traceback(caplog, "orphan_upload_sweeper error")
