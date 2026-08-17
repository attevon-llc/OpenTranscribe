"""Tests for ``app/utils/diarization_merge.py`` (issue #474).

Pure function, no I/O: assigns diarization speaker labels onto an independent
ASR transcript via a bisect-based midpoint lookup, then majority-votes a
segment-level speaker from its words. Zero test coverage before this file.
"""

from __future__ import annotations

import pytest

from app.services.asr.types import ASRResult
from app.services.asr.types import ASRSegment
from app.services.asr.types import ASRWord
from app.services.diarization.types import DiarizeResult
from app.services.diarization.types import DiarizeSegment
from app.utils.diarization_merge import merge_cloud_diarization

pytestmark = pytest.mark.unit


def _word(word: str, start: float, end: float, confidence: float = 1.0) -> ASRWord:
    return ASRWord(word=word, start=start, end=end, confidence=confidence)


def _dseg(start: float, end: float, speaker: str) -> DiarizeSegment:
    return DiarizeSegment(start=start, end=end, speaker=speaker)


def _asr(segments: list[ASRSegment], **kwargs) -> ASRResult:
    return ASRResult(segments=segments, language="en", **kwargs)


def _diarize(segments: list[DiarizeSegment], **kwargs) -> DiarizeResult:
    return DiarizeResult(
        segments=segments,
        num_speakers=len({s.speaker for s in segments}),
        provider_name="pyannote",
        **kwargs,
    )


# =============================================================================
# Empty diarization — pass-through
# =============================================================================
def test_empty_diarization_segments_returns_the_original_asr_result_unchanged():
    asr = _asr([ASRSegment(text="hi", start=0.0, end=1.0, words=[_word("hi", 0.0, 1.0)])])
    diarize = _diarize([])

    result = merge_cloud_diarization(asr, diarize)

    assert result is asr


# =============================================================================
# Majority vote at the segment level
# =============================================================================
def test_segment_speaker_is_the_majority_of_its_word_speakers():
    diarize = _diarize([_dseg(0.0, 5.0, "SPEAKER_00"), _dseg(5.0, 10.0, "SPEAKER_01")])
    seg = ASRSegment(
        text="hello there friend",
        start=0.0,
        end=7.0,
        words=[
            _word("hello", 0.0, 1.0),  # mid 0.5 -> SPEAKER_00
            _word("there", 1.0, 2.0),  # mid 1.5 -> SPEAKER_00
            _word("friend", 6.0, 7.0),  # mid 6.5 -> SPEAKER_01
        ],
    )
    asr = _asr([seg])

    result = merge_cloud_diarization(asr, diarize)

    assert len(result.segments) == 1
    merged = result.segments[0]
    assert merged.speaker == "SPEAKER_00"
    assert merged.text == "hello there friend"
    assert merged.start == 0.0
    assert merged.end == 7.0


def test_tied_vote_keeps_the_first_encountered_speaker():
    """``max(counts, key=counts.get)`` breaks ties by dict insertion order, and
    ``counts`` is built by iterating words in order — so the speaker whose word
    comes first in the segment wins a 1-1 tie."""
    diarize = _diarize([_dseg(0.0, 5.0, "SPEAKER_00"), _dseg(5.0, 10.0, "SPEAKER_01")])
    seg = ASRSegment(
        text="a b",
        start=0.0,
        end=6.0,
        words=[
            _word("a", 0.0, 1.0),  # mid 0.5 -> SPEAKER_00 (first)
            _word("b", 5.0, 6.0),  # mid 5.5 -> SPEAKER_01
        ],
    )
    asr = _asr([seg])

    result = merge_cloud_diarization(asr, diarize)

    assert result.segments[0].speaker == "SPEAKER_00"


def test_words_falling_outside_any_diarization_segment_are_excluded_from_the_vote():
    diarize = _diarize([_dseg(10.0, 20.0, "SPEAKER_00")])
    seg = ASRSegment(
        text="early word",
        start=0.0,
        end=1.0,
        words=[_word("early", 0.0, 1.0)],  # mid 0.5, no covering segment
    )
    asr = _asr([seg])

    result = merge_cloud_diarization(asr, diarize)

    assert result.segments[0].speaker is None
    assert result.has_speakers is False


def test_words_preserve_text_timestamps_and_confidence_but_carry_no_speaker_field():
    diarize = _diarize([_dseg(0.0, 5.0, "SPEAKER_00")])
    seg = ASRSegment(
        text="word",
        start=0.0,
        end=1.0,
        words=[_word("word", 0.1, 0.9, confidence=0.87)],
    )
    asr = _asr([seg])

    result = merge_cloud_diarization(asr, diarize)

    new_word = result.segments[0].words[0]
    assert new_word.word == "word"
    assert new_word.start == 0.1
    assert new_word.end == 0.9
    assert new_word.confidence == 0.87
    assert not hasattr(new_word, "speaker")


# =============================================================================
# No-words fallback — segment midpoint
# =============================================================================
def test_segment_with_no_words_falls_back_to_segment_midpoint():
    diarize = _diarize([_dseg(0.0, 10.0, "SPEAKER_02")])
    seg = ASRSegment(text="", start=2.0, end=4.0, words=[])  # midpoint 3.0
    asr = _asr([seg])

    result = merge_cloud_diarization(asr, diarize)

    assert result.segments[0].speaker == "SPEAKER_02"


