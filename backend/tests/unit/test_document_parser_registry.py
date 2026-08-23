"""The single branch point: which tier parses a document, and what happens when it is down.

Every resolution rule lives in ``registry.get_parser_for``. These tests exist so that a
future ``if parser.name == ...`` somewhere else has to break something visible.

The stub parsers here are *test doubles for a Protocol*, not mocks of our code: the thing
under test is the resolution policy, and driving it with real Docling would make the test
about Docling's availability instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.services.documents import registry
from app.services.documents.ir import IRBuilder
from app.services.documents.ir import ParsedDocument
from app.services.documents.protocol import DocumentParser
from app.services.documents.types import DocumentParserUnavailableError
from app.services.documents.types import DocumentUnsupportedError
from app.services.documents.types import ParseOptions
from app.services.documents.types import ParseSource

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OLE2 = "application/x-ole-storage"


class StubParser:
    """A `DocumentParser` whose availability and format claims the test controls."""

    def __init__(
        self,
        name: str,
        *,
        mimes: set[str],
        ocr: bool = False,
        available: bool = True,
    ) -> None:
        self.name = name
        self.version = "stub-1"
        self._mimes = mimes
        self._ocr = ocr
        self._available = available
        self.parsed: list[str] = []

    def supports(self, mime: str, filename: str, *, needs_ocr: bool) -> bool:
        if needs_ocr and not self._ocr:
            return False
        return mime in self._mimes

    def health(self) -> tuple[bool, str]:
        return self._available, "stub up" if self._available else "stub down"

    def parse(self, source: ParseSource, *, options: ParseOptions) -> ParsedDocument:
        self.parsed.append(source.filename)
        builder = IRBuilder()
        builder.add("paragraph", f"parsed by {self.name}")
        return builder.build(parser=self.name, parser_version=self.version)


@pytest.fixture
def tiers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, StubParser]]:
    """Install three controllable tiers and clear the process-global caches."""
    registry.reset_for_tests()
    stubs = {
        "docling.serve": StubParser("docling.serve", mimes={PDF, DOCX}, ocr=True),
        "docling.slim": StubParser("docling.slim", mimes={PDF, DOCX}),
        "tika": StubParser("tika", mimes={OLE2}),
    }
    monkeypatch.setattr(registry, "get_backend", lambda name: stubs.get(name))
    yield stubs
    registry.reset_for_tests()


def _policy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(registry.C, "DOCUMENT_PARSER_BACKEND", value)


class TestAutoPolicy:
    def test_the_sidecar_wins_when_it_is_up(self, tiers, monkeypatch):
        """It is the only tier with layout, tables and OCR, so up means strictly better."""
        _policy(monkeypatch, "auto")
        assert registry.get_parser_for(PDF, "a.pdf").name == "docling.serve"

    def test_it_falls_back_to_slim_when_the_sidecar_is_down(self, tiers, monkeypatch):
        _policy(monkeypatch, "auto")
        tiers["docling.serve"]._available = False
        assert registry.get_parser_for(PDF, "a.pdf").name == "docling.slim"

    def test_a_legacy_format_reaches_tika_past_two_tiers_that_do_not_claim_it(
        self, tiers, monkeypatch
    ):
        _policy(monkeypatch, "auto")
        assert registry.get_parser_for(OLE2, "old.doc").name == "tika"

    def test_needs_ocr_skips_every_tier_without_ocr(self, tiers, monkeypatch):
        """A scan must never be silently handed to a tier that returns an empty parse."""
        _policy(monkeypatch, "auto")
        assert registry.get_parser_for(PDF, "scan.pdf", needs_ocr=True).name == "docling.serve"

        tiers["docling.serve"]._available = False
        registry.mark_unavailable("docling.serve", "stub down")
        with pytest.raises(DocumentParserUnavailableError):
            registry.get_parser_for(PDF, "scan.pdf", needs_ocr=True)

    def test_an_unreachable_tier_reports_which_tier_and_why(self, tiers, monkeypatch):
        _policy(monkeypatch, "auto")
        for stub in tiers.values():
            stub._available = False
        with pytest.raises(DocumentParserUnavailableError) as excinfo:
            registry.get_parser_for(PDF, "a.pdf")
        assert "docling.serve" in (excinfo.value.detail or "")
        assert "stub down" in (excinfo.value.detail or "")

    def test_unreachable_is_retryable_but_unsupported_is_not(self, tiers, monkeypatch):
        """The distinction the parse task branches on: retry a down sidecar, never a
        format nothing can read."""
        _policy(monkeypatch, "auto")
        for stub in tiers.values():
            stub._available = False
        with pytest.raises(DocumentParserUnavailableError) as down:
            registry.get_parser_for(PDF, "a.pdf")
        assert down.value.retryable is True

        for name, stub in tiers.items():
            stub._available = True
            registry.health_of(name, refresh=True)
        with pytest.raises(DocumentUnsupportedError) as unsupported:
            registry.get_parser_for("application/x-nintendo-rom", "mario.nes")
        assert unsupported.value.retryable is False


class TestPinnedPolicies:
    @pytest.mark.parametrize(
        ("policy", "expected"),
        [("slim", "docling.slim"), ("serve", "docling.serve")],
    )
    def test_a_pinned_policy_uses_only_that_tier(self, tiers, monkeypatch, policy, expected):
        _policy(monkeypatch, policy)
        assert registry.get_parser_for(PDF, "a.pdf").name == expected

    def test_a_pinned_tier_that_is_down_does_not_silently_fall_back(self, tiers, monkeypatch):
        """`serve` means serve. Falling back would make an OCR-required deployment
        quietly stop OCR'ing."""
        _policy(monkeypatch, "serve")
        tiers["docling.serve"]._available = False
        with pytest.raises(DocumentParserUnavailableError):
            registry.get_parser_for(PDF, "a.pdf")

    def test_slim_refuses_a_document_that_needs_ocr_rather_than_returning_nothing(
        self, tiers, monkeypatch
    ):
        _policy(monkeypatch, "slim")
        with pytest.raises(DocumentUnsupportedError, match="OCR is not available"):
            registry.get_parser_for(PDF, "scan.pdf", needs_ocr=True)

    def test_an_unknown_policy_degrades_to_auto_rather_than_crashing_every_parse(
        self, tiers, monkeypatch, caplog
    ):
        _policy(monkeypatch, "docling-supreme")
        assert registry.get_parser_for(PDF, "a.pdf").name == "docling.serve"
        assert "DOCUMENT_PARSER_BACKEND" in caplog.text


