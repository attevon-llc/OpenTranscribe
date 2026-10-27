"""CPU postprocessing task for the transcription pipeline.

Handles speaker embeddings, search indexing, downstream task dispatch,
and MinIO temp cleanup after GPU processing completes.

Speaker matching runs synchronously before marking COMPLETED so the user
sees accurate speaker labels immediately. Search indexing and downstream
tasks (summarization, speaker attributes, clustering) are dispatched as
background work — they each send their own WebSocket events when done.

Part of the 3-stage chain: preprocess (CPU) → transcribe (GPU) → postprocess (CPU)
"""

import logging
import time

import numpy as np

from app.core import stage_timing
from app.core.celery import celery_app
from app.core.constants import CeleryQueues
from app.core.constants import CPUPriority
from app.core.constants import UtilityPriority
from app.core.enums import PostProcessingState
from app.core.enums import PostProcessingTask
from app.core.enums import TranscriptionStage
from app.core.task_liveness import clear_infra_requeues
from app.core.task_liveness import clear_run_markers
from app.core.task_liveness import run_heartbeat
from app.db.session_utils import session_scope
from app.services import post_processing_status
from app.services.file_provenance import record_inline_voiceprints
from app.services.privacy.gates import voiceprints_allowed
from app.utils import benchmark_timing
from app.utils.task_utils import update_task_status
from app.utils.task_utils import update_task_status_with_duration

from .context import plan_of
from .notifications import send_completion_notification
from .notifications import send_progress_notification
from .run_ownership import SUPERSEDED

# How long to wait before folding the enrichment tail into the timing row. Long enough for
# indexing, clustering, summary and redaction to finish on a normal file; the upsert merges,
# so an early flush simply records less rather than corrupting anything.
TIMING_TAIL_FLUSH_DELAY_S = 180

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="transcription.postprocess",
    priority=CPUPriority.PIPELINE_CRITICAL,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=2,
    autoretry_for=(ConnectionError, TimeoutError, IOError),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
)
def finalize_transcription(self, gpu_result: dict) -> dict:
    """Stage 3 of the pipeline: :func:`_finalize`, run under the run's lease.

    Like every other stage it holds the run lease while it works, so a worker killed here is
    noticed within one lease TTL (``app/core/broker_orphans.py``) instead of leaving the run
    reading as "queued". Once the run is COMPLETED its liveness markers are dropped: nothing
    of it is queued or running any more. A run still waiting on an asynchronous embedding
    task stays handed back to the broker, as before.
    """
    task_id = gpu_result.get("task_id")
    if not task_id or gpu_result.get("status") in _NO_WORK_STATUSES:
        return _finalize(gpu_result)
    with (
        run_heartbeat(str(task_id)),
        stage_timing.stage(
            "postprocess", task_id=task_id, file_id=gpu_result.get("file_id")
        ) as run,
    ):
        result = _finalize(gpu_result)
        if result.get("status") != "success":
            run.fail()
    if result.get("status") == "success" and _run_completed(str(task_id)):
        clear_run_markers(str(task_id))
        clear_infra_requeues(str(gpu_result.get("file_uuid") or ""))
    return result


#: Payload statuses for which finalize only cleans up (no real work, so no lease).
_NO_WORK_STATUSES = frozenset({"error", "cancelled", SUPERSEDED, "split_forwarded"})


def _run_completed(task_id: str) -> bool:
    try:
        from app.models.media import Task

        with session_scope() as db:
            row = db.query(Task.status).filter(Task.id == task_id).first()
            return row is not None and row[0] == "completed"
    except Exception as e:  # noqa: BLE001 - only decides whether to drop two Redis keys early
        logger.debug("Could not read the status of run %s: %s", task_id, e)
        return False


