"""Row lookups behind the transcript-segment speaker routes.

Plain ``Session`` helpers; none of them commits or flushes.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker


def count_speaker_segments(db: Session, speaker_id: int) -> int:
    """How many transcript segments are still attributed to a speaker."""
    return db.query(TranscriptSegment).filter(TranscriptSegment.speaker_id == speaker_id).count()


def get_speaker(db: Session, speaker_id: int) -> Speaker | None:
    """One speaker by primary key, or None."""
    return db.query(Speaker).filter(Speaker.id == speaker_id).first()


def get_segment_media_file(db: Session, media_file_id: int) -> MediaFile | None:
    """The media file a segment belongs to, or None when it is gone."""
    return db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
