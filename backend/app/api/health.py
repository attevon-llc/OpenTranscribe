"""Liveness and readiness probes, mounted at the root (no ``API_PREFIX``).

Kubernetes semantics: liveness (``/health``) answers "is this process wedged?" and touches no
dependency; readiness (``/health/ready``) answers "may this replica take traffic?" and checks the
back-end services. Neither may fail merely because the replica is busy, so readiness does its
blocking work on a dedicated one-thread limiter and a dedicated one-connection engine, and serves
a short-lived cached snapshot (``READINESS_CACHE_SECONDS``) so probe storms cost one probe.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Callable
from typing import Any

import anyio
from anyio import to_thread
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.constants import DEPENDENCY_PROBE_TIMEOUT_SECONDS
from app.core.version import APP_VERSION

logger = logging.getLogger(__name__)
router = APIRouter()

_probe_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter] = (
    weakref.WeakKeyDictionary()
)
_refresh_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)
# (checks, ready, taken_at monotonic)
_snapshot: tuple[dict[str, str], bool, float] | None = None


def reset_readiness_state() -> None:
    """Forget the cached snapshot and the schema result (tests, and nothing else)."""
    global _snapshot
    from app.db import probe_engine

    _snapshot = None
    probe_engine.reset_schema_cache()


def _probe_limiter() -> anyio.CapacityLimiter:
    loop = asyncio.get_running_loop()
    limiter = _probe_limiters.get(loop)
    if limiter is None:
        limiter = anyio.CapacityLimiter(1)
        _probe_limiters[loop] = limiter
    return limiter


def _refresh_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _refresh_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[loop] = lock
    return lock


def _probe_postgres_and_schema(checks: dict[str, str]) -> None:
    """``SELECT 1`` and the Alembic revision on the one probe connection."""
    from app.db.probe_engine import check_postgres_and_schema

    _run_probe(checks, "postgres", lambda: check_postgres_and_schema(checks, _run_probe))
    if "schema" not in checks:  # the connection itself failed, so there was no schema to read
        checks["schema"] = checks["postgres"]


def _db_pool_report() -> str:
    from app.db.base import engine

    capacity = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    return f"{engine.pool.checkedout()}/{capacity}"  # type: ignore[attr-defined]


def _probe_redis() -> str:
    from app.core.redis import get_probe_redis

    get_probe_redis().ping()
    return "ok"


def _probe_opensearch() -> str:
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if client is not None and client.ping(request_timeout=DEPENDENCY_PROBE_TIMEOUT_SECONDS):
        return "ok"
    return "unavailable"


def _probe_minio() -> str:
    from app.services.minio_service import get_probe_client

    get_probe_client().bucket_exists(settings.MEDIA_BUCKET_NAME)
    return "ok"


def _run_probe(checks: dict[str, str], name: str, probe: Callable[[], str]) -> None:
    """Record one probe's result; a failure is its exception type, never a raised error."""
    try:
        checks[name] = probe()
    except Exception as exc:  # noqa: BLE001 - a probe reports its failure in the response, never raises
        checks[name] = f"error: {type(exc).__name__}"


def _probe_dependencies() -> tuple[dict[str, str], bool]:
    """Run every probe once (blocking; call on the probe limiter). Returns (checks, ready)."""
    checks: dict[str, str] = {}
    _probe_postgres_and_schema(checks)
    _run_probe(checks, "redis", _probe_redis)
    _run_probe(checks, "opensearch", _probe_opensearch)
    _run_probe(checks, "minio", _probe_minio)
    # Pool saturation is reported, never critical: busy is not broken.
    _run_probe(checks, "db_pool", _db_pool_report)

    # Schema is critical only when a migrate job owns migrations (issue #284 A1.4).
    schema_is_critical = not settings.RUN_MIGRATIONS_ON_STARTUP
    ready = (
        checks.get("postgres") == "ok"
        and checks.get("redis") == "ok"
        and (not schema_is_critical or checks.get("schema", "ok") == "ok")
    )
    return checks, ready


async def _refresh() -> None:
    global _snapshot
    checks, ready = await to_thread.run_sync(_probe_dependencies, limiter=_probe_limiter())
    _snapshot = (checks, ready, time.monotonic())


async def _readiness_snapshot() -> tuple[dict[str, str], bool]:
    ttl = settings.READINESS_CACHE_SECONDS
    snap = _snapshot
    if snap is not None and time.monotonic() - snap[2] < ttl:
        return snap[0], snap[1]
    lock = _refresh_lock()
    if snap is not None and lock.locked():
        # Someone is already refreshing: answer with the previous snapshot, do not queue.
        return snap[0], snap[1]
    async with lock:
        snap = _snapshot
        if snap is None or time.monotonic() - snap[2] >= ttl:
            try:
                async with asyncio.timeout(DEPENDENCY_PROBE_TIMEOUT_SECONDS * 2):
                    await _refresh()
            except TimeoutError:
                if _snapshot is None:
                    return {"postgres": "error: TimeoutError"}, False
        snap = _snapshot
    assert snap is not None
    return snap[0], snap[1]


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Liveness: touches no dependency and no thread, so it answers while every thread is busy."""
    return {"status": "healthy", "version": APP_VERSION}


@router.get("/health/live")
async def live_check() -> dict[str, str]:
    """Kubernetes-named alias of ``/health``."""
    return await health_check()


@router.get("/health/ready", response_model=None)
async def readiness_check() -> JSONResponse | dict[str, Any]:
    """Readiness: 503 only when a critical dependency (PostgreSQL, Redis, schema) is down.

    OpenSearch and MinIO failures are reported, not fatal. Pool saturation (``db_pool``) is
    reported, never critical.
    """
    checks, ready = await _readiness_snapshot()
    if not ready:
        return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}
