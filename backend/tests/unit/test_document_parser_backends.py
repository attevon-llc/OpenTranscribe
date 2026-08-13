"""Format coverage, measured on the real corpora rather than on fixtures we authored.

A fixture we generate proves our generator and our parser agree. These files were produced
by Word, PowerPoint, Excel, LaTeX, government web publishing systems and 1914 typewriters,
which is the only population that can falsify a claim about format coverage. `govdocs1` in
particular is 991 uncurated real-world files and exists here purely as a **robustness**
control: the assertion is about the crash rate, not about extraction quality.

Every count in an assertion below was measured before it was written, and the negative
controls are as load-bearing as the positive ones — ``old_scans`` having *no* text layer is
what proves the ``has_embedded_text`` discrimination is real and not always-true.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from app.services.documents import ParseOptions
from app.services.documents import ParseSource
from app.services.documents import detect_document_mime
from app.services.documents import validate_ir
from app.services.documents.backends.docling_slim import DoclingSlimParser
from app.services.documents.backends.docling_slim import linearize_table
from app.services.documents.types import DocumentEncryptedError

NAS_ROOT = Path(os.environ.get("RAG_EVAL_DATA_DIR", "/mnt/nas/opentranscribe-benchmarks"))
DOCS = NAS_ROOT / "documents"
FIXTURES = DOCS / "docling-fixtures" / "tests" / "data"

pytestmark = pytest.mark.skipif(
    not FIXTURES.is_dir(),
    reason=(
        "$RAG_EVAL_DATA_DIR/documents/docling-fixtures not present. These tests are "
        "deliberately corpus-backed — a synthetic DOCX cannot falsify a format-coverage "
        "claim. Fetch with scripts/fetch-rag-eval-data.sh --only docling-fixtures."
    ),
)


@pytest.fixture(scope="module")
def parser() -> DoclingSlimParser:
    return DoclingSlimParser()


@pytest.fixture(scope="module")
def docx_corpus(parser) -> dict[str, object]:
    """Parse the 32 DOCX fixtures ONCE.

    Three tests assert different things about the same set, and DOCX is the slowest
    format here (~31 s for the set). Parsing per test tripled the module's wall clock for
    no additional coverage.
    """
    out: dict[str, object] = {}
    for path in sorted((FIXTURES / "docx" / "sources").glob("*.docx")):
        out[path.name] = _parse(parser, path)
    return out


def _parse(parser: DoclingSlimParser, path: Path, **option_kwargs):
    data = path.read_bytes()
    mime = detect_document_mime(path.name, data[:512], data)
    assert mime is not None, f"{path.name} was not detected as a document at all"
    document = parser.parse(
        ParseSource(filename=path.name, mime=mime, data=data),
        options=ParseOptions(**option_kwargs),
    )
    validate_ir(document)
    return document


class TestEveryV1FormatParses:
    """One assertion per format, over EVERY fixture of that format, not a sample."""

    @pytest.mark.parametrize(
        ("subdir", "suffix", "min_files"),
        [
            ("pptx", ".pptx", 8),
            ("xlsx", ".xlsx", 10),
            ("md", ".md", 10),
            ("csv", ".csv", 9),
            ("html", ".html", 30),
        ],
    )
    def test_the_whole_fixture_set_for_a_format_parses_with_a_valid_ir(
        self, parser, subdir, suffix, min_files
    ):
        files = sorted((FIXTURES / subdir / "sources").glob(f"*{suffix}"))
        assert len(files) >= min_files, (
            f"expected at least {min_files} {suffix} fixtures, found {len(files)} — "
            f"the corpus shrank and this assertion is now weaker than it was written to be"
        )

        failures: list[tuple[str, str]] = []
        empty: list[str] = []
        for path in files:
            try:
                document = _parse(parser, path)
            except Exception as exc:  # noqa: BLE001 - the failure list IS the assertion
                failures.append((path.name, f"{type(exc).__name__}: {exc}"[:120]))
                continue
            if not document.text.strip():
                empty.append(path.name)

        assert not failures, f"{len(failures)}/{len(files)} {suffix} files failed: {failures[:5]}"
        assert not empty, f"{len(empty)}/{len(files)} {suffix} files produced no text: {empty[:5]}"

    def test_docx_coverage_is_complete_including_the_equation_documents(self, docx_corpus):
        """Regression pin for the ``pylatexenc`` trap.

        Docling's Word backend references ``UnicodeToLatexEncoder`` unguarded; without
        ``pylatexenc`` installed, 5 of these 32 files raise a bare ``NameError`` — which
        reads as a bug in our tree, not a missing dependency. Measured: 27/32 before,
        32/32 after. If the extra is ever dropped from requirements.txt, this fails.
        """
        assert len(docx_corpus) == 32, f"the docx fixture set changed size ({len(docx_corpus)})"
        assert all(doc.blocks for doc in docx_corpus.values())  # type: ignore[attr-defined]


class TestTablesSurviveWithTheirGrid:
    def test_a_docx_containing_tables_yields_both_table_and_prose_blocks(self, docx_corpus):
        """The ``docling#3335`` pin: its DOCX **chunkers** return 0 chunks when a table is
        present. We walk the document tree instead of chunking, so we are not exposed —
        and this asserts that a future switch to ``HybridChunker`` cannot silently
        reintroduce it, because the counts would go to zero.
        """
        candidates = {n: d for n, d in docx_corpus.items() if "table" in n}
        assert len(candidates) >= 3, f"only {len(candidates)} table-named DOCX fixtures"

        with_tables = sum(
            1
            for doc in candidates.values()
            if "table" in {b.type for b in doc.blocks}  # type: ignore[attr-defined]
        )
        assert with_tables == len(candidates), (
            f"only {with_tables}/{len(candidates)} table-named DOCX fixtures produced a "
            f"table block — this is the shape docling#3335 destroys"
        )

    def test_a_table_bearing_docx_still_yields_its_prose(self, docx_corpus):
        """The half of docling#3335 that actually loses data.

        ``table_with_equations.docx`` is legitimately nothing but a table, so "every
        table document also has prose" is false. The real invariant is that a document
        with prose AND a table keeps both — which is exactly what the upstream chunker
        bug destroys.
        """
        both = 0
        for doc in docx_corpus.values():
            types = {block.type for block in doc.blocks}  # type: ignore[attr-defined]
            if "table" in types and types - {"table"}:
                both += 1
        assert both >= 2, f"only {both} DOCX fixtures kept both a table and its prose"

    def test_plain_text_files_from_the_wild_parse(self, parser):
        """No dedicated TXT fixture directory exists, so this uses govdocs1's real ones —
        which is the better corpus anyway: government web text with mixed encodings."""
        root = DOCS / "govdocs1"
        if not root.is_dir():
            pytest.skip("govdocs1 not fetched")
        files = sorted(p for p in root.rglob("*.txt") if p.stat().st_size < 4_000_000)[:40]
        assert len(files) >= 20, f"only {len(files)} real .txt files found"

        parsed = 0
        for path in files:
            document = _parse(parser, path)
            if document.text.strip():
                parsed += 1
        assert parsed == len(files), f"{len(files) - parsed} real .txt files produced no text"

    def test_a_table_block_keeps_its_grid_alongside_the_linearised_text(self, parser):
        files = sorted((FIXTURES / "xlsx" / "sources").glob("*.xlsx"))
        assert files

        grids = 0
        for path in files:
            for block in _parse(parser, path).blocks:
                if block.type == "table" and block.table:
                    grids += 1
                    assert isinstance(block.table[0], list)
        assert grids, "no XLSX fixture produced a table block carrying its grid"

    def test_linearisation_puts_the_header_next_to_its_value(self):
        """Why ``header: cell`` and not Markdown — BM25 recall on a numeric query needs
        the header term adjacent to the number, and a pipe table separates them."""
        rendered = linearize_table([["Item", "Cost"], ["Widget", "42"]])
        assert "Item: Widget" in rendered
        assert "Cost: 42" in rendered

    def test_a_header_only_table_still_renders_something(self):
        assert linearize_table([["A", "B"]]) == "A | B"

    def test_an_empty_grid_renders_empty_rather_than_raising(self):
        assert linearize_table([]) == ""


class TestPdfTextLayerAndTheScanDiscrimination:
    def test_text_layer_pdfs_extract_with_page_numbers_on_every_block(self, parser):
        files = sorted((DOCS / "pmc-oa" / "pdf").glob("*.pdf"))[:20]
        if not files:
            pytest.skip("pmc-oa not fetched")
        assert len(files) >= 10

        for path in files:
            document = _parse(parser, path)
            assert document.has_embedded_text, (
                f"{path.name} has a text layer but was flagged as a scan"
            )
            assert document.page_count > 0
            assert document.blocks, f"{path.name} produced no blocks"
            pages = {block.page for block in document.blocks}
            assert None not in pages, f"{path.name} produced blocks with no page number"
            assert max(p for p in pages if p) <= document.page_count

    def test_scanned_pdfs_are_flagged_as_needing_ocr(self, parser):
        """The negative control for the assertion above. Measured: 60/60 of olmOCR-bench's
        ``old_scans`` extract **zero** characters, against ~2,300 chars/page for the
        ``tables`` category — the threshold sits an order of magnitude from both.
        """
        scans = sorted(
            (DOCS / "olmocr-bench" / "repo" / "bench_data" / "pdfs" / "old_scans").glob("*.pdf")
        )[:30]
        if not scans:
            pytest.skip("olmocr-bench not fetched")
        assert len(scans) >= 20

        flagged = 0
        for path in scans:
            document = _parse(parser, path)
            if not document.has_embedded_text:
                flagged += 1
                assert any("OCR" in warning for warning in document.warnings), (
                    f"{path.name} needs OCR but the parse carries no warning saying so — "
                    f"this is the silent-degradation failure mode"
                )
        assert flagged == len(scans), f"only {flagged}/{len(scans)} scans were flagged"

    def test_a_password_protected_pdf_is_refused_as_encrypted(self, parser):
        path = FIXTURES / "pdf_password" / "sources" / "2206.01062_pg3.pdf"
        if not path.is_file():
            pytest.skip("the password-protected fixture is not present")
        with pytest.raises(DocumentEncryptedError):
            _parse(parser, path)

    def test_the_page_ceiling_truncates_with_a_warning_rather_than_failing(self, parser):
        """Half a long document beats none of it — as long as the truncation is said."""
        multipage = [
            p
            for p in sorted((DOCS / "ucsf-idl" / "pdf").glob("*.pdf"))[:40]
            if p.stat().st_size > 100_000
        ]
        if not multipage:
            pytest.skip("ucsf-idl not fetched")

        document = _parse(parser, multipage[0], max_pages=3)
        assert document.blocks
        pages = {block.page for block in document.blocks if block.page}
        assert max(pages) <= 3
        if document.page_count > 3:
            assert any("page ceiling" in warning for warning in document.warnings)


class TestRobustnessAgainstUncuratedInput:
    """`govdocs1` — 991 real files off the public web, 20 formats, no curation at all.

    The claim under test is **"nothing crashes in a way the pipeline cannot classify"**,
    not "everything parses". A file we cannot read must raise a typed
    ``DocumentParseError``; anything else is an unhandled exception that would land a
    document in ``processing_error`` with no guidance.
    """

    def test_no_uncurated_file_produces_an_untyped_exception(self, parser):
        from app.services.documents.types import DocumentParseError

        root = DOCS / "govdocs1"
        if not root.is_dir():
            pytest.skip("govdocs1 not fetched")
        every = sorted(p for p in root.rglob("*") if p.is_file() and p.stat().st_size < 1_000_000)
        assert len(every) >= 500, f"only {len(every)} govdocs1 files — corpus is not the real one"
        # A deterministic stride sample, not a slice: the corpus is ordered by filename and
        # a head slice would be one contiguous crawl of similar documents. Bounded because
        # the full 991-file sweep is 275 s, which alone would more than double the suite
        # (issue #431's rule: a slow suite stops being run).
        files = every[::3][:220]

        attempted = 0
        typed_failures = 0
        untyped: list[tuple[str, str]] = []
        for path in files:
            data = path.read_bytes()
            mime = detect_document_mime(path.name, data[:512], data)
            if mime is None or not parser.supports(mime, path.name, needs_ocr=False):
                continue
            attempted += 1
            try:
                document = parser.parse(
                    ParseSource(filename=path.name, mime=mime, data=data),
                    options=ParseOptions(max_pages=50),
                )
                validate_ir(document)
            except DocumentParseError:
                typed_failures += 1
            except Exception as exc:  # noqa: BLE001 - collecting these IS the assertion
                untyped.append((path.name, f"{type(exc).__name__}: {exc}"[:100]))

        assert attempted >= 120, f"only {attempted} govdocs1 files were even claimed by a tier"
        assert not untyped, (
            f"{len(untyped)}/{attempted} uncurated files raised an UNTYPED exception "
            f"(the pipeline cannot classify these): {untyped[:5]}"
        )
        assert typed_failures / attempted < 0.10, (
            f"{typed_failures}/{attempted} typed failures — over 10% of real-world input "
            f"being rejected means a detection or backend regression, not bad input"
        )


def _sidecar_url() -> str | None:
    """The reachable docling-serve, or ``None``.

    Auto-enables by TCP probe, the way the root conftest auto-enables the MinIO- and
    OpenSearch-backed suites. An explicit ``DOCUMENT_PARSER_URL`` wins; otherwise the
    loopback port ``docker-compose.documents.yml`` publishes is probed, so
    ``./opentr.sh start dev --with-documents`` is enough to make the OCR tests below run.

    Requiring the env var was a silent-skip trap: the overlay sets ``DOCUMENT_PARSER_URL``
    inside the *containers*, and host-side pytest never sees it — so the two tests that
    prove OCR works skipped on exactly the setup that was meant to run them, and a green
    local run proved less than it looked.
    """
    url = os.environ.get("DOCUMENT_PARSER_URL", "").strip()
    if not url:
        url = f"http://localhost:{os.environ.get('DOCLING_SERVE_PORT', '5197')}"
    host = url.split("//", 1)[-1].split("/", 1)[0]
    hostname, _, port = host.partition(":")
    try:
        with socket.create_connection((hostname, int(port or 80)), timeout=1.0):
            return url
    except OSError:
        return None


@pytest.mark.skipif(
    _sidecar_url() is None,
    reason=(
        "No reachable docling-serve. The OCR path cannot be proven without one — a mocked "
        "sidecar would only prove the mock. Start one with "
        "`./opentr.sh start dev --with-documents` (publishes 127.0.0.1:5197), or point "
        "DOCUMENT_PARSER_URL at an existing sidecar."
    ),
)
class TestTheSidecarOcrPath:
    """Real OCR against a real sidecar. Verified on docling-serve 1.30.0 / docling 2.118.0."""

    @pytest.fixture(scope="class")
    def sidecar(self):
        from app.services.documents.backends.docling_serve import DoclingServeParser

        parser = DoclingServeParser(_sidecar_url() or "", poll_timeout=600)
        available, detail = parser.health()
        assert available, detail
        return parser

    def test_a_scan_with_no_text_layer_comes_back_with_text(self, sidecar):
        """The end-to-end OCR proof, and the ``traverse_pictures`` regression pin.

        The layout model classifies a full-page scan as one ``PictureItem`` and attaches
        every OCR'd cell as a *child* of it. A default document walk returns the literal
        Markdown ``<!-- image -->`` and the document indexes as empty — OCR succeeded and
        produced nothing. Measured on this file: 30 blocks / 730 characters.
        """
        scans = sorted(
            (DOCS / "olmocr-bench" / "repo" / "bench_data" / "pdfs" / "old_scans").glob("*.pdf")
        )
        if not scans:
            pytest.skip("olmocr-bench not fetched")

        document = sidecar.parse(
            ParseSource(filename=scans[0].name, mime="application/pdf", path=scans[0]),
            options=ParseOptions(ocr="auto"),
        )
        validate_ir(document)
        assert len(document.blocks) >= 10, "OCR produced almost nothing — check traverse_pictures"
        assert len(document.text) >= 200
        assert document.ocr_applied

    def test_ocr_pages_are_attributed_so_a_citation_can_anchor_to_a_page(self, sidecar):
        """OCR cells carry no provenance of their own; the page comes from the ancestor
        picture. Without that walk every OCR'd block is ``page=None`` and a page-anchored
        citation into a scan degrades to a file-level one."""
        scans = sorted(
            (DOCS / "olmocr-bench" / "repo" / "bench_data" / "pdfs" / "old_scans").glob("*.pdf")
        )
        if not scans:
            pytest.skip("olmocr-bench not fetched")

        document = sidecar.parse(
            ParseSource(filename=scans[0].name, mime="application/pdf", path=scans[0]),
            options=ParseOptions(ocr="auto"),
        )
        assert document.blocks
        pages = {block.page for block in document.blocks}
        assert None not in pages, "OCR'd blocks lost their page attribution"
        assert document.ocr_pages == document.page_count
        assert not document.warnings, f"unexpected degradation warnings: {document.warnings}"
