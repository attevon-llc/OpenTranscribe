import logging
import uuid as uuid_module
from typing import Any

from sqlalchemy.orm import Session

from app.models.media import Speaker

logger = logging.getLogger(__name__)

# Fallback speaker label used when a segment has no speaker attribution.
_FALLBACK_SPEAKER = "SPEAKER_00"


def normalize_speaker_label(speaker_id: str) -> str:
    """
    Normalize speaker labels to ensure consistent format (SPEAKER_XX).

    Args:
        speaker_id: Original speaker ID

    Returns:
        Normalized speaker ID
    """
    if speaker_id is None:
        return "SPEAKER_00"

    if not speaker_id.startswith("SPEAKER_"):
        return f"SPEAKER_{speaker_id}"

    return speaker_id


def extract_unique_speakers(segments: list[dict[str, Any]]) -> set[str]:
    """
    Extract unique speaker IDs from transcription segments.

    Segments whose ``speaker`` field is None (e.g. from cloud providers that don't
    support diarization) are assigned the canonical fallback label ``SPEAKER_00``
    so that a corresponding DB speaker record is always created.  This prevents
    ``process_segments_with_speakers`` from generating an ad-hoc label (via
    ``i % 2``) that is absent from *speaker_mapping*, which would leave every
    such segment with ``speaker_id = NULL`` in the database.

    Args:
        segments: List of transcription segments

    Returns:
        Set of unique speaker IDs (all non-None, all normalised to SPEAKER_XX format)
    """
    unique_speakers = set()

    for segment in segments:
        if "speaker" in segment and segment["speaker"] is not None:
            normalized_speaker = normalize_speaker_label(segment["speaker"])
        else:
            # No speaker label from the provider — assign a consistent fallback so
            # a DB speaker row is created and the FK constraint can be satisfied.
            normalized_speaker = _FALLBACK_SPEAKER
            segment["speaker"] = normalized_speaker  # Update in place

        unique_speakers.add(normalized_speaker)

    return unique_speakers


def create_or_get_speaker(
    db: Session, user_id: int, media_file_id: int, speaker_label: str
) -> Speaker:
    """
    Create a new speaker or get existing one from database for a specific file.

    Args:
        db: Database session
        user_id: User ID
        media_file_id: Media file ID to associate speaker with
        speaker_label: Speaker label (e.g., "SPEAKER_01")

    Returns:
        Speaker object
    """
    # Check if speaker already exists for this user and file
    speaker = (
        db.query(Speaker)
        .filter(
            Speaker.user_id == user_id,
            Speaker.media_file_id == media_file_id,
            Speaker.name == speaker_label,
        )
        .first()
    )

    if not speaker:
        try:
            speaker_uuid = str(uuid_module.uuid4())

            speaker = Speaker(
                name=speaker_label,
                display_name=None,
                uuid=speaker_uuid,
                user_id=user_id,
                media_file_id=media_file_id,
                verified=False,
            )
            db.add(speaker)
            db.flush()  # Get the ID without committing
            logger.info(
                f"Created new speaker: {speaker_label} with UUID: {speaker_uuid} for file: {media_file_id}"
            )

        except Exception as e:
            logger.error(
                f"Error creating speaker {speaker_label} for file {media_file_id}: {str(e)}"
            )
            # The failed flush leaves the session's transaction marked for rollback,
            # and the likely cause is a concurrent insert of this exact
            # (user_id, media_file_id, name) row (this repo's documented duplicate-
            # task-delivery risk — acks_late with no visibility_timeout override). A
            # blind re-insert with a new UUID would hit the same unique constraint
            # again and raise PendingRollbackError uncaught. Roll back and reuse
            # whatever now exists instead.
            db.rollback()
            speaker = (
                db.query(Speaker)
                .filter(
                    Speaker.user_id == user_id,
                    Speaker.media_file_id == media_file_id,
                    Speaker.name == speaker_label,
                )
                .first()
            )
            if not speaker:
                speaker_uuid = str(uuid_module.uuid4())
                speaker = Speaker(
                    name=speaker_label,
                    display_name=None,
                    uuid=speaker_uuid,
                    user_id=user_id,
                    media_file_id=media_file_id,
                    verified=False,
                )
                db.add(speaker)
                db.flush()

    return speaker  # type: ignore[no-any-return]


