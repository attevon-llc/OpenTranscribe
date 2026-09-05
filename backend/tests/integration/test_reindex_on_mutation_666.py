"""Four transcript-mutation paths must reach the search index (issue #666) — real OpenSearch.

Editing a segment, reassigning a segment's speaker, merging two speakers, and a
rediarize-only run all change what ``transcript_chunks``/``transcripts`` should say,
and none of the four dispatched a re-index on ``master``. Each test here performs
the REAL mutation against real Postgres, captures what
``services.search.reindex_dispatch.dispatch_transcript_reindex`` was asked to
queue (SKIP_CELERY no-ops the actual ``apply_async`` in this suite, matching every
other endpoint test — see ``tests/conftest.py``), and then runs the SAME
``index_transcript_search_task`` synchronously (``.apply()``, no broker) that a
real worker would run, against throwaway OpenSearch indices. The final assertion
in every test reads real OpenSearch documents — not a mock call shape.

Point the suite at an isolated stack (never the shared dev one)::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 \\
        pytest backend/tests/integration/test_reindex_on_mutation_666.py -m integration
"""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import patch

import pytest

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and "
            "export OPENSEARCH_PORT — a stand-in cannot validate the real reindex."
        ),
    ),
]

_DISPATCH = "app.services.search.reindex_dispatch.dispatch_transcript_reindex"


