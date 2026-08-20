"""Re-mask a document digest sentence through its ``char_range`` provenance.

The document analog of ``chat/redactor.py``'s ``_gather_sentence_segments`` +
``_mask_from_spans`` for transcripts. That module fails closed on a document sentence
today — ``_gather_sentence_segments`` explicitly returns ``[]`` (withholding the
sentence) whenever a provenance's ``kind`` is not ``segment_ids``, so a ``char_range``
sentence reaching it is *correctly* refused rather than mis-masked. This module is the
mechanism a future ``chat/redactor.py`` change needs to stop refusing it — not wired in
here, because ``redactor.py`` is outside this lane's file set.

Same unit-of-masking choice the transcript path makes: a digest sentence's maskable unit
is not a sub-slice of its own char range, but every ``document_chunk`` row the range
overlaps, masked **whole** from that chunk's own cached ``redactions`` spans and joined
— mirroring how ``_mask_from_spans`` masks a sentence's whole ``TranscriptSegment`` rows
rather than slicing to the sentence's exact span within them. A document_chunk row is
already the document plane's retrieval unit (``chat/redactor.py``'s own
``_gather_document_chunk_spans`` docstring: "a document_chunk row already IS the
retrieval unit indexed into OpenSearch — there is no rebuild-from-several-rows step"),
so masking it whole here keeps the same "whole retrieval unit, joined" contract chat
already applies to the chunk plane, rather than inventing a third one for the digest
plane.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .provenance import KIND_CHAR_RANGE


@dataclass(frozen=True)
class ChunkSpans:
    """One ``document_chunk`` row's text and cached detection spans, as plain data.

    Deliberately not the ORM row: the first attribute read on a detached
    ``DocumentChunk`` would re-open a session, the same reason
    ``chat/redactor._SegmentSpans`` exists for transcript segments.
    """

    text: str
    char_start: int
    char_end: int
    redactions: list[Any]


def chunks_for_char_range(
    chunks: list[ChunkSpans], char_start: int, char_end: int
) -> list[ChunkSpans]:
    """Chunks whose ``[char_start, char_end)`` overlaps the given range.

    Mirrors ``chat/redactor._gather_chunk_segments``'s time-range overlap query
    (``end_time >= chunk.start_time and start_time <= chunk.end_time``) in the
    document plane's own coordinate space.
    """
    return [c for c in chunks if c.char_end >= char_start and c.char_start <= char_end]


def mask_char_range_provenance(
    chunks: list[ChunkSpans],
    provenance: Mapping[str, Any],
    cfg: Any,
) -> str:
    """Mask the text one ``char_range``-kinded sentence provenance names.

    Args:
        chunks: Every ``document_chunk`` row of the owning document (or at least
            every one plausibly overlapping *provenance* — the caller may narrow this
            with :func:`chunks_for_char_range` itself, or pass the full set and let
            this function do it).
        provenance: One digest sentence's stored provenance dict.
        cfg: An ``EffectiveRedactionConfig`` (``app.services.redaction.config``).

    Returns:
        The masked text of every overlapping chunk, joined with spaces — or ``""``
        when the provenance is not ``char_range``-kinded (never guesses at a kind it
        does not understand, the same rule
        ``chat/redactor._gather_sentence_segments`` applies to a ``segment_ids``
        reader handed this shape) or when nothing overlaps.
    """
    from app.services.redaction.service import RedactionService

    if provenance.get("kind") != KIND_CHAR_RANGE:
        return ""
    char_start = int(provenance.get("char_start", 0))
    char_end = int(provenance.get("char_end", 0))
    overlapping = chunks_for_char_range(chunks, char_start, char_end)
    if not overlapping:
        return ""

    parts: list[str] = []
    for chunk in overlapping:
        if not chunk.text:
            continue
        masked, _applied = RedactionService.mask_segment(
            chunk.text, chunk.redactions, None, cfg, set()
        )
        parts.append(masked)
    return " ".join(parts).strip()
