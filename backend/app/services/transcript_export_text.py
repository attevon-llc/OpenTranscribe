"""TXT / JSON / CSV transcript builders (layout unchanged from the retired client exporter).

Pure functions of already-masked data; see ``transcript_export_turns`` for the shared seam.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.transcript_export_turns import SpeakerNamer
from app.services.transcript_export_turns import Turn
from app.services.transcript_export_turns import format_duration
from app.services.transcript_export_turns import merge_by_timestamp


def build_txt(
    turns: list[Turn],
    comment_rows: list[dict[str, Any]],
    include_timestamps: bool,
    include_speakers: bool,
    user_comment_label: str,
) -> str:
    """One block per turn (``[start --> end] Speaker:`` then the text), comments merged in."""
    seg_lines: list[tuple[float, str]] = []
    for turn in turns:
        parts = []
        if include_timestamps:
            parts.append(f"[{format_duration(turn.start)} --> {format_duration(turn.end)}]")
        if include_speakers:
            parts.append(f"{turn.speaker}:")
        header = " ".join(parts)
        seg_lines.append((turn.start, f"{header}\n{turn.text}" if header else turn.text))

    comment_lines = [
        (
            c["timestamp"],
            f"[{format_duration(c['timestamp'])}] {user_comment_label}: {c['user_name']}: {c['text']}",
        )
        for c in comment_rows
    ]
    return "\n\n".join(merge_by_timestamp(seg_lines, comment_lines))


def build_json(
    masked: list[dict[str, Any]],
    speaker_name: SpeakerNamer,
    comment_rows: list[dict[str, Any]],
    filename: str | None,
    duration: float | None,
) -> str:
    """Segments (and comments, when present) as indented UTF-8 JSON."""
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


def build_csv(
    masked: list[dict[str, Any]],
    speaker_name: SpeakerNamer,
    comment_rows: list[dict[str, Any]],
    user_comment_label: str,
    comment_type_label: str,
    csv_header_default: str,
    csv_header_with_comments: str,
) -> str:
    """One row per segment, comments merged in with a trailing type column."""
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
        rows = merge_by_timestamp(seg_rows, comment_csv_rows)
    else:
        header = csv_header_default
        rows = [row for _, row in seg_rows]

    return header + "\n" + "\n".join(rows)
