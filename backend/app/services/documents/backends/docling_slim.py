"""In-worker parsing tier: Docling's declarative backends plus a pypdfium2 text layer.

This tier runs **inside the Celery worker**, so it must not pull the model stack. Two
measured facts from the Phase-0 bake-off shape it, and both contradict the plan text:

1. **``docling.document_converter.DocumentConverter`` cannot be imported without torch.**
   Its module body imports ``StandardPdfPipeline`` → ``docling_ibm_models`` → ``torch``,
   and before that ``docling_parse``. So the "``docling-slim[format-office]`` parses
   Office formats with no torch at all" claim is true of the *backends* and false of the
   *converter*. This module therefore instantiates the declarative backends directly and
   never imports ``DocumentConverter``. All of ``msword``/``mspowerpoint``/``msexcel``/
   ``md``/``html``/``csv``/``jats`` import cleanly with no torch present.

2. **Docling has no torch-free PDF path.** Both PDF backends are *paginated*, not
   declarative — they have no ``convert()`` and only produce a document through the
   layout pipeline. So text-layer PDF extraction here is ``pypdfium2`` directly
   (BSD-3/Apache-2.0, already a docling dependency). Measured over 4,058 pages of the
   ``pmc-oa``/``cuad``/``contractnli``/``olmocr-bench``/``ucsf-idl`` corpora: **230
   pages/s, zero failures**. Anything needing layout, table structure or OCR is escalated
   to the sidecar — which is exactly the tier split the plan asks for.

Upstream traps pinned here:

* **``docling#3335`` — DOCX chunkers return 0 chunks when the document contains tables.**
  That is a *chunker* defect; this module does not use Docling's chunkers, it walks the
  document tree. ``tests/unit/test_document_parser_backends.py`` asserts a DOCX with a
  table yields both table and non-table blocks, so a future switch to ``HybridChunker``
  cannot reintroduce it silently.
* **A DOCX containing equations raises ``NameError``, not ``ImportError``.** Docling's
  Word backend references ``UnicodeToLatexEncoder`` unguarded when ``pylatexenc`` is
  absent. 5 of the 32 ``docling-fixtures`` DOCX files hit it. ``pylatexenc`` is therefore
  a required dependency of this tier, and :func:`_convert_declarative` translates a bare
  ``NameError`` into a typed parser error so it can never read as a code bug in our tree.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

from ..ir import IRBuilder
from ..ir import ParsedDocument
from ..safety import prescan
from ..types import DocumentEmptyError
from ..types import DocumentParseError
from ..types import DocumentParserUnavailableError
from ..types import DocumentUnsupportedError
from ..types import ParseOptions
from ..types import ParseSource

logger = logging.getLogger(__name__)

#: mime → (``InputFormat`` attribute, backend module, backend class). Resolved lazily so
#: importing this module on a worker without the extras installed still works — the
#: registry needs :meth:`DoclingSlimParser.health` to answer, not to raise.
_DECLARATIVE: dict[str, tuple[str, str, str]] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        "DOCX",
        "docling.backend.msword_backend",
        "MsWordDocumentBackend",
    ),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        "PPTX",
        "docling.backend.mspowerpoint_backend",
        "MsPowerpointDocumentBackend",
    ),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        "XLSX",
        "docling.backend.msexcel_backend",
        "MsExcelDocumentBackend",
    ),
    "text/markdown": ("MD", "docling.backend.md_backend", "MarkdownDocumentBackend"),
    "text/html": ("HTML", "docling.backend.html_backend", "HTMLDocumentBackend"),
    "text/csv": ("CSV", "docling.backend.csv_backend", "CsvDocumentBackend"),
}

#: Handled here without Docling: plain text needs no parser, and PDFs take the pypdfium2
#: text-layer path.
_NATIVE: frozenset[str] = frozenset({"text/plain", "text/tab-separated-values", "application/pdf"})

#: Docling label → our block type. Anything unmapped becomes ``paragraph``; that default
#: is safe (it is indexed and chunked normally) whereas guessing ``page_header`` would
#: silently drop text from the embedded content.
_LABEL_TO_BLOCK: dict[str, str] = {
    "title": "heading",
    "section_header": "heading",
    "list_item": "list_item",
    "code": "code",
    "caption": "caption",
    "footnote": "footnote",
    "page_header": "page_header",
    "page_footer": "page_footer",
    "table": "table",
    "formula": "paragraph",
    "text": "paragraph",
    "paragraph": "paragraph",
    "checkbox_selected": "list_item",
    "checkbox_unselected": "list_item",
}


def _docling_version() -> str:
    try:
        from importlib.metadata import version

        return version("docling-slim")
    except Exception:  # noqa: BLE001
        try:
            from importlib.metadata import version

            return version("docling")
        except Exception:  # noqa: BLE001
            return "unknown"


def linearize_table(grid: list[list[str]]) -> str:
    """Render a table as ``header: cell`` pairs, one row per line.

    Chosen over Markdown for the *indexed* text on the measured grounds in the plan: it
    puts header terms adjacent to their values, which is what BM25 recall on a numeric
    query needs, where a Markdown pipe table separates them by the whole row. The grid
    survives on ``Block.table`` so an LLM-facing consumer can still render Markdown —
    one parse, two renderings, no reparse.
    """
    if not grid:
        return ""
    header = [c.strip() for c in grid[0]]
    if len(grid) == 1:
        return " | ".join(c for c in header if c)
    lines = []
    for row in grid[1:]:
        pairs = [
            f"{header[i]}: {cell.strip()}" if i < len(header) and header[i] else cell.strip()
            for i, cell in enumerate(row)
            if cell and cell.strip()
        ]
        if pairs:
            lines.append(" | ".join(pairs))
    return "\n".join(lines)


def _table_grid(item: Any) -> list[list[str]]:
    """Extract row-major cell text from a Docling ``TableItem``, defensively."""
    data = getattr(item, "data", None)
    if data is None:
        return []
    grid = getattr(data, "grid", None)
    if not grid:
        return []
    out: list[list[str]] = []
    for row in grid:
        out.append([str(getattr(cell, "text", "") or "") for cell in row])
    return out


def _page_of(item: Any, doc: Any = None) -> int | None:
    """Page number for an item, resolved through its ancestors when it has no provenance.

    An OCR text cell nested under a ``PictureItem`` carries no ``prov`` of its own — only
    the picture does. Without the walk up, every OCR'd block on a scanned page gets
    ``page=None``, and a page-anchored citation into a scan silently degrades to a
    file-level one.
    """
    seen = 0
    node = item
    while node is not None and seen < 8:
        prov = getattr(node, "prov", None)
        if prov:
            page_no = getattr(prov[0], "page_no", None)
            if page_no is not None:
                return int(page_no)
        parent_ref = getattr(node, "parent", None)
        if parent_ref is None or doc is None:
            return None
        try:
            node = parent_ref.resolve(doc)
        except Exception:  # noqa: BLE001 - a broken ref means "no page", not a crash
            return None
        seen += 1
    return None


def _blocks_from_docling(doc: Any, builder: IRBuilder) -> set[int]:
    """Walk a ``DoclingDocument`` tree into the IR builder, in reading order.

    ``traverse_pictures=True`` is **not optional**, and this is the single most important
    line in the sidecar path. Measured against a live ``docling-serve`` 1.30.0 on
    ``olmocr-bench/old_scans/1.pdf``: the layout model classifies a full-page scan as one
    ``PictureItem`` and RapidOCR attaches all 29 recognised text cells as *children of
    that picture*. With the default walk, ``json_content`` carries 30 texts, the Markdown
    export is the literal string ``<!-- image -->``, and the document indexes as **empty**
    — a scanned page that OCR'd perfectly, reported success, and produced nothing. That is
    the silent-OCR-degradation failure this feature exists to prevent, and it is invisible
    unless you look at the item tree.

    ``included_content_layers`` likewise takes ``furniture`` as well as ``body``: running
    heads and page numbers are classified into the furniture layer and we *want* them, as
    typed ``page_header``/``page_footer`` blocks the indexer can then exclude deliberately
    rather than lose accidentally.

    Returns:
        The set of 1-based page numbers that produced at least one non-empty block. The
        caller compares it against the page count to detect OCR that ran and yielded
        nothing — see ``docling_serve._ocr_coverage``.
    """
    from docling_core.types.doc.common.content_layer import ContentLayer

    layers = {ContentLayer.BODY, ContentLayer.FURNITURE}
    pages_with_text: set[int] = set()
    for item, _level in doc.iterate_items(traverse_pictures=True, included_content_layers=layers):
        label = str(getattr(item, "label", "") or "")
        if "." in label:  # DocItemLabel enum reprs stringify as the value already
            label = label.rsplit(".", 1)[-1]
        block_type = _LABEL_TO_BLOCK.get(label, "paragraph")

        page = _page_of(item, doc)

        if block_type == "table":
            grid = _table_grid(item)
            text = linearize_table(grid)
            if builder.add("table", text, page=page, table=grid or None) and page:
                pages_with_text.add(page)
            continue

        text = getattr(item, "text", "") or ""
        if not text.strip():
            continue

        level: int | None = None
        if block_type == "heading":
            level = 1 if label == "title" else int(getattr(item, "level", 2) or 2) + 1

        if builder.add(block_type, text, page=page, level=level) and page:
            pages_with_text.add(page)

    return pages_with_text


def _convert_declarative(source: ParseSource, mime: str, builder: IRBuilder) -> Any:
    """Instantiate the matching declarative backend and convert. Never uses the pipeline."""
    import importlib

    fmt_name, module_path, class_name = _DECLARATIVE[mime]
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.document import InputDocument

        backend_cls = getattr(importlib.import_module(module_path), class_name)
    except ImportError as exc:
        raise DocumentParserUnavailableError(
            f"the docling slim tier is missing the extra for {mime}", detail=str(exc)
        ) from exc

    data = source.read_bytes()
    in_doc = InputDocument(
        path_or_stream=BytesIO(data),
        format=getattr(InputFormat, fmt_name),
        backend=backend_cls,
        filename=source.filename,
    )

    # ⚠️ `InputDocument.__init__` SWALLOWS a backend construction failure: it logs, sets
    # `valid = False`, and never assigns `_backend` at all. Reading the attribute then
    # raises `AttributeError`, which is untyped and reads as a bug in our tree. Found by
    # the govdocs1 robustness suite — two of 589 uncurated files hit it (CSVs whose
    # dialect sniffing throws inside the constructor).
    backend = getattr(in_doc, "_backend", None)  # noqa: SLF001 - docling's only seam
    if backend is None or not getattr(in_doc, "valid", True):
        raise DocumentParseError(
            f"this {fmt_name} could not be opened — the file is malformed or truncated"
        )

    try:
        if not backend.is_valid():
            raise DocumentParseError(f"docling rejected this {fmt_name} as unreadable")
        try:
            doc = backend.convert()  # type: ignore[attr-defined]
        except NameError as exc:
            # docling#pylatexenc: the Word backend references UnicodeToLatexEncoder
            # unguarded. A bare NameError here would read as a bug in OUR tree.
            raise DocumentParserUnavailableError(
                "the docling slim tier is missing an optional dependency this document "
                "needs (equations require pylatexenc)",
                detail=str(exc),
            ) from exc
        except DocumentParseError:
            raise
        except Exception as exc:
            # Everything a third-party reader can throw on malformed input — `csv.Error`,
            # `struct.error`, `KeyError` from a missing OOXML part. Untyped here means
            # `processing_error` with no guidance for the user, so it is translated once,
            # at the boundary, rather than in each caller.
            raise DocumentParseError(
                f"this {fmt_name} could not be parsed",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
    finally:
        backend.unload()

    _blocks_from_docling(doc, builder)
    return doc


def _convert_pdf_text_layer(
    source: ParseSource, options: ParseOptions, builder: IRBuilder
) -> tuple[int, bool, list[str]]:
    """Extract the PDF's embedded text layer with pypdfium2. No layout model, no OCR.

    Returns:
        ``(page_count, has_embedded_text, warnings)``. ``has_embedded_text=False`` is the
        signal that sends the document to the OCR path; it is **not** a failure.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - pypdfium2 ships with the slim tier
        raise DocumentParserUnavailableError("pypdfium2 is not installed") from exc

    warnings: list[str] = []
    data = source.read_bytes()
    pdf = pdfium.PdfDocument(BytesIO(data))
    try:
        total = len(pdf)
        first, last = options.page_range or (1, total)
        last = min(last, total, options.max_pages)
        if total > options.max_pages:
            warnings.append(
                f"only the first {options.max_pages} of {total} pages were parsed (page ceiling)"
            )

        extracted = 0
        for page_no in range(first, last + 1):
            page = pdf[page_no - 1]
            textpage = page.get_textpage()
            try:
                raw = textpage.get_text_bounded() or ""
            finally:
                textpage.close()
            extracted += len(raw)
            for para in _split_paragraphs(raw):
                builder.add("paragraph", para, page=page_no)

        pages_read = max(last - first + 1, 1)
        has_text = (extracted / pages_read) >= options.ocr_text_threshold
        return total, has_text, warnings
    finally:
        pdf.close()


