"""GPU transcription task (stage 2 of the 3-stage pipeline chain).

Split out of a single 2901-line module (issue #284, A3.5). This module
stays the import surface every existing caller uses: ``dispatch.py``,
``postprocess.py``, ``rediarize_task.py`` and the Celery ``include=``
list all reference ``app.tasks.transcription.core``, so the symbols they
import are re-exported below.

Where the rest of the pipeline now lives: :mod:`context`,
:mod:`user_settings`, :mod:`pipelines`, :mod:`cloud_asr`,
:mod:`embeddings`, :mod:`finalize`, :mod:`background`, :mod:`downstream`,
:mod:`legacy_task`, :mod:`diarize_task`, :mod:`cpu_task`.
"""

import logging
import os
import tempfile
import time

from celery import chain

from app.core.celery import celery_app
from app.core.constants import CLOUD_ASR_MAX_RETRIES
from app.core.constants import CLOUD_ASR_RETRY_BASE
from app.core.constants import CLOUD_ASR_RETRY_MAX
from app.core.constants import CeleryQueues
from app.core.constants import CPUPriority
from app.core.constants import GPUPriority
from app.core.constants import gpu_split_enabled
from app.core.exceptions import ASRConfigurationError
from app.db.session_utils import get_refreshed_object
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services.asr.errors import ASRRateLimitedError
from app.utils import benchmark_timing
from app.utils.task_utils import update_task_status

from .cloud_asr import _run_cloud_asr_pipeline
from .context import TranscriptionContext
from .context import _get_user_friendly_error_message
from .context import _handle_transcription_failure
from .context import _validate_transcription_result
from .cpu_task import transcribe_cpu_task
from .diarize_task import diarize_gpu_task
from .downstream import trigger_automatic_summarization
from .embeddings import _process_speaker_embeddings
from .embeddings import _process_speaker_embeddings_native
from .embeddings import _should_use_native_embeddings
from .embeddings import _store_native_centroids_in_v4_staging
from .finalize import _process_and_save_critical
from .legacy_task import transcribe_audio_task
from .notifications import send_progress_notification
from .pipelines import _run_engine_pipeline
from .pipelines import _run_transcribe_only_stage
from .pipelines import _run_transcription_pipeline
from .user_settings import _get_user_transcription_settings

logger = logging.getLogger(__name__)

# Re-exported for callers that predate the split. `dispatch.py`,
# `transcription/__init__.py`, `preprocess.py`, `postprocess.py` and
# `rediarize_task.py` all import these from here, and `core.py` is the module
# listed in `celery_app`'s `include=`, so importing it must still register
# every transcription task.
__all__ = [
    "TranscriptionContext",
    "_get_user_transcription_settings",
    "_process_speaker_embeddings",
    "_process_speaker_embeddings_native",
    "_should_use_native_embeddings",
    "_store_native_centroids_in_v4_staging",
    "diarize_gpu_task",
    "transcribe_audio_task",
    "transcribe_cpu_task",
    "transcribe_gpu_task",
    "trigger_automatic_summarization",
]


def _claim_gpu_split_diarize_dispatch(task_id: str) -> bool:
    """Best-effort once-only claim for dispatching the diarize leg of a gpu-split run.

    ``transcribe_gpu_task`` is ``acks_late=True`` (backend/app/tasks/CLAUDE.md): if the
    worker dies after this function's caller has already fired the diarize_gpu_task ->
    finalize_transcription chain but before Celery records the ack, the broker's
    ``visibility_timeout`` redelivers the ENTIRE transcribe_gpu_task to another worker,
    which would otherwise dispatch a second, duplicate diarize chain over the same
    file/segments. The claim key's TTL matches that redelivery window (plus margin) so
    the guard outlives every window in which a duplicate could still occur.

    Fails OPEN (returns True, "go ahead and dispatch") on a Redis error: a lost dedup
    guard risks a rare double-dispatch, but failing closed here would silently drop
    diarization for every gpu-split file whenever Redis hiccups — worse.
    """
    from app.core.redis import get_redis

    ttl_s = int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "21600")) + 3600
    key = f"gpu_split_diarize_dispatched:{task_id}"
    try:
        return bool(get_redis().set(key, "1", nx=True, ex=ttl_s))
    except Exception as e:
        logger.warning(f"gpu-split diarize dispatch claim failed for task {task_id}: {e}")
        return True


