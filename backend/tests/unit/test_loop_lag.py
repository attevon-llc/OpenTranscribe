"""The event-loop lag histogram records a lag when something blocks the loop (scale plan 6)."""

from __future__ import annotations

import asyncio
import time

from app.core import loop_lag
from app.core.loop_lag import loop_lag_monitor


def _histogram_sum() -> float:
    samples = {
        s.name: s.value for m in loop_lag.event_loop_lag_seconds.collect() for s in m.samples
    }
    return samples["event_loop_lag_seconds_sum"]


def test_blocking_the_loop_for_300ms_is_recorded(monkeypatch):
    before = _histogram_sum()
    observed = []
    real_observe = loop_lag.event_loop_lag_seconds.observe

    async def _main() -> None:
        sampled = asyncio.Event()

        def _observe(value: float) -> None:
            real_observe(value)
            observed.append(value)
            sampled.set()

        monkeypatch.setattr(loop_lag.event_loop_lag_seconds, "observe", _observe)
        async with loop_lag_monitor(interval=0.05):
            await sampled.wait()  # the sampler is now asleep in its interval
            sampled.clear()
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:  # holds the loop; no await
                pass
            await sampled.wait()

    asyncio.run(_main())
    assert len(observed) >= 2
    assert max(observed) >= 0.2
    assert _histogram_sum() - before >= 0.2
