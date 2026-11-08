"""A running embedding-consistency repair keeps its lease, and only its own batches end it.

The repair is one orchestrator and many GPU batches; the run flag
``embedding_consistency_running`` is what stops the 10-minute beat from starting a second
run. The #1337 load test saw the repair restart while it was still progressing:

* the orchestrator never renewed the flag, so a check slower than its TTL let the next beat
  start a second run beside it;
* any batch that counted "processed >= its total" deleted the flag, including a batch left
  over from an EARLIER run, which counted into the current run's progress and released it
  early; the next beat then re-detected everything and dispatched it all again.

These tests run the real orchestrator and the real batch task against a real Redis.
"""

from __future__ import annotations

import json
import time

import pytest

from app.tasks import embedding_consistency_repair as repair_mod
from app.tasks import embedding_consistency_run as run_mod
from app.tasks import speaker_embedding_consistency as task_mod
from tests.unit._consistency_harness import RUN_KEYS
from tests.unit._consistency_harness import library
from tests.unit._consistency_harness import run_batch
from tests.unit._consistency_harness import run_check

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]

LOCK = "embedding_consistency_running"


@pytest.fixture
def redis_client(local_model_redis):
    local_model_redis.delete(*RUN_KEYS)
    yield local_model_redis
    local_model_redis.delete(*RUN_KEYS)


@pytest.fixture
def short_lease(monkeypatch):
    """A one-second lease, so 'longer than the lease' takes seconds, not hours."""
    for module, name in (
        (run_mod, "LOCK_TTL"),
        (task_mod, "_LOCK_TTL"),
        (repair_mod, "_LOCK_TTL"),
    ):
        monkeypatch.setattr(module, name, 1, raising=False)
    return 1


def test_the_lease_outlives_a_check_slower_than_its_ttl(redis_client, short_lease):
    """A beat that fires while a slow check is still detecting must see it running."""
    pg, fuuids = library(files=3, speakers_per_file=2)
    seen: dict[str, object] = {}

    def _slow_detection() -> None:
        if "nested" in seen:
            return
        seen["nested"] = None
        # Stay "busy detecting" for 2.5 lease lengths, watching the flag the whole time:
        # the first moment it is gone is the failure.
        busy_until = time.monotonic() + short_lease * 2.5
        held = True
        while time.monotonic() < busy_until:
            if not redis_client.exists(LOCK):
                held = False
                break
            time.sleep(0.05)
        seen["held"] = held
        seen["nested"] = run_check(redis_client, pg, fuuids)[0]["status"]

    result, dispatched = run_check(redis_client, pg, fuuids, on_detect=_slow_detection)

    assert seen["held"] is True, "the run flag lapsed while the check was still running"
    assert seen["nested"] == "already_running", (
        f"a second check started beside the first one ({seen['nested']!r})"
    )
    assert result["status"] == "repairing"
    assert len(dispatched) == 1


def test_a_superseded_runs_batches_cannot_end_the_current_run(redis_client):
    """Batches of a run whose lease lapsed stand down: no progress, no release."""
    pg, fuuids = library(files=30, speakers_per_file=2)
    _, old_batches = run_check(redis_client, pg, fuuids)
    redis_client.delete(LOCK)  # the old run's lease lapsed while its batches waited
    current, new_batches = run_check(redis_client, pg, fuuids)
    assert current["status"] == "repairing"
    holder = redis_client.get(LOCK)

    for kwargs in old_batches:
        run_batch(redis_client, kwargs)

    assert redis_client.get(LOCK) == holder, (
        "a batch of the superseded run released the current run's lease"
    )
    assert run_check(redis_client, pg, fuuids)[0]["status"] == "already_running"
    progress = json.loads(redis_client.get("embedding_consistency_progress"))
    assert progress["processed_files"] == 0, "the old run's batches counted into this run"

    # The control: the current run's own batches do finish it, and release the lease.
    for kwargs in new_batches:
        run_batch(redis_client, kwargs)
    assert not redis_client.exists(LOCK)
    last = json.loads(redis_client.get("embedding_consistency_last_run"))
    assert last["status"] == "repaired"
    assert last["total_files"] == 30


def test_a_long_repair_keeps_its_lease_across_batches(redis_client, short_lease):
    """Batches that together take several lease lengths never let a new run in."""
    pg, fuuids = library(files=60, speakers_per_file=1)
    _, batches = run_check(redis_client, pg, fuuids)
    assert len(batches) == 3

    for i, kwargs in enumerate(batches):
        run_batch(redis_client, kwargs, per_file_seconds=0.06)  # ~1.5 lease lengths each
        if i < len(batches) - 1:
            assert run_check(redis_client, pg, fuuids)[0]["status"] == "already_running"

    assert not redis_client.exists(LOCK), "the last batch did not release the lease"
