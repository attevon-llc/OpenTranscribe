"""Shared transcript building utilities.

Functions for formatting transcript segments into text, extracting speaker
statistics, and resolving speaker display names. Used by speaker identification,
summarization, and other transcript-processing tasks.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _seg_text(segment, redaction_cfg=None) -> str:
    """Return a segment's text, masked when an enabled redaction config is supplied.

    Used to keep PII/profane content out of prompts sent to external LLM providers
    (``redact_before_llm``). Masking is read-time; the DB text is never modified.
    """
    text = str(segment.text or "")
    if redaction_cfg is None or not getattr(redaction_cfg, "enabled", False):
        return text
    try:
        from app.services.redaction.service import RedactionService

        masked, _ = RedactionService.mask_segment(
            text, segment.redactions or [], segment.words, redaction_cfg, set()
        )
        return masked
    except Exception:  # noqa: BLE001
        return text


def get_speaker_name(segment) -> str:
    """Get the best available speaker name from a transcript segment.

    Priority: verified display_name > high-confidence suggestion > original label.
    """
    if not segment.speaker:
        return "Unknown Speaker"

    speaker = segment.speaker
    if speaker.display_name and speaker.verified:
        return str(speaker.display_name)
    if speaker.suggested_name and speaker.confidence and speaker.confidence >= 0.75:
        return f"{speaker.suggested_name} (suggested)"
    return str(speaker.name)


def build_full_transcript(transcript_segments, redaction_cfg=None) -> str:
    """Build formatted transcript text from segments with speaker labels and timestamps.

    When ``redaction_cfg`` is enabled, segment text is masked (for ``redact_before_llm``).
    """
    lines = []
    for segment in transcript_segments:
        speaker_name = segment.speaker.name if segment.speaker else "Unknown"
        timestamp = f"[{int(segment.start_time // 60):02d}:{int(segment.start_time % 60):02d}]"
        lines.append(f"{speaker_name}: {timestamp} {_seg_text(segment, redaction_cfg)}")
    return "\n" + "\n".join(lines)


def build_speaker_segments(transcript_segments, limit: int = 50) -> list[dict[str, Any]]:
    """Build speaker segments data for LLM analysis.

    Args:
        transcript_segments: List of TranscriptSegment ORM objects.
        limit: Maximum number of segments to include (default 50).
    """
    return [
        {
            "speaker_label": segment.speaker.name if segment.speaker else "Unknown",
            "start_time": segment.start_time,
            "end_time": segment.end_time,
            "text": segment.text[:200],
        }
        for segment in transcript_segments[:limit]
    ]


def build_transcript_and_stats(
    transcript_segments,
    redaction_cfg=None,
) -> tuple[str, dict[str, Any]]:
    """Build full transcript text and speaker statistics from segments.

    When ``redaction_cfg`` is enabled, segment text is masked (for ``redact_before_llm``);
    word counts use the original text so stats stay accurate.

    Returns:
        Tuple of (transcript_text, speaker_stats_dict).
    """
    full_transcript = ""
    current_speaker: str | None = None
    speaker_stats: dict[str, Any] = {}

    for segment in transcript_segments:
        speaker_name = get_speaker_name(segment)

        segment_duration = segment.end_time - segment.start_time
        if speaker_name not in speaker_stats:
            speaker_stats[speaker_name] = {
                "total_time": 0,
                "segment_count": 0,
                "word_count": 0,
            }
        speaker_stats[speaker_name]["total_time"] += segment_duration
        speaker_stats[speaker_name]["segment_count"] += 1
        speaker_stats[speaker_name]["word_count"] += len(str(segment.text or "").split())

        if speaker_name != current_speaker:
            full_transcript += f"\n\n{speaker_name}: "
            current_speaker = speaker_name
        else:
            full_transcript += " "

        timestamp = f"[{int(segment.start_time // 60):02d}:{int(segment.start_time % 60):02d}]"
        full_transcript += f"{timestamp} {_seg_text(segment, redaction_cfg)}"

    total_time = sum(stats["total_time"] for stats in speaker_stats.values())
    for stats in speaker_stats.values():
        stats["percentage"] = (stats["total_time"] / total_time * 100) if total_time > 0 else 0

    return full_transcript, speaker_stats
