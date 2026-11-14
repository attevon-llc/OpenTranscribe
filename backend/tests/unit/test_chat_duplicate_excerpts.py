"""One passage, one source id: the numbered excerpt list carries no duplicates.

Seen on a live stack (Qwen3-4B, two short FEMA recordings): sources [3] and [6] of one
answer were the same excerpt, same file, same 0:02 start, same text. Read-time expansion
(#523) widens every short chunk to its surrounding exchange and keeps the list's length,
so two short neighbours in a short recording come back as one span under two
``chunk_index`` values. Retrieval's ``(file_uuid, chunk_index)`` dedup cannot see that.

These tests drive the real expansion, the real merge, the real excerpt numbering and the
real citation builder, so the duplicate is produced the way production produces it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.services.chat import context_expansion as ce
from app.services.chat.citations import build_offered_citations
from app.services.chat.evidence import _merge_evidence
from app.services.chat.prompt_blocks import format_excerpts
from app.services.chat.redactor import MaskedChunk
from app.services.chat.retrieval import RetrievalResult
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit

FILE = "22222222-2222-2222-2222-222222222222"
OTHER_FILE = "33333333-3333-3333-3333-333333333333"

# A short recording: every segment sits inside one expansion window of every other.
SEGMENTS = [
    SimpleNamespace(start_time=2.0, end_time=6.0, text="Build a kit before you need it."),
    SimpleNamespace(start_time=6.0, end_time=9.0, text="Water, food, a radio."),
    SimpleNamespace(start_time=9.0, end_time=12.0, text="And a flashlight."),
    SimpleNamespace(start_time=12.0, end_time=18.0, text="Store it where everyone can reach it."),
]


class _Query:
    """The ``query().filter().order_by().all()`` chain ``expand_one`` reads segments with."""

    def filter(self, *_: Any) -> _Query:
        return self

    def order_by(self, *_: Any) -> _Query:
        return self

    def all(self) -> list[SimpleNamespace]:
        return list(SEGMENTS)


class _Db:
    def query(self, *_: Any) -> _Query:
        return _Query()


def _hit(index: int, content: str, start: float, end: float, *, file_uuid: str = FILE) -> ChunkHit:
    return ChunkHit(
        file_uuid=file_uuid,
        file_id=7,
        chunk_index=index,
        content=content,
        title="FEMA emergency kit",
        start_time=start,
        end_time=end,
    )


def _digest(section: int, *, summary: bool = False) -> ChunkHit:
    return ChunkHit(
        file_uuid=FILE,
        file_id=7,
        chunk_index=-1 - section,
        content=f"Digest section {section}: build a kit with water, food and a radio.",
        title="FEMA emergency kit",
        start_time=2.0,
        end_time=18.0,
        digest_section=section,
        is_llm_summary=summary,
    )


def _numbered_sources(digests: list[ChunkHit], chunks: list[ChunkHit]) -> list[dict]:
    """Run the path from masked evidence to the ``sources`` frame's citations."""
    masked = [MaskedChunk(source=c, content=c.content) for c in chunks]
    digest_masked = [MaskedChunk(source=d, content=d.content) for d in digests]
    merged = _merge_evidence(
        masked, digest_masked, RetrievalResult(chunks=chunks), meta={}, recorder=None
    )
    _, excerpt_ids = format_excerpts(merged, budget_chars=100_000)
    return build_offered_citations(merged, excerpt_ids)


def test_two_short_neighbours_expand_to_one_span():
    """The mechanism: expansion turns two distinct chunks into the same excerpt."""
    first, second = ce.expand_chunks(
        cast(Session, _Db()),
        [_hit(0, "Water, food, a radio.", 6.0, 9.0), _hit(1, "And a flashlight.", 9.0, 12.0)],
    )

    assert first.chunk_index != second.chunk_index
    assert (first.start_time, first.end_time, first.content) == (
        second.start_time,
        second.end_time,
        second.content,
    )


def test_an_expanded_duplicate_is_offered_once():
    chunks = ce.expand_chunks(
        cast(Session, _Db()),
        [
            _hit(0, "Water, food, a radio.", 6.0, 9.0),
            _hit(5, " ".join(["unrelated"] * 30), 300.0, 330.0, file_uuid=OTHER_FILE),
            _hit(1, "And a flashlight.", 9.0, 12.0),
        ],
    )

    citations = _numbered_sources([], chunks)

    spans = [(c["file_uuid"], c["start_time"], c["end_time"]) for c in citations]
    assert len(spans) == len(set(spans)), spans
    assert len(citations) == 2
    # The first, best-ranked copy is the one kept, and ids stay contiguous.
    assert [c["id"] for c in citations] == [1, 2]
    assert citations[0]["chunk_index"] == 0


def test_the_drop_is_recorded_in_the_turn_metadata():
    chunks = ce.expand_chunks(
        cast(Session, _Db()),
        [_hit(0, "Water, food, a radio.", 6.0, 9.0), _hit(1, "And a flashlight.", 9.0, 12.0)],
    )
    meta: dict[str, Any] = {}

    _merge_evidence(
        [MaskedChunk(source=c, content=c.content) for c in chunks],
        [],
        RetrievalResult(chunks=chunks),
        meta=meta,
        recorder=None,
    )

    assert meta["chunks_dropped_duplicate"] == 1


def test_distinct_excerpts_are_all_kept():
    """Control: overlapping but different spans, and digests sharing a chunk's bounds."""
    chunks = [
        _hit(0, "Water, food, a radio.", 6.0, 9.0),
        _hit(1, "Water, food, a radio. And a flashlight.", 6.0, 12.0),
        _hit(2, "Build a kit before you need it.", 2.0, 18.0),
    ]
    digests = [_digest(0), _digest(1), _digest(0, summary=True)]
    meta: dict[str, Any] = {}

    masked = [MaskedChunk(source=c, content=c.content) for c in chunks]
    merged = _merge_evidence(
        masked,
        [MaskedChunk(source=d, content=d.content) for d in digests],
        RetrievalResult(chunks=chunks),
        meta=meta,
        recorder=None,
    )

    assert len(merged) == 6
    assert "chunks_dropped_duplicate" not in meta