def _record_postprocess_failure(gpu_result: dict, error: Exception) -> dict:
    """Record a postprocess failure on the run's Task row; return the chain payload.

    A run whose cancellation was requested is recorded as cancelled instead (issue #1163): a
    user who stopped the file must not see it reported as a failure.
    """
    from app.services.transcription_retry import finish_cancelled_run
    from app.services.transcription_retry import run_cancel_requested

    task_id = gpu_result["task_id"]
    file_id = gpu_result["file_id"]
    if run_cancel_requested(task_id, file_id):
        return finish_cancelled_run(
            task_id, file_id, gpu_result["file_uuid"], gpu_result["user_id"]
        )
    try:
        with session_scope() as db:
            update_task_status(
                db,
                task_id,
                "failed",
                error_message=f"Post-processing error: {error}",
                completed=True,
            )
    except Exception as task_err:
        logger.debug(f"Failed to mark task as failed: {task_err}")
    return {
        "status": "error",
        "file_id": file_id,
        "error": str(error),
        "segment_count": gpu_result.get("segment_count", 0),
    }


def _finalize(gpu_result: dict) -> dict:
    """CPU postprocessing: speaker matching → mark COMPLETED → background enrichment.

    Stage 3 of the 3-stage pipeline chain. Receives result from GPU task.

    Speaker matching runs first so the user sees accurate profile names
    (not generic "Speaker 1") when the file is marked completed.

    Search indexing and downstream tasks (summarization, speaker attributes,
    clustering) are dispatched as background work after completion. Each
    sends its own WebSocket event when done.
    """
    # Record postprocess received timestamp + pickup markers for inter-stage
    # gap measurement. Cold-start flag is shared with preprocess (same worker
    # role).
    task_id_for_bench = gpu_result.get("task_id")
    benchmark_timing.mark(task_id_for_bench, "postprocess_received")
    benchmark_timing.mark(task_id_for_bench, "postprocess_task_prerun")

    if gpu_result.get("status") == "error":
        logger.warning(
            f"Skipping postprocess — GPU task failed for file {gpu_result.get('file_id')}"
        )
        _cleanup_temp(gpu_result.get("file_uuid"))
        return gpu_result

    if gpu_result.get("status") == "cancelled":
        # issue #823: the GPU/CPU stage stood down at a cooperative checkpoint because the user
        # cancelled this file, and `cancellation.finish_cancelled` has already written the
        # terminal state and notified. It RETURNS rather than raising precisely so acks_late
        # acks the message — which means this successor link still runs, and its job is to
        # release the temp audio and otherwise do nothing. Marking the file COMPLETED here
        # would undo the cancellation the user asked for.
        logger.info(
            "Skipping postprocess — file %s was cancelled by the user (task %s)",
            gpu_result.get("file_id"),
            gpu_result.get("task_id"),
        )
        _cleanup_temp(gpu_result.get("file_uuid"))
        return gpu_result

    if gpu_result.get("status") in (SUPERSEDED, "split_forwarded"):
        # issue #1020 (superseded): a stage found its run replaced by a newer one and stood
        # down. Unlike the cancelled branch above this must NOT release the temp audio: it is
        # keyed by file, and the replacement run owns it now.
        #
        # gpu-split topology (core.py::transcribe_gpu_task): this dict is what the
        # transcribe-only leg returns to satisfy the OUTER pipeline chain's
        # unconditional third link. It carries no user_id/speaker_mapping/etc. — the
        # real completion belongs to diarize_gpu_task, which core.py already chained
        # to a SEPARATE finalize_transcription invocation of its own. This call is
        # that outer chain running against a payload that isn't the real result; it
        # must be a no-op, not a KeyError (issue that made every --with-gpu-split job
        # fail visibly even though diarization went on to complete correctly).
        logger.debug(
            "finalize_transcription: no-op for %s result (file_id=%s, task_id=%s)",
            gpu_result.get("status"),
            gpu_result.get("file_id"),
            gpu_result.get("task_id"),
        )
        return gpu_result

    file_uuid = gpu_result["file_uuid"]
    file_id = gpu_result["file_id"]
    user_id = gpu_result["user_id"]
    task_id = gpu_result["task_id"]
    speaker_mapping = gpu_result.get("speaker_mapping", {})
    native_embeddings = gpu_result.get("native_embeddings")
    use_native = gpu_result.get("use_native_embeddings", False)
    asr_provider = gpu_result.get("asr_provider", "local")
    downstream_tasks = gpu_result.get("downstream_tasks")

    post_start = time.perf_counter()
    # When we dispatch a stage that still needs the preprocessed WAV (the
    # speaker-detection post stage, or the voiceprint stage) we MUST NOT clean up
    # the temp audio here — those tasks clean it up themselves once they finish.
    # Phase 2 PR #4 relies on this for the embedding stage to read from scratch.
    defer_temp_cleanup = False

    try:
        plan = plan_of(gpu_result)
        # Speaker matching must complete before marking "completed" so the
        # frontend shows matched profile names, not generic "Speaker 1" labels.
        # ``handed_off`` names the stage that now owns the run's completion (and its
        # metering): exactly one terminus per run.
        handed_off: str | None = None
        if plan.diarization.execution in ("post_stage_local", "post_stage_cloud"):
            handed_off = "post_stage" if _dispatch_post_stage(gpu_result, plan) else None
        elif plan.speaker_id.execution == "embedding_stage":
            handed_off = "voiceprint_stage" if _dispatch_embedding_stage(gpu_result) else None
        elif plan.speaker_id.execution == "inline_native":
            # This server's engine diarized inside the GPU stage and handed back the
            # per-speaker vectors: match them here, nothing to dispatch.
            send_progress_notification(
                user_id,
                file_id,
                0.80,
                "Processing speaker identification",
                stage=TranscriptionStage.SPEAKERS,
            )
            if use_native and native_embeddings:
                _process_native_embeddings(
                    file_id, user_id, task_id, native_embeddings, speaker_mapping
                )
            if native_embeddings and not use_native:
                _store_v4_centroids(file_id, file_uuid, user_id, native_embeddings, speaker_mapping)
        else:
            logger.info(f"Skipping speaker embeddings for file {file_id} (diarization disabled)")
        defer_temp_cleanup = handed_off is not None

        if handed_off == "post_stage":
            # The text is usable now; the post stage sends its own completion notification
            # and is the run's metering terminus (never here: no double count).
            send_progress_notification(
                user_id,
                file_id,
                0.90,
                "Transcription complete, detecting speakers...",
                stage=TranscriptionStage.SPEAKERS,
            )
            with session_scope() as db:
                _task, task_duration_seconds = update_task_status_with_duration(
                    db, task_id, "completed", progress=1.0, completed=True
                )
            send_completion_notification(user_id, file_id, duration_seconds=task_duration_seconds)
        elif handed_off == "voiceprint_stage":
            # The voiceprint stage completes, notifies and meters the run.
            send_progress_notification(
                user_id,
                file_id,
                0.90,
                "Processing speaker identification",
                stage=TranscriptionStage.SPEAKERS,
            )
            with session_scope() as db:
                update_task_status(db, task_id, "in_progress", progress=0.90)
        else:
            # Nothing handed off (inline speakers, none, or a hand-off that could not be
            # queued): everything is done — this is the run's single metering terminus.
            send_progress_notification(
                user_id,
                file_id,
                0.95,
                "Finalizing transcription",
                stage=TranscriptionStage.SPEAKERS,
            )
            with session_scope() as db:
                _task, task_duration_seconds = update_task_status_with_duration(
                    db, task_id, "completed", progress=1.0, completed=True
                )
                _fire_completion_metering(
                    db, file_id=file_id, run_id=task_id, provider=asr_provider, success=True
                )
            send_completion_notification(user_id, file_id, duration_seconds=task_duration_seconds)

        completion_elapsed = time.perf_counter() - post_start
        logger.info(
            f"TIMING: postprocess speaker matching + completion "
            f"in {completion_elapsed:.3f}s for file {file_id}"
        )

        # Background enrichment (search indexing + downstream tasks). The fan-out reports
        # what it dispatched as a post-processing plan, and each task reports its own
        # outcome (app/services/post_processing_status.py).
        enrich_and_dispatch.delay(
            file_id=file_id,
            file_uuid=file_uuid,
            user_id=user_id,
            downstream_tasks=downstream_tasks,
            pipeline_task_id=task_id,
        )

    except Exception as e:
        logger.error(f"Postprocess failed for file {file_id}: {e}")
        # Don't re-raise — segments are already saved by GPU task
        return _record_postprocess_failure(gpu_result, e)

    finally:
        if defer_temp_cleanup:
            logger.debug(
                f"Deferring temp audio cleanup for {file_uuid} "
                "(downstream GPU task still needs the preprocessed WAV)"
            )
        else:
            _cleanup_temp(file_uuid)
        # Mark end-of-postprocess for inter-stage gap measurement and flush
        # the Redis benchmark hash into the durable file_pipeline_timing row.
        # Both are no-ops when ENABLE_BENCHMARK_TIMING is off.
        benchmark_timing.mark(task_id, "postprocess_end")
        _persist_timing_row(task_id, file_id, user_id)
        # Everything after this — indexing, clustering, summary, redaction — writes its
        # markers to Redis *after* the row above is saved, which is why those columns were
        # always null. Re-flush once the tail has had time to finish; the upsert merges, and
        # the Redis hash lives 24 h so nothing is lost if the tail runs long.
        _schedule_timing_tail_flush(task_id, file_id, user_id)

    elapsed = time.perf_counter() - post_start
    logger.info(f"TIMING: postprocess completed in {elapsed:.3f}s for file {file_id}")

    return {
        "status": "success",
        "file_id": file_id,
        "segment_count": gpu_result.get("segment_count", 0),
    }


