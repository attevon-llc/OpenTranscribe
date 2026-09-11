"""Stage 2b of the multi-GPU split: diarization only (``gpu-diarize`` queue).

Runs when ``ENGINE_GPU_SPLIT=true``.
"""

import logging

from app.core.celery import celery_app
from app.core.constants import GPUPriority
from app.core.task_cancellation import TranscriptionCancelledError
from app.core.task_cancellation import cancellation_scope
from app.core.task_cancellation import stand_down_if_requested
from app.core.worker_shutdown import TranscriptionAbortedError
from app.db.session_utils import session_scope
from app.transcription.diarizer_native import DiarSidecarUnavailableError
from app.utils import benchmark_timing
from app.utils.task_utils import update_task_status

from .cancellation import finish_cancelled
from .context import TranscriptionContext
from .context import _get_user_friendly_error_message
from .context import _handle_transcription_failure
from .context import _validate_transcription_result
from .context import requeue_after_abort
from .context import retry_on_diar_sidecar_unavailable
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

    # issue #823: bind this run so the cooperative checkpoints inside the engine know WHICH
    # file they are executing. Everything that can raise TranscriptionCancelledError must sit
    # inside this block -- outside it `stand_down_if_requested` has no run to ask about and
    # silently never fires. The context manager's reset is what stops celery's REUSED pool
    # thread from carrying this run's id into the next task.
    with cancellation_scope(task_id, file_uuid):
        try:
            # issue #823: the cheapest place to catch a cancel that landed while this
            # message sat in the broker queue -- one Redis read, before any download,
            # model warm-up or GPU allocation. The engine's own entry checkpoints cover
            # the stage bodies; this covers everything the task does before reaching one.
            stand_down_if_requested("diarize_gpu_task.entry")

            with session_scope() as db:
                update_task_status(db, task_id, "in_progress", progress=0.52)

            send_progress_notification(user_id, file_id, 0.52, "Analyzing speaker patterns")

            transcript = RawTranscriptResult.deserialize(transcript_data)

            if not transcript.raw_segments:
                logger.warning(
                    "Diarize task: no segments from transcription — skipping diarization"
                )
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

        except DiarSidecarUnavailableError as exc:
            # issue #656 Step 5: BEFORE `except Exception` — see the twin comment in
            # `core.py::transcribe_gpu_task` for why the ordering is load-bearing. Deliberately
            # does NOT clean up the shared-volume WAV (unlike the generic handler below): the
            # redelivered attempt needs it, and it is cleaned up on eventual success or on
            # exhaustion of the retry ladder.
            retry_on_diar_sidecar_unavailable(self, exc, file_uuid)
        except TranscriptionCancelledError as cancelled:
            # issue #823: MUST sit before `except Exception` below. Unlike the abort branch it
            # does NOT requeue — the user stopped this file, so handing it to the next worker
            # would resurrect it.
            #
            # ⚠️ It DOES clean up the shared-volume WAV, which the two branches below
            # deliberately do not: they leave it for a redelivered attempt, and after a cancel
            # there is no redelivery to leave it for.
            try:
                from app.transcription.engine.audio_loader import cleanup_shared_volume_wav

                wav_path = getattr(locals().get("transcript"), "local_wav_path", "")
                if wav_path:
                    cleanup_shared_volume_wav(wav_path)
            except Exception as _cleanup_err:  # nosec B110 - cleanup must not mask the outcome
                logger.debug("WAV cleanup on diarize cancel skipped: %s", _cleanup_err)
            return finish_cancelled(ctx, task_id, file_uuid, cancelled, stage="GPU diarization")
        except TranscriptionAbortedError as abort:
            # issue #809: MUST sit before `except Exception` below. An abort is INTERRUPTED work,
            # not broken work — routed through the generic handler it would mark the file ERROR,
            # notify the user, and then `raise`, which under `acks_late=True` ACKS the message and
            # LOSES the diarization entirely. `requeue_after_abort` rejects with requeue=True so the
            # broker redelivers it to the next worker instead.
            #
            # Deliberately does NOT clean up the shared-volume WAV — same reasoning as the sidecar
            # retry branch above: the redelivered attempt needs that WAV, and deleting it here would
            # force a re-download and re-preprocess from MinIO (or fail outright, since Stage 2a has
            # already returned and will not rewrite it).
            requeue_after_abort(file_uuid, abort, stage="GPU diarization")
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
