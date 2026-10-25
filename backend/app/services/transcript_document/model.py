"""Data model shared by the DOCX and PDF renderers (issue #1278).

Everything here is plain frozen data: no database, no session, no clock, no library import. A
renderer receives a list of :class:`Block` (already masked, already grouped into turns) plus
the :class:`DocumentOptions`, :class:`DocumentLabels` and :class:`DocumentMeta` of the request,
and produces bytes. That is what lets the layout be tested against a monospace stand-in and the
renderers be compared against golden output.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from typing import Literal

from app.core.constants import DOCUMENT_LINES_PER_PAGE

Direction = Literal["ltr", "rtl"]
LineKind = Literal["header", "body", "comment", "blank"]
LineNumbering = Literal["per_page", "continuous", "none"]
PageSizeName = Literal["a4", "letter"]


@dataclass(frozen=True)
class PageSpec:
    """Physical page geometry in PDF points (1/72 inch).

    The page is a fixed grid of ``lines_per_page`` numbered slots, so the line height is derived
    from the printable height rather than chosen: A4 and Letter both keep 25 lines.
    """

    width: float
    height: float
    margin_top: float = 72.0
    margin_bottom: float = 72.0
    margin_right: float = 54.0
    #: Left margin; the line-number column sits inside it, so a number never overlaps text.
    gutter_left: float = 54.0
    font_size: float = 11.0
    lines_per_page: int = DOCUMENT_LINES_PER_PAGE

    @property
    def text_width(self) -> float:
        """Width available to a line of text (between the gutter and the right margin)."""
        return self.width - self.gutter_left - self.margin_right

    @property
    def line_height(self) -> float:
        """Distance between two numbered slots."""
        return (self.height - self.margin_top - self.margin_bottom) / self.lines_per_page

    @classmethod
    def named(cls, name: PageSizeName) -> PageSpec:
        """``a4`` (595 x 842 pt) or ``letter`` (612 x 792 pt)."""
        if name == "letter":
            return cls(width=612.0, height=792.0)
        return cls(width=595.0, height=842.0)


@dataclass(frozen=True)
class DocumentOptions:
    """What the caller asked the document to contain and how to number it."""

    include_timestamps: bool = True
    include_speakers: bool = True
    include_comments: bool = False
    line_numbering: LineNumbering = "per_page"
    page_size: PageSizeName = "a4"


@dataclass(frozen=True)
class DocumentLabels:
    """Caller-supplied, already-translated strings (the backend stays translation-free).

    ``page_label`` and ``exported_label`` are templates: ``{page}``/``{pages}`` and ``{date}``
    are substituted with ``str.replace``, never ``str.format``, so a hostile label such as
    ``{__class__}`` is printed as typed.
    """

    speaker_default_label: str = "Speaker"
    user_comment_label: str = "USER COMMENT"
    page_label: str = "Page {page} of {pages}"
    exported_label: str = "Exported {date}"
    redacted_label: str = "Redacted"


@dataclass(frozen=True)
class DocumentMeta:
    """Facts for the unnumbered header and the file's core properties."""

    title: str
    exported_at: datetime
    #: True only when some enabled category is still masked after the reveal.
    redacted: bool = False
    language: str | None = None

    @property
    def exported_on(self) -> date:
        """The UTC calendar date printed in the header (ISO ``YYYY-MM-DD``)."""
        return self.exported_at.date()


@dataclass(frozen=True)
class TurnBlock:
    """One speaker turn: a header line (timestamp, speaker) then the spoken text."""

    header: str
    text: str
    direction: Direction = "ltr"


@dataclass(frozen=True)
class CommentBlock:
    """A user comment, printed inline in the transcript where it was made."""

    text: str
    direction: Direction = "ltr"


Block = TurnBlock | CommentBlock


@dataclass(frozen=True)
class LayoutLine:
    """One numbered slot of a page. ``number`` is ``None`` when numbering is off."""

    number: int | None
    kind: LineKind
    text: str
    direction: Direction = "ltr"


@dataclass
class DocumentLayout:
    """The paginated document: pages of lines, each page at most ``lines_per_page`` long."""

    spec: PageSpec
    line_numbering: LineNumbering
    pages: list[list[LayoutLine]] = field(default_factory=list)

    @property
    def total_lines(self) -> int:
        """Slots used across all pages, blank padding included."""
        return sum(len(page) for page in self.pages)


class DocumentTooLongError(Exception):
    """The transcript lays out to more lines than ``DOCUMENT_EXPORT_MAX_LINES``."""


class FontsUnavailableError(Exception):
    """The fonts a PDF needs are not installed in this image (it never prints without them)."""
