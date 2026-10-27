"""Server-side transcript export serialization (txt / json / csv / srt / vtt / md).

Ports the client-side exporter that used to live at
``frontend/src/lib/export/transcriptExport.ts`` to the backend (issue #673). That module
serialized already-downloaded transcript data in the browser, which meant the admin
``export_locked`` floor (mandated censored exports) was enforced for subtitle downloads
(``files/subtitles.py``) and nowhere else — every other export format simply never asked.

This module and its caller (``api/endpoints/files/transcript_export.py``) follow the exact
fail-closed pattern documented in ``files/subtitles.py``: an unresolvable redaction policy
withholds the export (503) rather than defaulting to unmasked, and ``export_locked`` always
wins over a per-request ``redact=false``.

TXT / JSON / CSV keep the retired client-side serializer's layout. SRT and WebVTT do not:
they are cut into broadcast-style cues by ``app.services.captions``, the segmenter the
file-detail player's caption track uses, so a downloaded caption file matches the player.
Markdown (issue #564) is its own builder with YAML front matter.

The module is the dispatcher; the formats live beside it. ``transcript_export_turns`` is the
shared seam (one masking path, one turn-grouping rule, one chronological merge),
``transcript_export_text`` holds TXT/JSON/CSV and ``transcript_export_markdown`` holds ``md``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from app.services.redaction.config import EffectiveRedactionConfig

from app.models.media import Comment
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.services.transcript_export_markdown import MarkdownMeta
from app.services.transcript_export_markdown import build_markdown
from app.services.transcript_export_text import build_csv
from app.services.transcript_export_text import build_json
from app.services.transcript_export_text import build_txt
from app.services.transcript_export_turns import comment_rows_from
from app.services.transcript_export_turns import group_turns
from app.services.transcript_export_turns import masked_segments
from app.services.transcript_export_turns import speaker_namer
from app.services.transcript_export_turns import voiceprint_names_for
from app.utils.speaker_labels import canonical_speaker_label_for_row

logger = logging.getLogger(__name__)

VALID_FORMATS = ("txt", "json", "csv", "srt", "vtt", "md")


def build_export_content(
    *,
    export_format: str,
    segments: list[TranscriptSegment],
    speakers: list[Speaker],
    comments: list[Comment] | None,
    include_comments: bool,
    include_timestamps: bool = True,
    include_speakers: bool = True,
    filename: str | None = None,
    duration: float | None = None,
    redaction_cfg: EffectiveRedactionConfig | None = None,
    reveal_categories: set | None = None,
    speaker_default_label: str = "Speaker",
    user_comment_label: str = "USER COMMENT",
    comment_type_label: str = "COMMENT",
    csv_header_default: str = "Start Time,End Time,Speaker,Text",
    csv_header_with_comments: str = "Start Time,End Time,Speaker,Text,Comment Type",
    markdown_meta: MarkdownMeta | None = None,
    voiceprint_names: bool | None = None,
) -> str:
    """Serialize transcript segments (+ optional comments) into the requested format.

    ``voiceprint_names`` is the file's voiceprint decision (#1280): False prints the diarizer
    label instead of a name a voiceprint made. ``None`` resolves it once from ``speakers``
    (fail closed), so a caller that does not have it still gets the policy.

    ``export_format`` is one of ``VALID_FORMATS``. Segments must already be sorted by
    ``start_time`` — callers query in that order (matches every other export surface).
    ``markdown_meta`` supplies the front matter for ``md``; without it the title is the
    ``filename`` and only the redaction flag is written.
    """
    if export_format not in VALID_FORMATS:
        raise ValueError(f"Unsupported export format: {export_format}")

    if voiceprint_names is None:
        voiceprint_names = voiceprint_names_for(speakers)
    speaker_map: dict[int, str] = {
        int(sp.id): canonical_speaker_label_for_row(sp, voiceprint_names=voiceprint_names)
        for sp in speakers
    }
    speaker_name = speaker_namer(speakers, speaker_default_label, voiceprint_names=voiceprint_names)
    comment_rows = comment_rows_from(comments, include_comments)

    if export_format in ("srt", "vtt"):
        return _build_cues(
            export_format,
            segments,
            speaker_map,
            comment_rows,
            user_comment_label,
            redaction_cfg,
            reveal_categories,
        )

    masked = masked_segments(segments, redaction_cfg, reveal_categories)
    masked.sort(key=lambda s: s["start_time"])

    if export_format == "txt":
        return build_txt(
            group_turns(masked, speaker_name),
            comment_rows,
            include_timestamps,
            include_speakers,
            user_comment_label,
        )
    if export_format == "json":
        return build_json(masked, speaker_name, comment_rows, filename, duration)
    if export_format == "md":
        return build_markdown(
            group_turns(masked, speaker_name),
            comment_rows,
            markdown_meta or MarkdownMeta(title=filename or "Transcript"),
            include_timestamps=include_timestamps,
            include_speakers=include_speakers,
            user_comment_label=user_comment_label,
            default_speaker_label=speaker_default_label,
        )
    if export_format == "csv":
        return build_csv(
            masked,
            speaker_name,
            comment_rows,
            user_comment_label,
            comment_type_label,
            csv_header_default,
            csv_header_with_comments,
        )
    # A format added to VALID_FORMATS without a builder must fail loudly: this used to be a
    # bare ``return build_csv(...)``, which served any such token as CSV with HTTP 200.
    raise AssertionError(f"No export builder for format {export_format!r}")


def _build_cues(
    export_format: str,
    segments: list[TranscriptSegment],
    speaker_map: dict[int, str],
    comment_rows: list[dict[str, Any]],
    user_comment_label: str,
    redaction_cfg: EffectiveRedactionConfig | None,
    reveal_categories: set | None,
) -> str:
    """SRT / WebVTT through the shared caption segmenter, comments as their own cues.

    The cues are the ones the file-detail player shows (``app.services.captions``), so
    a downloaded caption file and the player agree. Comments are not speech: each keeps
    its two-second cue at its timestamp, beside whatever is being said.
    """
    from app.services.captions import LATIN_RULES
    from app.services.captions import Cue
    from app.services.captions import CuePart
    from app.services.captions import build_caption_track
    from app.services.captions import to_srt
    from app.services.captions import to_webvtt
    from app.services.captions.source import caption_segments_from_rows

    track = build_caption_track(
        caption_segments_from_rows(
            segments,
            speaker_map,
            include_speakers=True,
            redaction_cfg=redaction_cfg,
            reveal_categories=reveal_categories,
        )
    )
    comments = [
        Cue(
            c["timestamp"],
            c["timestamp"] + 2,
            [CuePart(None, None, False, [f"{user_comment_label}: {c['user_name']}: {c['text']}"])],
            LATIN_RULES,
        )
        for c in comment_rows
    ]
    if export_format == "srt":
        return to_srt(track, comments)
    return to_webvtt(track, comments)
