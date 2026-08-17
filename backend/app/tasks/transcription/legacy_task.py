"""Legacy monolithic transcription task (``transcription.process_file``).

Superseded by the 3-stage preprocess -> GPU -> postprocess chain; kept
registered because the task name is still routable.
"""

import logging
import os
import tempfile

from app.core.celery import celery_app
from app.core.constants import GPUPriority
from app.db.session_utils import get_refreshed_object
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services.minio_service import download_file
from app.utils.task_utils import create_task_record
from app.utils.task_utils import update_task_status

from .audio_processor import get_audio_file_extension
from .audio_processor import prepare_audio_for_transcription
from .cloud_asr import _run_cloud_asr_pipeline
from .context import TranscriptionContext
from .context import _get_media_file_context
from .context import _get_user_friendly_error_message
from .context import _handle_outer_exception
from .context import _handle_transcription_failure
from .context import _validate_transcription_result
from .finalize import _process_transcription_result
from .metadata_extractor import extract_media_metadata
from .metadata_extractor import update_media_file_metadata
from .notifications import send_processing_notification
from .notifications import send_progress_notification
from .pipelines import _run_transcription_pipeline

logger = logging.getLogger(__name__)


def _extract_metadata_if_available(temp_file_path: str, ctx: TranscriptionContext) -> None:
    """Extract and save media metadata from file."""
    extracted_metadata = extract_media_metadata(temp_file_path)
    if not extracted_metadata:
        return

    with session_scope() as db:
        media_file = get_refreshed_object(db, MediaFile, ctx.file_id)
        if media_file:
            update_media_file_metadata(
                media_file, extracted_metadata, ctx.content_type, temp_file_path
            )
            db.commit()


def _download_and_extract_metadata(
    storage_path: str, temp_file_path: str, ctx: TranscriptionContext
) -> None:
    """Download file from MinIO and extract metadata (for video presigned URL path)."""
    try:
        from app.services.minio_service import download_file_to_path

        download_file_to_path(storage_path, temp_file_path)
        _extract_metadata_if_available(temp_file_path, ctx)
    except Exception as e:
        logger.warning(f"Metadata extraction failed for file {ctx.file_id}: {e}")


def _process_file_in_temp_dir(
    ctx: TranscriptionContext,
    temp_dir: str,
    file_data,
    file_ext: str,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
    downstream_tasks: list[str] | None = None,
    minio_url: str | None = None,
) -> dict:
    """Process the transcription pipeline within a temporary directory."""
    import threading

    with session_scope() as db:
        update_task_status(db, ctx.task_id, "in_progress", progress=0.25)

    # Create progress callback for audio extraction phase (25% to 38% of overall progress)
    def audio_extraction_progress_callback(stage_progress: float, message: str) -> None:
        overall_progress = 0.25 + (stage_progress * 0.13)
        send_progress_notification(ctx.user_id, ctx.file_id, overall_progress, message)

    if minio_url:
        # Video file: FFmpeg reads directly from MinIO presigned URL
        # No need to download full video — just extract the audio stream
        send_progress_notification(ctx.user_id, ctx.file_id, 0.25, "Extracting audio from video")
        temp_audio_path = os.path.join(temp_dir, "audio.wav")
        from .audio_processor import extract_audio_from_video

        extract_audio_from_video(minio_url, temp_audio_path, audio_extraction_progress_callback)
        audio_file_path = temp_audio_path

        # Download file in background for metadata extraction only
        temp_file_path = os.path.join(temp_dir, f"input{file_ext}")
        metadata_thread = threading.Thread(
            target=_download_and_extract_metadata,
            args=(ctx.file_path, temp_file_path, ctx),
            name=f"metadata-{ctx.file_id}",
            daemon=True,
        )
        metadata_thread.start()
    else:
        # Audio file: use downloaded data
        temp_file_path = os.path.join(temp_dir, f"input{file_ext}")
        with open(temp_file_path, "wb") as f:
            f.write(file_data.read())

        # Run metadata extraction in background thread
        metadata_thread = threading.Thread(
            target=_extract_metadata_if_available,
            args=(temp_file_path, ctx),
            name=f"metadata-{ctx.file_id}",
            daemon=True,
        )
        metadata_thread.start()

        send_progress_notification(ctx.user_id, ctx.file_id, 0.25, "Starting audio preparation")
        audio_file_path = prepare_audio_for_transcription(
            temp_file_path,
            ctx.content_type,
            temp_dir,
            progress_callback=audio_extraction_progress_callback,
        )

    # Ensure metadata extraction completed (typically finishes well before audio extraction)
    metadata_thread.join(timeout=30)

    # Check whether the user has a cloud ASR provider configured.
    # If so, route to the cloud pipeline; otherwise fall through to the local WhisperX pipeline.
    # The provider instance is passed directly into _run_cloud_asr_pipeline to avoid a
    # redundant DB round-trip (factory.create_for_user hits the DB every call).
    #
    # Only fall back to local for factory/config-loading errors (e.g. DB connectivity or
    # missing env vars at startup).  Errors from the provider's transcribe() call itself
    # (network failures, quota exceeded, bad API key) are re-raised so the outer handler
    # marks the file as FAILED with a clear error message rather than silently re-running
    # on local GPU (which can be very confusing in production).
    try:
        from app.services.asr.factory import ASRProviderFactory

        with session_scope() as db:
            provider = ASRProviderFactory.create_for_user(ctx.user_id, db)
    except Exception as factory_exc:
        logger.warning(
            "Failed to instantiate ASR provider for file %d (user %d), "
            "falling back to local pipeline: %s",
            ctx.file_id,
            ctx.user_id,
            factory_exc,
        )
        provider = None  # type: ignore[assignment]

    if provider is not None and provider.provider_name != "local":
        # Resolve diarization_source from the user's setting, same as the modern
        # preprocess.py path — without this it silently defaulted to "provider"
        # regardless of what the user configured (e.g. "off" or "pyannote").
        from .core import _get_user_transcription_settings

        with session_scope() as db:
            user_ts = _get_user_transcription_settings(db, ctx.user_id)
            diarization_source = user_ts.get("diarization_source", "provider")

        # Cloud pipeline — errors propagate so the task is marked FAILED, not silently
        # re-attempted on local GPU.
        result = _run_cloud_asr_pipeline(
            ctx,
            audio_file_path,
            min_speakers,
            max_speakers,
            num_speakers,
            provider=provider,
            diarization_source=diarization_source,
        )
        # Validate transcription result
        validation_error = _validate_transcription_result(result, ctx, ctx.task_id)
        if validation_error:
            return validation_error
        return _process_transcription_result(ctx, result, audio_file_path, downstream_tasks)

    # Local pipeline
    result = _run_transcription_pipeline(
        ctx, audio_file_path, min_speakers, max_speakers, num_speakers
    )

    # Validate transcription result
    validation_error = _validate_transcription_result(result, ctx, ctx.task_id)
    if validation_error:
        return validation_error

    # Process successful result
    return _process_transcription_result(ctx, result, audio_file_path, downstream_tasks)


