"""Post-processing for transcript segments: speaker-based resegmentation and merging."""

from __future__ import annotations

from typing import Any


def resegment_by_speaker(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split segments at speaker boundaries so each segment has exactly one speaker.

    Words within a segment may have different speaker labels after diarization.
    This function splits such segments into sub-segments grouped by consecutive
    same-speaker words.

    Segments without words or with a single speaker pass through unchanged.
    """
    result: list[dict[str, Any]] = []

    for segment in segments:
        words = segment.get("words")
        if not words:
            result.append(segment)
            continue

        # Fast path: check if all words share the same speaker
        speakers = {w.get("speaker") for w in words if "speaker" in w}
        if len(speakers) <= 1:
            result.append(segment)
            continue

        # Group consecutive words by speaker
        groups: list[list[dict[str, Any]]] = []
        current_group: list[dict[str, Any]] = [words[0]]

        for word in words[1:]:
            if word.get("speaker") == current_group[-1].get("speaker"):
                current_group.append(word)
            else:
                groups.append(current_group)
                current_group = [word]
        groups.append(current_group)

        # Build sub-segments from each group
        confidence = segment.get("confidence", 1.0)
        for group in groups:
            text = "".join(w.get("word", "") for w in group).strip()
            if not text:
                continue
            result.append(
                {
                    "start": group[0].get("start", segment["start"]),
                    "end": group[-1].get("end", segment["end"]),
                    "text": text,
                    "speaker": group[0].get("speaker"),
                    "words": group,
                    "confidence": confidence,
                }
            )

    return result


def merge_consecutive_segments(
    segments: list[dict[str, Any]],
    max_duration: float = 30.0,
) -> list[dict[str, Any]]:
    """Merge adjacent segments that share the same speaker.

    After resegmentation, many small consecutive segments from the same speaker
    may exist. This merges them into larger, more natural segments.

    Args:
        segments: List of segment dicts with start/end/text/speaker/words keys.
        max_duration: Maximum duration (seconds) for a merged segment. Prevents
            unbounded merging when a provider returns only one speaker, which
            would collapse the entire transcript into a single segment.
    """
    if not segments:
        return []

    result: list[dict[str, Any]] = []
    current = _copy_segment(segments[0])

    for segment in segments[1:]:
        current_duration = current.get("end", 0) - current.get("start", 0)
        same_speaker = segment.get("speaker") and segment.get("speaker") == current.get("speaker")
        if same_speaker and current_duration < max_duration:
            # Merge into current
            current["end"] = segment["end"]
            current["text"] = current["text"] + " " + segment.get("text", "")
            current_words = current.get("words") or []
            seg_words = segment.get("words") or []
            current["words"] = current_words + seg_words
        else:
            result.append(current)
            current = _copy_segment(segment)

    result.append(current)
    return result


def _copy_segment(segment: dict[str, Any]) -> dict[str, Any]:
    """Shallow copy a segment dict, deep-copying the words list."""
    copied = dict(segment)
    if copied.get("words"):
        copied["words"] = list(copied["words"])
    return copied


def finalize_segments(
    segments: list[dict[str, Any]],
    smoothing_config: Any = None,
) -> list[dict[str, Any]]:
    """Single post-diarization chokepoint: optional boundary smoothing → resegment → merge.

    Pure function of segments + config. This is the one place every transcription path
    (legacy pipeline, combined engine, split engine, multi-GPU) routes through for
    speaker-boundary post-processing, and the benchmark harness (issue #193) calls it
    verbatim. With ``smoothing_config`` disabled or ``None`` it is byte-identical to the
    previous ``merge_consecutive_segments(resegment_by_speaker(segments))``.

    Args:
        segments: Word-level-labeled segments (each word carries a ``speaker``).
        smoothing_config: A ``BoundarySmoothingConfig`` (or None). Smoothing only runs
            when ``smoothing_config.enabled`` is truthy.
    """
    if smoothing_config is not None and getattr(smoothing_config, "enabled", False):
        from app.transcription.boundary_resolver import smooth_word_speakers

        segments = smooth_word_speakers(segments, smoothing_config)
    return merge_consecutive_segments(resegment_by_speaker(segments))
