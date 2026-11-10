"""Audit and error-cache failures must not break a document export.

Injection per https://docs.pytest.org/en/stable/how-to/monkeypatch.html.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.services.download_events import DOCUMENT_EXPORT_ERRORS
from app.tasks import transcript_document


def test_export_audit_failure_is_logged_and_swallowed(monkeypatch, caplog):
    def _boom():
        raise SQLAlchemyError("db down")

    monkeypatch.setattr(transcript_document, "session_scope", _boom)

    with caplog.at_level(logging.WARNING, logger=transcript_document.logger.name):
        transcript_document._record_export(1, 2, "docx")  # must not raise

    assert "Could not record the document export event: SQLAlchemyError" in caplog.text


def test_error_still_published_when_the_redis_cache_write_fails(monkeypatch, caplog):
    reason = next(iter(DOCUMENT_EXPORT_ERRORS))
    redis = MagicMock()
    redis.setex.side_effect = RedisError("down")
    monkeypatch.setattr("app.core.redis.get_redis", lambda: redis)
    published: list[dict] = []
    monkeypatch.setattr(
        transcript_document, "publish_bulk_event", lambda job_id, **kw: published.append(kw)
    )

    with caplog.at_level(logging.WARNING, logger=transcript_document.logger.name):
        result = transcript_document._fail("job-1", reason)

    assert result == {"status": "error", "reason": reason}
    assert published == [
        {"status": "error", "message": DOCUMENT_EXPORT_ERRORS[reason], "reason": reason}
    ]
    assert "Could not cache a document export error: RedisError" in caplog.text
    assert json.dumps(published)  # payload is serialisable for the live event
