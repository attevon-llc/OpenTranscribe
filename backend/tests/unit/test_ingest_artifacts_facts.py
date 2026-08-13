"""``file_facts.facts`` — the numbers Stage 4 answers aggregation questions from EXACTLY.

These are not retrieval results, so they must be right rather than plausible: "who talked
most" answered from a digest is a guess, answered from here it is arithmetic.
"""

from __future__ import annotations

import datetime

import pytest

from app.services.ingest_artifacts.facts import build_facts
from app.services.ingest_artifacts.keyphrases import extract_keyphrases
from app.utils.transcript_builders import compute_speaker_stats


class _Speaker:
    def __init__(self, name):
        self.name = name
        self.display_name = name
        self.verified = True
        self.suggested_name = None
        self.confidence = None


class _Segment:
    def __init__(self, text, start, end, speaker):
        self.text = text
        self.start_time = start
        self.end_time = end
        self.speaker = _Speaker(speaker)


def _make(script):
    """script: list of (speaker, text, start, end) → (dicts, speaker_stats)."""
    segments = [
        {"id": i + 1, "text": t, "start_time": s, "end_time": e, "speaker": sp}
        for i, (sp, t, s, e) in enumerate(script)
    ]
    stats = compute_speaker_stats([_Segment(t, s, e, sp) for sp, t, s, e in script])
    return segments, stats


def _facts(script, duration=None):
    segments, stats = _make(script)
    return build_facts(
        segments,
        speaker_stats=stats,
        duration=duration,
        language="en",
        recorded_at=datetime.datetime(2026, 8, 12, tzinfo=datetime.UTC),
    )


def test_talk_time_and_percentage_are_exact():
    facts = _facts(
        [
            ("Dana", "one two three", 0.0, 30.0),
            ("Marcus", "four five", 30.0, 40.0),
        ]
    )
    by_name = {s["name"]: s for s in facts["speakers"]}
    assert by_name["Dana"]["total_time"] == 30.0
    assert by_name["Marcus"]["total_time"] == 10.0
    assert by_name["Dana"]["percentage"] == 75.0
    assert by_name["Marcus"]["percentage"] == 25.0
    assert facts["spoken_seconds"] == 40.0


def test_speakers_are_ordered_loudest_first():
    facts = _facts(
        [
            ("Quiet", "a b", 0.0, 5.0),
            ("Loud", "c d", 5.0, 60.0),
        ]
    )
    assert [s["name"] for s in facts["speakers"]] == ["Loud", "Quiet"]


def test_an_exact_talk_time_tie_is_broken_by_name_not_by_dict_order():
    facts = _facts(
        [
            ("Zoe", "a b", 0.0, 10.0),
            ("Adam", "c d", 10.0, 20.0),
        ]
    )
    assert [s["name"] for s in facts["speakers"]] == ["Adam", "Zoe"]


def test_turns_group_consecutive_segments_by_the_same_speaker():
    facts = _facts(
        [
            ("Dana", "a b", 0.0, 5.0),
            ("Dana", "c d", 5.0, 10.0),
            ("Marcus", "e f", 10.0, 15.0),
            ("Dana", "g h", 15.0, 20.0),
        ]
    )
    assert facts["segment_count"] == 4
    assert facts["turn_count"] == 3
    by_name = {s["name"]: s for s in facts["speakers"]}
    assert by_name["Dana"]["turn_count"] == 2
    assert by_name["Marcus"]["turn_count"] == 1


def test_the_longest_monologue_is_the_longest_turn_not_the_longest_segment():
    """Two adjacent 10 s segments from one speaker are a 20 s monologue.

    Measuring the longest *segment* would answer 12 here, which is the wrong answer to
    "who spoke longest without interruption".
    """
    facts = _facts(
        [
            ("Dana", "a b", 0.0, 10.0),
            ("Dana", "c d", 10.0, 20.0),
            ("Marcus", "e f", 20.0, 32.0),
        ]
    )
    assert facts["longest_monologue"]["speaker"] == "Dana"
    assert facts["longest_monologue"]["seconds"] == 20.0
    assert facts["longest_monologue"]["start_time"] == 0.0
    assert facts["longest_monologue"]["end_time"] == 20.0


def test_a_turn_does_not_inherit_the_previous_turns_end_time():
    """Regression: the turn walker once carried ``turn_end`` across a speaker change.

    With that bug a later short turn measured from the previous turn's end and could win
    "longest monologue" outright.
    """
    facts = _facts(
        [
            ("Dana", "a b", 0.0, 100.0),
            ("Marcus", "c d", 100.0, 101.0),
        ]
    )
    by_name = {s["name"]: s for s in facts["speakers"]}
    assert by_name["Marcus"]["longest_turn"] == 1.0
    assert facts["longest_monologue"]["speaker"] == "Dana"


