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


@celery_app.task(
    name="download.prepare_bulk_subtitles",
    bind=True,
    priority=DownloadPriority.PLAYLIST,
    max_retries=1,
    retry_backoff=True,
)
def prepare_bulk_subtitles_task(
    self, file_specs: list, subtitle_format: str, include_speakers: bool, job_id: str
) -> dict:
    """Build a ZIP of subtitles for a batch of files and push a presigned URL over SSE.

    Args:
        file_specs: ``[[file_id, base_filename], ...]`` — already permission-filtered
            by the prepare endpoint (the worker does not re-authorize).
        subtitle_format: ``srt`` | ``webvtt`` | ``txt``.
        include_speakers: Whether to embed speaker labels.
        job_id: Opaque per-request id keying the SSE channel + reconnect result cache.
    """
    import json

    from app.core.redis import get_redis
    from app.services.download_events import publish_bulk_event
    from app.services.minio_service import get_presigned_download_url
    from app.services.subtitle_service import SubtitleService

    db = SessionLocal()
    try:
        publish_bulk_event(job_id, status="processing", message="Building subtitle archive…")
        zip_bytes, exported, skipped = SubtitleService.build_subtitle_archive(
            db,
            [(int(fid), str(name)) for fid, name in file_specs],
            subtitle_format,
            include_speakers,
        )
        if exported == 0:
            publish_bulk_event(
                job_id, status="error", message="No files could be exported.", skipped=skipped
            )
            return {"status": "error", "skipped": skipped}

        fmt = subtitle_format.lower()
        key = f"bulk/{job_id}.zip"
        # Instantiating VideoProcessingService ensures the processed-videos bucket exists.
        service = VideoProcessingService(MinIOService())
        service.minio_service.upload_bytes(service.cache_bucket, key, zip_bytes, "application/zip")
        filename = f"transcripts_{fmt}.zip"
        url = get_presigned_download_url(
            key,
            bucket_name=service.cache_bucket,
            download_filename=filename,
            content_type="application/zip",
        )
        # Reconnect-safe: an SSE client that connects after completion reads this instead
        # of waiting forever on a pub/sub message it already missed.
        get_redis().setex(
            f"bulk_export_result:{job_id}",
            900,
            json.dumps(
                {"url": url, "filename": filename, "exported": exported, "skipped": skipped}
            ),
        )
        publish_bulk_event(
            job_id,
            status="completed",
            message="Export ready.",
            url=url,
            filename=filename,
            exported=exported,
            skipped=skipped,
        )
        return {"status": "success", "exported": exported, "skipped": skipped}
    except Exception as e:
        logger.error(f"prepare_bulk_subtitles_task failed (job {job_id}): {e}")
        publish_bulk_event(job_id, status="error", message="Failed to build export.")
        return {"status": "error", "message": str(e)}
    finally:
        db.close()
