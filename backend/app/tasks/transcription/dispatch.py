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

from app.core.exceptions import PipelinePlanRefusedError
from app.core.queues import CeleryQueues
from app.core.queues import CPUPriority
from app.core.queues import GPUPriority
from app.core.queues import gpu_split_enabled
from app.core.task_liveness import mark_queued
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.pipeline_plan import PipelinePlan
from app.utils.task_utils import create_task_record
from app.utils.task_utils import update_media_file_status
from app.utils.task_utils import update_task_status

from .hooks import DispatchBlockedError
from .hooks import QuotaExceededError
from .pipeline_error import on_pipeline_error
from .queue_consumers import _gpu_transcribe_consumer_present
from .requested_options import resolve_dispatch_options

logger = logging.getLogger(__name__)


def _plan_for_dispatch(
    db, media_file: MediaFile, *, whisper_model: str | None, skip_diarization: bool
) -> PipelinePlan:
    """The file's pipeline plan, or a refusal recorded on the file (issue #1225).

    Computed ONCE per run, here, and carried to every stage in the task context. The plan
    names the transcription queue (``cloud-asr`` for a vendor, ``cpu-transcribe`` for a
    per-file lightweight model, ``gpu``/``gpu-transcribe`` for local Whisper) and every
    later step. A plan with a blocking issue is refused exactly like the #865 refusal it
    generalises: the file is marked ERROR with the issue's fixed sentence (classified as a
    permanent configuration failure, so nothing retries it) and no task record is created.

    Raises:
        PipelinePlanRefusedError: The plan has a blocking issue.
    """
    from app.services.error_categorization_service import ErrorCategorizationService
    from app.services.pipeline_plan_inputs import plan_for_user

    def _plan(gpu_split: bool) -> PipelinePlan:
        return plan_for_user(
            db,
            int(media_file.user_id),
            whisper_model=whisper_model,
            disable_diarization=skip_diarization,
            gpu_split=gpu_split,
            # The file's own tenant, from its row: the privacy policy is judged per
            # (owner, organization), never from whoever dispatched.
            organization_id=media_file.organization_id,
            media_file_id=int(media_file.id),
        )

    plan = _plan(gpu_split_enabled())
    if plan.transcription.queue == CeleryQueues.GPU_TRANSCRIBE and (
        not _gpu_transcribe_consumer_present()
    ):
        # ``ENGINE_GPU_SPLIT`` is an ordinary ``.env`` knob that reaches every container
        # whichever overlay is loaded, so it can be on with no celery-worker-gpu-transcribe
        # running; routing on it alone black-holes every transcription (issue #703). The
        # broker is asked only for a run the split would actually take.
        logger.warning(
            f"ENGINE_GPU_SPLIT is set but no worker is consuming "
            f"'{CeleryQueues.GPU_TRANSCRIBE}' — routing to '{CeleryQueues.GPU}' instead "
            f"of a queue nothing would drain. Start the stack with --with-gpu-split, "
            f"or unset ENGINE_GPU_SPLIT if split topology isn't intended here."
        )
        plan = _plan(False)
    blocking = plan.blocking()
    if blocking:
        issue = blocking[0]
        failure = ErrorCategorizationService.classify_failure(issue.message)
        logger.warning(
            "Refusing to dispatch file %s: pipeline plan issue %s", media_file.uuid, issue.code
        )
        media_file.last_error_message = failure.user_message
        media_file.error_category = failure.retry_category.value
        update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
        raise PipelinePlanRefusedError(issue.code, issue.message)
    logger.info(
        "Pipeline plan for file %s: transcription=%s on %s, speakers=%s (%s)",
        media_file.uuid,
        plan.transcription.engine,
        plan.transcription.queue,
        plan.diarization.engine,
        plan.diarization.execution,
    )
    return plan


