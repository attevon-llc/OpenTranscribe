"""The post stage: speaker detection for a transcript that is already saved.

One task, parametrised by the engine the pipeline plan names (``services/pipeline_plan``,
plan C4), serving every combination where speakers are detected AFTER transcription:

* ``post_stage_local`` — this server's engine (diar-native, PyAnnote fallback) after a cloud
  text-only transcription, a per-file CPU model, or Automatic + a text-only timed model;
  routed to ``gpu_preferred_queue()``. Full image only: lite has no local speaker detection
  (#1228), so the plan never produces it there and this task refuses it at the top.
* ``post_stage_cloud`` — pyannote.ai after local Whisper or a CPU model; routed to
  ``cloud-asr`` so no GPU slot is held while the vendor works.
* a user's manual "Re-diarize" (``files/reprocess.py``), with the engine of their current
  choice.

Then the turns are merged into the stored words (per word, or per segment for a
segment-timed model), segments are re-split at speaker changes by ``finalize_segments`` (the
single chokepoint every path goes through), and the voiceprint stage runs exactly once.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from app.core import stage_timing
from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import CeleryQueues
from app.core.constants import GPUPriority
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.services.minio_service import cleanup_temp_audio
from app.services.minio_service import download_file
from app.services.minio_service import download_temp_audio
from app.services.minio_service import temp_audio_exists
from app.tasks.transcription.user_settings import SpeakerRange
from app.tasks.transcription.user_settings import resolve_speaker_range_for_user
from app.utils import benchmark_timing
from app.utils.task_utils import create_task_record
from app.utils.task_utils import update_media_file_status
from app.utils.task_utils import update_task_status

logger = logging.getLogger(__name__)

#: Recorded on the post stage's task row when a cloud speaker detector fails: the transcript
#: is kept and the file completes without speakers (a vendor outage must not fail a file
#: whose transcription succeeded). Fixed text, never the vendor's error.
CLOUD_DIARIZATION_FAILED_MESSAGE = (
    "The cloud speaker detection service did not return a result, so this transcript has no "
    "speakers. Use Re-diarize to try again."
)


def _resolve_asr_provider(db, file_id: int) -> str:
    """Resolve the ASR provider that produced this transcript, for metering.

    Reads ``FilePipelineTiming.asr_provider`` (recorded by the transcription
    pipeline); falls back to "local" when not recorded.
    """
    try:
        from app.models.pipeline_timing import FilePipelineTiming

        row = (
            db.query(FilePipelineTiming.asr_provider)
            .filter(FilePipelineTiming.file_id == file_id)
            .first()
        )
        if row and row.asr_provider:
            return str(row.asr_provider)
    except Exception as e:  # pragma: no cover - best-effort provider lookup
        logger.debug(f"Could not resolve asr_provider for file {file_id}: {e}")
    return "local"


def _load_segments_as_transcript(file_id: int) -> dict:
    """Load existing transcript segments from DB in the pipeline result format.

    Reconstructs the dict format expected by assign_speakers:
    {"segments": [{"text": ..., "start": ..., "end": ..., "words": [...]}]}

    Raises:
        ValueError: If no segments found.
    """
    with session_scope() as db:
        segments = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == file_id)
            .order_by(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.id,
            )
            .all()
        )

        if not segments:
            raise ValueError(f"No transcript segments found for file {file_id}")

        result_segments = []
        for seg in segments:
            segment_dict: dict = {
                "text": seg.text,
                "start": seg.start_time,
                "end": seg.end_time,
            }
            # Preserve word-level timestamps for fine-grained speaker assignment
            if seg.words:
                segment_dict["words"] = [
                    {
                        "word": w.get("word", ""),
                        "start": w["start"],
                        "end": w["end"],
                        "score": w.get("score", 1.0),
                    }
                    for w in seg.words
                    if "start" in w and "end" in w
                ]
            result_segments.append(segment_dict)

    return {"segments": result_segments}


def _prepare_audio(
    storage_path: str,
    content_type: str,
    filename: str,
    file_uuid: str | None = None,
) -> tuple[str, str | None]:
    """Get a 16 kHz WAV on local disk for diarization.

    Returns ``(audio_file_path, temp_dir_to_clean)``. ``temp_dir_to_clean`` is ``None`` when
    ``audio_file_path`` is the scratch-volume WAV itself (issue #661 E2) — the caller must NOT
    rmtree that directory, since it is ``pipeline_scratch``'s ``<file_uuid>/`` namespace, still
    needed by other consumers and cleaned up on its own lifetime by ``scratch_volume.cleanup``.
    It is non-``None`` (and must be rmtree'd by the caller) for every other path.

    Prefers, in order: (1) the scratch-volume WAV already staged by the main pipeline, read
    directly; (2) the preprocessed MinIO-temp WAV; (3) downloading the original from MinIO and
    re-running the FFmpeg conversion.
    """
    from app.tasks.transcription.audio_processor import get_audio_file_extension
    from app.tasks.transcription.audio_processor import prepare_audio_for_transcription
    from app.utils import scratch_volume

    if file_uuid:
        scratch_path = scratch_volume.scratch_audio_path(file_uuid)
        if scratch_volume.is_scratch_available() and scratch_path.exists():
            logger.info("rediarize: reusing scratch-volume WAV directly (no copy)")
            return str(scratch_path), None

    temp_dir = tempfile.mkdtemp(prefix="rediarize_")

    if file_uuid and temp_audio_exists(file_uuid):
        try:
            audio_file_path = os.path.join(temp_dir, "audio.wav")
            download_temp_audio(file_uuid, audio_file_path)
            logger.info("rediarize: reusing preprocessed WAV from temp")
            return audio_file_path, temp_dir
        except Exception as temp_err:
            logger.warning(
                f"rediarize: temp WAV fetch failed ({temp_err}); falling back to original"
            )

    file_data, _, _ = download_file(storage_path)
    file_ext = get_audio_file_extension(content_type, filename)

    temp_file_path = os.path.join(temp_dir, f"input{file_ext}")
    with open(temp_file_path, "wb") as f:
        f.write(file_data.read())

    audio_file_path = prepare_audio_for_transcription(temp_file_path, content_type, temp_dir)
    return audio_file_path, temp_dir


@dataclass
class _Detection:
    """What a speaker detector produced, as plain data the merge and save phases read."""

    frame: Any  # transcription.diarize_result.DiarizeResult (start/end/speaker arrays)
    overlap_info: dict
    native_embeddings: dict | None  # per-speaker vectors when the local engine made them
    provider: str | None
    model: str | None


class _CloudDetectionFailedError(Exception):
    """A configured cloud detector failed at the vendor (non-fatal for the transcript)."""


def _start(file_uuid: str, task_id: str, engine: str) -> dict:
    """Read phase: the file's plain metadata, the task row, PROCESSING.

    Raises:
        PipelinePlanRefusedError: ``engine == "local"`` on a lite deployment, which has no
            local speaker detection (defence in depth behind the plan and the API).
    """
    from app.utils.uuid_helpers import get_file_by_uuid

    if engine == "local" and str(settings.DEPLOYMENT_MODE).lower() == "lite":
        from app.core.exceptions import PipelinePlanRefusedError
        from app.services.pipeline_plan import ISSUE_MESSAGES

        code = "local_diarization_unavailable"
        raise PipelinePlanRefusedError(code, ISSUE_MESSAGES[code])

    with session_scope() as db:
        media_file = get_file_by_uuid(db, file_uuid)
        file_id, user_id = int(media_file.id), int(media_file.user_id)
        meta: dict[str, Any] = {
            "file_id": file_id,
            "user_id": user_id,
            "storage_path": str(media_file.storage_path),
            "content_type": str(media_file.content_type),
            "filename": str(media_file.filename),
        }
        create_task_record(db, task_id, user_id, file_id, "rediarize")
        update_task_status(db, task_id, "in_progress", progress=0.05)
        update_media_file_status(db, file_id, FileStatus.PROCESSING)
    return meta


def _build_detector(engine: str, user_id: int, config_uuid: str | None):
    """The plan's speaker detector, built in a short read-only session (no HTTP inside).

    Raises:
        DiarizationNotConfiguredError: pyannote.ai with no usable key (fatal, fixed sentence).
        DiarizerConfigUnusableError: an engine that cannot be built (fatal, fixed sentence).
    """
    from app.services.diarization.factory import DiarizationProviderFactory

    with session_scope() as db:
        return DiarizationProviderFactory.create_for_engine(
            engine, user_id, db, config_uuid=config_uuid
        )


def _detect(
    engine: str,
    detector,
    audio_file_path: str,
    speaker_range: SpeakerRange,
    sidecar_wav_path: str | None,
) -> _Detection:
    """Run the detector with NO session open.

    Raises:
        _CloudDetectionFailedError: a cloud detector failed at the vendor.
    """
    from app.services.diarization.types import DiarizeConfig
    from app.transcription.speaker_assigner import diarize_segments_to_frame

    config = DiarizeConfig(
        min_speakers=speaker_range.min_speakers,
        max_speakers=speaker_range.max_speakers,
        num_speakers=speaker_range.num_speakers,
        sidecar_wav_path=sidecar_wav_path,
    )
    if engine == "local":
        result = detector.diarize(audio_file_path, config)
        return _Detection(
            frame=result.metadata["diarize_df"],
            overlap_info=result.metadata.get("overlap_info") or {},
            native_embeddings=result.metadata.get("native_embeddings"),
            provider=result.provider_name or None,
            model=result.model_name or None,
        )
    try:
        result = detector.diarize(audio_file_path, config)
    except Exception as exc:
        # The vendor's text never leaves this log line (no exception text on the wire).
        logger.warning(
            "Cloud speaker detection (%s) failed: %s", engine, type(exc).__name__, exc_info=True
        )
        raise _CloudDetectionFailedError() from exc
    return _Detection(
        frame=diarize_segments_to_frame(result.segments),
        overlap_info={},
        native_embeddings=None,
        provider=result.provider_name or None,
        model=result.model_name or None,
    )


def _merge_turns(detection: _Detection, transcript: dict, merge: str) -> list[dict]:
    """Assign the turns to the stored text, then re-split segments at speaker changes.

    ``merge`` is the plan's granularity (C7): ``word`` assigns every word by overlap;
    ``segment`` gives each segment its dominant speaker and its words follow. Then
    ``finalize_segments`` (boundary smoothing, re-segment at word-speaker boundaries, merge
    same-speaker neighbours), the chokepoint every transcription path routes through.
    """
    from app.transcription.boundary_resolver import BoundarySmoothingConfig
    from app.transcription.speaker_assigner import assign_speakers
    from app.utils.segment_postprocess import finalize_segments

    result = assign_speakers(
        detection.frame, transcript, granularity="segment" if merge == "segment" else "word"
    )
    with session_scope() as db:
        smoothing_cfg = BoundarySmoothingConfig.from_db_env(db)
    return finalize_segments(result["segments"], smoothing_cfg)


def _save(meta: dict, task_id: str, segments: list[dict], detection: _Detection):
    """Write phase: speakers, segments and the detector that actually ran."""
    from app.tasks.transcription.speaker_processor import create_speaker_mapping
    from app.tasks.transcription.speaker_processor import extract_unique_speakers
    from app.tasks.transcription.speaker_processor import mark_overlapping_segments
    from app.tasks.transcription.speaker_processor import process_segments_with_speakers
    from app.tasks.transcription.storage import save_transcript_segments

    file_id, user_id = meta["file_id"], meta["user_id"]
    unique_speakers = extract_unique_speakers(segments)
    with session_scope() as db:
        speaker_mapping = create_speaker_mapping(db, user_id, file_id, unique_speakers)
        update_task_status(db, task_id, "in_progress", progress=0.75)

    processed = process_segments_with_speakers(segments, speaker_mapping)
    overlap_regions = detection.overlap_info.get("regions", [])
    if os.getenv("ENABLE_OVERLAP_DETECTION", "true").lower() == "true" and overlap_regions:
        processed = mark_overlapping_segments(processed, overlap_regions)

    with session_scope() as db:
        save_transcript_segments(db, file_id, processed)
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if media_file is not None:
            media_file.diarization_disabled = False
            media_file.diarization_provider = detection.provider
            media_file.diarization_model = detection.model
        update_task_status(db, task_id, "in_progress", progress=0.85)
    return processed, speaker_mapping, unique_speakers


def _voiceprint_stage(
    meta: dict,
    file_uuid: str,
    task_id: str,
    detection: _Detection,
    processed: list[dict],
    speaker_mapping: dict,
    pipeline_task_id: str | None,
) -> bool:
    """Run the voiceprint stage exactly once. Returns True when it was dispatched (it then
    owns the temp WAV and cleans it up itself).

    Free when this server's engine already produced usable per-speaker vectors (matched
    inline, as the inline pipeline does); otherwise the CPU embedding stage computes them
    through the one extractor interface. It never meters or closes the run: this post
    stage is the run's terminus.
    """
    from app.tasks.transcription.context import TranscriptionContext
    from app.tasks.transcription.embeddings import _process_speaker_embeddings_native
    from app.tasks.transcription.embeddings import _should_use_native_embeddings
    from app.tasks.transcription.embeddings import _store_native_centroids_in_v4_staging

    native = detection.native_embeddings
    ctx = TranscriptionContext(
        task_id=task_id,
        file_id=meta["file_id"],
        file_uuid=file_uuid,
        user_id=meta["user_id"],
        file_path=meta["storage_path"],
        file_name=meta["filename"],
        content_type=meta["content_type"],
    )
    if native and _should_use_native_embeddings({"native_speaker_embeddings": native}):
        with stage_timing.stage("speaker_embedding", task_id=task_id, file_id=meta["file_id"]):
            _process_speaker_embeddings_native(ctx, native, processed, speaker_mapping)
        return False

    if native:
        # A v3 (512-d) index cannot take the 256-d centroids; keep them staged for the
        # migration, and let the embedding stage produce vectors in the index's space.
        try:
            _store_native_centroids_in_v4_staging(ctx, native, speaker_mapping)
        except Exception as e:
            logger.warning(f"v4 staging error during rediarize (non-fatal): {e}")

    from app.tasks.speaker_embedding_task import extract_speaker_embeddings_task

    extract_speaker_embeddings_task.apply_async(
        args=[file_uuid],
        kwargs={"pipeline_task_id": pipeline_task_id, "closes_pipeline": False},
        queue=CeleryQueues.CPU,
    )
    return True


def _finish(
    meta: dict,
    task_id: str,
    pipeline_completion: bool,
    pipeline_task_id: str | None,
    note: str | None = None,
) -> None:
    """Complete the post stage (and, for a pipeline run, meter it: the one terminus)."""
    file_id = meta["file_id"]
    with session_scope() as db:
        update_task_status(
            db, task_id, "completed", progress=1.0, completed=True, error_message=note
        )
        update_media_file_status(db, file_id, FileStatus.COMPLETED)
        # Metering: a post stage that completes a fresh pipeline run is that run's ONLY
        # terminus (finalize_transcription deliberately does not meter when it dispatches
        # one, and the voiceprint stage it dispatches never meters). A user's manual
        # re-diarization (pipeline_completion=False) never meters: the transcript was
        # already billed. run_id is the pipeline's id: the reservation and the idempotency
        # key were taken under it at dispatch.
        if pipeline_completion:
            from app.tasks.transcription.postprocess import _fire_completion_metering

            _fire_completion_metering(
                db,
                file_id=file_id,
                run_id=pipeline_task_id or task_id,
                provider=_resolve_asr_provider(db, file_id),
                success=True,
            )

    from app.tasks.transcription.notifications import send_completion_notification

    send_completion_notification(meta["user_id"], file_id)


def _dispatch_downstream(
    downstream_tasks: list[str],
    file_uuid: str,
    file_id: int | None = None,
    user_id: int | None = None,
) -> None:
    """Dispatch downstream tasks by stage name after rediarization completes."""
    from app.api.endpoints.files.reprocess import dispatch_task_by_name

    for stage in downstream_tasks:
        try:
            dispatch_task_by_name(stage, file_uuid, file_id=file_id, user_id=user_id)
            logger.info(f"Dispatched downstream task '{stage}' for file {file_uuid}")
        except Exception as e:
            logger.warning(f"Failed to dispatch downstream task '{stage}' for {file_uuid}: {e}")


def _after_speakers_changed(meta: dict, file_uuid: str, downstream_tasks: list[str] | None) -> None:
    """Speaker attributes → LLM speaker ID, the unconditional reindex, then the rest."""
    file_id, user_id = meta["file_id"], meta["user_id"]
    try:
        from app.tasks.speaker_attribute_task import dispatch_speaker_attribute_pipeline

        dispatch_speaker_attribute_pipeline(str(file_uuid), user_id)
    except Exception as e:
        logger.warning(f"Failed to dispatch speaker attribute detection: {e}")

    # Rediarization reassigns which speaker every segment belongs to, which changes both the
    # chunk plane and the full-document index — so it reindexes UNCONDITIONALLY (#666).
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex

    dispatch_transcript_reindex(file_id=file_id, file_uuid=file_uuid, user_id=user_id)

    # speaker_llm is chained from the attribute task; search_indexing is covered (debounced)
    # by the reindex above, and dispatching the full task again would race it.
    if downstream_tasks:
        filtered = [s for s in downstream_tasks if s not in ("speaker_llm", "search_indexing")]
        if filtered:
            _dispatch_downstream(filtered, file_uuid, file_id=file_id, user_id=user_id)


def _record_failure(file_uuid: str, task_id: str, error: Exception) -> str:
    """Fail the file with the classified fixed sentence (raw text to the log only, #959)."""
    from app.services.error_categorization_service import ErrorCategorizationService
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex
    from app.utils.uuid_helpers import get_file_by_uuid

    failure = ErrorCategorizationService.classify_failure(str(error))
    error_msg = failure.user_message
    try:
        with session_scope() as db:
            update_task_status(db, task_id, "failed", error_message=error_msg, completed=True)
            try:
                media_file = get_file_by_uuid(db, file_uuid)
                update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
                media_file.last_error_message = error_msg
                media_file.error_category = failure.retry_category.value
                db.commit()
                # A failure after the segment save leaves committed speaker changes with no
                # reindex; the dispatch is idempotent/debounced, so fire it on any failure.
                dispatch_transcript_reindex(
                    file_id=int(media_file.id),
                    file_uuid=file_uuid,
                    user_id=int(media_file.user_id),
                )
            except Exception as file_err:
                logger.warning(f"Could not update file error status: {file_err}")
    except Exception as update_err:
        logger.error(f"Error updating task status after rediarize failure: {update_err}")
    return error_msg


@celery_app.task(bind=True, name="rediarize", priority=GPUPriority.USER_REDIARIZ)
def rediarize_task(
    self,
    file_uuid: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    downstream_tasks: list[str] | None = None,
    pipeline_completion: bool = False,
    pipeline_task_id: str | None = None,
    engine: str = "local",
    merge: str | None = None,
    diarizer_config_uuid: str | None = None,
):
    """Detect speakers for an existing transcript without re-transcribing.

    Args:
        file_uuid: UUID of the MediaFile.
        min_speakers: Minimum speakers (None = the user's saved range).
        max_speakers: Maximum speakers (None = the user's saved range).
        num_speakers: Fixed speaker count (None = the env default).
        downstream_tasks: Optional downstream stage names to dispatch afterwards.
        pipeline_completion: True ONLY when this is the completion terminus of a fresh
            transcription run (the plan's ``post_stage_*``) — the sole case that meters.
        pipeline_task_id: The originating pipeline's task id, the metering run_id.
        engine: The plan's post-stage engine (``pipeline_plan.post_stage_engine``):
            ``local``, ``pyannote_ai`` or ``asr_config``.
        merge: The plan's merge granularity (``word`` | ``segment``); ``None`` (a manual
            re-run) derives it from the stored transcript: word times present -> ``word``.
        diarizer_config_uuid: The ASR configuration named as the detector (``asr_config``).
    """
    import shutil

    task_id = self.request.id
    temp_dir = None
    voiceprint_owns_audio = False
    timing_id = pipeline_task_id or task_id

    try:
        meta = _start(file_uuid, task_id, engine)
        file_id, user_id = meta["file_id"], meta["user_id"]
        logger.info(
            "Starting post-stage speaker detection for file %s (engine=%s)", file_uuid, engine
        )
        from app.tasks.transcription.notifications import send_progress_notification

        send_progress_notification(user_id, file_id, 0.05, "Starting speaker detection")
        transcript = _load_segments_as_transcript(file_id)
        if merge is None:
            has_words = any(seg.get("words") for seg in transcript["segments"])
            merge = "word" if has_words else "segment"
        if merge == "segment":
            logger.warning(
                "Speakers for file %s are assigned per segment (segment_timing_only): a "
                "speaker change inside a segment is not detected",
                file_id,
            )

        send_progress_notification(user_id, file_id, 0.15, "Preparing audio")
        audio_file_path, temp_dir = _prepare_audio(
            meta["storage_path"], meta["content_type"], meta["filename"], file_uuid=file_uuid
        )
        speaker_range = resolve_speaker_range_for_user(
            user_id, min_speakers, max_speakers, num_speakers
        )
        detector = _build_detector(engine, user_id, diarizer_config_uuid)

        send_progress_notification(user_id, file_id, 0.30, "Running speaker detection")
        step_start = time.perf_counter()
        benchmark_timing.mark(timing_id, "diarize_request_sent")
        try:
            with stage_timing.stage("diarization", task_id=task_id, file_id=file_id):
                detection = _detect(
                    engine,
                    detector,
                    audio_file_path,
                    speaker_range,
                    audio_file_path if temp_dir is None else None,
                )
        except _CloudDetectionFailedError:
            # Non-fatal: the transcript stands, without speakers, with the reason recorded.
            _finish(
                meta,
                task_id,
                pipeline_completion,
                pipeline_task_id,
                note=CLOUD_DIARIZATION_FAILED_MESSAGE,
            )
            return {"status": "completed_without_speakers", "file_id": file_id}
        finally:
            benchmark_timing.mark(timing_id, "diarize_joined")
        logger.info(
            "TIMING: post-stage speaker detection (%s) completed in %.3fs - %d turns",
            engine,
            time.perf_counter() - step_start,
            len(detection.frame),
        )

        send_progress_notification(user_id, file_id, 0.60, "Assigning speakers")
        with stage_timing.stage("speaker_assignment", task_id=task_id, file_id=file_id):
            segments = _merge_turns(detection, transcript, merge)
        send_progress_notification(user_id, file_id, 0.70, "Saving updated transcript")
        with stage_timing.stage("finalize", task_id=task_id, file_id=file_id):
            processed, speaker_mapping, unique_speakers = _save(meta, task_id, segments, detection)

        send_progress_notification(user_id, file_id, 0.85, "Processing speaker voiceprints")
        voiceprint_owns_audio = _voiceprint_stage(
            meta, file_uuid, task_id, detection, processed, speaker_mapping, pipeline_task_id
        )

        _finish(meta, task_id, pipeline_completion, pipeline_task_id)
        logger.info(
            f"Rediarization completed for file {file_uuid} - "
            f"{len(processed)} segments, {len(unique_speakers)} speakers"
        )
        _after_speakers_changed(meta, file_uuid, downstream_tasks)
        return {
            "status": "success",
            "file_id": file_id,
            "segments": len(processed),
            "speakers": len(unique_speakers),
            "merge": merge,
        }

    except Exception as e:
        logger.error(f"Rediarization failed for file {file_uuid}: {e}")
        return {"status": "error", "message": _record_failure(file_uuid, task_id, e)}

    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as cleanup_err:
                logger.warning(f"Failed to clean up temp dir {temp_dir}: {cleanup_err}")

        # We were the deferred consumer of the preprocessed WAV — unless the voiceprint
        # stage we dispatched now owns it (it cleans up itself).
        if not voiceprint_owns_audio:
            try:
                cleanup_temp_audio(file_uuid)
            except Exception as cleanup_err:
                logger.debug(f"Temp audio cleanup after rediarize failed: {cleanup_err}")

        if engine == "local":
            # Free intermediate CUDA tensors for follow-on tasks
            from app.tasks.migration_pipeline import cleanup_gpu_memory

            cleanup_gpu_memory()