def _dispatch_gpu_split_diarize_chain(
    task_id: str, file_uuid: str, transcript_data: dict, preprocess_context: dict
) -> bool:
    """Dispatch the SECOND leg of a gpu-split run: diarize_gpu_task -> finalize_transcription.

    Issue #703-followup: the outer pipeline chain built by dispatch.py is
    ``preprocess -> transcribe_gpu_task -> finalize_transcription``, and that third link is
    UNCONDITIONAL — it always runs against whatever ``transcribe_gpu_task`` returns. A split
    run's real completion depends on ``diarize_gpu_task``, which hasn't executed yet, so the
    outer chain's finalize stage cannot do real work here (it short-circuits on a
    ``status: split_forwarded`` result — see ``postprocess.finalize_transcription``). This
    builds a SEPARATE two-link chain, with its own ``link_error``, so finalize actually runs
    against ``diarize_gpu_task``'s result (segments + speaker mapping), and a genuine failure
    anywhere in this second leg still marks the file ERROR exactly once.

    Guarded by :func:`_claim_gpu_split_diarize_dispatch` so a redelivered
    ``transcribe_gpu_task`` (``acks_late=True``, backend/app/tasks/CLAUDE.md's
    visibility_timeout note) cannot dispatch a second, duplicate diarize chain over the same
    file/segments.

    Returns:
        True if the chain was dispatched, False if skipped as a duplicate.
    """
    if not _claim_gpu_split_diarize_dispatch(task_id):
        logger.warning(
            "gpu-split diarize dispatch already claimed for task %s "
            "(redelivered transcribe_gpu_task) — not dispatching a duplicate diarize_gpu_task",
            task_id,
        )
        return False

    from .dispatch import on_pipeline_error
    from .postprocess import finalize_transcription

    diarize_chain = chain(
        diarize_gpu_task.s(transcript_data, preprocess_context).set(
            queue=CeleryQueues.GPU_DIARIZE, priority=GPUPriority.USER_IMPORT
        ),
        finalize_transcription.s().set(
            queue=CeleryQueues.CPU, priority=CPUPriority.PIPELINE_CRITICAL
        ),
    )
    diarize_chain.apply_async(
        link_error=[on_pipeline_error.si(file_uuid, task_id).set(queue=CeleryQueues.UTILITY)],
    )
    return True


def _resolve_asr_provider_or_none(user_id: int):
    """Resolve the user's ASR provider, or ``None`` on a config-loading failure.

    ``None`` signals the caller to fall through to the local WhisperX pipeline — the
    documented "asymmetric failure" behaviour (errors *constructing* a provider fall back
    to local; errors from ``transcribe()`` itself propagate and fail the file).

    ``ASRConfigurationError`` is different: it is a deliberate refusal to silently resolve
    to a local provider this deployment (e.g. ``DEPLOYMENT_MODE=lite``) cannot run, so it
    is re-raised rather than swallowed — falling through here would still hit the local
    pipeline and fail later with a confusing ``ModuleNotFoundError``.
    """
    from app.services.asr.factory import ASRProviderFactory

    try:
        with session_scope() as db:
            return ASRProviderFactory.create_for_user(user_id, db)
    except ASRConfigurationError:
        raise
    except Exception:
        return None


def _log_shared_wav_fallback_reason(local_wav_path: str | None, file_id: int) -> None:
    """Say WHY the GPU task fell back to a MinIO download instead of the shared-volume WAV.

    Extracted from ``transcribe_gpu_task`` to keep it under the complexity gate, but the
    three-way split is the point: before this, every fallback logged identically, so a
    misconfigured shared volume — a path written by preprocess but not resolvable on the GPU
    worker, which is the ``ENGINE_SHARED_VOLUME_PATH`` mismatch issue #661 E0 fixes — was
    indistinguishable from the two entirely expected reasons. That silence is what let the
    fast path stay off on real installs without anyone noticing.
    """
    if not local_wav_path:
        logger.info(
            "GPU task: no shared-volume WAV recorded by preprocess for file %d — "
            "falling back to MinIO download",
            file_id,
        )
    elif not os.path.exists(local_wav_path):
        logger.warning(
            "GPU task: shared-volume WAV path '%s' recorded by preprocess but NOT FOUND "
            "on this container for file %d — falling back to MinIO download. Check that "
            "ENGINE_SHARED_VOLUME_PATH resolves to the same mounted volume on every "
            "worker (see .env.example).",
            local_wav_path,
            file_id,
        )
    else:
        logger.info(
            "GPU task: shared-volume WAV found but ASR provider is not local for file "
            "%d — falling back to MinIO download",
            file_id,
        )


