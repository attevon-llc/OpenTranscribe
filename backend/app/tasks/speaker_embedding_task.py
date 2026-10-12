"""
Speaker embedding extraction and reassignment tasks.

Handles voice embedding extraction after transcription completes (routed to
the CPU queue — ``SpeakerEmbeddingService`` resolves its device via
``hardware_detection.py`` and needs no GPU diarization pass, see issue #584),
and embedding updates when segments are manually reassigned to different
speakers.
"""

import contextlib
import logging

from app.core.celery import celery_app
from app.core.constants import GPUPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.services.file_provenance import record_stage
from app.services.file_provenance import voiceprint_facts
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


def _close_pipeline_task(pipeline_task_id: str | None, *, error: str | None = None) -> None:
    """Close the TRANSCRIPTION task this embedding run was delegated from.

    ``transcription/postprocess.py`` deliberately leaves the transcription task at
    ``in_progress / progress=0.90`` on the cloud-ASR + provider-diarization path
    and delegates the terminus here. That delegation was only ever honoured for
    the *notification*: ``pipeline_task_id`` was read for metering and benchmark
    marks and nothing else, so the task ROW stayed at 90% forever. The
    notification panel reads its percentage from that row, which is why a fully
    processed file sat at "Processing speaker identification - 90%".

    Every terminal path of the task below must call this, including the early
    "no segments" return and the exception handler - an errored run that leaves
    the row parked is the same bug wearing a different hat.

    Never raises: failing to close a bookkeeping row must not fail the run.
    """
    if not pipeline_task_id:
        return

    from app.utils.task_utils import update_task_status

    with contextlib.suppress(Exception), session_scope() as db:
        if error:
            update_task_status(db, pipeline_task_id, "failed", error_message=error, completed=True)
        else:
            update_task_status(db, pipeline_task_id, "completed", progress=1.0, completed=True)


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


def _extract_aggregated_embeddings(file_uuid: str, task_id: str, inputs: dict) -> dict:
    """Phase 2 — audio staging and the voiceprint extractor. **No DB session is open.**

    Goes through the voiceprint stage's one interface
    (``services/voiceprint_extractor``): audio path + speaker turns in, one L2-normalised
    vector per speaker out. Turns are keyed by the speaker's DB id, so the result maps
    straight back onto this file's speakers whatever produced the turns.
    """
    import tempfile

    from app.services.voiceprint_extractor import SpeakerTurn
    from app.services.voiceprint_extractor import get_voiceprint_extractor
    from app.tasks.transcription.embeddings import _wait_for_migration_lock_to_clear

    turns = [
        SpeakerTurn(speaker=str(seg["speaker_id"]), start=seg["start"], end=seg["end"])
        for seg in inputs["processed_segments"]
        if seg["speaker_id"] is not None
    ]
    # Real pause point for a running v4 migration, which shares the warm model (#657).
    _wait_for_migration_lock_to_clear()
    with tempfile.TemporaryDirectory() as temp_dir:
        audio_file_path = _stage_audio_for_embeddings(file_uuid, inputs, temp_dir)
        _set_task_progress(task_id, 0.4)
        voiceprints = get_voiceprint_extractor().extract(audio_file_path, turns)
    inputs["voiceprint_facts"] = voiceprint_facts(voiceprints)
    logger.info(
        "Voiceprints for file %s: %d speakers, %d-d (%s)",
        inputs["file_id"],
        len(voiceprints.vectors),
        voiceprints.dims,
        voiceprints.model,
    )
    import numpy as np

    return {int(label): np.asarray(vector) for label, vector in voiceprints.vectors.items()}


