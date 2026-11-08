"""Chat's blocking steps run on chat's own thread limiter (audit H1).

Retrieval, masking, persistence, the Stop-flag read and Bedrock's boto3 stream are
blocking, so they run in worker threads. They used the default limiter every other
request shares; a burst of chats could hold all of it. They now go through
``run_in_chat_thread`` / ``iterate_in_chat_thread``, capped at ``CHAT_THREADPOOL_SIZE``.
"""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path

import anyio.to_thread
import pytest

from app.core import threadpool
from app.core.config import settings


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(settings, "DB_POOL_SIZE", 20)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 40)
    monkeypatch.setattr(settings, "CHAT_THREADPOOL_SIZE", 0)


def test_default_size_is_a_quarter_of_the_db_pool(pool):
    assert threadpool.chat_threadpool_size() == 15


def test_a_small_pool_still_gets_the_floor(pool, monkeypatch):
    monkeypatch.setattr(settings, "DB_POOL_SIZE", 2)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 0)
    assert threadpool.chat_threadpool_size() == threadpool.CHAT_MIN_THREADS


def test_an_explicit_size_wins(pool, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_THREADPOOL_SIZE", 3)
    assert threadpool.chat_threadpool_size() == 3


@pytest.mark.asyncio
async def test_a_chat_hop_borrows_from_chat_not_from_the_shared_pool(pool):
    seen: dict[str, int] = {}
    inside = threading.Event()
    leave = threading.Event()

    def _work() -> str:
        inside.set()
        leave.wait(5)
        return "done"

    task = asyncio.create_task(threadpool.run_in_chat_thread(_work))
    await asyncio.to_thread(inside.wait, 5)
    seen["default"] = int(anyio.to_thread.current_default_thread_limiter().borrowed_tokens)
    seen["chat"] = int(threadpool.chat_thread_limiter().borrowed_tokens)
    leave.set()

    assert await task == "done"
    # asyncio.to_thread above is the stdlib executor, not anyio's limiter.
    assert seen == {"default": 0, "chat": 1}


@pytest.mark.asyncio
async def test_chat_never_runs_more_threads_than_its_cap(pool, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_THREADPOOL_SIZE", 3)
    lock = threading.Lock()
    running = 0
    peak = 0

    def _work() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        threading.Event().wait(0.05)
        with lock:
            running -= 1

    await asyncio.gather(*(threadpool.run_in_chat_thread(_work) for _ in range(12)))

    assert peak == 3


@pytest.mark.asyncio
async def test_iterate_in_chat_thread_yields_every_item_in_order(pool):
    items = [item async for item in threadpool.iterate_in_chat_thread(iter([1, 2, 3]))]
    assert items == [1, 2, 3]


def test_no_chat_module_uses_the_shared_threadpool_helpers():
    """``run_in_threadpool`` / ``iterate_in_threadpool`` borrow from the shared limiter."""
    chat = Path(threadpool.__file__).resolve().parents[1] / "services" / "chat"
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in sorted(chat.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Name | ast.alias)
        and (node.id if isinstance(node, ast.Name) else node.name)
        in {"run_in_threadpool", "iterate_in_threadpool"}
    ]
    assert offenders == []