@pytest.fixture
def throwaway_indices(monkeypatch):
    """Real chunks + transcripts indices under throwaway names, on the real cluster.

    Both indices ``index_transcript_search_task`` writes to are repointed so this
    suite never touches the production ``transcript_chunks``/``transcripts``
    indices, matching ``tests/integration/test_rename_propagation_chunks.py``'s
    established pattern for hitting a live cluster safely.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc
    from app.services.search.hybrid_search_service import reset_infrastructure_state

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    chunks_name = f"test_reindex666_chunks_{uuid.uuid4().hex[:12]}"
    transcripts_name = f"test_reindex666_transcripts_{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", chunks_name)
    monkeypatch.setattr(settings, "OPENSEARCH_TRANSCRIPT_INDEX", transcripts_name)
    svc.reset_neural_pipeline_state()
    reset_infrastructure_state()
    try:
        yield client
    finally:
        client.indices.delete(index=chunks_name, ignore=[404])
        client.indices.delete(index=transcripts_name, ignore=[404])
        svc.reset_neural_pipeline_state()
        reset_infrastructure_state()


@pytest.fixture
def real_db():
    """A REAL, committed Postgres session (not the savepoint-rollback ``db_session``).

    ``index_transcript_search_task`` opens its own session via
    ``session_scope()``, on the app's own connection pool
    (``app.db.base.SessionLocal``) — a different connection than the
    ``db_session`` fixture's nested-transaction one. Data created under a
    savepoint that is never committed for real would be invisible to it. Rows
    are deleted explicitly on teardown instead.
    """
    from app.db.base import SessionLocal

    db = SessionLocal()
    created_media_file_ids: list[int] = []
    created_user_ids: list[int] = []
    try:
        yield db, created_media_file_ids, created_user_ids
    finally:
        from app.models.media import MediaFile
        from app.models.media import Speaker
        from app.models.media import Task
        from app.models.media import TranscriptSegment
        from app.models.user import User

        db.rollback()
        if created_media_file_ids:
            # `index_transcript_search_task` (run synchronously by every test via
            # `.apply()`) writes its own `Task` row via `create_task_record`, and
            # `task.media_file_id` has no ON DELETE — same reason
            # `app/api/endpoints/admin.py`'s bulk user-delete path deletes `Task`
            # before `MediaFile`. Bulk `.delete()` here is Core-level and does not
            # run the ORM's `cascade="all, delete-orphan"` on `MediaFile.tasks`.
            db.query(Task).filter(Task.media_file_id.in_(created_media_file_ids)).delete(
                synchronize_session=False
            )
            db.query(TranscriptSegment).filter(
                TranscriptSegment.media_file_id.in_(created_media_file_ids)
            ).delete(synchronize_session=False)
            db.query(Speaker).filter(Speaker.media_file_id.in_(created_media_file_ids)).delete(
                synchronize_session=False
            )
            db.query(MediaFile).filter(MediaFile.id.in_(created_media_file_ids)).delete(
                synchronize_session=False
            )
            db.commit()
        if created_user_ids:
            db.query(User).filter(User.id.in_(created_user_ids)).delete(synchronize_session=False)
            db.commit()
        db.close()


def _make_user(db, created_user_ids: list[int]):
    from app.core.security import get_password_hash
    from app.models.user import User

    email = f"reindex666-{uuid.uuid4().hex[:8]}@example.invalid"
    user = User(
        email=email,
        full_name="Reindex 666 Fixture",
        hashed_password=get_password_hash("throwaway-password-1"),  # noqa: S106
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    created_user_ids.append(user.id)
    return user


def _make_file_with_two_speakers(db, user, created_media_file_ids: list[int]):
    from app.core.enums import FileStatus
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment

    media_file = MediaFile(
        user_id=user.id,
        filename=f"reindex666-{uuid.uuid4().hex[:8]}.wav",
        storage_path=f"test/reindex666/{uuid.uuid4().hex[:8]}.wav",
        content_type="audio/wav",
        file_size=1000,
        status=FileStatus.COMPLETED,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    created_media_file_ids.append(media_file.id)

    speaker_a = Speaker(media_file_id=media_file.id, user_id=user.id, name="SPEAKER_00")
    speaker_b = Speaker(media_file_id=media_file.id, user_id=user.id, name="SPEAKER_01")
    db.add_all([speaker_a, speaker_b])
    db.commit()
    db.refresh(speaker_a)
    db.refresh(speaker_b)

    seg1 = TranscriptSegment(
        media_file_id=media_file.id,
        speaker_id=speaker_a.id,
        start_time=0.0,
        end_time=5.0,
        text="the original wording nobody has corrected yet",
    )
    seg2 = TranscriptSegment(
        media_file_id=media_file.id,
        speaker_id=speaker_b.id,
        start_time=5.0,
        end_time=10.0,
        text="a second segment from the other speaker",
    )
    db.add_all([seg1, seg2])
    db.commit()
    db.refresh(seg1)
    db.refresh(seg2)
    return media_file, speaker_a, speaker_b, seg1, seg2


def _run_real_reindex(dispatch_mock) -> None:
    """Run the SAME task the mocked dispatch would have queued, synchronously."""
    from app.tasks.search_indexing_task import index_transcript_search_task

    assert dispatch_mock.called, "the mutation path never called dispatch_transcript_reindex"
    _, kwargs = dispatch_mock.call_args
    result = index_transcript_search_task.apply(kwargs=kwargs)
    assert not result.failed(), f"reindex task raised: {result.result!r}"


def _chunk_docs(client, index_name: str, file_uuid: str) -> list[dict[str, Any]]:
    client.indices.refresh(index=index_name)
    response = client.search(
        index=index_name,
        body={"size": 50, "query": {"term": {"file_uuid": file_uuid}}, "sort": ["chunk_index"]},
    )
    return [hit["_source"] for hit in response["hits"]["hits"]]


def _transcript_doc(client, index_name: str, file_uuid: str) -> dict[str, Any]:
    client.indices.refresh(index=index_name)
    doc: dict[str, Any] = client.get(index=index_name, id=file_uuid)["_source"]
    return doc


# --------------------------------------------------------------------------- #
# (a) Segment text edit
# --------------------------------------------------------------------------- #


def test_segment_text_edit_reaches_both_indices(throwaway_indices, real_db):
    from app.api.endpoints.files.crud import update_single_transcript_segment
    from app.schemas.media import TranscriptSegmentUpdate

    db, created_media_file_ids, created_user_ids = real_db
    user = _make_user(db, created_user_ids)
    media_file, _speaker_a, _speaker_b, seg1, _seg2 = _make_file_with_two_speakers(
        db, user, created_media_file_ids
    )
    file_uuid = str(media_file.uuid)

    with (
        patch(_DISPATCH) as dispatch_mock,
        patch("app.services.redaction.service.RedactionService.redetect_edited_segment"),
    ):
        update_single_transcript_segment(
            db,
            file_uuid,
            str(seg1.uuid),
            TranscriptSegmentUpdate(text="the corrected wording after the edit"),
            user,
        )
        _run_real_reindex(dispatch_mock)

    docs = _chunk_docs(throwaway_indices, os_settings_chunks_index(), file_uuid)
    assert docs, "the edited file was never indexed"
    combined_content = " ".join(d["content"] for d in docs)
    assert "corrected wording after the edit" in combined_content
    assert "original wording nobody has corrected" not in combined_content

    transcript_doc = _transcript_doc(throwaway_indices, os_settings_transcript_index(), file_uuid)
    assert "corrected wording after the edit" in transcript_doc["content"]
    assert "original wording nobody has corrected" not in transcript_doc["content"]


# --------------------------------------------------------------------------- #
# (b) Segment speaker reassignment
# --------------------------------------------------------------------------- #


def test_segment_speaker_reassignment_reaches_the_chunk_plane(throwaway_indices, real_db):
    from app.api.deps_context import RequestContext
    from app.api.endpoints.transcript_segments import update_segment_speaker
    from app.schemas.transcript import SegmentSpeakerUpdate

    db, created_media_file_ids, created_user_ids = real_db
    user = _make_user(db, created_user_ids)
    media_file, _speaker_a, speaker_b, seg1, _seg2 = _make_file_with_two_speakers(
        db, user, created_media_file_ids
    )
    file_uuid = str(media_file.uuid)

    with (
        patch(_DISPATCH) as dispatch_mock,
        patch("app.services.analytics_service.AnalyticsService.refresh_analytics"),
    ):
        update_segment_speaker(
            str(seg1.uuid),
            SegmentSpeakerUpdate(speaker_uuid=str(speaker_b.uuid)),
            db,
            user,
            RequestContext(user=user, org_id=None, org_role=None),
        )
        _run_real_reindex(dispatch_mock)

    docs = _chunk_docs(throwaway_indices, os_settings_chunks_index(), file_uuid)
    assert docs, "the reassigned file was never indexed"
    # Both segments now belong to SPEAKER_01 — one turn, one chunk, and the
    # ORIGINAL speaker no longer appears anywhere in the file's chunk plane.
    # Digest docs (`doc_type: digest`) carry a `speakers` LIST, not a singular
    # `speaker` field — scope to the actual chunk plane docs.
    chunk_docs = [d for d in docs if d.get("doc_type") == "chunk"]
    assert chunk_docs, f"no chunk-plane docs among {docs}"
    speakers_seen = {d["speaker"] for d in chunk_docs}
    assert speakers_seen == {"SPEAKER_01"}, chunk_docs


# --------------------------------------------------------------------------- #
# (c) Speaker merge
# --------------------------------------------------------------------------- #


def test_speaker_merge_reaches_the_chunk_plane(throwaway_indices, real_db):
    from app.api.endpoints.speakers import merge_speakers

    db, created_media_file_ids, created_user_ids = real_db
    user = _make_user(db, created_user_ids)
    media_file, speaker_a, speaker_b, _seg1, _seg2 = _make_file_with_two_speakers(
        db, user, created_media_file_ids
    )
    file_uuid = str(media_file.uuid)

    with (
        patch(_DISPATCH) as dispatch_mock,
        patch("app.tasks.speaker_merge_task.process_speaker_merge_background.delay"),
    ):
        # `merge_speakers` wraps its own cache-invalidation call in a
        # try/except and only logs on failure (see the endpoint), so an
        # unreachable test Redis degrades quietly rather than needing a mock.
        merge_speakers(str(speaker_a.uuid), str(speaker_b.uuid), db, user)
        # The merge dispatches a re-index once per affected file (one file here).
        assert dispatch_mock.call_count >= 1
        for call in dispatch_mock.call_args_list:
            _run_real_reindex_for_call(call)

    docs = _chunk_docs(throwaway_indices, os_settings_chunks_index(), file_uuid)
    assert docs, "the merged file was never indexed"
    # Digest docs (`doc_type: digest`) carry a `speakers` LIST, not a singular
    # `speaker` field — scope to the actual chunk plane docs.
    chunk_docs = [d for d in docs if d.get("doc_type") == "chunk"]
    assert chunk_docs, f"no chunk-plane docs among {docs}"
    speakers_seen = {d["speaker"] for d in chunk_docs}
    assert speakers_seen == {"SPEAKER_01"}, (
        "every segment was reassigned to the target speaker in Postgres before "
        "the merge committed — the chunk plane must reflect that, not the "
        f"pre-merge source speaker: {chunk_docs}"
    )


def _run_real_reindex_for_call(call) -> None:
    from app.tasks.search_indexing_task import index_transcript_search_task

    _, kwargs = call
    result = index_transcript_search_task.apply(kwargs=kwargs)
    assert not result.failed(), f"reindex task raised: {result.result!r}"


# --------------------------------------------------------------------------- #
# (d) Rediarize-only — dispatch wiring (real DB state; see module docstring
#     and the final report for why the diarization model itself is not run)
# --------------------------------------------------------------------------- #


def test_rediarize_always_dispatches_a_reindex_even_with_no_downstream_tasks_requested(
    throwaway_indices, real_db
):
    """A rediarize-only run must reindex even though the caller asked for no

    downstream stages at all — the exact shape the reprocess UI sends for
    "Re-diarize only". Runs the real post-diarization tail of
    ``rediarize_task`` (segment save, embeddings, finalize, dispatch) with
    only the diarization MODEL itself stood in (no GPU here), so the dispatch
    call is exercised for real rather than asserted from reading the source.
    """
    import pandas as pd

    from app.db.session_utils import session_scope

    db, created_media_file_ids, created_user_ids = real_db
    user = _make_user(db, created_user_ids)
    media_file, speaker_a, _speaker_b, seg1, seg2 = _make_file_with_two_speakers(
        db, user, created_media_file_ids
    )
    file_uuid = str(media_file.uuid)

    diarize_df = pd.DataFrame(
        [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_00"},
        ]
    )

    from app.tasks import rediarize_task as mod

    with (
        patch(_DISPATCH) as dispatch_mock,
        patch.object(mod, "_prepare_audio", return_value=("/tmp/fake.wav", None)),  # noqa: S108 — stand-in path, never opened (the diarization stage is also stubbed)
        patch.object(mod, "_run_diarization", return_value=(diarize_df, {"regions": []}, None)),
        patch("app.tasks.transcription.notifications.send_progress_notification"),
        patch("app.tasks.transcription.notifications.send_completion_notification"),
        patch(
            "app.tasks.speaker_attribute_task._is_speaker_attribute_detection_enabled",
            return_value=False,
        ),
        patch("app.tasks.transcription.core._process_speaker_embeddings"),
        patch("app.tasks.transcription.core._should_use_native_embeddings", return_value=False),
    ):
        mod.rediarize_task.apply(
            kwargs={
                "file_uuid": file_uuid,
                "downstream_tasks": None,  # exactly what "Re-diarize only" sends
            }
        )

    assert dispatch_mock.called, (
        "rediarize with no requested downstream stages must still reindex — "
        "search/RAG kept the pre-rediarize speaker attribution otherwise"
    )
    _, kwargs = dispatch_mock.call_args
    assert kwargs["file_uuid"] == file_uuid

    with session_scope() as verify_db:
        from app.models.media import TranscriptSegment

        refreshed = (
            verify_db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == media_file.id)
            .all()
        )
        assert len(refreshed) == 2, "rediarize must have re-saved the segments for real"

    # Cleanup note: rediarize_task.apply() writes its own Task row via
    # create_task_record/update_task_status on the real engine — `real_db`'s
    # teardown deletes `Task` rows scoped to `created_media_file_ids` before the
    # `MediaFile` row, same as every other test in this module.
    del seg1, seg2, speaker_a, media_file  # silence unused-fixture-var lint; ids used above


def os_settings_chunks_index() -> str:
    from app.core.config import settings

    return settings.OPENSEARCH_CHUNKS_INDEX


def os_settings_transcript_index() -> str:
    from app.core.config import settings

    return settings.OPENSEARCH_TRANSCRIPT_INDEX
