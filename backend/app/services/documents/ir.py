"""The document intermediate representation — one canonical string, blocks indexing into it.

Issue #362 / #403 Stage 6a. Every consumer of a parsed document (the chunker, chat
citations, the viewer's highlight anchors, redaction spans, the `char_range` digest
provenance of ``services/ingest_artifacts/provenance.py``) addresses text by
**character offset into one string**. That is the whole reason this module exists:

    ``text[block.char_start:block.char_end] == block.text``

:func:`validate_ir` asserts exactly that, plus ordering and non-overlap, and it is called
at the **producer** — a backend that cannot satisfy it fails the parse rather than
shipping offsets that point at the wrong words. A citation into the wrong passage is the
silent-wrong-answer class this epic keeps finding.

Blocks are built through :class:`IRBuilder` rather than by hand. Hand-computing offsets in
three backends is three chances to be off by the length of a separator, and the invariant
would then only fail for documents nobody tested.
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any

#: Bumped when the IR's *shape* changes in a way that makes a stored artifact
#: un-consumable. Persisted on ``document.parse_version`` and stamped into the artifact
#: key, so a reparse sweep is a version comparison and old artifacts stay readable until
#: collected. Phase 10's document-level facets attach to a stored IR without a reparse
#: precisely because this is versioned and ``char_start``/``char_end`` are in it.
IR_VERSION = 1

#: The block vocabulary. Deliberately closed: the chunker breaks on ``heading`` level, the
#: indexer drops ``page_header``/``page_footer`` from embedded content (running heads are
#: the noise olmOCR-bench's 753 ``absent`` assertions exist to catch), and an unknown type
#: would silently fall through both.
BLOCK_TYPES: frozenset[str] = frozenset(
    {
        "heading",
        "paragraph",
        "list_item",
        "table",
        "caption",
        "code",
        "footnote",
        "page_header",
        "page_footer",
    }
)

#: ``block.source`` values. ``ocr`` is what makes "this passage was guessed by a model"
#: visible to the viewer and to the parse notification.
BLOCK_SOURCES: frozenset[str] = frozenset({"text", "ocr"})

#: What :class:`IRBuilder` writes between two blocks. Two newlines, so the canonical text
#: reads as prose and so a naive whitespace-split never fuses the last word of one block
#: with the first of the next.
BLOCK_SEPARATOR = "\n\n"


class IRValidationError(ValueError):
    """The IR violates an invariant that downstream offsets depend on."""


@dataclass(slots=True)
class Block:
    """One structural unit of a document, addressed by offset into the IR text."""

    type: str
    text: str
    char_start: int
    char_end: int
    #: 1-based, or ``None`` for formats with no pagination (MD, HTML, CSV, XLSX sheets).
    page: int | None = None
    #: Heading depth (1 = top). Only meaningful for ``heading``; the chunker breaks on a
    #: heading whose level is at or above the current section's.
    level: int | None = None
    #: Enclosing heading breadcrumb, outermost first — e.g. ``["3 Methods", "3.1 Data"]``.
    section_path: list[str] = field(default_factory=list)
    source: str = "text"
    #: OCR confidence in [0, 1]; ``None`` for text-layer extraction.
    confidence: float | None = None
    #: ``(x0, y0, x1, y1)`` in the page's coordinate space when the backend knows one.
    bbox: tuple[float, float, float, float] | None = None
    #: Row-major cells for ``table`` blocks. The *text* of a table block is a linearized
    #: rendering; this keeps the grid so the indexer can re-render it per consumer
    #: (``header: cell`` for BM25, Markdown for an LLM) without re-parsing.
    table: list[list[str]] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise, dropping defaults so a 500-page IR is not mostly ``null``."""
        raw = asdict(self)
        return {k: v for k, v in raw.items() if v not in (None, [], "text")}


