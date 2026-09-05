"""Server-side transcript export serialization (txt / json / csv / srt / vtt).

Ports the client-side exporter that used to live at
``frontend/src/lib/export/transcriptExport.ts`` to the backend (issue #673). That module
serialized already-downloaded transcript data in the browser, which meant the admin
``export_locked`` floor (mandated censored exports) was enforced for subtitle downloads
(``files/subtitles.py``) and nowhere else — every other export format simply never asked.

This module and its caller (``api/endpoints/files/transcript_export.py``) follow the exact
fail-closed pattern documented in ``files/subtitles.py``: an unresolvable redaction policy
withholds the export (503) rather than defaulting to unmasked, and ``export_locked`` always
wins over a per-request ``redact=false``.

Output is intentionally byte-compatible with the retired client-side serializer so removing
it does not change what users download.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from app.services.redaction.config import EffectiveRedactionConfig

from app.models.media import Comment
from app.models.media import Speaker
from app.models.media import TranscriptSegment

logger = logging.getLogger(__name__)

VALID_FORMATS = ("txt", "json", "csv", "srt", "vtt")


def _format_duration(total_seconds: float) -> str:
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


def _format_srt_timestamp(total_seconds: float) -> str:
    """``HH:MM:SS,mmm`` — matches the retired ``formatSrtTimestamp``."""
    if total_seconds < 0 or total_seconds != total_seconds:
        total_seconds = 0.0
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    millis = int(round((total_seconds - int(total_seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _format_vtt_timestamp(total_seconds: float) -> str:
    return _format_srt_timestamp(total_seconds).replace(",", ".")


def _redact_segments(
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

    enabled = bool(redaction_cfg is not None and getattr(redaction_cfg, "enabled", False))
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
) -> str:
    """Serialize transcript segments (+ optional comments) into the requested format.

    ``export_format`` is one of ``VALID_FORMATS``. Segments must already be sorted by
    ``start_time`` — callers query in that order (matches every other export surface).
    """
    if export_format not in VALID_FORMATS:
        raise ValueError(f"Unsupported export format: {export_format}")

    speaker_map: dict[int, str] = {}
    for sp in speakers:
        speaker_map[sp.id] = str(sp.display_name) if sp.display_name else str(sp.name)

    masked = _redact_segments(segments, redaction_cfg, reveal_categories)
    masked.sort(key=lambda s: s["start_time"])

    def speaker_name(seg: dict[str, Any]) -> str:
        if seg["speaker_id"] is None:
            return speaker_default_label
        return speaker_map.get(seg["speaker_id"], speaker_default_label)

    comment_rows: list[dict[str, Any]] = []
    if include_comments and comments:
        sorted_comments = sorted(comments, key=lambda c: c.timestamp or 0.0)
        for c in sorted_comments:
            user_name = "Anonymous"
            if c.user is not None:
                user_name = (
                    getattr(c.user, "full_name", None)
                    or getattr(c.user, "username", None)
                    or getattr(c.user, "email", None)
                    or "Anonymous"
                )
            comment_rows.append(
                {"timestamp": float(c.timestamp or 0.0), "text": c.text, "user_name": user_name}
            )

    if export_format == "txt":
        return _build_txt(
            masked,
            speaker_name,
            comment_rows,
            include_timestamps,
            include_speakers,
            user_comment_label,
        )
    if export_format == "json":
        return _build_json(masked, speaker_name, comment_rows, filename, duration)
    if export_format == "csv":
        return _build_csv(
            masked,
            speaker_name,
            comment_rows,
            user_comment_label,
            comment_type_label,
            csv_header_default,
            csv_header_with_comments,
        )
    if export_format == "srt":
        return _build_srt(masked, speaker_name, comment_rows, user_comment_label)
    return _build_vtt(masked, speaker_name, comment_rows, user_comment_label)


def _merge_by_timestamp(
    segment_lines: list[tuple[float, str]], comment_lines: list[tuple[float, str]]
) -> list[str]:
    """Chronological merge of two ``(timestamp, line)`` lists — segments win ties."""
    merged: list[str] = []
    si = ci = 0
    while si < len(segment_lines) and ci < len(comment_lines):
        if segment_lines[si][0] <= comment_lines[ci][0]:
            merged.append(segment_lines[si][1])
            si += 1
        else:
            merged.append(comment_lines[ci][1])
            ci += 1
    merged.extend(line for _, line in segment_lines[si:])
    merged.extend(line for _, line in comment_lines[ci:])
    return merged


def _build_txt(
    masked: list[dict[str, Any]],
    speaker_name,
    comment_rows: list[dict[str, Any]],
    include_timestamps: bool,
    include_speakers: bool,
    user_comment_label: str,
) -> str:
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for seg in masked:
        sp = speaker_name(seg)
        if current is not None and current["speaker"] == sp:
            current["end"] = seg["end_time"]
            current["texts"].append(seg["text"])
        else:
            if current is not None:
                groups.append(current)
            current = {
                "speaker": sp,
                "start": seg["start_time"],
                "end": seg["end_time"],
                "texts": [seg["text"]],
            }
    if current is not None:
        groups.append(current)

    seg_lines: list[tuple[float, str]] = []
    for g in groups:
        parts = []
        if include_timestamps:
            parts.append(f"[{_format_duration(g['start'])} --> {_format_duration(g['end'])}]")
        if include_speakers:
            parts.append(f"{g['speaker']}:")
        header = " ".join(parts)
        text = " ".join(g["texts"])
        seg_lines.append((g["start"], f"{header}\n{text}" if header else text))

    if comment_rows:
        comment_lines = [
            (
                c["timestamp"],
                f"[{_format_duration(c['timestamp'])}] {user_comment_label}: {c['user_name']}: {c['text']}",
            )
            for c in comment_rows
        ]
        lines = _merge_by_timestamp(seg_lines, comment_lines)
    else:
        lines = [line for _, line in seg_lines]

    return "\n\n".join(lines)


def _build_json(
    masked: list[dict[str, Any]],
    speaker_name,
    comment_rows: list[dict[str, Any]],
    filename: str | None,
    duration: float | None,
) -> str:
    data: dict[str, Any] = {
        "filename": filename,
        "duration": duration,
        "segments": [
            {
                "start_time": seg["start_time"],
                "end_time": seg["end_time"],
                "speaker": speaker_name(seg),
                "text": seg["text"],
            }
            for seg in masked
        ],
    }
    if comment_rows:
        data["comments"] = [
            {"timestamp": c["timestamp"], "user": c["user_name"], "text": c["text"]}
            for c in comment_rows
        ]
    return json.dumps(data, indent=2, ensure_ascii=False)


def _csv_escape(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _build_csv(
    masked: list[dict[str, Any]],
    speaker_name,
    comment_rows: list[dict[str, Any]],
    user_comment_label: str,
    comment_type_label: str,
    csv_header_default: str,
    csv_header_with_comments: str,
) -> str:
    seg_rows: list[tuple[float, str]] = []
    for seg in masked:
        row = f'{seg["start_time"]},{seg["end_time"]},"{speaker_name(seg)}",{_csv_escape(seg["text"])}'
        seg_rows.append((seg["start_time"], row))

    if comment_rows:
        header = csv_header_with_comments
        comment_csv_rows = [
            (
                c["timestamp"],
                f'{c["timestamp"]},{c["timestamp"]},"{user_comment_label}: {c["user_name"]}",'
                f'{_csv_escape(c["text"])},"{comment_type_label}"',
            )
            for c in comment_rows
        ]
        seg_rows = [(t, row + ',""') for t, row in seg_rows]
        rows = _merge_by_timestamp(seg_rows, comment_csv_rows)
    else:
        header = csv_header_default
        rows = [row for _, row in seg_rows]

    return header + "\n" + "\n".join(rows)


def _build_srt(
    masked: list[dict[str, Any]],
    speaker_name,
    comment_rows: list[dict[str, Any]],
    user_comment_label: str,
) -> str:
    items: list[dict[str, Any]] = []
    for seg in masked:
        items.append(
            {
                "start": seg["start_time"],
                "end": seg["end_time"],
                "text": f"{speaker_name(seg)}: {seg['text']}",
            }
        )
    for c in comment_rows:
        items.append(
            {
                "start": c["timestamp"],
                "end": c["timestamp"] + 2,
                "text": f"{user_comment_label}: {c['user_name']}: {c['text']}",
            }
        )
    if comment_rows:
        items.sort(key=lambda it: it["start"])

    blocks = []
    for idx, item in enumerate(items, start=1):
        start_ts = _format_srt_timestamp(item["start"])
        end_ts = _format_srt_timestamp(item["end"])
        blocks.append(f"{idx}\n{start_ts} --> {end_ts}\n{item['text']}\n")
    return "\n".join(blocks)


def _build_vtt(
    masked: list[dict[str, Any]],
    speaker_name,
    comment_rows: list[dict[str, Any]],
    user_comment_label: str,
) -> str:
    items: list[dict[str, Any]] = []
    for seg in masked:
        items.append(
            {
                "start": seg["start_time"],
                "end": seg["end_time"],
                "text": f"{speaker_name(seg)}: {seg['text']}",
            }
        )
    for c in comment_rows:
        items.append(
            {
                "start": c["timestamp"],
                "end": c["timestamp"] + 2,
                "text": f"{user_comment_label}: {c['user_name']}: {c['text']}",
            }
        )
    if comment_rows:
        items.sort(key=lambda it: it["start"])

    body = "\n".join(
        f"{_format_vtt_timestamp(it['start'])} --> {_format_vtt_timestamp(it['end'])}\n{it['text']}\n"
        for it in items
    )
    return "WEBVTT\n\n" + body
