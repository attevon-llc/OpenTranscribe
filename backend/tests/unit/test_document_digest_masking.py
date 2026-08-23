"""``ingest_artifacts/document_digest_masking.py`` — char_range provenance masking.

The document analog of ``chat/redactor.py``'s ``_gather_sentence_segments`` +
``_mask_from_spans`` for transcripts, exercised in isolation because ``redactor.py``
itself is outside this lane's file set. Masks whole overlapping ``document_chunk`` rows
from their own cached spans, mirroring the transcript path's "mask the whole retrieval
unit, join" contract.
"""

from __future__ import annotations

from app.services.ingest_artifacts.document_digest_masking import ChunkSpans
from app.services.ingest_artifacts.document_digest_masking import chunks_for_char_range
from app.services.ingest_artifacts.document_digest_masking import mask_char_range_provenance
from app.services.ingest_artifacts.provenance import char_range_provenance
from app.services.redaction.config import EffectiveRedactionConfig


def _cfg(*, enabled: bool = True, categories: set[str] | None = None) -> EffectiveRedactionConfig:
    return EffectiveRedactionConfig(
        enabled=enabled,
        enabled_categories=categories or {"pii"},
        # `mask_segment` filters cached PII spans by the entity types the user/admin
        # actually wants masked (`app/services/redaction/service.py`) — an empty
        # `pii_entities` (the dataclass default) drops every PII span before it ever
        # reaches `apply_redactions`, which looks exactly like "masking did nothing"
        # rather than "this entity type isn't enabled".
        pii_entities={"PERSON"},
        style="label",
    )


def test_chunks_for_char_range_selects_only_overlapping_chunks():
    chunks = [
        ChunkSpans(text="alpha beta gamma", char_start=0, char_end=17, redactions=[]),
        ChunkSpans(text="delta epsilon", char_start=20, char_end=33, redactions=[]),
        ChunkSpans(text="zeta eta theta", char_start=40, char_end=54, redactions=[]),
    ]
    overlapping = chunks_for_char_range(chunks, 18, 25)
    assert overlapping == [chunks[1]]

    # A range spanning two chunks selects both.
    overlapping = chunks_for_char_range(chunks, 10, 22)
    assert overlapping == [chunks[0], chunks[1]]

    # No overlap at all.
    assert chunks_for_char_range(chunks, 100, 110) == []


def test_mask_char_range_provenance_declines_a_non_char_range_provenance():
    chunks = [ChunkSpans(text="John Smith called today.", char_start=0, char_end=25, redactions=[])]
    segment_provenance = {
        "kind": "segment_ids",
        "segment_ids": [1],
        "start_time": 0.0,
        "end_time": 1.0,
    }
    assert mask_char_range_provenance(chunks, segment_provenance, _cfg()) == ""


def test_mask_char_range_provenance_masks_the_whole_overlapping_chunk():
    text = "Contact John Smith for details."
    span_start = text.index("John Smith")
    span_end = span_start + len("John Smith")
    chunk = ChunkSpans(
        text=text,
        char_start=100,
        char_end=100 + len(text),
        redactions=[
            {
                "category": "pii",
                "entity_type": "PERSON",
                "char_start": span_start,
                "char_end": span_end,
                "confidence": 1.0,
            }
        ],
    )
    provenance = char_range_provenance(100, 100 + len(text))

    masked = mask_char_range_provenance([chunk], provenance, _cfg())

    assert "John Smith" not in masked
    assert "[PERSON]" in masked
    assert "Contact" in masked and "for details." in masked


def test_mask_char_range_provenance_passes_through_when_redaction_is_disabled():
    text = "Contact John Smith for details."
    chunk = ChunkSpans(
        text=text,
        char_start=0,
        char_end=len(text),
        redactions=[
            {
                "category": "pii",
                "entity_type": "PERSON",
                "char_start": text.index("John Smith"),
                "char_end": text.index("John Smith") + len("John Smith"),
                "confidence": 1.0,
            }
        ],
    )
    provenance = char_range_provenance(0, len(text))

    masked = mask_char_range_provenance([chunk], provenance, _cfg(enabled=False))
    assert masked == text


def test_mask_char_range_provenance_joins_multiple_overlapping_chunks():
    chunk_a = ChunkSpans(text="Part one.", char_start=0, char_end=9, redactions=[])
    chunk_b = ChunkSpans(text="Part two.", char_start=9, char_end=18, redactions=[])
    provenance = char_range_provenance(5, 14)

    masked = mask_char_range_provenance([chunk_a, chunk_b], provenance, _cfg())
    assert masked == "Part one. Part two."


def test_mask_char_range_provenance_returns_empty_when_nothing_overlaps():
    chunk = ChunkSpans(text="Unrelated text.", char_start=0, char_end=15, redactions=[])
    provenance = char_range_provenance(1000, 1010)
    assert mask_char_range_provenance([chunk], provenance, _cfg()) == ""