@celery_app.task(bind=True, name="transcription.process_file", priority=GPUPriority.USER_IMPORT)
def transcribe_audio_task(
    self,
    file_uuid: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    downstream_tasks: list[str] | None = None,
):
    """Process an audio/video file for transcription and speaker diarization.

    Uses the unified faster-whisper + PyAnnote v4 pipeline with
    user-configurable VAD and accuracy settings.

    Args:
        file_uuid: UUID of the MediaFile to transcribe.
        min_speakers: Minimum speakers for diarization (falls back to settings).
        max_speakers: Maximum speakers for diarization (falls back to settings).
        num_speakers: Fixed speaker count for diarization (falls back to settings).
        downstream_tasks: Optional list of specific post-transcription stages to run.
            None = run all tasks. Valid values: 'analytics', 'speaker_llm',
            'summarization', 'topic_extraction', 'search_indexing'.
    """
    task_id = self.request.id
    ctx = None

    try:
        # Get file information and create context
        ctx = _get_media_file_context(file_uuid, task_id)
        if not ctx:
            return {"status": "error", "message": f"Media file with UUID {file_uuid} not found"}

        # Send processing notification
        send_processing_notification(ctx.user_id, ctx.file_id)

        # Create and initialize task record
        with session_scope() as db:
            create_task_record(db, task_id, ctx.user_id, ctx.file_id, "transcription")
            update_task_status(db, task_id, "in_progress", progress=0.1)

        file_ext = get_audio_file_extension(ctx.content_type, ctx.file_name)
        is_video = ctx.content_type.startswith("video/")

        # For video files: stream audio directly from MinIO via presigned URL
        # (avoids downloading full video through Python — FFmpeg reads MinIO directly)
        # For audio files: download normally (small, fast)
        if is_video:
            from app.services.minio_service import get_file_url

            logger.info(f"Generating presigned URL for direct FFmpeg access: {ctx.file_path}")
            minio_url = get_file_url(ctx.file_path, expires=3600)
            file_data = None
        else:
            logger.info(f"Downloading audio file {ctx.file_path}")
            file_data, _, _ = download_file(ctx.file_path)
            minio_url = None

        # Process in temporary directory
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                return _process_file_in_temp_dir(
                    ctx,
                    temp_dir,
                    file_data,
                    file_ext,
                    min_speakers,
                    max_speakers,
                    num_speakers,
                    downstream_tasks,
                    minio_url=minio_url,
                )
        except PermissionError as e:
            logger.error(f"PyAnnote model access error: {str(e)}")
            return _handle_transcription_failure(ctx, task_id, str(e), "gated_model_access")
        except Exception as e:
            logger.error(f"Error in transcription processing: {str(e)}")
            error_message = _get_user_friendly_error_message(str(e))
            return _handle_transcription_failure(ctx, task_id, error_message, "processing_error")

    except Exception as e:
        return _handle_outer_exception(ctx, task_id, e)
