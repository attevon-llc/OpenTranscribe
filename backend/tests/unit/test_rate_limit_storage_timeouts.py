"""The limiter's Redis storage carries the request socket timeouts, and falls back to memory."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from limits.errors import ConfigurationError
from redis import ConnectionPool

from app.api import rate_limit
from app.core.config import settings

pytestmark = pytest.mark.unit


def test_timeouts_are_added_to_the_url_query():
    url = rate_limit._with_request_timeouts("redis://:pw@cache:6379/2")
    assert url.startswith("redis://:pw@cache:6379/2?")
    assert f"socket_timeout={settings.REDIS_REQUEST_SOCKET_TIMEOUT_SECONDS}" in url
    assert f"socket_connect_timeout={settings.REDIS_REQUEST_CONNECT_TIMEOUT_SECONDS}" in url


def test_existing_query_options_are_kept():
    url = rate_limit._with_request_timeouts("redis://cache:6379/0?health_check_interval=30")
    assert "health_check_interval=30" in url
    assert "socket_timeout=" in url


def test_redis_py_parses_the_timeouts_as_floats():
    url = rate_limit._with_request_timeouts("redis://cache:6379/0")
    kwargs = ConnectionPool.from_url(url).connection_kwargs
    assert kwargs["socket_timeout"] == float(settings.REDIS_REQUEST_SOCKET_TIMEOUT_SECONDS)
    assert kwargs["socket_connect_timeout"] == float(settings.REDIS_REQUEST_CONNECT_TIMEOUT_SECONDS)


def test_limiter_falls_back_to_memory_when_the_redis_limiter_cannot_be_built():
    real = rate_limit._new_limiter
    calls: list[str | None] = []

    def flaky(storage_uri):
        calls.append(storage_uri)
        if storage_uri is not None:
            raise ConfigurationError("no redis client")
        return real(None)

    with (
        patch.object(rate_limit, "_new_limiter", flaky),
        patch.object(rate_limit, "_redis_reachable"),
    ):
        limiter = rate_limit._create_limiter()
    assert calls[0] is not None and calls[1] is None
    assert limiter is not None
