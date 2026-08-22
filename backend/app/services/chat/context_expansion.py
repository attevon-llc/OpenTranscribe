"""Read-time "small-to-big" context expansion for short retrieved chunks (issue #523).

**The observed failure.** A speaker-scoped question over a multi-party meeting
("What did the Marketing role contribute across the TS3005 meeting series?")
was answered from two near-empty fragments — "Y" and "Which ones were yours?"
— while substantive Marketing material sat in the same recording, retrieved
correctly for OTHER questions in the same run. Nothing was hallucinated: the
citations were real, the retrieval was just impoverished.

**The mechanism.** ``services/search/chunking_service.py`` merges a short
speaker turn into the previous chunk only when the previous chunk is the SAME
speaker. In multi-party conversation a short turn ("Y", "Yes", "Which ones
were yours?") sits between DIFFERENT speakers most of the time, so the merge
rarely fires and the short turn is indexed as its own tiny chunk. The
measured average speaker-turn chunk is 17 words — already below the 20-word
merge threshold — so this is not a rare edge case on this corpus.

**Deliberately NOT a re-chunk.** Changing the chunking strategy is a full
reindex over a corpus in active use and is explicitly deferred by #523. This
module works entirely at READ time: it widens a short chunk's own
``start_time``/``end_time`` to include its surrounding exchange, by
timestamp, using the SAME ``TranscriptSegment`` rows the digest/chunk planes
were built from — never dropping the short turn itself (it may be the entire
answer to "did we approve the budget?"), only giving it neighbours.

**Masking.** This module never decides what is safe to send an LLM — it only
widens a :class:`~app.services.search.chunk_retrieval.ChunkHit`'s time range
and rebuilds its ``content`` from the segments in that range. The caller
(``redactor.mask_chunks``, ``expand_short_chunks=True``) re-derives the
widened chunk's cached-span masking from the SAME widened ``start_time``/
``end_time`` this module writes, so an expanded chunk takes the identical
per-file strictest-wins policy any other excerpt does — nothing here bypasses
that gate, and masking still fails closed exactly as it does for an
unexpanded chunk.

**Budget.** Expansion competes for the same hard excerpt-budget ceiling
(``prompting.format_excerpts``) as everything else, so unbounded growth on
one short chunk would silently evict other files' evidence — trading this
bug for #517's coverage bug. :data:`MAX_EXPANSION_SEGMENTS` and
:data:`MAX_EXPANDED_WORDS` bound how much one chunk may grow, and the
selection is proximity-ranked (closest segments to the original short turn
first) so a capped expansion still centers on the turn that was retrieved.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from sqlalchemy.orm import Session

from app.services.search.chunk_retrieval import ChunkHit
from app.services.search.chunking_service import count_words

logger = logging.getLogger(__name__)

#: Below this many words, a retrieved chunk is a candidate for expansion.
#: Matches ``chunking_service.py``'s own short-turn-merge threshold
#: (``word_count < 20``) so "short" means the same thing at index time and at
#: read time — a chunk this module would widen is exactly the shape the
#: indexer already tried, and failed, to merge away.
SHORT_CHUNK_WORD_THRESHOLD = 20

#: How far, in seconds, to look on EACH side of a short chunk's own time
#: range for its surrounding exchange. Not admin-tunable: a fixed,
#: conservative window until this is measured (see ``chat/CLAUDE.md``'s
#: posture on unmeasured retrieval shapes).
EXPANSION_WINDOW_SECONDS = 45.0

#: Hard ceiling on how many transcript segments one expansion may pull in.
MAX_EXPANSION_SEGMENTS = 12

#: Hard ceiling on the expanded chunk's own word count. Segments are added in
#: proximity order and stop being added once this would be exceeded, so a
#: short chunk sitting in a long, exchange-dense stretch cannot balloon past
#: a predictable size before the excerpt budget ever sees it.
MAX_EXPANDED_WORDS = 250


class _SegmentLike(Protocol):
    start_time: float
    end_time: float
    #: Matches ``TranscriptSegment.text`` exactly (``Mapped[str]``, non-null)
    #: — a Protocol attribute is checked INVARIANTLY, so declaring this
    #: ``str | None`` would silently stop the real ORM row from satisfying
    #: the protocol at all despite every call site handling a falsy value
    #: defensively (``str(seg.text or "")``) regardless.
    text: str


def needs_expansion(chunk: ChunkHit) -> bool:
    """Whether ``chunk`` is a candidate for expansion.

    Transcript chunks only — a digest section is already a summary (no
    single time range to widen meaningfully) and a document chunk has no
    timeline at all (``char_start``/``char_end`` addressing instead).
    """
    if chunk.is_digest or chunk.is_document:
        return False
    return count_words(chunk.content) < SHORT_CHUNK_WORD_THRESHOLD


def expansion_window(chunk: ChunkHit) -> tuple[float, float]:
    """The ``(window_start, window_end)`` seconds to search for neighbours in."""
    end_time = chunk.end_time if chunk.end_time is not None else chunk.start_time
    window_start = max(0.0, chunk.start_time - EXPANSION_WINDOW_SECONDS)
    window_end = end_time + EXPANSION_WINDOW_SECONDS
    return window_start, window_end


def _distance_from_chunk(seg: _SegmentLike, chunk: ChunkHit) -> float:
    """Seconds between ``seg`` and the chunk's own span; ``0.0`` when they overlap."""
    end_time = chunk.end_time if chunk.end_time is not None else chunk.start_time
    seg_start = float(seg.start_time)
    seg_end = float(seg.end_time)
    if seg_end < chunk.start_time:
        return chunk.start_time - seg_end
    if seg_start > end_time:
        return seg_start - end_time
    return 0.0


