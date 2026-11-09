"""Service-level behaviour of the topic-suggestion and summary-column helpers."""

from __future__ import annotations

import uuid

import pytest

from app.models.media import MediaFile
from app.models.topic import TopicSuggestion
from app.models.upload_batch import UploadBatch
from app.services.file_summary_writes import clear_summary_columns
from app.services.topic_suggestion_queries import link_files_to_new_batch
from app.services.topic_suggestion_queries import suggestion_for_file


def _file(db, owner, **kw) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    mf = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="f.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1,
        status="completed",
        **kw,
    )
    db.add(mf)
    db.flush()
    return mf


def test_suggestion_for_file_returns_only_that_files_row(db_session, normal_user):
    mine, other = _file(db_session, normal_user), _file(db_session, normal_user)
    row = TopicSuggestion(media_file_id=mine.id, user_id=normal_user.id, status="pending")
    db_session.add(row)
    db_session.flush()
    assert suggestion_for_file(db_session, mine.id) is row
    assert suggestion_for_file(db_session, other.id) is None


def test_link_files_creates_one_batch_for_unbatched_files(db_session, normal_user):
    files = [_file(db_session, normal_user) for _ in range(2)]
    batch = link_files_to_new_batch(db_session, [str(f.uuid) for f in files], normal_user.id)
    assert batch is not None and batch.file_count == 2
    assert {f.upload_batch_id for f in files} == {batch.id}


def test_link_files_leaves_files_that_already_share_a_batch(db_session, normal_user):
    existing = UploadBatch(uuid=uuid.uuid4(), user_id=normal_user.id, source="x", file_count=2)
    db_session.add(existing)
    db_session.flush()
    files = [_file(db_session, normal_user, upload_batch_id=existing.id) for _ in range(2)]
    assert link_files_to_new_batch(db_session, [str(f.uuid) for f in files], normal_user.id) is None
    assert {f.upload_batch_id for f in files} == {existing.id}


def test_clear_summary_columns_nulls_only_the_named_columns(db_session, normal_user):
    mf = _file(db_session, normal_user, summary_data={"a": 1}, summary_opensearch_id="os-1")
    clear_summary_columns(db_session, mf.id, ["summary_opensearch_id"])
    db_session.refresh(mf)
    assert mf.summary_opensearch_id is None
    assert mf.summary_data == {"a": 1}


def test_clear_summary_columns_rejects_other_columns(db_session, normal_user):
    mf = _file(db_session, normal_user)
    with pytest.raises(ValueError, match="filename"):
        clear_summary_columns(db_session, mf.id, ["filename"])
