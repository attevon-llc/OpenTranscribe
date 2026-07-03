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

from app.core.celery import celery_app
from app.core.constants import CeleryQueues
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.utils import benchmark_timing
from app.utils.task_utils import update_task_status
from app.utils.websocket_notify import send_ws_event

from .notifications import send_completion_notification
from .notifications import send_progress_notification

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
    # When we dispatch a GPU-side task that still needs the preprocessed
    # WAV (cloud ASR → speaker embedding, or cloud ASR → local rediarize)
    # we MUST NOT clean up the temp audio here — those downstream tasks
    # will clean it up themselves once they finish. Phase 2 PR #4 relies
    # on this for the cloud-ASR embedding path to read from scratch.
    defer_temp_cleanup = False

    try:
        diarization_disabled = gpu_result.get("diarization_disabled", False)
        diarization_source = gpu_result.get("diarization_source", "provider")
        is_cloud_asr = asr_provider and asr_provider != "local"
        needs_local_diarization = (
            is_cloud_asr and diarization_source == "local" and not diarization_disabled
        )

        # Speaker matching must complete before marking "completed" so the
        # frontend shows matched profile names, not generic "Speaker 1" labels.
        if needs_local_diarization:
            # Cloud ASR + local diarization: dispatch rediarize_task to GPU
            # The rediarize task handles everything: diarization, speaker assignment,
            # embedding extraction, and downstream task dispatch.
            send_progress_notification(user_id, file_id, 0.85, "Queuing local speaker diarization")
            try:
                from app.tasks.rediarize_task import rediarize_task

                rediarize_task.apply_async(
                    kwargs={
                        "file_uuid": str(file_uuid),
                        "min_speakers": None,
                        "max_speakers": None,
                        "num_speakers": None,
                        "downstream_tasks": downstream_tasks,
                        # This rediarize completes a fresh billable pipeline run —
                        # it is the metering terminus (we deliberately don't meter
                        # here to avoid double-counting), and it meters under THIS
                        # pipeline's id so the reservation taken at dispatch is
                        # released and replays dedup on a stable key.
                        "pipeline_completion": True,
                        "pipeline_task_id": task_id,
                    },
                    queue=CeleryQueues.GPU,
                )
                # rediarize consumes the temp WAV on the GPU worker; keep
                # the file around until that task finishes its own cleanup.
                defer_temp_cleanup = True
                logger.info(f"Dispatched local rediarization for cloud-transcribed file {file_id}")
            except Exception as e:
                logger.warning(f"Failed to dispatch rediarization: {e}")
        elif not diarization_disabled:
            if is_cloud_asr:
                # Cloud ASR with provider diarization: dispatch GPU embedding extraction
                send_progress_notification(
                    user_id, file_id, 0.80, "Dispatching speaker embedding extraction"
                )
                try:
                    from app.tasks.speaker_embedding_task import extract_speaker_embeddings_task

                    extract_speaker_embeddings_task.apply_async(
                        args=[str(file_uuid), speaker_mapping],
                        kwargs={"pipeline_task_id": task_id},
                        queue=CeleryQueues.GPU,
                    )
                    # Embedding task reads the preprocessed WAV; defer
                    # cleanup until it finishes (it cleans up itself).
                    defer_temp_cleanup = True
                    logger.info(
                        f"Dispatched speaker embedding extraction to GPU queue for "
                        f"cloud-transcribed file {file_id}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to dispatch speaker embedding task: {e}")
            else:
                # Local ASR: process embeddings inline
                send_progress_notification(
                    user_id, file_id, 0.80, "Processing speaker identification"
                )

                if use_native and native_embeddings:
                    _process_native_embeddings(
                        file_id, user_id, task_id, native_embeddings, speaker_mapping
                    )

                # Store v4 centroids if applicable
                if native_embeddings and not use_native:
                    _store_v4_centroids(
                        file_id, file_uuid, user_id, native_embeddings, speaker_mapping
                    )
        else:
            logger.info(f"Skipping speaker embeddings for file {file_id} (diarization disabled)")

        if needs_local_diarization:
            # Cloud ASR + local diarization: mark transcription completed (text is usable),
            # rediarize_task will send its own completion notification after GPU diarization
            send_progress_notification(
                user_id, file_id, 0.90, "Transcription complete, diarizing on GPU..."
            )
            with session_scope() as db:
                update_task_status(db, task_id, "completed", progress=1.0, completed=True)
            send_completion_notification(user_id, file_id)
        elif is_cloud_asr and not diarization_disabled:
            # Cloud ASR with provider diarization: embedding task runs async on GPU
            send_progress_notification(user_id, file_id, 0.90, "Processing speaker identification")
            with session_scope() as db:
                update_task_status(db, task_id, "in_progress", progress=0.90)
            # Completion notification will be sent by extract_speaker_embeddings_task
        else:
            # Local ASR or diarization disabled: embeddings already done — mark completed.
            # This is the single-task terminus for the local pipeline, so fire the
            # metering hook here (the cloud-ASR + local-diarization path meters from
            # rediarize_task instead, to avoid double-counting the same run).
            send_progress_notification(user_id, file_id, 0.95, "Finalizing transcription")
            with session_scope() as db:
                update_task_status(db, task_id, "completed", progress=1.0, completed=True)
                _fire_completion_metering(
                    db, file_id=file_id, run_id=task_id, provider=asr_provider, success=True
                )
            send_completion_notification(user_id, file_id)

        completion_elapsed = time.perf_counter() - post_start
        logger.info(
            f"TIMING: postprocess speaker matching + completion "
            f"in {completion_elapsed:.3f}s for file {file_id}"
        )

        # Dispatch background enrichment (search indexing + downstream tasks).
        # Each task sends its own WebSocket event when done.
        enrichment_tasks = _build_enrichment_task_list(downstream_tasks)

        enrich_and_dispatch.delay(
            file_id=file_id,
            file_uuid=file_uuid,
            user_id=user_id,
            downstream_tasks=downstream_tasks,
            pipeline_task_id=task_id,
        )

        # Notify frontend that background enrichment tasks are running
        send_ws_event(
            user_id,
            "enrichment_started",
            {
                "file_id": str(file_uuid),
                "tasks": enrichment_tasks,
            },
        )

    except Exception as e:
        logger.error(f"Postprocess failed for file {file_id}: {e}")
        try:
            with session_scope() as db:
                update_task_status(
                    db,
                    task_id,
                    "failed",
                    error_message=f"Post-processing error: {e}",
                    completed=True,
                )
        except Exception as task_err:
            logger.debug(f"Failed to mark task as failed: {task_err}")
        # Don't re-raise — segments are already saved by GPU task

        return {
            "status": "error",
            "file_id": file_id,
            "error": str(e),
            "segment_count": gpu_result.get("segment_count", 0),
        }

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

    elapsed = time.perf_counter() - post_start
    logger.info(f"TIMING: postprocess completed in {elapsed:.3f}s for file {file_id}")

    return {
        "status": "success",
        "file_id": file_id,
        "segment_count": gpu_result.get("segment_count", 0),
    }


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


