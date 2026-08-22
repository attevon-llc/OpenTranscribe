"""The document analog of ``digest.py`` — sentences from ``document_chunk`` rows,
addressed by ``char_range`` provenance instead of ``segment_ids`` (#403 D3, #362 Stage 6).

``provenance.char_range_provenance`` was "designed and never used" until this module:
the tagged union has carried both arms since D3, but the only producer was the
transcript path. This is that second producer — no second provenance shape invented.

Nothing here calls an LLM, loads a model, or touches OpenSearch: the same #403 **D6**
no-LLM tier documents inherit from the transcript digest, and this module reuses
``digest.py``'s ranking/partitioning machinery rather than duplicating it (this
package's own convention: "iterate on existing patterns... delete the old one" applies
in reverse here — there is no old one, so the new one shares rather than forks).

Documents have no timeline, so a document digest section carries **pages**, not
``start_time``/``end_time`` — ``provenance.provenance_timespan`` already returns
``None`` for a ``char_range`` sentence, which is Stage 4's existing signal to cite the
file rather than deep-link to a timestamp that does not exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.services.search.chunking_service import split_into_sentences

from . import sizing
from .digest import DIGEST_GENERATOR
from .digest import DIGEST_SCHEMA_VERSION
from .digest import MIN_SENTENCE_WORDS
from .digest import _partition  # generic over any (.word_count) sentence — see the docstring
from .digest import _select  # generic over any (.text/.order/.word_count) sentence
from .provenance import char_range_provenance

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentSourceSentence:
    """One candidate sentence, addressed by absolute offset into the document's IR text."""

    text: str
    order: int
    char_start: int
    char_end: int
    page: int | None

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def document_candidate_sentences(
    chunks: list[dict[str, Any]], language: str | None = None
) -> list[DocumentSourceSentence]:
    """Split ordered ``document_chunk`` rows into rankable sentences with provenance.

    Args:
        chunks: Dicts with ``id``/``text``/``char_start``/``char_end``/``page``, **in
            chunk_index order** — the document plane's equivalent of the total-order
            requirement ``ingest_artifacts/service.py``'s segment loader documents for
            transcripts (#433): chunk order already IS a total order (a unique,
            contiguous ``chunk_index``), so no further sort happens here, and a caller
            reading in a different order silently produces a different digest.
        language: The document's detected language, or ``None``. Passed straight
            through to :func:`split_into_sentences` — **never** defaulted to ``"en"``
            here, mirroring ``documents/chunking.py``'s own
            ``_split_long_block``: a hardcoded ``"en"`` would defeat that function's
            script/terminator guard (issue #448) for every non-Latin document.

    Returns:
        Sentences in document reading order, each carrying the absolute
        ``[char_start, char_end)`` range it occupies in the document's IR text.
    """
    sentences: list[DocumentSourceSentence] = []
    order = 0
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        if not text.strip():
            continue
        base = int(chunk["char_start"])
        page = chunk.get("page")
        search_from = 0
        for raw in split_into_sentences(text, language):
            sentence = raw.strip()
            if not sentence:
                continue
            found = text.find(sentence, search_from)
            if found < 0:  # tokenizer normalised whitespace; fall back to sequential layout
                found = search_from
            local_end = found + len(sentence)
            search_from = local_end
            if len(sentence.split()) < MIN_SENTENCE_WORDS:
                continue
            sentences.append(
                DocumentSourceSentence(
                    text=sentence,
                    order=order,
                    char_start=base + found,
                    char_end=base + local_end,
                    page=page,
                )
            )
            order += 1
    return sentences


def build_document_digest(
    chunks: list[dict[str, Any]],
    *,
    language: str | None = None,
) -> dict[str, Any]:
    """Build the sectioned extractive digest for one document.

    Mirrors ``digest.build_digest`` exactly — partition by word budget, TextRank-select
    within each section — substituting :func:`~.provenance.char_range_provenance` for
    ``segment_provenance``. Ranking itself always resolves a real language ("en" when
    none was detected, the same fallback ``ingest_artifacts.service.build_artifacts``
    uses for transcripts): that only affects which stopwords TF-IDF ranks around, not
    where a sentence boundary is cut, so it is not the class of bug the guard above
    exists to prevent.

    Args:
        chunks: Ordered ``document_chunk`` dicts (see
            :func:`document_candidate_sentences`).
        language: ISO 639-1 code, or ``None``.

    Returns:
        The ``file_facts.digest`` JSONB payload. ``sections`` is empty only when the
        document contains no sentence of :data:`~.digest.MIN_SENTENCE_WORDS` words — a
        real outcome for a short document, not a failure; callers must treat it the same
        way they already treat an empty transcript digest.
    """
    sentences = document_candidate_sentences(chunks, language)
    total_words = sum(s.word_count for s in sentences)
    parts = sizing.section_count_for(total_words)
    ranking_language = (language or "en").split("-")[0].lower()

    sections: list[dict[str, Any]] = []
    for group in _partition(sentences, parts):
        selected = _select(group, ranking_language)
        if not selected:
            continue
        pages = sorted({s.page for s, _ in selected if s.page is not None})
        sections.append(
            {
                "index": len(sections),
                "text": " ".join(s.text for s, _ in selected),
                "word_count": sum(s.word_count for s, _ in selected),
                "pages": pages,
                "sentences": [
                    {
                        "text": s.text,
                        "order": s.order,
                        "rank": round(score, 8),
                        "provenance": char_range_provenance(s.char_start, s.char_end, page=s.page),
                    }
                    for s, score in selected
                ],
            }
        )

    return {
        "schema_version": DIGEST_SCHEMA_VERSION,
        "generator": DIGEST_GENERATOR,
        "language": language,
        "sections": sections,
        "word_count": sum(section["word_count"] for section in sections),
        "candidate_sentence_count": len(sentences),
        "embedding_window_wordpieces": sizing.EMBEDDING_MAX_WORDPIECES,
        "section_max_words": sizing.DIGEST_SECTION_MAX_WORDS,
    }
