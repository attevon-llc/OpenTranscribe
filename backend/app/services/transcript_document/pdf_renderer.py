"""PDF renderer (fpdf2, LGPL-3.0, with HarfBuzz shaping through uharfbuzz, Apache-2.0).

The PDF is the citable artifact: every page is the same grid of numbered lines, so
"page 3, line 14" names one place however the lines above it wrapped. :mod:`layout` decides the
grid with a measurer; this module is that measurer (:class:`FpdfMeasurer`, so measuring and
drawing share one font and shaping state) and the drawing code that follows the layout.

Shaping is switched per LINE and only for a line that contains a script that joins or reorders
(Arabic, Indic, Thai, ...): shaping every line measured 4x slower on a Latin transcript with
no visible difference. Right-to-left lines are right-aligned and run through fpdf2's UAX #9
bidi; the line-number gutter stays on the left in every document, so there is one citation column.

fpdf2 is LGPL-3.0-only. It is a normal, unmodified, separately installed pip package (never
vendored or patched), which keeps it replaceable, and its licence text ships with the image.

fpdf2's logger prints the missing glyphs themselves, which are transcript content (issue #1022),
so it is silenced here and only a COUNT of uncovered characters is logged.
"""

from __future__ import annotations

import logging
from datetime import UTC
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import MethodReturnValue
from fpdf.enums import WrapMode

from app.core.constants import DOCUMENT_EXPORT_MAX_LINES
from app.services.transcript_document.fonts import BASE_FAMILY
from app.services.transcript_document.fonts import build_font_plan
from app.services.transcript_document.fonts import count_missing_glyphs
from app.services.transcript_document.layout import paginate
from app.services.transcript_document.model import Block
from app.services.transcript_document.model import CommentBlock
from app.services.transcript_document.model import Direction
from app.services.transcript_document.model import DocumentLabels
from app.services.transcript_document.model import DocumentLayout
from app.services.transcript_document.model import DocumentMeta
from app.services.transcript_document.model import DocumentOptions
from app.services.transcript_document.model import LayoutLine
from app.services.transcript_document.model import LineKind
from app.services.transcript_document.model import PageSpec
from app.services.transcript_document.model import TurnBlock
from app.services.transcript_document.scripts import base_direction
from app.services.transcript_document.scripts import breaks_between_characters
from app.services.transcript_document.scripts import needs_shaping

logger = logging.getLogger(__name__)

#: fpdf2 style letter per line kind: speaker headers bold, comments italic.
_STYLE: dict[LineKind, str] = {"header": "B", "body": "", "comment": "I", "blank": ""}
_NUMBER_SIZE = 8.0
_PAGE_FURNITURE_SIZE = 9.0
_NUMBER_GAP = 8.0


def _select_font(pdf: FPDF, kind: LineKind, size: float) -> None:
    pdf.set_font(BASE_FAMILY, _STYLE[kind], size)


def _set_shaping(pdf: FPDF, text: str) -> None:
    """Shaping on for a line that needs it, off otherwise (see the module docstring)."""
    pdf.set_text_shaping(needs_shaping(text))


class FpdfMeasurer:
    """Wraps paragraphs with the SAME font and shaping state the drawing uses."""

    def __init__(self, pdf: FPDF, spec: PageSpec) -> None:
        self._pdf = pdf
        self._spec = spec

    def wrap(self, text: str, *, kind: LineKind, width: float, direction: Direction) -> list[str]:
        if not text:
            return [""]
        _select_font(self._pdf, kind, self._spec.font_size)
        _set_shaping(self._pdf, text)
        mode = WrapMode.CHAR if breaks_between_characters(text) else WrapMode.WORD
        lines = self._pdf.multi_cell(
            w=width,
            h=self._spec.line_height,
            text=text,
            dry_run=True,
            output=MethodReturnValue.LINES,
            wrapmode=mode,
        )
        if not isinstance(lines, list):  # only the LINES output was asked for
            return [""]
        return [str(line) for line in lines] or [""]


def _ellipsize(pdf: FPDF, text: str, width: float) -> str:
    """``text`` cut to ``width`` points with an ellipsis, measured in the current font."""
    if pdf.get_string_width(text) <= width:
        return text
    ellipsis = "\u2026"
    while text and pdf.get_string_width(text + ellipsis) > width:
        text = text[:-1]
    return text + ellipsis


