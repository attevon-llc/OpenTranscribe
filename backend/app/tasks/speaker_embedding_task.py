"""
Speaker embedding extraction and reassignment tasks.

Handles GPU-intensive voice embedding extraction after transcription
completes, and embedding updates when segments are manually reassigned
to different speakers.
"""

import contextlib
import logging

from app.core.celery import celery_app
from app.core.constants import GPUPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.utils import benchmark_timing

logger = logging.getLogger(__name__)


def _set_task_progress(task_id: str, progress: float) -> None:
    """Stamp task progress in its own sub-millisecond transaction.

    Deliberately a separate scope: the progress marks bracket the slow audio /
    model phase, which must run with no transaction open.
    """
    from app.utils.task_utils import update_task_status

    with session_scope() as db:
        update_task_status(db, task_id, "in_progress", progress=progress)


def _load_speaker_embedding_inputs(file_uuid: str, task_id: str) -> dict:
    """Phase 1 — read everything the extraction phase needs, then close the session.

    Returns plain data only (no ORM instances): an ORM instance that escapes the
    session lazy-loads on first attribute touch, which would silently reopen a
    transaction inside the slow phase and reintroduce the leak this split exists
    to remove.
    """
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
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

        # Column-only select + explicit outer join: the previous full-entity
        # query plus a per-row ``seg.speaker`` lazy load kept ``transcript_segment``
        # under ACCESS SHARE for the whole task.
        segment_rows = (
            db.query(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.text,
                TranscriptSegment.speaker_id,
                Speaker.name,
            )
            .outerjoin(Speaker, Speaker.id == TranscriptSegment.speaker_id)
            .filter(TranscriptSegment.media_file_id == file_id)
            .order_by(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.id,
            )
            .all()
        )

    processed_segments = [
        {
            "start": start_time,
            "end": end_time,
            "text": text,
            "speaker": speaker_name if speaker_id is not None else "SPEAKER_00",
            "speaker_id": speaker_id,
        }
        for start_time, end_time, text, speaker_id, speaker_name in segment_rows
    ]

    return {
        "file_id": file_id,
        "user_id": user_id,
        "storage_path": storage_path,
        "content_type": content_type,
        "filename": filename,
        "processed_segments": processed_segments,
    }


def _stage_audio_for_embeddings(file_uuid: str, inputs: dict, temp_dir: str) -> str:
    """Materialize a 16 kHz WAV for the file in ``temp_dir``. No DB session.

    Prefers the preprocessed WAV the preprocess stage staged (a hard-link on the
    shared scratch volume, otherwise one ~50 MB fetch) over re-downloading the
    original media (Phase 2 PR #4).
    """
    import os

    from app.services.minio_service import download_file
    from app.services.minio_service import download_temp_audio
    from app.services.minio_service import temp_audio_exists
    from app.tasks.transcription.audio_processor import get_audio_file_extension
    from app.tasks.transcription.audio_processor import prepare_audio_for_transcription

    audio_file_path = os.path.join(temp_dir, "audio.wav")
    if temp_audio_exists(file_uuid):
        try:
            download_temp_audio(file_uuid, audio_file_path)
            logger.info("Using preprocessed WAV for speaker embedding extraction")
            return audio_file_path
        except Exception as temp_err:
            logger.warning(f"Temp WAV fetch failed ({temp_err}); falling back to original")

    logger.info(f"Downloading original {inputs['storage_path']} for speaker embedding extraction")
    file_data, _, _ = download_file(inputs["storage_path"])
    file_ext = get_audio_file_extension(inputs["content_type"], inputs["filename"])
    temp_file_path = os.path.join(temp_dir, f"input{file_ext}")
    with open(temp_file_path, "wb") as f:
        f.write(file_data.read())
    # Re-run the 16 kHz mono conversion the preprocess task would otherwise
    # already have done for us.
    return prepare_audio_for_transcription(temp_file_path, inputs["content_type"], temp_dir)


