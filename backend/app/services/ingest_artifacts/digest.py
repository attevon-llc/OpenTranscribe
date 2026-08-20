"""The extractive digest: sentences the speakers actually said, with provenance.

Extractive, not generative — #403 **D6**. Nothing here calls an LLM, loads a model, or
touches OpenSearch; the input is ``TranscriptSegment`` rows and the output is JSON. That
is what lets a deployment with ``LLM_PROVIDER`` empty still have a summary tier.

Three properties the rest of the epic depends on:

1. **Every sentence is quotable.** It is verbatim source text, so Stage 4 can cite it and
   the read-time masking path (``redactor._gather_chunk_segments``) can re-mask it from the
   cached spans of the very segments named in its provenance.
2. **Every sentence carries provenance** (:mod:`.provenance`, D3) — segment ids *and*
   real timestamps, so a digest citation deep-links to when it was said instead of to
   ``0:00`` (addendum G7).
3. **Sections, not one blob.** Sized by :mod:`.sizing` from the measured 128-wordpiece
   embedding window, because a digest longer than the window is a digest whose vector
   describes only its opening.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.services.search.chunking_service import split_into_sentences
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABEL

from . import sizing
from .provenance import segment_provenance
from .textrank import rank_sentences

logger = logging.getLogger(__name__)

#: Bumped when the algorithm changes in a way that makes stored digests non-comparable.
#: Stage 3 regenerates any row below the current value (addendum G1).
DIGEST_SCHEMA_VERSION = 1

#: Recorded in the payload so a mixed-vintage corpus is diagnosable.
DIGEST_GENERATOR = "textrank-tfidf"

#: Sentences shorter than this are dropped before ranking: "Yeah." and "Mm-hmm." are
#: excellent PageRank hubs in a meeting transcript and terrible summary sentences.
MIN_SENTENCE_WORDS = 5


@dataclass(frozen=True)
class SourceSentence:
    """One candidate sentence with everything needed to cite it."""

    text: str
    order: int
    speaker: str
    segment_ids: tuple[int, ...]
    start_time: float
    end_time: float

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def _turns(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group consecutive same-speaker segments, mirroring the search chunker's notion."""
    turns: list[list[dict[str, Any]]] = []
    for segment in segments:
        speaker = segment.get("speaker") or UNKNOWN_SPEAKER_LABEL
        if turns and (turns[-1][0].get("speaker") or UNKNOWN_SPEAKER_LABEL) == speaker:
            turns[-1].append(segment)
        else:
            turns.append([segment])
    return turns


def _sentences_for_turn(
    turn: list[dict[str, Any]], language: str, start_order: int
) -> list[SourceSentence]:
    """Sentence-split one speaker turn and map each sentence back to its segments.

    The turn text is the segments joined with single spaces, so each segment occupies a
    known ``[offset, offset+len)`` span of it. A sentence's contributing segments are
    those whose span overlaps the sentence's span — which is how a sentence that runs
    across an ASR segment boundary ends up with two ids instead of losing one.
    """
    spans: list[tuple[int, int, dict[str, Any]]] = []
    pieces: list[str] = []
    cursor = 0
    for segment in turn:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        if pieces:
            cursor += 1  # the joining space
        spans.append((cursor, cursor + len(text), segment))
        pieces.append(text)
        cursor += len(text)

    if not pieces:
        return []

    turn_text = " ".join(pieces)
    speaker = turn[0].get("speaker") or UNKNOWN_SPEAKER_LABEL

    results: list[SourceSentence] = []
    search_from = 0
    order = start_order
    for raw in split_into_sentences(turn_text, language):
        sentence = raw.strip()
        if not sentence:
            continue
        found = turn_text.find(sentence, search_from)
        if found < 0:  # tokenizer normalised whitespace; fall back to sequential layout
            found = search_from
        sentence_span = (found, found + len(sentence))
        search_from = sentence_span[1]

        overlapping = [
            segment
            for start, end, segment in spans
            if start < sentence_span[1] and end > sentence_span[0]
        ]
        if not overlapping:
            overlapping = [spans[-1][2]]

        if len(sentence.split()) < MIN_SENTENCE_WORDS:
            continue

        results.append(
            SourceSentence(
                text=sentence,
                order=order,
                speaker=str(speaker),
                segment_ids=tuple(int(s["id"]) for s in overlapping),
                start_time=float(overlapping[0].get("start_time") or 0.0),
                end_time=float(overlapping[-1].get("end_time") or 0.0),
            )
        )
        order += 1

    return results


