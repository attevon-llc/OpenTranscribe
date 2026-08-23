"""Issue #523: read-time context expansion for short retrieved chunks.

Pure-function tests for ``chat/context_expansion.py`` — no database, no
masking. The integration with ``redactor.mask_chunks`` (the guarantee that an
expanded chunk still takes the same masking policy as any other excerpt) is
covered in ``test_chat_redactor_context_expansion.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.services.chat import context_expansion as ce
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


def _chunk(
    content: str = "Y",
    *,
    start: float = 100.0,
    end: float = 101.0,
    file_id: int = 5,
    digest_section: int | None = None,
) -> ChunkHit:
    return ChunkHit(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=file_id,
        chunk_index=3,
        content=content,
        title="TS3005 planning",
        speaker="Marketing",
        start_time=start,
        end_time=end,
        digest_section=digest_section,
    )


def _seg(text: str, start: float, end: float) -> SimpleNamespace:
    return SimpleNamespace(start_time=start, end_time=end, text=text)


# --------------------------------------------------------------------------- #
# needs_expansion
# --------------------------------------------------------------------------- #


def test_short_transcript_chunk_needs_expansion():
    assert ce.needs_expansion(_chunk("Y")) is True


def test_long_transcript_chunk_does_not_need_expansion():
    long_text = " ".join(["word"] * 40)
    assert ce.needs_expansion(_chunk(long_text)) is False


def test_chunk_at_exactly_the_threshold_does_not_need_expansion():
    exactly_20 = " ".join(["word"] * ce.SHORT_CHUNK_WORD_THRESHOLD)
    assert ce.needs_expansion(_chunk(exactly_20)) is False


def test_digest_hit_never_needs_expansion():
    digest = _chunk("Y", digest_section=0)
    assert digest.is_digest is True
    assert ce.needs_expansion(digest) is False


# --------------------------------------------------------------------------- #
# expansion_window
# --------------------------------------------------------------------------- #


def test_expansion_window_widens_both_sides():
    chunk = _chunk("Y", start=100.0, end=102.0)
    start, end = ce.expansion_window(chunk)
    assert start == pytest.approx(100.0 - ce.EXPANSION_WINDOW_SECONDS)
    assert end == pytest.approx(102.0 + ce.EXPANSION_WINDOW_SECONDS)


def test_expansion_window_never_goes_negative():
    chunk = _chunk("Y", start=5.0, end=6.0)
    start, _end = ce.expansion_window(chunk)
    assert start == 0.0


def test_expansion_window_falls_back_to_start_time_when_end_time_is_none():
    chunk = _chunk("Y", start=100.0, end=100.0)
    chunk.end_time = None
    start, end = ce.expansion_window(chunk)
    assert start == pytest.approx(100.0 - ce.EXPANSION_WINDOW_SECONDS)
    assert end == pytest.approx(100.0 + ce.EXPANSION_WINDOW_SECONDS)


# --------------------------------------------------------------------------- #
# select_expansion_segments — the bounded, proximity-ranked selection policy
# --------------------------------------------------------------------------- #


def test_selection_keeps_the_original_chunks_own_segment():
    chunk = _chunk("Y", start=100.0, end=102.0)
    own = _seg("Y", 100.0, 102.0)
    far = _seg("unrelated", 10.0, 12.0)
    selected = ce.select_expansion_segments([far, own], chunk)
    assert own in selected


def test_selection_prefers_closer_segments_when_over_the_segment_ceiling(monkeypatch):
    monkeypatch.setattr(ce, "MAX_EXPANSION_SEGMENTS", 2)
    chunk = _chunk("Y", start=100.0, end=101.0)
    own = _seg("Y", 100.0, 101.0)
    near = _seg("close neighbour", 102.0, 105.0)
    far = _seg("distant neighbour", 200.0, 205.0)

    selected = ce.select_expansion_segments([far, near, own], chunk)

    assert len(selected) == 2
    assert far not in selected
    assert own in selected
    assert near in selected


def test_selection_bounded_by_the_word_ceiling(monkeypatch):
    monkeypatch.setattr(ce, "MAX_EXPANDED_WORDS", 10)
    chunk = _chunk("Y", start=100.0, end=101.0)
    own = _seg("Y", 100.0, 101.0)
    long_neighbor = _seg(" ".join(["word"] * 20), 102.0, 110.0)

    selected = ce.select_expansion_segments([long_neighbor, own], chunk)

    assert own in selected
    assert long_neighbor not in selected


def test_selection_never_drops_the_original_segment_for_being_long_on_its_own(monkeypatch):
    """The word ceiling only refuses ADDITIONAL segments — the short chunk's
    own span is always kept, even if it alone would exceed the budget."""
    monkeypatch.setattr(ce, "MAX_EXPANDED_WORDS", 3)
    chunk = _chunk("Y", start=100.0, end=101.0)
    own = _seg(" ".join(["word"] * 10), 100.0, 101.0)

    selected = ce.select_expansion_segments([own], chunk)

    assert selected == [own]


def test_selection_returns_chronological_order_regardless_of_input_order():
    chunk = _chunk("Y", start=100.0, end=101.0)
    before = _seg("before", 90.0, 95.0)
    own = _seg("Y", 100.0, 101.0)
    after = _seg("after", 105.0, 110.0)

    selected = ce.select_expansion_segments([after, own, before], chunk)

    assert selected == [before, own, after]


def test_selection_ignores_blank_segments():
    chunk = _chunk("Y", start=100.0, end=101.0)
    own = _seg("Y", 100.0, 101.0)
    blank = _seg("   ", 101.5, 102.0)

    selected = ce.select_expansion_segments([own, blank], chunk)

    assert selected == [own]


def test_selection_never_exceeds_the_segment_ceiling_with_many_candidates():
    chunk = _chunk("Y", start=1000.0, end=1001.0)
    candidates = [_seg(f"segment {i}", 900.0 + i, 900.0 + i + 1) for i in range(50)]

    selected = ce.select_expansion_segments(candidates, chunk)

    assert len(selected) <= ce.MAX_EXPANSION_SEGMENTS


# --------------------------------------------------------------------------- #
# expand_one / expand_chunks — with a fake DB (no real Postgres)
# --------------------------------------------------------------------------- #


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *args, **kwargs):
        return _FakeQuery(self._rows)


class _RaisingDB:
    def query(self, *args, **kwargs):
        raise RuntimeError("boom")


def test_expand_one_widens_a_short_chunk_to_its_surrounding_exchange():
    """The reproduction shape, at the pure-function level: a content-free
    'Y' sits between substantive neighbours."""
    chunk = _chunk("Y", start=100.0, end=101.0)
    segments = [
        _seg("What did marketing contribute this quarter?", 95.0, 99.0),
        _seg("Y", 100.0, 101.0),
        _seg("We shipped the new pricing page and ran the launch campaign.", 102.0, 108.0),
    ]
    db = _FakeDB(segments)

    expanded = ce.expand_one(cast(Session, db), chunk)

    assert "launch campaign" in expanded.content
    assert expanded.content != "Y"
    assert expanded.start_time == 95.0
    assert expanded.end_time == 108.0


def test_expand_one_returns_the_original_chunk_when_nothing_is_found():
    chunk = _chunk("Y")
    expanded = ce.expand_one(cast(Session, _FakeDB([])), chunk)
    assert expanded is chunk


def test_expand_one_fails_closed_to_the_original_chunk_on_a_db_error():
    """An expansion failure must degrade to the un-expanded chunk, never
    raise into the caller — it is an enhancement, not a dependency."""
    chunk = _chunk("Y")
    expanded = ce.expand_one(cast(Session, _RaisingDB()), chunk)
    assert expanded is chunk


def test_expand_chunks_leaves_long_chunks_untouched():
    short = _chunk("Y", start=100.0, end=101.0)
    long_text = " ".join(["word"] * 40)
    long_chunk = _chunk(long_text, start=500.0, end=510.0)
    segments = [_seg("Y", 100.0, 101.0), _seg("more context here", 101.5, 104.0)]
    db = _FakeDB(segments)

    expanded = ce.expand_chunks(cast(Session, db), [short, long_chunk])

    assert expanded[0].content != "Y"
    assert expanded[1] is long_chunk


def test_expand_chunks_preserves_list_length_and_order():
    a = _chunk("Y", start=100.0, end=101.0, file_id=1)
    b = _chunk("also short", start=200.0, end=201.0, file_id=2)
    db = _FakeDB([])  # nothing found for either — both pass through unchanged

    expanded = ce.expand_chunks(cast(Session, db), [a, b])

    assert expanded == [a, b]


def test_expand_chunks_skips_digests_without_querying():
    """A digest hit must never trigger a TranscriptSegment read — there is no
    time-range concept for one that this module can widen."""

    class _ExplodingDB:
        def query(self, *a, **k):
            raise AssertionError("expand_chunks queried a digest hit")

    digest = _chunk("Y", digest_section=0)

    expanded = ce.expand_chunks(cast(Session, _ExplodingDB()), [digest])

    assert expanded == [digest]