def _build_enrichment_task_list(downstream_tasks: list[str] | None) -> list[str]:
    """Build the list of enrichment task names that will run in background.

    Only includes tasks that send enrichment_task_complete events (chip-worthy).
    Summarization and topic extraction have their own progressive notification
    bars and are NOT listed here.
    """
    tasks = ["search_indexing", "analytics"]
    # Gender detection always runs — LLM speaker ID chains from it
    tasks.append("speaker_attributes")
    tasks.append("speaker_identification")
    if downstream_tasks is None or "speaker_clustering" not in downstream_tasks:
        tasks.append("speaker_clustering")
    return tasks


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
    """Fire-and-forget: search indexing + downstream task dispatch.

    Runs after the main postprocess marks the file as COMPLETED.
    Each downstream task sends its own WebSocket event when done.

    ``pipeline_task_id`` is the upstream application task_id; when supplied
    it is propagated into child tasks so their ``_start``/``_end`` benchmark
    markers land in the same ``benchmark:{task_id}`` Redis hash.
    """
    # Search indexing (invisible to user)
    try:
        _index_transcript(file_id, file_uuid, user_id, pipeline_task_id=pipeline_task_id)
        send_ws_event(
            user_id,
            "enrichment_task_complete",
            {"file_id": str(file_uuid), "task": "search_indexing"},
        )
    except Exception as e:
        logger.warning(f"Search indexing failed for file {file_id}: {e}")

    # Content redaction detection — runs ALWAYS (independent of user settings).
    # Dedicated CPU service; masking applies at read time once spans land.
    try:
        _dispatch_redaction(file_id, user_id, pipeline_task_id=pipeline_task_id)
    except Exception as e:
        logger.warning(f"Redaction dispatch failed for file {file_id}: {e}")

    # Downstream tasks — each wrapped individually so one failure
    # doesn't prevent the others from dispatching
    logger.info(f"Dispatching downstream enrichment tasks for file {file_id}")

    try:
        from .core import trigger_automatic_summarization

        trigger_automatic_summarization(file_id, file_uuid, tasks_to_run=downstream_tasks)
    except Exception as e:
        logger.warning(f"Summarization dispatch failed for file {file_id}: {e}")

    try:
        _dispatch_speaker_attributes(file_uuid, user_id, downstream_tasks)
    except Exception as e:
        logger.warning(f"Speaker attribute dispatch failed for file {file_id}: {e}")

    try:
        _dispatch_speaker_clustering(file_uuid, user_id, downstream_tasks)
    except Exception as e:
        logger.warning(f"Speaker clustering dispatch failed for file {file_id}: {e}")


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

    step_start = time.perf_counter()

    db_embeddings: dict[int, np.ndarray] = {}
    for label, emb_list in native_embeddings_serialized.items():
        db_id = speaker_mapping.get(label)
        if db_id is not None:
            db_embeddings[db_id] = np.array(emb_list)

    if not db_embeddings:
        return

    with session_scope() as db:
        accessible_ids = PermissionService.get_accessible_profile_ids(db, user_id)
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
    """Store native centroids in v4 staging index (fire-and-forget)."""
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
) -> None:
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
    except Exception as e:
        logger.warning(f"Failed to dispatch search indexing: {e}")


