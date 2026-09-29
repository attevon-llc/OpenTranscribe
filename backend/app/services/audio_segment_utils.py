"""
Shared audio segment utilities for speaker analysis pipelines.

Pure functions — no model or DB dependencies. Used by:
- SpeakerEmbeddingService (PyAnnote embedding extraction)
- SpeakerAttributeService (wav2vec2 gender detection)
- migration_pipeline (batch processing infrastructure)
- Per-file speaker analysis tasks
"""

import logging
import subprocess
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def merge_adjacent_segments(
    segments: list[dict[str, Any]],
    max_gap: float = 0.5,
) -> list[dict[str, Any]]:
    """Merge adjacent/overlapping segments into continuous speaking sections.

    Diarization often splits continuous speech into many short segments at
    sentence or pause boundaries. Merging reconstructs actual speaking turns,
    producing longer audio clips for higher-fidelity analysis.

    Args:
        segments: List of dicts with "start" and "end" float keys.
        max_gap: Maximum gap in seconds between segments to still merge.

    Returns:
        Merged list sorted by start time.
    """
    if len(segments) <= 1:
        return [dict(s) for s in segments]

    sorted_segs = sorted(segments, key=lambda x: x["start"])
    merged = [dict(sorted_segs[0])]

    for seg in sorted_segs[1:]:
        prev = merged[-1]
        if seg["start"] - prev["end"] <= max_gap:
            prev["end"] = max(prev["end"], seg["end"])
        else:
            merged.append(dict(seg))

    return merged


def select_top_segments(
    segments: list[dict[str, Any]],
    min_duration: float = 1.0,
    max_segments: int = 5,
) -> list[dict[str, Any]]:
    """Select the longest segments above a minimum duration threshold.

    Args:
        segments: List of dicts with "start" and "end" keys.
        min_duration: Minimum segment duration in seconds.
        max_segments: Maximum number of segments to return.

    Returns:
        Top segments sorted by duration (longest first).
    """
    sorted_segs = sorted(
        segments,
        key=lambda s: s["end"] - s["start"],
        reverse=True,
    )
    return [s for s in sorted_segs[:max_segments] if s["end"] - s["start"] >= min_duration]


def center_window(segment: dict[str, Any], max_duration: float) -> dict[str, Any]:
    """Shrink a segment to at most ``max_duration`` seconds around its midpoint.

    For a model with a fixed useful input length: fetching the rest of a multi-minute
    speaking turn only to throw it away costs bandwidth and decode memory.
    """
    duration = segment["end"] - segment["start"]
    if duration <= max_duration:
        return dict(segment)
    start = segment["start"] + (duration - max_duration) / 2
    return {**segment, "start": start, "end": start + max_duration}


def extract_audio_segment_np(
    audio_source: str,
    start: float,
    duration: float,
    target_sr: int = 16000,
) -> np.ndarray | None:
    """Extract a specific audio segment via ffmpeg seeking.

    Uses -ss before -i for fast demuxer-level seek (no full file decode).
    Works with local file paths and presigned HTTP URLs.

    Args:
        audio_source: Local file path or HTTP/presigned URL.
        start: Segment start time in seconds.
        duration: Segment duration in seconds.
        target_sr: Target sample rate.

    Returns:
        1-D float32 numpy array or None on failure.
    """
    if duration <= 0:
        logger.debug("Skipping segment with non-positive duration: %.3f", duration)
        return None

    cmd = [
        "ffmpeg",
        "-ss",
        str(max(0.0, start)),
        "-i",
        audio_source,
        "-t",
        str(duration),
        "-vn",
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "-ac",
        "1",
        "-ar",
        str(target_sr),
        "-v",
        "quiet",
        "-",
    ]
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603
            cmd,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0 and len(proc.stdout) > 0:
            return np.frombuffer(proc.stdout, dtype=np.float32).copy()
        if proc.returncode != 0:
            logger.debug(
                "ffmpeg exited with code %d for start=%.2f dur=%.2f",
                proc.returncode,
                start,
                duration,
            )
    except Exception as e:
        logger.debug("Segment extraction failed: %s", e)
    return None


def load_full_audio_np(
    audio_path: str,
    target_sr: int = 16000,
) -> np.ndarray:
    """Load entire audio file to float32 numpy array via ffmpeg.

    Args:
        audio_path: Path to the audio file.
        target_sr: Target sample rate.

    Returns:
        1-D float32 numpy array (mono, normalized).

    Raises:
        subprocess.CalledProcessError: If ffmpeg fails.
    """
    cmd = [
        "ffmpeg",
        "-i",
        audio_path,
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "-ac",
        "1",
        "-ar",
        str(target_sr),
        "-v",
        "quiet",
        "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, check=True, timeout=300)  # noqa: S603  # nosec B603
    return np.frombuffer(result.stdout, dtype=np.float32).copy()


def group_segments_by_speaker(
    segments: list[dict[str, Any]],
    speaker_mapping: dict[str, int],
) -> dict[int, list[dict[str, Any]]]:
    """Group transcript segments by speaker database ID.

    Args:
        segments: List of dicts with "speaker", "start", "end" keys.
        speaker_mapping: Maps speaker label to database speaker ID.

    Returns:
        Dict mapping speaker ID to list of their segments.
    """
    grouped: dict[int, list[dict[str, Any]]] = {}
    for seg in segments:
        label = seg.get("speaker")
        if not label:
            continue
        speaker_id = speaker_mapping.get(label)
        if speaker_id is None:
            continue
        if speaker_id not in grouped:
            grouped[speaker_id] = []
        grouped[speaker_id].append(seg)
    return grouped
