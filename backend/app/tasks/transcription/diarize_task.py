"""Stage 2b of the multi-GPU split: diarization only (``gpu-diarize`` queue).

Runs when ``ENGINE_GPU_SPLIT=true``.
"""

import logging

from app.core.celery import celery_app
from app.core.constants import GPUPriority
from app.db.session_utils import session_scope
from app.utils import benchmark_timing
from app.utils.task_utils import update_task_status

from .context import TranscriptionContext
from .context import _get_user_friendly_error_message
from .context import _handle_transcription_failure
from .context import _validate_transcription_result
from .finalize import _process_and_save_critical
from .notifications import send_progress_notification

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="transcription.diarize_gpu",
    priority=GPUPriority.USER_IMPORT,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=1,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=30,
    queue="gpu-diarize",
)
def diarize_gpu_task(self, transcript_data: dict, preprocess_context: dict) -> dict:
    """Stage 2b (GPU-diarize): diarization only for the Phase 4 multi-GPU split.

    Receives a serialized RawTranscriptResult from transcribe_gpu_task,
    runs PyAnnote diarization, then falls through to the identical
    finalize / save / notification chain used by transcribe_gpu_task.

    Args:
        transcript_data: Serialized RawTranscriptResult from Stage 2a.
        preprocess_context: Original preprocess dict forwarded by Stage 2a,
            used for file metadata and downstream task wiring.
    """
    from app.transcription import Engine
    from app.transcription import EngineConfig
    from app.transcription.engine.job import RawTranscriptResult

    task_id = preprocess_context["task_id"]
    file_uuid = preprocess_context["file_uuid"]
    file_id = preprocess_context["file_id"]
    user_id = preprocess_context["user_id"]

    benchmark_timing.mark(task_id, "diarize_received")
    benchmark_timing.mark(task_id, "diarize_task_prerun")

    diarization_source = preprocess_context.get("diarization_source", "provider")
    disable_diarization = diarization_source == "off"

    ctx = TranscriptionContext(
        task_id=task_id,
        file_id=file_id,
        file_uuid=file_uuid,
        user_id=user_id,
        file_path=preprocess_context["storage_path"],
        file_name=preprocess_context["file_name"],
        content_type=preprocess_context["content_type"],
    )

    try:
        with session_scope() as db:
            update_task_status(db, task_id, "in_progress", progress=0.52)

        send_progress_notification(user_id, file_id, 0.52, "Analyzing speaker patterns")

        transcript = RawTranscriptResult.deserialize(transcript_data)

        if not transcript.raw_segments:
            logger.warning("Diarize task: no segments from transcription — skipping diarization")
            error_msg = (
                "No audio content could be detected in this file. "
                "The file may be corrupted, contain only silence, or be in an unsupported format."
            )
            return _handle_transcription_failure(ctx, task_id, error_msg, "no_valid_audio")

        engine_config = EngineConfig.from_snapshot(transcript.config_snapshot)
        engine = Engine(engine_config)

        def _progress(progress: float, message: str) -> None:
            with session_scope() as db:
                update_task_status(db, ctx.task_id, "in_progress", progress=progress)
            send_progress_notification(ctx.user_id, ctx.file_id, progress, message)

        raw = engine.run_diarize_only(transcript, progress_callback=_progress)
        job_result = engine.run_cpu_finalize(raw)

        # Shared-volume WAV has been read by both Stage 2a and 2b — clean up now
        from app.transcription.engine.audio_loader import cleanup_shared_volume_wav

        cleanup_shared_volume_wav(transcript.local_wav_path)

        result = job_result.to_pipeline_dict()
        result.setdefault("asr_provider", "local")
        result.setdefault("asr_model", engine_config.transcription_config.model_name)
        result["diarization_disabled"] = disable_diarization
        result["diarization_source"] = diarization_source

        validation_error = _validate_transcription_result(result, ctx, task_id)
        if validation_error:
            return {
                "status": "error",
                "file_uuid": file_uuid,
                "file_id": file_id,
                "task_id": task_id,
            }

        gpu_result = _process_and_save_critical(ctx, result, preprocess_context)

        benchmark_timing.mark(task_id, "gpu_end")
        benchmark_timing.set_context(
            task_id,
            {
                "asr_provider": "local",
                "asr_model": engine_config.transcription_config.model_name,
            },
        )

        return gpu_result

    except Exception as e:
        logger.error(f"Diarize GPU task failed for file {file_uuid}: {e}")
        # Best-effort cleanup on failure — WAV is no longer needed
        try:
            if "transcript" in dir() and hasattr(transcript, "local_wav_path"):
                from app.transcription.engine.audio_loader import cleanup_shared_volume_wav

                cleanup_shared_volume_wav(transcript.local_wav_path)
        except Exception as _cleanup_err:  # nosec B110
            logger.debug("WAV cleanup on diarize error skipped: %s", _cleanup_err)
        error_message = _get_user_friendly_error_message(str(e))
        _handle_transcription_failure(ctx, task_id, error_message, "gpu_processing_error")
        raise
