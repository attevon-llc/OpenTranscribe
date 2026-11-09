"""Outage injection for the erasure ledger's best-effort handlers (broad-except lane B7).

Each handler keeps its documented contract (never abort the erasure) but must make the
outage visible: an ERROR record carrying the traceback, not a bare message.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from app.models.erasure import ErasureLedgerEntry
from app.services import erasure_ledger_service as ledger


def _traceback_records(caplog, level=logging.WARNING):
    return [
        r
        for r in caplog.records
        if r.name == ledger.logger.name and r.levelno >= level and r.exc_info
    ]


def _entry() -> ErasureLedgerEntry:
    return ErasureLedgerEntry(
        uuid=uuid_pkg.uuid4(), subject_type="user", status="pending", attempts=0
    )


def _db_down() -> MagicMock:
    db = MagicMock()
    db.commit.side_effect = OperationalError("COMMIT", {}, Exception("connection lost"))
    return db


def test_journal_append_failure_is_logged_with_traceback(tmp_path, monkeypatch, caplog):
    from app.core.config import settings

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "DATA_DIR", blocker)
    monkeypatch.setattr(ledger, "_journal_record", lambda entry: {"uuid": "x"})

    with caplog.at_level(logging.ERROR, logger=ledger.logger.name):
        ledger._append_journal(_entry())

    assert _traceback_records(caplog, logging.ERROR), "journal outage logged without traceback"


def test_unreadable_journal_is_none_and_logged_with_traceback(tmp_path, monkeypatch, caplog):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    ledger.journal_path().mkdir(parents=True)  # a directory where the file should be

    with caplog.at_level(logging.ERROR, logger=ledger.logger.name):
        result = ledger._file_journal_records()

    assert result is None, "unreadable must stay distinguishable from an empty journal"
    assert _traceback_records(caplog, logging.ERROR)


def test_reopen_entry_db_outage_rolls_back_and_logs_traceback(caplog):
    db = _db_down()
    with caplog.at_level(logging.ERROR, logger=ledger.logger.name):
        ledger.reopen_entry(db, _entry())
    db.rollback.assert_called_once()
    assert _traceback_records(caplog, logging.ERROR)


def test_record_request_db_outage_returns_none_and_logs_traceback(caplog):
    db = _db_down()
    with caplog.at_level(logging.ERROR, logger=ledger.logger.name):
        result = ledger.record_request(db, subject_type="user", subject_user_id=1)
    assert result is None
    db.rollback.assert_called_once()
    assert _traceback_records(caplog, logging.ERROR)


def test_record_outcome_db_outage_returns_entry_and_logs_traceback(caplog):
    db = _db_down()
    entry = _entry()
    with caplog.at_level(logging.ERROR, logger=ledger.logger.name):
        assert ledger.record_outcome(db, entry, {}) is entry
    db.rollback.assert_called_once()
    assert _traceback_records(caplog, logging.ERROR)


def test_record_resurrection_db_outage_logs_traceback(caplog):
    db = _db_down()
    with caplog.at_level(logging.ERROR, logger=ledger.logger.name):
        ledger.record_resurrection(db, _entry())
    db.rollback.assert_called_once()
    assert _traceback_records(caplog, logging.ERROR)


def test_audit_sink_failure_never_breaks_the_erasure_but_logs_traceback(monkeypatch, caplog):
    boom = MagicMock(side_effect=RuntimeError("sink down"))
    monkeypatch.setattr(ledger.audit_logger, "log", boom)
    with caplog.at_level(logging.WARNING, logger=ledger.logger.name):
        ledger._audit(_entry(), action="x", outcome=ledger.AuditOutcome.SUCCESS)
    boom.assert_called_once()
    assert _traceback_records(caplog)


def test_a_programming_error_in_a_db_write_is_not_swallowed():
    db = MagicMock()
    db.commit.side_effect = AttributeError("bug, not an outage")
    with pytest.raises(AttributeError):
        ledger.reopen_entry(db, _entry())
