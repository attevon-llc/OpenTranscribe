"""``utils.speaker_stats`` — the one talk-time tally both planes are built on (D11)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.utils.speaker_stats import SpeakerTally
from app.utils.speaker_stats import count_words
from app.utils.speaker_stats import segment_duration
from app.utils.speaker_stats import tally_by_speaker

pytestmark = pytest.mark.unit


def _seg(speaker_id, label, text, start, end):
    return SimpleNamespace(
        speaker_id=speaker_id, label=label, text=text, start_time=start, end_time=end
    )


@pytest.mark.parametrize(
    ("start", "end", "seconds"),
    [(1.0, 3.5, 2.5), (0.0, None, 0.0), (None, 5.0, 0.0), (None, None, 0.0), (4.0, 3.0, -1.0)],
)
def test_segment_duration_counts_a_missing_time_as_no_talk_time(start, end, seconds):
    assert segment_duration(SimpleNamespace(start_time=start, end_time=end)) == seconds


@pytest.mark.parametrize(
    ("text", "words"),
    [(None, 0), ("", 0), ("  ", 0), ("a b", 2), ("a\u00a0b\tc\nd", 4), ("\u200b", 1)],
)
def test_count_words_splits_on_unicode_whitespace(text, words):
    assert count_words(text) == words


def test_speakers_are_tallied_by_id_and_merged_only_when_their_labels_collide():
    segments = [
        _seg(1, "Alice", "a b c", 0.0, 2.0),
        _seg(2, "Bob", "d", 2.0, 3.0),
        _seg(3, "Alice", "e f", 3.0, 4.5),
        _seg(1, "Alice", "g", 4.5, 5.0),
    ]
    calls: list[int] = []

    def label(segment):
        calls.append(segment.speaker_id)
        return segment.label

    tallies = tally_by_speaker(segments, label)

    assert tallies == {
        "Alice": SpeakerTally(total_time=4.0, segment_count=3, word_count=6),
        "Bob": SpeakerTally(total_time=1.0, segment_count=1, word_count=1),
    }
    assert list(tallies) == ["Alice", "Bob"]
    # A label is resolved once per speaker, never once per segment.
    assert calls == [1, 2, 3]


def test_a_segment_without_a_speaker_id_is_keyed_by_its_label():
    segments = [
        _seg(None, "Unknown Speaker", "x", 0.0, 1.0),
        _seg(None, "Unknown Speaker", "y z", 1.0, 3.0),
        _seg(7, "Carol", "w", 3.0, 4.0),
    ]

    tallies = tally_by_speaker(segments, lambda s: s.label)

    assert tallies == {
        "Unknown Speaker": SpeakerTally(total_time=3.0, segment_count=2, word_count=3),
        "Carol": SpeakerTally(total_time=1.0, segment_count=1, word_count=1),
    }


def test_no_segments_is_no_speakers():
    assert tally_by_speaker([], lambda s: s.label) == {}
