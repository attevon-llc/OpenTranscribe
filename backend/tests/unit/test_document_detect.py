"""Format detection: magic bytes beat extensions, and ZIP means five different things.

The security-relevant assertion here is the first one: an executable renamed ``.pdf`` must
not reach a parser. The correctness-relevant one is ZIP disambiguation — DOCX, XLSX, PPTX,
ODT and EPUB share a signature, and routing on the extension means a mislabelled file is
handed to the wrong reader, which fails in whatever way that reader fails.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from app.services.documents.detect import DOCUMENT_MIME_TYPES
from app.services.documents.detect import LEGACY_MIME_TYPES
from app.services.documents.detect import detect_document_mime
from app.services.documents.detect import guess_document_mime
from app.services.documents.detect import looks_like_text
from app.services.documents.detect import sniff_zip_format

NAS_ROOT = Path(os.environ.get("RAG_EVAL_DATA_DIR", "/mnt/nas/opentranscribe-benchmarks"))
FIXTURES = NAS_ROOT / "documents" / "docling-fixtures" / "tests" / "data"


def _ooxml(marker: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(marker, "<xml/>")
    return buffer.getvalue()


class TestMagicBytesWinOverTheExtension:
    def test_an_elf_renamed_to_pdf_is_not_a_document(self):
        """The whole reason detection reads content: an extension is a user-supplied claim."""
        assert detect_document_mime("report.pdf", b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64) is None

    def test_a_pdf_named_txt_is_detected_as_a_pdf(self):
        assert (
            detect_document_mime("notes.txt", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3") == "application/pdf"
        )

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (b"%PDF-1.4", "application/pdf"),
            (b"{\\rtf1\\ansi", "application/rtf"),
            (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"\xff\xd8\xff\xe0", "image/jpeg"),
            (b"GIF89a", "image/gif"),
            (b"II*\x00", "image/tiff"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        ],
    )
    def test_each_signature_maps_to_an_accepted_mime(self, header: bytes, expected: str):
        assert detect_document_mime("x.bin", header) == expected
        assert expected in DOCUMENT_MIME_TYPES

    def test_riff_that_is_not_webp_is_not_a_document(self):
        """A WAV is also ``RIFF``. Matching the prefix alone would make audio a document."""
        assert detect_document_mime("clip.wav", b"RIFF\x24\x08\x00\x00WAVEfmt ") is None


class TestZipDisambiguation:
    @pytest.mark.parametrize(
        ("marker", "expected"),
        [
            ("word/document.xml", "docx"),
            ("xl/workbook.xml", "xlsx"),
            ("ppt/presentation.xml", "pptx"),
        ],
    )
    def test_the_central_directory_identifies_the_ooxml_flavour(self, marker: str, expected: str):
        data = _ooxml(marker)
        mime = detect_document_mime("anything.zip", data[:512], data)
        assert mime is not None
        assert DOCUMENT_MIME_TYPES[mime] == expected

    def test_a_docx_named_xlsx_is_routed_by_content(self):
        data = _ooxml("word/document.xml")
        mime = detect_document_mime("mislabelled.xlsx", data[:512], data)
        assert mime is not None
        assert DOCUMENT_MIME_TYPES[mime] == "docx"

    def test_a_plain_zip_is_not_a_document(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("holiday-photos/1.jpg", "not really")
        data = buffer.getvalue()
        assert detect_document_mime("archive.zip", data[:512], data) is None

    def test_an_unreadable_archive_degrades_to_generic_zip_rather_than_raising(self):
        assert sniff_zip_format(b"PK\x03\x04garbage", b"PK\x03\x04garbage") == "application/zip"


class TestTheTextHeuristic:
    def test_utf16_with_a_bom_is_text_despite_being_half_nul_bytes(self):
        """The case a NUL-based heuristic gets wrong; the corpora contain UTF-16 fixtures."""
        data = "hello world".encode("utf-16")
        assert looks_like_text(data)
        assert detect_document_mime("notes.txt", data) == "text/plain"

    def test_binary_without_a_bom_is_not_text(self):
        assert not looks_like_text(b"\x00\x01\x02\x03" * 100)
        assert detect_document_mime("notes.txt", b"\x00\x01\x02\x03" * 100) is None

    def test_an_empty_file_is_treated_as_text(self):
        assert looks_like_text(b"")

    def test_a_markdown_extension_is_believed_only_when_the_bytes_agree(self):
        assert detect_document_mime("readme.md", b"# Title\n\nBody") == "text/markdown"
        assert detect_document_mime("readme.md", b"\x00\xff\x00\xff" * 100) is None


class TestTheLegacyTier:
    def test_ole2_and_rtf_are_flagged_as_the_legacy_set(self):
        """The upload path uses this to say 'convert to .docx or .pdf first'."""
        assert detect_document_mime("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") in (
            LEGACY_MIME_TYPES
        )
        assert detect_document_mime("old.rtf", b"{\\rtf1") in LEGACY_MIME_TYPES

    def test_every_legacy_mime_is_also_an_accepted_document_mime(self):
        assert set(DOCUMENT_MIME_TYPES) >= LEGACY_MIME_TYPES


class TestTheExtensionOnlyGuess:
    """``guess_document_mime`` is the watch scanner's filter — names, no bytes yet."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("q3.pdf", "pdf"),
            ("notes.DOCX", "docx"),
            ("costs.xlsx", "xlsx"),
            ("deck.pptx", "pptx"),
            ("readme.md", "md"),
            ("page.htm", "html"),
            ("scan.TIFF", "image"),
        ],
    )
    def test_it_recognises_the_v1_formats_case_insensitively(self, name: str, expected: str):
        mime = guess_document_mime(name)
        assert mime is not None
        assert DOCUMENT_MIME_TYPES[mime] == expected

    def test_it_returns_none_for_media_so_the_scanner_keeps_treating_them_as_media(self):
        for name in ("meeting.mp4", "call.wav", "clip.mkv"):
            assert guess_document_mime(name) is None

    def test_xml_is_deliberately_not_a_document_format(self):
        """Routing every .xml to the JATS backend produced 11 bare AttributeErrors over
        the 21 XML files in docling-fixtures. Re-adding XML needs a root-element sniff."""
        assert guess_document_mime("patent.xml") is None
        assert detect_document_mime("patent.xml", b"<?xml version='1.0'?><us-patent/>") is None