@celery_app.task(
    bind=True,
    name="transcription.gpu_transcribe",
    priority=GPUPriority.USER_IMPORT,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=1,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=30,
)
def transcribe_gpu_task(self, preprocess_context: dict) -> dict:
    """GPU-only: Whisper transcription + PyAnnote diarization + save to DB.

    Stage 2 of the 3-stage pipeline chain. Receives context from
    preprocess_for_transcription (CPU), runs AI models on GPU, saves
    segments to DB, returns context for finalize_transcription (CPU).
    """
    task_id = preprocess_context["task_id"]
    file_uuid = preprocess_context["file_uuid"]
    file_id = preprocess_context["file_id"]
    user_id = preprocess_context["user_id"]

    # Record GPU received timestamp + pickup markers for inter-stage gap
    # measurement. gpu_task_prerun and gpu_received fire at roughly the same
    # instant; we keep both so the Redis hash schema stays stable across the
    # legacy `gpu_received` tooling and the new `*_task_prerun` convention.
    benchmark_timing.mark(task_id, "gpu_received")
    benchmark_timing.mark(task_id, "gpu_task_prerun")
    benchmark_timing.mark_cold_start(task_id, "gpu")

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

        # ── Check for cloud ASR provider (needed in both code paths) ──────────
        provider = _resolve_asr_provider_or_none(user_id)

        # Read diarization settings (needed in both code paths)
        diarization_source = preprocess_context.get("diarization_source", "provider")
        disable_diarization = diarization_source == "off"
        with session_scope() as db:
            media_file = get_refreshed_object(db, MediaFile, file_id)
            if media_file:
                media_file.diarization_disabled = disable_diarization
                db.commit()

        whisper_model = preprocess_context.get("whisper_model")

        # ── Phase 1b fast path: shared-volume WAV skips MinIO download ────────
        # When the preprocess task wrote a WAV to the shared volume we can feed it
        # directly to Engine.run_gpu_stage() without touching MinIO or creating a
        # temp dir.  Cloud ASR always needs the MinIO download, so we only use
        # this path for local ASR.
        local_wav_path = preprocess_context.get("local_wav_path", "")
        has_shared_wav = (
            bool(local_wav_path)
            and os.path.exists(local_wav_path)
            and (provider is None or provider.provider_name == "local")
        )

        if has_shared_wav:
            logger.info(
                "GPU task: using engine fast path (shared WAV) for file %d — skipping "
                "MinIO download",
                file_id,
            )
            with benchmark_timing.stage(task_id, "gpu_audio_load"):
                pass  # no download — file is already local; mark stage for timing parity

            send_progress_notification(user_id, file_id, 0.25, "Starting AI transcription")

            # ── Phase 4: multi-GPU split path ─────────────────────────────────
            # When ENGINE_GPU_SPLIT=true, transcription-only runs here on the
            # gpu-transcribe queue and the result is forwarded to diarize_gpu_task
            # on the gpu-diarize queue.  The current task returns the serialized
            # RawTranscriptResult so Celery records it; the finalize chain runs
            # after diarize_gpu_task completes.
            if gpu_split_enabled():
                transcript_data = _run_transcribe_only_stage(
                    ctx, local_wav_path, preprocess_context
                )

                # Dispatch the real completion chain — see
                # _dispatch_gpu_split_diarize_chain's docstring (issue #703-followup) for why
                # this can't just be another apply_async of diarize_gpu_task alone.
                _dispatch_gpu_split_diarize_chain(
                    task_id, file_uuid, transcript_data, preprocess_context
                )

                benchmark_timing.mark(task_id, "gpu_end")
                return {
                    "status": "split_forwarded",
                    "file_uuid": file_uuid,
                    "file_id": file_id,
                    "task_id": task_id,
                    "split_stage": "transcribe_only",
                }

            result = _run_engine_pipeline(ctx, local_wav_path, preprocess_context)

            # Annotate result with diarization flags for downstream
            if isinstance(result, dict):
                result["diarization_disabled"] = disable_diarization
                result["diarization_source"] = diarization_source

            # Validate result
            validation_error = _validate_transcription_result(result, ctx, task_id)
            if validation_error:
                return {
                    "status": "error",
                    "file_uuid": file_uuid,
                    "file_id": file_id,
                    "task_id": task_id,
                }

            # Process speakers, save to DB, release GPU
            gpu_result = _process_and_save_critical(ctx, result, preprocess_context)

            # Shared-volume WAV is no longer needed after GPU stage finishes
            from app.transcription.engine.audio_loader import cleanup_shared_volume_wav

            cleanup_shared_volume_wav(local_wav_path)

            benchmark_timing.mark(task_id, "gpu_end")
            if isinstance(result, dict):
                benchmark_timing.set_context(
                    task_id,
                    {
                        "asr_provider": result.get("asr_provider", "local"),
                        "asr_model": result.get("asr_model"),
                    },
                )

            return gpu_result

        # ── Fallback / cloud path: download from MinIO ────────────────────────
        # Reaching here means `has_shared_wav` was False — log WHICH condition failed so a
        # misconfigured shared volume (path written but not found — the ENGINE_SHARED_VOLUME_PATH
        # mismatch issue #661 E0 fixes) is distinguishable from the expected reasons (no local
        # ASR provider, or preprocess never wrote a shared WAV at all). Before this, every
        # fallback logged identically, so a broken shared-volume mount was invisible.
        _log_shared_wav_fallback_reason(local_wav_path, file_id)
        with tempfile.TemporaryDirectory() as temp_dir:
            # Download preprocessed audio from MinIO temp
            step_start = time.perf_counter()
            local_audio_path = os.path.join(temp_dir, "audio.wav")
            with benchmark_timing.stage(task_id, "gpu_audio_load"):
                download_temp_audio(file_uuid, local_audio_path)
            logger.info(
                f"TIMING: audio download from temp completed in "
                f"{time.perf_counter() - step_start:.3f}s"
            )

            send_progress_notification(user_id, file_id, 0.25, "Starting AI transcription")

            if provider is not None and provider.provider_name != "local":
                # Per-task model override is only for local ASR
                if whisper_model:
                    logger.info(
                        "whisper_model override '%s' ignored for cloud ASR provider",
                        whisper_model,
                    )
                result = _run_cloud_asr_pipeline(
                    ctx,
                    local_audio_path,
                    preprocess_context.get("min_speakers"),
                    preprocess_context.get("max_speakers"),
                    preprocess_context.get("num_speakers"),
                    provider=provider,
                    diarization_source=diarization_source,
                )
            else:
                result = _run_transcription_pipeline(
                    ctx,
                    local_audio_path,
                    preprocess_context.get("min_speakers"),
                    preprocess_context.get("max_speakers"),
                    preprocess_context.get("num_speakers"),
                    source_language=preprocess_context.get("source_language"),
                    translate_to_english=preprocess_context.get("translate_to_english"),
                    disable_diarization=disable_diarization,
                    whisper_model=whisper_model,
                )

            # Annotate result with diarization flags for downstream
            if isinstance(result, dict):
                result["diarization_disabled"] = disable_diarization
                result["diarization_source"] = diarization_source

            # Validate result
            validation_error = _validate_transcription_result(result, ctx, task_id)
            if validation_error:
                return {
                    "status": "error",
                    "file_uuid": file_uuid,
                    "file_id": file_id,
                    "task_id": task_id,
                }

            # Process speakers, save to DB, release GPU
            gpu_result = _process_and_save_critical(ctx, result, preprocess_context)

            # Record GPU end timestamp for inter-stage gap measurement.
            # Persist ASR provider + model context for the timing table.
            benchmark_timing.mark(task_id, "gpu_end")
            if isinstance(result, dict):
                benchmark_timing.set_context(
                    task_id,
                    {
                        "asr_provider": result.get("asr_provider", "local"),
                        "asr_model": result.get("asr_model"),
                    },
                )

            return gpu_result

    except ASRRateLimitedError as exc:
        # Vendor throttle/quota response — retry with the vendor's own Retry-After when
        # given, else a bounded exponential backoff. Deliberately a SEPARATE policy from
        # the task's autoretry_for=(ConnectionError, TimeoutError): that one's
        # retry_backoff_max=30/max_retries=1 are tuned for a GPU-path connection blip, far
        # too short for a vendor throttle, and autoretry_for cannot honor Retry-After.
        # self.retry() raises celery.exceptions.Retry, which Celery's task machinery
        # handles as a scheduled retry (not a task failure) — dispatch.py's on_pipeline_error
        # link_error callback does not fire for it, so the file is not marked FAILED here.
        countdown = exc.retry_after or min(
            CLOUD_ASR_RETRY_BASE * 2**self.request.retries, CLOUD_ASR_RETRY_MAX
        )
        logger.warning(
            "Cloud ASR rate-limited for file %s (attempt %d/%d), retrying in %.0fs: %s",
            file_uuid,
            self.request.retries + 1,
            CLOUD_ASR_MAX_RETRIES,
            countdown,
            exc,
        )
        raise self.retry(exc=exc, countdown=countdown, max_retries=CLOUD_ASR_MAX_RETRIES) from exc
    except Exception as e:
        # Best-effort cleanup of shared-volume WAV on failure
        _wav = locals().get("local_wav_path", "")
        if _wav:
            try:
                from app.transcription.engine.audio_loader import cleanup_shared_volume_wav

                cleanup_shared_volume_wav(_wav)
            except Exception as _cleanup_err:  # nosec B110
                logger.debug("WAV cleanup on error skipped: %s", _cleanup_err)
        logger.error(f"GPU transcription failed for file {file_uuid}: {e}")
        error_message = _get_user_friendly_error_message(str(e))
        _handle_transcription_failure(ctx, task_id, error_message, "gpu_processing_error")
        raise
