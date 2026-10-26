"""Pagination of blocks into a fixed grid of numbered lines (the PDF's citable page:line).

A pure two-phase model: :func:`paginate` turns blocks into a :class:`DocumentLayout` (pages of
``(number, kind, text, direction)`` lines) using a :class:`Measurer` to wrap text; the PDF
renderer then only draws what the layout says. Production uses a measurer backed by the same
font and shaping state as the drawing, so measuring and drawing agree; tests use a monospace
stand-in so the golden layouts depend on no font.

The grid is the point. Every page has the same number of slots, so ``page 3, line 14`` names
one place in the document however long the lines above it wrapped. Two rules protect that:

- **Keep-with-next.** A turn header is never the last thing on a page. The slots it would
  have used stay blank (and numbered) and the header starts the next page, so a header is
  always followed by at least the first line of what was said.
- **Slots are counted, not just text.** Blank padding consumes numbers, which is what makes
  ``continuous`` numbering match the page grid.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

from app.services.transcript_document.model import Block
from app.services.transcript_document.model import CommentBlock
from app.services.transcript_document.model import Direction
from app.services.transcript_document.model import DocumentLayout
from app.services.transcript_document.model import DocumentTooLongError
from app.services.transcript_document.model import LayoutLine
from app.services.transcript_document.model import LineKind
from app.services.transcript_document.model import LineNumbering
from app.services.transcript_document.model import PageSpec
from app.services.transcript_document.model import TurnBlock


class Measurer(Protocol):
    """Wraps one paragraph (no newline) to ``width`` points."""

    def wrap(self, text: str, *, kind: LineKind, width: float, direction: Direction) -> list[str]:
        """The paragraph's lines, at least one; a word longer than ``width`` is cut."""
        ...


@dataclasses.dataclass
class _Slot:
    kind: LineKind
    text: str
    direction: Direction


class _Pager:
    """Accumulates slots page by page and enforces the line cap."""

    def __init__(self, spec: PageSpec, max_lines: int) -> None:
        self.spec = spec
        self.max_lines = max_lines
        self.pages: list[list[_Slot]] = [[]]
        self.total = 0

    @property
    def remaining(self) -> int:
        """Free slots on the current page."""
        return self.spec.lines_per_page - len(self.pages[-1])

    def add(self, kind: LineKind, text: str, direction: Direction) -> None:
        if self.remaining == 0:
            self.pages.append([])
        self.total += 1
        if self.total > self.max_lines:
            raise DocumentTooLongError(f"document exceeds {self.max_lines} lines")
        self.pages[-1].append(_Slot(kind, text, direction))

    def pad_page(self) -> None:
        """Fill the rest of the current page with blank numbered slots."""
        while self.remaining > 0 and self.pages[-1]:
            self.add("blank", "", "ltr")


def _wrap_text(
    measurer: Measurer, text: str, kind: LineKind, spec: PageSpec, direction: Direction
) -> list[str]:
    """Every paragraph of ``text`` wrapped; an empty paragraph is an empty line."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        wrapped = measurer.wrap(paragraph, kind=kind, width=spec.text_width, direction=direction)
        lines.extend(wrapped or [""])
    return lines


def _place_turn(pager: _Pager, block: TurnBlock, measurer: Measurer) -> None:
    spec = pager.spec
    header = (
        _wrap_text(measurer, block.header, "header", spec, block.direction) if block.header else []
    )
    body = _wrap_text(measurer, block.text, "body", spec, block.direction)
    if header and pager.remaining < len(header) + 1:
        pager.pad_page()
    for line in header:
        pager.add("header", line, block.direction)
    for line in body:
        pager.add("body", line, block.direction)


def _place_comment(pager: _Pager, block: CommentBlock, measurer: Measurer) -> None:
    for line in _wrap_text(measurer, block.text, "comment", pager.spec, block.direction):
        pager.add("comment", line, block.direction)


def _numbered(slots: list[list[_Slot]], numbering: LineNumbering) -> list[list[LayoutLine]]:
    pages: list[list[LayoutLine]] = []
    running = 0
    for page in slots:
        out = []
        for index, slot in enumerate(page):
            running += 1
            number: int | None
            if numbering == "per_page":
                number = index + 1
            elif numbering == "continuous":
                number = running
            else:
                number = None
            out.append(LayoutLine(number, slot.kind, slot.text, slot.direction))
        pages.append(out)
    return pages


def paginate(
    blocks: list[Block],
    measurer: Measurer,
    spec: PageSpec,
    line_numbering: LineNumbering,
    max_lines: int,
) -> DocumentLayout:
    """Lay ``blocks`` out on ``spec``'s grid.

    Raises:
        DocumentTooLongError: when the document needs more than ``max_lines`` slots.
    """
    pager = _Pager(spec, max_lines)
    for block in blocks:
        if isinstance(block, TurnBlock):
            _place_turn(pager, block, measurer)
        else:
            _place_comment(pager, block, measurer)
    slots = pager.pages if pager.pages[-1] else pager.pages[:-1]
    return DocumentLayout(spec, line_numbering, _numbered(slots, line_numbering))