def _dispatch_post_stage(gpu_result: dict, plan) -> bool:
    """Queue the plan's post stage (speaker detection after the transcript is saved).

    ``rediarize_task`` with the plan's engine and merge granularity, on the plan's queue:
    ``cloud-asr`` for a cloud detector (no GPU slot held while the vendor works),
    ``gpu_preferred_queue()`` for this server's engine. It is the run's completion and
    metering terminus. Returns True when queued: it then owns the temp WAV.
    """
    from app.services.pipeline_plan import post_stage_engine
    from app.tasks.rediarize_task import rediarize_task

    from .user_settings import resolve_speaker_range_for_user

    file_id, user_id = gpu_result["file_id"], gpu_result["user_id"]
    send_progress_notification(
        user_id, file_id, 0.85, "Queuing speaker detection", stage=TranscriptionStage.SPEAKERS
    )
    issues = [issue.code for issue in plan.issues if issue.severity == "warning"]
    try:
        speaker_range = resolve_speaker_range_for_user(
            user_id,
            gpu_result.get("min_speakers"),
            gpu_result.get("max_speakers"),
            gpu_result.get("num_speakers"),
        )
        rediarize_task.apply_async(
            kwargs={
                "file_uuid": str(gpu_result["file_uuid"]),
                "min_speakers": speaker_range.min_speakers,
                "max_speakers": speaker_range.max_speakers,
                "num_speakers": speaker_range.num_speakers,
                "downstream_tasks": gpu_result.get("downstream_tasks"),
                "pipeline_completion": True,
                "pipeline_task_id": gpu_result["task_id"],
                "engine": post_stage_engine(plan.diarization.engine),
                "merge": plan.diarization.merge,
                "diarizer_config_uuid": plan.diarization.config_uuid,
            },
            queue=plan.diarization.queue,
        )
    except Exception as e:
        logger.warning(f"Failed to dispatch the speaker-detection post stage: {e}")
        return False
    logger.info(
        "Dispatched %s speaker detection (%s) for file %s on %s; plan warnings: %s",
        plan.diarization.engine,
        plan.diarization.execution,
        file_id,
        plan.diarization.queue,
        issues or "none",
    )
    return True


