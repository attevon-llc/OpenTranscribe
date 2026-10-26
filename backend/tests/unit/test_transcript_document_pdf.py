"""The PDF renderer (issue #1278): grid, numbering, direction, determinism, safety.

Every document is rendered with the pinned fixture fonts (``tests/fixtures/fonts``), never
whatever the host has, and read back with pypdf the way a reader's text extraction would. The
numbers asserted (9 pages for 100 turns, a page starting at slot 26) were worked out by hand from
the 25-slot grid and the keep-with-next rule, not copied from the output.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fixtures.document_export import FIXTURE_FONT_DIR
from fixtures.document_export import pdf_text
from fpdf import FPDF
from pypdf import PageObject
from pypdf import PdfReader

from app.services.transcript_document import pdf_renderer
from app.services.transcript_document.fonts import build_font_plan
from app.services.transcript_document.model import Block
from app.services.transcript_document.model import CommentBlock
from app.services.transcript_document.model import DocumentLabels
from app.services.transcript_document.model import DocumentMeta
from app.services.transcript_document.model import DocumentOptions
from app.services.transcript_document.model import DocumentTooLongError
from app.services.transcript_document.model import FontsUnavailableError
from app.services.transcript_document.model import LineNumbering
from app.services.transcript_document.model import PageSizeName
from app.services.transcript_document.model import PageSpec
from app.services.transcript_document.model import TurnBlock
from app.services.transcript_document.pdf_renderer import FpdfMeasurer
from app.services.transcript_document.pdf_renderer import render_pdf

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 25, 14, 2, 11, tzinfo=UTC)
ARABIC = "مرحبا بالعالم هذا اختبار"


def _render(
    blocks: list[Block],
    *,
    numbering: LineNumbering = "per_page",
    page: PageSizeName = "a4",
    redacted: bool = False,
    title: str = "My Title",
    labels: DocumentLabels | None = None,
    language: str | None = None,
    font_dir: Path = FIXTURE_FONT_DIR,
) -> bytes:
    options = DocumentOptions(line_numbering=numbering, page_size=page)
    meta = DocumentMeta(title=title, exported_at=NOW, redacted=redacted, language=language)
    return render_pdf(
        blocks,
        PageSpec.named(page),
        options,
        labels or DocumentLabels(),
        meta,
        font_dir=font_dir,
    )


def _pairs(count: int) -> list[Block]:
    return [TurnBlock(f"[00:00:{i % 60:02d}] Ana:", f"line{i}") for i in range(count)]


def _text_x_positions(page: PageObject, containing: str) -> list[float]:
    """The x origin of every text run on ``page`` whose text contains ``containing``."""
    found: list[float] = []

    def visit(text: str, cm: list[float], tm: list[float], font_dict: object, size: object) -> None:
        if containing in text:
            found.append(float(tm[4]))

    page.extract_text(visitor_text=visit)
    return found


def _page_lines(data: bytes, page: int) -> list[str]:
    return PdfReader(io.BytesIO(data)).pages[page].extract_text().splitlines()


def test_two_renders_of_one_transcript_are_byte_identical_and_carry_the_fixed_clock() -> None:
    first = _render(_pairs(30))
    second = _render(_pairs(30))
    assert first == second
    info = PdfReader(io.BytesIO(first)).metadata
    assert info is not None
    assert str(info["/CreationDate"]) == "D:20261025140211Z"


def test_a_hundred_turns_fill_nine_pages_of_a_25_slot_grid() -> None:
    """Two slots a turn: 12 turns (24 slots) per page, the 25th slot left blank by keep-with-next."""
    reader = PdfReader(io.BytesIO(_render(_pairs(100))))
    assert len(reader.pages) == 9  # 8 pages x 12 turns + 4
    first = reader.pages[0].extract_text().splitlines()
    numbered = [ln for ln in first if re.match(r"^\d+( |$)", ln)]
    assert [ln.split(" ")[0] for ln in numbered] == [str(n) for n in range(1, 26)]


def test_per_page_numbering_restarts_and_continuous_does_not() -> None:
    per_page = _page_lines(_render(_pairs(100)), 1)
    continuous = _page_lines(_render(_pairs(100), numbering="continuous"), 1)
    first_per_page = next(ln for ln in per_page if re.match(r"^\d+ \[", ln))
    first_continuous = next(ln for ln in continuous if re.match(r"^\d+ \[", ln))
    assert first_per_page.startswith("1 [00:00:12] Ana:")
    assert first_continuous.startswith("26 [00:00:12] Ana:")


def test_no_numbering_prints_no_gutter_numbers() -> None:
    lines = _page_lines(_render(_pairs(5), numbering="none"), 0)
    assert any(ln.startswith("[00:00:00] Ana:") for ln in lines)
    assert not any(re.match(r"^\d+ \[", ln) for ln in lines)


def test_a_turn_header_never_ends_a_page() -> None:
    reader = PdfReader(io.BytesIO(_render(_pairs(100))))
    assert len(reader.pages) == 9, "the loop below must have pages to check"
    for page in reader.pages:
        numbered = [ln for ln in page.extract_text().splitlines() if re.match(r"^\d+( |$)", ln)]
        assert not numbered[-1].endswith("Ana:"), numbered[-3:]


@pytest.mark.parametrize(("page", "size"), [("a4", (595, 842)), ("letter", (612, 792))])
def test_page_sizes(page: PageSizeName, size: tuple[int, int]) -> None:
    box = PdfReader(io.BytesIO(_render(_pairs(3), page=page))).pages[0].mediabox
    assert (int(box.width), int(box.height)) == size


def test_both_page_sizes_keep_the_25_line_grid() -> None:
    sizes: tuple[PageSizeName, ...] = ("a4", "letter")
    for page in sizes:
        lines = _page_lines(_render(_pairs(100), page=page), 0)
        numbered = [ln for ln in lines if re.match(r"^\d+( |$)", ln)]
        assert len(numbered) == 25, page


def test_every_page_has_the_title_the_iso_date_and_its_own_page_label() -> None:
    reader = PdfReader(io.BytesIO(_render(_pairs(100), redacted=True)))
    assert len(reader.pages) == 9, "the loop below must have pages to check"
    for number, page in enumerate(reader.pages, start=1):
        lines = page.extract_text().splitlines()
        assert lines[0] == "My Title"
        assert lines[1] == "Exported 2026-10-25 Redacted"
        assert f"Page {number} of 9" in lines


def test_the_redacted_note_appears_only_when_something_was_masked() -> None:
    assert "Redacted" not in pdf_text(_render(_pairs(3), redacted=False))
    assert "Redacted" in pdf_text(_render(_pairs(3), redacted=True))


def test_labels_are_printed_as_typed() -> None:
    labels = DocumentLabels(page_label="{__class__} p{page}/{pages}", exported_label="{0} {date}")
    text = pdf_text(_render(_pairs(3), labels=labels))
    assert "{__class__} p1/1" in text
    assert "{0} 2026-10-25" in text


def test_a_long_title_is_ellipsised_to_the_page_width() -> None:
    text = pdf_text(_render(_pairs(3), title="Quarterly " * 40))
    first = text.splitlines()[0]
    assert first.endswith("…") and len(first) < 200


def test_arabic_is_right_aligned_in_visual_order_and_the_gutter_stays_left() -> None:
    data = _render([TurnBlock("[00:00:05] Bo:", ARABIC, "rtl")])
    line = _page_lines(data, 0)[-1]
    assert line.startswith("2 "), "the number stays in the left gutter"
    # Extraction reads visual order: a right-to-left line comes out with its words reversed.
    assert line[2:].split() == list(reversed(ARABIC.split()))
    page = PdfReader(io.BytesIO(data)).pages[0]
    assert max(_text_x_positions(page, "")) > 300, "the Arabic text sits at the right edge"


def test_a_latin_turn_is_left_aligned() -> None:
    page = PdfReader(io.BytesIO(_render([TurnBlock("Ana:", "hello world")]))).pages[0]
    xs = _text_x_positions(page, "hello")
    assert xs and min(xs) < 80


def test_shaping_runs_only_for_a_line_that_needs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    original = FPDF.set_text_shaping

    def spy(self: FPDF, use_shaping_engine: bool = True, **kwargs: Any) -> None:
        calls.append(use_shaping_engine)
        original(self, use_shaping_engine, **kwargs)

    monkeypatch.setattr(FPDF, "set_text_shaping", spy)
    _render(_pairs(20))
    assert calls and not any(calls), "a Latin transcript must never turn the shaping engine on"
    calls.clear()
    _render([TurnBlock("Bo:", ARABIC, "rtl")])
    assert any(calls), "control: an Arabic line must turn shaping on"


def test_a_document_with_no_transcript_still_has_one_page() -> None:
    assert len(PdfReader(io.BytesIO(_render([]))).pages) == 1


def test_comments_are_numbered_lines_between_turns() -> None:
    blocks: list[Block] = [
        TurnBlock("Ana:", "first"),
        CommentBlock("USER COMMENT: Bo [00:00:07]: check slide 4"),
        TurnBlock("Cy:", "second"),
    ]
    lines = _page_lines(_render(blocks), 0)
    body = [ln for ln in lines if re.match(r"^\d+ ", ln)]
    assert [ln.split(" ", 1)[1] for ln in body] == [
        "Ana:",
        "first",
        "USER COMMENT: Bo [00:00:07]: check slide 4",
        "Cy:",
        "second",
    ]


def test_an_unbroken_word_longer_than_a_line_is_cut_not_lost() -> None:
    word = "x" * 400
    lines = [ln for ln in _page_lines(_render([TurnBlock("", word)]), 0) if re.match(r"^\d+ x", ln)]
    assert len(lines) >= 3, "a 400-character word cannot fit one line"
    assert all(len(ln) < 400 for ln in lines)
    assert "".join(ln.split(" ", 1)[1] for ln in lines) == word


def test_thai_without_spaces_wraps_inside_the_line_width() -> None:
    text = "สวัสดีครับ" * 20
    plan = build_font_plan(text, "th", FIXTURE_FONT_DIR)
    pdf = FPDF(unit="pt", format=(595, 842))
    plan.install(pdf)
    pdf.add_page()
    spec = PageSpec.named("a4")
    lines = FpdfMeasurer(pdf, spec).wrap(text, kind="body", width=200, direction="ltr")
    assert len(lines) > 1
    assert "".join(lines) == text
    pdf.set_font("NotoSans", "", spec.font_size)
    pdf.set_text_shaping(True)
    assert all(pdf.get_string_width(line) <= 200.5 for line in lines)


def test_unspaced_runs_separated_by_spaces_still_pack_each_line_full() -> None:
    """Word wrapping would break at the space and strand a short line; character wrapping packs."""
    text = ("สวัสดีครับ" * 5 + " ") * 6
    plan = build_font_plan(text, "th", FIXTURE_FONT_DIR)
    pdf = FPDF(unit="pt", format=(595, 842))
    plan.install(pdf)
    pdf.add_page()
    spec = PageSpec.named("a4")
    lines = FpdfMeasurer(pdf, spec).wrap(text, kind="body", width=200, direction="ltr")
    pdf.set_font("NotoSans", "", spec.font_size)
    pdf.set_text_shaping(True)
    widths = [pdf.get_string_width(line.rstrip()) for line in lines[:-1]]
    assert len(lines) > 3
    assert all(w >= 180 for w in widths), widths


def test_missing_glyphs_are_counted_and_never_named(caplog: pytest.LogCaptureFixture) -> None:
    """The fixture fonts have no CJK face, so these characters have nowhere to be drawn."""
    secret = "你好世界"
    with caplog.at_level(logging.DEBUG):
        data = _render([TurnBlock("Ana:", f"ok {secret}")])
    assert data.startswith(b"%PDF")
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "4 characters have no glyph" in messages
    assert not any(ch in caplog.text for ch in secret)


def test_a_missing_required_font_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FontsUnavailableError):
        _render(_pairs(3), font_dir=tmp_path)


def test_the_line_cap_refuses_an_overlong_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_renderer, "DOCUMENT_EXPORT_MAX_LINES", 10)
    with pytest.raises(DocumentTooLongError):
        _render(_pairs(20))
    assert _render(_pairs(3)).startswith(b"%PDF")


def test_the_file_names_a_title_and_never_a_person() -> None:
    info = PdfReader(io.BytesIO(_render(_pairs(3)))).metadata
    assert info is not None
    assert info.title == "My Title"
    assert info.author is None
    assert "@" not in str(dict(info))
