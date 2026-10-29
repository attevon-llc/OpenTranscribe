"""Talk time as the two planes report it, pinned on one shared transcript (D11).

``compute_speaker_stats`` (display labels; LLM prompts and ``file_facts`` -> chat) and
``AnalyticsService._compute_from_segments`` (raw diarizer labels; the analytics panel) are
fed the SAME segments here, so a change to the arithmetic either plane shares shows up as a
changed literal below, not as a silent drift between the two.
"""

from __future__ import annotations

import pytest

from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.services.analytics_service import AnalyticsService
from app.utils.transcript_builders import compute_speaker_stats

pytestmark = pytest.mark.unit


def _speaker(speaker_id: int, name: str, display_name: str | None = None) -> Speaker:
    return Speaker(
        id=speaker_id,
        name=name,
        display_name=display_name,
        verified=display_name is not None,
        identity_source="human" if display_name else None,
    )


def _segment(
    speaker: Speaker | None, text: str, start: float | None, end: float | None
) -> TranscriptSegment:
    return TranscriptSegment(
        speaker=speaker,
        speaker_id=speaker.id if speaker else None,
        text=text,
        start_time=start,
        end_time=end,
    )


S00 = _speaker(1, "SPEAKER_00", "Alice")
S01 = _speaker(2, "SPEAKER_01", "Bob")
# The diarizer split Alice in two; a human renamed the second half to the same name.
S02 = _speaker(3, "SPEAKER_02", "Alice")
S03 = _speaker(4, "SPEAKER_03")

MEETING = [
    _segment(S00, "Hello there everyone.", 0.0, 4.0),
    _segment(S01, "Thanks Alice.", 3.5, 6.0),  # starts before Alice ends: an interruption
    _segment(S02, "I agree with\u00a0that", 6.0, 9.0),  # NBSP is whitespace to str.split
    _segment(S00, "Let us begin?", 10.0, 12.5),
    _segment(None, "[inaudible]", 12.5, 13.0),
    _segment(S03, "  tabs\tand\nnewlines \u200b ", 13.0, 15.25),  # ZWSP is NOT whitespace
    _segment(S03, "", 15.25, 15.5),
]


def test_display_label_plane_merges_speakers_that_share_a_label():
    stats = compute_speaker_stats(MEETING, voiceprint_names=True)

    assert list(stats) == ["Alice", "Bob", "Unknown Speaker", "SPEAKER_03"]
    assert stats["Alice"] == {
        "total_time": 9.5,
        "segment_count": 3,
        "word_count": 10,
        "percentage": 9.5 / 15.0 * 100,
    }
    assert stats["Bob"] == {
        "total_time": 2.5,
        "segment_count": 1,
        "word_count": 2,
        "percentage": 2.5 / 15.0 * 100,
    }
    assert stats["Unknown Speaker"]["total_time"] == 0.5
    assert stats["Unknown Speaker"]["word_count"] == 1
    assert stats["SPEAKER_03"] == {
        "total_time": 2.5,
        "segment_count": 2,
        "word_count": 4,
        "percentage": 2.5 / 15.0 * 100,
    }


def test_raw_label_plane_keeps_each_diarized_speaker_apart():
    analytics = AnalyticsService._compute_from_segments(MEETING, total_duration=20.0)

    assert analytics.talk_time.by_speaker == {
        "SPEAKER_00": 6.5,
        "SPEAKER_01": 2.5,
        "SPEAKER_02": 3.0,
        "Unknown": 0.5,
        "SPEAKER_03": 2.5,
    }
    assert analytics.talk_time.total == 15.0
    assert analytics.word_count == 17
    assert analytics.turn_taking.by_speaker == {
        "SPEAKER_00": 2,
        "SPEAKER_01": 1,
        "SPEAKER_02": 1,
        "Unknown": 1,
        "SPEAKER_03": 2,
    }
    assert analytics.turn_taking.total_turns == 7
    assert analytics.interruptions.by_speaker == {"SPEAKER_01": 1}
    assert analytics.questions.by_speaker == {"SPEAKER_00": 1}
    assert analytics.speaking_pace == 17 / 15.0 * 60
    assert analytics.silence_ratio == 0.25


def test_both_planes_agree_on_talk_time_and_words_per_diarized_speaker():
    """The planes differ in LABELS only: summed per display label, the numbers match."""
    by_label = compute_speaker_stats(MEETING, voiceprint_names=True)
    analytics = AnalyticsService._compute_from_segments(MEETING, total_duration=20.0)
    raw_to_label = {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob", "SPEAKER_02": "Alice"}
    raw_to_label |= {"Unknown": "Unknown Speaker", "SPEAKER_03": "SPEAKER_03"}

    summed: dict[str, float] = {}
    for raw, seconds in analytics.talk_time.by_speaker.items():
        summed[raw_to_label[raw]] = summed.get(raw_to_label[raw], 0.0) + seconds

    assert summed == {label: s["total_time"] for label, s in by_label.items()}
    assert sum(s["word_count"] for s in by_label.values()) == analytics.word_count


# A segment with a missing time cannot come from the database (both columns are NOT NULL),
# but an in-memory caller can hand one in. Each missing time contributes no talk time; the
# segment still counts, and so do its words.
MISSING_TIMES = [
    _segment(S00, "one two", 0.0, None),
    _segment(S00, "three", None, 5.0),
    _segment(S01, "four", 5.0, 7.0),
]


def test_display_label_plane_counts_a_missing_time_as_no_talk_time():
    # Changed by D11: this raised TypeError (`None - 0.0`) before the shared tally.
    stats = compute_speaker_stats(MISSING_TIMES, voiceprint_names=True)

    assert stats == {
        "Alice": {"total_time": 0.0, "segment_count": 2, "word_count": 3, "percentage": 0.0},
        "Bob": {"total_time": 2.0, "segment_count": 1, "word_count": 1, "percentage": 100.0},
    }


def test_raw_label_plane_counts_a_missing_time_as_no_talk_time():
    # Changed by D11: `(end or 0) - (start or 0)` counted the missing-start segment's absolute
    # end time (5.0 s) as talk, giving SPEAKER_00 5.0, total 7.0, pace 34.29 wpm, silence 0.3.
    analytics = AnalyticsService._compute_from_segments(MISSING_TIMES, total_duration=10.0)

    assert analytics.talk_time.by_speaker == {"SPEAKER_00": 0.0, "SPEAKER_01": 2.0}
    assert analytics.talk_time.total == 2.0
    assert analytics.speaking_pace == 4 / 2.0 * 60
    assert analytics.silence_ratio == 0.8
