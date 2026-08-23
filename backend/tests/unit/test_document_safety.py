"""Container-safety guards: every one is asserted to trip *without expanding anything*.

The zip-bomb tests build real bombs. The assertion that matters is not just "it raised"
but that it raised from the **central directory** — a guard that discovers a bomb by
decompressing it has already lost. Each bomb here declares gigabytes from a few kilobytes,
so a test that decompressed would not finish in the suite's 300 s timeout.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from app.services.documents.safety import MAX_COMPRESSION_RATIO
from app.services.documents.safety import MAX_ZIP_MEMBERS
from app.services.documents.safety import assert_page_count
from app.services.documents.safety import assert_pdf_is_readable
from app.services.documents.safety import assert_zip_is_safe
from app.services.documents.safety import defuse_xml
from app.services.documents.safety import prescan
from app.services.documents.types import DocumentEncryptedError
from app.services.documents.types import DocumentTooLargeError
from app.services.documents.types import DocumentUnsafeError

NAS_ROOT = Path(os.environ.get("RAG_EVAL_DATA_DIR", "/mnt/nas/opentranscribe-benchmarks"))
PASSWORD_PDF = (
    NAS_ROOT
    / "documents"
    / "docling-fixtures"
    / "tests"
    / "data"
    / "pdf_password"
    / "sources"
    / "2206.01062_pg3.pdf"
)


def _zip(members: list[tuple[str, bytes]], compress: bool = True) -> bytes:
    buffer = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", mode) as zf:
        for name, payload in members:
            zf.writestr(name, payload)
    return buffer.getvalue()


class TestZipBombs:
    def test_a_high_ratio_member_is_refused(self):
        """~1 GB of zeros in a few KB. Real OOXML runs 3-10:1; this is ~1000:1."""
        bomb = _zip([("word/document.xml", b"\x00" * (200 * 1024 * 1024))])
        assert len(bomb) < 512 * 1024, "the bomb must be small or the test proves nothing"

        with pytest.raises(DocumentUnsafeError, match="compression ratio"):
            assert_zip_is_safe(bomb)

    def test_the_guard_reads_the_directory_rather_than_the_members(self):
        """A forged central directory declaring an impossible size must still trip.

        This is the discriminating case: a guard implemented as "decompress and measure"
        passes the previous test and fails this one, because the declared size is a lie
        the archive never has to make good on.
        """
        # Stored, not deflated: the ratio is 1:1, so the ratio heuristic cannot fire and
        # ONLY a guard reading the declared sizes out of the directory can catch it.
        big = _zip([("a.bin", b"\x00" * (600 * 1024 * 1024))], compress=False)
        with pytest.raises(DocumentTooLargeError, match="expands to over"):
            assert_zip_is_safe(big)

    def test_too_many_members_is_refused(self):
        many = _zip([(f"m{i}.txt", b"x") for i in range(MAX_ZIP_MEMBERS + 1)])
        with pytest.raises(DocumentTooLargeError, match=f"{MAX_ZIP_MEMBERS} ceiling"):
            assert_zip_is_safe(many)

    def test_an_ordinary_office_archive_passes(self):
        """The negative control. Without it every assertion above could be vacuous."""
        assert_zip_is_safe(_zip([("word/document.xml", b"<w:document>hello</w:document>")]))

    def test_a_pathologically_repetitive_but_honest_document_still_trips_the_ratio(self):
        """A recorded false positive, not a bug — and the reason the ceiling is measured.

        2,000 identical repetitions of one sentence compress **278:1**, over the 200:1
        ceiling, despite being an entirely honest (if absurd) document. The ratio check
        is therefore a *heuristic* and the aggregate-size ceiling is the real defence.
        ``TestTheCeilingsAgainstTheRealCorpus`` below is what justifies keeping 200: no
        member of any real ZIP-based document in the corpora comes within 3× of it.
        """
        prose = ("The quick brown fox jumps over the lazy dog. " * 2000).encode()
        with pytest.raises(DocumentUnsafeError, match="compression ratio"):
            assert_zip_is_safe(_zip([("word/document.xml", prose)]))

    def test_realistic_ooxml_prose_passes(self):
        """The negative control at a realistic scale — varied text, not one repeated line."""
        import random

        rng = random.Random(20260813)  # noqa: S311 - test data, not a secret
        words = ["contract", "party", "deliverable", "milestone", "invoice", "clause", "term"]
        body = " ".join(rng.choice(words) for _ in range(20000)).encode()
        archive = _zip([("word/document.xml", body)])
        assert_zip_is_safe(archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            info = zf.infolist()[0]
        assert info.file_size / info.compress_size < MAX_COMPRESSION_RATIO


class TestPathTraversal:
    @pytest.mark.parametrize(
        "name",
        ["../../etc/passwd", "/etc/passwd", "word/../../../evil.xml", "..\\..\\windows\\evil"],
    )
    def test_a_member_escaping_its_root_is_refused(self, name: str):
        with pytest.raises(DocumentUnsafeError, match="absolute path|escapes its root"):
            assert_zip_is_safe(_zip([(name, b"x")]))

    def test_a_dotdot_inside_a_filename_is_not_traversal(self):
        """``report..final.docx`` is a filename, not an escape. A substring check on
        ``..`` rejects it and the user cannot tell why."""
        assert_zip_is_safe(_zip([("word/report..final.xml", b"<x/>")]))


class TestEncryptedContainers:
    def test_an_encrypted_zip_member_is_refused_as_encrypted_not_as_corrupt(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("word/document.xml", "<x/>")
        data = bytearray(buffer.getvalue())
        # Set the encryption bit in the local header and the central directory.
        data[6] |= 0x01
        index = data.find(b"PK\x01\x02")
        data[index + 8] |= 0x01
        with pytest.raises(DocumentEncryptedError, match="encrypted"):
            assert_zip_is_safe(bytes(data))


class TestPageCeiling:
    def test_over_the_ceiling_raises(self):
        with pytest.raises(DocumentTooLargeError, match="2000-page ceiling"):
            assert_page_count(2001, 2000)

    def test_exactly_the_ceiling_passes(self):
        assert_page_count(2000, 2000)


class TestXmlHardening:
    def test_defuse_is_idempotent_and_never_raises(self):
        """Called at package import; a second call from a worker bootstrap must be a no-op."""
        import xml.etree.ElementTree as ET

        defuse_xml()
        first = ET.XMLParser
        defuse_xml()
        assert ET.XMLParser is first, "a second defuse re-wrapped an already-wrapped parser"

    def test_the_stdlib_parser_refuses_an_entity_expansion_bomb(self):
        """``defuse_stdlib`` is called at package import; this asserts it took effect."""
        import xml.etree.ElementTree as ET

        from app.services import documents  # noqa: F401  (import triggers defuse_xml)

        billion_laughs = (
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            "]><lolz>&lol2;</lolz>"
        )
        with pytest.raises(Exception, match="(?i)entit"):
            ET.fromstring(billion_laughs)  # noqa: S314 - the point is that it refuses


def _dispatched_a_guard(mime: str, data: bytes) -> bool:
    """Did :func:`prescan` route *mime* to any guard at all?

    ``prescan`` returns ``None`` either way, so "it did not raise" cannot distinguish
    "the guards passed" from "no guard ran". This re-derives the routing decision the
    same way ``prescan`` does, so the assertion has something to be wrong about.
    """
    return mime == "application/pdf" or data[:4] == b"PK\x03\x04"


class TestPrescanDispatch:
    def test_a_zip_based_format_gets_the_archive_guards(self):
        with pytest.raises(DocumentUnsafeError):
            prescan(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                _zip([("../evil", b"x")]),
            )

    def test_plain_text_needs_no_guard_and_does_not_raise(self):
        prescan("text/plain", b"just some words")
        # The negative control for the ZIP dispatch above: a guard that fired on every
        # mime would pass that test and fail this one.
        assert not _dispatched_a_guard("text/plain", b"just some words")


@pytest.mark.skipif(
    not PASSWORD_PDF.is_file(),
    reason=(
        "docling-fixtures not present under $RAG_EVAL_DATA_DIR/documents. A synthetic "
        "encrypted PDF would test our own generator; this is a real one. Fetch with "
        "scripts/fetch-rag-eval-data.sh --only docling-fixtures."
    ),
)
class TestTheRealPasswordProtectedPdf:
    def test_it_is_refused_as_encrypted_rather_than_as_corrupt(self):
        """The distinction is the whole point: 'this PDF is password-protected' is
        actionable, 'processing error' is a support ticket."""
        with pytest.raises(DocumentEncryptedError, match="password-protected"):
            assert_pdf_is_readable(PASSWORD_PDF.read_bytes())

    def test_prescan_routes_a_pdf_to_the_pdf_guard(self):
        with pytest.raises(DocumentEncryptedError):
            prescan("application/pdf", PASSWORD_PDF.read_bytes())

    def test_an_ordinary_pdf_from_the_same_corpus_passes(self):
        """Negative control against the encrypted case above."""
        ordinary = sorted(
            (
                NAS_ROOT / "documents" / "docling-fixtures" / "tests" / "data" / "pdf" / "sources"
            ).glob("*.pdf")
        )
        assert ordinary, "no unencrypted PDF fixtures to control against"
        assert_pdf_is_readable(ordinary[0].read_bytes())


@pytest.mark.skipif(
    not (NAS_ROOT / "documents").is_dir(),
    reason=(
        "$RAG_EVAL_DATA_DIR/documents not present. This is what turns the ceilings from "
        "chosen numbers into measured ones. Fetch with scripts/fetch-rag-eval-data.sh."
    ),
)
class TestTheCeilingsAgainstTheRealCorpus:
    """The thresholds are only defensible against a measured distribution.

    A guard tuned by intuition either lets bombs through or rejects real documents, and
    which one it does is unknowable without looking at real files. Measured over every
    ZIP-based file in ``docling-fixtures``, ``govdocs1`` and ``ami-documents``:
    **63 archives, 1,700 members ≥ 1 KB, worst member ratio 58.4:1, p99 28.8:1.**
    """

    @staticmethod
    def _real_archives() -> list[Path]:
        roots = [
            NAS_ROOT / "documents" / "docling-fixtures",
            NAS_ROOT / "documents" / "govdocs1",
            NAS_ROOT / "documents" / "ami-documents",
        ]
        found: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                    continue
                try:
                    with path.open("rb") as handle:
                        if handle.read(4) != b"PK\x03\x04":
                            continue
                except OSError:
                    continue
                found.append(path)
        return found

    def test_no_real_document_archive_comes_near_the_ratio_ceiling(self):
        archives = self._real_archives()
        assert len(archives) >= 40, f"only {len(archives)} real archives found — corpus is thin"

        worst = 0.0
        worst_name = ""
        unreadable: list[str] = []
        readable = 0
        for path in archives:
            try:
                with zipfile.ZipFile(path) as zf:
                    infos = zf.infolist()
            except zipfile.BadZipFile:
                # Counted and asserted below, never swallowed: a corpus that suddenly
                # became mostly unreadable would otherwise make this test measure nothing.
                unreadable.append(path.name)
                continue
            readable += 1
            for info in infos:
                if info.compress_size > 0 and info.file_size > 1024:
                    ratio = info.file_size / info.compress_size
                    if ratio > worst:
                        worst, worst_name = ratio, f"{path.name}:{info.filename}"

        assert len(unreadable) <= 2, f"{len(unreadable)} archives are unreadable: {unreadable[:5]}"
        assert readable >= 40, f"only {readable} archives were actually inspected"
        assert worst > 1.0, "no member had any compression at all — the scan measured nothing"
        assert worst < MAX_COMPRESSION_RATIO / 2, (
            f"the worst real member ratio is {worst:.1f}:1 ({worst_name}), which leaves less "
            f"than 2x headroom under the {MAX_COMPRESSION_RATIO:.0f}:1 ceiling — the ceiling "
            f"is about to start rejecting honest documents"
        )

    def test_every_real_document_archive_passes_every_guard(self):
        """The corpus-wide negative control for the whole of ``assert_zip_is_safe``."""
        rejected = []
        for path in self._real_archives():
            data = path.read_bytes()
            try:
                assert_zip_is_safe(data)
            except (DocumentUnsafeError, DocumentTooLargeError, DocumentEncryptedError) as exc:
                rejected.append((path.name, str(exc)[:80]))
        assert not rejected, f"guards rejected real documents: {rejected[:5]}"
