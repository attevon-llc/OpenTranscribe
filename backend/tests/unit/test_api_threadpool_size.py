"""API threadpool sizing against the DB pool (#1169) — see ``app/core/threadpool.py``."""

from __future__ import annotations

import anyio.to_thread
import pytest

from app.core.config import settings
from app.core.threadpool import STARLETTE_DEFAULT_THREADS
from app.core.threadpool import api_threadpool_size
from app.core.threadpool import configure_api_threadpool


@pytest.fixture
def pool(monkeypatch):
    def _set(size: int, overflow: int, threads: int = 0, connection_limit: int = 0) -> None:
        monkeypatch.setattr(settings, "DB_POOL_SIZE", size)
        monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", overflow)
        monkeypatch.setattr(settings, "API_THREADPOOL_SIZE", threads)
        # 0 = no uvicorn connection limit, so these cases isolate the pool rule.
        monkeypatch.setattr(settings, "UVICORN_LIMIT_CONCURRENCY", connection_limit)

    return _set


def test_default_matches_pool_capacity(pool):
    pool(20, 40)
    assert api_threadpool_size() == 60


def test_never_below_starlette_default(pool):
    pool(10, 20)
    assert api_threadpool_size() == STARLETTE_DEFAULT_THREADS == 40


def test_explicit_value_wins(pool):
    pool(20, 40, threads=25)
    assert api_threadpool_size() == 25


@pytest.mark.asyncio
async def test_configure_applies_to_the_running_loop(pool):
    pool(30, 50)
    assert configure_api_threadpool() == 80
    assert anyio.to_thread.current_default_thread_limiter().total_tokens == 80


def test_negative_env_value_is_clamped_to_auto(monkeypatch):
    monkeypatch.setenv("API_THREADPOOL_SIZE", "-5")
    from app.core.config import Settings

    assert Settings().API_THREADPOOL_SIZE == 0


def test_threads_rise_so_the_pool_plus_threads_exceed_the_connection_limit(pool):
    """Audit H2: with L open connections allowed, C + T must exceed L (threadpool.py)."""
    pool(20, 40, connection_limit=512)
    assert api_threadpool_size() == 512 - 60 + 1


def test_a_small_connection_limit_leaves_the_pool_rule_in_charge(pool):
    pool(20, 40, connection_limit=100)
    assert api_threadpool_size() == 60
