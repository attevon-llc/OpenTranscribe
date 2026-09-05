"""
Unified batch processing pipeline for speaker analysis migrations.

Shared infrastructure for embedding migration (v3->v4), speaker attribute
migration, and combined migration. Provides:
- SpeakerSnapshot / PreparedFile data classes
- File preparation (DB query + presigned URL generation)
- Segment collection with merge + top-N selection
- I/O-pipelined GPU processing (all extractions submitted upfront)

Architecture:
    ┌──────────────────────────────────────────────┐
    │  I/O Pool (16 ffmpeg threads)                │
    │  ALL segments for ALL files submitted UPFRONT │
    └────────────────────┬─────────────────────────┘
                         │ np.ndarray futures
                         ▼
    ┌──────────────────────────────────────────────┐
    │  Sequential GPU processing                   │
    │  One runner instance per batch task           │
    │  Data already extracted before GPU needs it   │
    └──────────────────────────────────────────────┘

Why sequential works well:
  - The I/O pool submits ALL ffmpeg extractions upfront (all files, all
    segments) so audio data is already in memory before the GPU asks for it.
  - GPU inference per segment is fast (~10-50ms); the bottleneck is I/O.
  - A single model copy uses minimal VRAM (~400MB-1.5GB), leaving room
    for residual transcription models.
  - Celery handles multi-GPU parallelism: each GPU worker container picks
    up its own batch task, so N GPUs process N batches simultaneously.
"""

import datetime
import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from app.core.config import settings
from app.core.constants import SPEAKER_SEGMENT_MIN_DURATION
from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
from app.db.session_utils import session_scope
from app.services.audio_segment_utils import extract_audio_segment_np
from app.services.audio_segment_utils import merge_adjacent_segments
from app.services.audio_segment_utils import select_top_segments
from app.services.speaker_analysis_models import MultiModelRunner
from app.services.speaker_analysis_models import SegmentResult

logger = logging.getLogger(__name__)


class PermanentFileError(Exception):
    """A file cannot be migrated and retrying will not help (issue #657, defect 5).

    Distinct from ``prepare_file`` returning ``None``, which means "this file
    legitimately has nothing to migrate" (no speakers, or speakers with no
    assigned segments) — a real skip that is correctly counted as processed.
    A missing MinIO object or a NULL ``storage_path`` is a FAILURE: the
    caller must route it to ``increment_processed(success=False)``/
    ``failed_files``, not silently count it as done. Before this, both cases
    returned ``None`` and were indistinguishable, so a permanently-missing
    file was retried forever by ``/retry-failed`` and its 512-d voiceprint
    was silently orphaned by the eventual alias swap.
    """


# I/O thread pool size — enough to keep ffmpeg extractions overlapping
DEFAULT_IO_WORKERS = 16

DEFAULT_MIN_DURATION = SPEAKER_SEGMENT_MIN_DURATION

# Audio sample rate that ffmpeg resamples to — used for the minimum sample count check
AUDIO_SAMPLE_RATE = 16000
# Minimum extracted audio length in samples — must match SPEAKER_SHORT_SEGMENT_MIN_DURATION
# so segments selected by the pipeline are never silently dropped here
MIN_AUDIO_SAMPLES = int(SPEAKER_SHORT_SEGMENT_MIN_DURATION * AUDIO_SAMPLE_RATE)
DEFAULT_MAX_SEGMENTS = 5
FILE_SLOW_THRESHOLD_SECONDS = 120


@dataclass
class SpeakerSnapshot:
    """Plain-data snapshot of a Speaker, detached from any SQLAlchemy session."""

    id: int
    uuid: str
    name: str
    profile_id: int | None = None


@dataclass
class PreparedFile:
    """All data needed to process one file, fully detached from DB session.

    No audio data is stored — segments are extracted on-demand via
    ffmpeg seeking from the audio_source (presigned URL or local path).
    """

    file_uuid: str
    audio_source: str  # Presigned MinIO URL or local file path
    speakers: list[SpeakerSnapshot]
    speaker_segments: dict[int, list[dict[str, float]]]  # speaker_id → segments
    media_file_id: int
    user_id: int
    extra: dict[str, Any] = field(default_factory=dict)  # Migration-specific data


