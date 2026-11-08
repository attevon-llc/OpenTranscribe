"""One user's bulk upload must not starve everyone else's transcriptions (concurrency audit F3).

Dispatch runs for real against real rows (``task`` rows are what the backlog is counted
from); only the broker publish is stubbed, and each run's stage priorities are read from
the signatures dispatch built.

The queue order is then derived the way the Redis transport serves it: ``priority_steps``
0-9 with ``queue_order_strategy: priority`` keeps one list per priority and drains the
lowest number first, FIFO within a list (Celery, "Routing Tasks: Redis Message Priorities";
``core/celery.py``). The first stage of every run is published on the CPU queue, so that
stage's priority and publish order decide which run starts first.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest

import app.tasks.transcription.dispatch as dispatch_module
from app.core.constants import DEFAULT_TRANSCRIPTION_FAIR_SHARE_THRESHOLD
from app.core.constants import TRANSCRIPTION_FAIR_SHARE_THRESHOLD_KEY
from app.core.queues import CPUPriority
from app.core.queues import GPUPriority
from app.models.media import MediaFile
from app.models.system_settings import SystemSettings
from tests.fixtures.ws_handshake import make_user


@pytest.fixture
def published(monkeypatch, db_session) -> list[dict]:
    """Each dispatched run's owner file and stage priorities, in publish order."""

    @contextlib.contextmanager
    def _scope():
        yield db_session

    runs: list[dict] = []

    def _chain(preprocess, *rest):
        runs.append(
            {
                "file_uuid": preprocess.kwargs["file_uuid"],
                "priorities": [preprocess.options["priority"]]
                + [stage.options["priority"] for stage in rest],
            }
        )
        return SimpleNamespace(apply_async=lambda **kw: SimpleNamespace(id="stub"))

    monkeypatch.setattr(dispatch_module, "session_scope", _scope)
    monkeypatch.setattr(dispatch_module, "chain", _chain)
    return runs


def _files(db_session, user_id: int, count: int) -> list[str]:
    uuids = []
    for _ in range(count):
        file_uuid = str(uuid.uuid4())
        db_session.add(
            MediaFile(
                uuid=file_uuid,
                filename="fair.wav",
                storage_path=f"media/test/{file_uuid}.wav",
                content_type="audio/wav",
                file_size=1,
                status="pending",
                user_id=user_id,
            )
        )
        uuids.append(file_uuid)
    db_session.commit()
    return uuids


def _service_order(runs: list[dict]) -> list[str]:
    """Run order the priority transport gives: lowest first-stage priority, then FIFO."""
    indexed = sorted(enumerate(runs), key=lambda pair: (pair[1]["priorities"][0], pair[0]))
    return [run["file_uuid"] for _, run in indexed]


def test_a_single_file_overtakes_most_of_another_users_bulk_upload(db_session, published):
    user_a, user_b = make_user(db_session), make_user(db_session)
    bulk = _files(db_session, user_a.id, 50)
    (single,) = _files(db_session, user_b.id, 1)

    for file_uuid in bulk:
        dispatch_module.dispatch_transcription_pipeline(file_uuid)
    dispatch_module.dispatch_transcription_pipeline(single)

    order = _service_order(published)
    position = order.index(single)
    behind_it = len(order) - 1 - position
    threshold = DEFAULT_TRANSCRIPTION_FAIR_SHARE_THRESHOLD
    # B waits only for A's first `threshold` runs, not for all 50.
    assert position == threshold, (position, threshold)
    assert behind_it == 50 - threshold
    assert behind_it > 25, "B's run must go ahead of most of A's"

    by_file = {run["file_uuid"]: run["priorities"] for run in published}
    gpu_stage = 1
    assert by_file[single] == [
        CPUPriority.PIPELINE_CRITICAL,
        GPUPriority.USER_IMPORT,
        CPUPriority.PIPELINE_CRITICAL,
    ]
    # Every stage of a demoted run is demoted, the GPU stage included, so the bulk tail
    # cannot jump back ahead of B's run at the GPU queue either.
    assert by_file[bulk[-1]] == [
        CPUPriority.PIPELINE_BACKLOG,
        GPUPriority.USER_BACKLOG,
        CPUPriority.PIPELINE_BACKLOG,
    ]
    assert by_file[bulk[0]][gpu_stage] == GPUPriority.USER_IMPORT


def test_without_the_threshold_the_queue_is_first_come_first_served(db_session, published):
    """The setting at 0 turns fairness off: B's file goes behind all 50 of A's."""
    db_session.add(SystemSettings(key=TRANSCRIPTION_FAIR_SHARE_THRESHOLD_KEY, value="0"))
    db_session.commit()
    user_a, user_b = make_user(db_session), make_user(db_session)
    bulk = _files(db_session, user_a.id, 50)
    (single,) = _files(db_session, user_b.id, 1)

    for file_uuid in bulk:
        dispatch_module.dispatch_transcription_pipeline(file_uuid)
    dispatch_module.dispatch_transcription_pipeline(single)

    assert _service_order(published).index(single) == 50
    assert {run["priorities"][0] for run in published} == {CPUPriority.PIPELINE_CRITICAL}


def test_a_retry_keeps_its_place_however_large_the_owners_backlog(db_session, published):
    """An interrupted run's automatic retry still goes ahead of new submissions."""
    user_a = make_user(db_session)
    bulk = _files(db_session, user_a.id, DEFAULT_TRANSCRIPTION_FAIR_SHARE_THRESHOLD + 2)
    for file_uuid in bulk[:-1]:
        dispatch_module.dispatch_transcription_pipeline(file_uuid)

    dispatch_module.dispatch_transcription_pipeline(bulk[-1], retry=True)

    assert published[-1]["priorities"][0] == CPUPriority.PIPELINE_RETRY
    assert published[-1]["priorities"][1] == GPUPriority.TRANSCRIPTION_RETRY


def test_the_batch_path_demotes_past_the_threshold_too(db_session, monkeypatch):
    """``dispatch_batch_transcription`` (bulk reprocess) applies the same rule per file."""
    from unittest.mock import patch

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(dispatch_module, "session_scope", _scope)
    user_a = make_user(db_session)
    bulk = _files(db_session, user_a.id, DEFAULT_TRANSCRIPTION_FAIR_SHARE_THRESHOLD + 3)

    with (
        patch(f"{dispatch_module.__name__}.group") as group,
        patch("app.core.redis.get_redis"),
    ):
        group.return_value.apply_async.return_value.id = "batch"
        dispatch_module.dispatch_batch_transcription(bulk)

    (chains,) = group.call_args.args
    first_stage = [chain.tasks[0].options["priority"] for chain in chains]
    threshold = DEFAULT_TRANSCRIPTION_FAIR_SHARE_THRESHOLD
    assert (
        first_stage
        == [CPUPriority.PIPELINE_CRITICAL] * threshold + [CPUPriority.PIPELINE_BACKLOG] * 3
    )
