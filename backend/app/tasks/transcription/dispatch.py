"""Transcription pipeline dispatch — Celery chain orchestration.

Builds a 3-stage chain for maximum GPU utilization:
  CPU preprocess → GPU transcribe+diarize → CPU postprocess

For batch processing (1000+ files), dispatch individual chains per file.
The CPU queue (concurrency=8) preprocesses multiple files ahead, ensuring
the GPU always has work ready. The GPU processes one file at a time while
the next file's audio is already prepared.

    File 1:  [CPU preprocess] → [GPU transcribe+diarize] → [CPU postprocess]
    File 2:       [CPU preprocess] → [GPU transcribe+diarize] → ...
    File 3:            [CPU preprocess] → ...
"""

import contextlib
import json
import logging
import uuid

from celery import chain
from celery import group

from app.core.celery import celery_app
from app.core.constants import CeleryQueues
from app.core.constants import CPUPriority
from app.core.constants import GPUPriority
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.transcription.config import LIGHTWEIGHT_MODELS
from app.utils.task_utils import create_task_record
from app.utils.task_utils import update_media_file_status
from app.utils.task_utils import update_task_status

logger = logging.getLogger(__name__)


def _resolve_gpu_queue(user_id: int, db) -> str:
    """Resolve the correct queue based on the user's active ASR provider.

    Returns 'cloud-asr' for cloud providers, 'gpu' for local.
    """
    try:
        from app.services.asr.factory import ASRProviderFactory

        provider = ASRProviderFactory.create_for_user(user_id, db)
        if provider.provider_name != "local":
            logger.info(
                f"Resolved ASR queue 'cloud-asr' for user {user_id} "
                f"(provider: {provider.provider_name})"
            )
            return CeleryQueues.CLOUD_ASR
    except Exception as e:
        logger.debug(f"ASR provider resolution failed, defaulting to 'gpu': {e}")
    return CeleryQueues.GPU


def dispatch_transcription_pipeline(
    file_uuid: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    downstream_tasks: list[str] | None = None,
    source_language: str | None = None,
    translate_to_english: bool | None = None,
    gpu_queue: str | None = None,
    disable_diarization: bool | None = None,
    diarization_source: str | None = None,
    whisper_model: str | None = None,
    task_id: str | None = None,
) -> str:
    """Build and dispatch a 3-stage transcription chain.

    Returns the application-level task_id used for frontend progress tracking.
    The chain pipelines CPU and GPU work: while the GPU processes file N,
    the CPU queue preprocesses file N+1.

    Args:
        file_uuid: UUID of the MediaFile to transcribe.
        min_speakers: Min speakers for diarization (falls back to settings).
        max_speakers: Max speakers for diarization (falls back to settings).
        num_speakers: Fixed speaker count (falls back to settings).
        downstream_tasks: Optional list of post-transcription stages to run.
        source_language: Override source language (None = auto-detect).
        translate_to_english: Override translation setting.
        gpu_queue: Queue for GPU task. None = auto-resolve from user's ASR
            provider ('gpu' for local, 'cloud-asr' for cloud providers).
        whisper_model: Optional per-task Whisper model override (local ASR only).
        task_id: Optional pre-generated application task_id. When provided
            (e.g., from the HTTP upload handler) it is reused so HTTP-phase
            benchmark markers share the ``benchmark:{task_id}`` Redis hash
            with the pipeline markers. When None, a fresh UUID is generated.
    """
    from .core import transcribe_cpu_task
    from .core import transcribe_gpu_task
    from .postprocess import finalize_transcription
    from .preprocess import preprocess_for_transcription

    if not task_id:
        task_id = str(uuid.uuid4())
    use_cpu = whisper_model in LIGHTWEIGHT_MODELS

    # Create task record and set file to PROCESSING
    with session_scope() as db:
        media_file = db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
        if not media_file:
            raise ValueError(f"Media file {file_uuid} not found")

        file_id = int(media_file.id)
        user_id = int(media_file.user_id)

        # Cloud-edition seam: quota reservation hook (no-op in community).
        # QuotaExceededError (HTTP 402) propagates BEFORE the task record is
        # created, so a blocked job leaves no trace and nothing dispatches.
        from decimal import Decimal

        from .hooks import DispatchContext
        from .hooks import fire_before_dispatch

        duration_s = media_file.duration
        fire_before_dispatch(
            DispatchContext(
                file_id=file_id,
                file_uuid=file_uuid,
                user_id=user_id,
                organization_id=media_file.organization_id,
                est_audio_hours=(Decimal(str(duration_s)) / Decimal(3600) if duration_s else None),
                task_id=task_id,
            )
        )

        # Auto-resolve queue from user's ASR provider if not specified
        if not use_cpu and gpu_queue is None:
            gpu_queue = _resolve_gpu_queue(user_id, db)

        create_task_record(db, task_id, user_id, file_id, "transcription")
        update_media_file_status(db, file_id, FileStatus.PROCESSING)
        update_task_status(db, task_id, "in_progress", progress=0.0)

    # Build the 3-stage chain — route lightweight models to CPU
    if use_cpu:
        logger.info(f"Routing file {file_uuid} to CPU transcription (model={whisper_model})")
        transcribe_task = transcribe_cpu_task.s().set(
            queue=CeleryQueues.CPU_TRANSCRIBE, priority=CPUPriority.PIPELINE_CRITICAL
        )
    else:
        transcribe_task = transcribe_gpu_task.s().set(
            queue=gpu_queue, priority=GPUPriority.USER_IMPORT
        )

    pipeline = chain(
        preprocess_for_transcription.s(
            file_uuid=file_uuid,
            task_id=task_id,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            downstream_tasks=downstream_tasks,
            source_language=source_language,
            translate_to_english=translate_to_english,
            disable_diarization=True if use_cpu else disable_diarization,
            diarization_source="off" if use_cpu else diarization_source,
            whisper_model=whisper_model,
        ).set(queue=CeleryQueues.CPU, priority=CPUPriority.PIPELINE_CRITICAL),
        transcribe_task,
        finalize_transcription.s().set(
            queue=CeleryQueues.CPU, priority=CPUPriority.PIPELINE_CRITICAL
        ),
    )

    # Record dispatch timestamp + queue depth snapshot for inter-stage gap
    # measurement. Both are no-ops when ENABLE_BENCHMARK_TIMING is off.
    from app.utils import benchmark_timing

    benchmark_timing.mark(task_id, "dispatch_timestamp")
    benchmark_timing.capture_queue_depth(task_id)

    # Dispatch with error callback for cleanup
    pipeline.apply_async(
        link_error=[on_pipeline_error.si(file_uuid, task_id).set(queue=CeleryQueues.UTILITY)],
    )

    route = "cpu-transcribe" if use_cpu else gpu_queue
    logger.info(
        f"Dispatched transcription pipeline for file {file_uuid} (task_id={task_id}, route={route})"
    )

    return task_id