def test_an_equal_length_monologue_tie_keeps_the_earlier_one():
    facts = _facts(
        [
            ("First", "a b", 0.0, 10.0),
            ("Second", "c d", 10.0, 20.0),
        ]
    )
    assert facts["longest_monologue"]["speaker"] == "First"


def test_the_roster_is_sorted_so_the_payload_serialises_identically_every_run():
    facts = _facts(
        [
            ("Zoe", "a b", 0.0, 5.0),
            ("Adam", "c d", 5.0, 10.0),
            ("Mia", "e f", 10.0, 15.0),
        ]
    )
    assert facts["roster"] == ["Adam", "Mia", "Zoe"]
    assert facts["speaker_count"] == 3


def test_duration_and_date_survive_into_the_payload():
    facts = _facts([("Dana", "a b", 0.0, 5.0)], duration=3600.4)
    assert facts["duration_seconds"] == 3600.4
    assert facts["recorded_at"].startswith("2026-08-12")


def test_an_empty_transcript_produces_a_payload_rather_than_an_exception():
    facts = build_facts([], speaker_stats={}, duration=None, language="en", recorded_at=None)
    assert facts["speaker_count"] == 0
    assert facts["turn_count"] == 0
    assert facts["first_utterance_at"] is None


def test_compute_speaker_stats_still_matches_build_transcript_and_stats():
    """The split must be a refactor, not a behaviour change.

    ``build_transcript_and_stats`` had the aggregation inlined; Stage 2 pulled it out so
    the no-LLM path could call it. Both must still return the same dict.
    """
    from app.utils.transcript_builders import build_transcript_and_stats

    segments = [
        _Segment("hello there friend", 0.0, 4.0, "Dana"),
        _Segment("and hello to you", 4.0, 9.0, "Marcus"),
        _Segment("shall we begin", 9.0, 12.0, "Dana"),
    ]
    _, inline = build_transcript_and_stats(segments)
    assert inline == compute_speaker_stats(segments)


# ------------------------------------------------------------------ keyphrases


def _corpus() -> str:
    return (
        "The quarterly budget review covers the new product line. "
        "The quarterly budget review must finish before the launch date. "
        "The launch date depends on the engineering timeline. "
        "The engineering timeline slipped, so the launch date moves. "
    ) * 3


def test_keyphrases_are_deterministic_and_totally_ordered():
    first = extract_keyphrases(_corpus())
    second = extract_keyphrases(_corpus())
    assert first == second
    scores = [(-p["score"], p["phrase"]) for p in first["phrases"]]
    assert scores == sorted(scores), "ordering must be a total order, not score alone"


def test_multi_word_phrases_beat_their_own_component_words():
    """RAKE's degree/frequency ratio is what promotes the phrase over its parts.

    Asserted as a property, not against a literal string: the candidate boundary is the
    stopword list, so the exact surface form ("quarterly budget review" vs the same run
    plus a trailing verb) depends on which words NLTK considers stoppable — a detail that
    changes with the corpus version and is not what this test is about.
    """
    phrases = [p["phrase"] for p in extract_keyphrases(_corpus())["phrases"]]
    assert any(" " in p for p in phrases)
    assert any("budget review" in p for p in phrases)
    top = phrases[0]
    assert " " in top, f"the highest-scoring phrase is a bare unigram: {top!r}"


def test_stopwords_never_appear_inside_a_keyphrase():
    phrases = extract_keyphrases(_corpus())["phrases"]
    assert len(phrases) >= 3, "no phrases — the loop below would pass vacuously"
    for phrase in phrases:
        assert " the " not in f" {phrase['phrase']} "
        assert not phrase["phrase"].startswith("the ")


def test_a_phrase_seen_once_is_dropped_as_noise():
    text = _corpus() + " A completely unrepeated aside about marmalade."
    phrases = [p["phrase"] for p in extract_keyphrases(text)["phrases"]]
    assert not any("marmalade" in p for p in phrases)


def test_empty_text_yields_an_empty_phrase_list():
    assert extract_keyphrases("")["phrases"] == []


@pytest.mark.parametrize("limit", [1, 5])
def test_the_limit_is_honoured(limit):
    assert len(extract_keyphrases(_corpus(), limit=limit)["phrases"]) <= limit
