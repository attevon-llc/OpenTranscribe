"""Pins for ``app/services/transcript_segment_queries.py``."""

from __future__ import annotations

import uuid

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.user import User
from app.services.transcript_segment_queries import count_speaker_segments
from app.services.transcript_segment_queries import get_segment_media_file
from app.services.transcript_segment_queries import get_speaker


def _world(db):
    user = User(
        email=f"seg-{uuid.uuid4().hex[:8]}@example.com",
        full_name="seg user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.flush()
    file_uuid = uuid.uuid4()
    media = MediaFile(
        uuid=file_uuid,
        user_id=user.id,
        filename="s.wav",
        storage_path=f"s/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1,
        status="completed",
    )
    db.add(media)
    db.flush()
    speaker = Speaker(user_id=user.id, media_file_id=media.id, name="S1", uuid=uuid.uuid4())
    db.add(speaker)
    db.flush()
    return media, speaker


def test_count_speaker_segments_counts_only_that_speakers_segments(db_session):
    media, speaker = _world(db_session)
    other = Speaker(user_id=speaker.user_id, media_file_id=media.id, name="S2", uuid=uuid.uuid4())
    db_session.add(other)
    db_session.flush()
    for index, who in enumerate((speaker, speaker, other)):
        db_session.add(
            TranscriptSegment(
                media_file_id=media.id,
                speaker_id=who.id,
                start_time=float(index),
                end_time=index + 0.5,
                text="x",
            )
        )
    db_session.flush()
    assert count_speaker_segments(db_session, speaker.id) == 2
    assert count_speaker_segments(db_session, other.id) == 1
    assert count_speaker_segments(db_session, -1) == 0


def test_get_speaker_and_media_file_return_the_row_or_none(db_session, monkeypatch):
    commits: list[int] = []
    monkeypatch.setattr(db_session, "commit", lambda: commits.append(1))
    media, speaker = _world(db_session)
    found_speaker = get_speaker(db_session, speaker.id)
    assert found_speaker is not None
    assert found_speaker.id == speaker.id
    assert get_speaker(db_session, -1) is None
    found_file = get_segment_media_file(db_session, media.id)
    assert found_file is not None
    assert found_file.id == media.id
    assert get_segment_media_file(db_session, -1) is None
    assert commits == []