@dataclass(slots=True)
class ParsedDocument:
    """A parsed document: the canonical text, its blocks, and what produced them."""

    text: str
    blocks: list[Block]
    parser: str
    parser_version: str
    ir_version: int = IR_VERSION
    page_count: int = 0
    language: str | None = None
    #: ``False`` means the source carried no extractable text layer — the signal that
    #: sends a PDF or an image down the OCR path instead of straight to chunking.
    has_embedded_text: bool = True
    ocr_applied: bool = False
    #: How many pages OCR actually produced text for. Compared against ``page_count`` by
    #: the parse notification: fewer means OCR silently degraded, which must be said out
    #: loud rather than shipped as a short document.
    ocr_pages: int = 0
    #: Free-form parser metadata (title, author, producer, sheet names …).
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Human-readable degradations. **Anything that made this parse worse than it should
    #: have been goes here** and is surfaced in the parse notification — a sidecar that
    #: was unreachable, OCR that was disabled, a page cap that truncated, a table the
    #: backend flattened. An empty list is the only "nothing was lost" claim.
    warnings: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict[str, Any]:
        """The on-disk artifact shape (gzipped JSON in object storage)."""
        return {
            "ir_version": self.ir_version,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "page_count": self.page_count,
            "language": self.language,
            "has_embedded_text": self.has_embedded_text,
            "ocr_applied": self.ocr_applied,
            "ocr_pages": self.ocr_pages,
            "metadata": self.metadata,
            "warnings": self.warnings,
            "text": self.text,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ParsedDocument:
        """Rebuild from :meth:`to_dict`. Round-trip is asserted by the IR test suite."""
        blocks = [
            Block(
                type=b["type"],
                text=b["text"],
                char_start=b["char_start"],
                char_end=b["char_end"],
                page=b.get("page"),
                level=b.get("level"),
                section_path=list(b.get("section_path") or []),
                source=b.get("source", "text"),
                confidence=b.get("confidence"),
                bbox=tuple(b["bbox"]) if b.get("bbox") else None,  # type: ignore[arg-type]
                table=b.get("table"),
            )
            for b in payload.get("blocks", [])
        ]
        return cls(
            text=payload["text"],
            blocks=blocks,
            parser=payload["parser"],
            parser_version=payload["parser_version"],
            ir_version=payload.get("ir_version", IR_VERSION),
            page_count=payload.get("page_count", 0),
            language=payload.get("language"),
            has_embedded_text=payload.get("has_embedded_text", True),
            ocr_applied=payload.get("ocr_applied", False),
            ocr_pages=payload.get("ocr_pages", 0),
            metadata=payload.get("metadata") or {},
            warnings=list(payload.get("warnings") or []),
        )


class IRBuilder:
    """Accumulates blocks and the canonical text together, so offsets cannot drift.

    Every backend appends through :meth:`add`; none of them ever computes an offset. The
    builder also maintains the heading breadcrumb, so ``section_path`` is derived from the
    heading stream rather than tracked independently in each backend.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._blocks: list[Block] = []
        self._cursor = 0
        self._headings: list[tuple[int, str]] = []

    def __len__(self) -> int:
        return len(self._blocks)

    def add(
        self,
        block_type: str,
        text: str,
        *,
        page: int | None = None,
        level: int | None = None,
        source: str = "text",
        confidence: float | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        table: list[list[str]] | None = None,
    ) -> Block | None:
        """Append a block. Empty or whitespace-only text is dropped, returning ``None``.

        Dropping is deliberate: a backend that emits an empty paragraph per layout gap
        would otherwise fill the IR with zero-length blocks that chunk into nothing and
        make ``char_start == char_end`` ranges that no highlight can render.
        """
        if block_type not in BLOCK_TYPES:
            raise IRValidationError(f"unknown block type {block_type!r}")
        if source not in BLOCK_SOURCES:
            raise IRValidationError(f"unknown block source {source!r}")
        cleaned = text.strip()
        if not cleaned:
            return None

        if self._parts:
            self._parts.append(BLOCK_SEPARATOR)
            self._cursor += len(BLOCK_SEPARATOR)

        start = self._cursor
        self._parts.append(cleaned)
        self._cursor += len(cleaned)

        if block_type == "heading":
            depth = level or 1
            while self._headings and self._headings[-1][0] >= depth:
                self._headings.pop()
            section_path = [h[1] for h in self._headings]
            self._headings.append((depth, cleaned))
        else:
            section_path = [h[1] for h in self._headings]

        block = Block(
            type=block_type,
            text=cleaned,
            char_start=start,
            char_end=self._cursor,
            page=page,
            level=level,
            section_path=section_path,
            source=source,
            confidence=confidence,
            bbox=bbox,
            table=table,
        )
        self._blocks.append(block)
        return block

    def build(
        self,
        *,
        parser: str,
        parser_version: str,
        page_count: int = 0,
        language: str | None = None,
        has_embedded_text: bool = True,
        ocr_applied: bool = False,
        ocr_pages: int = 0,
        metadata: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> ParsedDocument:
        """Finalise and validate. The only way a :class:`ParsedDocument` should be made."""
        doc = ParsedDocument(
            text="".join(self._parts),
            blocks=self._blocks,
            parser=parser,
            parser_version=parser_version,
            page_count=page_count,
            language=language,
            has_embedded_text=has_embedded_text,
            ocr_applied=ocr_applied,
            ocr_pages=ocr_pages,
            metadata=metadata or {},
            warnings=list(warnings or []),
        )
        validate_ir(doc)
        return doc


def validate_ir(doc: ParsedDocument) -> None:
    """Raise :class:`IRValidationError` unless every offset invariant holds.

    Checks, in the order a violation is most likely:

    1. ``ir_version`` is the one this code understands.
    2. Offsets are in range and ``char_start <= char_end``.
    3. Blocks are ordered by ``char_start`` and do not overlap.
    4. ``text[char_start:char_end] == block.text`` — the one that makes citations,
       highlights and redaction spans share a coordinate system.
    """
    if doc.ir_version != IR_VERSION:
        raise IRValidationError(f"ir_version {doc.ir_version} != {IR_VERSION}")

    n = len(doc.text)
    previous_end = -1
    for i, block in enumerate(doc.blocks):
        if block.type not in BLOCK_TYPES:
            raise IRValidationError(f"block {i}: unknown type {block.type!r}")
        if block.source not in BLOCK_SOURCES:
            raise IRValidationError(f"block {i}: unknown source {block.source!r}")
        if block.char_start < 0 or block.char_end > n:
            raise IRValidationError(
                f"block {i}: [{block.char_start}, {block.char_end}) outside text of length {n}"
            )
        if block.char_start > block.char_end:
            raise IRValidationError(
                f"block {i}: char_start {block.char_start} > char_end {block.char_end}"
            )
        if block.char_start < previous_end:
            raise IRValidationError(
                f"block {i}: starts at {block.char_start}, overlapping the previous block "
                f"which ends at {previous_end}"
            )
        actual = doc.text[block.char_start : block.char_end]
        if actual != block.text:
            raise IRValidationError(
                f"block {i}: text[{block.char_start}:{block.char_end}] is {actual[:40]!r} "
                f"but block.text is {block.text[:40]!r}"
            )
        if block.confidence is not None and not 0.0 <= block.confidence <= 1.0:
            raise IRValidationError(f"block {i}: confidence {block.confidence} outside [0, 1]")
        previous_end = block.char_end

    if doc.ocr_pages > doc.page_count and doc.page_count:
        raise IRValidationError(f"ocr_pages {doc.ocr_pages} exceeds page_count {doc.page_count}")
