"""Markdown transcript builder: YAML front matter plus escaped, speaker-labelled turns (#564).

A pure function of already-masked data and caller labels (no DB, no session, no clock: the
export timestamp arrives in :class:`MarkdownMeta`), so a batch collector can reuse it.

Two safety rules carry the design. Every YAML scalar is written by :func:`yaml_scalar`, so a
title with a colon, quote, ``#``, leading dash or newline cannot break a vault's front matter;
and every piece of transcript text is escaped so a segment saying ``# not a heading`` or
``---`` cannot become Markdown structure.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

from app.services.transcript_export_turns import Turn
from app.services.transcript_export_turns import format_clock
from app.services.transcript_export_turns import merge_by_timestamp

_INLINE_SPECIALS = re.compile(r"[\\*_`\[\]<>]")
_LEADING_MARKER = re.compile(r"^(\s*)([#\-+|=~])")
_LEADING_ORDINAL = re.compile(r"^(\s*\d+)([.)])")
#: Characters a YAML parser rejects or treats as a line break inside a double-quoted scalar.
_YAML_UNSAFE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")


@dataclass(frozen=True)
class MarkdownMeta:
    """Document-level facts for the front matter. ``None`` omits the key."""

    title: str
    recorded_date: str | None = None
    date_source: str | None = None
    duration_seconds: float | None = None
    language: str | None = None
    file_uuid: str | None = None
    exported_at: datetime | None = None
    redacted: bool = False


def yaml_scalar(value: str) -> str:
    """A string as a double-quoted YAML scalar that parses back to exactly ``value``.

    JSON strings are valid YAML, so ``json.dumps`` does the quoting; it leaves a handful of
    characters raw that YAML refuses or folds (DEL, C1 controls, U+2028/2029, BOM), which are
    written as ``\\uXXXX`` escapes instead.
    """
    quoted = json.dumps(value, ensure_ascii=False)
    return _YAML_UNSAFE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", quoted)


def escape_inline(text: str) -> str:
    """Backslash-escape the characters that start inline Markdown constructs."""
    return _INLINE_SPECIALS.sub(lambda m: "\\" + m.group(0), text)


def escape_line(text: str) -> str:
    """:func:`escape_inline` plus the markers that make a LINE a block (heading, list, rule)."""
    text = escape_inline(text)
    text = _LEADING_MARKER.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", text)
    return _LEADING_ORDINAL.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", text)


def _front_matter(meta: MarkdownMeta, participants: list[str]) -> str:
    lines = [f"title: {yaml_scalar(meta.title)}"]
    if meta.recorded_date:
        lines.append(f"date: {yaml_scalar(meta.recorded_date)}")
        if meta.date_source:
            lines.append(f"date_source: {yaml_scalar(meta.date_source)}")
    if meta.duration_seconds is not None:
        lines.append(f"duration: {yaml_scalar(format_clock(meta.duration_seconds))}")
    if participants:
        lines.append("participants: [" + ", ".join(yaml_scalar(p) for p in participants) + "]")
    if meta.language:
        lines.append(f"language: {yaml_scalar(meta.language)}")
    if meta.file_uuid:
        lines.append(f"opentranscribe_uuid: {yaml_scalar(meta.file_uuid)}")
    if meta.exported_at is not None:
        stamp = meta.exported_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"exported_at: {yaml_scalar(stamp)}")
    lines.append(f"redacted: {'true' if meta.redacted else 'false'}")
    return "---\n" + "\n".join(lines) + "\n---"


def _turn_block(turn: Turn, include_timestamps: bool, include_speakers: bool) -> str:
    parts = []
    if include_speakers:
        parts.append(f"**{escape_inline(turn.speaker)}**")
    if include_timestamps:
        parts.append(f"[{format_clock(turn.start)}]")
    body = "\n".join(escape_line(line) for line in turn.text.splitlines() or [""])
    header = " ".join(parts)
    return f"{header}\n{body}" if header else body


def _comment_block(row: dict[str, Any], label: str) -> str:
    lines = str(row["text"]).splitlines() or [""]
    head = (
        f"> **{escape_inline(label)}** — {escape_inline(str(row['user_name']))} "
        f"[{format_clock(row['timestamp'])}]: {escape_inline(lines[0])}"
    )
    return "\n".join([head, *(f"> {escape_line(line)}" for line in lines[1:])])


def build_markdown(
    turns: list[Turn],
    comment_rows: list[dict[str, Any]],
    meta: MarkdownMeta,
    *,
    include_timestamps: bool,
    include_speakers: bool,
    user_comment_label: str,
    default_speaker_label: str,
) -> str:
    """Render ``turns`` (+ comments) as a Markdown document with YAML front matter."""
    participants: list[str] = []
    if include_speakers:
        for turn in turns:
            if turn.speaker != default_speaker_label and turn.speaker not in participants:
                participants.append(turn.speaker)

    blocks = merge_by_timestamp(
        [(t.start, _turn_block(t, include_timestamps, include_speakers)) for t in turns],
        [(c["timestamp"], _comment_block(c, user_comment_label)) for c in comment_rows],
    )
    heading = "# " + escape_inline(" ".join(meta.title.split()))
    document = [_front_matter(meta, participants), heading, *blocks]
    return "\n\n".join(document) + "\n"
