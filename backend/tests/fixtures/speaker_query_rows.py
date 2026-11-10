"""Row builders shared by the speaker query and speaker endpoint suites.

``file_row`` / ``speaker_row`` / ``segment_rows`` only ``flush``, so they run inside the
savepoint-isolated ``db_session``. ``committed_file_with_speaker`` commits, for endpoint
tests whose request opens its own session view of the rows.
"""

from __future__ import annotations

import uuid

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker


def file_row(db, owner, **kw) -> MediaFile:
    mf = MediaFile(
        user_id=owner.id,
        filename=f"{uuid.uuid4().hex[:6]}.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status=kw.pop("status", "completed"),
        **kw,
    )
    db.add(mf)
    db.flush()
    return mf


def speaker_row(db, owner, mf, **kw) -> Speaker:
    spk = Speaker(
        user_id=owner.id,
        media_file_id=mf.id,
        name=kw.pop("name", f"S_{uuid.uuid4().hex[:6]}"),
        **kw,
    )
    db.add(spk)
    db.flush()
    return spk


def segment_rows(db, mf, speaker, starts_and_lengths):
    rows = [
        TranscriptSegment(
            media_file_id=mf.id,
            speaker_id=speaker.id,
            start_time=start,
            end_time=start + length,
            text="t",
        )
        for start, length in starts_and_lengths
    ]
    db.add_all(rows)
    db.flush()
    return rows


def committed_file_with_speaker(db_session, owner) -> tuple[MediaFile, Speaker]:
    mf = MediaFile(
        user_id=owner.id,
        filename="c.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    spk = Speaker(user_id=owner.id, media_file_id=mf.id, name="SPEAKER_00", display_name="Ann")
    db_session.add(spk)
    db_session.commit()
    db_session.refresh(spk)
    return mf, spk
