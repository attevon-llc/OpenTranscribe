"""Shared transcript building utilities.

Functions for formatting transcript segments into text, extracting speaker
statistics, and resolving speaker display names. Used by speaker identification,
summarization, and other transcript-processing tasks.
"""

import logging
from typing import Any

from app.core import constants as C  # noqa: N812
from app.utils.speaker_labels import canonical_speaker_label
from app.utils.speaker_labels import canonical_speaker_label_for_row

logger = logging.getLogger(__name__)


def mask_segment_text(segment, redaction_cfg=None) -> str:
    """Return a segment's text, masked when an enabled redaction config is supplied.

    Used to keep PII/profane content out of prompts sent to external LLM providers
    (``redact_before_llm``). Masking is read-time; the DB text is never modified.

    **Fails closed.** A masking error yields ``REDACTION_LLM_FAILSAFE_TEXT``, not the
    original text: callers pass a config precisely because this content must not
    leave the deployment, and handing back the raw string on error would defeat the
    setting at exactly the moment it matters. Losing one segment from a prompt
    degrades a summary; leaking one segment is not recoverable.

    Callers must obtain ``redaction_cfg`` from
    ``services.redaction.llm_guard.resolve_llm_masking``, which is what guarantees
    the cached spans this relies on actually exist.
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
        # FAIL CLOSED. This previously returned the raw text, which is the exact
        # outcome the function exists to prevent: redaction is enabled, so this
        # string is about to be put in a prompt for an EXTERNAL LLM provider, and
        # a masking failure would have sent the unredacted PII off-site.
        # Dropping the segment loses context; leaking it is unrecoverable.
        logger.exception(
            "Redaction masking failed for a segment; withholding its text rather than "
            "sending unmasked content to an external provider"
        )
        return C.REDACTION_LLM_FAILSAFE_TEXT


def get_speaker_name(segment, *, voiceprint_names: bool | None = None) -> str:
    """Get the best available speaker name from a transcript segment.

    Delegates to :func:`app.utils.speaker_labels.canonical_speaker_label` — the single
    home for this resolution, shared with ``ingest_artifacts`` (facts/digest) and
    ``SpeakerStatusService`` so all three planes agree on one label for the same speaker.

    Args:
        segment: Anything with a ``speaker`` (a ``Speaker`` row or a duck-typed stand-in).
        voiceprint_names: The segment's FILE decision (#1280): False leaves out a name a
            voiceprint made. A caller that walks a whole file passes the decision it
            resolved once (``privacy.gates.voiceprints_allowed``). ``None`` asks the policy
            for this one row: correct, but one policy resolution per call.
    """
    if not segment.speaker:
        return canonical_speaker_label(None)

    # `canonical_speaker_label_for_row` tolerates the duck-typed stand-ins callers pass
    # (a missing `suggested_name` degrades to "no suggestion") and withholds a voiceprint
    # name while the file's voiceprints are off (#1280).
    return canonical_speaker_label_for_row(segment.speaker, voiceprint_names=voiceprint_names)


def build_full_transcript(transcript_segments, redaction_cfg=None) -> str:
    """Build formatted transcript text from segments with speaker labels and timestamps.

    When ``redaction_cfg`` is enabled, segment text is masked (for ``redact_before_llm``).

    Speakers are named by their raw diarizer label (``Speaker.name``, which nothing rewrites),
    never by a display name, so there is no voiceprint decision to make here (#1280).
    """
    lines = []
    for segment in transcript_segments:
        speaker_name = segment.speaker.name if segment.speaker else "Unknown"
        timestamp = f"[{int(segment.start_time // 60):02d}:{int(segment.start_time % 60):02d}]"
        lines.append(f"{speaker_name}: {timestamp} {mask_segment_text(segment, redaction_cfg)}")
    return "\n" + "\n".join(lines)


def build_speaker_segments(
    transcript_segments, limit: int = 50, redaction_cfg=None
) -> list[dict[str, Any]]:
    """Build speaker segments data for LLM analysis.

    Masking is applied *before* truncation, so the 200-character window can never
    slice a mask open and expose the tail of a redacted span.

    Args:
        transcript_segments: List of TranscriptSegment ORM objects.
        limit: Maximum number of segments to include (default 50).
        redaction_cfg: Effective config from ``resolve_llm_masking``, or None when
            the owner's policy does not require pre-LLM masking. This payload goes
            off-box exactly like ``build_full_transcript``'s, so it needs the same
            masking.

    Like :func:`build_full_transcript`, labels are the raw diarizer label: nothing here
    depends on the voiceprint switch.
    """
    return [
        {
            "speaker_label": segment.speaker.name if segment.speaker else "Unknown",
            "start_time": segment.start_time,
            "end_time": segment.end_time,
            "text": mask_segment_text(segment, redaction_cfg)[:200],
        }
        for segment in transcript_segments[:limit]
    ]


def compute_speaker_stats(
    transcript_segments, *, voiceprint_names: bool | None = None
) -> dict[str, Any]:
    """Per-speaker talk time, segment count, word count and share of the conversation.

    Split out of :func:`build_transcript_and_stats` for #383 Phase 2: these numbers were
    computed on every summarization run and then **thrown away**, and were never computed
    at all on a deployment with no LLM — yet they are the exact answer to "who talked
    most / how long / who was in this". ``services/ingest_artifacts`` now persists them
    (``file_facts``) on the transcription-completion path instead.

    Reads the original text for word counts, never the masked text, so redaction settings
    cannot change a statistic.

    Args:
        transcript_segments: ``TranscriptSegment`` rows, in any order — these are order-
            independent aggregates.
        voiceprint_names: The file's voiceprint decision, as for :func:`get_speaker_name`.
            Statistics are keyed by the label, so a hidden name must not key them either.

    Returns:
        ``{speaker_name: {"total_time", "segment_count", "word_count", "percentage"}}``.
    """
    speaker_stats: dict[str, Any] = {}

    for segment in transcript_segments:
        speaker_name = get_speaker_name(segment, voiceprint_names=voiceprint_names)
        if speaker_name not in speaker_stats:
            speaker_stats[speaker_name] = {
                "total_time": 0,
                "segment_count": 0,
                "word_count": 0,
            }
        speaker_stats[speaker_name]["total_time"] += segment.end_time - segment.start_time
        speaker_stats[speaker_name]["segment_count"] += 1
        speaker_stats[speaker_name]["word_count"] += len(str(segment.text or "").split())

    total_time = sum(stats["total_time"] for stats in speaker_stats.values())
    for stats in speaker_stats.values():
        stats["percentage"] = (stats["total_time"] / total_time * 100) if total_time > 0 else 0

    return speaker_stats


def build_transcript_and_stats(
    transcript_segments,
    redaction_cfg=None,
    *,
    voiceprint_names: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build full transcript text and speaker statistics from segments.

    When ``redaction_cfg`` is enabled, segment text is masked (for ``redact_before_llm``);
    word counts use the original text so stats stay accurate. ``voiceprint_names`` is the
    file's voiceprint decision (see :func:`get_speaker_name`).

    Returns:
        Tuple of (transcript_text, speaker_stats_dict).
    """
    full_transcript = ""
    current_speaker: str | None = None

    for segment in transcript_segments:
        speaker_name = get_speaker_name(segment, voiceprint_names=voiceprint_names)

        if speaker_name != current_speaker:
            full_transcript += f"\n\n{speaker_name}: "
            current_speaker = speaker_name
        else:
            full_transcript += " "

        timestamp = f"[{int(segment.start_time // 60):02d}:{int(segment.start_time % 60):02d}]"
        full_transcript += f"{timestamp} {mask_segment_text(segment, redaction_cfg)}"

    return full_transcript, compute_speaker_stats(
        transcript_segments, voiceprint_names=voiceprint_names
    )
