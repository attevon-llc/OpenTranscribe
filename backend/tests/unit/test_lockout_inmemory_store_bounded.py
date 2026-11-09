"""The in-memory lockout fallback expires records like Redis and cannot grow without bound.

Redis gets ``set(key, value, ex=ttl)`` and drops the key itself; the fallback used to ignore
``ex`` and keep every record for the life of the process, with its only reaper
(``cleanup_expired_lockouts``) called by nothing.
"""

from __future__ import annotations

import pytest

from app.auth.lockout_store import InMemoryLockoutStore

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_record_is_gone_after_its_ttl():
    clock = _Clock()
    store = InMemoryLockoutStore(clock=clock)
    store.set("lockout:a", "v", ex=60)

    clock.now += 59
    assert store.get("lockout:a") == "v"
    clock.now += 2
    assert store.get("lockout:a") is None
    assert store.delete("lockout:a") == 0
    assert store.keys("lockout:*") == []


def test_a_record_without_ttl_does_not_expire():
    clock = _Clock()
    store = InMemoryLockoutStore(clock=clock)
    store.set("lockout:a", "v")

    clock.now += 10**9

    assert store.get("lockout:a") == "v"


def test_rewriting_a_key_restarts_its_ttl_and_ttl_can_be_removed():
    clock = _Clock()
    store = InMemoryLockoutStore(clock=clock)
    store.set("k", "v1", ex=10)
    clock.now += 9
    store.set("k", "v2", ex=10)
    clock.now += 9
    assert store.get("k") == "v2"
    store.set("k", "v3")
    clock.now += 1000
    assert store.get("k") == "v3"


def test_expired_records_are_swept_by_writes_without_any_read():
    clock = _Clock()
    store = InMemoryLockoutStore(clock=clock)
    for i in range(300):
        store.set(f"lockout:{i}", "v", ex=5)
    clock.now += 10

    for i in range(300, 600):  # enough writes to trigger the amortised sweep
        store.set(f"lockout:{i}", "v", ex=5)

    assert len(store._data) <= 300 + 256
    assert len(store._expires) == len(store._data)


def test_the_store_never_exceeds_its_ceiling_and_evicts_the_oldest():
    store = InMemoryLockoutStore(max_records=100)

    for i in range(1000):
        store.set(f"lockout:{i}", "v", ex=3600)

    assert len(store._data) == 100
    assert len(store._expires) == 100
    assert store.get("lockout:999") == "v"
    assert store.get("lockout:0") is None


def test_a_rewritten_record_counts_as_recent_for_eviction():
    store = InMemoryLockoutStore(max_records=3)
    for key in ("a", "b", "c"):
        store.set(key, "v")
    store.set("a", "v2")  # a is now the newest
    store.set("d", "v")

    assert store.get("a") == "v2"
    assert store.get("b") is None
