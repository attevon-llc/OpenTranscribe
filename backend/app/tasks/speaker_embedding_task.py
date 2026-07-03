"""
Speaker embedding extraction and reassignment tasks.

Handles GPU-intensive voice embedding extraction after transcription
completes, and embedding updates when segments are manually reassigned
to different speakers.
"""

import logging

from sqlalchemy.orm import Session

from app.core.celery import celery_app
from app.core.constants import GPUPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.utils import benchmark_timing

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="extract_speaker_embeddings", priority=GPUPriority.NEAR_REALTIME)
def extract_speaker_embeddings_task(
    self,
    file_uuid: str,
    speaker_mapping: dict[str, int],
    pipeline_task_id: str | None = None,
):
    """
    Extract speaker embeddings asynchronously after transcription completes.

    This task runs in the background to extract voice embeddings for speaker
    matching, allowing the main transcription to complete faster. It downloads
    the audio file from MinIO and processes it independently.

    Args:
        file_uuid: UUID of the MediaFile
        speaker_mapping: Mapping of speaker labels to database IDs
    """
    import os
    import tempfile

    from app.services.minio_service import cleanup_temp_audio
    from app.services.minio_service import download_file
    from app.services.minio_service import download_temp_audio
    from app.services.minio_service import temp_audio_exists
    from app.services.speaker_embedding_service import SpeakerEmbeddingService
    from app.services.speaker_matching_service import SpeakerMatchingService
    from app.tasks.transcription.audio_processor import get_audio_file_extension
    from app.tasks.transcription.audio_processor import prepare_audio_for_transcription
    from app.utils.hardware_detection import detect_hardware
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status
    from app.utils.uuid_helpers import get_file_by_uuid

    task_id = self.request.id
    benchmark_timing.mark(pipeline_task_id, "speaker_upsert_start")

    with session_scope() as db:
        try:
            media_file = get_file_by_uuid(db, file_uuid)
            if not media_file:
                raise ValueError(f"Media file with UUID {file_uuid} not found")

            file_id = int(media_file.id)
            user_id = int(media_file.user_id)
            storage_path = str(media_file.storage_path)
            content_type = str(media_file.content_type)
            filename = str(media_file.filename)

            create_task_record(db, task_id, user_id, file_id, "speaker_embedding")
            update_task_status(db, task_id, "in_progress", progress=0.1)

            # Force GPU synchronization before loading embedding model
            hardware_config = detect_hardware()
            hardware_config.optimize_memory_usage()
            logger.info("GPU memory synchronized before speaker embedding extraction")

            # Get transcript segments for embedding extraction
            transcript_segments = (
                db.query(TranscriptSegment)
                .filter(TranscriptSegment.media_file_id == file_id)
                .order_by(TranscriptSegment.start_time)
                .all()
            )

            if not transcript_segments:
                logger.warning(f"No transcript segments found for file {file_id}")
                update_task_status(db, task_id, "completed", progress=1.0, completed=True)
                return {"status": "skipped", "message": "No segments to process"}

            # Convert segments to dict format for embedding service
            processed_segments = [
                {
                    "start": seg.start_time,
                    "end": seg.end_time,
                    "text": seg.text,
                    "speaker": seg.speaker.name if seg.speaker else "SPEAKER_00",
                    "speaker_id": seg.speaker_id,
                }
                for seg in transcript_segments
            ]

            update_task_status(db, task_id, "in_progress", progress=0.2)

            # Prefer the preprocessed WAV that preprocess just staged. When
            # the shared scratch volume is mounted this is a hard-link (zero
            # copy); otherwise it's one MinIO fetch of a ~50 MB WAV instead
            # of a full re-download of the original media (Phase 2 PR #4).
            file_ext = get_audio_file_extension(content_type, filename)

            with tempfile.TemporaryDirectory() as temp_dir:
                audio_file_path = os.path.join(temp_dir, "audio.wav")
                used_temp_wav = False
                if temp_audio_exists(file_uuid):
                    try:
                        download_temp_audio(file_uuid, audio_file_path)
                        used_temp_wav = True
                        logger.info("Using preprocessed WAV for speaker embedding extraction")
                    except Exception as temp_err:
                        logger.warning(
                            f"Temp WAV fetch failed ({temp_err}); falling back to original"
                        )

                if not used_temp_wav:
                    logger.info(
                        f"Downloading original {storage_path} for speaker embedding extraction"
                    )
                    file_data, _, _ = download_file(storage_path)
                    temp_file_path = os.path.join(temp_dir, f"input{file_ext}")
                    with open(temp_file_path, "wb") as f:
                        f.write(file_data.read())
                    # Re-run the 16 kHz mono conversion the preprocess task
                    # would otherwise have already done for us.
                    audio_file_path = prepare_audio_for_transcription(
                        temp_file_path, content_type, temp_dir
                    )

                update_task_status(db, task_id, "in_progress", progress=0.4)

                # Initialize embedding service and extract embeddings
                embedding_service = SpeakerEmbeddingService()
                logger.info(
                    f"Using speaker embedding mode: {embedding_service.mode} ({embedding_service.model_name})"
                )

                try:
                    # Compute accessible profiles for cross-user matching
                    from app.services.permission_service import PermissionService

                    accessible_ids = PermissionService.get_accessible_profile_ids(db, user_id)

                    matching_service = SpeakerMatchingService(db, embedding_service)
                    logger.info(
                        f"Starting speaker matching for {len(speaker_mapping)} speakers in file {file_id}"
                    )

                    speaker_results = matching_service.process_speaker_segments(
                        audio_file_path,
                        file_id,
                        user_id,
                        processed_segments,
                        speaker_mapping,
                        accessible_profile_ids=accessible_ids,
                    )

                    update_task_status(db, task_id, "in_progress", progress=0.9)
                    logger.info(
                        f"Speaker matching completed: {len(speaker_results) if speaker_results else 0} results"
                    )

                finally:
                    # Clean up embedding service to free VRAM
                    embedding_service.cleanup()
                    hardware_config.optimize_memory_usage()

            update_task_status(db, task_id, "completed", progress=1.0, completed=True)

            # Also mark the parent transcription task as completed if it was
            # left at in_progress by postprocess (cloud ASR path).
            from app.models.media import Task as TaskModel

            parent_task = (
                db.query(TaskModel)
                .filter(
                    TaskModel.media_file_id == file_id,
                    TaskModel.task_type == "transcription",
                    TaskModel.status == "in_progress",
                )
                .first()
            )
            if parent_task:
                parent_task.status = "completed"
                parent_task.progress = 1.0
                parent_task.completed = True
                db.commit()

                # Cloud-edition seam: a still-in_progress parent transcription
                # task means finalize_transcription deferred completion to us
                # (the cloud-ASR + provider-diarization path) — so THIS task is
                # that run's completion terminus and must fire metering exactly
                # once. The monolithic core.py path and the local-ASR path mark
                # the parent completed themselves and meter inline, leaving no
                # in_progress parent here, so this block (and its metering) is
                # correctly skipped for them. run_id is the stable app-level
                # pipeline task_id threaded in by finalize_transcription (NOT
                # this task's transient celery request id), matching how
                # finalize_transcription/rediarize pass run_id=task_id.
                from app.tasks.rediarize_task import _resolve_asr_provider
                from app.tasks.transcription.postprocess import _fire_completion_metering

                _fire_completion_metering(
                    db,
                    file_id=file_id,
                    run_id=pipeline_task_id or task_id,
                    provider=_resolve_asr_provider(db, file_id),
                    success=True,
                )

            # Send completion notification so frontend updates with speaker labels
            try:
                from app.tasks.transcription.notifications import send_completion_notification

                send_completion_notification(user_id, file_id)
                logger.info(f"Sent completion notification for cloud-transcribed file {file_id}")
            except Exception as notify_err:
                logger.warning(f"Failed to send completion notification: {notify_err}")

            benchmark_timing.mark(pipeline_task_id, "speaker_upsert_end")
            # We were the deferred consumer of the temp WAV — clean it up
            # now that we're done. No-op if postprocess already cleaned it.
            try:
                cleanup_temp_audio(file_uuid)
            except Exception as cleanup_err:
                logger.debug(f"Temp audio cleanup after embedding extraction failed: {cleanup_err}")
            return {
                "status": "success",
                "file_id": file_id,
                "speakers_processed": len(speaker_results) if speaker_results else 0,
            }

        except Exception as e:
            logger.error(f"Error in speaker embedding task for {file_uuid}: {str(e)}")
            logger.error("Full traceback:", exc_info=True)
            update_task_status(db, task_id, "failed", error_message=str(e), completed=True)
            benchmark_timing.mark(pipeline_task_id, "speaker_upsert_end")
            try:
                cleanup_temp_audio(file_uuid)
            except Exception as cleanup_err:
                logger.debug(f"Temp audio cleanup after embedding error failed: {cleanup_err}")
            return {"status": "error", "message": str(e)}


