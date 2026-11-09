"""A transcript page loads each speaker once, not once per segment (#1337, D2)."""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.services.file_route_queries import page_transcript_segments
from tests.query_budget import count_statements

pytestmark = pytest.mark.unit


def _file_with_segments(db, owner, n):
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        filename="t.mp4",
        title="t",
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
    )
    db.add(media)
    db.flush()
    speakers = [
        Speaker(
            uuid=uuid_pkg.uuid4(), user_id=owner.id, media_file_id=media.id, name=f"SPEAKER_0{i}"
        )
        for i in range(2)
    ]
    db.add_all(speakers)
    db.flush()
    db.add_all(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media.id,
            speaker_id=speakers[i % 2].id,
            start_time=float(i),
            end_time=i + 0.5,
            text=f"s{i}",
        )
        for i in range(n)
    )
    db.flush()
    return media.id


def test_speaker_is_not_joined_per_segment_and_statements_stay_constant(db_session, normal_user):
    small_id = _file_with_segments(db_session, normal_user, 3)
    big_id = _file_with_segments(db_session, normal_user, 30)
    db_session.expire_all()
    with count_statements(db_session.connection()) as small:
        rows, total = page_transcript_segments(db_session, small_id, None, 0)
        assert all(r.speaker is not None for r in rows) and total == 3
    db_session.expire_all()
    with count_statements(db_session.connection()) as big:
        rows, total = page_transcript_segments(db_session, big_id, None, 0)
        names = {r.speaker.name for r in rows if r.speaker is not None}
        assert names == {"SPEAKER_00", "SPEAKER_01"} and total == 30
    assert big.count == small.count, big.describe()
    segment_selects = big.matching(r"FROM transcript_segment")
    assert segment_selects and not [s for s in segment_selects if "JOIN speaker" in s], (
        big.describe()
    )