def _draw_furniture(
    pdf: FPDF, spec: PageSpec, meta: DocumentMeta, labels: DocumentLabels, page: int, pages: int
) -> None:
    """The unnumbered header and footer of one page."""
    top = spec.margin_top / 2 - _PAGE_FURNITURE_SIZE - 2
    pdf.set_text_color(60)
    _select_font(pdf, "header", _PAGE_FURNITURE_SIZE)
    _set_shaping(pdf, meta.title)
    title = _ellipsize(pdf, meta.title, spec.text_width)
    pdf.set_xy(spec.gutter_left, top)
    align = "R" if base_direction(meta.title) == "rtl" else "L"
    pdf.cell(w=spec.text_width, h=_PAGE_FURNITURE_SIZE + 2, text=title, align=align)

    _select_font(pdf, "body", _PAGE_FURNITURE_SIZE)
    exported = labels.exported_label.replace("{date}", meta.exported_on.isoformat())
    _set_shaping(pdf, exported)
    pdf.set_xy(spec.gutter_left, top + _PAGE_FURNITURE_SIZE + 3)
    pdf.cell(w=spec.text_width, h=_PAGE_FURNITURE_SIZE + 2, text=exported, align="L")
    if meta.redacted:
        _select_font(pdf, "comment", _PAGE_FURNITURE_SIZE)
        _set_shaping(pdf, labels.redacted_label)
        pdf.set_xy(spec.gutter_left, top + _PAGE_FURNITURE_SIZE + 3)
        pdf.cell(
            w=spec.text_width, h=_PAGE_FURNITURE_SIZE + 2, text=labels.redacted_label, align="R"
        )

    _select_font(pdf, "body", _PAGE_FURNITURE_SIZE)
    footer = labels.page_label.replace("{pages}", str(pages)).replace("{page}", str(page))
    _set_shaping(pdf, footer)
    pdf.set_xy(spec.gutter_left, spec.height - spec.margin_bottom / 2)
    pdf.cell(w=spec.text_width, h=_PAGE_FURNITURE_SIZE + 2, text=footer, align="C")
    pdf.set_text_color(0)


def _draw_line(pdf: FPDF, spec: PageSpec, index: int, line: LayoutLine) -> None:
    y = spec.margin_top + index * spec.line_height
    if line.number is not None:
        pdf.set_text_color(120)
        _select_font(pdf, "body", _NUMBER_SIZE)
        pdf.set_text_shaping(False)
        pdf.set_xy(4.0, y)
        pdf.cell(
            w=spec.gutter_left - _NUMBER_GAP - 4.0,
            h=spec.line_height,
            text=str(line.number),
            align="R",
        )
        pdf.set_text_color(0)
    text = line.text.rstrip()
    if not text:
        return
    _select_font(pdf, line.kind, spec.font_size)
    _set_shaping(pdf, text)
    pdf.set_xy(spec.gutter_left, y)
    pdf.cell(
        w=spec.text_width,
        h=spec.line_height,
        text=text,
        align="R" if line.direction == "rtl" else "L",
    )


def _all_text(blocks: list[Block], meta: DocumentMeta, labels: DocumentLabels) -> str:
    parts = [meta.title, labels.exported_label, labels.page_label, labels.redacted_label]
    for block in blocks:
        if isinstance(block, TurnBlock):
            parts.extend((block.header, block.text))
        elif isinstance(block, CommentBlock):
            parts.append(block.text)
    return "\n".join(parts)


def _draw(pdf: FPDF, layout: DocumentLayout, meta: DocumentMeta, labels: DocumentLabels) -> None:
    spec = layout.spec
    pages = max(len(layout.pages), 1)
    for number, page in enumerate(layout.pages or [[]], start=1):
        if number > 1:
            pdf.add_page()
        _draw_furniture(pdf, spec, meta, labels, number, pages)
        for index, line in enumerate(page):
            _draw_line(pdf, spec, index, line)


def render_pdf(
    blocks: list[Block],
    spec: PageSpec,
    options: DocumentOptions,
    labels: DocumentLabels,
    meta: DocumentMeta,
    *,
    font_dir: Path | None = None,
) -> bytes:
    """The transcript as a PDF file's bytes.

    Raises:
        FontsUnavailableError: when ``Noto Sans`` is not installed.
        DocumentTooLongError: when the transcript needs more than ``DOCUMENT_EXPORT_MAX_LINES``.
    """
    text = _all_text(blocks, meta, labels)
    plan = build_font_plan(text, meta.language, font_dir)
    # fpdf2 logs the missing characters themselves (transcript content); only a count leaves.
    logging.getLogger("fpdf").setLevel(logging.ERROR)

    pdf = FPDF(unit="pt", format=(spec.width, spec.height))
    pdf.set_auto_page_break(False)
    pdf.set_margins(spec.gutter_left, spec.margin_top, spec.margin_right)
    pdf.c_margin = 0
    plan.install(pdf)
    # fpdf2 measures against the current page, so one exists before the layout is computed;
    # the first laid-out page is drawn on it.
    pdf.add_page()
    # A fixed (UTC) creation date and no author: two renders of one transcript are byte-identical,
    # and the file names a title, never a person.
    pdf.set_creation_date(meta.exported_at.astimezone(UTC))
    pdf.set_title(meta.title)

    missing = count_missing_glyphs(pdf, text)
    if missing:
        logger.warning("PDF export: %d characters have no glyph in the installed fonts", missing)

    layout = paginate(
        blocks, FpdfMeasurer(pdf, spec), spec, options.line_numbering, DOCUMENT_EXPORT_MAX_LINES
    )
    _draw(pdf, layout, meta, labels)
    return bytes(pdf.output())