def create_speaker_mapping(
    db: Session, user_id: int, media_file_id: int, unique_speakers: set[str]
) -> dict[str, int]:
    """
    Create a mapping of speaker labels to database IDs for a specific file.

    Args:
        db: Database session
        user_id: User ID
        media_file_id: Media file ID
        unique_speakers: Set of unique speaker labels

    Returns:
        Dictionary mapping speaker labels to database IDs
    """
    speaker_mapping = {}

    for speaker_id in unique_speakers:
        speaker = create_or_get_speaker(db, user_id, media_file_id, speaker_id)
        speaker_mapping[speaker_id] = int(speaker.id)

    # Transcription creates this user's speakers — bust their speaker cache so
    # the list reflects the new speakers (back-door mutation path). Best-effort.
    if unique_speakers:
        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_speakers(int(user_id))
        except Exception as e:
            logger.debug(f"Speaker cache invalidation failed (non-critical): {e}")

    return speaker_mapping


def process_segments_with_speakers(
    segments: list[dict[str, Any]], speaker_mapping: dict[str, int]
) -> list[dict[str, Any]]:
    """
    Process transcription segments and add speaker database IDs.

    Args:
        segments: List of transcription segments from WhisperX
        speaker_mapping: Mapping of speaker labels to database IDs

    Returns:
        Processed segments with speaker information
    """
    processed_segments = []

    for _, segment in enumerate(segments):
        # Get basic segment info
        segment_start = segment.get("start", 0.0)
        segment_end = segment.get("end", 0.0)
        segment_text = segment.get("text", "")

        # Get speaker ID with fallback.
        # extract_unique_speakers() guarantees every segment already has a non-None
        # speaker label, so this branch is only reached if process_segments_with_speakers
        # is called without a prior extract_unique_speakers pass.  Use SPEAKER_00 for
        # consistency with the canonical fallback defined there.
        speaker_id = segment.get("speaker")
        if speaker_id is None:
            speaker_id = "SPEAKER_00"

        speaker_id = normalize_speaker_label(speaker_id)
        speaker_db_id = speaker_mapping.get(speaker_id)

        # Process word-level timestamps (always available from faster-whisper
        # native word_timestamps=True)
        words_data = []
        if "words" in segment:
            for word in segment["words"]:
                if "start" in word and "end" in word:
                    words_data.append(
                        {
                            "word": word.get("word", ""),
                            "start": word.get("start", 0.0),
                            "end": word.get("end", 0.0),
                            "score": word.get("score", 1.0),
                        }
                    )

        # Create processed segment
        processed_segment = {
            "start": segment_start,
            "end": segment_end,
            "text": segment_text,
            "speaker": speaker_id,
            "speaker_id": speaker_db_id,
            "words": words_data,
            "confidence": segment.get("confidence", 1.0),
        }

        processed_segments.append(processed_segment)

    return processed_segments