def prepare_file(
    file_uuid: str,
    include_profile: bool = False,
) -> PreparedFile | None:
    """Prepare a file for processing: query DB + generate presigned URL.

    Returns PreparedFile (session-independent), or ``None`` for a legitimate
    skip (no speakers, or speakers with no assigned segments — nothing to
    migrate). Raises ``PermanentFileError`` — never returns ``None`` — for a
    real failure the caller must count as failed: no ``storage_path``, or
    the MinIO object is missing.

    Args:
        file_uuid: UUID of the media file.
        include_profile: If True, include speaker profile UUIDs in extra.
    """
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
        media_file = get_file_by_uuid(db, file_uuid)
        if not media_file:
            raise ValueError(f"Media file {file_uuid} not found")

        speakers = db.query(Speaker).filter(Speaker.media_file_id == media_file.id).all()
        if not speakers:
            return None

        # Build speaker → segments mapping
        segments = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == media_file.id)
            .order_by(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.id,
            )
            .all()
        )

        speaker_segments: dict[int, list[dict[str, float]]] = {}
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

        # Snapshot speaker data
        speaker_snapshots = []
        extra: dict[str, Any] = {}
        speaker_profiles: dict[int, str | None] = {}

        for sp in speakers:
            snapshot = SpeakerSnapshot(
                id=int(sp.id),
                uuid=str(sp.uuid),
                name=str(sp.name),
                profile_id=int(sp.profile_id) if sp.profile_id else None,
            )
            speaker_snapshots.append(snapshot)
            if include_profile:
                profile_uuid = None
                if sp.profile_id and sp.profile:
                    profile_uuid = str(sp.profile.uuid)
                speaker_profiles[int(sp.id)] = profile_uuid

        if include_profile:
            extra["speaker_profiles"] = speaker_profiles

        storage_path = media_file.storage_path
        media_file_id = int(media_file.id)
        user_id = int(media_file.user_id)

    # Guard: speakers exist but none have transcript segments
    if not speaker_segments:
        logger.info("%s has speakers but no assigned segments — skipping", file_uuid[:12])
        return None

    # Guard: NULL or empty storage_path — a FAILURE, not a skip: this file has
    # speakers/segments to migrate and cannot, ever, at this storage_path.
    if not storage_path:
        raise PermanentFileError(f"{file_uuid} has no storage_path")

    # Guard: check MinIO object exists — also a FAILURE for the same reason.
    from app.services.minio_service import minio_client

    try:
        minio_client.stat_object(settings.MEDIA_BUCKET_NAME, storage_path)
    except Exception as e:
        raise PermanentFileError(f"{file_uuid} MinIO object missing ({storage_path}): {e}") from e

    # Generate presigned URL — ffmpeg will seek directly into MinIO.
    #
    # Issue #657, defect 6: this used to be a flat 2h regardless of the
    # deployment's actual signing-credential lifetime, while ALL 25 files in
    # a batch are presigned upfront (submit_segment_fetches, below) and
    # processed sequentially — a slow batch can outlive its own last file's
    # URL. clamp_presigned_expiry() uses the deployment's real ceiling
    # (PRESIGNED_URL_MAX_SECONDS, 6h default) instead of a value picked with
    # no relation to it, which does not fix a batch slower than the ceiling
    # but at minimum stops leaving 4 of those 6 hours unused for free.
    from app.services.storage_backend import max_presigned_seconds

    audio_source = minio_client.presigned_get_object(
        bucket_name=settings.MEDIA_BUCKET_NAME,
        object_name=storage_path,
        expires=datetime.timedelta(seconds=max_presigned_seconds()),
    )

    return PreparedFile(
        file_uuid=file_uuid,
        audio_source=audio_source,
        speakers=speaker_snapshots,
        speaker_segments=speaker_segments,
        media_file_id=media_file_id,
        user_id=user_id,
        extra=extra,
    )


def collect_work_items(
    prepared: PreparedFile,
    min_duration: float = DEFAULT_MIN_DURATION,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
) -> list[tuple[SpeakerSnapshot, dict[str, float]]]:
    """Collect eligible speaking sections for analysis.

    Adjacent segments (gap <= 0.5s) are merged into continuous speaking
    sections, then the top N longest (above min_duration) are selected.
    """
    items: list[tuple[SpeakerSnapshot, dict[str, float]]] = []
    for speaker in prepared.speakers:
        segs = prepared.speaker_segments.get(speaker.id, [])
        if not segs:
            continue
        merged = merge_adjacent_segments(segs)
        selected = select_top_segments(merged, min_duration=min_duration, max_segments=max_segments)
        for seg in selected:
            items.append((speaker, seg))
    return items


def submit_segment_fetches(
    prepared: PreparedFile,
    pool: ThreadPoolExecutor,
    min_duration: float = DEFAULT_MIN_DURATION,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
) -> list[tuple[SpeakerSnapshot, dict, Future]]:
    """Submit ffmpeg segment extraction jobs to a shared I/O thread pool.

    Returns futures immediately — does NOT block.
    """
    work_items = collect_work_items(prepared, min_duration, max_segments)
    futures = []
    for speaker, seg in work_items:
        duration = seg["end"] - seg["start"]
        fut = pool.submit(extract_audio_segment_np, prepared.audio_source, seg["start"], duration)
        futures.append((speaker, seg, fut))
    return futures


