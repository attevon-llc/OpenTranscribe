"""Chat stream slots and the hourly counter are atomic, and a live stream keeps its slot.

Against a real Redis (concurrency audit L2): the slot acquire was ZREMRANGEBYSCORE, ZCARD
and ZADD as three commands, so concurrent requests could each count a free slot and each
add one, exceeding the cap; the hourly limit was INCR then a separate EXPIRE. Both are now
one server-side script. A stream re-stamps its slot while it runs, so the leak prune
(``_ACTIVE_TTL_SECONDS``) no longer frees a legitimately long stream.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.core import redis as redis_core
from app.services.chat import limits

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]


@pytest.fixture
def r(local_model_redis, monkeypatch):
    local_model_redis.flushdb()
    monkeypatch.setattr(redis_core, "get_redis", lambda: local_model_redis)
    yield local_model_redis
    local_model_redis.flushdb()


def _race_round(user_id: int, cap: int, racers: int) -> int:
    """Release ``racers`` simultaneous acquires; return how many were granted."""
    barrier = threading.Barrier(racers)
    granted: list[str | None] = []
    guard = threading.Lock()

    def racer() -> None:
        barrier.wait()
        slot = limits.acquire_stream_slot(user_id=user_id, max_concurrent=cap)
        with guard:
            granted.append(slot)

    threads = [threading.Thread(target=racer) for _ in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(granted) == racers
    return len([slot for slot in granted if slot])


def test_racing_acquires_never_exceed_the_cap(r):
    """Many rounds of many simultaneous acquires; the cap must hold in every round."""
    cap, racers, rounds = 2, 32, 25
    overshoots = []
    for round_no in range(rounds):
        user_id = 1000 + round_no
        granted = _race_round(user_id, cap, racers)
        held = r.zcard(f"chat:active:{user_id}")
        if granted != cap or held != cap:
            overshoots.append((granted, held))

    assert overshoots == [], f"rounds that admitted more than {cap}: {overshoots}"


def test_racing_messages_count_every_one_and_the_counter_expires(r):
    racers = 20
    barrier = threading.Barrier(racers)
    verdicts: list[bool] = []
    guard = threading.Lock()

    def racer() -> None:
        barrier.wait()
        allowed, _ = limits.check_hourly_limit(user_id=9, limit=10)
        with guard:
            verdicts.append(allowed)

    threads = [threading.Thread(target=racer) for _ in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert verdicts.count(True) == 10
    (key,) = r.keys("rl:chat:msg:9:*")
    assert 0 < r.ttl(key) <= 3600


def test_a_live_stream_is_not_pruned_after_the_leak_window(r, monkeypatch):
    slot = limits.acquire_stream_slot(user_id=3, max_concurrent=1)
    assert slot
    # The stream has run longer than the leak window, refreshing as it went.
    long_ago = time.time() - limits._ACTIVE_TTL_SECONDS - 60
    r.zadd("chat:active:3", {slot: long_ago})
    limits.refresh_stream_slot(3, slot)

    # Another request now prunes stale slots before counting: the live one survives.
    assert limits.acquire_stream_slot(user_id=3, max_concurrent=1) is None
    assert r.zscore("chat:active:3", slot) > long_ago


def test_refreshing_a_released_slot_does_not_bring_it_back(r):
    slot = limits.acquire_stream_slot(user_id=4, max_concurrent=1)
    limits.release_stream_slot(4, slot)

    limits.refresh_stream_slot(4, slot)

    assert r.zscore("chat:active:4", slot) is None


def test_the_streaming_response_refreshes_its_slot_while_frames_flow(monkeypatch):
    import asyncio

    from app.api.endpoints.chat import messages

    refreshed: list[tuple[int, str]] = []
    monkeypatch.setattr(limits, "SLOT_REFRESH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(limits, "refresh_stream_slot", lambda u, s: refreshed.append((u, s)))
    monkeypatch.setattr(limits, "release_stream_slot", lambda u, s=None: None)

    async def _frames(**_kwargs):
        for n in range(3):
            yield f"data: {n}\n\n"

    monkeypatch.setattr(messages.ChatService, "stream_reply", staticmethod(_frames))
    response = messages._streaming_response({"_slot_id": "slot-1"}, user_id=5)

    async def _drain() -> list[str | bytes | memoryview]:
        return [frame async for frame in response.body_iterator]

    frames = asyncio.run(_drain())

    assert len(frames) == 3
    assert refreshed and set(refreshed) == {(5, "slot-1")}
