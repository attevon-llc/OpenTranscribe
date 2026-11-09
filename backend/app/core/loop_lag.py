"""Event-loop lag instrument.

Sleeps ``LOOP_LAG_INTERVAL_SECONDS`` and records how much later than that it woke up in
the ``event_loop_lag_seconds`` histogram. A starved loop (blocking code on the loop) and a
starved thread pool or DB pool look alike from outside; this separates them. No library in
the stack exposes asyncio loop lag, so this is the minimal sampler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from prometheus_client import Histogram

logger = logging.getLogger(__name__)

LOOP_LAG_INTERVAL_SECONDS = 0.5

event_loop_lag_seconds = Histogram(
    "event_loop_lag_seconds",
    "How much later than scheduled the asyncio event loop woke a 0.5 s sleeper.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


async def sample_loop_lag(interval: float = LOOP_LAG_INTERVAL_SECONDS) -> None:
    """Record the loop's wake-up overshoot forever; cancel to stop."""
    while True:
        started = time.monotonic()
        await asyncio.sleep(interval)
        event_loop_lag_seconds.observe(max(0.0, time.monotonic() - started - interval))


@asynccontextmanager
async def loop_lag_monitor(interval: float = LOOP_LAG_INTERVAL_SECONDS) -> AsyncIterator[None]:
    """Run :func:`sample_loop_lag` for the life of the ``async with`` block.

    The task reference is held here: the event loop keeps only a weak one, so an
    unreferenced task can be garbage-collected mid-run (asyncio.create_task docs).
    """
    task = asyncio.create_task(sample_loop_lag(interval), name="event-loop-lag")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
