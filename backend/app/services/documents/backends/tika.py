"""Long-tail tier — Apache Tika, for the formats Docling has no reader for.

Scope is deliberately narrow: **legacy OLE2 (``.doc``/``.xls``/``.ppt``) and RTF**.
Docling reads neither, and the AMI shared-doc corpus alone is 987 ``.ppt`` + 842 ``.doc``
+ 87 ``.xls`` — i.e. the single largest real-world office corpus available to this project
is entirely OLE2. Tika is Apache-2.0 and runs as its own JVM container.

It has **no layout model**, so it is strictly a fallback: Tika's XHTML output gives
paragraphs, headings and tables but no page numbers, no bounding boxes and no reading-order
recovery. A document parsed here is searchable and citable by character range; it is not
citable by page. That is a real capability difference and
:attr:`TikaParser.supports` is what keeps it from quietly serving PDFs that the sidecar
would parse better.

Optional by design: when the container is absent the registry never selects this tier and
the upload path returns "convert to .docx or .pdf first", which is a better answer than a
worse parse.
"""

from __future__ import annotations

import logging
import re

from ..ir import IRBuilder
from ..ir import ParsedDocument
from ..types import DocumentEmptyError
from ..types import DocumentParseError
from ..types import DocumentParserUnavailableError
from ..types import DocumentUnsupportedError
from ..types import ParseOptions
from ..types import ParseSource

logger = logging.getLogger(__name__)

#: The formats this tier exists for. Not a superset of the others: Tika *can* read a PDF,
#: but doing so would silently downgrade every PDF from page-anchored to character-anchored
#: citations the day the sidecar goes down.
_SUPPORTED: frozenset[str] = frozenset({"application/x-ole-storage", "application/rtf"})

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_SPLIT_RE = re.compile(r"</(?:p|h[1-6]|li|tr|div)>", re.IGNORECASE)
_HEADING_RE = re.compile(r"<h([1-6])[^>]*>", re.IGNORECASE)
_LIST_RE = re.compile(r"<li[^>]*>", re.IGNORECASE)


class TikaParser:
    """HTTP client for an Apache Tika ``-full`` server."""

    name = "tika"

    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._version = "unknown"

    @property
    def version(self) -> str:
        return self._version

    def supports(self, mime: str, filename: str, *, needs_ocr: bool) -> bool:
        """OLE2 and RTF only, and never for OCR — Tika's OCR needs Tesseract, not shipped."""
        if needs_ocr:
            return False
        return mime in _SUPPORTED

    def health(self) -> tuple[bool, str]:
        """``GET /version`` — Tika answers with a plain-text version string."""
        try:
            import requests

            response = requests.get(f"{self.base_url}/version", timeout=self.timeout)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return False, f"tika unreachable at {self.base_url}: {exc}"
        self._version = response.text.strip() or "unknown"
        return True, self._version

    def parse(self, source: ParseSource, *, options: ParseOptions) -> ParsedDocument:
        """``PUT /tika`` with ``Accept: text/html``, then flatten the XHTML into blocks."""
        import requests

        if not self.supports(source.mime, source.filename, needs_ocr=False):
            raise DocumentUnsupportedError(f"the tika tier does not handle {source.mime}")

        try:
            response = requests.put(
                f"{self.base_url}/tika",
                data=source.read_bytes(),
                headers={"Accept": "text/html", "Content-Type": source.mime},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise DocumentParserUnavailableError(
                "the legacy-format parser (Tika) is not reachable", detail=str(exc)
            ) from exc

        builder = IRBuilder()
        _xhtml_to_blocks(response.text, builder)
        if len(builder) == 0:
            raise DocumentEmptyError("Tika extracted no text from this document")

        return builder.build(
            parser=self.name,
            parser_version=self.version,
            page_count=0,
            language=options.language,
            has_embedded_text=True,
            ocr_applied=False,
            ocr_pages=0,
            metadata={"mime": source.mime, "tier": "tika"},
            warnings=[
                "parsed by the legacy-format fallback: text and tables are recovered, "
                "but page numbers and layout are not, so citations anchor to character "
                "ranges rather than pages"
            ],
        )


def _xhtml_to_blocks(xhtml: str, builder: IRBuilder) -> None:
    """Flatten Tika's XHTML into the IR. No layout claims, only element semantics."""
    import html as html_module

    body = xhtml.split("<body", 1)[-1]
    for fragment in _BLOCK_SPLIT_RE.split(body):
        heading = _HEADING_RE.search(fragment)
        is_list = bool(_LIST_RE.search(fragment))
        text = html_module.unescape(_TAG_RE.sub(" ", fragment))
        text = re.sub(r"[ \t]+", " ", text).strip()
        if not text:
            continue
        if heading:
            builder.add("heading", text, level=int(heading.group(1)))
        elif is_list:
            builder.add("list_item", text)
        else:
            builder.add("paragraph", text)


def _raise_unparseable(detail: str) -> None:  # pragma: no cover - kept for symmetry
    raise DocumentParseError("Tika could not parse this document", detail=detail)