def _extract_aggregated_embeddings(
    file_uuid: str, task_id: str, inputs: dict, speaker_mapping: dict[str, int]
) -> dict:
    """Phase 2 — audio download, ffmpeg conversion and embedding inference.

    **No DB session is open for any of this.** Returns one aggregated,
    L2-normalized embedding per speaker DB id, so the matching phase needs
    neither the audio nor the model.
    """
    import tempfile

    from app.services.speaker_embedding_service import SpeakerEmbeddingService
    from app.utils.hardware_detection import detect_hardware

    hardware_config = detect_hardware()
    hardware_config.optimize_memory_usage()
    logger.info("GPU memory synchronized before speaker embedding extraction")

    embedding_service = SpeakerEmbeddingService()
    logger.info(
        f"Using speaker embedding mode: {embedding_service.mode} ({embedding_service.model_name})"
    )
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_file_path = _stage_audio_for_embeddings(file_uuid, inputs, temp_dir)
            _set_task_progress(task_id, 0.4)
            raw_embeddings = embedding_service.extract_embeddings_for_segments(
                audio_file_path, inputs["processed_segments"], speaker_mapping
            )
        # ``aggregate_embeddings`` mean-pools and L2-normalizes; feeding the
        # result back through the matching service is a no-op re-normalization,
        # so this is the same vector ``process_speaker_segments`` would have
        # matched on.
        return {
            speaker_id: embedding_service.aggregate_embeddings(embeddings)
            for speaker_id, embeddings in raw_embeddings.items()
            if embeddings
        }
    finally:
        # Free VRAM before the matching phase reopens a DB session.
        embedding_service.cleanup()
        hardware_config.optimize_memory_usage()


def _match_and_finalize_embeddings(
    inputs: dict,
    task_id: str,
    aggregated_embeddings: dict,
    speaker_mapping: dict[str, int],
    pipeline_task_id: str | None,
) -> int:
    """Phase 3 — profile matching and task completion, in a short write session."""
    from app.services.permission_service import PermissionService
    from app.services.speaker_matching_service import SpeakerMatchingService
    from app.utils.task_utils import update_task_status

    file_id = inputs["file_id"]
    user_id = inputs["user_id"]

    with session_scope() as db:
        accessible_ids = PermissionService.get_accessible_profile_ids(db, user_id)

        # ``embedding_service=None`` because the embeddings arrive already
        # aggregated and normalized from phase 2 — the matching service's only
        # use for it on this path is that aggregation.
        matching_service = SpeakerMatchingService(db, embedding_service=None)
        logger.info(
            f"Starting speaker matching for {len(speaker_mapping)} speakers in file {file_id}"
        )
        speaker_results = matching_service.process_speaker_embeddings_native(
            media_file_id=file_id,
            user_id=user_id,
            native_embeddings=aggregated_embeddings,
            accessible_profile_ids=accessible_ids,
        )
        logger.info(
            f"Speaker matching completed: {len(speaker_results) if speaker_results else 0} results"
        )

        update_task_status(db, task_id, "in_progress", progress=0.9)
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

    return len(speaker_results) if speaker_results else 0


