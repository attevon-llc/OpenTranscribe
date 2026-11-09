"""Outage injection for fail-soft service handlers (broad-except lane B7).

``detect_mode`` pins a real fail-open: an unreachable OpenSearch used to be answered "new
install, v4" AND written to the shared cache, so one failed probe made an existing v3
deployment serve v4 for the whole cache TTL.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
import requests
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services import ai_insights_companion
from app.services import analytics_service
from app.services import embedding_mode_service
from app.services import huggingface_token
from app.services import media_source_resolution
from app.services import migration_lock_service


def _traceback_records(caplog, name):
    return [r for r in caplog.records if r.name == name and r.exc_info]


def test_detect_mode_does_not_cache_a_guess_made_during_an_opensearch_outage(monkeypatch, caplog):
    service = embedding_mode_service.EmbeddingModeService
    client = MagicMock()
    client.indices.exists.side_effect = OpenSearchConnectionError("N/A", "down", Exception("x"))
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: client)
    monkeypatch.setattr(service, "_get_cached_mode", classmethod(lambda cls: None))
    set_cache = MagicMock()
    monkeypatch.setattr(service, "_set_cached_mode", classmethod(lambda cls, mode: set_cache(mode)))

    with caplog.at_level(logging.ERROR, logger=embedding_mode_service.logger.name):
        mode = service.detect_mode(force_refresh=True)

    assert mode == embedding_mode_service.MODE_V4
    set_cache.assert_not_called()
    assert _traceback_records(caplog, embedding_mode_service.logger.name)


def test_document_sampling_failure_propagates_instead_of_reading_as_no_embeddings():
    client = MagicMock()
    client.search.side_effect = OpenSearchConnectionError("N/A", "down", Exception("x"))
    with pytest.raises(OpenSearchConnectionError):
        embedding_mode_service.EmbeddingModeService._detect_dimension_from_documents(client, "idx")


def test_embedding_mode_cache_write_redis_outage_is_logged_with_traceback(monkeypatch, caplog):
    redis_client = MagicMock()
    redis_client.setex.side_effect = RedisConnectionError("down")
    monkeypatch.setattr("app.core.redis.get_request_redis", lambda: redis_client)
    with caplog.at_level(logging.WARNING, logger=embedding_mode_service.logger.name):
        embedding_mode_service.EmbeddingModeService._set_cached_mode(embedding_mode_service.MODE_V4)
    assert _traceback_records(caplog, embedding_mode_service.logger.name)


def test_migration_lock_redis_outage_is_logged_with_traceback(monkeypatch, caplog):
    monkeypatch.setattr(
        migration_lock_service,
        "get_request_redis",
        MagicMock(side_effect=RedisConnectionError("down")),
    )
    lock = migration_lock_service.MigrationLockService()
    with caplog.at_level(logging.ERROR, logger=migration_lock_service.logger.name):
        assert lock.is_active() is False
    assert _traceback_records(caplog, migration_lock_service.logger.name)


def test_companion_guard_fails_closed_with_traceback_on_a_redis_outage(monkeypatch, caplog):
    redis_client = MagicMock()
    redis_client.set.side_effect = RedisConnectionError("down")
    monkeypatch.setattr("app.core.redis.get_request_redis", lambda: redis_client)
    with caplog.at_level(logging.WARNING, logger=ai_insights_companion.logger.name):
        assert ai_insights_companion._claim_guard(1) is None
    assert _traceback_records(caplog, ai_insights_companion.logger.name)


def test_media_source_db_failure_is_logged_not_silently_empty(monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(
        "app.db.base.SessionLocal",
        MagicMock(side_effect=OperationalError("SELECT", {}, Exception("db down"))),
    )
    resolver = media_source_resolution.MediaSourceResolver()
    with caplog.at_level(logging.WARNING, logger=media_source_resolution.logger.name):
        assert resolver._get_db_sources() == []
    assert _traceback_records(caplog, media_source_resolution.logger.name)


def test_hf_token_check_reports_unreachable_without_logging_the_exception_text(monkeypatch, caplog):
    secret_url = "https://hub.example/api/whoami-v2?token=hf_SECRET"
    monkeypatch.setattr(
        requests, "get", MagicMock(side_effect=requests.ConnectionError(secret_url))
    )
    with caplog.at_level(logging.WARNING, logger=huggingface_token.logger.name):
        result = huggingface_token.check_token("hf_x")
    assert result == huggingface_token.TEST_UNREACHABLE
    assert "hf_SECRET" not in caplog.text
    assert "ConnectionError" in caplog.text


def test_hf_token_check_does_not_swallow_a_bug(monkeypatch):
    monkeypatch.setattr(requests, "get", MagicMock(side_effect=AttributeError("bug")))
    with pytest.raises(AttributeError):
        huggingface_token.check_token("hf_x")


def test_analytics_compute_failure_returns_none_with_traceback(caplog):
    db = MagicMock()
    db.query.side_effect = RuntimeError("db exploded")
    with caplog.at_level(logging.ERROR, logger=analytics_service.logger.name):
        assert analytics_service.AnalyticsService.compute_analytics(db, 1) is None
    assert _traceback_records(caplog, analytics_service.logger.name)