def mark_overlapping_segments(
    segments: list[dict[str, Any]],
    overlap_regions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Mark segments that occur during overlapping speech regions.

    Uses NumPy vectorized operations for O(1) overlap detection instead of O(n*m) nested loops.
    Segments with time overlap with detected overlap regions are grouped together
    with a shared overlap_group_id for UI display as simultaneous speech.

    Args:
        segments: List of processed transcript segments
        overlap_regions: List of overlap regions from PyAnnote v4 diarization.
                        Each region has 'start' and 'end' keys

    Returns:
        Updated segments with is_overlap, overlap_group_id, and overlap_confidence set
    """
    import time

    start_time = time.perf_counter()

    if not overlap_regions:
        logger.info("No overlap regions provided, skipping overlap marking")
        return segments

    if not segments:
        logger.info("No segments provided, skipping overlap marking")
        return segments

    import numpy as np

    logger.info(
        f"Marking overlapping segments from {len(overlap_regions)} detected overlap regions "
        f"across {len(segments)} segments (vectorized)"
    )

    # Convert segment times to NumPy arrays for vectorized operations
    seg_starts = np.array([s["start"] for s in segments], dtype=np.float64)
    seg_ends = np.array([s["end"] for s in segments], dtype=np.float64)
    seg_durations = seg_ends - seg_starts

    # Convert region times to NumPy arrays
    region_starts = np.array([r["start"] for r in overlap_regions], dtype=np.float64)
    region_ends = np.array([r["end"] for r in overlap_regions], dtype=np.float64)

    # Vectorized overlap detection using broadcasting
    # Shape: (num_segments, num_regions)
    # Overlap occurs when: seg_start < region_end AND seg_end > region_start
    overlaps_matrix = (seg_starts[:, np.newaxis] < region_ends) & (
        seg_ends[:, np.newaxis] > region_starts
    )

    # Pre-compute all overlap assignments to minimize Python loop overhead
    # This is critical for large files (3700+ segments)

    # Find regions with 2+ overlapping segments (valid overlap groups)
    segments_per_region = overlaps_matrix.sum(axis=0)  # Count per region
    valid_region_mask = segments_per_region >= 2
    valid_region_indices = np.where(valid_region_mask)[0]

    if len(valid_region_indices) == 0:
        logger.info("No valid overlap groups found (need 2+ segments per region)")
        return segments

    # Pre-generate all group IDs at once (batch UUID generation)
    group_ids = [str(uuid_module.uuid4()) for _ in range(len(valid_region_indices))]

    # Build assignment list: (segment_idx, group_id, region_idx) tuples
    # This avoids repeated np.where() calls in a loop
    assignments: list[tuple[int, str, int]] = []
    for i, region_idx in enumerate(valid_region_indices):
        overlapping_indices = np.where(overlaps_matrix[:, region_idx])[0]
        group_id = group_ids[i]
        for seg_idx in overlapping_indices:
            assignments.append((int(seg_idx), group_id, int(region_idx)))

    # Vectorized confidence calculation for all assignments at once
    if assignments:
        assign_seg_indices = np.array([a[0] for a in assignments])
        assign_region_indices = np.array([a[2] for a in assignments])

        # Get segment and region boundaries for all assignments
        assign_seg_starts = seg_starts[assign_seg_indices]
        assign_seg_ends = seg_ends[assign_seg_indices]
        assign_seg_durations = seg_durations[assign_seg_indices]
        assign_region_starts = region_starts[assign_region_indices]
        assign_region_ends = region_ends[assign_region_indices]

        # Vectorized overlap calculation
        overlap_starts = np.maximum(assign_seg_starts, assign_region_starts)
        overlap_ends = np.minimum(assign_seg_ends, assign_region_ends)
        overlap_durations = np.maximum(0.0, overlap_ends - overlap_starts)
        confidences = np.where(
            assign_seg_durations > 0, overlap_durations / assign_seg_durations, 0.0
        )

    # Single pass to update all segment dicts
    overlap_count = 0
    for i, (seg_idx, group_id, _) in enumerate(assignments):
        segments[seg_idx]["is_overlap"] = True
        segments[seg_idx]["overlap_group_id"] = group_id
        segments[seg_idx]["overlap_confidence"] = float(confidences[i])
        overlap_count += 1

    elapsed = time.perf_counter() - start_time
    logger.info(
        f"TIMING: mark_overlapping_segments completed in {elapsed:.3f}s - "
        f"Marked {overlap_count} segments as overlapping speech "
        f"({len(valid_region_indices)} overlap groups, {len(segments)} total segments)"
    )
    return segments
