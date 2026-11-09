"""Handlers narrowed to what their try body can raise (lane B8b, plan section 1.2).

Each test injects an outage the handler is meant to degrade on (it must still be
visible in the log with a traceback), and an unexpected failure (a programming
error) that must no longer be swallowed into a default.
"""

import logging
from unittest.mock import MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.services import platform_access
from app.services import youtube_rate_limiter
from app.services.llm_types import LLMProvider
from app.services.redis_cache_service import RedisCacheService
from app.services.topic_extraction_service import TopicExtractionService


def _cache_with(client: MagicMock) -> RedisCacheService:
    svc = RedisCacheService()
    svc._redis = client
    return svc


class TestRedisCacheService:
    def test_redis_outage_is_a_miss_with_traceback(self, caplog):
        client = MagicMock()
        client.get.side_effect = RedisConnectionError("down")
        with caplog.at_level(logging.DEBUG, logger="app.services.redis_cache_service"):
            assert _cache_with(client).get("k") is None
        assert any(r.exc_info for r in caplog.records)

    def test_programming_error_is_not_swallowed_into_a_miss(self):
        client = MagicMock()
        client.get.side_effect = RuntimeError("bug")
        with pytest.raises(RuntimeError):
            _cache_with(client).get("k")

    def test_delete_pattern_outage_reports_zero(self, caplog):
        client = MagicMock()
        client.delete.side_effect = RedisConnectionError("down")
        with caplog.at_level(logging.WARNING, logger="app.services.redis_cache_service"):
            assert _cache_with(client).delete_pattern("plain-key") == 0
        assert any(r.exc_info for r in caplog.records)

    def test_delete_pattern_programming_error_propagates(self):
        client = MagicMock()
        client.delete.side_effect = RuntimeError("bug")
        with pytest.raises(RuntimeError):
            _cache_with(client).delete_pattern("plain-key")


class TestTenancyDetection:
    def _db(self, exc: Exception) -> MagicMock:
        db = MagicMock()
        db.scalar.side_effect = exc
        return db

    def test_database_outage_is_multi_and_not_positive(self, monkeypatch, caplog):
        monkeypatch.setattr(platform_access.settings, "DEPLOYMENT_EDITION", "community")
        db = self._db(OperationalError("select", {}, Exception("down")))
        with caplog.at_level(logging.ERROR, logger="app.services.platform_access"):
            assert platform_access._detect_auto(db) == (platform_access.TenancyMode.MULTI, False)
        assert any(r.exc_info for r in caplog.records)

    def test_programming_error_is_not_read_as_multi_tenant(self, monkeypatch):
        monkeypatch.setattr(platform_access.settings, "DEPLOYMENT_EDITION", "community")
        with pytest.raises(RuntimeError):
            platform_access._detect_auto(self._db(RuntimeError("bug")))


@pytest.fixture
def policy_on(monkeypatch):
    monkeypatch.setattr(
        youtube_rate_limiter.youtube_policy,
        "current_youtube_policy",
        lambda: MagicMock(user_rate_limit_enabled=True),
    )


@pytest.mark.usefixtures("policy_on")
class TestYoutubeRateLimiter:
    def _limiter(self, client: MagicMock):
        limiter = youtube_rate_limiter.YouTubeRateLimiter()
        limiter._redis = client
        return limiter

    def test_record_download_programming_error_propagates(self, monkeypatch):
        client = MagicMock()
        client.zadd.side_effect = RuntimeError("bug")
        client.pipeline.side_effect = RuntimeError("bug")
        with pytest.raises(RuntimeError):
            self._limiter(client).record_download(1)

    def test_record_download_redis_outage_is_logged_not_raised(self, caplog):
        client = MagicMock()
        client.zadd.side_effect = RedisConnectionError("down")
        client.pipeline.side_effect = RedisConnectionError("down")
        with caplog.at_level(logging.WARNING, logger="app.services.youtube_rate_limiter"):
            self._limiter(client).record_download(1)
        assert any(r.exc_info for r in caplog.records)


class TestTopicParseLogHygiene:
    SECRET = "SECRET-MODEL-OUTPUT-1234"

    def _parse(self, text: str):
        return TopicExtractionService()._parse_llm_response(text, LLMProvider.CUSTOM)

    def test_validation_error_does_not_log_model_output(self, caplog):
        bad = f'{{"suggested_collections": "{self.SECRET}", "suggested_tags": 5}}'
        with caplog.at_level(logging.DEBUG):
            assert self._parse(bad) is None
        # pydantic's message quotes the offending value as ``input_value=``.
        assert "input_value" not in caplog.text
        assert "ValidationError" in caplog.text

    def test_unexpected_exception_is_not_swallowed(self, monkeypatch):
        import app.services.topic_extraction_service as mod

        def boom(_s):
            raise RuntimeError("bug")

        monkeypatch.setattr(mod.json, "loads", boom)
        with pytest.raises(RuntimeError):
            self._parse('{"suggested_tags": []}')