def _match_and_finalize_embeddings(
    inputs: dict,
    task_id: str,
    aggregated_embeddings: dict,
    pipeline_task_id: str | None,
    closes_pipeline: bool,
) -> int:
    """Phase 3 — profile matching and task completion, in a short write session.

    ``closes_pipeline`` says whether this stage is the run's completion terminus. It is
    when ``finalize_transcription`` deferred completion here (``inline_asr`` /
    ``parallel_cloud``): the parent transcription task is completed and metered exactly
    once, here. It is not after a post stage: ``rediarize_task`` already completed and
    metered the run, so this stage only matches.
    """
    from app.services.permission_service import PermissionService
    from app.services.speaker_matching_service import SpeakerMatchingService
    from app.utils.task_utils import update_task_status

    file_id = inputs["file_id"]
    user_id = inputs["user_id"]

    with session_scope() as db:
        accessible_ids = PermissionService.get_accessible_profile_ids_for_file(db, user_id, file_id)

        # ``embedding_service=None`` because the embeddings arrive already
        # aggregated and normalized from phase 2 — the matching service's only
        # use for it on this path is that aggregation.
        matching_service = SpeakerMatchingService(db, embedding_service=None)
        logger.info(
            f"Starting speaker matching for {len(aggregated_embeddings)} speakers in file {file_id}"
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

        if aggregated_embeddings and inputs.get("voiceprint_facts"):
            record_stage(db, file_id, "voiceprints", **inputs["voiceprint_facts"])
        update_task_status(db, task_id, "in_progress", progress=0.9)
        update_task_status(db, task_id, "completed", progress=1.0, completed=True)

        if closes_pipeline:
            _complete_and_meter_parent(db, file_id, task_id, pipeline_task_id)

    return len(speaker_results) if speaker_results else 0


def _complete_and_meter_parent(db, file_id: int, task_id: str, pipeline_task_id: str | None):
    """Complete the parent transcription task this stage was delegated, and meter it once.

    A still-in_progress parent means ``finalize_transcription`` deferred completion to us,
    so THIS task is that run's completion terminus. ``run_id`` is the stable app-level
    pipeline task id threaded in by ``finalize_transcription`` (never this task's transient
    celery request id), matching how ``finalize_transcription``/``rediarize_task`` meter.
    """
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
    if not parent_task:
        return
    parent_task.status = "completed"
    parent_task.progress = 1.0
    parent_task.completed = True
    db.commit()

    from app.tasks.rediarize_task import _resolve_asr_provider
    from app.tasks.transcription.postprocess import _fire_completion_metering

    _fire_completion_metering(
        db,
        file_id=file_id,
        run_id=pipeline_task_id or task_id,
        provider=_resolve_asr_provider(db, file_id),
        success=True,
    )


@celery_app.task(bind=True, name="extract_speaker_embeddings", priority=GPUPriority.NEAR_REALTIME)
def extract_speaker_embeddings_task(
    self,
    file_uuid: str,
    pipeline_task_id: str | None = None,
    closes_pipeline: bool = True,
):
    """The voiceprint stage: per-speaker vectors for a saved transcript, then matching.

    Runs after ANY speaker detection that did not already hand back vectors (a cloud
    transcription vendor, pyannote.ai, or this server's engine when it returned none), on
    the ``cpu`` queue. The vectors come from the one extractor interface
    (``services/voiceprint_extractor``); matching, storage and clustering do not know which
    implementation produced them.

    Runs in three phases, and the split is load-bearing. Previously the whole body sat
    inside a single ``session_scope``: on a large file that was tens of minutes of Postgres
    backend sitting ``idle in transaction`` holding ACCESS SHARE on ``transcript_segment``
    (queueing every Alembic upgrade, pinning the vacuum horizon, holding a pool
    connection). Now: (1) a short read session returning plain data, (2) audio + model work
    with **no** session held, (3) a short write session for matching and task completion.

    Args:
        file_uuid: UUID of the MediaFile.
        pipeline_task_id: The transcription run this stage belongs to (timing markers, and
            the task it closes when ``closes_pipeline``).
        closes_pipeline: Whether this stage is the run's completion terminus (completes and
            meters the parent task). False after a post stage, which already did both.
    """
    from app.services.minio_service import cleanup_temp_audio
    from app.utils.task_utils import update_task_status

    task_id = self.request.id
    benchmark_timing.mark(pipeline_task_id, "speaker_upsert_start")
    close_id = pipeline_task_id if closes_pipeline else None

    try:
        # Phase 1 — read (DB only, short).
        inputs = _load_speaker_embedding_inputs(file_uuid, task_id)

        if not inputs["processed_segments"]:
            logger.warning(f"No transcript segments found for file {inputs['file_id']}")
            with session_scope() as db:
                update_task_status(db, task_id, "completed", progress=1.0, completed=True)
            _close_pipeline_task(close_id)
            return {"status": "skipped", "message": "No segments to process"}

        _set_task_progress(task_id, 0.2)

        # Phase 2 — audio + extractor. NO DB session is held here.
        aggregated_embeddings = _extract_aggregated_embeddings(file_uuid, task_id, inputs)

        # Phase 3 — write (DB only, short).
        speakers_processed = _match_and_finalize_embeddings(
            inputs, task_id, aggregated_embeddings, pipeline_task_id, closes_pipeline
        )

        # Send completion notification so frontend updates with speaker labels
        try:
            from app.tasks.transcription.notifications import send_completion_notification

            send_completion_notification(inputs["user_id"], inputs["file_id"])
        except Exception as notify_err:
            logger.warning(f"Failed to send completion notification: {notify_err}")

        benchmark_timing.mark(pipeline_task_id, "speaker_upsert_end")
        # We were the deferred consumer of the temp WAV — clean it up
        # now that we're done. No-op if postprocess already cleaned it.
        try:
            cleanup_temp_audio(file_uuid)
        except Exception as cleanup_err:
            logger.debug(f"Temp audio cleanup after embedding extraction failed: {cleanup_err}")
        _close_pipeline_task(close_id)
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
        _close_pipeline_task(close_id, error=str(e))
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
            # The write replaces the whole document: without the file's tenant an org
            # speaker's voiceprint would come back unstamped, i.e. personal.
            organization_id=plan["organization_id"],
        )

        # The segment is no longer the source's (issue #1233): its voice leaves the source's
        # voiceprint by the exact inverse of the weighted add above, so the source's
        # clusters and profile stop averaging audio the user moved to someone else.
        if source_speaker_uuid and plan["source_speaker_exists"]:
            source_doc = get_speaker_document(source_speaker_uuid)
            if source_doc and source_doc["segment_count"] > 1:
                _remove_segment_from_voiceprint(
                    source_speaker_uuid,
                    plan["source_speaker"],
                    source_doc,
                    new_embedding,
                    user_id,
                    organization_id=plan["organization_id"],
                )
                logger.info(f"Removed the moved segment from speaker {source_speaker_uuid}")

        # Phase 4 — every aggregate averaging the shifted voiceprint, in its own session.
        residual = _rebuild_affected_aggregates(plan)
        if residual:
            return {
                "status": "partial",
                "target_speaker_uuid": target_speaker_uuid,
                "cleanup_residual_stages": sorted({str(r["stage"]) for r in residual}),
            }

        logger.info(
            f"Successfully updated embeddings after segment {segment_uuid} "
            f"reassignment to speaker {target_speaker_uuid}"
        )
        return {"status": "success", "target_speaker_uuid": target_speaker_uuid}

    except Exception as e:
        logger.error(f"Error updating speaker embedding on reassignment: {type(e).__name__}: {e}")
        logger.error("Full traceback:", exc_info=True)
        return {"status": "error", "message": str(e)}


