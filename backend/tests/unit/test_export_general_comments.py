"""A general comment (``timestamp`` null, about the whole file) in every transcript export.

The comment composer makes general comments a first-class choice, so an export must not print
one as if it were made at ``00:00:00``: that is a real time, and a reader cannot tell the two
apart. A general comment keeps a null time where the format has a field for it (JSON, CSV),
prints no time where the format writes one inline (TXT, Markdown, DOCX/PDF), and is placed
before the transcript, the way the comment list shows it first. Caption files cannot hold an
untimed cue, so there it stays a cue at the very start.
"""

from __future__ import annotations

import json

import pytest

from app.models.media import Comment
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.user import User
from app.services.transcript_document.blocks import build_blocks
from app.services.transcript_document.model import CommentBlock
from app.services.transcript_document.model import DocumentLabels
from app.services.transcript_document.model import DocumentOptions
from app.services.transcript_document.model import TurnBlock
from app.services.transcript_export_service import build_export_content
from app.services.transcript_export_turns import Turn
from app.services.transcript_export_turns import comment_rows_from

pytestmark = pytest.mark.unit


def _comment(timestamp: float | None, text: str) -> Comment:
    comment = Comment(timestamp=timestamp, text=text)
    comment.user = User(full_name="Ana")
    return comment


def _export(fmt: str, comments: list[Comment]) -> str:
    speaker = Speaker(id=1, name="SPEAKER_00", display_name="Sarah")
    segments = [
        TranscriptSegment(start_time=0.0, end_time=4.0, text="Opening words.", speaker_id=1),
        TranscriptSegment(start_time=30.0, end_time=34.0, text="Later words.", speaker_id=1),
    ]
    return build_export_content(
        export_format=fmt,
        segments=segments,
        speakers=[speaker],
        comments=comments,
        include_comments=True,
    )


GENERAL = "about the whole file"
TIMED = "at the start"


@pytest.fixture
def both() -> list[Comment]:
    # The timed one is listed first, as the database's ORDER BY timestamp (NULLS LAST) returns.
    return [_comment(0.0, TIMED), _comment(None, GENERAL)]


def test_rows_keep_the_null_and_put_general_comments_first(both) -> None:
    rows = comment_rows_from(both, True)
    assert [r["text"] for r in rows] == [GENERAL, TIMED]
    assert rows[0]["timestamp"] is None
    assert rows[1]["timestamp"] == 0.0


def test_txt_prints_no_time_for_a_general_comment(both) -> None:
    text = _export("txt", both)
    assert f"USER COMMENT: Ana: {GENERAL}" in text
    assert f"[00:00] USER COMMENT: Ana: {TIMED}" in text
    assert text.index(GENERAL) < text.index("Opening words.")


def test_markdown_prints_no_time_for_a_general_comment(both) -> None:
    document = _export("md", both)
    general_line = next(line for line in document.splitlines() if GENERAL in line)
    assert "[" not in general_line
    timed_line = next(line for line in document.splitlines() if TIMED in line)
    assert "[00:00:00]" in timed_line
    assert document.index(GENERAL) < document.index("Opening words.")


def test_json_keeps_the_null_timestamp(both) -> None:
    data = json.loads(_export("json", both))
    by_text = {c["text"]: c for c in data["comments"]}
    assert by_text[GENERAL]["timestamp"] is None
    assert by_text[TIMED]["timestamp"] == 0.0


def test_csv_leaves_the_time_columns_empty(both) -> None:
    lines = _export("csv", both).splitlines()
    general_row = next(line for line in lines if GENERAL in line)
    timed_row = next(line for line in lines if TIMED in line)
    assert general_row.startswith(",,")
    assert timed_row.startswith("0.0,0.0,")
    assert lines.index(general_row) == 1


@pytest.mark.parametrize("fmt", ["srt", "vtt"])
def test_captions_place_a_general_comment_at_the_start(fmt, both) -> None:
    captions = _export(fmt, both)
    assert GENERAL in captions
    first_cue_time = "00:00:00,000 -->" if fmt == "srt" else "00:00:00.000 -->"
    assert first_cue_time in captions


def test_document_blocks_print_no_time_for_a_general_comment() -> None:
    turns = [Turn("Sarah", 0.0, 5.0, ["first"])]
    rows = comment_rows_from([_comment(None, GENERAL), _comment(10.0, "later")], True)
    labels = DocumentLabels(user_comment_label="NOTE")
    blocks = build_blocks(turns, rows, DocumentOptions(include_comments=True), labels)
    assert [type(b) for b in blocks] == [CommentBlock, TurnBlock, CommentBlock]
    assert isinstance(blocks[0], CommentBlock)
    assert blocks[0].text == f"NOTE: Ana: {GENERAL}"
    assert isinstance(blocks[2], CommentBlock)
    assert blocks[2].text == "NOTE: Ana [00:00:10]: later"
