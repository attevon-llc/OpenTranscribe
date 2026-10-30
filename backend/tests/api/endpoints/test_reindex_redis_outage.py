"""Redis outage on the reindex routes must read as "unknown", never as "idle".

``_running_reindex_run_id`` used to swallow a Redis error and return ``None``, so
``GET /reindex/status`` answered ``in_progress: false`` and ``POST /reindex/stop``
answered ``not_running`` while a reindex might be running.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import status
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services.search.reindex_lock import ReindexLockUnavailableError

REINDEX_STOP = "/api/search/reindex/stop"
REINDEX_STATUS = "/api/search/reindex/status"


class _EmptyRedis:
    """Healthy Redis holding no keys."""

    def get(self, _key: str) -> None:
        return None


@pytest.fixture
def standin_redis():
    with patch("app.core.redis.get_redis", return_value=_EmptyRedis()):
        yield


_REDIS_DOWN_TEXT = "redis-secret-host.internal:6379 connection refused"


@pytest.fixture
def unreachable_redis():
    """Every ``get_redis()`` call raises, as during a Redis outage."""
    with patch("app.core.redis.get_redis", side_effect=ConnectionError(_REDIS_DOWN_TEXT)):
        yield


def test_reindex_status_says_unknown_when_redis_is_unreachable(
    client, user_token_headers, unreachable_redis
):
    """An outage must not read as "idle" (fail-open): the panel would offer Start
    during a live reindex. Counts it can still compute are still returned."""
    response = client.get(REINDEX_STATUS, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status_unavailable"] is True
    assert body["in_progress"] is False
    assert body["stop_requested"] is False
    assert body["total_files"] == 0
    assert "current_model" in body
    assert _REDIS_DOWN_TEXT not in response.text


def test_reindex_status_is_available_when_redis_answers(client, user_token_headers, standin_redis):
    """The control: a healthy lookup, running or not, is never flagged unavailable."""
    assert (
        client.get(REINDEX_STATUS, headers=user_token_headers).json()["status_unavailable"] is False
    )


def test_stop_is_503_not_not_running_when_redis_is_unreachable(
    client, admin_token_headers, unreachable_redis
):
    """ "not_running" during an outage would tell an admin a live reindex is not running."""
    response = client.post(REINDEX_STOP, headers=admin_token_headers)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "not_running" not in response.text
    assert _REDIS_DOWN_TEXT not in response.text


def test_stop_is_503_when_only_the_lock_lookup_fails(client, admin_token_headers, standin_redis):
    """Fan-out read works but the lock lookup cannot tell: still 503, never "not_running"."""
    with patch(
        "app.api.endpoints.search_reindex._running_reindex_run_id",
        side_effect=ReindexLockUnavailableError("x"),
    ):
        response = client.post(REINDEX_STOP, headers=admin_token_headers)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


class _LockReadFailsRedis:
    """Answers every read except the per-owner reindex lock, whose GET raises."""

    def get(self, key: str) -> None:
        if key.startswith("reindex_lock:"):
            raise RedisConnectionError(_REDIS_DOWN_TEXT)
        return None


def test_stop_is_503_when_the_lock_reader_itself_hits_a_redis_error(client, admin_token_headers):
    """Fan-out read succeeds; ``reindex_lock`` GET raises a real redis error and converts it."""
    with patch("app.core.redis.get_redis", return_value=_LockReadFailsRedis()):
        response = client.post(REINDEX_STOP, headers=admin_token_headers)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == (
        "Reindex state is temporarily unavailable. Please try again shortly."
    )
    assert _REDIS_DOWN_TEXT not in response.text
    assert "not_running" not in response.text
