"""``/files/prepare`` must keep the event loop free while a slow upload-limits resolver runs (#1169).

``prepare_upload`` is an ``async def`` handler, so anything it calls inline runs ON the event
loop. It used to call ``validate_file_size_for_tenant`` inline, and that calls whichever
``UploadLimitsResolver`` a deployment registered. A resolver that does blocking I/O (a
database read, an HTTP call) then stalls every other request served by the process —
including health checks — for as long as it blocks, multiplied by the number of concurrent
prepares, because the loop serialises them.

Two tests, because they answer different questions and need different load:

* ``test_slow_resolver_does_not_stall_the_event_loop`` asks whether the loop stays free. The
  resolver must never run on the loop thread (checked by thread identity, which does not depend
  on how fast the machine is) and the resolver calls must overlap in worker threads. A heartbeat
  coroutine then bounds the longest gap between its own wake-ups. Inline, 20 prepares hold the
  loop for 20 x 200 ms in one run (measured 1.8-3.4 s longest gap on 2 CPUs); in the threadpool
  the longest gap measured 0.04-0.14 s, including with coverage tracing and two busy-loop
  processes on the same two CPUs.
* ``test_a_burst_over_the_pool_capacity_completes_without_a_pool_deadlock`` asks whether a
  burst of 100 requests finishes. It carries no timing assertion. It used to share the
  heartbeat's 1 s bound, and 100 simultaneously runnable worker threads on a 2-CPU runner
  starve the loop thread for 1.0-1.7 s with the resolver doing nothing wrong (#1305): that is
  CPU and GIL scheduling, and it scales with the thread count, not with the resolver.

Every request uses a real pooled session, so a connection-pool timeout would surface as a 500
here. That half depends on the threadpool being sized against the DB pool
(``app/core/threadpool.py``): a request holds its connection from authentication on, so with
``C`` connections and ``T`` threads a burst of ``C + T`` or more requests can deadlock until
the pool timeout — ``C`` holding a connection while waiting for a thread, ``T`` holding every
thread while waiting for a connection. (Measured with the default 20 + 40 pool and
Starlette's 40 threads: 100 concurrent prepares hit ``QueuePool limit ... timed out``.)

The tests run on their own engine of ``C = 20`` connections rather than the app's 60, so they
stay well inside the test database's connection limit while the rest of the suite runs in
parallel, and size ``T`` through ``API_THREADPOOL_SIZE``: 100 for the burst (``C + T`` = 120 >
100 requests), 40 for the loop test.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import httpx
import pytest
from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.core import tenant_limits
from app.core.config import settings
from app.core.threadpool import configure_api_threadpool
from app.db.base import get_db
from app.main import app
from app.models.user import User
from tests.conftest import TestingSessionLocal
from tests.user_owned_rows import make_user

BURST_PREPARES = 100
BURST_THREADS = 100  # C + T = 120 > BURST_PREPARES
LOOP_PREPARES = 20
LOOP_THREADS = 40
RESOLVER_BLOCK_S = 0.2
# Between the measured worst case with the resolver in the threadpool (0.14 s) and the
# shortest longest-gap measured with it inline (1.8 s), see the module docstring.
MAX_LOOP_STALL_S = 1.0
HEARTBEAT_INTERVAL_S = 0.01
POOL_SIZE, POOL_OVERFLOW = 5, 15  # C = 20


@pytest.fixture
def prepare_user():
    s = TestingSessionLocal()
    user = make_user(s, "prepare-loop")
    user_id = user.id
    s.close()
    yield user_id
    s = TestingSessionLocal()
    s.execute(text("DELETE FROM media_file WHERE user_id = :u"), {"u": user_id})
    s.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": user_id})
    s.commit()
    s.close()


@dataclass
class ResolverCalls:
    """What the blocking resolver observed: where it ran and how many ran at once."""

    threads: list[int] = field(default_factory=list)
    running: int = 0
    peak_running: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def enter(self) -> None:
        with self._lock:
            self.threads.append(threading.get_ident())
            self.running += 1
            self.peak_running = max(self.peak_running, self.running)

    def leave(self) -> None:
        with self._lock:
            self.running -= 1


@pytest.fixture
def slow_resolver():
    calls = ResolverCalls()

    def _blocking_resolver(_org_id):
        calls.enter()
        try:
            time.sleep(RESOLVER_BLOCK_S)  # stands in for a blocking DB/HTTP read
        finally:
            calls.leave()
        return None

    tenant_limits.set_upload_limits_resolver(_blocking_resolver)
    yield calls
    tenant_limits.reset_resolvers()


@pytest.fixture
def real_pool_app(prepare_user, monkeypatch):
    """The app with real per-request pooled sessions (no shared savepoint session)."""
    engine = create_engine(
        settings.DATABASE_URL,
        pool_size=POOL_SIZE,
        max_overflow=POOL_OVERFLOW,
        pool_timeout=10,
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(settings, "DB_POOL_SIZE", POOL_SIZE)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", POOL_OVERFLOW)
    monkeypatch.setattr(settings, "API_THREADPOOL_SIZE", BURST_THREADS)

    def _db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def _ctx(db: Session = Depends(get_db)) -> RequestContext:
        user = db.get(User, prepare_user)
        assert user is not None
        return RequestContext(user=user, org_id=None)

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_context] = _ctx
    yield app
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)
    engine.dispose()


async def _heartbeat(stop: asyncio.Event, gaps: list[float]) -> None:
    last = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        now = time.monotonic()
        gaps.append(now - last - HEARTBEAT_INTERVAL_S)
        last = now


async def _prepare_concurrently(app, count: int) -> list[httpx.Response]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await asyncio.gather(
            *[
                client.post(
                    "/api/files/prepare",
                    json={
                        "filename": f"loop-{i}.wav",
                        "file_size": 4096,
                        "content_type": "audio/wav",
                    },
                )
                for i in range(count)
            ]
        )


def _assert_all_prepared(responses: list[httpx.Response], count: int) -> None:
    statuses = [r.status_code for r in responses]
    failures = [r.text for r in responses if r.status_code != 200][:3]
    assert statuses == [200] * count, failures


@pytest.mark.asyncio
async def test_slow_resolver_does_not_stall_the_event_loop(
    real_pool_app, slow_resolver, monkeypatch
):
    monkeypatch.setattr(settings, "API_THREADPOOL_SIZE", LOOP_THREADS)
    # What the lifespan does (ASGITransport does not run it).
    assert configure_api_threadpool() == LOOP_THREADS
    loop_thread = threading.get_ident()
    stop = asyncio.Event()
    gaps: list[float] = []

    beat = asyncio.create_task(_heartbeat(stop, gaps))
    responses = await _prepare_concurrently(real_pool_app, LOOP_PREPARES)
    stop.set()
    await beat

    _assert_all_prepared(responses, LOOP_PREPARES)
    assert len(slow_resolver.threads) == LOOP_PREPARES  # the resolver really ran for every request
    on_loop = [t for t in slow_resolver.threads if t == loop_thread]
    assert on_loop == [], f"{len(on_loop)} resolver calls ran on the event loop thread"
    assert slow_resolver.peak_running > 1, "the resolver calls were serialised, not run in threads"
    assert max(gaps) < MAX_LOOP_STALL_S, f"event loop stalled for {max(gaps):.2f}s"


@pytest.mark.asyncio
async def test_a_burst_over_the_pool_capacity_completes_without_a_pool_deadlock(
    real_pool_app, slow_resolver
):
    assert configure_api_threadpool() == BURST_THREADS

    responses = await _prepare_concurrently(real_pool_app, BURST_PREPARES)

    _assert_all_prepared(responses, BURST_PREPARES)
    assert len(slow_resolver.threads) == BURST_PREPARES


def _inline_calls_in_async_defs(module_path: Path, name: str) -> list[str]:
    """``name(...)`` called directly inside an ``async def`` (not handed to the threadpool)."""
    tree = ast.parse(module_path.read_text())
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
            ):
                found.append(f"{module_path.name}:{fn.name}:{node.lineno}")
    return found


@pytest.mark.parametrize("module", ["prepare_upload.py", "upload.py"])
def test_upload_size_validation_is_never_called_on_the_event_loop(module):
    """The legacy ``POST /files`` path had the same inline call as ``/files/prepare``."""
    path = Path(__file__).resolve().parents[2] / "app/api/endpoints/files" / module
    assert _inline_calls_in_async_defs(path, "validate_file_size_for_tenant") == []