def stage_priorities(*, retry: bool) -> tuple[int, int]:
    """``(cpu_priority, gpu_priority)`` for a pipeline's stages.

    A retry outranks a fresh submission on every queue it touches, which is what keeps a
    requeued file in front of work submitted after it (kombu's Redis transport serves the
    lower priority number first).
    """
    if retry:
        return CPUPriority.PIPELINE_RETRY, GPUPriority.TRANSCRIPTION_RETRY
    return CPUPriority.PIPELINE_CRITICAL, GPUPriority.USER_IMPORT


def _mark_dispatch_failed(file_uuid: str, task_id: str, message: str) -> None:
    """Record a failed pipeline publish on the file and its task row.

    The publish at the end of dispatch_transcription_pipeline happens AFTER
    create_task_record has committed the file to PROCESSING, in an already-closed
    session. A broker failure there leaves an in_progress task and a PROCESSING
    file nothing will ever advance — link_error only fires for a chain that was
    actually published. Best-effort: never raises, never masks the real exception.
    """
    truncated = message[:2000]
    try:
        with session_scope() as db:
            # Task row first, file status LAST: update_task_status's terminal
            # branch calls update_media_file_from_task_status, which would derive
            # COMPLETED for a file owning an older completed task otherwise.
            update_task_status(db, task_id, "failed", error_message=truncated, completed=True)

            media_file = db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
            if media_file is None:
                return
            file_id = int(media_file.id)
            user_id = int(media_file.user_id)
            media_file.last_error_message = truncated
            update_media_file_status(db, file_id, FileStatus.ERROR)  # takedown-aware, #824
    except Exception as e:
        logger.error(
            f"Could not record the failed dispatch of {file_uuid} (task {task_id}): {e}",
            exc_info=True,
        )
        return

    with contextlib.suppress(Exception):
        from .notifications import send_error_notification

        send_error_notification(user_id, file_id, truncated)


def _run_pre_dispatch_gate(db, media_file: MediaFile, file_uuid: str, task_id: str) -> None:
    """Checks every dispatch path runs before it creates a task record.

    Shared by :func:`dispatch_transcription_pipeline` and
    :func:`dispatch_batch_transcription` so a registered gate cannot be bypassed by
    dispatching as a batch (#1169).

    1. The per-organization duration ceiling from the upload-limits resolver (``None``
       when no resolver is registered). Enforced here, the earliest point where the true
       duration is known; upload-time checks only see bytes. The global URL-ingest cap
       is enforced separately at ingest.
    2. Registered before-dispatch hooks (none by default).

    Raises:
        ValueError: The media is longer than the organization's ceiling; the file has
            been marked ERROR.
        QuotaExceededError: A hook refused because the account is over quota.
        DispatchBlockedError: A hook refused for any other deliberate reason.
    """
    from decimal import Decimal

    from app.core.tenant_limits import resolve_upload_limits

    from .hooks import DispatchContext
    from .hooks import fire_before_dispatch

    # est_audio_hours stays None when the duration is genuinely unknown (metadata
    # extraction hasn't populated it yet). Coercing unknown to 0 would let a quota
    # hook wave every such job through; only a positive, known duration becomes a
    # concrete estimate, and the hook decides what to do with None.
    duration_s = media_file.duration

    limits = resolve_upload_limits(media_file.organization_id)
    if (
        limits is not None
        and limits.max_duration_seconds is not None
        and duration_s
        and duration_s > limits.max_duration_seconds
    ):
        update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
        raise ValueError(
            f"Media duration {duration_s:.0f}s exceeds the plan limit of "
            f"{limits.max_duration_seconds}s"
        )

    est_audio_hours = (
        Decimal(str(duration_s)) / Decimal(3600) if duration_s and duration_s > 0 else None
    )
    fire_before_dispatch(
        DispatchContext(
            file_id=int(media_file.id),
            file_uuid=file_uuid,
            user_id=int(media_file.user_id),
            organization_id=media_file.organization_id,
            est_audio_hours=est_audio_hours,
            task_id=task_id,
        )
    )


