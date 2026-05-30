"""Async media-download preparation.

Runs ffmpeg-based audio extraction / subtitle embedding on the dedicated
``download`` worker (network + CPU I/O, no GPU), caches the result in object
storage, and hands the browser a presigned URL so the actual transfer streams
straight from MinIO/S3 — never through the API container's memory.

Progress and the final download URL are pushed to the frontend over the
existing ``download_progress`` WebSocket channel.
"""

import logging

from app.core.celery import celery_app
from app.core.constants import DownloadPriority
from app.db.base import SessionLocal
from app.models.media import MediaFile
from app.services.download_events import publish_download_event
from app.services.minio_service import MinIOService
from app.services.video_processing_service import NoAudioTrackError
from app.services.video_processing_service import VideoProcessingService

logger = logging.getLogger(__name__)


@celery_app.task(
    name="download.prepare_media",
    bind=True,
    priority=DownloadPriority.SINGLE_URL,
    max_retries=1,
    retry_backoff=True,
)
def prepare_media_download_task(self, file_id: int, user_id: int, mode: str) -> dict:
    """Prepare a derived download asset and push a presigned URL over SSE.

    Args:
        file_id: MediaFile database ID.
        user_id: Requesting user (kept for context / future authz).
        mode: One of ``audio_mp3``, ``audio_wav``, ``audio_original``, ``video_subtitles``.

    Returns:
        Result dict with ``status`` and (on success) the cache key.
    """
    db = SessionLocal()
    file_uuid = ""
    try:
        db_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if not db_file:
            return {"status": "error", "message": "File not found"}

        file_uuid = str(db_file.uuid)
        filename = str(db_file.filename)
        base_name = filename.rsplit(".", 1)[0] if "." in filename else filename
        storage_path = str(db_file.storage_path)

        publish_download_event(
            file_uuid, status="processing", mode=mode, message="Preparing your download…"
        )

        service = VideoProcessingService(MinIOService())

        if mode == "video_subtitles":
            cache_key = service.process_video_with_subtitles(
                db=db,
                file_id=file_id,
                original_object_name=storage_path,
                user_id=None,  # SSE owns the messaging for this download
                include_speakers=True,
                output_format="mp4",
            )
            content_type = "video/mp4"
            download_filename = f"{base_name}_with_subtitles.mp4"
        elif mode.startswith("audio_"):
            audio_format = mode.split("_", 1)[1]  # mp3 | wav | original
            cache_key, ext, content_type = service.extract_audio(
                db=db,
                file_id=file_id,
                original_object_name=storage_path,
                audio_format=audio_format,
            )
            download_filename = f"{base_name}.{ext}"
        else:
            publish_download_event(
                file_uuid, status="error", mode=mode, message=f"Unsupported mode: {mode}"
            )
            return {"status": "error", "message": f"Unsupported mode {mode}"}

        url = service.presigned_download_url(cache_key, download_filename, content_type)

        publish_download_event(
            file_uuid,
            status="completed",
            mode=mode,
            message="Download ready.",
            url=url,
            filename=download_filename,
        )
        return {"status": "success", "cache_key": cache_key}

    except NoAudioTrackError as e:
        if file_uuid:
            publish_download_event(file_uuid, status="error", mode=mode, message=str(e))
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.error(f"prepare_media_download_task failed for file {file_id} ({mode}): {e}")
        if file_uuid:
            publish_download_event(
                file_uuid, status="error", mode=mode, message="Failed to prepare download."
            )
        return {"status": "error", "message": str(e)}
    finally:
        db.close()
