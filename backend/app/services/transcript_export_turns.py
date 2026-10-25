"""The shared seam under every transcript export format (issue #564).

One masking path (:func:`masked_segments`), one turn-grouping rule (:func:`group_turns`) and one
chronological merge (:func:`merge_by_timestamp`), so the TXT, JSON, CSV and Markdown builders
cannot disagree about what a transcript says. Everything here is a pure function of data that
is already masked plus caller-supplied labels: no database, no session, which is what lets a
batch collector reuse the builders (issue #796).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from app.services.redaction.config import EffectiveRedactionConfig

from app.models.media import Comment
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.utils.speaker_labels import canonical_speaker_label_for_row

logger = logging.getLogger(__name__)

#: Resolves a masked segment dict to the speaker label shown for it.
SpeakerNamer = Callable[[dict[str, Any]], str]


def format_duration(total_seconds: float) -> str:
    """``HH:MM:SS`` (or ``MM:SS`` under an hour) — matches the retired
    ``formatDuration`` in ``frontend/src/lib/utils/formatting.ts``."""
    if total_seconds != total_seconds or total_seconds < 0:  # NaN check
        return "00:00"
    total_seconds = int(total_seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_clock(total_seconds: float) -> str:
    """Always ``HH:MM:SS`` (a fixed width, for machine-read documents)."""
    if total_seconds != total_seconds or total_seconds < 0:  # NaN check
        return "00:00:00"
    total = int(total_seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def masked_segments(
    segments: list[TranscriptSegment],
    redaction_cfg: EffectiveRedactionConfig | None,
    reveal_categories: set | None,
) -> list[dict[str, Any]]:
    """Return ``[{text, start_time, end_time, speaker_id}, ...]`` with masked text.

    Mirrors ``SubtitleService._redact_segments_inplace``: masking is applied to a
    detached in-memory copy, never written back to the ORM session. This module never
    holds a `session_scope`-committing session, but expunging first keeps the guarantee
    structural rather than a property of the caller.
    """
    from sqlalchemy.orm import object_session

    from app.services.redaction.export_policy import plain_text_policy

    enabled = bool(redaction_cfg is not None and getattr(redaction_cfg, "enabled", False))
    if redaction_cfg is not None:
        redaction_cfg = plain_text_policy(redaction_cfg)
    out = []
    for seg in segments:
        text = str(seg.text or "")
        if enabled and redaction_cfg is not None:
            session = object_session(seg)
            if session is not None:
                session.expunge(seg)
            try:
                from app.services.redaction.service import RedactionService

                text, _ = RedactionService.mask_segment(
                    text,
                    seg.redactions or [],
                    seg.words,
                    redaction_cfg,
                    reveal_categories or set(),
                )
            except Exception:
                # FAIL CLOSED — see SubtitleService._redact_segments_inplace for why a
                # masking failure must never fall back to the raw text.
                logger.exception(
                    "Redaction masking failed for a segment; withholding its text from export"
                )
                text = "[redacted — masking unavailable]"
        out.append(
            {
                "text": text,
                "start_time": float(seg.start_time or 0.0),
                "end_time": float(seg.end_time or 0.0),
                "speaker_id": seg.speaker_id,
            }
        )
    return out


def comment_rows_from(
    comments: list[Comment] | None, include_comments: bool
) -> list[dict[str, Any]]:
    """Plain ``{timestamp, text, user_name}`` rows, chronological, or ``[]`` when excluded."""
    if not (include_comments and comments):
        return []
    rows: list[dict[str, Any]] = []
    for c in sorted(comments, key=lambda c: c.timestamp or 0.0):
        user_name = "Anonymous"
        if c.user is not None:
            user_name = (
                getattr(c.user, "full_name", None)
                or getattr(c.user, "username", None)
                or getattr(c.user, "email", None)
                or "Anonymous"
            )
        rows.append(
            {"timestamp": float(c.timestamp or 0.0), "text": c.text, "user_name": user_name}
        )
    return rows


def speaker_namer(speakers: list[Speaker], default_label: str) -> SpeakerNamer:
    """A :data:`SpeakerNamer` over ``speakers``: a segment with no (or an unknown) speaker gets
    ``default_label``, every other one its canonical label."""
    names: dict[int, str] = {int(sp.id): canonical_speaker_label_for_row(sp) for sp in speakers}

    def speaker_name(seg: dict[str, Any]) -> str:
        if seg["speaker_id"] is None:
            return default_label
        return names.get(seg["speaker_id"], default_label)

    return speaker_name


@dataclass
class Turn:
    """Consecutive segments by the same speaker, as one block of speech."""

    speaker: str
    start: float
    end: float
    texts: list[str]

    @property
    def text(self) -> str:
        """The turn's segments joined into one paragraph."""
        return " ".join(self.texts)


def group_turns(masked: list[dict[str, Any]], speaker_name: SpeakerNamer) -> list[Turn]:
    """Fold consecutive same-speaker segments into :class:`Turn` blocks."""
    turns: list[Turn] = []
    for seg in masked:
        speaker = speaker_name(seg)
        if turns and turns[-1].speaker == speaker:
            turns[-1].end = seg["end_time"]
            turns[-1].texts.append(seg["text"])
        else:
            turns.append(Turn(speaker, seg["start_time"], seg["end_time"], [seg["text"]]))
    return turns


def merge_by_timestamp[T](
    primary: list[tuple[float, T]], secondary: list[tuple[float, T]]
) -> list[T]:
    """Chronological merge of two ``(timestamp, item)`` lists — ``primary`` wins ties."""
    merged: list[T] = []
    pi = si = 0
    while pi < len(primary) and si < len(secondary):
        if primary[pi][0] <= secondary[si][0]:
            merged.append(primary[pi][1])
            pi += 1
        else:
            merged.append(secondary[si][1])
            si += 1
    merged.extend(item for _, item in primary[pi:])
    merged.extend(item for _, item in secondary[si:])
    return merged