def dispatch_batch_transcription(
    file_uuids: list[str],
    gpu_queue: str | None = None,
    user_id: int | None = None,
    **kwargs,
) -> dict:
    """Dispatch transcription chains for a batch of files using Celery group.

    Each file gets its own chain within a Celery group. The CPU queue
    (concurrency=8) preprocesses multiple files in parallel, keeping audio
    ready for the GPU. The GPU processes files one at a time (concurrency=1)
    with zero idle time.

    Returns dict with batch_id and task_ids for tracking.
    """
    from .core import transcribe_cpu_task
    from .core import transcribe_gpu_task
    from .postprocess import finalize_transcription
    from .preprocess import preprocess_for_transcription

    task_ids = []
    chains = []
    batch_whisper_model = kwargs.get("whisper_model")
    use_cpu = batch_whisper_model in LIGHTWEIGHT_MODELS

    for file_uuid in file_uuids:
        try:
            task_id = str(uuid.uuid4())

            with session_scope() as db:
                media_file = db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
                if not media_file:
                    logger.error(f"Media file {file_uuid} not found, skipping")
                    continue

                file_id = int(media_file.id)
                owner_id = int(media_file.user_id)

                resolved_queue = gpu_queue
                if not use_cpu and resolved_queue is None:
                    resolved_queue = _resolve_gpu_queue(owner_id, db)

                create_task_record(db, task_id, owner_id, file_id, "transcription")
                update_media_file_status(db, file_id, FileStatus.PROCESSING)
                update_task_status(db, task_id, "in_progress", progress=0.0)

            # Force disable_diarization for CPU path
            batch_kwargs = dict(kwargs)
            if use_cpu:
                batch_kwargs["disable_diarization"] = True

            if use_cpu:
                transcribe_task = transcribe_cpu_task.s().set(
                    queue=CeleryQueues.CPU_TRANSCRIBE, priority=CPUPriority.PIPELINE_CRITICAL
                )
            else:
                transcribe_task = transcribe_gpu_task.s().set(
                    queue=resolved_queue, priority=GPUPriority.USER_IMPORT
                )

            pipeline = chain(
                preprocess_for_transcription.s(
                    file_uuid=file_uuid,
                    task_id=task_id,
                    **batch_kwargs,
                ).set(queue=CeleryQueues.CPU, priority=CPUPriority.PIPELINE_CRITICAL),
                transcribe_task,
                finalize_transcription.s().set(
                    queue=CeleryQueues.CPU, priority=CPUPriority.PIPELINE_CRITICAL
                ),
            )
            pipeline.set(
                link_error=[
                    on_pipeline_error.si(file_uuid, task_id).set(queue=CeleryQueues.UTILITY)
                ]
            )

            chains.append(pipeline)
            task_ids.append(task_id)

        except Exception as e:
            logger.error(f"Failed to build pipeline for {file_uuid}: {e}")

    if not chains:
        return {"batch_id": None, "task_ids": []}

    batch = group(chains)
    result = batch.apply_async()

    # Store batch metadata in Redis for completion tracking (24h TTL)
    try:
        from app.core.redis import get_redis

        get_redis().set(
            f"batch:{result.id}",
            json.dumps({"file_uuids": file_uuids, "task_ids": task_ids}),
            ex=86400,
        )
    except Exception as e:
        logger.warning(f"Failed to store batch metadata: {e}")

    logger.info(
        f"Dispatched batch of {len(chains)} pipelines "
        f"(batch_id={result.id}, task_ids={len(task_ids)})"
    )

    return {"batch_id": str(result.id), "task_ids": task_ids}