def candidate_sentences(
    segments: list[dict[str, Any]], language: str = "en"
) -> list[SourceSentence]:
    """Split *segments* into rankable sentences with provenance.

    Args:
        segments: Segment dicts with ``id``/``text``/``start_time``/``end_time``/``speaker``,
            **already in total order** — ``(start_time, end_time, id)``, per #433. Sorting
            here would hide a caller reading in a non-total order, which is the defect that
            made one corpus index to three different chunk counts.
        language: ISO 639-1 code, for sentence splitting.
    """
    sentences: list[SourceSentence] = []
    for turn in _turns(segments):
        sentences.extend(_sentences_for_turn(turn, language, len(sentences)))
    return sentences


def _partition(sentences: list[SourceSentence], parts: int) -> list[list[SourceSentence]]:
    """Split into *parts* contiguous groups of roughly equal word count.

    Contiguous, so each section covers a real time span and its digest document can carry
    honest ``start_time``/``end_time``. Equal *words* rather than equal sentence count,
    because a section of twenty one-liners and a section of five paragraphs are not
    comparable summaries.
    """
    if parts <= 1 or len(sentences) <= 1:
        return [sentences]

    total_words = sum(s.word_count for s in sentences) or len(sentences)
    groups: list[list[SourceSentence]] = []
    current: list[SourceSentence] = []
    accumulated = 0
    for sentence in sentences:
        current.append(sentence)
        accumulated += sentence.word_count
        remaining_parts = parts - len(groups)
        if remaining_parts > 1 and accumulated >= total_words / parts:
            groups.append(current)
            current = []
            accumulated = 0
    if current:
        groups.append(current)
    return groups


def _select(sentences: list[SourceSentence], language: str) -> list[tuple[SourceSentence, float]]:
    """Rank one section's sentences and take the best until the word target is met."""
    if not sentences:
        return []

    scores = rank_sentences([s.text for s in sentences], language)
    # (-score, order): position breaks score ties, so equal-scoring sentences always
    # resolve the same way. argsort alone is only stable by accident of implementation.
    ordered = sorted(range(len(sentences)), key=lambda i: (-float(scores[i]), sentences[i].order))

    chosen: list[int] = []
    words = 0
    for i in ordered:
        candidate_words = sentences[i].word_count
        if chosen and words + candidate_words > sizing.DIGEST_SECTION_MAX_WORDS:
            continue
        chosen.append(i)
        words += candidate_words
        if words >= sizing.DIGEST_SECTION_TARGET_WORDS:
            break

    if not chosen:
        chosen = [ordered[0]]

    chosen.sort(key=lambda i: sentences[i].order)  # read in the order it was said
    return [(sentences[i], float(scores[i])) for i in chosen]


def build_digest(
    segments: list[dict[str, Any]],
    *,
    language: str = "en",
) -> dict[str, Any]:
    """Build the sectioned extractive digest for one transcript.

    Args:
        segments: Ordered segment dicts (see :func:`candidate_sentences`).
        language: ISO 639-1 code.

    Returns:
        The ``file_facts.digest`` JSONB payload. ``sections`` is empty only when the
        transcript contains no sentence of :data:`MIN_SENTENCE_WORDS` words — a real
        outcome for a 10-second clip, and the reason callers must treat an empty digest
        as valid rather than as failure.
    """
    sentences = candidate_sentences(segments, language)
    total_words = sum(s.word_count for s in sentences)
    parts = sizing.section_count_for(total_words)

    sections: list[dict[str, Any]] = []
    for group in _partition(sentences, parts):
        selected = _select(group, language)
        if not selected:
            continue
        sections.append(
            {
                "index": len(sections),
                "text": " ".join(s.text for s, _ in selected),
                "word_count": sum(s.word_count for s, _ in selected),
                "start_time": round(min(s.start_time for s, _ in selected), 2),
                "end_time": round(max(s.end_time for s, _ in selected), 2),
                "speakers": sorted({s.speaker for s, _ in selected}),
                "sentences": [
                    {
                        "text": s.text,
                        "order": s.order,
                        "speaker": s.speaker,
                        "rank": round(score, 8),
                        "provenance": segment_provenance(
                            list(s.segment_ids), s.start_time, s.end_time
                        ),
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


def digest_text(digest: dict[str, Any], separator: str = "\n\n") -> str:
    """Flatten a digest to prose — the no-LLM composed overview's raw material."""
    return separator.join(section["text"] for section in digest.get("sections", []))
