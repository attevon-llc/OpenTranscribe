"""Liveness and readiness must not fail because the replica is busy (plan 4, M2 #9).

Driven through the ASGI app with ``httpx`` so the real routing and threadpool behaviour apply.
Postgres is the suite database; Redis answers a canned ping so only the probe under test can fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import httpx
import pytest
from anyio import to_thread
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

from app.api import health
from app.core.config import settings
from app.db.base import engine
from app.main import app
from tests import state_resets

pytestmark = pytest.mark.asyncio


class _Pong:
    def ping(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import app.core.redis as redis_module

    monkeypatch.setattr(redis_module, "get_probe_redis", lambda: _Pong())
    monkeypatch.setattr(settings, "RUN_MIGRATIONS_ON_STARTUP", True)
    state_resets.reset_readiness_state()
    yield
    state_resets.reset_readiness_state()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_liveness_answers_with_the_database_unreachable(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(engine, "connect", boom)
    async with _client() as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert set(r.json()) == {"status", "version"}


async def test_readiness_answers_while_the_request_pool_is_exhausted(monkeypatch):
    # A small pool exhausts exactly like the production one, but takes 3 server connections, not
    # 30: under xdist the shared suite Postgres runs out of max_connections otherwise ("too many
    # clients"), which made this test depend on what the other workers were doing.
    pool_size, max_overflow = 2, 1
    # A probe that queued for the request pool would sit the full wait; the bound below is a
    # fraction of it, so it separates "waited" from "was slow" however loaded the host is
    # (a fixed 0.5 s failed at 3.9 s under a 48-worker sweep).
    pool_wait = 30
    monkeypatch.setattr(settings, "DB_POOL_SIZE", pool_size)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", max_overflow)
    cargs, cparams = engine.dialect.create_connect_args(engine.url)
    small_pool = QueuePool(
        lambda: engine.dialect.connect(*cargs, **cparams),
        pool_size=pool_size,
        max_overflow=max_overflow,
        timeout=pool_wait,
    )
    monkeypatch.setattr(engine, "pool", small_pool)
    # OpenSearch and MinIO are not what this test measures; their real probes resolve unreachable
    # hostnames in a bare test env and cost 0.4-0.7 s, which swamped the 0.5 s bound below.
    monkeypatch.setattr(health, "_probe_opensearch", lambda: "ok")
    monkeypatch.setattr(health, "_probe_minio", lambda: "ok")
    capacity = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    with contextlib.ExitStack() as stack:
        for _ in range(capacity):
            stack.enter_context(engine.connect())
        async with _client() as c:
            started = time.monotonic()
            r = await asyncio.wait_for(c.get("/health/ready"), timeout=pool_wait)
            elapsed = time.monotonic() - started
    assert r.status_code == 200, r.text
    assert elapsed < pool_wait / 2
    assert r.json()["checks"]["db_pool"] == f"{capacity}/{capacity}"


async def test_readiness_answers_with_every_default_thread_taken():
    limiter = to_thread.current_default_thread_limiter()
    borrowers = [object() for _ in range(int(limiter.total_tokens))]
    for b in borrowers:
        await limiter.acquire_on_behalf_of(b)
    try:
        async with _client() as c:
            r = await asyncio.wait_for(c.get("/health/ready"), timeout=5)
    finally:
        for b in borrowers:
            limiter.release_on_behalf_of(b)
    assert r.status_code == 200, r.text


async def test_readiness_is_503_when_the_probe_engines_postgres_is_down(monkeypatch):
    dead = create_engine(
        "postgresql://x:y@127.0.0.1:1/none", connect_args={"connect_timeout": 1}, pool_size=1
    )
    monkeypatch.setattr("app.db.probe_engine.get_probe_engine", lambda: dead)
    async with _client() as c:
        r = await c.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"].startswith("error:")


async def test_two_calls_within_the_ttl_run_one_probe(monkeypatch):
    monkeypatch.setattr(settings, "READINESS_CACHE_SECONDS", 60.0)
    real = health._probe_dependencies
    calls = []

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(health, "_probe_dependencies", counting)
    async with _client() as c:
        first = await c.get("/health/ready")
        second = await c.get("/health/ready")
    assert first.status_code == second.status_code == 200
    assert len(calls) == 1


async def test_a_stale_snapshot_is_refreshed(monkeypatch):
    monkeypatch.setattr(settings, "READINESS_CACHE_SECONDS", 0.0)
    real = health._probe_dependencies
    calls = []

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(health, "_probe_dependencies", counting)
    async with _client() as c:
        await c.get("/health/ready")
        await c.get("/health/ready")
    assert len(calls) == 2


async def test_response_shapes_are_unchanged():
    async with _client() as c:
        ready = await c.get("/health/ready")
        live = await c.get("/health/live")
    body = ready.json()
    assert body["status"] == "ready"
    assert {"postgres", "redis", "opensearch", "minio", "schema"} <= set(body["checks"])
    assert live.json()["status"] == "healthy"
