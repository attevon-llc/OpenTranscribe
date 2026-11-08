"""``refresh_cleared_speakers`` issues a flat number of statements per batch (issue #1337, V5).

It loaded ORM rows and lazy-loaded each speaker's profile while computing the status, then
flushed one UPDATE per dirty row. The profile is now eager-loaded and the writes are one
primary-key bulk UPDATE per batch, so the count must not grow with the speakers refreshed.
"""

import uuid as uuid_pkg

from app.models.media import MediaFile
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.services.privacy import purge
from tests.query_budget import count_statements


def _speakers(db_session, owner, n: int) -> list[Speaker]:
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename="refresh.mp4",
        storage_path=f"refresh/{uuid_pkg.uuid4().hex}.mp4",
        content_type="video/mp4",
        file_size=1,
        user_id=owner.id,
    )
    db_session.add(media)
    db_session.flush()
    rows = []
    for i in range(n):
        profile = SpeakerProfile(user_id=owner.id, name=f"Person {uuid_pkg.uuid4().hex[:6]}")
        db_session.add(profile)
        db_session.flush()
        rows.append(
            Speaker(
                user_id=owner.id,
                media_file_id=media.id,
                name=f"SPEAKER_{i:02d}",
                profile_id=profile.id,
            )
        )
    db_session.add_all(rows)
    db_session.commit()
    return rows


def _refresh(db_session, owner, n: int):
    rows = _speakers(db_session, owner, n)
    ids = [int(s.id) for s in rows]
    db_session.expire_all()
    with count_statements(db_session.connection()) as log:
        out = purge.refresh_cleared_speakers(db_session, ids, reindex=lambda files: 0)
    db_session.expire_all()
    statuses = {db_session.get(Speaker, i).computed_status for i in ids}
    return log, out, statuses


def test_statement_count_does_not_grow_with_the_speakers_refreshed(db_session, normal_user):
    small, small_out, small_status = _refresh(db_session, normal_user, 2)
    large, large_out, large_status = _refresh(db_session, normal_user, 12)

    assert small_out["statuses_refreshed"] == 2 and large_out["statuses_refreshed"] == 12
    assert None not in small_status | large_status  # control: statuses really written
    assert large.count == small.count, large.describe()
