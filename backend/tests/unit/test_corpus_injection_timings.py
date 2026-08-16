# mypy: disable-error-code="operator,index,type-var,arg-type,return-value"
# These suites assert directly on ``Turn.start``/``Turn.end`` (typed
# ``float | None`` because an adapter leaves them unset until timings are
# resolved) and on ``TimingInfo.params`` (``dict | None``). Every such
# assertion runs *after* the call that populates the field — that is the
# thing being tested — so narrowing each one would bury the assertion in
# ``assert x is not None`` noise. Declared once here rather than widening a
# production signature to suit a test.
"""Synthetic timestamps must be generated, flagged, and refused (issue #403).

QMSum has no timestamps and OpenTranscribe cites by time, so the injector
generates them. The risk is not the generation — it is a later reader computing
a "citation timestamp error" over invented numbers and putting it in a paper.
These tests pin the four defences: generation is deterministic, provenance is
recorded, ``words`` is emptied so no word-level metric has data, and the guard
raises instead of quietly filtering.
"""

from __future__ import annotations

import pytest

from app.scripts.corpus_injection.model import TIMING_REAL
from app.scripts.corpus_injection.model import TIMING_SYNTHETIC
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import TimingInfo
from app.scripts.corpus_injection.model import Turn
from app.scripts.corpus_injection.model import Word
from app.scripts.corpus_injection.timings import SYNTHETIC_INTER_TURN_GAP_S
from app.scripts.corpus_injection.timings import SYNTHETIC_MIN_TURN_S
from app.scripts.corpus_injection.timings import SYNTHETIC_WORDS_PER_SECOND
from app.scripts.corpus_injection.timings import SyntheticTimingError
from app.scripts.corpus_injection.timings import assert_real_timings
from app.scripts.corpus_injection.timings import generate_synthetic_timings
from app.scripts.corpus_injection.timings import interpolate_missing
from app.scripts.corpus_injection.timings import resolve_timings


def _doc(turns: list[Turn], timing: TimingInfo | None = None) -> MeetingDoc:
    return MeetingDoc(
        corpus="test", meeting_id="M1", title="M1", turns=turns, timing=timing or TimingInfo()
    )


def _plain(count: int, words: int = 10) -> list[Turn]:
    return [
        Turn(turn_index=i, speaker=f"S{i % 3}", text=" ".join(["word"] * words))
        for i in range(count)
    ]


class TestSyntheticGeneration:
    def test_times_are_monotonic_and_non_degenerate(self):
        turns = _plain(5)
        generate_synthetic_timings(turns)
        for turn in turns:
            assert turn.start is not None and turn.end is not None
            assert turn.end > turn.start
        starts = [t.start for t in turns]
        assert starts == sorted(starts)

    def test_duration_follows_the_declared_word_rate(self):
        turns = [Turn(turn_index=0, speaker="A", text=" ".join(["w"] * 25))]
        generate_synthetic_timings(turns)
        assert turns[0].end - turns[0].start == pytest.approx(25 / SYNTHETIC_WORDS_PER_SECOND)

    def test_short_turn_gets_the_floor_not_a_zero_span(self):
        turns = [Turn(turn_index=0, speaker="A", text="Yeah")]
        generate_synthetic_timings(turns)
        assert turns[0].end - turns[0].start == pytest.approx(SYNTHETIC_MIN_TURN_S)

    def test_gap_between_turns_matches_the_declared_constant(self):
        turns = _plain(2)
        generate_synthetic_timings(turns)
        assert turns[1].start - turns[0].end == pytest.approx(SYNTHETIC_INTER_TURN_GAP_S)

    def test_generation_is_deterministic(self):
        first, second = _plain(6), _plain(6)
        generate_synthetic_timings(first)
        generate_synthetic_timings(second)
        assert [(t.start, t.end) for t in first] == [(t.start, t.end) for t in second]

    def test_no_word_timings_are_invented(self):
        turns = _plain(3)
        turns[0].words = [Word("word", 0.0, 1.0)]
        generate_synthetic_timings(turns)
        assert all(t.words is None for t in turns)


class TestInterpolation:
    def test_untimed_turn_lands_between_its_neighbours(self):
        turns = _plain(3)
        turns[0].start, turns[0].end = 0.0, 1.0
        turns[2].start, turns[2].end = 5.0, 6.0
        assert interpolate_missing(turns) == 1
        assert 1.0 <= turns[1].start < turns[1].end <= 5.0

    def test_a_run_of_untimed_turns_shares_the_gap_in_order(self):
        turns = _plain(5)
        turns[0].start, turns[0].end = 0.0, 1.0
        turns[4].start, turns[4].end = 10.0, 11.0
        assert interpolate_missing(turns) == 3
        middle = [(t.start, t.end) for t in turns[1:4]]
        assert middle == sorted(middle)
        assert len({m[0] for m in middle}) == 3

    def test_zero_width_gap_still_yields_distinct_spans(self):
        """The DB has ``UNIQUE(media_file_id, start, end, md5(text))``.

        Two identical backchannels collapsed onto the same instant abort the
        whole insert, which is exactly how this was found.
        """
        turns = [
            Turn(0, "A", "Okay .", start=1.0, end=2.0),
            Turn(1, "B", "Yeah ."),
            Turn(2, "C", "Yeah ."),
            Turn(3, "A", "Right .", start=2.0, end=3.0),
        ]
        interpolate_missing(turns)
        spans = {(t.start, t.end, t.text) for t in turns}
        assert len(spans) == 4

    def test_no_timed_turns_at_all_is_a_no_op(self):
        turns = _plain(3)
        assert interpolate_missing(turns) == 0
        assert all(t.start is None for t in turns)


