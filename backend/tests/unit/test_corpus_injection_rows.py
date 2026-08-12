# mypy: disable-error-code="operator,index,type-var,arg-type,return-value"
# These suites assert directly on ``Turn.start``/``Turn.end`` (typed
# ``float | None`` because an adapter leaves them unset until timings are
# resolved) and on ``TimingInfo.params`` (``dict | None``). Every such
# assertion runs *after* the call that populates the field — that is the
# thing being tested — so narrowing each one would bury the assertion in
# ``assert x is not None`` noise. Declared once here rather than widening a
# production signature to suit a test.
"""Segment/metadata row construction for injected corpora (issue #403).

These rows have to satisfy constraints the ORM does not declare — most
importantly the live DDL's
``UNIQUE (media_file_id, start_time, end_time, md5(text))`` — and they have to
carry the synthetic-timing provenance that keeps generated timestamps out of
metrics. Both are asserted here rather than discovered by a failing insert.
"""

from __future__ import annotations

from app.scripts.corpus_injection import ids
from app.scripts.corpus_injection import rows as rowbuild
from app.scripts.corpus_injection.model import TIMING_REAL
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import TimingInfo
from app.scripts.corpus_injection.model import Turn
from app.scripts.corpus_injection.model import Word
from app.scripts.corpus_injection.timings import resolve_timings


def _doc(turns: list[Turn], timing: TimingInfo | None = None) -> MeetingDoc:
    return MeetingDoc(
        corpus="qmsum", meeting_id="M1", title="M1", turns=turns, timing=timing or TimingInfo()
    )


class TestDuplicateSpanSeparation:
    def test_identical_span_and_text_is_separated(self):
        rows = [
            {"start_time": 1.0, "end_time": 2.0, "text": "Yeah ."},
            {"start_time": 1.0, "end_time": 2.0, "text": "Yeah ."},
        ]
        assert rowbuild.separate_duplicate_spans(rows) == 1
        assert rows[0]["end_time"] != rows[1]["end_time"]

    def test_the_onset_is_never_moved(self):
        """Citations point at the start. Only the end may be nudged."""
        rows = [
            {"start_time": 1.0, "end_time": 2.0, "text": "Yeah ."},
            {"start_time": 1.0, "end_time": 2.0, "text": "Yeah ."},
        ]
        rowbuild.separate_duplicate_spans(rows)
        assert rows[0]["start_time"] == rows[1]["start_time"] == 1.0

    def test_same_span_different_text_is_left_alone(self):
        rows = [
            {"start_time": 1.0, "end_time": 2.0, "text": "Yeah ."},
            {"start_time": 1.0, "end_time": 2.0, "text": "Okay ."},
        ]
        assert rowbuild.separate_duplicate_spans(rows) == 0
        assert rows[1]["end_time"] == 2.0

    def test_three_way_collision_resolves_to_three_distinct_keys(self):
        rows = [{"start_time": 1.0, "end_time": 2.0, "text": "Mm ."} for _ in range(3)]
        assert rowbuild.separate_duplicate_spans(rows) == 2
        keys = {(r["start_time"], r["end_time"], r["text"]) for r in rows}
        assert len(keys) == 3

    def test_the_nudge_is_small_enough_to_be_annotation_noise(self):
        rows = [{"start_time": 1.0, "end_time": 2.0, "text": "Mm ."} for _ in range(3)]
        rowbuild.separate_duplicate_spans(rows)
        assert max(r["end_time"] for r in rows) - 2.0 < 0.01

    def test_nothing_is_nudged_when_the_file_is_already_unique(self):
        rows = [
            {"start_time": 0.0, "end_time": 1.0, "text": "a"},
            {"start_time": 1.0, "end_time": 2.0, "text": "b"},
        ]
        assert rowbuild.separate_duplicate_spans(rows) == 0


