"""The DOCX renderer (issue #1278): structure, line numbering, direction, fonts, safety.

A DOCX has no fixed pagination, so the golden is its XML structure, not rendered pages. The
expected outline below was written by hand from the rules in ``docx_renderer.py``.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC
from datetime import datetime

import pytest
from lxml import etree

from app.services.transcript_document.docx_renderer import render_docx
from app.services.transcript_document.model import Block
from app.services.transcript_document.model import CommentBlock
from app.services.transcript_document.model import DocumentLabels
from app.services.transcript_document.model import DocumentMeta
from app.services.transcript_document.model import DocumentOptions
from app.services.transcript_document.model import LineNumbering
from app.services.transcript_document.model import PageSizeName
from app.services.transcript_document.model import PageSpec
from app.services.transcript_document.model import TurnBlock

pytestmark = pytest.mark.unit

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NOW = datetime(2026, 10, 25, 14, 2, 11, tzinfo=UTC)
ARABIC = "مرحبا بالعالم"

BLOCKS: list[Block] = [
    TurnBlock("[00:00:00] Ana:", "Hello world"),
    TurnBlock("[00:00:05] Bo:", ARABIC, "rtl"),
    CommentBlock("USER COMMENT: Ana [00:00:07]: check slide 4"),
]


def _render(
    blocks: list[Block] = BLOCKS,
    *,
    numbering: LineNumbering = "per_page",
    page: PageSizeName = "a4",
    redacted: bool = False,
    language: str | None = None,
    labels: DocumentLabels | None = None,
    title: str = "Q4 Review",
) -> bytes:
    options = DocumentOptions(line_numbering=numbering, page_size=page)
    meta = DocumentMeta(title=title, exported_at=NOW, redacted=redacted, language=language)
    return render_docx(
        blocks, PageSpec.named(options.page_size), options, labels or DocumentLabels(), meta
    )


def _part(data: bytes, name: str) -> etree._Element:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return etree.fromstring(archive.read(name))


def _outline(data: bytes) -> list[str]:
    """Each body paragraph as ``P <flags> | <run flags>: <text>``."""
    body = _part(data, "word/document.xml").find(f"{W}body")
    lines = []
    for p in body.findall(f"{W}p"):
        ppr = p.find(f"{W}pPr")
        flags = []
        if ppr.find(f"{W}keepNext") is not None:
            flags.append("keepNext")
        if ppr.find(f"{W}bidi") is not None:
            flags.append("bidi")
        flags.append("after=" + ppr.find(f"{W}spacing").get(f"{W}after"))
        run = p.find(f"{W}r")
        rpr = run.find(f"{W}rPr")
        run_flags = [tag for tag in ("b", "i", "rtl") if rpr.find(f"{W}{tag}") is not None]
        text = "".join(t.text or "" for t in p.iter(f"{W}t"))
        lines.append(f"P {' '.join(flags)} | {' '.join(run_flags)}: {text}")
    return lines


def test_the_body_outline_matches_the_hand_written_golden() -> None:
    assert _outline(_render()) == [
        "P keepNext after=0 | b: [00:00:00] Ana:",
        "P after=120 | : Hello world",
        "P keepNext bidi after=0 | b rtl: [00:00:05] Bo:",
        f"P bidi after=120 | rtl: {ARABIC}",
        "P after=80 | i: USER COMMENT: Ana [00:00:07]: check slide 4",
    ]


def test_bidi_precedes_spacing_in_the_paragraph_properties() -> None:
    """Word rejects a ``w:pPr`` whose children are out of schema order."""
    body = _part(_render(), "word/document.xml").find(f"{W}body")
    rtl_paragraph = body.findall(f"{W}p")[3]
    order = [child.tag.removeprefix(W) for child in rtl_paragraph.find(f"{W}pPr")]
    assert order.index("bidi") < order.index("spacing")


def _numbering(data: bytes) -> dict[str, str] | None:
    sect = _part(data, "word/document.xml").find(f"{W}body").find(f"{W}sectPr")
    element = sect.find(f"{W}lnNumType")
    if element is None:
        return None
    return {k.removeprefix(W): v for k, v in element.attrib.items()}


def test_per_page_numbering_restarts_every_page() -> None:
    assert _numbering(_render(numbering="per_page")) == {
        "countBy": "1",
        "restart": "newPage",
        "distance": "360",
    }


def test_continuous_numbering_never_restarts() -> None:
    numbering = _numbering(_render(numbering="continuous"))
    assert numbering is not None and numbering["restart"] == "continuous"


def test_no_numbering_omits_the_element() -> None:
    assert _numbering(_render(numbering="none")) is None


def test_line_numbering_sits_between_page_margins_and_columns_in_the_section() -> None:
    sect = _part(_render(), "word/document.xml").find(f"{W}body").find(f"{W}sectPr")
    order = [child.tag.removeprefix(W) for child in sect]
    assert order.index("pgMar") < order.index("lnNumType") < order.index("cols")


@pytest.mark.parametrize(
    ("page", "width", "height"), [("a4", "11900", "16840"), ("letter", "12240", "15840")]
)
def test_page_size_in_twips(page: PageSizeName, width: str, height: str) -> None:
    sect = _part(_render(page=page), "word/document.xml").find(f"{W}body").find(f"{W}sectPr")
    size = sect.find(f"{W}pgSz")
    assert (size.get(f"{W}w"), size.get(f"{W}h")) == (width, height)


def test_rtl_marks_appear_on_the_arabic_turn_only() -> None:
    root = _part(_render(), "word/document.xml")
    assert len(root.findall(f".//{W}bidi")) == 2  # header + body of the Arabic turn
    assert len(root.findall(f".//{W}rtl")) == 2
    latin = _outline(_render([TurnBlock("Ana:", "Hello")]))
    assert all("bidi" not in line and "rtl" not in line for line in latin)


def test_header_has_the_title_the_iso_date_and_redaction_only_when_masked() -> None:
    plain = " ".join(t.text or "" for t in _part(_render(), "word/header1.xml").iter(f"{W}t"))
    assert plain == "Q4 Review Exported 2026-10-25"
    masked = _render(redacted=True)
    text = " ".join(t.text or "" for t in _part(masked, "word/header1.xml").iter(f"{W}t"))
    assert text == "Q4 Review Exported 2026-10-25 Redacted"


def test_the_footer_carries_live_page_fields_and_literal_braces() -> None:
    labels = DocumentLabels(page_label="{__class__} Page {page} of {pages}")
    footer = _part(_render(labels=labels), "word/footer1.xml")
    fields = [f.get(f"{W}instr").strip() for f in footer.iter(f"{W}fldSimple")]
    assert fields == ["PAGE", "NUMPAGES"]
    runs = "".join(t.text or "" for t in footer.iter(f"{W}t"))
    assert runs.startswith("{__class__} Page ")


@pytest.mark.parametrize(
    ("language", "family"),
    [
        ("ja", "Noto Sans CJK JP"),
        ("ko", "Noto Sans CJK KR"),
        ("zh", "Noto Sans CJK SC"),
        (None, "Noto Sans CJK SC"),
    ],
)
def test_east_asian_font_follows_the_file_language(language: str | None, family: str) -> None:
    styles = _part(_render(language=language), "word/styles.xml")
    normal = next(s for s in styles.iter(f"{W}style") if s.get(f"{W}styleId") == "Normal")
    fonts = normal.find(f"{W}rPr").find(f"{W}rFonts")
    assert fonts.get(f"{W}eastAsia") == family
    assert fonts.get(f"{W}ascii") == "Noto Sans"
    assert fonts.get(f"{W}cs") == "Noto Naskh Arabic"
    size = normal.find(f"{W}rPr")
    assert size.find(f"{W}szCs").get(f"{W}val") == "22"


def test_characters_xml_forbids_are_dropped_rather_than_raising() -> None:
    dirty: list[Block] = [TurnBlock("Ana:\x0b", "he\x00llo\x1f wor￾ld"), CommentBlock("a\x08b")]
    outline = _outline(_render(dirty, title="T\x00itle"))
    assert outline[1].endswith(": hello world")
    assert outline[2].endswith(": ab")


def test_core_properties_are_the_documents_not_the_librarys() -> None:
    core = _part(_render(), "docProps/core.xml")
    ns = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
        "dcterms": "http://purl.org/dc/terms/",
    }
    assert core.find("dc:title", ns).text == "Q4 Review"
    assert not core.find("dc:creator", ns).text
    assert not core.find("cp:lastModifiedBy", ns).text
    assert not core.find("dc:description", ns).text
    assert core.find("dcterms:created", ns).text == "2026-10-25T14:02:11Z"
    assert core.find("dcterms:modified", ns).text == "2026-10-25T14:02:11Z"


def test_a_long_document_renders() -> None:
    many: list[Block] = [TurnBlock(f"[00:00:{i % 60:02d}] S:", "word " * 40) for i in range(500)]
    assert len(_outline(_render(many))) == 1000
