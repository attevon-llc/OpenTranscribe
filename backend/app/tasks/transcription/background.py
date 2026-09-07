"""CPU-bound post-GPU work, run off the GPU worker thread.

Everything here executes in a daemon thread started by
:func:`finalize._process_transcription_result` so the GPU worker can pick
up the next task immediately.
"""

import logging

from app.core.constants import CeleryQueues
from app.db.session_utils import get_refreshed_object
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services.opensearch_service import index_transcript
from app.utils.task_utils import update_task_status

from .context import TranscriptionContext
from .downstream import trigger_automatic_summarization
from .embeddings import _run_speaker_embeddings_with_retry
from .embeddings import _should_use_native_embeddings
from .embeddings import _store_native_centroids_in_v4_staging
from .notifications import send_completion_notification
from .notifications import send_progress_notification
from .storage import generate_full_transcript
from .storage import get_unique_speaker_names

logger = logging.getLogger(__name__)


def _index_transcript_in_search(ctx: TranscriptionContext, processed_segments: list) -> None:
    """Index transcript in OpenSearch with chunk-level embeddings and legacy whole-doc."""
    full_transcript = generate_full_transcript(processed_segments)
    speaker_names = get_unique_speaker_names(processed_segments)

    with session_scope() as db:
        media_file = get_refreshed_object(db, MediaFile, ctx.file_id)
        file_title = (
            (media_file.title or media_file.filename) if media_file else f"File {ctx.file_id}"
        )
        file_uuid = media_file.uuid if media_file else None

    if not file_uuid:
        logger.warning(f"Could not index transcript: file_uuid not found for file_id {ctx.file_id}")
        return

    # Legacy whole-doc index (backward compatibility)
    index_transcript(
        ctx.file_id, file_uuid, ctx.user_id, full_transcript, speaker_names, file_title
    )

    # Dispatch chunk-level search indexing as a separate tracked Celery task
    try:
        from app.tasks.search_indexing_task import index_transcript_search_task

        index_transcript_search_task.delay(
            file_id=ctx.file_id,
            file_uuid=str(file_uuid),
            user_id=ctx.user_id,
        )
        logger.info(f"Dispatched search indexing task for file {file_uuid}")
    except Exception as e:
        logger.warning(f"Failed to dispatch search indexing task for file {file_uuid}: {e}")


