"""Long-tail tier — Apache Tika, for the formats Docling has no reader for.

Scope is deliberately narrow: **legacy OLE2 (``.doc``/``.xls``/``.ppt``) and RTF**.
Docling reads neither, and the AMI shared-doc corpus alone is 987 ``.ppt`` + 842 ``.doc``
+ 87 ``.xls`` — i.e. the single largest real-world office corpus available to this project
is entirely OLE2. Tika is Apache-2.0 and runs as its own JVM container.

It has **no layout model**, so it is strictly a fallback: Tika's XHTML output gives
paragraphs, headings and tables but no page numbers, no bounding boxes and no reading-order
recovery. A document parsed here is searchable and citable by character range; it is not
citable by page. That is a real capability difference and :attr:`TikaParser.supports` is
what keeps it from quietly serving PDFs that the sidecar would parse better.

Optional by design: when the container is absent the registry never selects this tier and
the upload path returns "convert to .docx or .pdf first", which is a better answer than a
worse parse.

Four things this module gets right that the obvious implementation does not, each of which
was a measured failure against a live Tika 3.3.1 and the AMI corpus:

1. **Never send our internal mime as the request ``Content-Type``.** Tika treats a supplied
   content type as a detection *override*. ``application/x-ole-storage`` is a container
   type Tika has no parser for, so it selects ``EmptyParser`` and answers **HTTP 200 with
   an empty body and no error** — a silent 100% data loss on every ``.doc``/``.ppt``/
   ``.xls``. The bytes are sent untyped; Tika's own sniffing resolves ``application/msword``
   and friends, and the filename travels as a hint in ``Content-Disposition``.
2. **``/rmeta/html``, not ``/tika``.** The plain endpoint discards the metadata, which is
   the only place Tika reports *which parser ran*. Without it there is no way to tell "this
   document is empty" from "I had no parser for it" — see :data:`_EMPTY_PARSER`.
3. **Embedded documents carry real text.** ``/rmeta`` returns one entry per embedded object;
   measured over a 60-file OLE2 sample, 26 embedded entries held >200 characters and **none
   of it was duplicated in the parent entry**. Dropping entries after the first would lose
   it, so all entries are concatenated.
4. **A bad HTTP status is not the tier being down.** Mapping every ``RequestException`` to
   :class:`DocumentParserUnavailableError` makes an unparseable document *retryable*, so it
   cycles through the retry sweep forever. Only transport failures and 5xx are the tier's
   fault; a 4xx is this document's.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from typing import Any

from ..ir import IRBuilder
from ..ir import ParsedDocument
from ..types import DocumentEmptyError
from ..types import DocumentEncryptedError
from ..types import DocumentParseError
from ..types import DocumentParserUnavailableError
from ..types import DocumentUnsupportedError
from ..types import ParseOptions
from ..types import ParseSource
from .docling_slim import linearize_table

logger = logging.getLogger(__name__)

#: **Routing set** — what the registry is allowed to send here. Not a superset of the other
#: tiers: Tika *can* read a PDF, but doing so would silently downgrade every PDF from
#: page-anchored to character-anchored citations the day the sidecar goes down.
_ROUTED: frozenset[str] = frozenset({"application/x-ole-storage", "application/rtf"})

#: **Cross-check set** — formats the Docling tiers own, which :meth:`TikaParser.parse` will
#: still accept from a caller that asks for them explicitly. The registry never routes
#: these here. They exist because a second, independently implemented extractor is a
#: differential oracle: on a format both tools read, disagreement flags a parser bug that
#: neither tool reports about itself.
_CROSS_CHECK: frozenset[str] = frozenset(
    {"text/html", "text/plain", "text/markdown", "text/csv", "text/tab-separated-values"}
)

_PARSEABLE: frozenset[str] = _ROUTED | _CROSS_CHECK

#: Tika's null parser. Selected when no registered parser claims the resolved type — the
#: server saying "I cannot read this", which is a *typed rejection*, not an empty document.
_EMPTY_PARSER = "org.apache.tika.parser.EmptyParser"

#: Metadata keys prefixed with this carry a stack trace from a parse that partly failed.
_EXCEPTION_PREFIX = "X-TIKA:EXCEPTION"

#: Substrings in a Tika exception that mean "password-protected", not "broken".
_ENCRYPTED_MARKERS = ("EncryptedDocument", "password", "Password")

_HEAD_RE = re.compile(r"<head\b.*?</head>", re.IGNORECASE | re.DOTALL)
_BODY_RE = re.compile(r"<body\b[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)
_EMPTY_BODY_RE = re.compile(r"<body\b[^>]*/>", re.IGNORECASE)
_TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_BLOCK_SPLIT_RE = re.compile(r"</(?:p|h[1-6]|li|div|br)>|<br\s*/?>", re.IGNORECASE)
_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>", re.IGNORECASE)
_LIST_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"</?[^<>]*>")
_NON_ASCII_RE = re.compile(r"[^\x20-\x7e]")


class TikaParser:
    """HTTP client for an Apache Tika ``-full`` server."""

    name = "tika"

    def __init__(
        self, base_url: str, *, timeout: float = 120.0, health_timeout: float = 5.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        #: A health probe must be cheap to *fail*. Sharing the parse timeout meant a dead
        #: container cost two minutes per registry resolution before falling through.
        self.health_timeout = health_timeout
        self._version = "unknown"

    @property
    def version(self) -> str:
        return self._version

    def supports(self, mime: str, filename: str, *, needs_ocr: bool) -> bool:
        """OLE2 and RTF only, and never for OCR — Tika's OCR needs Tesseract, not shipped."""
        if needs_ocr:
            return False
        return mime in _ROUTED

    def health(self) -> tuple[bool, str]:
        """``GET /version`` — Tika answers with a plain-text version string."""
        try:
            import requests

            response = requests.get(f"{self.base_url}/version", timeout=self.health_timeout)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return False, f"tika unreachable at {self.base_url}: {exc}"
        self._version = response.text.strip() or "unknown"
        return True, self._version

    def parse(self, source: ParseSource, *, options: ParseOptions) -> ParsedDocument:
        """``PUT /rmeta/html``, then flatten each entry's XHTML into blocks."""
        if source.mime not in _PARSEABLE:
            raise DocumentUnsupportedError(f"the tika tier does not handle {source.mime}")

        entries = self._request(source)
        if not entries:
            raise DocumentParseError("Tika returned no metadata entries for this document")

        container = entries[0]
        _raise_for_tika_exception(container)
        _raise_for_empty_parser(container, source.mime)

        builder = IRBuilder()
        for entry in entries:
            _xhtml_to_blocks(str(entry.get("X-TIKA:content") or ""), builder)

        if len(builder) == 0:
            raise DocumentEmptyError("Tika extracted no text from this document")

        warnings = [
            "parsed by the legacy-format fallback: text and tables are recovered, "
            "but page numbers and layout are not, so citations anchor to character "
            "ranges rather than pages"
        ]
        embedded_failures = sum(1 for entry in entries[1:] if _tika_exception(entry))
        if embedded_failures:
            warnings.append(
                f"{embedded_failures} embedded object(s) in this document could not be "
                f"read; their text is missing from the extraction"
            )

        return builder.build(
            parser=self.name,
            parser_version=self.version,
            page_count=_page_count(container),
            language=_metadata_str(container, "dc:language") or options.language,
            has_embedded_text=True,
            ocr_applied=False,
            ocr_pages=0,
            metadata={
                "mime": _metadata_str(container, "Content-Type") or source.mime,
                "declared_mime": source.mime,
                "tier": "tika",
                "title": _metadata_str(container, "dc:title"),
                "author": _metadata_str(container, "dc:creator"),
                "embedded_entries": len(entries) - 1,
            },
            warnings=warnings,
        )

    def _request(self, source: ParseSource) -> list[dict[str, Any]]:
        """PUT the bytes untyped and return the ``/rmeta`` entry list.

        The absence of a ``Content-Type`` header is the whole point — see the module
        docstring. ``Content-Disposition`` gives Tika the filename as a *hint*, which its
        detector weighs below the magic bytes, so a mislabelled file is still routed by
        content (measured: one ``.doc`` in the AMI sample is really ``message/rfc822`` and
        Tika reads it as mail).
        """
        import requests

        safe_name = _NON_ASCII_RE.sub("_", source.filename).replace('"', "'")
        try:
            response = requests.put(
                f"{self.base_url}/rmeta/html",
                data=source.read_bytes(),
                headers={
                    "Accept": "application/json",
                    "Content-Disposition": f'attachment; filename="{safe_name}"',
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DocumentParserUnavailableError(
                "the legacy-format parser (Tika) is not reachable", detail=str(exc)
            ) from exc

        if response.status_code >= 500:
            raise DocumentParserUnavailableError(
                "the legacy-format parser (Tika) returned a server error",
                detail=f"HTTP {response.status_code}",
            )
        if response.status_code == 415:
            raise DocumentUnsupportedError(
                "Tika has no parser for this format", detail=f"HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise DocumentParseError(
                "Tika rejected this document", detail=f"HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise DocumentParseError(
                "Tika returned a body that is not JSON", detail=str(exc)[:200]
            ) from exc
        if not isinstance(payload, list):
            raise DocumentParseError(f"Tika returned {type(payload).__name__}, expected a list")
        return [entry for entry in payload if isinstance(entry, dict)]


def _metadata_str(entry: dict[str, Any], key: str) -> str | None:
    """Read one metadata value. Tika returns either a scalar or a list per key."""
    value = entry.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _page_count(entry: dict[str, Any]) -> int:
    for key in ("xmpTPg:NPages", "meta:page-count", "meta:slide-count"):
        raw = _metadata_str(entry, key)
        if raw:
            try:
                return max(0, int(raw))
            except ValueError:
                continue
    return 0


def _tika_exception(entry: dict[str, Any]) -> str | None:
    for key, value in entry.items():
        if key.startswith(_EXCEPTION_PREFIX):
            return str(value)[:500]
    return None


def _raise_for_tika_exception(entry: dict[str, Any]) -> None:
    """A container-level exception is a parse failure, and an encrypted one is its own kind.

    Password-protected documents are refused rather than prompted for (``safety.py``), and
    the distinction matters to the user: "convert this" and "unlock this" are different
    instructions.
    """
    detail = _tika_exception(entry)
    if detail is None:
        return
    if any(marker in detail for marker in _ENCRYPTED_MARKERS):
        raise DocumentEncryptedError(
            "this document is password-protected and cannot be read", detail=detail
        )
    raise DocumentParseError("Tika could not parse this document", detail=detail)


def _raise_for_empty_parser(entry: dict[str, Any], declared_mime: str) -> None:
    """``EmptyParser`` means Tika had no reader — a rejection, not an empty document."""
    parsers = entry.get("X-TIKA:Parsed-By") or []
    if isinstance(parsers, str):
        parsers = [parsers]
    if _EMPTY_PARSER in parsers:
        resolved = _metadata_str(entry, "Content-Type") or declared_mime
        raise DocumentUnsupportedError(
            f"Tika has no parser for {resolved}", detail="X-TIKA:Parsed-By is EmptyParser"
        )


def _body_of(xhtml: str) -> str:
    """The document body, or ``""`` for a self-closing ``<body/>``.

    Splitting on the literal string ``"<body"`` — the obvious implementation — leaves the
    ``"/>"`` of a self-closing body in the output, which ``_TAG_RE`` cannot match. Those
    two junk characters are enough to make a *completely empty* extraction look like a
    successful one and defeat the ``len(builder) == 0`` check below.
    """
    match = _BODY_RE.search(xhtml)
    if match:
        return match.group(1)
    if _EMPTY_BODY_RE.search(xhtml):
        return ""
    return _HEAD_RE.sub("", xhtml)


def _table_grid(fragment: str) -> list[list[str]]:
    grid: list[list[str]] = []
    for row in _ROW_RE.finditer(fragment):
        cells = [_plain_text(cell) for cell in _CELL_RE.findall(row.group(0))]
        if any(cells):
            grid.append(cells)
    return grid


def _plain_text(fragment: str) -> str:
    return re.sub(r"[ \t]+", " ", html_module.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _flush_prose(fragment: str, builder: IRBuilder) -> None:
    for part in _BLOCK_SPLIT_RE.split(fragment):
        heading = _HEADING_RE.search(part)
        is_list = bool(_LIST_RE.search(part))
        text = _plain_text(part)
        if not text:
            continue
        if heading:
            builder.add("heading", text, level=int(heading.group(1)))
        elif is_list:
            builder.add("list_item", text)
        else:
            builder.add("paragraph", text)


def _xhtml_to_blocks(xhtml: str, builder: IRBuilder) -> None:
    """Flatten Tika's XHTML into the IR. No layout claims, only element semantics.

    Tables are lifted out first and kept as ``table`` blocks with their grid, because
    ``.xls`` is *nothing but* tables — flattening those rows into paragraphs would strip
    the header/value adjacency that :func:`linearize_table` exists to preserve.
    """
    body = _body_of(xhtml)
    cursor = 0
    for match in _TABLE_RE.finditer(body):
        _flush_prose(body[cursor : match.start()], builder)
        grid = _table_grid(match.group(0))
        if grid:
            builder.add("table", linearize_table(grid), table=grid)
        cursor = match.end()
    _flush_prose(body[cursor:], builder)
