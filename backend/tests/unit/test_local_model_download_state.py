"""The model-download lock, state and cancel flag against a real Redis (#1246).

The lock's whole job is two Redis behaviours a mock cannot prove: ``SET NX`` lets exactly one of
two simultaneous starters in, and a Lua compare-and-delete releases only the holder's own lock.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from app.services.local_models.state import DownloadStore

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]


@pytest.fixture
def store(local_model_redis, lm_namespace):
    return DownloadStore(local_model_redis, lm_namespace)


def test_a_second_acquire_is_refused_and_names_the_first(store):
    assert store.try_acquire("tiny", "task-a", 1) is True

    assert store.try_acquire("base", "task-b", 2) is False

    held = store.current_lock()
    assert held is not None
    assert (held["model_id"], held["task_id"], held["user_id"]) == ("tiny", "task-a", 1)


def test_eight_threads_starting_at_once_let_exactly_one_in(store):
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def start(n: int) -> None:
        barrier.wait()
        results.append(store.try_acquire("tiny", f"task-{n}", n))

    threads = [threading.Thread(target=start, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == [False] * 7 + [True]


def test_releasing_with_a_stale_task_id_is_a_no_op(store):
    store.try_acquire("tiny", "task-new", 1)

    assert store.release("task-old") is False

    assert store.current_lock() is not None
    assert store.release("task-new") is True
    assert store.current_lock() is None


def test_refresh_extends_only_the_holders_lock(store, local_model_redis, lm_namespace):
    store.try_acquire("tiny", "task-a", 1, ttl=5)

    assert store.refresh("task-other", ttl=500) is False
    assert local_model_redis.ttl(f"{lm_namespace}:lock") <= 5
    assert store.refresh("task-a", ttl=500) is True
    assert local_model_redis.ttl(f"{lm_namespace}:lock") > 100


def test_a_lock_expires_by_ttl_so_a_dead_worker_cannot_hold_it_forever(store):
    store.try_acquire("tiny", "task-a", 1, ttl=1)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if store.current_lock() is None:
            break
        time.sleep(0.1)

    assert store.current_lock() is None
    assert store.try_acquire("base", "task-b", 2) is True


def test_adopt_retakes_an_expired_lock_but_never_steals_a_live_one(store):
    assert store.adopt("tiny", "task-a", 1) is True  # absent: taken
    assert store.adopt("tiny", "task-a", 1) is True  # its own: refreshed
    assert store.adopt("base", "task-b", 2) is False  # someone else's
    held = store.current_lock()
    assert held is not None
    assert held["task_id"] == "task-a"


def test_state_merges_and_replaces(store):
    store.write_state(model_id="tiny", status="queued", bytes_done=0)
    store.write_state(status="downloading", bytes_done=40)

    merged = store.read_state()
    assert merged is not None
    assert (merged["model_id"], merged["status"], merged["bytes_done"]) == (
        "tiny",
        "downloading",
        40,
    )

    store.write_state(replace=True, model_id="base", status="queued")
    replaced = store.read_state()
    assert replaced is not None
    assert replaced["model_id"] == "base"
    assert "bytes_done" not in replaced


def test_the_cancel_flag_round_trips_per_task(store):
    assert store.cancel_requested("task-a") is False

    store.request_cancel("task-a")

    assert store.cancel_requested("task-a") is True
    assert store.cancel_requested("task-b") is False


def test_namespaces_never_collide(local_model_redis):
    one = DownloadStore(local_model_redis, "test:lm:ns-one")
    two = DownloadStore(local_model_redis, "test:lm:ns-two")
    one.try_acquire("tiny", "task-a", 1)

    assert two.current_lock() is None
    assert two.try_acquire("base", "task-b", 2) is True
    one.release("task-a")
    two.release("task-b")


def test_the_lock_value_holds_no_credential_field(store, local_model_redis, lm_namespace):
    store.try_acquire("tiny", "task-a", 1)

    stored = json.loads(local_model_redis.get(f"{lm_namespace}:lock"))

    assert set(stored) == {"model_id", "task_id", "user_id", "started_at"}
