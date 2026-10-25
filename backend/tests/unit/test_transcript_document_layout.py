"""Pagination of the DOCX/PDF transcript document (issue #1278): the page:line grid.

The layouts are driven through a monospace measurer (ten characters a line, five lines a page)
so each expected page below is small enough to verify by eye and depends on no font. Every
expectation was written by hand from the rules in ``layout.py``'s docstring rather than copied
from the output.
"""

from __future__ import annotations

import pytest

from app.services.transcript_document.layout import paginate
from app.services.transcript_document.model import Block
from app.services.transcript_document.model import CommentBlock
from app.services.transcript_document.model import DocumentLayout
from app.services.transcript_document.model import DocumentTooLongError
from app.services.transcript_document.model import TurnBlock
from tests.unit._monospace_measurer import MonospaceMeasurer
from tests.unit._monospace_measurer import tiny_spec

pytestmark = pytest.mark.unit

BIG = 10_000


def _show(layout: DocumentLayout) -> list[list[str]]:
    """Each page as ``number:K:text`` strings (``K`` = H header, B body, C comment, - blank)."""
    letter = {"header": "H", "body": "B", "comment": "C", "blank": "-"}
    return [
        [
            f"{ln.number if ln.number is not None else '.'}:{letter[ln.kind]}:{ln.text}"
            for ln in page
        ]
        for page in layout.pages
    ]


def _turn(header: str, text: str) -> TurnBlock:
    return TurnBlock(header=header, text=text)


def test_per_page_numbering_restarts_on_every_page() -> None:
    blocks: list[Block] = [_turn("Ann:", "one two"), _turn("Bob:", "three four five six seven")]
    layout = paginate(blocks, MonospaceMeasurer(), tiny_spec(), "per_page", BIG)
    assert _show(layout) == [
        ["1:H:Ann:", "2:B:one two", "3:H:Bob:", "4:B:three four", "5:B:five six"],
        ["1:B:seven"],
    ]


def test_continuous_numbering_never_restarts() -> None:
    blocks: list[Block] = [_turn("Ann:", "one two"), _turn("Bob:", "three four five six seven")]
    layout = paginate(blocks, MonospaceMeasurer(), tiny_spec(), "continuous", BIG)
    assert _show(layout) == [
        ["1:H:Ann:", "2:B:one two", "3:H:Bob:", "4:B:three four", "5:B:five six"],
        ["6:B:seven"],
    ]


def test_no_numbering_leaves_every_number_empty() -> None:
    blocks: list[Block] = [_turn("Ann:", "one two"), _turn("Bob:", "three four five six seven")]
    layout = paginate(blocks, MonospaceMeasurer(), tiny_spec(), "none", BIG)
    assert [ln.number for page in layout.pages for ln in page] == [None] * 6
    assert len(layout.pages) == 2


def test_a_header_is_never_the_last_slot_of_a_page() -> None:
    # Ann takes 4 slots (header + 3 lines), so Bob's header would sit alone on slot 5.
    blocks: list[Block] = [_turn("Ann:", "aaa bbb ccc ddd eee fff"), _turn("Bob:", "hello")]
    layout = paginate(blocks, MonospaceMeasurer(), tiny_spec(), "per_page", BIG)
    assert _show(layout) == [
        ["1:H:Ann:", "2:B:aaa bbb", "3:B:ccc ddd", "4:B:eee fff", "5:-:"],
        ["1:H:Bob:", "2:B:hello"],
    ]


def test_a_blank_slot_still_consumes_a_continuous_number() -> None:
    blocks: list[Block] = [_turn("Ann:", "aaa bbb ccc ddd eee fff"), _turn("Bob:", "hello")]
    layout = paginate(blocks, MonospaceMeasurer(), tiny_spec(), "continuous", BIG)
    assert [ln.number for ln in layout.pages[1]] == [6, 7]


def test_a_header_with_exactly_one_body_line_of_room_is_not_moved() -> None:
    # Ann takes 3 slots, so Bob's header (slot 4) is followed by its first line (slot 5).
    blocks: list[Block] = [_turn("Ann:", "aaa bbb ccc"), _turn("Bob:", "hi"), _turn("Cy:", "yo")]
    layout = paginate(blocks, MonospaceMeasurer(), tiny_spec(), "per_page", BIG)
    assert _show(layout) == [
        ["1:H:Ann:", "2:B:aaa bbb", "3:B:ccc", "4:H:Bob:", "5:B:hi"],
        ["1:H:Cy:", "2:B:yo"],
    ]


def test_comments_are_their_own_lines_between_turns() -> None:
    blocks: list[Block] = [_turn("Ann:", "hi"), CommentBlock("note"), _turn("Bob:", "yo")]
    layout = paginate(blocks, MonospaceMeasurer(), tiny_spec(), "per_page", BIG)
    assert _show(layout) == [["1:H:Ann:", "2:B:hi", "3:C:note", "4:H:Bob:", "5:B:yo"]]


def test_an_overlong_unspaced_word_is_cut_to_the_line_width() -> None:
    blocks: list[Block] = [_turn("", "x" * 25)]
    layout = paginate(blocks, MonospaceMeasurer(), tiny_spec(), "per_page", BIG)
    assert _show(layout) == [["1:B:xxxxxxxxxx", "2:B:xxxxxxxxxx", "3:B:xxxxx"]]


def test_a_newline_in_the_text_starts_a_new_line_and_a_blank_one_is_kept() -> None:
    layout = paginate([_turn("", "ab\n\ncd")], MonospaceMeasurer(), tiny_spec(), "per_page", BIG)
    assert _show(layout) == [["1:B:ab", "2:B:", "3:B:cd"]]


def test_the_line_cap_refuses_a_document_that_is_too_long() -> None:
    blocks: list[Block] = [_turn("Ann:", "a")] * 3  # 6 slots on a ten-line page
    with pytest.raises(DocumentTooLongError):
        paginate(blocks, MonospaceMeasurer(), tiny_spec(10), "per_page", 5)
    assert paginate(blocks, MonospaceMeasurer(), tiny_spec(10), "per_page", 6).total_lines == 6


def test_an_empty_document_has_no_pages() -> None:
    assert paginate([], MonospaceMeasurer(), tiny_spec(), "per_page", BIG).pages == []
