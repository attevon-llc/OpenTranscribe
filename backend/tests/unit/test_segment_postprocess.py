"""Tests for segment post-processing: resegmentation and merging."""

from copy import deepcopy

import pytest

from app.utils.segment_postprocess import merge_consecutive_segments
from app.utils.segment_postprocess import resegment_by_speaker


class TestResegmentBySpeaker:
    def test_empty_input(self):
        assert resegment_by_speaker([]) == []

    def test_single_speaker_passthrough(self):
        seg = {
            "start": 0.0,
            "end": 2.0,
            "text": "hello world",
            "speaker": "SPEAKER_00",
            "words": [
                {"word": "hello", "start": 0.0, "end": 0.5, "speaker": "SPEAKER_00"},
                {"word": " world", "start": 0.5, "end": 1.0, "speaker": "SPEAKER_00"},
            ],
        }
        result = resegment_by_speaker([seg])
        assert len(result) == 1
        assert result[0] is seg  # unchanged, same object

    def test_no_words_passthrough(self):
        seg = {"start": 0.0, "end": 2.0, "text": "hello", "speaker": "SPEAKER_00"}
        result = resegment_by_speaker([seg])
        assert len(result) == 1
        assert result[0] is seg

    def test_mixed_speakers_split(self):
        seg = {
            "start": 0.0,
            "end": 3.0,
            "text": "I agree let's move on",
            "speaker": "SPEAKER_00",
            "confidence": 0.9,
            "words": [
                {"word": "I", "start": 0.0, "end": 0.3, "speaker": "SPEAKER_00"},
                {"word": " agree", "start": 0.3, "end": 0.8, "speaker": "SPEAKER_00"},
                {"word": " let's", "start": 1.0, "end": 1.5, "speaker": "SPEAKER_01"},
                {"word": " move", "start": 1.5, "end": 2.0, "speaker": "SPEAKER_01"},
                {"word": " on", "start": 2.0, "end": 2.5, "speaker": "SPEAKER_01"},
            ],
        }
        result = resegment_by_speaker([seg])
        assert len(result) == 2
        assert result[0]["speaker"] == "SPEAKER_00"
        assert result[0]["text"] == "I agree"
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 0.8
        assert len(result[0]["words"]) == 2
        assert result[1]["speaker"] == "SPEAKER_01"
        assert result[1]["text"] == "let's move on"
        assert result[1]["start"] == 1.0
        assert result[1]["end"] == 2.5
        assert len(result[1]["words"]) == 3

    def test_three_speaker_changes(self):
        seg = {
            "start": 0.0,
            "end": 3.0,
            "text": "a b c",
            "speaker": "SPEAKER_00",
            "confidence": 0.8,
            "words": [
                {"word": "a", "start": 0.0, "end": 0.5, "speaker": "SPEAKER_00"},
                {"word": " b", "start": 1.0, "end": 1.5, "speaker": "SPEAKER_01"},
                {"word": " c", "start": 2.0, "end": 2.5, "speaker": "SPEAKER_00"},
            ],
        }
        result = resegment_by_speaker([seg])
        assert len(result) == 3
        assert [r["speaker"] for r in result] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]


class TestMergeConsecutiveSegments:
    def test_empty_input(self):
        assert merge_consecutive_segments([]) == []

    def test_single_segment(self):
        seg = {"start": 0.0, "end": 1.0, "text": "hello", "speaker": "SPEAKER_00", "words": []}
        result = merge_consecutive_segments([seg])
        assert len(result) == 1
        assert result[0]["text"] == "hello"

    def test_merge_same_speaker(self):
        segs = [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "hello",
                "speaker": "SPEAKER_00",
                "words": [{"word": "hello", "start": 0.0, "end": 1.0}],
            },
            {
                "start": 1.0,
                "end": 2.0,
                "text": "world",
                "speaker": "SPEAKER_00",
                "words": [{"word": "world", "start": 1.0, "end": 2.0}],
            },
        ]
        result = merge_consecutive_segments(segs)
        assert len(result) == 1
        assert result[0]["text"] == "hello world"
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 2.0
        assert len(result[0]["words"]) == 2

    def test_no_merge_different_speakers(self):
        segs = [
            {"start": 0.0, "end": 1.0, "text": "hello", "speaker": "SPEAKER_00"},
            {"start": 1.0, "end": 2.0, "text": "world", "speaker": "SPEAKER_01"},
        ]
        result = merge_consecutive_segments(segs)
        assert len(result) == 2

    def test_no_merge_none_speaker(self):
        segs = [
            {"start": 0.0, "end": 1.0, "text": "a", "speaker": None},
            {"start": 1.0, "end": 2.0, "text": "b", "speaker": None},
        ]
        result = merge_consecutive_segments(segs)
        assert len(result) == 2  # None speakers should not merge

    def test_mixed_merge_pattern(self):
        segs = [
            {"start": 0.0, "end": 1.0, "text": "a", "speaker": "SPEAKER_00"},
            {"start": 1.0, "end": 2.0, "text": "b", "speaker": "SPEAKER_00"},
            {"start": 2.0, "end": 3.0, "text": "c", "speaker": "SPEAKER_01"},
            {"start": 3.0, "end": 4.0, "text": "d", "speaker": "SPEAKER_01"},
            {"start": 4.0, "end": 5.0, "text": "e", "speaker": "SPEAKER_00"},
        ]
        result = merge_consecutive_segments(segs)
        assert len(result) == 3
        assert result[0]["text"] == "a b"
        assert result[1]["text"] == "c d"
        assert result[2]["text"] == "e"


