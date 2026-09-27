"""Audit indexing must stay cheap on the request path (issue #997).

``AuditLogger.log`` indexes synchronously, inside the request that produced the event.
It used to precede EVERY document with ``indices.exists`` (a HEAD round trip) on a
client with the library's default 10 s timeout and retries, so a slow OpenSearch made
every audited anonymous request — a password-reset probe, a failed login — hold a
worker and a DB session for as long as OpenSearch took to answer. One observed
request spent 9.6 s of 17.4 s on that HEAD alone.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.auth import audit as audit_module
from app.auth.audit import AuditEventType
from app.auth.audit import AuditLogger
from app.auth.audit import AuditOutcome
from app.core.config import settings


class _RecordingOpenSearch:
    """Just enough of the opensearch-py client surface, recording every request."""

    def __init__(self, *, index_errors: list[Exception | None] | None = None, exists_error=None):
        self.requests: list[tuple[str, str]] = []
        self._index_errors = list(index_errors or [])
        self._exists_error = exists_error
        self.indices = self

    def exists(self, index: str) -> bool:
        self.requests.append(("HEAD", index))
        if self._exists_error is not None:
            raise self._exists_error
        return True

    def create(self, index: str, body: dict) -> None:
        self.requests.append(("PUT", index))

    def index(self, index: str, body: dict) -> None:
        self.requests.append(("POST", index))
        if self._index_errors:
            error = self._index_errors.pop(0)
            if error is not None:
                raise error


@pytest.fixture
def indexing_on(monkeypatch):
    monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "AUDIT_LOG_TO_OPENSEARCH", True)
    monkeypatch.setattr(settings, "AUDIT_LOG_FALLBACK_ENABLED", False)


def _log(logger: AuditLogger) -> None:
    logger.log(
        event_type=AuditEventType.AUTH_PASSWORD_RESET_REQUEST,
        outcome=AuditOutcome.FAILURE,
        source_ip="10.0.0.1",
        error_code="NO_LOCAL_ACCOUNT",
    )


def test_index_existence_is_checked_once_per_index_not_per_document(indexing_on, monkeypatch):
    monkeypatch.setattr(audit_module, "_audit_index_name", lambda: "audit-logs-2026.09")
    logger = AuditLogger()
    client = _RecordingOpenSearch()

    with patch.object(logger, "_get_opensearch_client", return_value=client):
        for _ in range(5):
            _log(logger)

    assert (
        client.requests == [("HEAD", "audit-logs-2026.09")] + [("POST", "audit-logs-2026.09")] * 5
    )


def test_a_new_month_checks_its_new_index(indexing_on, monkeypatch):
    logger = AuditLogger()
    client = _RecordingOpenSearch()

    with patch.object(logger, "_get_opensearch_client", return_value=client):
        monkeypatch.setattr(audit_module, "_audit_index_name", lambda: "audit-logs-2026.09")
        _log(logger)
        _log(logger)
        monkeypatch.setattr(audit_module, "_audit_index_name", lambda: "audit-logs-2026.10")
        _log(logger)

    heads = [index for verb, index in client.requests if verb == "HEAD"]
    assert heads == ["audit-logs-2026.09", "audit-logs-2026.10"]


def test_a_failed_write_forgets_the_index_so_it_is_rechecked(indexing_on, monkeypatch):
    """If the index was deleted out from under us the next write must re-create it
    with the proper mapping, not keep trusting a stale cache entry."""
    monkeypatch.setattr(audit_module, "_audit_index_name", lambda: "audit-logs-2026.09")
    logger = AuditLogger()
    client = _RecordingOpenSearch(index_errors=[RuntimeError("index_not_found"), None])

    with patch.object(logger, "_get_opensearch_client", return_value=client):
        _log(logger)
        _log(logger)

    verbs = [verb for verb, _ in client.requests]
    assert verbs == ["HEAD", "POST", "HEAD", "POST"]


def test_an_indexing_timeout_falls_back_instead_of_failing_the_request(
    indexing_on, monkeypatch, tmp_path
):
    fallback = tmp_path / "audit-fallback.jsonl"
    monkeypatch.setattr(settings, "AUDIT_LOG_FALLBACK_ENABLED", True)
    monkeypatch.setattr(settings, "AUDIT_LOG_FALLBACK_PATH", str(fallback))
    logger = AuditLogger()
    client = _RecordingOpenSearch(exists_error=TimeoutError("opensearch timed out"))

    with patch.object(logger, "_get_opensearch_client", return_value=client):
        _log(logger)  # must not raise

    written = [json.loads(line) for line in fallback.read_text().splitlines()]
    assert [event["error_code"] for event in written] == ["NO_LOCAL_ACCOUNT"]


def test_the_audit_writer_client_uses_a_short_timeout_and_no_retries(indexing_on, monkeypatch):
    captured: dict = {}

    class _FakeOpenSearch:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("opensearchpy.OpenSearch", _FakeOpenSearch)
    AuditLogger()._get_opensearch_client()

    assert captured["timeout"] <= 3
    assert captured["max_retries"] == 0
    assert captured["retry_on_timeout"] is False
