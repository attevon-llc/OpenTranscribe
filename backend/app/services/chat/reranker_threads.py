"""Scope a torch thread cap to the chat reranker's scoring call (issue #1270).

``CrossEncoder.predict`` on 50 pairs is dominated by intra-op parallelism. Under torch's
default thread count (the physical-core count, 24 on the 48-thread dev host) a warm
rerank measured 7-16 s on a busy host, against 3.4-5.5 s with 4-8 threads. The API
process also runs the query-embedding model and the output redactor, which must keep
torch's default, so the cap cannot be process-wide.

Why a dedicated pool and not ``set_num_threads`` around ``predict`` on the request thread:
``torch.set_num_threads`` changes a PROCESS default that every thread not yet latched
inherits when it first touches torch (measured by
``app/scripts/probe_torch_thread_scope.py``). A save/restore window therefore leaks the cap
into any Starlette worker thread that first uses torch during it, and two concurrent turns
race each other's restore. A thread that has already latched keeps its count whatever
others do afterwards. So:

1. Start ``CHAT_RERANKER_SCORING_WORKERS`` threads, each calling ``set_num_threads(cap)``
   and then ``get_num_threads()`` (which forces the latch).
2. Restore the process default from a short-lived helper thread, so the embedding model and
   the redactor are unaffected.
3. Run ``model.predict`` on those threads. As a side effect at most that many reranks
   execute at once, each at the cap, instead of N turns each at the default.

The cap is a DEPLOYMENT setting, ``CHAT_RERANKER_TORCH_THREADS`` (``auto`` or a positive
integer), applied once per process. It is not live-tunable: torch latches the count per
thread, so a changed value only takes effect after a restart.

Precedence: an explicit integer always wins. ``auto`` defers to an operator's own
``OMP_NUM_THREADS``/``TORCH_NUM_THREADS`` (the precedence ``core/celery.py`` uses): when
either is set the pool is not used and ``predict`` runs inline under that value. Otherwise
``auto`` is ``min(CHAT_RERANKER_TORCH_THREADS_AUTO_MAX, max(1, effective_cpu_count() // 2))``.
If the pool cannot start (no torch, an initializer error) one warning is logged and
``predict`` runs inline, which is exactly the behaviour before this module existed.

Residual risk, accepted: between step 1's latch and step 2's restore, a thread that first
touches torch inherits the cap. That window is milliseconds, once per process, normally at
startup (``reranker_warmup`` starts the pool); the result is one thread running capped.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from app.core import constants as C  # noqa: N812
from app.core.config import settings
from app.utils.cpu_budget import effective_cpu_count
from app.utils.cpu_budget import parse_thread_count_setting

logger = logging.getLogger(__name__)

THREAD_NAME_PREFIX = "chat-rerank-score"
#: Not prefixed with :data:`THREAD_NAME_PREFIX`, so a test can tell the thread that restored
#: the process default from a scoring thread.
HELPER_THREAD_NAME = "chat-rerank-helper"
_START_TIMEOUT_S = 30.0

#: Why the effective cap is what it is. ``configured``: an explicit integer.
#: ``auto``: derived from the CPU budget. ``torch_env``: ``auto`` deferred to an
#: ``OMP_NUM_THREADS``/``TORCH_NUM_THREADS`` the operator set, so no cap is applied here.
SOURCE_CONFIGURED = "configured"
SOURCE_AUTO = "auto"
SOURCE_TORCH_ENV = "torch_env"


@dataclass(frozen=True)
class ThreadCap:
    """The resolved scoring thread cap.

    Attributes:
        threads: Threads each scoring thread latches, or None when this module applies no
            cap (the operator's torch environment variables govern).
        source: One of ``configured``, ``auto``, ``torch_env``.
    """

    threads: int | None
    source: str


def auto_thread_cap() -> int:
    """Return the ``auto`` cap for this process's CPU budget."""
    return min(C.CHAT_RERANKER_TORCH_THREADS_AUTO_MAX, max(1, effective_cpu_count() // 2))


def resolve_thread_cap(raw: str | None = None) -> ThreadCap:
    """Resolve the cap from the deployment setting.

    Args:
        raw: Override for the setting, for tests and the benchmark. Defaults to
            ``settings.CHAT_RERANKER_TORCH_THREADS``.

    Returns:
        The cap and where it came from.
    """
    configured = parse_thread_count_setting(raw if raw is not None else _setting())
    if configured is not None:
        return ThreadCap(configured, SOURCE_CONFIGURED)
    if os.getenv("OMP_NUM_THREADS") or os.getenv("TORCH_NUM_THREADS"):
        return ThreadCap(None, SOURCE_TORCH_ENV)
    return ThreadCap(auto_thread_cap(), SOURCE_AUTO)


def _setting() -> str:
    return settings.CHAT_RERANKER_TORCH_THREADS


def describe_thread_cap() -> dict[str, Any]:
    """The effective cap, for the read-only admin system status.

    Returns:
        ``{"threads": int | None, "source": str, "setting": str}``. ``threads`` is None when
        the operator's ``OMP_NUM_THREADS``/``TORCH_NUM_THREADS`` governs instead; ``setting``
        is the raw ``CHAT_RERANKER_TORCH_THREADS`` value.
    """
    cap = resolve_thread_cap()
    return {"threads": cap.threads, "source": cap.source, "setting": _setting()}


_pool_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_pool_unavailable = False


def _on_fresh_thread(fn: Any) -> Any:
    """Run ``fn`` on a short-lived thread and return its result.

    A fresh thread is the only way to read or write the process default without touching
    the calling thread's own latched count.
    """
    box: dict[str, Any] = {}

    def body() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller below
            box["error"] = exc

    thread = threading.Thread(target=body, name=HELPER_THREAD_NAME)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _latch(torch: Any, cap: int) -> None:
    torch.set_num_threads(cap)
    torch.get_num_threads()


def _build_pool(cap: int) -> ThreadPoolExecutor:
    """Start every scoring thread at ``cap``, then restore the process default."""
    import torch

    workers = C.CHAT_RERANKER_SCORING_WORKERS
    default = _on_fresh_thread(torch.get_num_threads)
    executor = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=THREAD_NAME_PREFIX,
        initializer=_latch,
        initargs=(torch, cap),
    )
    try:
        # A pool starts a thread only when none is idle, and a thread blocked on the
        # barrier is never idle, so this forces all ``workers`` threads to exist.
        # The caller waits on the futures, not on the barrier, so a failed initializer
        # (which fails every queued future at once) surfaces immediately, not at the timeout.
        barrier = threading.Barrier(workers)
        futures = [executor.submit(barrier.wait, _START_TIMEOUT_S) for _ in range(workers)]
        for future in futures:
            future.result(timeout=_START_TIMEOUT_S)
        _on_fresh_thread(lambda: torch.set_num_threads(default))
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    logger.info(
        "Chat reranker scoring pool started: %d threads at %d torch threads each "
        "(process default %d restored)",
        workers,
        cap,
        default,
    )
    return executor


def _get_pool(cap: int) -> ThreadPoolExecutor | None:
    global _pool, _pool_unavailable
    if _pool is not None:
        return _pool
    if _pool_unavailable:
        return None
    with _pool_lock:
        if _pool is not None:
            return _pool
        if _pool_unavailable:
            return None
        try:
            _pool = _build_pool(cap)
        except Exception as exc:  # noqa: BLE001 - a thread-cap optimisation must not fail a chat
            _pool_unavailable = True
            logger.warning(
                "Chat reranker scoring pool unavailable (%s: %s); scoring runs inline at "
                "torch's own thread count",
                type(exc).__name__,
                exc,
            )
            return None
        return _pool


def score_pairs(model: Any, pairs: list[tuple[str, str]]) -> Any:
    """Score ``pairs`` with ``model.predict`` on the capped scoring pool.

    Args:
        model: A loaded cross-encoder.
        pairs: ``(query, passage)`` strings. Only strings cross to the pool threads.

    Returns:
        Whatever ``model.predict`` returns. Errors from ``predict`` propagate.
    """
    cap = resolve_thread_cap()
    if cap.threads is None:
        return model.predict(pairs)
    pool = _get_pool(cap.threads)
    if pool is None:
        return model.predict(pairs)
    return pool.submit(model.predict, pairs).result()


def shutdown_for_tests() -> None:
    """Stop the scoring pool and forget that it ever failed to start. For tests only."""
    global _pool, _pool_unavailable
    with _pool_lock:
        pool, _pool, _pool_unavailable = _pool, None, False
    if pool is not None:
        pool.shutdown(wait=True)
