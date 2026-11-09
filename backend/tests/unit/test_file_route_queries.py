"""Pins for ``app/services/file_route_queries.py`` and ``upload_prepare_queries.py``.

The statements the ``/api/files`` routes used to run inline. Each test runs against the real
database; none of the helpers may commit, so every route keeps its own transaction boundary.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.models.media import Analytics
from app.models.media import Comment
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.upload_batch import UploadBatch
from app.models.user import User
from app.services import file_route_queries as q
from app.services import upload_prepare_queries as up


@pytest.fixture
def no_commit(db_session, monkeypatch):
    commits: list[int] = []
    monkeypatch.setattr(db_session, "commit", lambda: commits.append(1))
    yield commits
    assert commits == []


def _id(row: Any) -> int:
    """The id of a row a lookup must have found (fails the test when it returned None)."""
    assert row is not None
    return int(row.id)


def _user(db) -> User:
    user = User(
        email=f"frq-{uuid.uuid4().hex[:8]}@example.com",
        full_name="frq user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.flush()
    return user


def _file(db, owner: User, *, status=FileStatus.COMPLETED, content_type="audio/wav") -> MediaFile:
    file_uuid = uuid.uuid4()
    media = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="f.wav",
        storage_path=f"f/{file_uuid}.wav",
        content_type=content_type,
        file_size=1,
        status=status,
    )
    db.add(media)
    db.flush()
    return media


def _segment(db, media, start, end, speaker=None) -> TranscriptSegment:
    seg = TranscriptSegment(
        media_file_id=media.id,
        speaker_id=speaker.id if speaker else None,
        start_time=start,
        end_time=end,
        text="t",
    )
    db.add(seg)
    db.flush()
    return seg


def _speaker(db, media, name="S") -> Speaker:
    speaker = Speaker(user_id=media.user_id, media_file_id=media.id, name=name, uuid=uuid.uuid4())
    db.add(speaker)
    db.flush()
    return speaker


def test_file_lookups_respect_their_own_scope(db_session, no_commit):
    owner, other = _user(db_session), _user(db_session)
    media = _file(db_session, owner)
    assert _id(q.get_file_by_id(db_session, media.id)) == media.id
    assert q.get_file_by_id(db_session, -1) is None
    assert _id(q.get_file_by_public_uuid(db_session, media.uuid)) == media.id
    assert _id(q.get_owned_file_by_uuid(db_session, media.uuid, owner.id)) == media.id
    assert q.get_owned_file_by_uuid(db_session, media.uuid, other.id) is None
    assert q.get_owned_file_in_tenant(db_session, media.id, user_id=owner.id, organization_id=None)
    assert (
        q.get_owned_file_in_tenant(db_session, media.id, user_id=other.id, organization_id=None)
        is None
    )


def test_get_pending_upload_needs_pending_status_and_the_callers_tenant(db_session, no_commit):
    owner = _user(db_session)
    pending = _file(db_session, owner, status=FileStatus.PENDING)
    done = _file(db_session, owner)
    uuid_of_pending = pending.uuid
    assert (
        _id(
            q.get_pending_upload(
                db_session, uuid_of_pending, user_id=owner.id, organization_id=None
            )
        )
        == pending.id
    )
    assert (
        q.get_pending_upload(db_session, done.uuid, user_id=owner.id, organization_id=None) is None
    )
    assert (
        q.get_pending_upload(db_session, pending.uuid, user_id=owner.id, organization_id=7) is None
    )


def test_withdraw_pending_redaction_only_moves_a_file_still_pending(db_session, no_commit):
    from app.core import redaction_defaults

    media = _file(db_session, _user(db_session))
    media.redaction_status = redaction_defaults.REDACTION_STATUS_PENDING
    db_session.flush()
    assert q.withdraw_pending_redaction(db_session, media.id, "none") == 1
    assert q.withdraw_pending_redaction(db_session, media.id, "other") == 0


def test_segment_reads_scope_to_the_file_and_order_by_time(db_session, no_commit):
    owner = _user(db_session)
    media, other = _file(db_session, owner), _file(db_session, owner)
    late = _segment(db_session, media, 5.0, 6.0)
    early = _segment(db_session, media, 0.0, 1.0)
    _segment(db_session, other, 0.0, 1.0)
    assert q.file_has_segments(db_session, media.id)
    assert not q.file_has_segments(db_session, -1)
    assert [s.id for s in q.list_ordered_segments(db_session, media.id)] == [early.id, late.id]
    assert _id(q.get_segment_with_speaker(db_session, early.uuid, media.id)) == early.id
    assert q.get_segment_with_speaker(db_session, early.uuid, other.id) is None
    assert q.segment_stats(db_session, media.id) == (2, 6.0)
    assert q.segment_stats(db_session, -1) == (0, 0.0)


def test_page_transcript_segments_pages_and_reports_the_full_total(db_session, no_commit):
    media = _file(db_session, _user(db_session))
    ids = [_segment(db_session, media, float(i), i + 0.5).id for i in range(5)]
    page, total = q.page_transcript_segments(db_session, media.id, 2, 1)
    assert total == 5
    assert [s.id for s in page] == ids[1:3]
    whole, total = q.page_transcript_segments(db_session, media.id, None, 0)
    assert (len(whole), total) == (5, 5)


def test_count_speaker_turns_merges_adjacent_same_speaker_segments(db_session, no_commit):
    media = _file(db_session, _user(db_session))
    a, b = _speaker(db_session, media, "A"), _speaker(db_session, media, "B")
    for start, who in enumerate([a, a, b, a]):
        _segment(db_session, media, float(start), start + 0.5, who)
    assert q.count_speaker_turns(db_session, media.id) == 3
    assert q.count_speaker_turns(db_session, -1) == 0


def test_speaker_comment_and_analytics_reads(db_session, no_commit):
    owner = _user(db_session)
    media = _file(db_session, owner)
    speaker = _speaker(db_session, media)
    late = Comment(media_file_id=media.id, user_id=owner.id, text="b", timestamp=9)
    early = Comment(media_file_id=media.id, user_id=owner.id, text="a", timestamp=1)
    db_session.add_all([late, early])
    db_session.add(Analytics(media_file_id=media.id, overall_analytics={}))
    db_session.flush()
    assert [s.id for s in q.list_file_speakers(db_session, media.id)] == [speaker.id]
    assert [s.id for s in q.list_file_speakers_with_profile(db_session, media.id)] == [speaker.id]
    assert q.list_file_speaker_ids(db_session, media.id) == [speaker.id]
    assert [c.id for c in q.list_comments_by_time(db_session, media.id)] == [early.id, late.id]
    assert q.get_file_analytics(db_session, media.id) is not None
    assert q.get_file_analytics(db_session, -1) is None


def test_clearing_helpers_remove_only_the_named_files_rows(db_session, no_commit):
    owner = _user(db_session)
    media, other = _file(db_session, owner), _file(db_session, owner)
    speaker = _speaker(db_session, media)
    _segment(db_session, media, 0.0, 1.0, speaker)
    _segment(db_session, other, 0.0, 1.0)
    db_session.add(Analytics(media_file_id=media.id, overall_analytics={}))
    db_session.flush()

    q.unlink_segment_speakers(db_session, media.id)
    assert db_session.query(TranscriptSegment).filter_by(speaker_id=speaker.id).count() == 0
    q.delete_file_speakers(db_session, media.id)
    assert q.list_file_speakers(db_session, media.id) == []
    q.delete_file_analytics(db_session, media.id)
    db_session.flush()
    assert q.get_file_analytics(db_session, media.id) is None
    q.delete_file_segments(db_session, media.id)
    assert q.file_has_segments(db_session, other.id)
    assert not q.file_has_segments(db_session, media.id)


def test_find_imported_duplicate_matches_url_then_legacy_keys_in_the_callers_tenant(
    db_session, no_commit
):
    owner, other = _user(db_session), _user(db_session)
    by_url = _file(db_session, owner)
    by_url.source_url = "https://example.com/v/1"
    by_meta = _file(db_session, owner)
    by_meta.metadata_raw = {"video_id": "vid-2"}
    by_legacy = _file(db_session, owner)
    by_legacy.metadata_raw = {"youtube_id": "vid-3"}
    db_session.flush()

    def find(user, url, video):
        return q.find_imported_duplicate(
            db_session, user_id=user.id, organization_id=None, normalized_url=url, video_id=video
        )

    assert find(owner, "https://example.com/v/1", "x").id == by_url.id
    assert find(owner, "https://nope", "vid-2").id == by_meta.id
    assert find(owner, "https://nope", "vid-3").id == by_legacy.id
    assert find(owner, "https://nope", "none") is None
    assert find(other, "https://example.com/v/1", "vid-2") is None


def test_waveform_reads_cover_only_completed_audio_and_video(db_session, no_commit):
    owner = _user(db_session)
    audio = _file(db_session, owner)
    video = _file(db_session, owner, content_type="video/mp4")
    video.waveform_data = {"peaks": []}
    _file(db_session, owner, status=FileStatus.PENDING)
    _file(db_session, owner, content_type="application/pdf")
    db_session.flush()

    needing = {f.id for f in q.waveform_candidates(db_session, force_regenerate=False)}
    everything = {f.id for f in q.waveform_candidates(db_session, force_regenerate=True)}
    assert audio.id in needing and video.id not in needing
    assert {audio.id, video.id} <= everything
    total, completed, with_waveform = q.waveform_counts(db_session)
    assert completed >= 2 and with_waveform >= 1 and total >= completed


def test_gallery_base_query_scopes_by_ownership(db_session, no_commit):
    owner, other = _user(db_session), _user(db_session)
    mine, theirs = _file(db_session, owner), _file(db_session, other)

    def ids(**kw):
        query, eff = q.gallery_base_query(db_session, [], user_id=owner.id, org_scope=None, **kw)
        return {f.id for f in query}, eff

    mine_ids, eff = ids(ownership="mine", sees_all=False)
    assert mine.id in mine_ids and theirs.id not in mine_ids and eff == owner.id
    all_ids, eff = ids(ownership="all", sees_all=True)
    assert {mine.id, theirs.id} <= all_ids and eff is None
    shared_ids, eff = ids(ownership="shared", sees_all=False)
    assert mine.id not in shared_ids and eff is None


def test_upload_batch_helpers_are_idempotent_and_count_atomically(db_session, no_commit):
    owner = _user(db_session)
    batch_uuid = uuid.uuid4()
    assert up.insert_upload_batch_if_absent(db_session, batch_uuid, owner.id, "multi_upload")
    assert not up.insert_upload_batch_if_absent(db_session, batch_uuid, owner.id, "multi_upload")
    batch = up.get_upload_batch(db_session, batch_uuid)
    assert batch.file_count == 0
    up.bump_upload_batch_file_count(db_session, batch.id)
    up.bump_upload_batch_file_count(db_session, batch.id)
    db_session.refresh(batch)
    assert batch.file_count == 2
    assert db_session.query(UploadBatch).filter_by(uuid=batch_uuid).count() == 1


def test_attach_tags_to_file_skips_tags_already_attached(db_session, no_commit):
    from app.models.tag import FileTag
    from app.models.tag import Tag

    owner = _user(db_session)
    media = _file(db_session, owner)
    tag = Tag(name=f"frq-{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db_session.add(tag)
    db_session.flush()
    up.attach_tags_to_file(db_session, media.id, [tag.id, tag.id])
    up.attach_tags_to_file(db_session, media.id, [tag.id])
    assert db_session.query(FileTag).filter_by(media_file_id=media.id).count() == 1
    assert [t.id for t in q.list_file_tags(db_session, media.id, None)] == [tag.id]


def test_reprocess_clear_keeps_its_single_commit_per_clear(db_session, monkeypatch):
    """Transaction boundary: the clear commits once, after the deletes, as it did inline."""
    from app.api.endpoints.files import reprocess_clear
    from app.tasks import search_file_field_tasks

    monkeypatch.setattr(search_file_field_tasks.index_file_summary, "delay", lambda *_a: None)
    monkeypatch.setattr(reprocess_clear, "reconcile_speaker_impact", lambda *_a: [])
    monkeypatch.setattr(reprocess_clear, "capture_speaker_impact", lambda *_a, **_k: None)
    media = _file(db_session, _user(db_session))
    _segment(db_session, media, 0.0, 1.0, _speaker(db_session, media))
    commits: list[int] = []
    real_commit = db_session.commit

    def counting_commit() -> None:
        commits.append(1)
        real_commit()

    monkeypatch.setattr(db_session, "commit", counting_commit)

    reprocess_clear.clear_existing_transcription_data(db_session, media)

    assert commits == [1]
    assert not q.file_has_segments(db_session, media.id)
    assert q.list_file_speakers(db_session, media.id) == []