@celery_app.task(bind=True, name="extract_speaker_embeddings", priority=GPUPriority.NEAR_REALTIME)
def extract_speaker_embeddings_task(
    self,
    file_uuid: str,
    speaker_mapping: dict[str, int],
    pipeline_task_id: str | None = None,
):
    """
    Extract speaker embeddings asynchronously after transcription completes.

    Runs in three phases, and the split is load-bearing. Previously the whole
    body sat inside a single ``session_scope``: it SELECTed every
    ``transcript_segment`` row for the file (full entities, plus a lazy
    ``seg.speaker`` load per row), then downloaded the media from MinIO, ran an
    ffmpeg conversion and loaded/ran the embedding model — all with that
    transaction still open. On a large file that is tens of minutes of Postgres
    backend sitting ``idle in transaction`` holding ACCESS SHARE on
    ``transcript_segment``: it queues every ``ALTER TABLE`` (i.e. any Alembic
    upgrade) behind it, pins the vacuum horizon on the largest table in the
    product, and consumes a pool connection for the duration.

    Now: (1) a short read session returning plain data, (2) audio + model work
    with **no** session held, (3) a short write session for profile matching and
    task completion.

    Args:
        file_uuid: UUID of the MediaFile
        speaker_mapping: Mapping of speaker labels to database IDs
    """
    from app.services.minio_service import cleanup_temp_audio
    from app.utils.task_utils import update_task_status

    task_id = self.request.id
    benchmark_timing.mark(pipeline_task_id, "speaker_upsert_start")

    try:
        # Phase 1 — read (DB only, short).
        inputs = _load_speaker_embedding_inputs(file_uuid, task_id)

        if not inputs["processed_segments"]:
            logger.warning(f"No transcript segments found for file {inputs['file_id']}")
            with session_scope() as db:
                update_task_status(db, task_id, "completed", progress=1.0, completed=True)
            return {"status": "skipped", "message": "No segments to process"}

        _set_task_progress(task_id, 0.2)

        # Phase 2 — audio + model. NO DB session is held here.
        aggregated_embeddings = _extract_aggregated_embeddings(
            file_uuid, task_id, inputs, speaker_mapping
        )

        # Phase 3 — write (DB only, short).
        speakers_processed = _match_and_finalize_embeddings(
            inputs, task_id, aggregated_embeddings, speaker_mapping, pipeline_task_id
        )

        # Send completion notification so frontend updates with speaker labels
        try:
            from app.tasks.transcription.notifications import send_completion_notification

            send_completion_notification(inputs["user_id"], inputs["file_id"])
            logger.info(
                f"Sent completion notification for cloud-transcribed file {inputs['file_id']}"
            )
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
            "file_id": inputs["file_id"],
            "speakers_processed": speakers_processed,
        }

    except Exception as e:
        logger.error(f"Error in speaker embedding task for {file_uuid}: {str(e)}")
        logger.error("Full traceback:", exc_info=True)
        with contextlib.suppress(Exception), session_scope() as db:
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
    import numpy as np

    from app.services.opensearch_service import add_speaker_embedding
    from app.services.opensearch_service import get_speaker_document
    from app.services.opensearch_service import update_speaker_segment_count

    try:
        # Phase 1 — read (DB only, short). Returns plain data; the ORM objects
        # must not outlive this call or their lazy loads would reopen a
        # transaction inside the download/inference phase below.
        plan = _load_reassignment_plan(segment_uuid, target_speaker_uuid, source_speaker_uuid)
        if "skip" in plan:
            return plan["skip"]

        # Phase 2 — MinIO download, ffmpeg conversion, embedding inference.
        # NO DB session is held here.
        logger.info(
            f"Extracting embedding for segment {segment_uuid} "
            f"(speaker {target_speaker_uuid}, {plan['duration']:.1f}s)"
        )
        embedding_result = _extract_segment_embedding(plan)
        if embedding_result is None:
            logger.warning(f"Failed to extract embedding for segment {segment_uuid}")
            return {"status": "error", "reason": "embedding_extraction_failed"}
        new_embedding = np.array(embedding_result)

        # Phase 3 — OpenSearch only; needs no DB session at all.
        target = plan["target_speaker"]
        existing_doc = get_speaker_document(target_speaker_uuid)

        if existing_doc is None:
            # New speaker with no existing embedding — store directly
            logger.info(
                f"Storing initial embedding for speaker {target_speaker_uuid} "
                f"from segment {segment_uuid}"
            )
            embedding_to_store = new_embedding
            segment_count = 1
        else:
            # Weighted average: (old * count + new) / (count + 1), then L2 normalize
            old_embedding = np.array(existing_doc["embedding"])
            old_count = existing_doc["segment_count"]
            segment_count = old_count + 1

            weighted = (old_embedding * old_count + new_embedding) / segment_count
            norm = np.linalg.norm(weighted)
            if norm > 0:
                weighted = weighted / norm
            embedding_to_store = weighted

            logger.info(
                f"Updating speaker {target_speaker_uuid} embedding: "
                f"segment_count {old_count} -> {segment_count}"
            )

        add_speaker_embedding(
            speaker_id=target["id"],
            speaker_uuid=target_speaker_uuid,
            user_id=user_id,
            name=target["name"],
            embedding=embedding_to_store.tolist(),
            profile_id=target["profile_id"],
            profile_uuid=target["profile_uuid"],
            media_file_id=target["media_file_id"],
            segment_count=segment_count,
            display_name=target["display_name"],
        )

        # Update source speaker segment_count (if it still exists)
        if source_speaker_uuid and plan["source_speaker_exists"]:
            source_doc = get_speaker_document(source_speaker_uuid)
            if source_doc and source_doc["segment_count"] > 1:
                update_speaker_segment_count(source_speaker_uuid, source_doc["segment_count"] - 1)
                logger.info(f"Decremented source speaker {source_speaker_uuid} segment_count")

        # Phase 4 — profile embedding refresh, in its own short session.
        _update_affected_profiles(plan["affected_profile_ids"])

        logger.info(
            f"Successfully updated embeddings after segment {segment_uuid} "
            f"reassignment to speaker {target_speaker_uuid}"
        )
        return {"status": "success", "target_speaker_uuid": target_speaker_uuid}

    except Exception as e:
        logger.error(f"Error updating speaker embedding on reassignment: {type(e).__name__}: {e}")
        logger.error("Full traceback:", exc_info=True)
        return {"status": "error", "message": str(e)}


