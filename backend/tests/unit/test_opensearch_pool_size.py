"""The OpenSearch client keeps one pooled connection per API thread (audit L3).

opensearch-py's transport keeps 10 connections per host by default. The API runs up to
``api_threadpool_size()`` threads (60 with the default database pool), so under load most
of them opened, used and discarded a connection on every search instead of reusing one.
"""

from __future__ import annotations

from app.core import opensearch_auth
from app.core.config import settings
from app.core.threadpool import api_threadpool_size


def test_the_pool_matches_the_api_threadpool(monkeypatch):
    monkeypatch.setattr(settings, "API_THREADPOOL_SIZE", 0)
    monkeypatch.setattr(settings, "DB_POOL_SIZE", 20)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 40)
    monkeypatch.setattr(settings, "UVICORN_LIMIT_CONCURRENCY", 0)

    kwargs = opensearch_auth.opensearch_connection_kwargs()

    assert kwargs["pool_maxsize"] == api_threadpool_size() == 60


def test_the_pool_follows_an_explicit_threadpool_size(monkeypatch):
    monkeypatch.setattr(settings, "API_THREADPOOL_SIZE", 25)

    assert opensearch_auth.opensearch_connection_kwargs()["pool_maxsize"] == 25


def test_a_caller_override_still_wins():
    assert opensearch_auth.opensearch_connection_kwargs(pool_maxsize=4)["pool_maxsize"] == 4