class TestHealthCaching:
    def test_a_failed_parse_invalidates_the_cached_health_immediately(self, tiers, monkeypatch):
        """A sidecar that dies mid-import must not stay cached as healthy for the TTL —
        every document in that window would pay the full connect timeout first."""
        _policy(monkeypatch, "auto")
        assert registry.get_parser_for(PDF, "a.pdf").name == "docling.serve"

        registry.mark_unavailable("docling.serve", "connection refused mid-parse")
        assert registry.get_parser_for(PDF, "b.pdf").name == "docling.slim"

    def test_a_repeated_resolution_does_not_re_probe(self, tiers, monkeypatch):
        """Without the TTL, importing 50 small PDFs is 50 HTTP round trips of pure wait."""
        _policy(monkeypatch, "auto")
        probes = {"n": 0}
        stub = tiers["docling.serve"]
        original = stub.health

        def counting_health() -> tuple[bool, str]:
            probes["n"] += 1
            available, detail = original()
            return available, detail

        stub.health = counting_health  # type: ignore[method-assign]

        for _ in range(5):
            registry.get_parser_for(PDF, "a.pdf")
        assert probes["n"] == 1

    def test_a_health_probe_that_raises_is_reported_as_unavailable_not_propagated(
        self, tiers, monkeypatch
    ):
        _policy(monkeypatch, "auto")

        def exploding_health() -> tuple[bool, str]:
            raise RuntimeError("connection reset")

        tiers["docling.serve"].health = exploding_health  # type: ignore[method-assign]
        available, detail = registry.health_of("docling.serve", refresh=True)
        assert available is False
        assert "connection reset" in detail

    def test_the_health_report_covers_every_tier(self, tiers, monkeypatch):
        _policy(monkeypatch, "auto")
        report = registry.health_report()
        assert set(report) == {"docling.slim", "docling.serve", "tika"}
        assert all("available" in entry for entry in report.values())


class TestTheRealBackendsSatisfyTheProtocol:
    """Structural conformance, checked against the real classes rather than the stubs."""

    def test_each_backend_is_a_document_parser(self):
        from app.services.documents.backends.docling_serve import DoclingServeParser
        from app.services.documents.backends.docling_slim import DoclingSlimParser
        from app.services.documents.backends.tika import TikaParser

        instances: list[Any] = [
            DoclingSlimParser(),
            DoclingServeParser("http://sidecar.invalid"),
            TikaParser("http://tika.invalid"),
        ]
        assert len(instances) == 3
        for instance in instances:
            assert isinstance(instance, DocumentParser), f"{type(instance).__name__} does not"

    def test_the_names_are_the_ones_the_registry_resolves(self):
        from app.services.documents.backends.docling_serve import DoclingServeParser
        from app.services.documents.backends.docling_slim import DoclingSlimParser
        from app.services.documents.backends.tika import TikaParser

        assert {DoclingSlimParser.name, DoclingServeParser.name, TikaParser.name} == {
            "docling.slim",
            "docling.serve",
            "tika",
        }

    def test_an_unreachable_sidecar_health_check_returns_false_and_does_not_raise(self):
        from app.services.documents.backends.docling_serve import DoclingServeParser

        available, detail = DoclingServeParser("http://127.0.0.1:1", timeout=0.5).health()
        assert available is False
        assert "unreachable" in detail
