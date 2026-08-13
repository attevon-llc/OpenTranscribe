"""
Celery task for speaker attribute detection.

Runs on the CPU queue (concurrency=8) after transcription completes.
Non-critical: failure does not affect transcription status.

Uses presigned URL + ffmpeg segment seeking instead of downloading
entire files from MinIO. Segments are fetched in parallel via a thread
pool for better throughput.
"""

import contextlib
import datetime
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.services.audio_segment_utils import extract_audio_segment_np
from app.services.audio_segment_utils import merge_adjacent_segments
from app.services.audio_segment_utils import select_top_segments
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


def _is_speaker_attribute_detection_enabled(user_id: int) -> bool:
    """Check if speaker attribute detection is enabled for a user.

    Resolution order: User setting > System setting > .env > default (True).
    """
    from app.models.prompt import UserSetting
    from app.services.system_settings_service import get_setting_bool

    env_enabled = os.environ.get("SPEAKER_ATTRIBUTE_DETECTION_ENABLED", "true").lower() == "true"

    with session_scope() as db:
        system_enabled = get_setting_bool(
            db, "speaker_attribute.detection_enabled", default=env_enabled
        )

        user_setting = (
            db.query(UserSetting)
            .filter(
                UserSetting.user_id == user_id,
                UserSetting.setting_key == "speaker_attribute_detection_enabled",
            )
            .first()
        )
        if user_setting:
            return str(user_setting.setting_value).lower() == "true"

    return system_enabled


def _dispatch_llm_speaker_identification(file_uuid: str) -> None:
    """Dispatch the LLM speaker identification task for a media file.

    Called at the end of detect_speaker_attributes_task (or its early-exit paths)
    so that the LLM always runs after gender attributes have been written to the DB.
    """
    try:
        from app.tasks.speaker_tasks import identify_speakers_llm_task

        identify_speakers_llm_task.delay(file_uuid=file_uuid)
        logger.info(
            f"Dispatched LLM speaker identification for {file_uuid} (gender attributes ready)"
        )
    except Exception as e:
        logger.warning(f"Failed to dispatch LLM speaker identification: {e}")


def _store_gender_results(
    speakers,
    speaker_probs: dict[int, dict[str, float]],
    speaker_clip_counts: dict[int, int],
) -> int:
    """Store gender inference results on speaker objects and mark unattempted speakers.

    Returns the number of speakers updated with gender predictions.
    """
    now = datetime.datetime.now(datetime.UTC)
    updated_count = 0
    speaker_by_id = {int(s.id): s for s in speakers}

    for sid, probs in speaker_probs.items():
        speaker_obj = speaker_by_id.get(sid)
        if not speaker_obj:
            continue
        clips = speaker_clip_counts[sid]
        final_gender = max(probs, key=lambda k: probs[k])
        final_conf = probs[final_gender] / clips

        speaker_obj.predicted_gender = final_gender
        speaker_obj.predicted_age_range = None
        speaker_obj.attribute_confidence = {"gender": round(final_conf, 3)}
        speaker_obj.attributes_predicted_at = now
        updated_count += 1

    # Mark remaining speakers as attempted (no valid segments)
    # to prevent perpetual re-processing by migration tasks
    for speaker_obj in speaker_by_id.values():
        if speaker_obj.attributes_predicted_at is None:
            speaker_obj.attributes_predicted_at = now

    return updated_count


def _run_gender_inference_parallel(
    audio_source: str,
    work_items: list[tuple[int, dict]],
    service,
) -> tuple[dict[int, dict[str, float]], dict[int, int]]:
    """Run gender inference on segments fetched in parallel.

    Returns (speaker_probs, speaker_clip_counts) dicts.
    """
    speaker_probs: dict[int, dict[str, float]] = {}
    speaker_clip_counts: dict[int, int] = {}

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="attr-ffmpeg") as pool:
        futures = []
        for speaker_id, seg in work_items:
            duration = seg["end"] - seg["start"]
            fut = pool.submit(extract_audio_segment_np, audio_source, seg["start"], duration)
            futures.append((speaker_id, fut))

        for speaker_id, fut in futures:
            try:
                audio_np = fut.result(timeout=30)
            except Exception as e:
                logger.debug("Segment fetch failed for speaker %s: %s", speaker_id, e)
                continue

            if audio_np is None or len(audio_np) < 16000:
                continue

            gender, confidence = service._run_inference(audio_np)

            if speaker_id not in speaker_probs:
                speaker_probs[speaker_id] = {"male": 0.0, "female": 0.0}
                speaker_clip_counts[speaker_id] = 0
            speaker_probs[speaker_id][gender] += confidence
            speaker_clip_counts[speaker_id] += 1

    return speaker_probs, speaker_clip_counts