def _split_paragraphs(raw: str) -> list[str]:
    """Split a page's raw text run into paragraph-ish blocks on blank lines.

    Crude by design. The slim tier makes no layout claim — that is what the sidecar is
    for — and a blank-line split is the one heuristic that never *invents* structure.
    """
    parts = [p.strip() for p in raw.replace("\r\n", "\n").split("\n\n")]
    return [p for p in parts if p]


class DoclingSlimParser:
    """The in-worker tier. Text-layer only: no layout model, no table structure, no OCR."""

    name = "docling.slim"

    def __init__(self) -> None:
        self._version: str | None = None

    @property
    def version(self) -> str:
        if self._version is None:
            self._version = _docling_version()
        return self._version

    def supports(self, mime: str, filename: str, *, needs_ocr: bool) -> bool:
        """No OCR here, ever. ``needs_ocr`` therefore excludes this tier outright."""
        if needs_ocr:
            return False
        return mime in _DECLARATIVE or mime in _NATIVE

    def health(self) -> tuple[bool, str]:
        """Importable? Reports which capability is missing rather than a bare False."""
        try:
            import docling.backend.msword_backend  # noqa: F401
        except ImportError as exc:
            return False, f"docling backends unavailable: {exc}"
        try:
            import pypdfium2  # noqa: F401
        except ImportError:
            return True, "available, but PDF text-layer extraction is off (no pypdfium2)"
        return True, f"docling-slim {self.version}"

    def parse(self, source: ParseSource, *, options: ParseOptions) -> ParsedDocument:
        """Parse *source*. Raises a typed ``DocumentParseError`` on every failure path."""
        mime = source.mime
        if not self.supports(mime, source.filename, needs_ocr=False):
            raise DocumentUnsupportedError(f"the slim tier cannot parse {mime}")

        data = source.read_bytes()
        prescan(mime, data, max_pages=options.max_pages)

        builder = IRBuilder()
        warnings: list[str] = []
        page_count = 0
        has_text = True
        language = options.language

        if mime == "application/pdf":
            page_count, has_text, warnings = _convert_pdf_text_layer(source, options, builder)
            if not has_text:
                warnings.append("no usable text layer — this document needs OCR to be searchable")
        elif mime in ("text/plain", "text/tab-separated-values"):
            text = _decode_text(data)
            for para in _split_paragraphs(text):
                builder.add("paragraph", para)
        else:
            doc = _convert_declarative(source, mime, builder)
            pages = getattr(doc, "pages", None)
            page_count = len(pages) if pages else 0
            origin = getattr(doc, "origin", None)
            if origin is not None and getattr(origin, "filename", None):
                pass  # origin carries no language; kept for the metadata block below

        if len(builder) == 0 and has_text:
            raise DocumentEmptyError("the parser produced no text from this document")

        return builder.build(
            parser=self.name,
            parser_version=self.version,
            page_count=page_count,
            language=language,
            has_embedded_text=has_text,
            ocr_applied=False,
            ocr_pages=0,
            metadata={"mime": mime, "tier": "slim"},
            warnings=warnings,
        )


def _decode_text(data: bytes) -> str:
    """Decode a text file, honouring a BOM. UTF-16 fixtures exist in the corpora."""
    for bom, encoding in (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if data.startswith(bom):
            return data.decode(encoding, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")