def _dispatch_embedding_stage(gpu_result: dict) -> bool:
    """Queue the voiceprint stage on ``cpu`` for speakers that came without vectors.

    A cloud transcription that returned speakers (``inline_asr``) or a cloud detector run
    beside it (``parallel_cloud``). It completes, notifies and meters the run. Returns True
    when queued: it then owns the temp WAV.
    """
    from app.tasks.speaker_embedding_task import extract_speaker_embeddings_task

    file_id, user_id = gpu_result["file_id"], gpu_result["user_id"]
    if not voiceprints_allowed(file_id):
        # Planned before the switch flipped. Not handed off, so the caller completes and
        # meters the run itself, exactly as when the stage could not be queued.
        logger.info("Voiceprints are off by policy for file %s; no voiceprint stage", file_id)
        return False
    send_progress_notification(
        user_id,
        file_id,
        0.80,
        "Dispatching speaker embedding extraction",
        stage=TranscriptionStage.SPEAKERS,
    )
    try:
        extract_speaker_embeddings_task.apply_async(
            args=[str(gpu_result["file_uuid"])],
            kwargs={"pipeline_task_id": gpu_result["task_id"]},
            queue=CeleryQueues.CPU,
        )
    except Exception as e:
        logger.warning(f"Failed to dispatch speaker embedding task: {e}")
        return False
    logger.info(f"Dispatched the voiceprint stage to the CPU queue for file {file_id}")
    return True


