"""Tests for ``app.utils.transcript_comparison`` — vectorized transcript-diff for
pipeline benchmarking (issue #193/#474).

``compare_transcripts`` / ``_build_speaker_mapping`` / ``_empty_comparison`` are pure
dict-in/dict-out functions, so they get exact-value assertions on hand-computed
boundary cases, following the ``test_audio_segment_utils.py`` convention. ``export_baseline``
and ``export_word_reference`` touch the real DB (via the savepoint-isolated ``db_session``
fixture) and the filesystem (via ``tmp_path``), so those write real rows and read the JSON
back off disk rather than mocking either side.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import numpy as np
import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.utils.transcript_comparison import _build_speaker_mapping
from app.utils.transcript_comparison import _empty_comparison
from app.utils.transcript_comparison import compare_transcripts
from app.utils.transcript_comparison import export_baseline
from app.utils.transcript_comparison import export_word_reference


def _make_media_file(db_session, user_id: int) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=fuuid,
        filename=f"comparison-fixture-{fuuid.hex[:8]}.wav",
        storage_path=f"media/comparison-fixture/{fuuid}.wav",
        content_type="audio/wav",
        file_size=1000,
        user_id=user_id,
        status="completed",
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


# ---------------------------------------------------------------------------
# _empty_comparison / compare_transcripts empty-segment edge cases
# ---------------------------------------------------------------------------


class TestEmptyComparison:
    def test_both_empty_is_a_match(self):
        baseline = {"file_id": 1, "segments": []}
        current = {"file_id": 2, "segments": []}
        result = compare_transcripts(baseline, current)

        assert result["baseline_segment_count"] == 0
        assert result["current_segment_count"] == 0
        assert result["segment_count_diff"] == 0
        assert result["text_exact_match_pct"] == 100.0
        assert result["text_word_overlap_avg"] == 100.0
        assert result["speaker_consistency_pct"] == 100.0
        assert result["speaker_mapping"] == {}
        assert result["pass_text"] is True
        assert result["pass_timestamps"] is True
        assert result["pass_speakers"] is True
        assert result["pass_overall"] is True

    def test_baseline_empty_current_not_is_a_mismatch(self):
        baseline = {"file_id": 1, "segments": []}
        current = {"file_id": 2, "segments": [{"start": 0.0, "end": 1.0, "text": "hi"}]}
        result = compare_transcripts(baseline, current)

        assert result["baseline_segment_count"] == 0
        assert result["current_segment_count"] == 1
        assert result["segment_count_diff"] == 1
        assert result["text_exact_match_pct"] == 0.0
        assert result["text_word_overlap_avg"] == 0.0
        assert result["speaker_consistency_pct"] == 0.0
        assert result["pass_overall"] is False

    def test_current_empty_baseline_not_is_a_mismatch(self):
        baseline = {"file_id": 1, "segments": [{"start": 0.0, "end": 1.0, "text": "hi"}]}
        current = {"file_id": 2, "segments": []}
        result = compare_transcripts(baseline, current)

        assert result["baseline_segment_count"] == 1
        assert result["current_segment_count"] == 0
        assert result["segment_count_diff"] == 1
        assert result["pass_overall"] is False

    def test_direct_call_reports_the_requested_match_state_regardless_of_counts(self):
        """``_empty_comparison`` itself just mirrors the ``match`` flag it is given."""
        baseline = {"file_id": 7, "segments": [1, 2, 3]}
        current = {"file_id": 8, "segments": [1]}

        matched = _empty_comparison(baseline, current, match=True)
        assert matched["baseline_segment_count"] == 3
        assert matched["current_segment_count"] == 1
        assert matched["segment_count_diff"] == 2
        assert matched["pass_overall"] is True

        mismatched = _empty_comparison(baseline, current, match=False)
        assert mismatched["segment_count_diff"] == 2
        assert mismatched["pass_overall"] is False


# ---------------------------------------------------------------------------
# compare_transcripts — normal cases, hand-computed
# ---------------------------------------------------------------------------


class TestCompareTranscriptsIdentical:
    def test_identical_segments_pass_everything(self):
        segs = [
            {"start": 0.0, "end": 1.0, "text": "hello world", "speaker_name": "A"},
            {"start": 2.0, "end": 3.0, "text": "foo bar", "speaker_name": "B"},
        ]
        baseline = {"file_id": 1, "segments": segs}
        current = {"file_id": 2, "segments": [dict(s) for s in segs]}

        result = compare_transcripts(baseline, current)

        assert result["segment_count_diff"] == 0
        assert result["timestamp_start_mae_seconds"] == 0.0
        assert result["timestamp_end_mae_seconds"] == 0.0
        assert result["text_exact_match_pct"] == 100.0
        assert result["text_word_overlap_avg"] == 100.0
        assert result["speaker_consistency_pct"] == 100.0
        assert result["speaker_mapping"] == {"A": "A", "B": "B"}
        assert result["pass_text"] is True
        assert result["pass_timestamps"] is True
        assert result["pass_speakers"] is True
        assert result["pass_overall"] is True


class TestCompareTranscriptsDrift:
    """A hand-computed scenario with timestamp drift, a partial text edit, and
    remapped speaker labels — every metric below is worked out by hand in the
    docstring-adjacent comment, not just sanity-checked.
    """

    def test_partial_drift_hand_computed_metrics(self):
        baseline = {
            "file_id": 1,
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "hello world", "speaker_name": "A"},
                {"start": 2.0, "end": 3.0, "text": "foo bar", "speaker_name": "B"},
            ],
        }
        current = {
            "file_id": 2,
            "segments": [
                {"start": 0.1, "end": 1.1, "text": "hello world", "speaker_name": "X"},
                {"start": 2.2, "end": 3.3, "text": "foo baz", "speaker_name": "Y"},
            ],
        }

        result = compare_transcripts(baseline, current)

        # nearest_indices: b[0](0.0) -> c[0](0.1) [|0.0-0.1|=0.1 < |0.0-2.2|=2.2]
        #                  b[1](2.0) -> c[1](2.2) [|2.0-2.2|=0.2 < |2.0-0.1|=1.9]
        # start_mae = mean(0.1, 0.2) = 0.15 ; end_mae = mean(0.1, 0.3) = 0.2
        assert result["timestamp_start_mae_seconds"] == pytest.approx(0.15)
        assert result["timestamp_end_mae_seconds"] == pytest.approx(0.2)

        # text: pair0 exact match, pair1 not -> 1/2 = 50%
        assert result["text_exact_match_pct"] == 50.0
        # word overlap: pair0 = 2/2 = 1.0 ; pair1 {foo,bar} vs {foo,baz} = 1/3
        # avg = mean(1.0, 1/3) * 100 = 66.666...  -> rounds to 66.67
        assert result["text_word_overlap_avg"] == 66.67

        # speaker mapping: A<->X get the only pair carrying overlap 1.0 (pair0),
        # B<->Y get the only remaining pair with overlap 1/3 (pair1) and are the
        # only speakers left, so greedy assignment maps both.
        assert result["speaker_mapping"] == {"A": "X", "B": "Y"}
        assert result["speaker_consistency_pct"] == 100.0

        assert result["pass_text"] is False  # 50 <= 80 and 66.67 <= 85
        assert result["pass_timestamps"] is True  # both MAEs < 5.0
        assert result["pass_speakers"] is True  # 100 > 75
        assert result["pass_overall"] is False

    def test_speaker_consistency_pct_uses_the_mapping_not_raw_label_equality(self):
        """Raw baseline/current labels differ on every segment, but once the mapping
        (B0<->C0) is applied every segment agrees — consistency must read 100%, not 0%.
        """
        baseline = {
            "file_id": 1,
            "segments": [{"start": 0.0, "end": 1.0, "text": "same words", "speaker_name": "B0"}],
        }
        current = {
            "file_id": 2,
            "segments": [{"start": 0.0, "end": 1.0, "text": "same words", "speaker_name": "C0"}],
        }

        result = compare_transcripts(baseline, current)

        assert result["speaker_mapping"] == {"B0": "C0"}
        assert result["speaker_consistency_pct"] == 100.0

    def test_both_texts_empty_counts_as_full_word_overlap(self):
        """The ``b_words or c_words`` guard: two blank segments must not divide by zero
        and must be treated as a perfect (not zero) overlap.
        """
        baseline = {
            "file_id": 1,
            "segments": [{"start": 0.0, "end": 1.0, "text": "   ", "speaker_name": "A"}],
        }
        current = {
            "file_id": 2,
            "segments": [{"start": 0.0, "end": 1.0, "text": "", "speaker_name": "A"}],
        }

        result = compare_transcripts(baseline, current)

        assert result["text_word_overlap_avg"] == 100.0
        assert result["text_exact_match_pct"] == 100.0  # "" == "" after strip()

    def test_pass_text_threshold_via_word_overlap_alone(self):
        """``pass_text`` is an OR: 0% exact match must still pass if word overlap > 85%.

        20 distinct words on each side, differing in exactly one -> intersection=19,
        union=21 -> 19/21 = 90.476...% overlap, while the text is never byte-identical
        so exact-match is 0%.
        """
        base_words = [f"w{i}" for i in range(1, 21)]  # w1..w20
        cur_words = base_words[:19] + ["wX"]  # w1..w19, wX instead of w20

        baseline = {
            "file_id": 1,
            "segments": [
                {"start": 0.0, "end": 1.0, "text": " ".join(base_words), "speaker_name": "A"}
            ],
        }
        current = {
            "file_id": 2,
            "segments": [
                {"start": 0.0, "end": 1.0, "text": " ".join(cur_words), "speaker_name": "A"}
            ],
        }

        result = compare_transcripts(baseline, current)

        assert result["text_exact_match_pct"] == 0.0
        assert result["text_word_overlap_avg"] == pytest.approx(round(19 / 21 * 100, 2))
        assert result["text_word_overlap_avg"] > 85.0
        assert result["pass_text"] is True

    def test_pass_timestamps_threshold_boundary(self):
        """MAE must be STRICTLY less than 5.0 seconds to pass (< not <=)."""
        baseline = {
            "file_id": 1,
            "segments": [{"start": 0.0, "end": 0.0, "text": "x", "speaker_name": "A"}],
        }
        current_at_boundary = {
            "file_id": 2,
            "segments": [{"start": 5.0, "end": 5.0, "text": "x", "speaker_name": "A"}],
        }
        result_at_boundary = compare_transcripts(baseline, current_at_boundary)
        assert result_at_boundary["timestamp_start_mae_seconds"] == 5.0
        assert result_at_boundary["pass_timestamps"] is False

        current_just_under = {
            "file_id": 2,
            "segments": [{"start": 4.99, "end": 4.99, "text": "x", "speaker_name": "A"}],
        }
        result_under = compare_transcripts(baseline, current_just_under)
        assert result_under["timestamp_start_mae_seconds"] == pytest.approx(4.99)
        assert result_under["pass_timestamps"] is True

    def test_multiple_baseline_segments_align_to_the_same_nearest_current_segment(self):
        """Two baseline segments can both nearest-match one current segment — the
        alignment is not required to be one-to-one.
        """
        baseline = {
            "file_id": 1,
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "same text", "speaker_name": "A"},
                {"start": 0.4, "end": 1.4, "text": "same text", "speaker_name": "A"},
            ],
        }
        current = {
            "file_id": 2,
            "segments": [{"start": 0.2, "end": 1.2, "text": "same text", "speaker_name": "A"}],
        }
        result = compare_transcripts(baseline, current)

        assert result["baseline_segment_count"] == 2
        assert result["current_segment_count"] == 1
        assert result["segment_count_diff"] == 1
        # both baseline segments align to the single current segment
        assert result["timestamp_start_mae_seconds"] == pytest.approx(0.2)
        assert result["text_exact_match_pct"] == 100.0


# ---------------------------------------------------------------------------
# _build_speaker_mapping — direct tests of the greedy assignment
# ---------------------------------------------------------------------------


class TestBuildSpeakerMapping:
    def test_no_baseline_speakers_returns_empty_mapping(self):
        assert _build_speaker_mapping([], [{"speaker_name": "X", "text": "hi"}], np.array([])) == {}

    def test_no_current_speakers_returns_empty_mapping(self):
        b_segs = [{"speaker_name": "A", "text": "hi"}]
        assert _build_speaker_mapping(b_segs, [], np.array([0])) == {}

    def test_tied_max_overlap_picks_first_encountered_bi_then_cj(self):
        """A vs X and B vs X are BOTH overlap 1.0 (only one current speaker exists),
        so the tie-break must go to the first baseline speaker in sorted order (A),
        and B is left unmapped because no other current speaker remains.
        """
        b_segs = [
            {"speaker_name": "A", "text": "one two three"},
            {"speaker_name": "B", "text": "one two three"},
        ]
        c_segs = [{"speaker_name": "X", "text": "one two three"}]
        nearest_indices = np.array([0, 0])

        mapping = _build_speaker_mapping(b_segs, c_segs, nearest_indices)

        assert mapping == {"A": "X"}

    def test_higher_overlap_wins_the_shared_current_speaker(self):
        """A has a perfect match, B a partial one; both align to the only current
        speaker. A must win X; B has nowhere left to go.
        """
        b_segs = [
            {"speaker_name": "A", "text": "one two three"},
            {"speaker_name": "B", "text": "one two"},
        ]
        c_segs = [{"speaker_name": "X", "text": "one two three"}]
        nearest_indices = np.array([0, 0])

        mapping = _build_speaker_mapping(b_segs, c_segs, nearest_indices)

        assert mapping == {"A": "X"}

    def test_zero_overlap_everywhere_maps_nothing(self):
        b_segs = [{"speaker_name": "A", "text": "zzz"}]
        c_segs = [{"speaker_name": "X", "text": "completely different words"}]
        nearest_indices = np.array([0])

        mapping = _build_speaker_mapping(b_segs, c_segs, nearest_indices)

        assert mapping == {}

    def test_stops_entirely_once_the_best_remaining_score_is_zero(self):
        """Once the round's best available (bi, cj) pair scores 0, the loop breaks —
        it does NOT keep looking for a later positive-scoring pair for a different
        baseline speaker. C never even gets a chance even though it might have scored
        positively against a speaker that was already claimed.
        """
        b_segs = [
            {"speaker_name": "A", "text": "apple banana"},
            {"speaker_name": "B", "text": "apple banana"},
            {"speaker_name": "C", "text": "zzz"},
        ]
        c_segs = [
            {"speaker_name": "X", "text": "apple banana"},
            {"speaker_name": "Y", "text": "apple cherry"},
        ]
        nearest_indices = np.array([0, 0, 1])

        mapping = _build_speaker_mapping(b_segs, c_segs, nearest_indices)

        # Round 1: (A,X)=1.0 and (B,X)=1.0 tie -> A (first) wins X.
        # Round 2: only column Y is free; (B,Y)=0.0 and (C,Y)=0.0 tie at 0 -> break.
        assert mapping == {"A": "X"}

    def test_three_speakers_each_side_full_bijection(self):
        b_segs = [
            {"speaker_name": "A", "text": "apple"},
            {"speaker_name": "B", "text": "banana"},
            {"speaker_name": "C", "text": "cherry"},
        ]
        c_segs = [
            {"speaker_name": "X", "text": "cherry"},
            {"speaker_name": "Y", "text": "apple"},
            {"speaker_name": "Z", "text": "banana"},
        ]
        nearest_indices = np.array([1, 2, 0])  # A->Y(apple), B->Z(banana), C->X(cherry)

        mapping = _build_speaker_mapping(b_segs, c_segs, nearest_indices)

        assert mapping == {"A": "Y", "B": "Z", "C": "X"}


# ---------------------------------------------------------------------------
# export_baseline — real DB + filesystem
# ---------------------------------------------------------------------------


class TestExportBaseline:
    def test_exports_segments_speakers_and_writes_the_json_file(
        self, db_session, normal_user, tmp_path
    ):
        media_file = _make_media_file(db_session, int(normal_user.id))
        speaker = Speaker(
            uuid=uuid_pkg.uuid4(),
            name="SPEAKER_00",
            display_name="Alice",
            user_id=normal_user.id,
            media_file_id=media_file.id,
        )
        db_session.add(speaker)
        db_session.commit()
        db_session.refresh(speaker)

        seg1 = TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media_file.id,
            speaker_id=speaker.id,
            start_time=0.0,
            end_time=1.5,
            text="hello there",
            is_overlap=False,
            words=[{"word": "hello", "start": 0.0, "end": 0.5}],
        )
        seg2 = TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media_file.id,
            speaker_id=None,
            start_time=1.5,
            end_time=2.5,
            text="general kenobi",
            is_overlap=True,
        )
        db_session.add_all([seg1, seg2])
        db_session.commit()

        output_path = tmp_path / "baseline.json"
        result = export_baseline(db_session, media_file.id, str(output_path))

        assert result["file_id"] == media_file.id
        assert result["segment_count"] == 2
        assert result["speaker_count"] == 1
        assert len(result["segments"]) == 2

        # Ordered by (start_time, end_time, id) — seg1 (0.0) must come before seg2 (1.5).
        assert result["segments"][0]["start"] == 0.0
        assert result["segments"][0]["text"] == "hello there"
        assert result["segments"][0]["speaker_name"] == "SPEAKER_00"
        assert result["segments"][0]["is_overlap"] is False
        assert result["segments"][0]["words"] == [{"word": "hello", "start": 0.0, "end": 0.5}]

        assert result["segments"][1]["start"] == 1.5
        assert result["segments"][1]["speaker_name"] == "UNKNOWN"  # speaker_id is None
        assert result["segments"][1]["is_overlap"] is True
        assert result["segments"][1]["words"] == []  # words was never set -> None -> []

        assert result["speakers"] == [
            {"id": speaker.id, "name": "SPEAKER_00", "display_name": "Alice"}
        ]

        assert output_path.exists()
        on_disk = json.loads(output_path.read_text())
        assert on_disk["segment_count"] == 2
        assert on_disk["speakers"] == result["speakers"]
        assert on_disk["segments"] == result["segments"]

    def test_file_with_no_segments_or_speakers_exports_empty_lists(
        self, db_session, normal_user, tmp_path
    ):
        media_file = _make_media_file(db_session, int(normal_user.id))
        output_path = tmp_path / "empty_baseline.json"

        result = export_baseline(db_session, media_file.id, str(output_path))

        assert result["segment_count"] == 0
        assert result["speaker_count"] == 0
        assert result["segments"] == []
        assert result["speakers"] == []


# ---------------------------------------------------------------------------
# export_word_reference — real DB + filesystem, plus the RTTM conversion
# ---------------------------------------------------------------------------


class TestExportWordReference:
    def test_exports_word_level_reference_and_drops_untimed_words(
        self, db_session, normal_user, tmp_path
    ):
        media_file = _make_media_file(db_session, int(normal_user.id))
        speaker = Speaker(
            uuid=uuid_pkg.uuid4(),
            name="SPEAKER_00",
            display_name="Alice",
            user_id=normal_user.id,
            media_file_id=media_file.id,
        )
        db_session.add(speaker)
        db_session.commit()
        db_session.refresh(speaker)

        seg = TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media_file.id,
            speaker_id=speaker.id,
            start_time=0.0,
            end_time=2.0,
            text="hello there friend",
            words=[
                {"word": "hello", "start": 0.0, "end": 0.4},
                {"word": "there", "start": 0.4, "end": 0.8},
                # No timing at all -> must be dropped entirely.
                {"word": "friend"},
            ],
        )
        db_session.add(seg)
        db_session.commit()

        words_path = tmp_path / "reference.words.json"
        rttm_path = tmp_path / "reference.rttm"

        result = export_word_reference(db_session, media_file.id, str(words_path), str(rttm_path))

        assert result["word_count"] == 2
        assert result["speaker_count"] == 1
        assert result["words_json"] == str(words_path)
        assert result["rttm"] == str(rttm_path)

        on_disk_words = json.loads(words_path.read_text())
        assert on_disk_words == [
            {"start": 0.0, "end": 0.4, "word": "hello", "speaker": "Alice"},
            {"start": 0.4, "end": 0.8, "word": "there", "speaker": "Alice"},
        ]

        # The two words are 0.4-0.8, consecutive, same speaker, gap 0.0 <= 0.5 ->
        # collapsed into a single RTTM SPEAKER line spanning 0.0 to 0.8.
        rttm_text = rttm_path.read_text()
        assert (
            rttm_text == f"SPEAKER file_{media_file.id} 1 0.000 0.800 <NA> <NA> Alice <NA> <NA>\n"
        )

    def test_speaker_name_falls_back_to_name_when_no_display_name(
        self, db_session, normal_user, tmp_path
    ):
        media_file = _make_media_file(db_session, int(normal_user.id))
        speaker = Speaker(
            uuid=uuid_pkg.uuid4(),
            name="SPEAKER_01",
            display_name=None,
            user_id=normal_user.id,
            media_file_id=media_file.id,
        )
        db_session.add(speaker)
        db_session.commit()
        db_session.refresh(speaker)

        seg = TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media_file.id,
            speaker_id=speaker.id,
            start_time=0.0,
            end_time=1.0,
            text="hi",
            words=[{"word": "hi", "start": 0.0, "end": 0.3}],
        )
        db_session.add(seg)
        db_session.commit()

        words_path = tmp_path / "words.json"
        result = export_word_reference(db_session, media_file.id, str(words_path))

        assert result["rttm"] is None  # out_rttm not provided
        on_disk = json.loads(words_path.read_text())
        assert on_disk == [{"start": 0.0, "end": 0.3, "word": "hi", "speaker": "SPEAKER_01"}]

    def test_no_segments_exports_empty_word_list(self, db_session, normal_user, tmp_path):
        media_file = _make_media_file(db_session, int(normal_user.id))
        words_path = tmp_path / "empty_words.json"

        result = export_word_reference(db_session, media_file.id, str(words_path))

        assert result["word_count"] == 0
        assert result["speaker_count"] == 0
        assert json.loads(words_path.read_text()) == []
