"""Every embedding-consistency repair message stays small, whatever the library size.

Measured by the #1337 load test: each batch message carried the run's WHOLE missing-speaker
set, so a library with ~100,000 affected files produced ~4,000 messages of ~117 MB each and
Redis peaked at 52 GB with no user doing anything. These tests drive the real orchestrator
over a large synthetic library, serialize each message it publishes with the app's own
Celery message builder, and hold every one to a fixed byte bound. They also pin that the
smaller messages lose nothing: every missing speaker is still named, in the batch that
holds its file.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import pytest

from app.core.celery import celery_app
from app.tasks import embedding_consistency_repair as repair_mod
from tests.unit._consistency_harness import RUN_KEYS
from tests.unit._consistency_harness import library
from tests.unit._consistency_harness import run_check

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]

#: The bound one message body may reach. A batch is at most 25 files and 400 speakers per
#: index list (~40 bytes a UUID): ~33 KB. The old shape grew by ~40 bytes per missing
#: speaker in the WHOLE library, in every message.
MESSAGE_BYTE_BOUND = 64 * 1024


def _message_body_bytes(kwargs: dict) -> int:
    """The serialized body Celery would put on the broker for these kwargs."""
    msg = celery_app.amqp.as_task_v2(
        str(uuid_pkg.uuid4()),
        repair_mod.speaker_embedding_consistency_repair_batch_task.name,
        kwargs=kwargs,
    )
    return len(json.dumps(msg.body).encode()) + len(json.dumps(msg.headers, default=str).encode())


@pytest.fixture
def redis_client(local_model_redis):
    local_model_redis.delete(*RUN_KEYS)
    yield local_model_redis
    local_model_redis.delete(*RUN_KEYS)


@pytest.mark.parametrize("v4", [False, True])
def test_every_repair_message_is_bounded_and_together_they_name_every_speaker(redis_client, v4):
    pg, fuuids = library(files=2_000, speakers_per_file=10, big_file_speakers=1_000)

    result, dispatched = run_check(redis_client, pg, fuuids, v4=v4)

    assert result["status"] == "repairing", result
    assert dispatched, "the check dispatched no repair at all"
    sizes = [_message_body_bytes(kw) for kw in dispatched]
    assert max(sizes) <= MESSAGE_BYTE_BOUND, (
        f"largest repair message is {max(sizes):,} bytes (bound {MESSAGE_BYTE_BOUND:,}); "
        f"{len(dispatched)} messages total {sum(sizes):,} bytes"
    )

    # Nothing was lost: every speaker is named once per index, in a batch holding its file.
    file_of = {s: fuuids[fid] for s, fid in pg.items()}
    named_v3: list[str] = []
    named_v4: list[str] = []
    for kw in dispatched:
        files = set(kw["file_uuids"])
        for s in kw["missing_v3_uuids"] + kw["missing_v4_uuids"]:
            assert file_of[s] in files, "a batch names a speaker whose file it does not hold"
        named_v3.extend(kw["missing_v3_uuids"])
        named_v4.extend(kw["missing_v4_uuids"])
    assert sorted(named_v3) == sorted(pg)
    assert sorted(named_v4) == (sorted(pg) if v4 else [])
    # Every entry is counted toward completion: the oversized file is split, and counted
    # once per batch that prepares it.
    assert result["total_files"] == sum(len(kw["file_uuids"]) for kw in dispatched)
    assert {kw["total_files"] for kw in dispatched} == {result["total_files"]}