def _process_file_segments(
    runner: MultiModelRunner,
    seg_futures: list[tuple[Any, dict, Future]],
) -> dict[str, list[SegmentResult]]:
    """Process all segment futures for one file, feeding audio to models.

    Returns results grouped by model name.
    Raises RuntimeError if ALL extractions fail (transient error).
    """
    results_by_model: dict[str, list[SegmentResult]] = {}
    extraction_failures = 0
    total_futures = len(seg_futures)

    for speaker, _seg, fut in seg_futures:
        try:
            audio_np = fut.result(timeout=30)
        except Exception as e:
            logger.debug("Segment future failed for speaker %s: %s", speaker.id, e)
            extraction_failures += 1
            continue

        if audio_np is None or len(audio_np) < MIN_AUDIO_SAMPLES:
            extraction_failures += 1
            continue

        # Feed same audio to all models back-to-back
        segment_results = runner.process_segment(audio_np, 16000, speaker.id)
        for sr in segment_results:
            if sr.model_name not in results_by_model:
                results_by_model[sr.model_name] = []
            results_by_model[sr.model_name].append(sr)

    if total_futures > 0 and extraction_failures == total_futures:
        raise RuntimeError(f"All {total_futures} segment extractions failed")

    return results_by_model


# ---------------------------------------------------------------------------
# Main pipeline entry point
# ---------------------------------------------------------------------------


def process_batch_pipelined(
    prepared_files: list[tuple[str, PreparedFile]],
    runner: MultiModelRunner,
    result_writer: Callable[[PreparedFile, dict[str, list[SegmentResult]]], int],
    is_running_check: Callable[[], bool],
    on_file_success: Callable[[str], None],
    on_file_failure: Callable[[str, Exception | None], None],
    min_duration: float = DEFAULT_MIN_DURATION,
    io_workers: int = DEFAULT_IO_WORKERS,
) -> tuple[int, int]:
    """Process files with I/O pipelining: all ffmpeg extractions upfront.

    How it works:
    1. Submit ALL segment extractions to the I/O thread pool (16 ffmpeg
       workers). This starts immediately for ALL files in the batch.
    2. Process files sequentially on GPU — by the time the GPU reaches
       each file, its audio data is already extracted and waiting.
    3. Write results (OpenSearch/PostgreSQL) after each file completes.

    Multi-GPU scaling is handled by Celery: each GPU worker container
    picks up its own batch task independently, so N GPUs = N parallel
    batch tasks, each with its own model instance.

    Args:
        prepared_files: List of (file_uuid, PreparedFile) tuples.
        runner: Warm-cached MultiModelRunner with loaded model(s).
        result_writer: Callback to write results; receives (prepared,
            results_by_model). Returns count of items written.
        is_running_check: Returns False to abort processing.
        on_file_success: Called with file_uuid after successful processing.
        on_file_failure: Called with (file_uuid, exception) on failure.
        min_duration: Minimum segment duration in seconds.
        io_workers: Number of I/O thread pool workers for ffmpeg.

    Returns:
        (success_count, failure_count) tuple.
    """
    if not prepared_files:
        return 0, 0

    success = 0
    failed = 0

    with ThreadPoolExecutor(
        max_workers=io_workers,
        thread_name_prefix="pipeline-ffmpeg",
    ) as io_pool:
        # Submit ALL segment fetches upfront — FIFO ordering ensures
        # early files' segments complete first, keeping GPU fed
        file_work: list[tuple[str, PreparedFile, list]] = []
        for fuuid, prepared in prepared_files:
            seg_futures = submit_segment_fetches(prepared, io_pool, min_duration)
            file_work.append((fuuid, prepared, seg_futures))

        # Process files sequentially — GPU processes one file while
        # I/O threads continue extracting segments for upcoming files
        for fuuid, prepared, seg_futures in file_work:
            if not is_running_check():
                logger.warning("Migration stopped, aborting batch")
                break

            file_start = time.time()
            try:
                results_by_model = _process_file_segments(runner, seg_futures)
                count = result_writer(prepared, results_by_model)
                elapsed = time.time() - file_start

                if elapsed > FILE_SLOW_THRESHOLD_SECONDS:
                    logger.warning("%s… took %.1fs (slow)", fuuid[:12], elapsed)

                success += 1
                on_file_success(fuuid)

                if count:
                    logger.info("%s… %d items processed", fuuid[:12], count)

            except Exception as e:
                logger.error("%s… failed: %s", fuuid[:12], e)
                failed += 1
                on_file_failure(fuuid, e)

    return success, failed


def cleanup_gpu_memory() -> None:
    """Release cached CUDA/MPS memory so follow-on tasks don't OOM.

    Call this at the end of GPU batch tasks to free intermediate tensors.
    Does NOT unload warm-cached models — only frees PyTorch allocator caches.
    """
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
    except Exception as e:
        logger.debug("CUDA cleanup skipped: %s", e)