def _fire_completion_metering(
    db, file_id: int, run_id: str, provider: str, success: bool = True
) -> None:
    """Fire the transcription-complete hook (cloud-edition metering seam).

    Builds a ``CompletionContext`` from the finalized ``MediaFile`` and runs the
    registered completion hooks. In the community edition no hook is registered,
    so this is a no-op; the cloud layer registers a usage-event/metering hook.

    Best-effort: wrapped in try/except so a metering failure can never fail a
    successfully finished transcription (``fire_transcription_complete`` already
    contains per-hook failures, but the surrounding DB read is guarded too).

    Args:
        db: Active SQLAlchemy session (the same one that marked the task complete).
        file_id: Internal media file id of the finalized file.
        run_id: Stable pipeline run id (the dispatch ``task_id``) — idempotency scope.
        provider: ASR provider used ("local" or a cloud-ASR provider name).
        success: Whether the run completed successfully.
    """
    try:
        from app.models.media import MediaFile as _MediaFile

        from .hooks import CompletionContext
        from .hooks import fire_transcription_complete

        media_file = db.query(_MediaFile).filter(_MediaFile.id == file_id).first()
        fire_transcription_complete(
            CompletionContext(
                file_id=file_id,
                file_uuid=str(media_file.uuid) if media_file else "",
                user_id=int(media_file.user_id) if media_file else 0,
                organization_id=media_file.organization_id if media_file else None,
                audio_duration_s=(
                    float(media_file.duration) if media_file and media_file.duration else 0.0
                ),
                run_id=run_id,
                provider=provider or "local",
                success=success,
            )
        )
    except Exception as e:  # pragma: no cover - metering must never break the pipeline
        logger.warning(f"Completion metering hook failed for file {file_id} (contained): {e}")


def _schedule_timing_tail_flush(task_id: str, file_id: int, user_id: int) -> None:
    """Re-persist the timing row once the enrichment tail has settled."""
    if not benchmark_timing.benchmark_enabled():
        return
    try:
        flush_pipeline_timing_tail.apply_async(
            args=[task_id, file_id, user_id],
            countdown=TIMING_TAIL_FLUSH_DELAY_S,
        )
    except Exception as e:  # pragma: no cover — instrumentation must not break the pipeline
        logger.debug(f"Timing tail flush not scheduled for file {file_id}: {e}")