def _load_reassignment_plan(
    segment_uuid: str, target_speaker_uuid: str, source_speaker_uuid: str | None
) -> dict:
    """Read phase for ``update_speaker_embedding_on_reassignment``.

    Returns plain data only. A ``skip`` key carries the early-return result for
    the guard paths (missing rows, races, too-short segments).
    """
    from app.utils.uuid_helpers import get_by_uuid

    with session_scope() as db:
        segment = get_by_uuid(db, TranscriptSegment, segment_uuid)
        if not segment:
            logger.warning(f"Segment {segment_uuid} not found, skipping embedding update")
            return {"skip": {"status": "skipped", "reason": "segment_not_found"}}

        target_speaker = get_by_uuid(db, Speaker, target_speaker_uuid)
        if not target_speaker:
            logger.warning(
                f"Target speaker {target_speaker_uuid} not found, skipping embedding update"
            )
            return {"skip": {"status": "skipped", "reason": "target_speaker_not_found"}}

        # Guard against race conditions: verify segment still belongs to target speaker
        if segment.speaker_id != target_speaker.id:
            logger.info(
                f"Segment {segment_uuid} no longer belongs to speaker {target_speaker_uuid} "
                f"(race condition), skipping"
            )
            return {"skip": {"status": "skipped", "reason": "segment_reassigned"}}

        # Skip segments shorter than 0.5s (unreliable embeddings)
        duration = float(segment.end_time) - float(segment.start_time)
        if duration < 0.5:
            logger.info(
                f"Segment {segment_uuid} too short ({duration:.2f}s), skipping embedding update"
            )
            return {"skip": {"status": "skipped", "reason": "segment_too_short"}}

        media_file = db.query(MediaFile).filter(MediaFile.id == segment.media_file_id).first()
        if not media_file:
            logger.error(f"Media file not found for segment {segment_uuid}")
            return {"skip": {"status": "error", "reason": "media_file_not_found"}}

        affected_profile_ids: set[int] = set()
        if target_speaker.profile_id:
            affected_profile_ids.add(int(target_speaker.profile_id))

        source_speaker_exists = False
        if source_speaker_uuid:
            source_speaker = get_by_uuid(db, Speaker, source_speaker_uuid)
            if source_speaker:
                source_speaker_exists = True
                if source_speaker.profile_id:
                    affected_profile_ids.add(int(source_speaker.profile_id))

        return {
            "duration": duration,
            "segment_start": float(segment.start_time),
            "segment_end": float(segment.end_time),
            "storage_path": str(media_file.storage_path),
            "content_type": str(media_file.content_type),
            "filename": str(media_file.filename),
            "target_speaker": {
                "id": int(target_speaker.id),
                "name": str(target_speaker.name),
                "profile_id": (
                    int(target_speaker.profile_id) if target_speaker.profile_id else None
                ),
                "profile_uuid": (
                    str(target_speaker.profile.uuid) if target_speaker.profile else None
                ),
                "media_file_id": int(target_speaker.media_file_id),
                "display_name": (
                    str(target_speaker.display_name) if target_speaker.display_name else None
                ),
            },
            "source_speaker_exists": source_speaker_exists,
            "affected_profile_ids": affected_profile_ids,
        }


def _extract_segment_embedding(plan: dict):
    """Download the media, convert it, and extract one segment embedding.

    Runs with **no** DB session open — this is the multi-minute phase (a full
    MinIO download of the original media plus an ffmpeg transcode) that used to
    sit inside the task's transaction.
    """
    import os
    import tempfile

    from app.services.minio_service import download_file
    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.tasks.transcription.audio_processor import get_audio_file_extension
    from app.tasks.transcription.audio_processor import prepare_audio_for_transcription

    file_data, _, _ = download_file(plan["storage_path"])
    file_ext = get_audio_file_extension(plan["content_type"], plan["filename"])

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_file_path = os.path.join(temp_dir, f"input{file_ext}")
        with open(temp_file_path, "wb") as f:
            f.write(file_data.read())

        audio_file_path = prepare_audio_for_transcription(
            temp_file_path, plan["content_type"], temp_dir
        )

        # Use cached embedding service for warm model reuse
        embedding_service = get_cached_embedding_service()
        return embedding_service.extract_embedding_from_file(
            audio_file_path,
            {"start": plan["segment_start"], "end": plan["segment_end"]},
        )


def _update_affected_profiles(profile_ids: set[int]) -> None:
    """Refresh profile embeddings for the profiles a reassignment touched.

    Takes plain ids and opens its own short session rather than borrowing the
    caller's, so it cannot extend a transaction that spans slow work.
    """
    from app.services.profile_embedding_service import ProfileEmbeddingService

    if not profile_ids:
        return

    with session_scope() as db:
        for profile_id in profile_ids:
            try:
                ProfileEmbeddingService.update_profile_embedding(db, profile_id)
                logger.info(f"Updated profile embedding for profile {profile_id}")
            except Exception as e:
                logger.warning(f"Failed to update profile embedding {profile_id}: {e}")
