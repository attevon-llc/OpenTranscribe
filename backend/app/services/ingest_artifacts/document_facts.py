"""``file_facts.facts`` for the document plane — the #362 / #403 Stage 6 analog of
``facts.py``.

A document has no speakers and no turns, so this is deliberately a much smaller payload
than the transcript ``facts`` shape: parser provenance and extraction quality (word/page/
chunk counts, OCR involvement, warning count) rather than speaker statistics. Both shapes
are read the same way downstream — as ``file_facts.facts``, a plain JSONB dict — so there
is no need for them to agree field-for-field, only for each to be internally honest about
what it is describing.
"""

from __future__ import annotations

from typing import Any

#: Bumped when this payload's shape changes in a way that makes a stored row
#: non-comparable — same rollout mechanism ``facts.FACTS_SCHEMA_VERSION`` uses.
DOCUMENT_FACTS_SCHEMA_VERSION = 1


def build_document_facts(
    *,
    word_count: int,
    chunk_count: int,
    page_count: int | None,
    language: str | None,
    parser: str | None,
    has_embedded_text: bool | None,
    ocr_applied: bool,
    ocr_pages: int,
    warning_count: int,
) -> dict[str, Any]:
    """Assemble the ``facts`` payload for one document.

    Args:
        word_count: Total words across the document's ``document_chunk`` rows.
        chunk_count: Number of chunk rows.
        page_count: ``Document.page_count``, when the parser knows one.
        language: Resolved document language — detected, or the parser's own hint, or
            ``None`` when neither could tell. Never a coerced ``"en"``.
        parser: Which tier parsed it (``Document.parser``).
        has_embedded_text: Whether the source carried a usable text layer.
        ocr_applied: Whether OCR ran.
        ocr_pages: How many pages OCR actually produced text for.
        warning_count: ``len(Document.parse_warnings)`` — a count, not the warnings
            themselves; the warnings are already surfaced via the parse notification and
            this JSONB is not the place to duplicate free text.

    Returns:
        The ``file_facts.facts`` JSONB payload for a document-owned row.
    """
    return {
        "schema_version": DOCUMENT_FACTS_SCHEMA_VERSION,
        "word_count": int(word_count),
        "chunk_count": int(chunk_count),
        "page_count": int(page_count) if page_count is not None else None,
        "language": language,
        "parser": parser,
        "has_embedded_text": has_embedded_text,
        "ocr_applied": bool(ocr_applied),
        "ocr_pages": int(ocr_pages),
        "warning_count": int(warning_count),
    }