@celery_app.task(
    bind=True, name="update_speaker_embedding_on_reassignment", priority=GPUPriority.INTERACTIVE
)
def update_speaker_embedding_on_reassignment(
    self,
    segment_uuid: str,
    media_file_uuid: str,
    target_speaker_uuid: str,
    source_speaker_uuid: str | None,
    user_id: int,
):
    """
    Update speaker embeddings after a segment is manually reassigned to a different speaker.

    Extracts the voice embedding from the reassigned segment's audio and incorporates
    it into the target speaker's embedding via weighted average. This enables iterative
    speaker profile refinement for difficult-to-match segments.

    Args:
        segment_uuid: UUID of the reassigned transcript segment
        media_file_uuid: UUID of the media file containing the segment
        target_speaker_uuid: UUID of the speaker that received the segment
        source_speaker_uuid: UUID of the speaker that lost the segment (or None if orphan-deleted)
        user_id: ID of the user who owns the data
    """
    import os
    import tempfile

    import numpy as np

    from app.services.minio_service import download_file
    from app.services.opensearch_service import add_speaker_embedding
    from app.services.opensearch_service import get_speaker_document
    from app.services.opensearch_service import update_speaker_segment_count
    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.tasks.transcription.audio_processor import get_audio_file_extension
    from app.tasks.transcription.audio_processor import prepare_audio_for_transcription
    from app.utils.uuid_helpers import get_by_uuid

    with session_scope() as db:
        try:
            # Look up the segment and verify it still belongs to the target speaker
            segment = get_by_uuid(db, TranscriptSegment, segment_uuid)
            if not segment:
                logger.warning(f"Segment {segment_uuid} not found, skipping embedding update")
                return {"status": "skipped", "reason": "segment_not_found"}

            target_speaker = get_by_uuid(db, Speaker, target_speaker_uuid)
            if not target_speaker:
                logger.warning(
                    f"Target speaker {target_speaker_uuid} not found, skipping embedding update"
                )
                return {"status": "skipped", "reason": "target_speaker_not_found"}

            # Guard against race conditions: verify segment still belongs to target speaker
            if segment.speaker_id != target_speaker.id:
                logger.info(
                    f"Segment {segment_uuid} no longer belongs to speaker {target_speaker_uuid} "
                    f"(race condition), skipping"
                )
                return {"status": "skipped", "reason": "segment_reassigned"}

            # Skip segments shorter than 0.5s (unreliable embeddings)
            duration = float(segment.end_time) - float(segment.start_time)
            if duration < 0.5:
                logger.info(
                    f"Segment {segment_uuid} too short ({duration:.2f}s), skipping embedding update"
                )
                return {"status": "skipped", "reason": "segment_too_short"}

            # Get the media file for audio download
            media_file = db.query(MediaFile).filter(MediaFile.id == segment.media_file_id).first()
            if not media_file:
                logger.error(f"Media file not found for segment {segment_uuid}")
                return {"status": "error", "reason": "media_file_not_found"}

            storage_path = str(media_file.storage_path)
            content_type = str(media_file.content_type)
            filename = str(media_file.filename)

            # Download audio from MinIO and extract embedding
            logger.info(
                f"Extracting embedding for segment {segment_uuid} "
                f"(speaker {target_speaker_uuid}, {duration:.1f}s)"
            )
            file_data, _, _ = download_file(storage_path)
            file_ext = get_audio_file_extension(content_type, filename)

            new_embedding = None
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_file_path = os.path.join(temp_dir, f"input{file_ext}")
                with open(temp_file_path, "wb") as f:
                    f.write(file_data.read())

                audio_file_path = prepare_audio_for_transcription(
                    temp_file_path, content_type, temp_dir
                )

                # Use cached embedding service for warm model reuse
                embedding_service = get_cached_embedding_service()

                embedding_result = embedding_service.extract_embedding_from_file(
                    audio_file_path,
                    {"start": float(segment.start_time), "end": float(segment.end_time)},
                )

                if embedding_result is not None:
                    new_embedding = np.array(embedding_result)
                else:
                    logger.warning(f"Failed to extract embedding for segment {segment_uuid}")
                    return {"status": "error", "reason": "embedding_extraction_failed"}

            # Update target speaker embedding via weighted average
            existing_doc = get_speaker_document(target_speaker_uuid)

            if existing_doc is None:
                # New speaker with no existing embedding — store directly
                logger.info(
                    f"Storing initial embedding for speaker {target_speaker_uuid} "
                    f"from segment {segment_uuid}"
                )
                add_speaker_embedding(
                    speaker_id=int(target_speaker.id),
                    speaker_uuid=target_speaker_uuid,
                    user_id=user_id,
                    name=str(target_speaker.name),
                    embedding=new_embedding.tolist(),
                    profile_id=(
                        int(target_speaker.profile_id) if target_speaker.profile_id else None
                    ),
                    profile_uuid=(
                        str(target_speaker.profile.uuid) if target_speaker.profile else None
                    ),
                    media_file_id=int(target_speaker.media_file_id),
                    segment_count=1,
                    display_name=(
                        str(target_speaker.display_name) if target_speaker.display_name else None
                    ),
                )
            else:
                # Weighted average: (old * count + new) / (count + 1), then L2 normalize
                old_embedding = np.array(existing_doc["embedding"])
                old_count = existing_doc["segment_count"]
                new_count = old_count + 1

                weighted = (old_embedding * old_count + new_embedding) / new_count
                norm = np.linalg.norm(weighted)
                if norm > 0:
                    weighted = weighted / norm

                logger.info(
                    f"Updating speaker {target_speaker_uuid} embedding: "
                    f"segment_count {old_count} -> {new_count}"
                )
                add_speaker_embedding(
                    speaker_id=int(target_speaker.id),
                    speaker_uuid=target_speaker_uuid,
                    user_id=user_id,
                    name=str(target_speaker.name),
                    embedding=weighted.tolist(),
                    profile_id=(
                        int(target_speaker.profile_id) if target_speaker.profile_id else None
                    ),
                    profile_uuid=(
                        str(target_speaker.profile.uuid) if target_speaker.profile else None
                    ),
                    media_file_id=int(target_speaker.media_file_id),
                    segment_count=new_count,
                    display_name=(
                        str(target_speaker.display_name) if target_speaker.display_name else None
                    ),
                )

            # Update source speaker segment_count (if it still exists)
            if source_speaker_uuid:
                source_speaker = get_by_uuid(db, Speaker, source_speaker_uuid)
                if source_speaker:
                    source_doc = get_speaker_document(source_speaker_uuid)
                    if source_doc and source_doc["segment_count"] > 1:
                        update_speaker_segment_count(
                            source_speaker_uuid, source_doc["segment_count"] - 1
                        )
                        logger.info(
                            f"Decremented source speaker {source_speaker_uuid} segment_count"
                        )

            # Update profile embeddings if either speaker has a profile_id
            _update_affected_profiles(db, target_speaker, source_speaker_uuid)

            logger.info(
                f"Successfully updated embeddings after segment {segment_uuid} "
                f"reassignment to speaker {target_speaker_uuid}"
            )
            return {"status": "success", "target_speaker_uuid": target_speaker_uuid}

        except Exception as e:
            logger.error(
                f"Error updating speaker embedding on reassignment: {type(e).__name__}: {e}"
            )
            logger.error("Full traceback:", exc_info=True)
            return {"status": "error", "message": str(e)}


def _update_affected_profiles(
    db: Session, target_speaker: Speaker, source_speaker_uuid: str | None
) -> None:
    """Update profile embeddings for speakers affected by a segment reassignment."""
    from app.services.profile_embedding_service import ProfileEmbeddingService

    profile_ids_to_update: set[int] = set()

    if target_speaker.profile_id:
        profile_ids_to_update.add(int(target_speaker.profile_id))

    if source_speaker_uuid:
        from app.utils.uuid_helpers import get_by_uuid

        source_speaker = get_by_uuid(db, Speaker, source_speaker_uuid)
        if source_speaker and source_speaker.profile_id:
            profile_ids_to_update.add(int(source_speaker.profile_id))

    for profile_id in profile_ids_to_update:
        try:
            ProfileEmbeddingService.update_profile_embedding(db, profile_id)
            logger.info(f"Updated profile embedding for profile {profile_id}")
        except Exception as e:
            logger.warning(f"Failed to update profile embedding {profile_id}: {e}")
