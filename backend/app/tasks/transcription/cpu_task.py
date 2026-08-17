"""Stage 2 alternative: lightweight Whisper transcription on CPU.

Diarization is skipped (PyAnnote requires CUDA).
"""

import logging
import os
import tempfile

from app.core.celery import celery_app
from app.core.config import settings
from app.db.session_utils import get_refreshed_object
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.transcription.config import LIGHTWEIGHT_MODELS
from app.utils import benchmark_timing
from app.utils.task_utils import update_task_status

from .context import TranscriptionContext
from .context import _get_user_friendly_error_message
from .context import _handle_transcription_failure
from .context import _validate_transcription_result
from .finalize import _process_and_save_critical
from .notifications import send_progress_notification
from .pipelines import _resolve_language_settings
from .user_settings import _get_user_transcription_settings

logger = logging.getLogger(__name__)


def _run_cpu_transcription(
    ctx: TranscriptionContext,
    audio_file_path: str,
    source_language: str | None = None,
    translate_to_english: bool | None = None,
    whisper_model: str | None = None,
) -> dict:
    """Run lightweight Whisper transcription on CPU."""
    from app.transcription import TranscriptionConfig
    from app.transcription import TranscriptionPipeline

    source_language, translate_to_english = _resolve_language_settings(
        ctx, source_language, translate_to_english
    )

    # Get user's transcription tuning settings
    with session_scope() as db:
        user_settings = _get_user_transcription_settings(db, ctx.user_id)

    overrides = dict(
        source_language=source_language,
        translate_to_english=translate_to_english,
        min_speakers=1,
        max_speakers=1,
        hf_token=settings.HUGGINGFACE_TOKEN,
        vad_threshold=user_settings["vad_threshold"],
        vad_min_silence_ms=user_settings["vad_min_silence_ms"],
        vad_min_speech_ms=user_settings["vad_min_speech_ms"],
        vad_speech_pad_ms=user_settings["vad_speech_pad_ms"],
        hallucination_silence_threshold=user_settings["hallucination_silence_threshold"],
        repetition_penalty=user_settings["repetition_penalty"],
    )

    if whisper_model and whisper_model in LIGHTWEIGHT_MODELS:
        overrides["model_name"] = whisper_model

    config = TranscriptionConfig.for_cpu_lightweight(**overrides)

    with session_scope() as db:
        update_task_status(db, ctx.task_id, "in_progress", progress=0.4)

    send_progress_notification(ctx.user_id, ctx.file_id, 0.4, "Running fast CPU transcription")

    def progress_callback(progress, message):
        with session_scope() as db:
            update_task_status(db, ctx.task_id, "in_progress", progress=progress)
        send_progress_notification(ctx.user_id, ctx.file_id, progress, message)

    pipeline = TranscriptionPipeline(config)
    raw_result = pipeline.process(
        audio_file_path, progress_callback=progress_callback, task_id=ctx.task_id
    )

    if isinstance(raw_result, dict):
        raw_result.setdefault("asr_provider", "local")
        raw_result.setdefault("asr_model", config.model_name)
        raw_result["diarization_disabled"] = True

    return raw_result


@celery_app.task(
    bind=True,
    name="transcription.cpu_transcribe",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=1,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=30,
)
def transcribe_cpu_task(self, preprocess_context: dict) -> dict:
    """CPU-only: Lightweight Whisper transcription (base/tiny models).

    Stage 2 alternative for the 3-stage pipeline chain. Runs on the CPU worker
    instead of GPU, using a small Whisper model with int8 quantization.
    Diarization is skipped (PyAnnote requires CUDA).
    """
    task_id = preprocess_context["task_id"]
    file_uuid = preprocess_context["file_uuid"]
    file_id = preprocess_context["file_id"]
    user_id = preprocess_context["user_id"]

    benchmark_timing.mark(task_id, "gpu_received")  # re-use key for CPU fast path
    benchmark_timing.mark(task_id, "gpu_task_prerun")
    benchmark_timing.mark_cold_start(task_id, "cpu_transcribe")

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
        from app.services.minio_service import download_temp_audio

        with session_scope() as db:
            update_task_status(db, task_id, "in_progress", progress=0.22)

        with tempfile.TemporaryDirectory() as temp_dir:
            # Download preprocessed audio from MinIO temp
            local_audio_path = os.path.join(temp_dir, "audio.wav")
            with benchmark_timing.stage(task_id, "gpu_audio_load"):
                download_temp_audio(file_uuid, local_audio_path)

            send_progress_notification(user_id, file_id, 0.25, "Starting fast CPU transcription")

            # Persist diarization_disabled=True for CPU path
            with session_scope() as db:
                media_file = get_refreshed_object(db, MediaFile, file_id)
                if media_file:
                    media_file.diarization_disabled = True
                    db.commit()

            whisper_model = preprocess_context.get("whisper_model")

            result = _run_cpu_transcription(
                ctx,
                local_audio_path,
                source_language=preprocess_context.get("source_language"),
                translate_to_english=preprocess_context.get("translate_to_english"),
                whisper_model=whisper_model,
            )

            # Validate result
            validation_error = _validate_transcription_result(result, ctx, task_id)
            if validation_error:
                return {
                    "status": "error",
                    "file_uuid": file_uuid,
                    "file_id": file_id,
                    "task_id": task_id,
                }

            # Process speakers, save to DB (same as GPU path)
            cpu_result = _process_and_save_critical(ctx, result, preprocess_context)
            benchmark_timing.mark(task_id, "gpu_end")
            return cpu_result

    except (ConnectionError, TimeoutError):
        # Let autoretry_for handle these silently ON A RETRYABLE ATTEMPT — running
        # the failure side effects here (ERROR status, user notification,
        # quota-release) would fire BEFORE Celery's retry wrapper ever sees the
        # exception, producing a false failure notification for a transient blip
        # the task is about to retry. But once retries are exhausted this IS the
        # final failure — Celery will not attempt again — so it must still be
        # reported like any other terminal error.
        if self.request.retries < self.max_retries:
            raise
        logger.error(f"CPU transcription failed for file {file_uuid} after all retries")
        error_message = _get_user_friendly_error_message("Connection or timeout error")
        _handle_transcription_failure(ctx, task_id, error_message, "cpu_processing_error")
        raise
    except Exception as e:
        logger.error(f"CPU transcription failed for file {file_uuid}: {e}")
        error_message = _get_user_friendly_error_message(str(e))
        _handle_transcription_failure(ctx, task_id, error_message, "cpu_processing_error")
        raise
