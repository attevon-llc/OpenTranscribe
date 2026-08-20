"""``ingest_artifacts/document_digest.py`` — the char_range provenance producer.

The transcript digest (``digest.py``) is exercised by
``tests/unit/test_ingest_artifacts_digest.py``; this suite is its document-plane twin,
and every test here is about the two things that differ: sentences are addressed by
absolute character offset into the document's IR text rather than by segment id, and
there are no speakers/turns to report.
"""

from __future__ import annotations

from app.services.ingest_artifacts.document_digest import build_document_digest
from app.services.ingest_artifacts.document_digest import document_candidate_sentences
from app.services.ingest_artifacts.provenance import KIND_CHAR_RANGE
from app.services.ingest_artifacts.provenance import validate_provenance

_PARA_A = (
    "This report summarizes the quarterly budget review for the engineering "
    "department. Spending remained within the approved limits for every team. "
    "The infrastructure team requested additional cloud capacity for the next "
    "quarter."
)
_PARA_B = (
    "Headcount grew by four engineers during the period under review. Two of "
    "the new hires joined the platform team and two joined the search team. "
    "Attrition stayed below the historical average for the department."
)


def _chunk(text: str, char_start: int, *, page: int | None = None, chunk_id: int = 1) -> dict:
    return {
        "id": chunk_id,
        "text": text,
        "char_start": char_start,
        "char_end": char_start + len(text),
        "page": page,
    }


def test_document_candidate_sentences_carry_absolute_char_range_offsets():
    chunks = [
        _chunk(_PARA_A, char_start=0, page=1, chunk_id=1),
        _chunk(_PARA_B, char_start=len(_PARA_A) + 2, page=2, chunk_id=2),
    ]
    full_text = _PARA_A + "\n\n" + _PARA_B

    sentences = document_candidate_sentences(chunks, "en")
    assert sentences, "expected at least one candidate sentence"

    for sentence in sentences:
        assert full_text[sentence.char_start : sentence.char_end] == sentence.text, (
            "a sentence's char_range must be a verbatim slice of the document's own text "
            "— the same invariant the document IR itself enforces for blocks"
        )

    pages = {sentence.page for sentence in sentences}
    assert pages == {1, 2}, "sentences must inherit their source chunk's page"


def test_document_candidate_sentences_orders_are_monotonic_and_contiguous():
    chunks = [
        _chunk(_PARA_A, char_start=0),
        _chunk(_PARA_B, char_start=len(_PARA_A) + 2, chunk_id=2),
    ]
    sentences = document_candidate_sentences(chunks, "en")
    orders = [s.order for s in sentences]
    assert orders == list(range(len(orders))), "order must be 0..N-1, in reading order"


def test_document_candidate_sentences_never_defaults_language_to_english():
    """Mirrors ``documents/chunking.py``'s own guard: passing None through must not
    become a hardcoded "en" here, or the punkt script/terminator guard (#448) is
    defeated for every non-Latin document.
    """
    import app.services.ingest_artifacts.document_digest as document_digest_module

    seen: list[str | None] = []
    real = document_digest_module.split_into_sentences

    def _spy(text: str, language: str | None = "en") -> list[str]:
        seen.append(language)
        return real(text, language)

    document_digest_module.split_into_sentences = _spy
    try:
        document_candidate_sentences([_chunk(_PARA_A, char_start=0)], None)
    finally:
        document_digest_module.split_into_sentences = real

    assert seen == [None], f"language must be passed through unchanged, got {seen!r}"


def test_build_document_digest_sections_use_char_range_provenance():
    chunks = [
        _chunk(_PARA_A, char_start=0, page=1, chunk_id=1),
        _chunk(_PARA_B, char_start=len(_PARA_A) + 2, page=2, chunk_id=2),
    ]
    full_text = _PARA_A + "\n\n" + _PARA_B

    digest = build_document_digest(chunks, language="en")
    assert digest["sections"], "expected at least one section for two real paragraphs"

    saw_provenance = False
    for section in digest["sections"]:
        assert section["sentences"], "a section must have at least one sentence"
        for sentence in section["sentences"]:
            provenance = sentence["provenance"]
            validate_provenance(provenance)  # never a malformed provenance at the producer
            assert provenance["kind"] == KIND_CHAR_RANGE
            start, end = provenance["char_start"], provenance["char_end"]
            assert full_text[start:end] == sentence["text"]
            saw_provenance = True
    assert saw_provenance


def test_build_document_digest_has_no_segment_ids_or_speakers():
    """Documents have no timeline and no speakers — the digest must not carry either
    shape, which would be silently wrong (a fabricated 0:00 citation, or a fictional
    speaker roster) rather than simply absent.
    """
    chunks = [_chunk(_PARA_A, char_start=0)]
    digest = build_document_digest(chunks, language="en")
    assert digest["sections"], (
        "expected at least one section — an empty list would pass every assert below vacuously"
    )
    for section in digest["sections"]:
        assert "speakers" not in section
        assert "start_time" not in section
        assert "end_time" not in section
        for sentence in section["sentences"]:
            assert sentence["provenance"]["kind"] == KIND_CHAR_RANGE
            assert "segment_ids" not in sentence["provenance"]


def test_build_document_digest_is_deterministic():
    chunks = [
        _chunk(_PARA_A, char_start=0),
        _chunk(_PARA_B, char_start=len(_PARA_A) + 2, chunk_id=2),
    ]
    first = build_document_digest(chunks, language="en")
    second = build_document_digest(chunks, language="en")
    assert first == second


def test_build_document_digest_empty_for_a_document_with_no_real_sentences():
    """A short document with nothing meeting MIN_SENTENCE_WORDS is a valid outcome, not
    a failure — same rule the transcript digest documents for a ten-second clip.
    """
    chunks = [_chunk("Hi. OK. Yes.", char_start=0)]
    digest = build_document_digest(chunks, language="en")
    assert digest["sections"] == []
    assert digest["word_count"] == 0
