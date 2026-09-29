"""``media.create_playback_rendition``: encode a browser-playable copy of an original.

Queued by preprocessing (``transcription/preprocess.py``) only for originals that
``services.playback_rendition.classify_playback`` says no browser plays. It runs on the
CPU queue beside the transcription, not inside it, so it never delays the GPU stage.
Measured on a 60-minute AIFF, the encode is ~1 minute of CPU, which is longer than
preprocessing itself.
"""

from __future__ import annotations

import logging
import os
import tempfile

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services.playback_rendition import RENDITION_CONTENT_TYPE
from app.services.playback_rendition import PlaybackNeed
from app.services.playback_rendition import classify_playback
from app.services.playback_rendition import encode_audio_rendition
from app.services.playback_rendition import probe_media
from app.services.playback_rendition import rendition_object_name

logger = logging.getLogger(__name__)


def _load(file_uuid: str) -> tuple[int, str, str | None] | None:
    """(id, storage_path, playback_path) for the file, or None if it is gone."""
    with session_scope() as db:
        row = (
            db.query(MediaFile.id, MediaFile.storage_path, MediaFile.playback_path)
            .filter(MediaFile.uuid == file_uuid)
            .first()
        )
        if row is None or not row.storage_path:
            return None
        return int(row.id), str(row.storage_path), row.playback_path


def _record(file_id: int, object_name: str) -> bool:
    """Point the row at the stored rendition. False if the row was deleted meanwhile.

    A takedown that landed while the encode ran has already tagged the original; the
    new object is tagged here too, or a presigned URL for it would outlive the takedown.
    """
    with session_scope() as db:
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if media_file is None:
            return False
        media_file.playback_path = object_name
        quarantined = bool(media_file.is_quarantined)
        db.commit()
    if quarantined:
        from app.services.minio_service import set_object_quarantine_tag

        set_object_quarantine_tag(object_name, True)
    return True


@celery_app.task(
    bind=True,
    name="media.create_playback_rendition",
    priority=CPUPriority.PIPELINE_CRITICAL,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=2,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=120,
)
def create_playback_rendition_task(self, file_uuid: str) -> dict:
    """Encode, store and record the playback rendition for one media file.

    Idempotent: a file that already has a rendition is left alone, so a reprocess or a
    redelivered message costs nothing. The original is re-probed here rather than
    trusting the dispatcher, because this is the step that spends the CPU.

    Returns:
        ``{"status": ...}``: ``created``, ``exists``, ``not_needed``, ``missing`` or
        ``failed``. A failed encode is not retried: it would fail the same way again,
        and the file still transcribes. It only means the original is what plays.
    """
    loaded = _load(file_uuid)
    if loaded is None:
        return {"status": "missing"}
    file_id, storage_path, playback_path = loaded
    if playback_path:
        return {"status": "exists"}

    from app.services.minio_service import delete_file
    from app.services.minio_service import download_file_to_path
    from app.services.minio_service import upload_file

    object_name = rendition_object_name(storage_path)
    with tempfile.TemporaryDirectory(prefix="playback-") as work_dir:
        source = os.path.join(work_dir, "original" + os.path.splitext(storage_path)[1])
        download_file_to_path(storage_path, source)
        try:
            probe = probe_media(source)
            need = classify_playback(probe)
            if need is PlaybackNeed.NONE:
                return {"status": "not_needed"}
            output = os.path.join(work_dir, "playback.m4a")
            encode_audio_rendition(source, output, probe)
        except RuntimeError as e:
            logger.warning("Playback rendition failed for file %s: %s", file_id, e)
            return {"status": "failed"}

        with open(output, "rb") as fh:
            upload_file(fh, os.path.getsize(output), object_name, RENDITION_CONTENT_TYPE)

    if not _record(file_id, object_name):
        # Deleted while encoding: its purge ran before this object existed.
        delete_file(object_name)
        return {"status": "missing"}
    logger.info("Playback rendition (%s) stored for file %s", need.value, file_id)
    return {"status": "created", "need": need.value, "object_name": object_name}
