"""A file whose repair failed is not re-queued by every scheduled check.

A file the repair cannot fix (unreadable audio, or no speaker yields a usable embedding)
stays "missing" after its run. Nothing remembered it, so every 10-minute beat queued it on
the GPU again, forever, and the progress record carried every failed uuid, rewritten and
sent to the browser on every file. These tests run the real orchestrator and batches
against a real Redis.
"""

from __future__ import annotations

import json

import pytest

from tests.unit._consistency_harness import RUN_KEYS
from tests.unit._consistency_harness import library
from tests.unit._consistency_harness import run_batch
from tests.unit._consistency_harness import run_check

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]


@pytest.fixture
def redis_client(local_model_redis):
    local_model_redis.delete(*RUN_KEYS)
    yield local_model_redis
    local_model_redis.delete(*RUN_KEYS)


def _files(batches: list[dict]) -> set[str]:
    return {f for kw in batches for f in kw["file_uuids"]}


def _repair(redis_client, batches: list[dict], failing: frozenset[str]) -> None:
    for kwargs in batches:
        run_batch(redis_client, kwargs, failing=failing)


def test_a_scheduled_check_defers_a_file_that_just_failed(redis_client):
    pg, fuuids = library(files=10, speakers_per_file=1)
    broken = fuuids[3]
    _, batches = run_check(redis_client, pg, fuuids)
    _repair(redis_client, batches, failing=frozenset({broken}))
    assert not redis_client.exists("embedding_consistency_running")

    result, again = run_check(redis_client, pg, fuuids)

    assert broken not in _files(again), "the failed file was queued on the GPU again"
    assert _files(again) == set(fuuids.values()) - {broken}
    assert result["deferred_files"] == 1


def test_a_manual_repair_retries_the_failed_file(redis_client):
    """The control: an operator pressing Repair gets every file, including the failed one."""
    pg, fuuids = library(files=10, speakers_per_file=1)
    broken = fuuids[3]
    _, batches = run_check(redis_client, pg, fuuids)
    _repair(redis_client, batches, failing=frozenset({broken}))

    result, again = run_check(redis_client, pg, fuuids, manual=True)

    assert broken in _files(again)
    assert result["deferred_files"] == 0


def test_the_run_record_counts_every_failure_but_lists_a_bounded_sample(redis_client):
    pg, fuuids = library(files=120, speakers_per_file=1)
    _, batches = run_check(redis_client, pg, fuuids)

    _repair(redis_client, batches, failing=frozenset(fuuids.values()))

    last = json.loads(redis_client.get("embedding_consistency_last_run"))
    assert len(last["failed_files"]) == 50, "the run record lists every failed file"
    assert last["failed_count"] == 120
    assert set(last["failed_files"]) <= set(fuuids.values())