def _remove_segment_from_voiceprint(
    speaker_uuid: str,
    source: dict,
    document: dict,
    segment_embedding,
    user_id: int,
    *,
    organization_id: int | None,
) -> None:
    """Take one segment's embedding back out of a voiceprint: ``(old * n - e) / (n - 1)``.

    The inverse of the weighted add the target receives, under the same model (the stored
    vector is the mean of ``segment_count`` segment embeddings). A result too close to
    zero means the voiceprint was little more than that segment; it is left unchanged and
    only the count moves, rather than storing noise as a voice.
    """
    import numpy as np

    from app.services.opensearch_service import add_speaker_embedding
    from app.services.opensearch_service import update_speaker_segment_count

    count = int(document["segment_count"])
    remaining = (np.array(document["embedding"]) * count - np.array(segment_embedding)) / (
        count - 1
    )
    norm = float(np.linalg.norm(remaining))
    if norm < 1e-6:
        update_speaker_segment_count(speaker_uuid, count - 1)
        return
    add_speaker_embedding(
        speaker_id=source["id"],
        speaker_uuid=speaker_uuid,
        user_id=user_id,
        name=source["name"],
        embedding=(remaining / norm).tolist(),
        profile_id=source["profile_id"],
        profile_uuid=source["profile_uuid"],
        media_file_id=source["media_file_id"],
        segment_count=count - 1,
        display_name=source["display_name"],
        organization_id=organization_id,
    )


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

        from app.services.speaker_cleanup import clusters_holding_speakers

        affected_profile_ids: set[int] = set()
        if target_speaker.profile_id:
            affected_profile_ids.add(int(target_speaker.profile_id))
        affected_speaker_ids = {int(target_speaker.id)}

        source_speaker_exists = False
        source: dict | None = None
        if source_speaker_uuid:
            source_speaker = get_by_uuid(db, Speaker, source_speaker_uuid)
            if source_speaker:
                source_speaker_exists = True
                affected_speaker_ids.add(int(source_speaker.id))
                if source_speaker.profile_id:
                    affected_profile_ids.add(int(source_speaker.profile_id))
                source = {
                    "id": int(source_speaker.id),
                    "name": str(source_speaker.name),
                    "profile_id": (
                        int(source_speaker.profile_id) if source_speaker.profile_id else None
                    ),
                    "profile_uuid": (
                        str(source_speaker.profile.uuid) if source_speaker.profile else None
                    ),
                    "media_file_id": int(source_speaker.media_file_id),
                    "display_name": (
                        str(source_speaker.display_name) if source_speaker.display_name else None
                    ),
                }

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
            "source_speaker": source,
            "affected_profile_ids": affected_profile_ids,
            # The target's voiceprint shifts below, and every centroid that averages it
            # shifts with it (issue #1233); read here, while the rows are at hand.
            "affected_cluster_uuids": clusters_holding_speakers(db, affected_speaker_ids),
            "owner_id": int(target_speaker.user_id),
            # Both speakers are of the segment's file, so of its tenant.
            "organization_id": (
                int(media_file.organization_id) if media_file.organization_id is not None else None
            ),
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


def _rebuild_affected_aggregates(plan: dict) -> list[dict]:
    """Rebuild every centroid and profile embedding that averages a shifted voiceprint.

    A reassignment moves the target's voiceprint (drift, not removal): its profile's
    embedding and the centroid of every cluster holding it, or the source, still carry
    the old vector until rebuilt (issue #1233). ``speaker_cleanup`` is the one rebuild
    path, and it returns, rather than swallows, what it could not rebuild (it also logs
    it at ERROR).

    Takes plain data and opens its own short session rather than borrowing the
    caller's, so it cannot extend a transaction that spans slow work.
    """
    from app.services.speaker_cleanup import SpeakerImpact
    from app.services.speaker_cleanup import reconcile_speaker_impact

    impact = SpeakerImpact(
        owner_ids={int(plan["owner_id"])},
        cluster_uuids=set(plan["affected_cluster_uuids"]),
        profile_ids=set(plan["affected_profile_ids"]),
    )
    if impact.is_empty():
        return []
    with session_scope() as db:
        return reconcile_speaker_impact(db, impact)