@pytest.mark.skipif(
    not FIXTURES.is_dir(),
    reason=(
        "docling-fixtures not present under $RAG_EVAL_DATA_DIR/documents. The synthetic "
        "cases above prove the algorithm; this one proves it on 330 real files of 30 "
        "formats. Fetch with scripts/fetch-rag-eval-data.sh --only docling-fixtures."
    ),
)
class TestAgainstTheRealFixtureCorpus:
    """Synthetic OOXML is three files in a ZIP; real ones have hundreds of members."""

    @pytest.mark.parametrize(
        ("subdir", "suffix", "expected"),
        [
            ("docx", ".docx", "docx"),
            ("xlsx", ".xlsx", "xlsx"),
            ("pptx", ".pptx", "pptx"),
            ("md", ".md", "md"),
            ("csv", ".csv", "csv"),
            ("html", ".html", "html"),
            ("pdf", ".pdf", "pdf"),
        ],
    )
    def test_every_real_file_of_a_format_detects_as_that_format(
        self, subdir: str, suffix: str, expected: str
    ):
        sources = FIXTURES / subdir / "sources"
        files = sorted(p for p in sources.glob(f"*{suffix}") if p.is_file())
        assert files, f"no {suffix} fixtures under {sources}"

        wrong = []
        for path in files:
            data = path.read_bytes()
            mime = detect_document_mime(path.name, data[:512], data)
            if mime is None or DOCUMENT_MIME_TYPES[mime] != expected:
                wrong.append((path.name, mime))
        assert not wrong, f"{len(wrong)}/{len(files)} misdetected: {wrong[:5]}"