class TestPipelineIntegration:
    """Test resegment + merge together as they're used in the pipeline."""

    def test_resegment_then_merge(self):
        """Mixed-speaker segment followed by same-speaker segment should merge after resegment."""
        segs = [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "yes no",
                "speaker": "SPEAKER_00",
                "confidence": 0.9,
                "words": [
                    {"word": "yes", "start": 0.0, "end": 0.5, "speaker": "SPEAKER_00"},
                    {"word": " no", "start": 1.0, "end": 1.5, "speaker": "SPEAKER_01"},
                ],
            },
            {
                "start": 2.0,
                "end": 3.0,
                "text": "maybe",
                "speaker": "SPEAKER_01",
                "confidence": 0.9,
                "words": [
                    {"word": "maybe", "start": 2.0, "end": 2.5, "speaker": "SPEAKER_01"},
                ],
            },
        ]
        result = merge_consecutive_segments(resegment_by_speaker(segs))
        assert len(result) == 2
        assert result[0]["speaker"] == "SPEAKER_00"
        assert result[0]["text"] == "yes"
        assert result[1]["speaker"] == "SPEAKER_01"
        assert result[1]["text"] == "no maybe"


def _import_finalize():
    """Import ``finalize_segments``.

    This used to be a try/except that turned an ImportError into ``pytest.skip`` "not
    available yet". Boundary smoothing shipped (issue #193, default ON), so the guard only
    ever masked a rename: 15 tests in this module would have silently reported as skipped
    while the feature went untested. A plain import fails loudly instead (issue #431).
    """
    from app.utils.segment_postprocess import finalize_segments

    return finalize_segments


def _import_smoothing_config():
    """Import ``BoundarySmoothingConfig`` — see ``_import_finalize`` on why not guarded."""
    from app.transcription.boundary_resolver import BoundarySmoothingConfig

    return BoundarySmoothingConfig


def _mixed_segment() -> dict:
    return {
        "start": 0.0,
        "end": 3.0,
        "text": "I agree let's move on",
        "speaker": "SPEAKER_00",
        "confidence": 0.9,
        "words": [
            {"word": "I", "start": 0.0, "end": 0.3, "speaker": "SPEAKER_00"},
            {"word": " agree", "start": 0.3, "end": 0.8, "speaker": "SPEAKER_00"},
            {"word": " let's", "start": 1.0, "end": 1.5, "speaker": "SPEAKER_01"},
            {"word": " move", "start": 1.5, "end": 2.0, "speaker": "SPEAKER_01"},
            {"word": " on", "start": 2.0, "end": 2.5, "speaker": "SPEAKER_01"},
        ],
    }


def _bleed_segment() -> dict:
    """A run with a 2-word B-island flanked by A on both sides (smoother target).

    0.4 s words on a 0.5 s grid keep the 2-word island under the smoother's 1.5 s
    duration cap and seam gaps under min_silent_gap, so ON collapses it.
    """
    words = []
    for i in range(10):
        spk = "SPEAKER_01" if i in (4, 5) else "SPEAKER_00"
        words.append({"word": f" w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4, "speaker": spk})
    return {
        "start": 0.0,
        "end": words[-1]["end"],
        "text": "".join(str(w["word"]) for w in words).strip(),
        "speaker": "SPEAKER_00",
        "confidence": 0.9,
        "words": words,
    }


class TestFinalizeSegmentsRefactorSafety:
    """finalize_segments(segs, None) must equal the legacy resegment+merge pipeline."""

    @pytest.mark.parametrize(
        "segments",
        [
            [_mixed_segment()],
            [
                _mixed_segment(),
                {
                    "start": 3.0,
                    "end": 4.0,
                    "text": "maybe",
                    "speaker": "SPEAKER_01",
                    "confidence": 0.9,
                    "words": [{"word": "maybe", "start": 3.0, "end": 3.5, "speaker": "SPEAKER_01"}],
                },
            ],
        ],
    )
    def test_no_smoothing_matches_legacy_pipeline(self, segments):
        finalize_segments = _import_finalize()
        legacy = merge_consecutive_segments(resegment_by_speaker(deepcopy(segments)))
        finalized = finalize_segments(deepcopy(segments), None)
        # Byte-identical via JSON canonicalization (order + content must match exactly).
        import json

        assert json.dumps(finalized, sort_keys=True) == json.dumps(legacy, sort_keys=True)

    def test_empty_input(self):
        finalize_segments = _import_finalize()
        assert finalize_segments([], None) == []


class TestFinalizeSegmentsSmoothingIdempotent:
    """Applying smoothing twice equals applying it once (stable fixed point)."""

    def test_smoothing_idempotent(self):
        finalize_segments = _import_finalize()
        BoundarySmoothingConfig = _import_smoothing_config()  # noqa: N806  # class, not a value
        cfg = BoundarySmoothingConfig(
            enabled=True,
            max_island_words=3,
            max_island_duration=1.5,
            min_flank_words=3,
            min_silent_gap=0.4,
        )
        segments = [_bleed_segment()]
        once = finalize_segments(deepcopy(segments), cfg)
        twice = finalize_segments(deepcopy(once), cfg)
        import json

        assert json.dumps(twice, sort_keys=True) == json.dumps(once, sort_keys=True)