def _run_post_gpu_background(
    ctx: TranscriptionContext,
    result: dict,
    audio_file_path: str,
    processed_segments: list,
    speaker_mapping: dict,
    downstream_tasks: list[str] | None,
) -> None:
    """Run CPU-bound post-processing in a background thread.

    This includes speaker embeddings, search indexing, and downstream task
    dispatch. Runs off the GPU worker thread so the next GPU task can start
    immediately.

    All errors are caught and logged — failures here do not affect the
    transcription result (segments are already saved to DB).
    """
    import time

    bg_start = time.perf_counter()
    logger.info(f"Background post-processing started for file {ctx.file_id}")

    try:
        diarization_disabled = result.get("diarization_disabled", False)

        # Speaker embeddings (skip when diarization disabled — single speaker)
        if not diarization_disabled:
            # Choose path based on ASR provider
            is_cloud_asr = result.get("asr_provider") and result.get("asr_provider") != "local"

            if is_cloud_asr:
                # Cloud ASR: no native embeddings available, dispatch CPU task.
                # Embedding extraction from known segments needs no GPU diarization
                # pass, so it belongs on the CPU queue — lite mode runs zero workers
                # on the GPU queue (issue #584).
                send_progress_notification(
                    ctx.user_id, ctx.file_id, 0.78, "Dispatching speaker embedding extraction"
                )
                try:
                    from app.tasks.speaker_embedding_task import extract_speaker_embeddings_task

                    extract_speaker_embeddings_task.apply_async(
                        args=[str(ctx.file_uuid), speaker_mapping],
                        queue=CeleryQueues.CPU,
                    )
                    logger.info(
                        f"Dispatched speaker embedding extraction to CPU queue for "
                        f"cloud-transcribed file {ctx.file_id}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to dispatch speaker embedding task: {e}")
            else:
                # Local ASR: embeddings available inline or via native centroids
                send_progress_notification(
                    ctx.user_id, ctx.file_id, 0.78, "Processing speaker identification"
                )
                _run_speaker_embeddings_with_retry(
                    ctx, result, audio_file_path, processed_segments, speaker_mapping
                )

                # Store native centroids in v4 staging index (fire-and-forget)
                native_embeddings_for_v4 = result.get("native_speaker_embeddings")
                if native_embeddings_for_v4 and not _should_use_native_embeddings(result):
                    try:
                        _store_native_centroids_in_v4_staging(
                            ctx, native_embeddings_for_v4, speaker_mapping
                        )
                    except Exception as e:
                        logger.warning(f"v4 staging: Error (non-fatal): {e}")
        else:
            logger.info(
                f"Skipping speaker embeddings for file {ctx.file_id} (diarization disabled)"
            )

        with session_scope() as db:
            update_task_status(db, ctx.task_id, "in_progress", progress=0.85)

        # Index in search (dispatches as separate Celery task)
        send_progress_notification(ctx.user_id, ctx.file_id, 0.85, "Dispatching search indexing")
        try:
            _index_transcript_in_search(ctx, processed_segments)
        except Exception as e:
            logger.warning(f"Error dispatching search indexing: {e}")

        # Finalize
        send_progress_notification(ctx.user_id, ctx.file_id, 0.95, "Finalizing transcription")
        with session_scope() as db:
            update_task_status(db, ctx.task_id, "completed", progress=1.0, completed=True)
            # Note: media_file.status is already set to COMPLETED by
            # update_media_file_transcription_status in the critical path

            # Deliberately NO metering hook here: this legacy monolithic path is
            # never dispatched (the split pipeline is the only producer), and its
            # cloud-ASR branch dispatches speaker embeddings without a pipeline
            # task id — firing here could double-meter under different run_ids.
            # The LIVE metering termini are postprocess.finalize_transcription
            # (local / disabled diarization), rediarize_task with
            # pipeline_completion=True (cloud ASR + local diarization), and
            # speaker_embedding_task (cloud ASR + provider diarization).

        send_completion_notification(ctx.user_id, ctx.file_id)

        logger.info(
            f"Transcription completed successfully for file {ctx.file_id}, "
            "triggering automatic summarization"
        )
        trigger_automatic_summarization(ctx.file_id, ctx.file_uuid, tasks_to_run=downstream_tasks)

        # Dispatch speaker attribute detection (fire-and-forget, CPU queue)
        speaker_llm_explicit = downstream_tasks is not None and "speaker_llm" in downstream_tasks
        if not speaker_llm_explicit:
            try:
                from app.tasks.speaker_attribute_task import _is_speaker_attribute_detection_enabled
                from app.tasks.speaker_attribute_task import detect_speaker_attributes_task

                if _is_speaker_attribute_detection_enabled(ctx.user_id):
                    detect_speaker_attributes_task.delay(str(ctx.file_uuid), ctx.user_id)
                    logger.info(f"Dispatched speaker attribute detection for {ctx.file_uuid}")
            except Exception as e:
                logger.warning(f"Failed to dispatch speaker attribute detection: {e}")

        # Speaker clustering
        if not downstream_tasks or "speaker_clustering" not in downstream_tasks:
            try:
                from app.tasks.speaker_clustering import cluster_speakers_for_file

                cluster_speakers_for_file.delay(str(ctx.file_uuid), ctx.user_id)
                logger.info(f"Dispatched speaker clustering for {ctx.file_uuid}")
            except Exception as e:
                logger.warning(f"Failed to dispatch speaker clustering: {e}")

    except Exception as e:
        logger.error(f"Background post-processing error for file {ctx.file_id}: {e}")
        # Try to mark task as failed if background processing crashes
        try:
            with session_scope() as db:
                update_task_status(
                    db,
                    ctx.task_id,
                    "failed",
                    error_message=f"Post-processing error: {e}",
                    completed=True,
                )
        except Exception as status_err:
            logger.warning(f"Failed to update task status after bg error: {status_err}")

    elapsed = time.perf_counter() - bg_start
    logger.info(
        f"TIMING: background post-processing completed in {elapsed:.3f}s for file {ctx.file_id}"
    )
