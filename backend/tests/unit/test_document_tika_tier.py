"""The Tika tier, measured against the corpus that justifies its existence.

Tika is here for legacy OLE2 — ``.doc``/``.ppt``/``.xls`` — which no Docling backend reads
and which a decade of real meeting attachments actually are. The AMI shared-document corpus
is **987 .ppt + 842 .doc + 87 .xls**, i.e. entirely OLE2, so it is both the justification for
the tier and the only population that can falsify a claim about it.

**Every assertion here is a floor on CHARACTERS EXTRACTED, not on absence of an exception.**
That is the whole point of this file. The tier shipped once as an unverified sketch which,
run against this corpus, reported ``.doc 14/14 ok, .ppt 23/23 ok, .xls 3/3 ok, 0 exceptions``
— while extracting **2 characters per document**. It sent our internal container mime
``application/x-ole-storage`` as the request ``Content-Type``; Tika took that as a detection
override, found no parser for it, selected ``EmptyParser`` and answered HTTP 200 with an
empty body. The two surviving characters were the ``/>`` of the self-closing ``<body/>``,
which the flattener's tag regex could not match — and which were enough to defeat the
"extracted nothing" check that would otherwise have caught it. A test that only counted
successes was green throughout.

Measured on the full 1,916-file AMI OLE2 sweep, Tika 3.3.1, after the fix:

======  =========  ======  ======  ========  ========
format  parsed     min     p10     median    max
======  =========  ======  ======  ========  ========
.doc    838/842       188     608      1433      5473
.ppt    987/987       177     479       787      2789
.xls     87/87        372    1109      1176      1275
======  =========  ======  ======  ========  ========

Zero untyped exceptions, zero typed failures, zero empty extractions. The four unparsed
``.doc`` files are one document duplicated across four meetings which is not OLE2 at all —
it is MHTML saved with a ``.doc`` extension, so ``detect_document_mime`` correctly returns
``None`` and it never reaches this tier. That is a detection gap, not a Tika failure.

The floors below are set from those numbers with roughly 2x headroom under the observed
minimum, which still leaves them ~50x above the 2-character regression they exist to catch.
"""

from __future__ import annotations

import os
import re
import socket
import statistics
from pathlib import Path

import pytest

from app.services.documents import ParseOptions
from app.services.documents import ParseSource
from app.services.documents import detect_document_mime
from app.services.documents import validate_ir
from app.services.documents.backends.tika import TikaParser
from app.services.documents.backends.tika import _body_of
from app.services.documents.types import DocumentParseError
from app.services.documents.types import DocumentParserUnavailableError
from app.services.documents.types import DocumentUnsupportedError

NAS_ROOT = Path(os.environ.get("RAG_EVAL_DATA_DIR", "/mnt/nas/opentranscribe-benchmarks"))
DOCS = NAS_ROOT / "documents"
AMI = DOCS / "ami-documents"
GOVDOCS = DOCS / "govdocs1"

#: How many files of each format the corpus tests parse. The full sweep is 1,916 files /
#: 234 s, which would more than double the whole backend suite — and issue #431's rule is
#: that a suite which stops being run gates nothing. A stride sample spreads the selection
#: across all 171 meetings instead of taking one contiguous run of near-identical documents.
SAMPLE_PER_FORMAT = 40

