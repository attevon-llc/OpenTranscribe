"""Recovery, redaction and duration-backfill handlers: outages are recorded, bugs propagate."""

import logging
from unittest.mock import MagicMock

import pytest
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from sqlalchemy.exc import OperationalError

from app.tasks import recovery
from app.tasks import recovery_tasks
from app.tasks import redaction_task


def _traceback_logged(caplog, fragment: str) -> bool:
    return any(fragment in r.getMessage() and r.exc_info for r in caplog.records)


def test_opensearch_health_outage_is_recorded_in_the_summary(monkeypatch, caplog):
    from app.services import opensearch_service

    def _down():
        raise OpenSearchConnectionError("N/A", "connection refused", Exception("refused"))

    monkeypatch.setattr(opensearch_service, "check_and_repair_indices", _down)
    summary: dict = {}
    with caplog.at_level(logging.WARNING, logger=recovery.logger.name):
        recovery._check_opensearch_health(summary)

    assert "opensearch_health_error" in summary
    assert _traceback_logged(caplog, "OpenSearch health check failed")


def test_opensearch_health_programming_error_is_not_swallowed(monkeypatch):
    from app.services import opensearch_service

    def _bug():
        raise TypeError("bug")

    monkeypatch.setattr(opensearch_service, "check_and_repair_indices", _bug)
    with pytest.raises(TypeError):
        recovery._check_opensearch_health({})


def test_superseded_sweep_db_failure_is_recorded_rolled_back_and_traced(monkeypatch, caplog):
    def _down(db):
        raise OperationalError("select", {}, Exception("server closed the connection"))

    monkeypatch.setattr(recovery, "resolve_superseded_task_rows", _down)
    db = MagicMock()
    summary: dict = {}
    with caplog.at_level(logging.ERROR, logger=recovery.logger.name):
        recovery._sweep_superseded_rows(db, summary)

    db.rollback.assert_called_once()
    assert "superseded_task_rows_error" in summary
    assert _traceback_logged(caplog, "Superseded task row sweep failed")


def test_superseded_sweep_programming_error_propagates(monkeypatch):
    def _bug(db):
        raise KeyError("bug")

    monkeypatch.setattr(recovery, "resolve_superseded_task_rows", _bug)
    with pytest.raises(KeyError):
        recovery._sweep_superseded_rows(MagicMock(), {})


def test_presign_failure_in_duration_probe_logs_traceback(monkeypatch, caplog):
    from app.services import minio_service

    def _down(path, expires):
        raise OSError("minio unreachable")

    monkeypatch.setattr(minio_service, "get_internal_presigned_url", _down)
    with caplog.at_level(logging.WARNING, logger=recovery_tasks.logger.name):
        assert recovery_tasks._tier_b_probe_duration("a/b.mp4") is None
    assert _traceback_logged(caplog, "could not mint presigned URL")


def test_redaction_task_record_update_failure_is_traced_and_non_fatal(monkeypatch, caplog):
    from app.db import session_utils

    def _down():
        raise OperationalError("update", {}, Exception("db down"))

    monkeypatch.setattr(session_utils, "session_scope", _down)
    with caplog.at_level(logging.WARNING, logger=redaction_task.logger.name):
        redaction_task._mark_redaction_task("t-1", "failed")
    assert _traceback_logged(caplog, "Could not update redaction task record")
