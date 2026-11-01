"""
Speaker embedding extraction task.

Handles voice embedding extraction after transcription completes (routed to
the CPU queue — ``SpeakerEmbeddingService`` resolves its device via
``hardware_detection.py`` and needs no GPU diarization pass, see issue #584).
Embedding updates when segments are manually reassigned to different speakers
live in ``speaker_embedding_reassignment_task``.
"""

import contextlib
import logging

from app.core.celery import celery_app
from app.core.queues import GPUPriority
from app.db.session_utils import session_scope
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.services.file_provenance import record_stage
from app.services.file_provenance import voiceprint_facts
from app.services.privacy.gates import voiceprints_allowed
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
    from app.services.uuid_helpers import get_file_by_uuid
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status

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
    from app.services.temp_audio_probe import temp_audio_available
    from app.tasks.transcription.audio_processor import get_audio_file_extension
    from app.tasks.transcription.audio_processor import prepare_audio_for_transcription

    audio_file_path = os.path.join(temp_dir, "audio.wav")
    if temp_audio_available(file_uuid, caller="speaker embeddings"):
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


def _skip_for_policy(
    file_uuid: str,
    task_id: str,
    inputs: dict,
    pipeline_task_id: str | None,
    closes_pipeline: bool,
) -> dict:
    """Finish the voiceprint stage without computing anything because policy forbids it.

    Does what the normal tail does that is not about voiceprints: completes this task row,
    completes and meters the parent run when this stage is its terminus, tells the user, and
    releases the temp audio.
    """
    from app.services.minio_service import cleanup_temp_audio
    from app.utils.task_utils import update_task_status

    file_id = inputs["file_id"]
    logger.info("Voiceprints are off by policy for file %s; skipping the voiceprint stage", file_id)
    with session_scope() as db:
        update_task_status(db, task_id, "completed", progress=1.0, completed=True)
        if closes_pipeline:
            _complete_and_meter_parent(db, file_id, task_id, pipeline_task_id)
    try:
        from app.tasks.transcription.notifications import send_completion_notification

        send_completion_notification(inputs["user_id"], file_id)
    except Exception as notify_err:
        logger.warning(f"Failed to send completion notification: {notify_err}")
    with contextlib.suppress(Exception):
        cleanup_temp_audio(file_uuid)
    _close_pipeline_task(pipeline_task_id if closes_pipeline else None)
    return {"status": "skipped", "reason": "voiceprints_disabled_by_policy"}


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

        if not voiceprints_allowed(inputs["file_id"]):
            # Dispatched before the switch flipped (or by a path that does not consult the
            # plan). Nothing is extracted, matched or stored, but the run still completes:
            # this stage may be its terminus.
            return _skip_for_policy(file_uuid, task_id, inputs, pipeline_task_id, closes_pipeline)

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
