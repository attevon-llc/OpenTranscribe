"""Masked turns and comments to renderer-neutral blocks.

The header text mirrors the TXT export (timestamp, then speaker) with a fixed-width clock so a
column of headers lines up. Everything is a pure function of already-masked data.
"""

from __future__ import annotations

from typing import Any

from app.services.transcript_document.model import Block
from app.services.transcript_document.model import CommentBlock
from app.services.transcript_document.model import DocumentLabels
from app.services.transcript_document.model import DocumentOptions
from app.services.transcript_document.model import TurnBlock
from app.services.transcript_document.scripts import base_direction
from app.services.transcript_export_turns import Turn
from app.services.transcript_export_turns import format_clock
from app.services.transcript_export_turns import merge_by_timestamp


def turn_block(turn: Turn, options: DocumentOptions) -> TurnBlock:
    """A turn's header (``[hh:mm:ss] Speaker:`` with the parts the options keep) and text."""
    parts = []
    if options.include_timestamps:
        parts.append(f"[{format_clock(turn.start)}]")
    if options.include_speakers:
        parts.append(f"{turn.speaker}:")
    header = " ".join(parts)
    text = turn.text
    # The first strong character of what was SAID decides the direction; a speaker label alone
    # (often Latin) must not turn an Arabic paragraph left-to-right.
    direction = base_direction(text, default=base_direction(header))
    return TurnBlock(header=header, text=text, direction=direction)


def comment_block(row: dict[str, Any], labels: DocumentLabels) -> CommentBlock:
    """``USER COMMENT: Ana [hh:mm:ss]: text``, printed inline."""
    text = (
        f"{labels.user_comment_label}: {row['user_name']} "
        f"[{format_clock(row['timestamp'])}]: {row['text']}"
    )
    return CommentBlock(text=text, direction=base_direction(str(row["text"])))


def build_blocks(
    turns: list[Turn],
    comment_rows: list[dict[str, Any]],
    options: DocumentOptions,
    labels: DocumentLabels,
) -> list[Block]:
    """Turns and (when requested) comments in chronological order; a turn wins a tie."""
    rows = comment_rows if options.include_comments else []
    return merge_by_timestamp(
        [(t.start, turn_block(t, options)) for t in turns],
        [(c["timestamp"], comment_block(c, labels)) for c in rows],
    )