@celery_app.task(name="pipeline_timing.flush_tail", priority=UtilityPriority.BACKGROUND)
def flush_pipeline_timing_tail(task_id: str, file_id: int, user_id: int) -> dict:
    """Fold the enrichment tail's markers into the durable timing row.

    Without this the row only ever describes the part of the job the user waits for, and the
    work that follows — indexing, clustering, summary, redaction — is invisible in telemetry
    even though every marker is sitting in Redis.
    """
    _persist_timing_row(task_id, file_id, user_id)
    return {"status": "flushed", "file_id": file_id}


def _persist_timing_row(task_id: str, file_id: int, user_id: int) -> None:
    """Flush the ``benchmark:{task_id}`` Redis hash into file_pipeline_timing.

    Best-effort: instrumentation failures must never break the pipeline. No-op
    when benchmark timing is disabled (the hash will be empty anyway).
    """
    if not benchmark_timing.benchmark_enabled():
        return
    try:
        from app.services.pipeline_timing_service import record_pipeline_timing

        record_pipeline_timing(task_id=task_id, file_id=file_id, user_id=user_id)
    except Exception as e:
        logger.debug(f"Failed to persist pipeline timing row for {task_id}: {e}")


@celery_app.task(
    name="transcription.enrich_and_dispatch",
    priority=CPUPriority.SYSTEM,
    max_retries=2,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    ignore_result=True,
)
def enrich_and_dispatch(
    file_id: int,
    file_uuid: str,
    user_id: int,
    downstream_tasks: list[str] | None = None,
    pipeline_task_id: str | None = None,
) -> None:
    """Fire-and-forget: search indexing + downstream task dispatch, then the plan.

    Runs after the transcript is committed. Every dispatch decision (queued, skipped by a
    setting, failed to queue) is collected and reported as ONE post-processing plan, which
    opens the run the notification card waits on; each task then reports its own outcome.
    A task that is not dispatched at all is absent from the plan, so nothing waits for it.

    ``pipeline_task_id`` is the upstream application task_id; when supplied
    it is propagated into child tasks so their ``_start``/``_end`` benchmark
    markers land in the same ``benchmark:{task_id}`` Redis hash.
    """
    run_at = time.time()
    plan: dict[PostProcessingTask, PostProcessingState] = {
        PostProcessingTask.SEARCH_INDEXING: _index_transcript(
            file_id, file_uuid, user_id, pipeline_task_id=pipeline_task_id
        )
    }

    # Deterministic ingest artifacts (facts / extractive digest / keyphrases). NOT gated
    # on an LLM provider — this is the tier that exists precisely so a deployment without
    # one still has a summary and exact per-file statistics (#403 D6).
    from app.tasks.ingest_artifacts_task import dispatch_file_facts

    plan[PostProcessingTask.FILE_FACTS] = (
        PostProcessingState.QUEUED
        if dispatch_file_facts(file_id, pipeline_task_id=pipeline_task_id)
        else PostProcessingState.FAILED
    )

    # Content redaction detection — gated on the owner's effective config (opt-out by
    # default); detection otherwise happens lazily on first open. Dedicated CPU service;
    # masking applies at read time once spans land.
    plan[PostProcessingTask.REDACTION] = _dispatch_redaction(
        file_id, user_id, pipeline_task_id=pipeline_task_id
    )

    # Downstream tasks — each wrapped individually so one failure
    # doesn't prevent the others from dispatching
    logger.info(f"Dispatching downstream enrichment tasks for file {file_id}")

    try:
        from .core import trigger_automatic_summarization

        plan.update(
            trigger_automatic_summarization(file_id, file_uuid, tasks_to_run=downstream_tasks)
        )
    except Exception as e:
        logger.warning(f"Summarization dispatch failed for file {file_id}: {e}")

    plan.update(_dispatch_speaker_attributes(file_uuid, user_id, downstream_tasks))
    clustering = _dispatch_speaker_clustering(file_uuid, user_id, downstream_tasks, file_id=file_id)
    if clustering is not None:
        plan[PostProcessingTask.SPEAKER_CLUSTERING] = clustering

    ref = post_processing_status.resolve_file(file_id=file_id)
    if ref is not None:
        post_processing_status.report_plan(ref, plan, run_at=run_at)


