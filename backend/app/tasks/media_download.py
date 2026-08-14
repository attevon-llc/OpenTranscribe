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
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services.download_events import publish_download_event
from app.services.download_events import release_download_prep_guard
from app.services.minio_service import MinIOService
from app.services.redaction.export_policy import ExportRedactionNotReadyError
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
def prepare_media_download_task(
    self, file_id: int, user_id: int, mode: str, variant: str = ""
) -> dict:
    """Prepare a derived download asset and push a presigned URL over SSE.

    Args:
        file_id: MediaFile database ID.
        user_id: Requesting user. **Load-bearing since #85**: for ``video_subtitles``
            it is the subject whose redaction policy masks the burned-in text.
        mode: One of ``audio_mp3``, ``audio_wav``, ``audio_original``, ``video_subtitles``.
        variant: Opaque routing token from the dispatcher (the redaction fingerprint at
            click time, ``""`` when nothing masks). It scopes the Redis dedup guard and
            the SSE events so two readers with different policies do not collapse onto
            one build and receive each other's artifact. It is **never** a masking
            input — the policy itself is re-resolved below, at run time.

    Returns:
        Result dict with ``status`` and (on success) the cache key.
    """
    file_uuid = ""
    try:
        # Phase 1 — read (short, DB only). Plain scalars, no ORM instance
        # escapes: the scope closes before any MinIO/ffmpeg work starts.
        with session_scope() as db:
            row = (
                db.query(MediaFile.uuid, MediaFile.filename, MediaFile.storage_path)
                .filter(MediaFile.id == file_id)
                .first()
            )
            if not row:
                return {"status": "error", "message": "File not found"}
            file_uuid = str(row[0])
            filename = str(row[1])
            storage_path = str(row[2])

        base_name = filename.rsplit(".", 1)[0] if "." in filename else filename

        publish_download_event(
            file_uuid,
            status="processing",
            mode=mode,
            variant=variant,
            message="Preparing your download…",
        )

        service = VideoProcessingService(MinIOService())

        if mode not in ("video_subtitles",) and not mode.startswith("audio_"):
            publish_download_event(
                file_uuid,
                status="error",
                mode=mode,
                variant=variant,
                message=f"Unsupported mode: {mode}",
            )
            return {"status": "error", "message": f"Unsupported mode {mode}"}

        # Phase 2 — MinIO download + ffmpeg transcode. NO DB session is held.
        #
        # This used to be wrapped in a ``session_scope`` because
        # ``VideoProcessingService`` took a ``Session`` and kept it open across
        # the whole transcode. It now opens its own short sessions for the two
        # reads it needs (the filename, and the transcript for the SRT), so
        # nothing here holds a transaction.
        if mode == "video_subtitles":
            cache_key = service.process_video_with_subtitles(
                file_id=file_id,
                original_object_name=storage_path,
                user_id=None,  # SSE owns the messaging for this download
                include_speakers=True,
                output_format="mp4",
                redaction_user_id=user_id,
            )
            content_type = "video/mp4"
            download_filename = f"{base_name}_with_subtitles.mp4"
        else:
            audio_format = mode.split("_", 1)[1]  # mp3 | wav | original
            cache_key, ext, content_type = service.extract_audio(
                file_id=file_id,
                original_object_name=storage_path,
                audio_format=audio_format,
            )
            download_filename = f"{base_name}.{ext}"

        url = service.presigned_download_url(cache_key, download_filename, content_type)

        publish_download_event(
            file_uuid,
            status="completed",
            mode=mode,
            variant=variant,
            message="Download ready.",
            url=url,
            filename=download_filename,
        )
        return {"status": "success", "cache_key": cache_key}

    except ExportRedactionNotReadyError as e:
        # The reader's policy masks this file and its scan has produced no spans yet.
        # Burning the raw transcript into a video is unrecoverable, so nothing is
        # produced; the message mirrors the single-file export's 409.
        logger.info(f"Withholding {mode} for file {file_id}: {e}")
        if file_uuid:
            publish_download_event(
                file_uuid, status="error", mode=mode, variant=variant, message=str(e)
            )
        return {"status": "error", "message": str(e)}
    except NoAudioTrackError as e:
        if file_uuid:
            publish_download_event(
                file_uuid, status="error", mode=mode, variant=variant, message=str(e)
            )
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.error(f"prepare_media_download_task failed for file {file_id} ({mode}): {e}")
        if file_uuid:
            publish_download_event(
                file_uuid,
                status="error",
                mode=mode,
                variant=variant,
                message="Failed to prepare download.",
            )
        return {"status": "error", "message": str(e)}
    finally:
        # Release the dispatch guard on EVERY path. Its 900 s expiry is only a
        # backstop: while the guard outlived the task, a download whose readiness
        # could not be resolved was unrecoverable for 15 minutes -- NX refused to
        # re-dispatch, so the SSE stream waited on an event nobody would publish.
        release_download_prep_guard(file_id, mode, variant)


@celery_app.task(
    name="download.prepare_bulk_subtitles",
    bind=True,
    priority=DownloadPriority.PLAYLIST,
    max_retries=1,
    retry_backoff=True,
)
def prepare_bulk_subtitles_task(
    self,
    file_specs: list,
    subtitle_format: str,
    include_speakers: bool,
    job_id: str,
    user_id: int | None = None,
) -> dict:
    """Build a ZIP of subtitles for a batch of files and push a presigned URL over SSE.

    The archive is masked with the **requesting user's** effective redaction policy,
    resolved here rather than at dispatch so an admin who tightens the floor between
    the click and the build is obeyed. ``services/redaction/export_policy.py`` carries
    both decisions and what the alternatives would have leaked (issue #85).

    Args:
        file_specs: ``[[file_id, base_filename], ...]`` — already permission-filtered
            by the prepare endpoint (the worker does not re-authorize).
        subtitle_format: ``srt`` | ``webvtt`` | ``txt``.
        include_speakers: Whether to embed speaker labels.
        job_id: Opaque per-request id keying the SSE channel + reconnect result cache.
        user_id: Whose redaction policy masks the archive. Optional **only** so a
            message queued by a pre-#85 API container cannot crash a new worker with a
            ``TypeError``; ``None`` refuses the export rather than exporting raw.
    """
    import json

    from app.core.redis import get_redis
    from app.services.download_events import publish_bulk_event
    from app.services.minio_service import get_presigned_download_url
    from app.services.redaction.config import resolve_effective_config
    from app.services.subtitle_service import SubtitleService

    if user_id is None:
        # FAIL CLOSED. No subject means no resolvable policy, and an export is
        # unrecoverable once it reaches the browser.
        logger.error(f"Bulk export job {job_id} has no requesting user; refusing to build it")
        publish_bulk_event(job_id, status="error", message="Failed to build export.")
        return {"status": "error", "message": "No requesting user for the export"}

    try:
        # session_scope auto-commits/rolls-back/closes; the archive build is read-only.
        with session_scope() as db:
            publish_bulk_event(job_id, status="processing", message="Building subtitle archive…")
            redaction_cfg = resolve_effective_config(db, user_id)
            zip_bytes, exported, skipped = SubtitleService.build_subtitle_archive(
                db,
                [(int(fid), str(name)) for fid, name in file_specs],
                subtitle_format,
                include_speakers,
                redaction_cfg,
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
