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
from contextlib import ExitStack
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.celery import celery_app
from app.tasks import embedding_consistency_repair as repair_mod
from app.tasks import speaker_embedding_consistency as task_mod

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]

#: The bound one message body may reach. A batch is at most 25 files and 400 speakers per
#: index list (~40 bytes a UUID): ~33 KB. The old shape grew by ~40 bytes per missing
#: speaker in the WHOLE library, in every message.
MESSAGE_BYTE_BOUND = 64 * 1024

_KEYS = (
    "embedding_consistency_running",
    "embedding_consistency_progress",
    "embedding_consistency:batch_task_ids",
)


def _library(files: int, speakers_per_file: int, big_file_speakers: int):
    """``({speaker_uuid: file_id}, {file_id: file_uuid})`` with one oversized file."""
    pg: dict[str, int] = {}
    fuuids: dict[int, str] = {}
    for fid in range(1, files + 1):
        fuuids[fid] = str(uuid_pkg.uuid4())
        count = big_file_speakers if fid == 1 else speakers_per_file
        for _ in range(count):
            pg[str(uuid_pkg.uuid4())] = fid
    return pg, fuuids


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
    local_model_redis.delete(*_KEYS)
    yield local_model_redis
    local_model_redis.delete(*_KEYS)


def _run_check(redis_client, pg: dict[str, int], fuuids: dict[int, str], v4: bool):
    """Run the real orchestrator with the stores replaced; return the published kwargs."""
    dispatched: list[dict] = []

    def _record(kwargs: dict, **_: object) -> MagicMock:
        dispatched.append(kwargs)
        return MagicMock(id=str(uuid_pkg.uuid4()))

    def _group(missing: set[str], uuid_to_file: dict[str, int]) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for s in missing:
            grouped.setdefault(fuuids[uuid_to_file[s]], []).append(s)
        return grouped

    client = MagicMock()
    client.indices.exists.return_value = v4
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(task_mod, "get_redis", return_value=redis_client))
        progress = enter(patch("app.services.migration_progress_service.migration_progress"))
        progress.is_running.return_value = False
        enter(patch.object(task_mod, "_get_pg_speaker_uuids_with_segments", return_value=pg))
        enter(patch.object(task_mod, "_get_all_pg_speaker_uuids", return_value=set(pg)))
        enter(patch.object(task_mod, "_get_opensearch_speaker_uuids", return_value=set()))
        enter(patch.object(task_mod, "_split_by_voiceprint_policy", lambda m, _p: (set(m), set())))
        enter(patch.object(task_mod, "_filter_unrepairable_speakers", return_value=set()))
        enter(patch.object(task_mod, "_group_by_file", _group))
        enter(
            patch(
                "app.services.embedding_mode_service.EmbeddingModeService.get_current_mode",
                return_value="v4" if v4 else "v3",
            )
        )
        enter(patch("app.services.opensearch_service.get_opensearch_client", return_value=client))
        enter(patch("app.services.progress_tracker.ProgressTracker"))
        enter(
            patch.object(
                repair_mod.speaker_embedding_consistency_repair_batch_task,
                "apply_async",
                side_effect=_record,
            )
        )
        enter(patch.object(task_mod, "send_ws_event"))
        result = task_mod.speaker_embedding_consistency_check_task.run()
    return result, dispatched


@pytest.mark.parametrize("v4", [False, True])
def test_every_repair_message_is_bounded_and_together_they_name_every_speaker(redis_client, v4):
    pg, fuuids = _library(files=2_000, speakers_per_file=10, big_file_speakers=1_000)

    result, dispatched = _run_check(redis_client, pg, fuuids, v4)

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