def _process_native_embeddings(
    file_id: int,
    user_id: int,
    task_id: str,
    native_embeddings_serialized: dict,
    speaker_mapping: dict,
) -> None:
    """Process speaker embeddings using native PyAnnote centroids (no GPU)."""
    from app.services.permission_service import PermissionService
    from app.services.speaker_matching_service import SpeakerMatchingService

    if not voiceprints_allowed(file_id):
        logger.info("Voiceprints are off by policy for file %s; no speaker matching", file_id)
        return

    step_start = time.perf_counter()

    db_embeddings: dict[int, np.ndarray] = {}
    for label, emb_list in native_embeddings_serialized.items():
        db_id = speaker_mapping.get(label)
        if db_id is not None:
            db_embeddings[db_id] = np.array(emb_list)

    if not db_embeddings:
        return

    with session_scope() as db:
        accessible_ids = PermissionService.get_accessible_profile_ids_for_file(db, user_id, file_id)
        matching_service = SpeakerMatchingService(db, embedding_service=None)
        logger.info(
            f"Starting native speaker matching for {len(db_embeddings)} speakers "
            f"(dim={next(iter(db_embeddings.values())).shape[0]})"
        )
        matching_service.process_speaker_embeddings_native(
            media_file_id=file_id,
            user_id=user_id,
            native_embeddings=db_embeddings,
            accessible_profile_ids=accessible_ids,
        )
        record_inline_voiceprints(db, file_id, next(iter(db_embeddings.values())).shape[0])
        update_task_status(db, task_id, "in_progress", progress=0.85)

    logger.info(
        f"TIMING: native speaker matching completed in {time.perf_counter() - step_start:.3f}s"
    )


def _store_v4_centroids(
    file_id: int,
    file_uuid: str,
    user_id: int,
    native_embeddings_serialized: dict,
    speaker_mapping: dict,
) -> None:
    """Store native centroids in v4 staging index (fire-and-forget).

    The policy gate lives in ``_store_native_centroids_in_v4_staging``, the one function every
    caller of the staging write goes through.
    """
    from .core import TranscriptionContext
    from .core import _store_native_centroids_in_v4_staging

    native_embs = {
        label: np.array(emb_list) for label, emb_list in native_embeddings_serialized.items()
    }

    ctx = TranscriptionContext(
        task_id="",
        file_id=file_id,
        file_uuid=file_uuid,
        user_id=user_id,
        file_path="",
        file_name="",
        content_type="",
    )

    try:
        _store_native_centroids_in_v4_staging(ctx, native_embs, speaker_mapping)
    except Exception as e:
        logger.warning(f"v4 staging error (non-fatal): {e}")


def _index_transcript(
    file_id: int, file_uuid: str, user_id: int, pipeline_task_id: str | None = None
) -> PostProcessingState:
    """Dispatch OpenSearch indexing for the completed transcript.

    Phase 2 PR #5: the full-document index used to be built inline on the
    CPU worker here (adding 200-400 ms to the postprocess critical path).
    It now runs inside ``index_transcript_search_task`` on the embedding
    worker together with the chunk-level index, so CPU postprocess can
    return as soon as speaker matching finishes.
    """
    try:
        from app.tasks.search_indexing_task import index_transcript_search_task

        index_transcript_search_task.delay(
            file_id=file_id,
            file_uuid=str(file_uuid),
            user_id=user_id,
            pipeline_task_id=pipeline_task_id,
        )
        logger.info(f"Dispatched search indexing task for file {file_uuid}")
        return PostProcessingState.QUEUED
    except Exception as e:
        logger.warning(f"Failed to dispatch search indexing: {e}")
        return PostProcessingState.FAILED