@celery_app.task(
    bind=True, name="detect_speaker_attributes", priority=CPUPriority.PIPELINE_CRITICAL
)
def detect_speaker_attributes_task(self, file_uuid: str, user_id: int):
    """Predict gender/age for all speakers in a media file.

    Uses presigned URL + ffmpeg segment seeking to avoid downloading the
    entire file. Segments for all speakers are fetched in parallel via
    a thread pool. Runs on CPU queue in parallel with GPU transcription.
    """
    # Idempotency guard: gender inference is minutes of CPU-bound wav2vec2 per
    # file, and rediarize/recovery flows can dispatch the same detection again
    # before the first finishes (observed 6-deep on one file). Duplicates would
    # compute identical results while multiplying CPU contention — skip them.
    # Fail-open if Redis is unavailable (e.g. unit tests with SKIP_REDIS).
    _guard = None
    _guard_key = f"speaker_attr_detect:{file_uuid}"
    try:
        from app.core.redis import get_redis

        _guard = get_redis()
        if not _guard.set(_guard_key, "1", nx=True, ex=7200):
            logger.info(
                f"Attribute detection already in progress for {file_uuid}; skipping duplicate"
            )
            _dispatch_llm_speaker_identification(file_uuid)
            return {"status": "skipped", "reason": "duplicate_in_progress"}
    except Exception:
        _guard = None  # Redis unavailable — proceed unguarded

    try:
        return _detect_speaker_attributes(file_uuid, user_id)
    finally:
        if _guard is not None:
            with contextlib.suppress(Exception):  # lock expires via TTL anyway
                _guard.delete(_guard_key)


def _detect_speaker_attributes(file_uuid: str, user_id: int):
    """Inner implementation of detect_speaker_attributes_task."""
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment
    from app.services.minio_service import minio_client
    from app.services.speaker_attribute_service import get_cached_attribute_service
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
        try:
            if not _is_speaker_attribute_detection_enabled(user_id):
                logger.info("Speaker attribute detection disabled, skipping")
                _dispatch_llm_speaker_identification(file_uuid)
                return {"status": "skipped", "reason": "disabled"}

            media_file = get_file_by_uuid(db, file_uuid)
            if not media_file:
                logger.error(f"Media file {file_uuid} not found for attribute detection")
                return {"status": "error", "reason": "file_not_found"}

            file_id = int(media_file.id)
            storage_path = str(media_file.storage_path)

            speakers = db.query(Speaker).filter(Speaker.media_file_id == file_id).all()
            if not speakers:
                logger.info(f"No speakers found for file {file_id}, skipping")
                _dispatch_llm_speaker_identification(file_uuid)
                return {"status": "skipped", "reason": "no_speakers"}

            segments = (
                db.query(TranscriptSegment)
                .filter(TranscriptSegment.media_file_id == file_id)
                .order_by(
                    TranscriptSegment.start_time, TranscriptSegment.end_time, TranscriptSegment.id
                )
                .all()
            )

            if not segments:
                _dispatch_llm_speaker_identification(file_uuid)
                return {"status": "skipped", "reason": "no_segments"}

            # Group, merge, and select top segments per speaker
            speaker_segments: dict[int, list[dict]] = {}
            for seg in segments:
                if not seg.speaker_id:
                    continue
                sid = int(seg.speaker_id)
                if sid not in speaker_segments:
                    speaker_segments[sid] = []
                speaker_segments[sid].append(
                    {
                        "start": float(seg.start_time),
                        "end": float(seg.end_time),
                    }
                )

            audio_source = minio_client.presigned_get_object(
                bucket_name=settings.MEDIA_BUCKET_NAME,
                object_name=storage_path,
                expires=datetime.timedelta(hours=1),
            )

            work_items = []
            for speaker in speakers:
                segs = speaker_segments.get(int(speaker.id), [])
                if not segs:
                    continue
                merged = merge_adjacent_segments(segs)
                selected = select_top_segments(
                    merged, min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION, max_segments=5
                )
                for seg in selected:
                    work_items.append((int(speaker.id), seg))

            service = get_cached_attribute_service()
            service.load_models()

            speaker_probs, speaker_clip_counts = _run_gender_inference_parallel(
                audio_source,
                work_items,
                service,
            )

            updated_count = _store_gender_results(speakers, speaker_probs, speaker_clip_counts)
            db.commit()

            logger.info(
                f"Speaker attribute detection complete for file {file_uuid}: "
                f"{updated_count}/{len(speakers)} speakers updated"
            )

            if updated_count > 0:
                send_ws_event(
                    user_id,
                    "speaker_updated",
                    {
                        "file_id": file_uuid,
                        "reason": "speaker_attributes_detected",
                        "speakers_updated": updated_count,
                    },
                )

            # Notify enrichment tracker that speaker attributes are done
            send_ws_event(
                user_id,
                "enrichment_task_complete",
                {"file_id": file_uuid, "task": "speaker_attributes"},
            )

            _dispatch_llm_speaker_identification(file_uuid)

            return {
                "status": "success",
                "file_uuid": file_uuid,
                "speakers_updated": updated_count,
                "total_speakers": len(speakers),
            }

        except Exception as e:
            logger.error(f"Speaker attribute detection failed for {file_uuid}: {e}")
            logger.error("Full traceback:", exc_info=True)
            _dispatch_llm_speaker_identification(file_uuid)
            return {"status": "error", "message": str(e)}
