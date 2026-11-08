"""Choosing which audio track of a multi-track file is transcribed and played (issue #1263).

Used by the reprocess endpoint. The choice is validated against the tracks ffprobe stored at
ingest BEFORE anything destructive runs (an unknown index is a 422), then stored on the file;
the decode paths read it from there (``utils/audio_stream``).
"""

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.utils.audio_stream import effective_audio_index
from app.utils.audio_stream import validate_audio_stream_index

logger = logging.getLogger(__name__)

_AUDIO_FORMATS = ("mp3", "wav", "original")


def validate_audio_track_request(
    media_file: MediaFile, requested: int | None, stages: list[str] | None
) -> None:
    """422 when *requested* names no stored track, or the stages cannot use a track."""
    if requested is None:
        return
    if stages and not {"transcription", "rediarize"}.intersection(stages):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "audio_stream_index only applies when the 'transcription' or 'rediarize' "
                "stage is reprocessed"
            ),
        )
    if not validate_audio_stream_index(requested, media_file.audio_streams):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"This file has no audio track {requested}",
        )


@dataclass(frozen=True)
class TrackChange:
    """What the previous track left behind, for :func:`drop_stale_track_artifacts`."""

    file_id: int
    filename: str
    rendition: str | None


def apply_audio_track(
    db: Session, media_file: MediaFile, requested: int | None
) -> TrackChange | None:
    """Store the chosen track and forget what was derived from the old one.

    Returns None when the effective track is unchanged. Otherwise the choice is committed and
    the row stops pointing at the rendition and the waveform, both built from the previous
    track; the caller then removes the stored objects with :func:`drop_stale_track_artifacts`,
    outside any session.
    """
    if requested is None:
        return None
    if effective_audio_index(requested) == effective_audio_index(media_file.audio_stream_index):
        return None
    change = TrackChange(
        file_id=int(media_file.id),
        filename=str(media_file.filename or ""),
        rendition=str(media_file.playback_path) if media_file.playback_path else None,
    )
    media_file.audio_stream_index = int(requested)
    media_file.playback_path = None
    media_file.waveform_data = None
    db.commit()
    return change


def drop_stale_track_artifacts(change: TrackChange) -> None:
    """Delete the playback rendition and cached audio downloads of the previous track.

    Takes no session. Failures are logged, not raised: the row no longer points at the
    rendition, and the derived cache also expires by its bucket lifecycle rule, so a survivor
    costs storage until then.
    """
    from app.services.minio_bucket_admin import MinIOService
    from app.services.minio_service import delete_file
    from app.services.video_processing_service import VideoProcessingService

    try:
        service = VideoProcessingService(MinIOService())
        keys = [service.audio_cache_key(change.file_id, change.filename, f) for f in _AUDIO_FORMATS]
        service.minio_service.delete_objects(service.cache_bucket, keys)
    except Exception:
        logger.warning("Could not clear cached audio of file %s", change.file_id, exc_info=True)
    if change.rendition:
        try:
            delete_file(change.rendition)
        except Exception:
            logger.warning("Could not delete the old rendition %s", change.rendition, exc_info=True)
