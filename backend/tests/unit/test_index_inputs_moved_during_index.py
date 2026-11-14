"""A rename or tag change that lands while a full index runs is not lost to its bulk write.

``index_transcript_search_task`` and ``reindex_batch`` read the file's speaker labels and tags,
then chunk and embed for seconds to minutes before writing. The rename's or the tag change's
own ``update_by_query`` rewrites the documents that exist then, which the bulk write replaces
with the snapshot. The indexer now compares the file's labels and tags before and after and
queues the file's rebuild when they moved. The index write is faked so that the edit lands
inside it; the dispatch boundary, ``queue_transcript_reindex``, is recorded.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import pytest
from fixtures.tag_world import attach
from fixtures.tag_world import make_tag

from app.services.privacy import policy
from app.services.search.reindex_dispatch import ReindexDispatch
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.provenance_world import build_world


class _Redis:
    """The run-state calls ``reindex_batch`` makes, in memory."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], int] = {}

    def hget(self, key, field):
        return self.values.get((key, field))

    def hincrby(self, key, field, amount=1):
        self.values[(key, field)] = int(self.values.get((key, field)) or 0) + amount
        return self.values[(key, field)]

    def sadd(self, key, *values):
        return len(values)

    def expire(self, key, seconds):
        return True


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


@pytest.fixture
def rebuilds(monkeypatch, db_session) -> list[dict[str, Any]]:
    """Every session is the test's; the rebuild dispatch is recorded."""
    queued: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def scope():
        yield db_session

    def queue(**kwargs: Any) -> ReindexDispatch:
        queued.append(kwargs)
        return ReindexDispatch.QUEUED

    monkeypatch.setattr("app.db.session_utils.session_scope", scope)
    monkeypatch.setattr("app.services.search.reindex_dispatch.queue_transcript_reindex", queue)
    return queued


def _rename_alice(db, world) -> None:
    world.alice.display_name = "Renamed mid-index"
    db.commit()


def _tag_it(db, world) -> None:
    attach(db, world.file, make_tag(db, "added-mid-index", user_id=world.file.user_id))
    db.commit()


def _nothing(db, world) -> None:
    return None


def _run_index_task(monkeypatch, db, world, user, during: Callable[[Any, Any], None]) -> None:
    from app.services.search import indexing_service
    from app.tasks import search_indexing_task

    def fake_chunks(self, **kwargs):
        during(db, world)
        return {"chunk_count": 1}

    monkeypatch.setattr(
        indexing_service.TranscriptIndexingService, "index_transcript_chunks", fake_chunks
    )
    monkeypatch.setattr("app.services.opensearch_service.index_transcript", lambda *a, **k: None)
    result = search_indexing_task.index_transcript_search_task.apply(
        kwargs={"file_id": world.file.id, "file_uuid": str(world.file.uuid), "user_id": user.id}
    ).get()
    assert result["status"] == "success", result


def _run_reindex_batch(monkeypatch, db, world, user, during: Callable[[Any, Any], None]) -> None:
    from app.tasks import reindex_task as rix

    @contextlib.contextmanager
    def scope():
        yield db

    class _Tracker:
        def __init__(self, **kwargs: Any) -> None:
            pass

        @staticmethod
        def get_state(task_type, user_id):
            return None

        def start(self, message=""):
            pass

        def resume_from_state(self, state):
            pass

    class _Indexing:
        def reindex_transcript(self, **kwargs):
            during(db, world)
            return 1

    monkeypatch.setattr(rix, "session_scope", scope)
    monkeypatch.setattr(rix, "get_redis", lambda: _Redis())
    monkeypatch.setattr(rix, "_send_reindex_progress", lambda *a, **kw: None)
    monkeypatch.setattr(rix, "_is_cancellation_requested", lambda user_id, run_id=None: False)
    monkeypatch.setattr(rix, "_handle_reindex_completion", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.progress_tracker.ProgressTracker", _Tracker)
    monkeypatch.setattr("app.services.search.indexing_service.TranscriptIndexingService", _Indexing)
    stats = rix.reindex_batch_task([world.file.id], user.id)
    assert stats["indexed"] == 1, stats


@pytest.mark.parametrize("run", [_run_index_task, _run_reindex_batch])
@pytest.mark.parametrize("edit", [_rename_alice, _tag_it])
def test_an_edit_during_the_index_queues_the_file_rebuild(
    db_session, normal_user, monkeypatch, rebuilds, run, edit
):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    run(monkeypatch, db_session, world, normal_user, edit)

    assert rebuilds == [
        {"file_id": world.file.id, "file_uuid": str(world.file.uuid), "user_id": normal_user.id}
    ]


@pytest.mark.parametrize("run", [_run_index_task, _run_reindex_batch])
def test_an_index_with_no_edit_queues_nothing(db_session, normal_user, monkeypatch, rebuilds, run):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    run(monkeypatch, db_session, world, normal_user, _nothing)

    assert rebuilds == []