class TestResolveTimings:
    def test_good_alignment_keeps_real_provenance(self):
        turns = _plain(10)
        for turn in turns[:9]:
            turn.start, turn.end = float(turn.turn_index), turn.turn_index + 0.5
        doc = _doc(turns, TimingInfo(source=TIMING_REAL, reference="ami:X"))
        resolve_timings(doc, min_alignment_rate=0.8)
        assert doc.timing.source == TIMING_REAL
        assert doc.timing.aligned_turns == 9
        assert doc.timing.params is None
        assert all(t.start is not None for t in doc.turns)

    def test_poor_alignment_falls_all_the_way_back_to_synthetic(self):
        """Provenance is per-file and all-or-nothing.

        A file that is 50 % measured and 50 % invented is neither, and every
        downstream consumer would need a per-segment predicate to use it safely.
        """
        turns = _plain(10)
        for turn in turns[:5]:
            turn.start, turn.end = float(turn.turn_index), turn.turn_index + 0.5
        doc = _doc(turns, TimingInfo(source=TIMING_REAL, reference="ami:X"))
        resolve_timings(doc, min_alignment_rate=0.8)
        assert doc.timing.source == TIMING_SYNTHETIC
        assert doc.timing.aligned_turns == 0
        assert doc.timing.params["generator"] == "uniform_rate_v1"
        # The partially-aligned real times were discarded, not blended in.
        assert doc.turns[0].start == 0.0

    def test_threshold_boundary_is_inclusive(self):
        turns = _plain(10)
        for turn in turns[:8]:
            turn.start, turn.end = float(turn.turn_index), turn.turn_index + 0.5
        doc = _doc(turns, TimingInfo(source=TIMING_REAL))
        resolve_timings(doc, min_alignment_rate=0.8)
        assert doc.timing.source == TIMING_REAL

    def test_untimed_corpus_gets_synthetic_times(self):
        doc = _doc(_plain(4))
        resolve_timings(doc)
        assert doc.timing.source == TIMING_SYNTHETIC
        assert all(t.start is not None for t in doc.turns)

    def test_generator_supplied_times_are_kept_but_never_called_real(self):
        turns = _plain(3)
        for turn in turns:
            turn.start, turn.end = turn.turn_index * 10.0, turn.turn_index * 10.0 + 4.0
        doc = _doc(turns, TimingInfo(source=TIMING_SYNTHETIC))
        resolve_timings(doc)
        assert doc.timing.source == TIMING_SYNTHETIC
        assert doc.timing.params["generator"] == "corpus_supplied_v1"
        assert doc.turns[2].start == 20.0  # pacing preserved

    def test_empty_meeting_does_not_crash(self):
        doc = _doc([])
        resolve_timings(doc)
        assert doc.timing.total_turns == 0


class TestGuard:
    def _record(self, meeting_id: str, source: str) -> dict:
        return {"meeting_id": meeting_id, "timing_source": source}

    def test_all_real_passes(self):
        assert_real_timings([self._record("A", TIMING_REAL), self._record("B", TIMING_REAL)])

    def test_one_synthetic_raises(self):
        with pytest.raises(SyntheticTimingError):
            assert_real_timings([self._record("A", TIMING_REAL), self._record("B", "synthetic")])

    def test_error_names_the_offenders_and_the_generator(self):
        with pytest.raises(SyntheticTimingError) as excinfo:
            assert_real_timings([self._record("covid_0", "synthetic")], context="latency metric")
        message = str(excinfo.value)
        assert "covid_0" in message
        assert "latency metric" in message
        assert "uniform_rate_v1" in message

    def test_it_raises_rather_than_returning_a_filtered_set(self):
        """Forgetting the check must crash, not silently narrow the corpus."""
        records = [self._record("A", TIMING_REAL), self._record("B", "synthetic")]
        with pytest.raises(SyntheticTimingError):
            assert_real_timings(records)
        assert len(records) == 2

    def test_it_accepts_manifest_objects_as_well_as_dicts(self):
        from app.scripts.corpus_injection.model import InjectionRecord

        record = InjectionRecord(
            corpus="qmsum",
            meeting_id="covid_0",
            file_uuid="u",
            media_file_id=1,
            title="t",
            turn_count=1,
            segment_count=1,
            word_count=1,
            speaker_count=1,
            duration_seconds=1.0,
            timing_source="synthetic",
            timing_reference=None,
            timing_aligned_turns=0,
            timing_alignment_rate=0.0,
            synthetic_timing_params={},
            content_sha256="x",
            language="en",
            action="created",
        )
        with pytest.raises(SyntheticTimingError):
            assert_real_timings([record])