def test_segment_with_no_words_and_midpoint_outside_any_segment_gets_none():
    diarize = _diarize([_dseg(0.0, 1.0, "SPEAKER_00")])
    seg = ASRSegment(text="", start=5.0, end=7.0, words=[])  # midpoint 6.0, uncovered
    asr = _asr([seg])

    result = merge_cloud_diarization(asr, diarize)

    assert result.segments[0].speaker is None


# =============================================================================
# Bisect lookup correctness — multiple diarization segments, unsorted input
# =============================================================================
def test_diarization_segments_are_sorted_before_lookup_even_if_given_out_of_order():
    diarize = _diarize(
        [
            _dseg(10.0, 15.0, "SPEAKER_01"),  # given out of chronological order
            _dseg(0.0, 5.0, "SPEAKER_00"),
        ]
    )
    seg = ASRSegment(
        text="late word",
        start=11.0,
        end=12.0,
        words=[_word("late", 11.0, 12.0)],  # mid 11.5 -> SPEAKER_01
    )
    asr = _asr([seg])

    result = merge_cloud_diarization(asr, diarize)

    assert result.segments[0].speaker == "SPEAKER_01"


def test_midpoint_falling_in_a_gap_between_segments_returns_none():
    diarize = _diarize([_dseg(0.0, 5.0, "SPEAKER_00"), _dseg(7.0, 10.0, "SPEAKER_01")])
    seg = ASRSegment(
        text="gap word",
        start=5.5,
        end=6.5,
        words=[_word("gap", 5.5, 6.5)],  # mid 6.0, in the 5.0-7.0 gap
    )
    asr = _asr([seg])

    result = merge_cloud_diarization(asr, diarize)

    assert result.segments[0].speaker is None


def test_midpoint_exactly_on_a_shared_boundary_goes_to_the_later_segment():
    diarize = _diarize([_dseg(0.0, 5.0, "SPEAKER_00"), _dseg(5.0, 10.0, "SPEAKER_01")])
    seg = ASRSegment(
        text="boundary word",
        start=4.0,
        end=6.0,
        words=[_word("boundary", 4.0, 6.0)],  # mid exactly 5.0
    )
    asr = _asr([seg])

    result = merge_cloud_diarization(asr, diarize)

    assert result.segments[0].speaker == "SPEAKER_01"


# =============================================================================
# Multi-segment result assembly, unique speakers, has_speakers
# =============================================================================
def test_multiple_segments_each_get_their_own_speaker_and_order_is_preserved():
    diarize = _diarize([_dseg(0.0, 5.0, "SPEAKER_00"), _dseg(5.0, 10.0, "SPEAKER_01")])
    asr = _asr(
        [
            ASRSegment(text="first", start=0.0, end=1.0, words=[_word("first", 0.0, 1.0)]),
            ASRSegment(text="second", start=6.0, end=7.0, words=[_word("second", 6.0, 7.0)]),
        ]
    )

    result = merge_cloud_diarization(asr, diarize)

    assert [s.text for s in result.segments] == ["first", "second"]
    assert [s.speaker for s in result.segments] == ["SPEAKER_00", "SPEAKER_01"]
    assert result.has_speakers is True


def test_has_speakers_is_false_when_no_segment_gets_a_speaker():
    diarize = _diarize([_dseg(100.0, 105.0, "SPEAKER_00")])
    asr = _asr([ASRSegment(text="x", start=0.0, end=1.0, words=[_word("x", 0.0, 1.0)])])

    result = merge_cloud_diarization(asr, diarize)

    assert result.has_speakers is False
    assert result.segments[0].speaker is None


# =============================================================================
# Preserved / merged metadata
# =============================================================================
def test_language_provider_and_model_name_are_preserved_from_the_asr_result():
    diarize = _diarize([_dseg(0.0, 5.0, "SPEAKER_00")], model_name="pyannote-3.1")
    asr = _asr(
        [ASRSegment(text="x", start=0.0, end=1.0, words=[_word("x", 0.0, 1.0)])],
        provider_name="deepgram",
        model_name="nova-2",
    )

    result = merge_cloud_diarization(asr, diarize)

    assert result.language == "en"
    assert result.provider_name == "deepgram"
    assert result.model_name == "nova-2"


def test_metadata_is_merged_with_diarization_provenance_and_originals_preserved():
    diarize = _diarize([_dseg(0.0, 5.0, "SPEAKER_00")], model_name="pyannote-3.1")
    asr = _asr(
        [ASRSegment(text="x", start=0.0, end=1.0, words=[_word("x", 0.0, 1.0)])],
        metadata={"existing_key": "existing_value"},
    )

    result = merge_cloud_diarization(asr, diarize)

    assert result.metadata["existing_key"] == "existing_value"
    assert result.metadata["diarization_provider"] == "pyannote"
    assert result.metadata["diarization_model"] == "pyannote-3.1"


def test_confidence_is_preserved_on_the_merged_segment():
    diarize = _diarize([_dseg(0.0, 5.0, "SPEAKER_00")])
    seg = ASRSegment(text="x", start=0.0, end=1.0, confidence=0.42, words=[_word("x", 0.0, 1.0)])
    asr = _asr([seg])

    result = merge_cloud_diarization(asr, diarize)

    assert result.segments[0].confidence == 0.42