def _dispatch_redaction(
    file_id: int, user_id: int, pipeline_task_id: str | None = None
) -> PostProcessingState:
    """Dispatch content-redaction detection — ONLY when the owner has redaction enabled
    (or an admin forces it). Redaction is opt-out by default, so we skip the (potentially
    expensive) scan for the common case. If a user enables redaction later, detection is
    dispatched lazily the first time they open the file.

    Consults the FILE's own ``organization_id`` for the tenant floor (issue #982/
    #987) — this pipeline callback carries only ``file_id``/``user_id``, no request
    context, and the file's own tenant is what the floor governs (#988).
    """
    try:
        from app.db.session_utils import session_scope
        from app.models.media import MediaFile
        from app.services.redaction.config import resolve_effective_config

        with session_scope() as db:
            organization_id = (
                db.query(MediaFile.organization_id).filter(MediaFile.id == file_id).scalar()
            )
            cfg = resolve_effective_config(db, user_id, organization_id=organization_id)
        if not cfg.enabled:
            logger.info(f"Redaction off for owner of file {file_id}; skipping detection")
            return PostProcessingState.SKIPPED

        from app.tasks.redaction_task import redaction_detect_task

        redaction_detect_task.delay(
            file_id=file_id,
            user_id=user_id,
            pipeline_task_id=pipeline_task_id,
        )
        logger.info(f"Dispatched redaction detection for file {file_id}")
        return PostProcessingState.QUEUED
    except Exception as e:
        logger.warning(f"Failed to dispatch redaction detection: {e}")
        return PostProcessingState.FAILED


def _dispatch_speaker_attributes(
    file_uuid: str, user_id: int, downstream_tasks: list[str] | None
) -> dict[PostProcessingTask, PostProcessingState]:
    """Dispatch speaker attribute detection and LLM speaker ID (fire-and-forget).

    LLM speaker ID chains from attribute detection (dispatched at the end of
    detect_speaker_attributes_task); with detection off it is queued directly, so the two
    features stay independent (issue #1148). Either way the LLM step's own dispatcher
    reports it, so only the attribute step is returned for the plan.
    """
    try:
        from app.tasks.speaker_attribute_task import dispatch_speaker_attribute_pipeline

        queued = dispatch_speaker_attribute_pipeline(
            str(file_uuid),
            user_id,
            llm_requested=downstream_tasks is not None and "speaker_llm" in downstream_tasks,
        )
    except Exception as e:
        logger.warning(f"Failed to dispatch speaker attribute detection: {e}")
        return {PostProcessingTask.SPEAKER_ATTRIBUTES: PostProcessingState.FAILED}
    state = PostProcessingState.QUEUED if queued == "attributes" else PostProcessingState.SKIPPED
    return {PostProcessingTask.SPEAKER_ATTRIBUTES: state}


def _dispatch_speaker_clustering(
    file_uuid: str, user_id: int, downstream_tasks: list[str] | None, *, file_id: int
) -> PostProcessingState | None:
    """Dispatch speaker clustering (fire-and-forget).

    Returns None when the downstream stage list owns the dispatch (and reports it).
    Clusters are built from voiceprints, so none is queued while the policy forbids them.
    """
    if downstream_tasks is not None and "speaker_clustering" in downstream_tasks:
        return None
    if not voiceprints_allowed(file_id):
        logger.info("Voiceprints are off by policy for file %s; no speaker clustering", file_id)
        return PostProcessingState.SKIPPED
    try:
        from app.tasks.speaker_clustering import cluster_speakers_for_file

        cluster_speakers_for_file.delay(str(file_uuid), user_id)
        logger.info(f"Dispatched speaker clustering for {file_uuid}")
        return PostProcessingState.QUEUED
    except Exception as e:
        logger.warning(f"Failed to dispatch speaker clustering: {e}")
        return PostProcessingState.FAILED


def _cleanup_temp(file_uuid: str | None) -> None:
    """Clean up MinIO temp audio (best-effort)."""
    if not file_uuid:
        return
    try:
        from app.services.minio_service import cleanup_temp_audio

        cleanup_temp_audio(file_uuid)
    except Exception as e:
        logger.debug(f"Temp cleanup failed (non-fatal): {e}")