#: ``(suffix, per-file char floor, median char floor)`` — see the table in the module
#: docstring. The per-file floor is what a returning ``EmptyParser`` regression trips.
OLE2_FLOORS: tuple[tuple[str, int, int], ...] = (
    (".doc", 100, 700),
    (".ppt", 100, 400),
    (".xls", 200, 800),
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tika_url() -> str | None:
    """The reachable Tika, or ``None``.

    Auto-enables the same way the root conftest auto-enables MinIO and OpenSearch: an
    explicit ``DOCUMENT_TIKA_URL`` wins, otherwise the port ``docker-compose.documents.yml``
    publishes on loopback is probed. So ``./opentr.sh start dev --with-documents`` is
    sufficient to make this module run, with no env plumbing.
    """
    url = os.environ.get("DOCUMENT_TIKA_URL", "").strip()
    if not url:
        url = f"http://localhost:{os.environ.get('TIKA_PORT', '5198')}"
    host = url.split("//", 1)[-1].split("/", 1)[0]
    hostname, _, port = host.partition(":")
    try:
        with socket.create_connection((hostname, int(port or 80)), timeout=1.0):
            return url
    except OSError:
        return None


needs_tika = pytest.mark.skipif(
    _tika_url() is None,
    reason=(
        "No reachable Apache Tika. This tier cannot be proven without one — a mocked Tika "
        "would only prove the mock, and the defect this file exists for was a real server "
        "answering HTTP 200 with an empty body. Start one with "
        "`./opentr.sh start dev --with-documents` (publishes 127.0.0.1:5198), or point "
        "DOCUMENT_TIKA_URL at an existing server."
    ),
)
needs_ami = pytest.mark.skipif(
    not AMI.is_dir(),
    reason=(
        "$RAG_EVAL_DATA_DIR/documents/ami-documents not present. A format-coverage claim "
        "cannot be falsified by fixtures we authored; fetch with "
        "scripts/fetch-rag-eval-data.sh --only ami-documents."
    ),
)


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


@pytest.fixture(scope="module")
def tika() -> TikaParser:
    parser = TikaParser(_tika_url() or "")
    available, detail = parser.health()
    assert available, detail
    return parser


@pytest.fixture(scope="module")
def ole2_corpus(tika) -> dict[str, dict[str, object]]:
    """Parse a stride sample of each OLE2 format ONCE; four tests read the same result."""
    out: dict[str, dict[str, object]] = {}
    for suffix, _min_floor, _median_floor in OLE2_FLOORS:
        every = sorted(AMI.rglob(f"*{suffix}"))
        files = every[:: max(1, len(every) // SAMPLE_PER_FORMAT)][:SAMPLE_PER_FORMAT]
        lengths: list[int] = []
        failures: list[tuple[str, str]] = []
        undetected: list[str] = []
        block_types: set[str] = set()
        grids: list[int] = []
        table_share: list[float] = []
        for path in files:
            data = path.read_bytes()
            mime = detect_document_mime(path.name, data[:512], data)
            if mime is None:
                undetected.append(path.name)
                continue
            try:
                document = tika.parse(
                    ParseSource(filename=path.name, mime=mime, data=data),
                    options=ParseOptions(),
                )
                validate_ir(document)
            except Exception as exc:  # noqa: BLE001 - the failure list IS the assertion
                failures.append((path.name, f"{type(exc).__name__}: {exc}"[:140]))
                continue
            lengths.append(len(document.text))
            block_types.update(block.type for block in document.blocks)
            tables = [block for block in document.blocks if block.type == "table"]
            grids.append(sum(1 for block in tables if block.table))
            table_share.append(
                sum(len(block.text) for block in tables) / max(1, len(document.text))
            )
        out[suffix] = {
            "total": len(files),
            "lengths": lengths,
            "failures": failures,
            "undetected": undetected,
            "block_types": block_types,
            "grids": grids,
            "table_share": table_share,
        }
    return out


@needs_tika
@needs_ami
class TestLegacyOfficeCoverageIsMeasuredInCharacters:
    @pytest.mark.parametrize(("suffix", "min_chars", "median_chars"), OLE2_FLOORS)
    def test_every_sampled_file_of_a_format_yields_real_text(
        self, ole2_corpus, suffix, min_chars, median_chars
    ):
        """n/N **and** the distribution.

        A per-format ``40/40`` with a p10 of 2 characters is the same lie in a different
        shape, so the count is only half the assertion — the floors are the other half.
        """
        result = ole2_corpus[suffix]
        lengths: list[int] = result["lengths"]  # type: ignore[assignment]
        failures = result["failures"]
        assert result["total"] >= 20, (
            f"only {result['total']} {suffix} files sampled — the corpus shrank and this "
            f"assertion is weaker than it was written to be"
        )
        assert not failures, (
            f"{len(failures)}/{result['total']} {suffix} files failed: {failures[:5]}"
        )
        assert len(lengths) == result["total"] - len(result["undetected"])  # type: ignore[arg-type]

        assert min(lengths) >= min_chars, (
            f"the shortest {suffix} extraction is {min(lengths)} characters, under the "
            f"{min_chars} floor. The EmptyParser regression this floor exists for returned "
            f"exactly 2 characters per file while reporting success"
        )
        assert statistics.median(lengths) >= median_chars, (
            f"median {suffix} extraction is {statistics.median(lengths)} characters, under "
            f"the {median_chars} floor measured over the full 1,916-file sweep"
        )

    def test_spreadsheets_keep_their_grid_rather_than_flattening_to_prose(self, ole2_corpus):
        """``.xls`` is *nothing but* tables — measured, 94-99% of its characters live in one.

        Flattening rows into paragraphs would still pass every character-count floor above
        while destroying the header/value adjacency ``linearize_table`` exists to preserve —
        which is what BM25 recall on a numeric query depends on. So the floors alone do not
        cover this; the grid has to be asserted separately.

        Asserting ``"table" in block_types`` over the aggregate is **not enough**: one lucky
        spreadsheet out of forty satisfies it. Measured over the 40-file stride sample, every
        single file yields a table block carrying a real grid, so the assertion is per-file.
        """
        result = ole2_corpus[".xls"]
        grids: list[int] = result["grids"]  # type: ignore[assignment]
        share: list[float] = result["table_share"]  # type: ignore[assignment]
        assert len(grids) >= 20, f"only {len(grids)} .xls files parsed"
        without = sum(1 for count in grids if count == 0)
        assert without == 0, (
            f"{without}/{len(grids)} sampled .xls produced no table block carrying a grid; "
            f"their rows were flattened into prose and the header/value adjacency is gone"
        )
        assert min(share) >= 0.80, (
            f"the worst .xls has only {min(share):.0%} of its characters inside table blocks "
            f"(measured floor is 94%) — the grid is being partially lost to prose"
        )

    def test_detection_refuses_exactly_the_four_files_that_are_not_really_ole2(self):
        """The negative control for the coverage claim, over the **whole population**.

        The *only* AMI OLE2 files that never reach Tika are four copies of one document
        saved as MHTML with a ``.doc`` extension; ``detect_document_mime`` returns ``None``
        for them because their first bytes are ``MIME-Version:``, not the OLE2 signature.
        Pinning that stops a future widening of detection — routing MHTML at Tika and
        indexing whatever falls out — from reading as improved coverage.

        This runs over all 1,916 files rather than the parsed sample, deliberately. The
        40-file stride sample happens to contain **none** of the four, so the same assertion
        against the sample is vacuous: an empty result set runs any loop zero times and
        passes, which is exactly the shape it exists to reject. Detection is header-only and
        needs no Tika, so the full population costs ~5 s.
        """
        files = sorted(p for suffix, _, _ in OLE2_FLOORS for p in AMI.rglob(f"*{suffix}"))
        assert len(files) >= 1900, (
            f"only {len(files)} AMI OLE2 files — the corpus shrank and the counts below no "
            f"longer describe it"
        )

        undetected = []
        for path in files:
            with path.open("rb") as handle:
                header = handle.read(512)
            if detect_document_mime(path.name, header) is None:
                undetected.append((path.name, header[:13]))

        assert len(undetected) == 4, (
            f"{len(undetected)} AMI OLE2 files are undetected, not the measured 4: "
            f"{[n for n, _ in undetected][:8]}. Fewer means detection widened and something "
            f"that is not OLE2 is now being routed at Tika; more means it narrowed."
        )
        assert {name for name, _ in undetected} == {"IS1009docs.Participant1.Abstract3.doc"}
        assert {header for _, header in undetected} == {b"MIME-Version:"}, (
            "the four rejections are no longer the known MHTML file — a DIFFERENT document "
            "is now being refused and its content is silently missing from the corpus"
        )


@needs_tika
class TestTheServerSayingNoIsTypedCorrectly:
    """Four distinct outcomes that all used to collapse into one, driven by the real server.

    Every case here is produced by a live Tika rather than a stub, because each one is a
    fact about Tika's protocol — that it answers 200 for a format it cannot read, that a
    zero-byte body comes back as a metadata key rather than a status code — and a stub
    would encode our belief about the protocol instead of the protocol.
    """

    #: Bytes no detector can identify. Reused by the two tests below, which differ only in
    #: the filename — which is the point they are making.
    UNIDENTIFIABLE = b"\x01\x02\x03 not a document at all \xff\xfe" * 50

    def test_a_format_tika_has_no_parser_for_is_unsupported_not_empty(self, tika):
        """``EmptyParser`` is the server saying "no reader", and it arrives as HTTP **200**.

        Inferring it from a low character count is what the original sketch effectively did,
        and it inferred wrong. It is read from ``X-TIKA:Parsed-By`` instead.
        """
        with pytest.raises(DocumentUnsupportedError):
            tika.parse(
                ParseSource(
                    filename="junk.bin", mime="application/x-ole-storage", data=self.UNIDENTIFIABLE
                ),
                options=ParseOptions(),
            )

    def test_the_filename_hint_reaches_tika_and_changes_what_it_tries(self, tika):
        """The same bytes, a ``.doc`` name, and a **different** typed error — which proves
        the ``Content-Disposition`` hint is actually being honoured.

        Without this the hint could be silently dropped (wrong header name, stripped by a
        proxy) and nothing would notice: detection would still work on every real file,
        because magic bytes carry those. It only matters for the files whose bytes say
        nothing — and those are exactly the ones a user is most likely to have mislabelled.
        Named ``junk.bin`` the same payload is ``application/octet-stream``/EmptyParser
        (above); named ``junk.doc`` Tika resolves ``application/msword``, hands it to
        POI, and POI throws — a parse failure, not an unsupported format.
        """
        with pytest.raises(DocumentParseError) as excinfo:
            tika.parse(
                ParseSource(
                    filename="junk.doc", mime="application/x-ole-storage", data=self.UNIDENTIFIABLE
                ),
                options=ParseOptions(),
            )
        assert not isinstance(excinfo.value, DocumentUnsupportedError)

    def test_a_zero_byte_document_is_a_parse_error_not_a_crash(self, tika):
        """Tika reports this as ``X-TIKA:EXCEPTION:container_exception``, still HTTP 200."""
        with pytest.raises(DocumentParseError) as excinfo:
            tika.parse(
                ParseSource(filename="empty.doc", mime="application/x-ole-storage", data=b""),
                options=ParseOptions(),
            )
        assert not isinstance(excinfo.value, DocumentParserUnavailableError)

    def test_an_unreachable_tika_is_retryable_so_the_row_stays_pending(self):
        """The document is fine; the tier is not.

        ``registry.mark_unavailable`` plus ``retryable=True`` is what leaves the row for the
        retry sweep instead of burning it to ``failed`` — a container recycle must not cost
        the user their upload.
        """
        parser = TikaParser("http://127.0.0.1:1", timeout=2.0, health_timeout=1.0)
        with pytest.raises(DocumentParserUnavailableError) as excinfo:
            parser.parse(
                ParseSource(filename="x.doc", mime="application/x-ole-storage", data=b"abc"),
                options=ParseOptions(),
            )
        assert excinfo.value.retryable is True
        assert excinfo.value.error_category == "processing_error"

    def test_an_http_error_from_a_reachable_tika_is_not_retryable(self):
        """The other half, and the one that was wrong.

        Mapping every ``requests.RequestException`` to "unavailable" makes a permanently
        unparseable document retryable, so it cycles through the retry sweep forever. Only
        transport failures and 5xx are the tier's fault. Driven against the real server via
        a path that genuinely 404s, so this is Tika's status code and not an invented one.
        """
        parser = TikaParser(f"{_tika_url()}/no-such-endpoint")
        with pytest.raises(DocumentParseError) as excinfo:
            parser.parse(
                ParseSource(filename="x.doc", mime="application/x-ole-storage", data=b"abc"),
                options=ParseOptions(),
            )
        assert not isinstance(excinfo.value, DocumentParserUnavailableError)
        assert excinfo.value.retryable is False


@needs_tika
@pytest.mark.skipif(
    not GOVDOCS.is_dir(),
    reason=(
        "$RAG_EVAL_DATA_DIR/documents/govdocs1 not present; fetch with "
        "scripts/fetch-rag-eval-data.sh --only govdocs1."
    ),
)
class TestRobustnessAgainstUncuratedLegacyInput:
    """`govdocs1` OLE2 — real files off the public web, no curation, some genuinely broken.

    AMI is a clean corpus produced by one organisation with one version of Office; it can
    show coverage but not robustness. The claim here is **"nothing fails in a way the
    pipeline cannot classify"** — an untyped exception lands a document in
    ``processing_error`` with no guidance, which for a document is almost always the wrong
    answer.

    Measured over the full govdocs1 OLE2 set (182 files, 641 s): ``.doc`` 67/67, ``.ppt``
    52/54, ``.xls`` 57/60, ``.rtf`` 1/1, and **zero untyped exceptions**. The two ``.ppt``
    failures are real: one is PowerPoint 4.0, which POI refuses by design
    (``OldPowerPointFormatException``), and one throws ``ArrayIndexOutOfBoundsException``
    inside POI on a corrupt record. Both surface as typed ``DocumentParseError``. The three
    ``.xls`` are not spreadsheets at all and ``detect_document_mime`` never routes them here.

    Bounded to small files on purpose: the full sweep is 641 s because govdocs1 contains
    multi-megabyte spreadsheets (one extracts 4.6 M characters), and issue #431's rule is
    that a suite which stops being run gates nothing.
    """

    def test_no_uncurated_legacy_file_produces_an_untyped_exception(self, tika):
        files = sorted(
            p
            for suffix in (".doc", ".ppt", ".xls", ".rtf")
            for p in GOVDOCS.rglob(f"*{suffix}")
            if p.stat().st_size < 500_000
        )[:24]
        assert len(files) >= 15, f"only {len(files)} small govdocs1 OLE2 files found"

        attempted = 0
        typed_failures: list[str] = []
        untyped: list[tuple[str, str]] = []
        for path in files:
            data = path.read_bytes()
            mime = detect_document_mime(path.name, data[:512], data)
            if mime is None or not tika.supports(mime, path.name, needs_ocr=False):
                continue
            attempted += 1
            try:
                document = tika.parse(
                    ParseSource(filename=path.name, mime=mime, data=data),
                    options=ParseOptions(),
                )
                validate_ir(document)
            except DocumentParseError as exc:
                typed_failures.append(f"{path.name}: {type(exc).__name__}")
            except Exception as exc:  # noqa: BLE001 - collecting these IS the assertion
                untyped.append((path.name, f"{type(exc).__name__}: {exc}"[:100]))

        assert attempted >= 15, f"only {attempted} files were claimed by the tika tier"
        assert not untyped, (
            f"{len(untyped)}/{attempted} uncurated files raised an UNTYPED exception — the "
            f"pipeline cannot classify these: {untyped[:5]}"
        )
        assert len(typed_failures) / attempted < 0.15, (
            f"{len(typed_failures)}/{attempted} typed failures ({typed_failures[:5]}) — over "
            f"15% of real-world legacy input being rejected is a regression, not bad input; "
            f"the measured rate over the full 182-file set is 2/180"
        )


@needs_tika
class TestRegistryRoutingForLegacyFormats:
    """The registry is the single branch point; these pin what it sends where."""

    @pytest.fixture(autouse=True)
    def _tika_configured(self, monkeypatch):
        from app.core import constants as C  # noqa: N812
        from app.services.documents import registry

        monkeypatch.setattr(C, "DOCUMENT_TIKA_URL", _tika_url() or "")
        monkeypatch.setattr(C, "DOCUMENT_PARSER_URL", "")
        monkeypatch.setattr(C, "DOCUMENT_PARSER_BACKEND", "auto")
        registry.reset_for_tests()
        yield
        registry.reset_for_tests()

    @pytest.mark.parametrize("mime", ["application/x-ole-storage", "application/rtf"])
    def test_legacy_formats_resolve_to_the_tika_tier(self, mime):
        from app.services.documents import registry

        assert registry.get_parser_for(mime, "legacy.doc").name == "tika"

    def test_tika_is_never_chosen_for_a_pdf_even_when_it_is_the_only_tier_up(self):
        """Serving a PDF from Tika would silently downgrade every citation in it from
        page-anchored to character-anchored the day the sidecar went down."""
        from app.services.documents import registry

        parser = registry.get_parser_for("application/pdf", "paper.pdf")
        assert parser.name != "tika"

    def test_tika_is_never_chosen_for_work_that_needs_ocr(self):
        """It has no Tesseract, so claiming OCR work would produce a confident empty parse."""
        from app.services.documents import registry

        with pytest.raises((DocumentUnsupportedError, DocumentParserUnavailableError)):
            registry.get_parser_for("application/x-ole-storage", "scan.doc", needs_ocr=True)

    def test_marking_the_tier_unavailable_takes_it_out_of_resolution(self):
        """What the parse task does on a ``DocumentParserUnavailableError``, so the next
        document in a bulk import does not pay the full connect timeout again."""
        from app.services.documents import registry

        assert registry.get_parser_for("application/x-ole-storage", "a.doc").name == "tika"
        registry.mark_unavailable("tika", "container recycled")
        with pytest.raises(DocumentParserUnavailableError):
            registry.get_parser_for("application/x-ole-storage", "b.doc")


@needs_tika
class TestTikaAsADifferentialOracle:
    """A second, independently implemented extractor over a format the primary tier owns.

    Neither parser reports its own bugs. Agreement is weak evidence; **disagreement is a
    finding**, and this is the cheapest place to get one — the Docling and Tika text paths
    for ``.txt`` share no code, no language and no vendor.

    **The tolerance, and why it is that number.** The metric is token recall — the fraction
    of Tika's distinct alphanumeric tokens that also appear in Docling's output. Measured
    over 60 real ``govdocs1`` ``.txt`` files: **59 agree exactly (1.000)** and one scores
    0.977. That one file is ``Non-ISO extended-ASCII`` (a cp1252 Federal Register notice) and
    the gap is the two decoders differing on high bytes — a real, bounded, understood
    difference, not lost content.

    The floor is therefore **0.95**, and it is chosen to *discriminate*, not to accommodate:
    the noise floor is 0.977 and the structural-loss failure mode measured on the same run
    (an HTML document where Docling recovered 3 of 140 tables) scored **0.445**. 0.95 sits
    an order of magnitude away from the defect and just under the noise. It is not a fudge
    factor tuned until the suite went green — it was set after both distributions were
    measured, and the class of bug it must catch is 11x below it.
    """

    #: See the class docstring. Do not raise this to make a run pass.
    MIN_TOKEN_RECALL = 0.95

    @pytest.fixture(scope="class")
    def slim(self):
        from app.services.documents.backends.docling_slim import DoclingSlimParser

        parser = DoclingSlimParser()
        available, detail = parser.health()
        if not available:
            pytest.skip(f"the docling slim tier is not installed here: {detail}")
        return parser

    @pytest.mark.skipif(
        not GOVDOCS.is_dir(),
        reason=(
            "$RAG_EVAL_DATA_DIR/documents/govdocs1 not present. The oracle needs real files "
            "with real encodings; fetch with scripts/fetch-rag-eval-data.sh --only govdocs1."
        ),
    )
    def test_two_independent_extractors_agree_on_real_plain_text(self, tika, slim):
        files = sorted(p for p in GOVDOCS.rglob("*.txt") if p.stat().st_size < 2_000_000)[:30]
        assert len(files) >= 20, f"only {len(files)} real .txt files found"

        recalls: list[tuple[float, str]] = []
        for path in files:
            data = path.read_bytes()
            source = ParseSource(filename=path.name, mime="text/plain", data=data)
            from_tika = _tokens(tika.parse(source, options=ParseOptions()).text)
            from_slim = _tokens(slim.parse(source, options=ParseOptions()).text)
            assert from_tika, f"{path.name}: Tika extracted no tokens at all"
            recalls.append((len(from_tika & from_slim) / len(from_tika), path.name))

        assert len(recalls) == len(files)
        disagreements = [(round(r, 3), n) for r, n in recalls if r < self.MIN_TOKEN_RECALL]
        assert not disagreements, (
            f"{len(disagreements)}/{len(files)} files where the two extractors disagree by "
            f"more than the measured tolerance ({self.MIN_TOKEN_RECALL}). One of the two "
            f"parsers is losing text and neither will say so: {disagreements[:5]}"
        )
        assert statistics.median(r for r, _ in recalls) == 1.0, (
            "the two extractors no longer agree EXACTLY on the median plain-text file; "
            "measured, 59 of 60 agree to 1.000"
        )


class TestTheFlattenerBugThatHidTheOtherOne:
    """Pure unit tests for :func:`_body_of` — no container, so these always run.

    The original flattener split on the literal string ``"<body"``. For Tika's
    ``EmptyParser`` response, whose body is the self-closing ``<body/>``, that leaves ``"/>"``
    behind, and the tag-stripping regex ``<[^>]+>`` cannot match a bare ``/>``. Two junk
    characters survived per document, ``len(builder) == 0`` never fired, and a total
    extraction failure was reported as success. This is the smallest possible pin on that.
    """

    def test_a_self_closing_body_yields_nothing_at_all(self):
        assert _body_of("<html><head><title>x</title></head><body/></html>") == ""

    def test_a_self_closing_body_leaves_no_stray_angle_bracket_fragment(self):
        """The specific two characters. Asserting emptiness alone would also pass for an
        implementation that returned ``" "``, which strips to empty — but the failure being
        pinned produced ``"/>"``, which does not."""
        assert "/>" not in _body_of("<html><body/></html>")

    def test_a_normal_body_is_returned_without_its_head(self):
        body = _body_of(
            "<html><head><meta name='a' content='b'></head><body><p>Hi</p></body></html>"
        )
        assert body == "<p>Hi</p>"

    def test_a_response_with_no_body_element_still_drops_the_metadata_head(self):
        """Tika metadata is emitted as ``<meta>`` tags in the head. Letting those through
        would index the producer application and the last author's name as document text."""
        stripped = _body_of("<html><head><meta name='dc:creator' content='ami'></head>Text</html>")
        assert "dc:creator" not in stripped
        assert "Text" in stripped
