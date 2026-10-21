"""Load a file's transcript as caption input: masked text, canonical speaker labels.

The ONE place a caption surface reads segments from the database, so the player track
and every SRT/WebVTT download apply the same two rules:

- **Masking.** The caller's resolved redaction policy is applied to each segment's
  cached spans (``RedactionService.mask_segment``, the one masker), with ``blur``
  replaced by ``label`` because a caption has no CSS to blur with
  (``export_policy.plain_text_policy``). A masking failure withholds the segment's text
  rather than falling back to the original. Nothing is written to the ORM objects: the
  masked text lives only in the returned :class:`CaptionSegment`, so a caller holding a
  committing session (both export workers) can never flush it back.
- **Speaker names.** ``utils.speaker_labels.canonical_speaker_label_for_row``: a name a
  human typed or verified, else a voiceprint match at >= 0.75, else the diarizer's
  placeholder. Never an LLM guess. A placeholder (``SPEAKER_03``) can be rendered
  through a localised template (``"Speaker {number}"``) supplied by the caller.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.services.captions.model import CaptionSegment
from app.utils.speaker_labels import canonical_speaker_label_for_row

if TYPE_CHECKING:
    from app.services.redaction.config import EffectiveRedactionConfig

logger = logging.getLogger(__name__)

#: Shown in place of a segment whose masking failed (fail closed).
MASKING_UNAVAILABLE_TEXT = "[redacted — masking unavailable]"

_PLACEHOLDER_RE = re.compile(r"^SPEAKER[_-]?(\d+)$", re.IGNORECASE)


def render_speaker_label(label: str, placeholder_template: str | None) -> str:
    """Render a diarizer placeholder through ``placeholder_template`` (``{number}``)."""
    match = _PLACEHOLDER_RE.match(label.strip())
    if match and placeholder_template and "{number}" in placeholder_template:
        return placeholder_template.replace("{number}", match.group(1))
    return label


def speaker_labels_for(
    db: Session, speaker_ids: set[int], placeholder_template: str | None = None
) -> dict[int, str]:
    """``{speaker_id: canonical display label}`` for ``speaker_ids``."""
    if not speaker_ids:
        return {}
    rows = (
        db.query(
            Speaker.id,
            Speaker.name,
            Speaker.display_name,
            Speaker.suggested_name,
            Speaker.confidence,
            Speaker.suggestion_source,
        )
        .filter(Speaker.id.in_(speaker_ids))
        .all()
    )
    return {
        int(row.id): render_speaker_label(
            canonical_speaker_label_for_row(row), placeholder_template
        )
        for row in rows
    }


def _masked_text(
    seg: TranscriptSegment,
    cfg: EffectiveRedactionConfig | None,
    reveal_categories: set[str] | None,
) -> tuple[str, bool]:
    """``(display_text, ok)``; ``ok`` is False when masking failed and text was withheld."""
    text = str(seg.text or "")
    if cfg is None or not cfg.enabled:
        return text, True
    from app.services.redaction.service import RedactionService

    try:
        masked, _ = RedactionService.mask_segment(
            text, seg.redactions or [], seg.words, cfg, reveal_categories or set()
        )
    except Exception:
        logger.exception("Redaction masking failed for a segment; withholding its caption text")
        return MASKING_UNAVAILABLE_TEXT, False
    return masked, True


def caption_segments_from_rows(
    segments: list[TranscriptSegment],
    speaker_labels: dict[int, str],
    *,
    include_speakers: bool,
    redaction_cfg: EffectiveRedactionConfig | None,
    reveal_categories: set[str] | None = None,
) -> list[CaptionSegment]:
    """Convert loaded segment rows to segmenter input, masking each one.

    Args:
        segments: ``TranscriptSegment`` rows of one file.
        speaker_labels: ``{speaker_id: label}`` as built by :func:`load_caption_segments`.
        include_speakers: Whether cues carry speaker names at all.
        redaction_cfg: The caller's resolved policy (``None``/disabled masks nothing).
        reveal_categories: Categories the caller is allowed to see unmasked.

    Returns:
        One :class:`CaptionSegment` per row with text.
    """
    from app.services.redaction.export_policy import plain_text_policy

    cfg = plain_text_policy(redaction_cfg) if redaction_cfg is not None else None
    out: list[CaptionSegment] = []
    for seg in segments:
        original = str(seg.text or "")
        display, ok = _masked_text(seg, cfg, reveal_categories)
        speaker_id = int(seg.speaker_id) if seg.speaker_id is not None else None
        out.append(
            CaptionSegment(
                start=float(seg.start_time or 0.0),
                end=float(seg.end_time or seg.start_time or 0.0),
                text=display,
                speaker_key=speaker_id,
                speaker_label=(
                    speaker_labels.get(speaker_id)
                    if include_speakers and speaker_id is not None
                    else None
                ),
                # Word timing is only usable when the words still describe the text:
                # a withheld segment has neither, and is timed by interpolation.
                words=list(seg.words) if ok and seg.words else None,
                source_text=original if ok and display != original else None,
                overlap_group=seg.overlap_group_id,
            )
        )
    return out


def load_caption_segments(
    db: Session,
    media_file_id: int,
    *,
    include_speakers: bool = True,
    redaction_cfg: EffectiveRedactionConfig | None,
    reveal_categories: set[str] | None = None,
    placeholder_template: str | None = None,
) -> list[CaptionSegment]:
    """Read a file's segments as masked, labelled caption input.

    Args:
        db: Database session (read only; nothing is mutated or flushed).
        media_file_id: Internal file id.
        include_speakers: Whether cues carry speaker names.
        redaction_cfg: The caller's resolved redaction policy.
        reveal_categories: Categories the caller may see unmasked.
        placeholder_template: How to render a diarizer placeholder, e.g.
            ``"Speaker {number}"``; ``None`` keeps the raw ``SPEAKER_00``.

    Returns:
        The segments in time order.

    Raises:
        ValueError: The file has no transcript segments.
    """
    segments = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == media_file_id)
        .order_by(TranscriptSegment.start_time, TranscriptSegment.end_time, TranscriptSegment.id)
        .all()
    )
    if not segments:
        raise ValueError("No transcript segments found for this media file")
    labels = (
        speaker_labels_for(
            db, {int(s.speaker_id) for s in segments if s.speaker_id}, placeholder_template
        )
        if include_speakers
        else {}
    )
    return caption_segments_from_rows(
        segments,
        labels,
        include_speakers=include_speakers,
        redaction_cfg=redaction_cfg,
        reveal_categories=reveal_categories,
    )
