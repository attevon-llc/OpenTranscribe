"""The shipped defaults keep overload a 503, never a pool/thread stall (audit H2).

``app/core/threadpool.py`` derives the rule: a request holds its database connection
while it waits for its next worker thread, so ``C`` connections and ``T`` threads can
stall each other once ``C + T`` requests are in flight, until the pool timeout fails
them. uvicorn's ``--limit-concurrency`` ``L`` caps requests in flight, so ``L < C + T``
makes that state unreachable. This test computes the relation from the code defaults
and checks that every way the API is started carries the same ``L``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.core.config import settings
from app.core.settings_capacity import CapacitySettings
from app.core.threadpool import api_threadpool_size

REPO = Path(__file__).resolve().parents[3]
DEFAULTS = CapacitySettings.model_fields


def _default(name: str) -> object:
    return DEFAULTS[name].default


@pytest.fixture
def shipped_defaults(monkeypatch):
    for name in (
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "API_THREADPOOL_SIZE",
        "UVICORN_LIMIT_CONCURRENCY",
    ):
        monkeypatch.setattr(settings, name, _default(name))


def test_the_connection_limit_is_below_pool_plus_threads(shipped_defaults):
    pool_capacity = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    limit = settings.UVICORN_LIMIT_CONCURRENCY

    assert limit > 0, "the API must ship with a connection limit"
    assert limit < pool_capacity + api_threadpool_size()


def test_the_pool_timeout_is_shorter_than_sqlalchemys_30_seconds():
    assert 0 < float(str(_default("DB_POOL_TIMEOUT_SECONDS"))) < 30


def test_the_engine_uses_the_configured_pool_timeout():
    from sqlalchemy.pool import QueuePool

    from app.db.base import engine

    assert isinstance(engine.pool, QueuePool)
    assert engine.pool.timeout() == settings.DB_POOL_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "dockerfile",
    ["backend/Dockerfile.prod", "backend/Dockerfile.lite", "backend/Dockerfile.blackwell"],
)
def test_every_image_ships_the_same_limit(dockerfile):
    text = (REPO / dockerfile).read_text()
    match = re.search(r"^ENV UVICORN_LIMIT_CONCURRENCY=(\d+)$", text, re.M)
    assert match, f"{dockerfile} starts uvicorn with no connection limit"
    assert int(match.group(1)) == _default("UVICORN_LIMIT_CONCURRENCY")


def test_compose_passes_the_same_limit_to_the_backend_and_the_dev_command():
    default = _default("UVICORN_LIMIT_CONCURRENCY")
    base = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    env = base["services"]["backend"]["environment"]
    assert f"UVICORN_LIMIT_CONCURRENCY=${{UVICORN_LIMIT_CONCURRENCY:-{default}}}" in env

    dev = yaml.safe_load((REPO / "docker-compose.override.yml").read_text())
    command = dev["services"]["backend"]["command"]
    assert f"--limit-concurrency ${{UVICORN_LIMIT_CONCURRENCY:-{default}}}" in command


def test_the_relation_holds_for_the_pool_the_container_actually_gets(monkeypatch):
    """``L < C + T`` is per process, and the container's pool is 15 + 15, not the coded 20 + 40."""
    env = dict(
        item.split("=", 1)
        for item in yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"][
            "backend"
        ]["environment"]
    )

    def resolved(name: str) -> int:
        match = re.fullmatch(r"\$\{\w+:-(\d+)\}", env[name])
        assert match, env[name]
        return int(match.group(1))

    monkeypatch.setattr(settings, "DB_POOL_SIZE", resolved("DB_POOL_SIZE"))
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", resolved("DB_MAX_OVERFLOW"))
    monkeypatch.setattr(settings, "API_THREADPOOL_SIZE", 0)
    limit = resolved("UVICORN_LIMIT_CONCURRENCY")
    monkeypatch.setattr(settings, "UVICORN_LIMIT_CONCURRENCY", limit)

    assert limit < settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW + api_threadpool_size()