def select_expansion_segments(
    candidates: Sequence[_SegmentLike], chunk: ChunkHit
) -> list[_SegmentLike]:
    """Bound and rank ``candidates`` into the segments one expansion may use.

    Pure — no I/O — so the selection policy (proximity first, then the two
    size ceilings) is testable without a database. Segments closest to the
    original chunk's own time range are preferred; ties break chronologically
    so two equidistant neighbours (one before, one after) keep the exchange
    symmetric rather than always favouring one side.

    Args:
        candidates: Every segment overlapping :func:`expansion_window`, in
            any order.
        chunk: The short chunk being expanded.

    Returns:
        The selected segments, in CHRONOLOGICAL order (ready to join into
        prose) — never more than :data:`MAX_EXPANSION_SEGMENTS` entries, and
        never more than :data:`MAX_EXPANDED_WORDS` words combined once at
        least one segment has been picked. The segment(s) covering the
        original chunk's own span are always closest (distance 0) and so are
        always picked first, before the budget can exclude them.
    """
    ranked = sorted(
        candidates,
        key=lambda seg: (_distance_from_chunk(seg, chunk), float(seg.start_time)),
    )

    selected: list[_SegmentLike] = []
    word_total = 0
    for seg in ranked:
        if len(selected) >= MAX_EXPANSION_SEGMENTS:
            break
        text = str(seg.text or "").strip()
        if not text:
            continue
        words = count_words(text)
        if selected and word_total + words > MAX_EXPANDED_WORDS:
            # Only refuse once something has already been selected — the
            # first (closest) segment is never dropped for being long on its
            # own, or a single over-length neighbour could empty the result.
            continue
        selected.append(seg)
        word_total += words

    selected.sort(key=lambda seg: (float(seg.start_time), float(seg.end_time)))
    return selected


def _widen_from_segments(chunk: ChunkHit, segments: list[_SegmentLike]) -> ChunkHit:
    """Rebuild ``chunk`` from ``segments``, or return it unchanged if that fails.

    Sets ``expanded=True`` on the returned chunk (issue #526) — the ONE place
    that flag is ever written. It rides the same ``dataclasses.replace`` that
    already widens ``start_time``/``end_time``/``content``, so a citation built
    from this chunk cannot report the widened span without also reporting that
    it is widened: there is no code path that produces one without the other.
    """
    if not segments:
        return chunk
    content = " ".join(str(seg.text or "").strip() for seg in segments).strip()
    if not content:
        return chunk
    new_start = min(float(seg.start_time) for seg in segments)
    new_end = max(float(seg.end_time) for seg in segments)
    return replace(chunk, content=content, start_time=new_start, end_time=new_end, expanded=True)


def expand_one(db: Session, chunk: ChunkHit) -> ChunkHit:
    """Widen one short chunk to its surrounding exchange, or return it unchanged.

    Reads ``TranscriptSegment`` rows in :func:`expansion_window` for
    ``chunk.file_id`` — the same table ``redactor.py``'s masking rebuild
    reads, and the caller widens ``chunk.start_time``/``end_time`` FIRST for
    exactly that reason: masking re-derives its own segment window from
    whatever time range the chunk carries, so an expanded chunk is re-masked
    over the expanded span with no separate wiring.

    Never raises — a failed expansion degrades to the original short chunk,
    which is always a legal (if impoverished) answer, never a broken turn.
    """
    from app.models.media import TranscriptSegment

    window_start, window_end = expansion_window(chunk)
    try:
        candidates = (
            db.query(TranscriptSegment)
            .filter(
                TranscriptSegment.media_file_id == chunk.file_id,
                TranscriptSegment.end_time >= window_start,
                TranscriptSegment.start_time <= window_end,
            )
            .order_by(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.id,
            )
            .all()
        )
    except Exception:  # noqa: BLE001 — an enhancement, never a dependency
        logger.exception(
            "Context expansion segment read failed for file %s; using the retrieved chunk as-is",
            chunk.file_id,
        )
        return chunk
    if not candidates:
        return chunk

    selected = select_expansion_segments(candidates, chunk)
    return _widen_from_segments(chunk, selected)


def expand_chunks(db: Session, chunks: list[ChunkHit]) -> list[ChunkHit]:
    """Widen every short chunk in ``chunks``; leave the rest untouched.

    Args:
        db: An OPEN session — the caller (``redactor.mask_chunks``) owns its
            lifetime and closes it before masking runs, matching every other
            gather step in that module (issue #83).
        chunks: Retrieved chunks, already reranked/diversity-sampled — this
            runs on the FINAL selected set, never the whole candidate pool,
            so the DB cost is bounded by ``settings.final_chunks``.

    Returns:
        A new list, same length and order as ``chunks``. Each entry is
        either the original :class:`ChunkHit` (long chunk, digest, document,
        or an expansion that found nothing/failed) or a widened copy.
    """
    return [expand_one(db, chunk) if needs_expansion(chunk) else chunk for chunk in chunks]


__all__ = [
    "EXPANSION_WINDOW_SECONDS",
    "MAX_EXPANDED_WORDS",
    "MAX_EXPANSION_SEGMENTS",
    "SHORT_CHUNK_WORD_THRESHOLD",
    "expand_chunks",
    "expand_one",
    "expansion_window",
    "needs_expansion",
    "select_expansion_segments",
]
