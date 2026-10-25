"""Script detection and block building for the DOCX/PDF transcript document (issue #1278)."""

from __future__ import annotations

import pytest

from app.services.transcript_document.blocks import build_blocks
from app.services.transcript_document.model import CommentBlock
from app.services.transcript_document.model import DocumentLabels
from app.services.transcript_document.model import DocumentOptions
from app.services.transcript_document.model import TurnBlock
from app.services.transcript_document.scripts import base_direction
from app.services.transcript_document.scripts import breaks_between_characters
from app.services.transcript_document.scripts import needs_shaping
from app.services.transcript_document.scripts import sanitize_for_xml
from app.services.transcript_export_turns import Turn

pytestmark = pytest.mark.unit

ARABIC = "مرحبا بالعالم"
HEBREW = "שלום עולם"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello", "ltr"),
        (ARABIC, "rtl"),
        (HEBREW, "rtl"),
        ("123 " + ARABIC, "rtl"),  # leading digits are weak: the first STRONG char decides
        ("(" + ARABIC + ") hello", "rtl"),
        (ARABIC + " hello", "rtl"),
        ("hello " + ARABIC, "ltr"),
        ("12:30", "ltr"),  # nothing strong: the default
        ("", "ltr"),
    ],
)
def test_base_direction_follows_the_first_strong_character(text: str, expected: str) -> None:
    assert base_direction(text) == expected


def test_base_direction_default_applies_only_when_nothing_is_strong() -> None:
    assert base_direction("12:30", default="rtl") == "rtl"
    assert base_direction("hello", default="rtl") == "ltr"


def test_shaping_is_requested_only_for_scripts_that_join_or_reorder() -> None:
    assert needs_shaping(ARABIC)
    assert needs_shaping("नमस्ते")  # Devanagari
    assert needs_shaping("สวัสดี")  # Thai
    assert needs_shaping("hello " + ARABIC)
    assert not needs_shaping("Hello world, ça va? Привет")
    assert not needs_shaping("你好世界")


def test_cjk_and_unspaced_scripts_wrap_between_characters() -> None:
    assert breaks_between_characters("你好世界")
    assert breaks_between_characters("こんにちは")
    assert breaks_between_characters("안녕하세요")
    assert breaks_between_characters("สวัสดีครับ")
    assert not breaks_between_characters("Hello world")
    assert not breaks_between_characters(ARABIC)


def test_sanitize_drops_only_what_xml_forbids() -> None:
    dirty = "a\x00b\x0bc\x1fd￾e￿"
    assert sanitize_for_xml(dirty) == "abcde"
    keep = "tab\there\nline\rend é 你"
    assert sanitize_for_xml(keep) == keep


def _header(turn: Turn, *, timestamps: bool = True, speakers: bool = True) -> str:
    options = DocumentOptions(include_timestamps=timestamps, include_speakers=speakers)
    block = build_blocks([turn], [], options, DocumentLabels())[0]
    assert isinstance(block, TurnBlock)
    return block.header


def test_headers_follow_the_options() -> None:
    turn = Turn("Ana", 75.0, 80.0, ["hi"])
    assert _header(turn) == "[00:01:15] Ana:"
    assert _header(turn, timestamps=False) == "Ana:"
    assert _header(turn, speakers=False) == "[00:01:15]"
    assert _header(turn, speakers=False, timestamps=False) == ""


def test_an_arabic_turn_under_a_latin_speaker_label_is_right_to_left() -> None:
    turn = Turn("Ana", 0.0, 1.0, [ARABIC])
    block = build_blocks([turn], [], DocumentOptions(), DocumentLabels())[0]
    assert isinstance(block, TurnBlock)
    assert block.direction == "rtl"


def test_comments_merge_by_time_and_only_when_requested() -> None:
    turns = [Turn("Ana", 0.0, 5.0, ["first"]), Turn("Bo", 20.0, 25.0, ["second"])]
    comments = [{"timestamp": 10.0, "text": "check slide 4", "user_name": "Ana"}]
    labels = DocumentLabels(user_comment_label="NOTE")
    merged = build_blocks(turns, comments, DocumentOptions(include_comments=True), labels)
    assert [type(b) for b in merged] == [TurnBlock, CommentBlock, TurnBlock]
    assert isinstance(merged[1], CommentBlock)
    assert merged[1].text == "NOTE: Ana [00:00:10]: check slide 4"
    assert len(build_blocks(turns, comments, DocumentOptions(include_comments=False), labels)) == 2