@celery_app.task(name="transcription.pipeline_error", ignore_result=True)
def on_pipeline_error(file_uuid: str, task_id: str) -> None:
    """Safety-net error handler for pipeline chain failures.

    Ensures temp audio is cleaned up and file/task status is marked ERROR
    even if the failing task's internal error handler didn't complete.
    Called via link_error when any task in the chain raises an exception.

    Special handling:
    - OOM errors: logs VRAM state and suggests reducing batch size
    - Postprocess failures: segments are already saved by GPU task, so
      the file is marked COMPLETED with a warning rather than ERROR
    """
    from app.services.minio_service import cleanup_temp_audio
    from app.utils import benchmark_timing
    from app.utils.uuid_helpers import get_file_by_uuid

    from .notifications import send_error_notification

    logger.warning(f"Pipeline error handler triggered for file {file_uuid}")

    # Clean up temp audio
    with contextlib.suppress(Exception):
        cleanup_temp_audio(file_uuid)

    # Record a terminal marker so the analysis layer can distinguish
    # error-mode completions from successful ones (Phase 2 PR #8, G27).
    benchmark_timing.mark(task_id, "pipeline_error_end")

    # Track the captured file id for the error-path timing flush below —
    # avoids running the flush inside the closed session_scope.
    flushed_file_id: int | None = None
    flushed_user_id: int | None = None

    # Ensure file is marked as errored
    try:
        with session_scope() as db:
            media_file = get_file_by_uuid(db, file_uuid)
            if media_file is not None:
                flushed_file_id = int(media_file.id)
                flushed_user_id = int(media_file.user_id)

            # Check task error to determine failure stage
            from app.models.media import Task

            task = db.query(Task).filter(Task.id == task_id).first()
            error_msg = (task.error_message or "") if task else ""

            is_oom = "CUDA out of memory" in error_msg or "OutOfMemoryError" in error_msg
            is_postprocess_only = task and task.status == "completed"

            if is_oom:
                _log_oom_diagnostics(error_msg)

            # If postprocess failed but task was already completed (segments saved),
            # keep the file as COMPLETED — the transcription data is intact
            if is_postprocess_only:
                logger.info(
                    f"Postprocess failed but segments already saved for {file_uuid}, "
                    "keeping COMPLETED status"
                )
                _flush_error_timing(task_id, flushed_file_id, flushed_user_id)
                return

            if media_file and media_file.status not in (
                FileStatus.ERROR,
                FileStatus.COMPLETED,
            ):
                update_media_file_status(db, int(media_file.id), FileStatus.ERROR)

            # Only update task if not already finalized
            if task and task.status not in ("completed", "failed"):
                user_error = _get_pipeline_error_message(error_msg, is_oom)
                update_task_status(
                    db,
                    task_id,
                    "failed",
                    error_message=user_error,
                    completed=True,
                )

                if media_file:
                    send_error_notification(
                        int(media_file.user_id),
                        int(media_file.id),
                        user_error,
                    )
    except Exception as e:
        logger.error(f"Error in pipeline error handler: {e}")
    finally:
        _flush_error_timing(task_id, flushed_file_id, flushed_user_id)


def _flush_error_timing(task_id: str, file_id: int | None, user_id: int | None) -> None:
    """Persist the Redis timing hash into ``file_pipeline_timing`` on failure.

    Mirrors the success-path flush that finalize_transcription performs, so
    the error wall-clock stays queryable. Best-effort: instrumentation
    failures never propagate.
    """
    from app.utils import benchmark_timing

    if not benchmark_timing.benchmark_enabled() or file_id is None:
        return
    try:
        from app.services.pipeline_timing_service import record_pipeline_timing

        record_pipeline_timing(task_id=task_id, file_id=file_id, user_id=user_id)
    except Exception as e:
        logger.debug(f"Error-path timing flush failed for {task_id}: {e}")


def _log_oom_diagnostics(error_msg: str) -> None:
    """Log VRAM diagnostics when an OOM error occurs."""
    logger.error(f"GPU OOM detected: {error_msg[:200]}")
    try:
        import torch

        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                free, total = torch.cuda.mem_get_info(i)
                logger.error(
                    f"GPU {i} VRAM: {free / 1024**2:.0f}MB free / {total / 1024**2:.0f}MB total"
                )
    except Exception as e:
        logger.debug(f"Could not read GPU VRAM during OOM diagnostics: {e}")


def _get_pipeline_error_message(error_msg: str, is_oom: bool) -> str:
    """Generate a user-friendly error message for pipeline failures."""
    if is_oom:
        return (
            "GPU ran out of memory during processing. "
            "Try reducing the number of concurrent tasks or using a smaller model."
        )
    return "Transcription pipeline failed unexpectedly"