def dispatch_transcription_pipeline(
    file_uuid: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    downstream_tasks: list[str] | None = None,
    source_language: str | None = None,
    translate_to_english: bool | None = None,
    disable_diarization: bool | None = None,
    whisper_model: str | None = None,
    task_id: str | None = None,
    retry: bool = False,
    countdown: int | None = None,
    reuse_requested_options: bool = False,
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
        disable_diarization: The per-file "skip speaker detection" (``None`` = not asked).
        whisper_model: Optional per-task Whisper model override (local ASR only); a
            lightweight one transcribes on the CPU (full image only).
        task_id: Optional pre-generated application task_id. When provided
            (e.g., from the HTTP upload handler) it is reused so HTTP-phase
            benchmark markers share the ``benchmark:{task_id}`` Redis hash
            with the pipeline markers. When None, a fresh UUID is generated.
        retry: An automatic retry of a run the infrastructure interrupted. Every stage
            is published at the retry priority (``CPUPriority.PIPELINE_RETRY`` /
            ``GPUPriority.TRANSCRIPTION_RETRY``) so the file goes ahead of submissions
            that arrived after it rather than to the back of the queue.
        countdown: Seconds to hold the first stage before it runs (retry backoff).
        reuse_requested_options: True for a re-run of a file the user already submitted
            (retry, recovery): every per-file option left as ``None`` is read back from the
            file's stored request (``requested_options``). False for a fresh request: the
            options passed, ``None`` included, become the file's stored request.
    """
    from .postprocess import finalize_transcription
    from .preprocess import preprocess_for_transcription

    if not task_id:
        task_id = str(uuid.uuid4())

    # Create task record and set file to PROCESSING
    with session_scope() as db:
        media_file = db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
        if not media_file:
            raise ValueError(f"Media file {file_uuid} not found")

        file_id = int(media_file.id)
        user_id = int(media_file.user_id)

        options = resolve_dispatch_options(
            media_file,
            reuse_requested_options=reuse_requested_options,
            whisper_model=whisper_model,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            disable_diarization=disable_diarization,
        )
        whisper_model = options.whisper_model
        min_speakers = options.min_speakers
        max_speakers = options.max_speakers
        num_speakers = options.num_speakers

        # Pre-dispatch gate (duration ceiling + before-dispatch hooks). A refusal
        # propagates BEFORE the task record is created, so a blocked job leaves no
        # trace and nothing dispatches.
        _run_pre_dispatch_gate(db, media_file, file_uuid, task_id)

        plan = _plan_for_dispatch(
            db,
            media_file,
            whisper_model=whisper_model,
            skip_diarization=options.skip_diarization,
        )

        create_task_record(db, task_id, user_id, file_id, "transcription")
        update_media_file_status(db, file_id, FileStatus.PROCESSING)
        update_task_status(db, task_id, "in_progress", progress=0.0)

    cpu_priority, gpu_priority = stage_priorities(retry=retry)
    transcribe_task = _transcribe_stage(plan, cpu_priority, gpu_priority)

    first_stage_options: dict = {"queue": CeleryQueues.CPU, "priority": cpu_priority}
    if countdown:
        first_stage_options["countdown"] = countdown

    pipeline = chain(
        preprocess_for_transcription.s(
            file_uuid=file_uuid,
            task_id=task_id,
            pipeline_plan=plan.to_context(),
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            downstream_tasks=downstream_tasks,
            source_language=source_language,
            translate_to_english=translate_to_english,
            whisper_model=whisper_model,
        ).set(**first_stage_options),
        transcribe_task,
        finalize_transcription.s().set(queue=CeleryQueues.CPU, priority=cpu_priority),
    )

    # Record dispatch timestamp + queue depth snapshot for inter-stage gap
    # measurement. Both are no-ops when ENABLE_BENCHMARK_TIMING is off.
    from app.utils import benchmark_timing

    benchmark_timing.mark(task_id, "dispatch_timestamp")
    benchmark_timing.capture_queue_depth(task_id)

    # issue #1020: recovery must be able to tell a run waiting for a worker from a lost one.
    mark_queued(task_id)

    # Dispatch with error callback for cleanup
    try:
        pipeline.apply_async(
            link_error=[on_pipeline_error.si(file_uuid, task_id).set(queue=CeleryQueues.UTILITY)],
        )
    except Exception as e:
        logger.error(
            f"Failed to publish the transcription pipeline for {file_uuid} "
            f"(task_id={task_id}): {e}",
            exc_info=True,
        )
        _mark_dispatch_failed(file_uuid, task_id, f"Could not queue transcription: {e}")
        raise

    logger.info(
        f"Dispatched transcription pipeline for file {file_uuid} (task_id={task_id}, "
        f"route={plan.transcription.queue}{', retry' if retry else ''})"
    )

    return task_id


def _transcribe_stage(plan: PipelinePlan, cpu_priority: int, gpu_priority: int):
    """The chain's transcription link, on the queue the plan names.

    A per-file lightweight model (``local_cpu``) runs ``transcription.cpu_transcribe`` on
    ``cpu-transcribe``; everything else runs ``transcription.gpu_transcribe`` on ``gpu``,
    ``gpu-transcribe`` (split topology, inline speaker detection only) or ``cloud-asr``.
    """
    from .core import transcribe_cpu_task
    from .core import transcribe_gpu_task

    queue = plan.transcription.queue
    if plan.transcription.engine == "local_cpu":
        logger.info(f"Routing to CPU transcription (model={plan.transcription.model})")
        return transcribe_cpu_task.s().set(queue=queue, priority=cpu_priority)
    return transcribe_gpu_task.s().set(queue=queue, priority=gpu_priority)


def dispatch_batch_transcription(
    file_uuids: list[str],
    user_id: int | None = None,
    **kwargs,
) -> dict:
    """Dispatch transcription chains for a batch of files using Celery group.

    Each file gets its own chain within a Celery group, planned exactly like a single
    dispatch (its own pipeline plan, its own refusal). The CPU queue (concurrency=8)
    preprocesses multiple files in parallel, keeping audio ready for the GPU.

    Returns dict with batch_id and task_ids for tracking.
    """
    from .postprocess import finalize_transcription
    from .preprocess import preprocess_for_transcription

    task_ids = []
    chains = []
    batch_kwargs = dict(kwargs)
    skip_diarization = bool(batch_kwargs.pop("disable_diarization", None))

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

                try:
                    _run_pre_dispatch_gate(db, media_file, file_uuid, task_id)
                except (QuotaExceededError, DispatchBlockedError) as e:
                    # A deliberate refusal: skip this file exactly as the single-file
                    # path would (no task record, status untouched), keep the batch going.
                    logger.warning(f"Dispatch of {file_uuid} refused by a hook: {e.detail}")
                    continue

                # A blocking plan marks THIS file ERROR (no task record) and raises; the
                # outer `except Exception` only logs, so the rest of the batch continues.
                plan = _plan_for_dispatch(
                    db,
                    media_file,
                    whisper_model=batch_kwargs.get("whisper_model"),
                    skip_diarization=skip_diarization,
                )

                create_task_record(db, task_id, owner_id, file_id, "transcription")
                update_media_file_status(db, file_id, FileStatus.PROCESSING)
                update_task_status(db, task_id, "in_progress", progress=0.0)

            pipeline = chain(
                preprocess_for_transcription.s(
                    file_uuid=file_uuid,
                    task_id=task_id,
                    pipeline_plan=plan.to_context(),
                    **batch_kwargs,
                ).set(queue=CeleryQueues.CPU, priority=CPUPriority.PIPELINE_CRITICAL),
                _transcribe_stage(plan, CPUPriority.PIPELINE_CRITICAL, GPUPriority.USER_IMPORT),
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

    for queued_task_id in task_ids:
        mark_queued(queued_task_id)  # issue #1020, as in dispatch_transcription_pipeline

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