class TestSegmentRows:
    def test_segments_are_emitted_in_time_order(self):
        turns = [Turn(0, "A", "late", 9.0, 9.5), Turn(1, "B", "early", 1.0, 1.5)]
        segments, turn_rows, _ = _build(turns)
        assert [s["text"] for s in segments] == ["early", "late"]
        assert [t["turn_index"] for t in turn_rows] == [1, 0]

    def test_turn_index_survives_the_reorder(self):
        """The gold-span mapping depends on this and nothing else does."""
        turns = [Turn(0, "A", "late", 9.0, 9.5), Turn(1, "B", "early", 1.0, 1.5)]
        _, turn_rows, _ = _build(turns)
        by_index = {row["turn_index"]: row for row in turn_rows}
        assert by_index[0]["start"] == 9.0
        assert by_index[1]["start"] == 1.0

    def test_segment_uuids_are_deterministic_for_the_same_input(self):
        turns = [Turn(0, "A", "hello", 0.0, 1.0), Turn(1, "B", "world", 1.0, 2.0)]
        first, _, _ = _build(turns)
        second, _, _ = _build([Turn(0, "A", "hello", 0.0, 1.0), Turn(1, "B", "world", 1.0, 2.0)])
        assert [s["uuid"] for s in first] == [s["uuid"] for s in second]
        assert first[0]["uuid"] == ids.segment_uuid("qmsum", "M1", "", 0)

    def test_empty_turns_are_dropped_not_written_as_blank_segments(self):
        turns = [Turn(0, "A", "", 0.0, 1.0), Turn(1, "B", "hello", 1.0, 2.0)]
        segments, turn_rows, _ = _build(turns)
        assert len(segments) == 1
        assert len(turn_rows) == 1

    def test_turn_table_end_matches_the_nudged_segment_end(self):
        """Otherwise the manifest and the database would disagree about a span."""
        turns = [Turn(0, "A", "Mm .", 1.0, 2.0), Turn(1, "B", "Mm .", 1.0, 2.0)]
        segments, turn_rows, nudged = _build(turns)
        assert nudged == 1
        assert [s["end_time"] for s in segments] == [t["end"] for t in turn_rows]

    def test_speaker_ids_are_attached_when_supplied(self):
        turns = [Turn(0, "A", "hi", 0.0, 1.0)]
        segments, _, _ = rowbuild.build_segment_rows(
            _doc(turns), "", media_file_id=7, speaker_ids={"A": 42}
        )
        assert segments[0]["speaker_id"] == 42
        assert segments[0]["media_file_id"] == 7

    def test_speaker_id_is_null_rather_than_wrong_when_unknown(self):
        turns = [Turn(0, "A", "hi", 0.0, 1.0)]
        segments, _, _ = rowbuild.build_segment_rows(_doc(turns), "", speaker_ids={"B": 42})
        assert segments[0]["speaker_id"] is None


class TestWordTimings:
    def test_real_timings_carry_word_level_data(self):
        turns = [Turn(0, "A", "hello world", 0.0, 1.0, [Word("hello", 0.0, 0.5)])]
        doc = _doc(turns, TimingInfo(source=TIMING_REAL, reference="ami:X"))
        resolve_timings(doc)
        segments, _, _ = rowbuild.build_segment_rows(doc, "")
        assert segments[0]["words"] == [{"word": "hello", "start": 0.0, "end": 0.5}]

    def test_synthetic_timings_write_null_words(self):
        """The strongest of the four guards: no rows, so no metric.

        A word-timing metric run over a synthetic meeting gets nothing to read
        rather than plausible-looking invented spans.
        """
        doc = _doc([Turn(0, "A", "hello world")])
        resolve_timings(doc)
        segments, _, _ = rowbuild.build_segment_rows(doc, "")
        assert segments[0]["words"] is None

    def test_asr_confidence_is_null_because_there_was_no_asr(self):
        doc = _doc([Turn(0, "A", "hello", 0.0, 1.0)])
        segments, _, _ = rowbuild.build_segment_rows(doc, "")
        assert segments[0]["confidence"] is None


class TestEvalMetadata:
    def test_synthetic_meeting_is_flagged_not_measurable(self):
        doc = _doc([Turn(0, "A", "hello")])
        resolve_timings(doc)
        block = rowbuild.eval_metadata(doc, "", "1.0.0", "deadbeef")[rowbuild.RAG_EVAL_KEY]
        assert block["timing_source"] == "synthetic"
        assert block["timings_are_measurements"] is False
        assert block["synthetic_timing_params"]["generator"] == "uniform_rate_v1"

    def test_real_meeting_is_flagged_measurable_and_names_its_reference(self):
        turns = [Turn(i, "A", "x", float(i), i + 0.5) for i in range(4)]
        doc = _doc(turns, TimingInfo(source=TIMING_REAL, reference="icsi:Bdb001"))
        resolve_timings(doc)
        block = rowbuild.eval_metadata(doc, "", "1.0.0", "deadbeef")[rowbuild.RAG_EVAL_KEY]
        assert block["timings_are_measurements"] is True
        assert block["timing_reference"] == "icsi:Bdb001"

    def test_metadata_records_that_there_is_no_media(self):
        doc = _doc([Turn(0, "A", "hello")])
        block = rowbuild.eval_metadata(doc, "", "1.0.0", "deadbeef")[rowbuild.RAG_EVAL_KEY]
        assert block["injected"] is True
        assert block["has_media"] is False
        assert block["content_sha256"] == "deadbeef"


class TestStoragePath:
    def test_it_mirrors_the_production_key_shape(self):
        assert rowbuild.storage_path(3, 77, "ES2004a").startswith("user_3/file_77/")

    def test_the_key_says_there_is_no_media(self):
        assert "no-media" in rowbuild.storage_path(3, 77, "ES2004a")

    def test_hostile_meeting_ids_are_sanitised(self):
        path = rowbuild.storage_path(1, 2, "../../etc/passwd")
        assert ".." not in path.split("/")[-1]
        assert path.count("/") == 2

    def test_the_content_type_is_not_audio_or_video(self):
        """A media MIME type on a row with no media invites a player to try."""
        assert not rowbuild.INJECTED_CONTENT_TYPE.startswith(("audio/", "video/"))


def _build(turns: list[Turn]):
    return rowbuild.build_segment_rows(_doc(turns), "")