def _dispatch_redaction(file_id: int, user_id: int, pipeline_task_id: str | None = None) -> None:
    """Dispatch content-redaction detection — ONLY when the owner has redaction enabled
    (or an admin forces it). Redaction is opt-out by default, so we skip the (potentially
    expensive) scan for the common case. If a user enables redaction later, detection is
    dispatched lazily the first time they open the file.
    """
    try:
        from app.db.session_utils import session_scope
        from app.services.redaction.config import resolve_effective_config

        with session_scope() as db:
            cfg = resolve_effective_config(db, user_id)
        if not cfg.enabled:
            logger.info(f"Redaction off for owner of file {file_id}; skipping detection")
            return

        from app.tasks.redaction_task import redaction_detect_task

        redaction_detect_task.delay(
            file_id=file_id,
            user_id=user_id,
            pipeline_task_id=pipeline_task_id,
        )
        logger.info(f"Dispatched redaction detection for file {file_id}")
    except Exception as e:
        logger.warning(f"Failed to dispatch redaction detection: {e}")


def _dispatch_speaker_attributes(
    file_uuid: str, user_id: int, downstream_tasks: list[str] | None
) -> None:
    """Dispatch speaker attribute detection (fire-and-forget).

    Always runs when transcription completes — gender detection is part of the
    standard pipeline. LLM speaker ID chains from gender (dispatched at the end
    of detect_speaker_attributes_task).
    """
    try:
        from app.tasks.speaker_attribute_task import _is_speaker_attribute_detection_enabled
        from app.tasks.speaker_attribute_task import detect_speaker_attributes_task

        if _is_speaker_attribute_detection_enabled(user_id):
            detect_speaker_attributes_task.delay(str(file_uuid), user_id)
            logger.info(f"Dispatched speaker attribute detection for {file_uuid}")
    except Exception as e:
        logger.warning(f"Failed to dispatch speaker attribute detection: {e}")


def _dispatch_speaker_clustering(
    file_uuid: str, user_id: int, downstream_tasks: list[str] | None
) -> None:
    """Dispatch speaker clustering (fire-and-forget)."""
    if downstream_tasks is not None and "speaker_clustering" in downstream_tasks:
        return
    try:
        from app.tasks.speaker_clustering import cluster_speakers_for_file

        cluster_speakers_for_file.delay(str(file_uuid), user_id)
        logger.info(f"Dispatched speaker clustering for {file_uuid}")
    except Exception as e:
        logger.warning(f"Failed to dispatch speaker clustering: {e}")


def _cleanup_temp(file_uuid: str | None) -> None:
    """Clean up MinIO temp audio (best-effort)."""
    if not file_uuid:
        return
    try:
        from app.services.minio_service import cleanup_temp_audio

        cleanup_temp_audio(file_uuid)
    except Exception as e:
        logger.debug(f"Temp cleanup failed (non-fatal): {e}")
