"""One merge of speaker turns into text: ``assign_speakers`` replaces the midpoint merge (#1226).

The cloud parallel path (cloud ASR + pyannote.ai, and now another vendor's speakers) used
``utils/diarization_merge.merge_cloud_diarization``: each word took the turn that covers its
MIDPOINT (bisect over turn starts), a segment took the majority of its words, and a word in a
gap between turns got no speaker. Every other path used ``assign_speakers``: each word takes
the speaker with the most OVERLAP, and a word that overlaps nothing takes the nearest turn.
Plan C7 / Q11: one merge, gated by this test.

Measured on HEAD (f881ec22 + merge of feat/v0.6.0-frontend-ux) against the two real frozen
GPU runs in ``tests/fixtures/boundary`` (Whisper words + diarizer turns, 10 minutes each):

| fixture | words | labelled by old | agree | gap words (old: none) | ground truth: old / new |
|---|---|---|---|---|---|
| ``karpathy_10m`` (PyAnnote) | 2284 | 2266 | 2259 = **99.69%** | 18 | 2215 / **2232** of 2281 |
| ``karpathy_10m_native`` | 2258 | 2244 | 2242 = **99.91%** | 14 | 2211 / **2227** of 2254 |

Every disagreement (7 + 2 words) is a word overlapping turns of TWO speakers, where the
midpoint fell in one turn and the larger overlap in the other; the overlap rule is the one
that agrees more often with the hand-labelled reference. Segments agree 26/26 and 25/25. The
``_midpoint_reference`` below is a frozen copy of the deleted rule, kept only as the oracle.

Segment timing (``whisper-1``, ``gpt-4o-transcribe-diarize``, Azure): each segment takes the
speaker with the most overlap over the whole segment and its words inherit it, so a speaker
change inside a segment is not detected. The pipeline plan reports that as the
``segment_timing_only`` warning.
"""

from __future__ import annotations

import bisect
import json
from collections import Counter
from pathlib import Path

import pytest

from app.services.diarization.types import DiarizeSegment
from app.transcription.speaker_assigner import MergeGranularity
from app.transcription.speaker_assigner import assign_speakers
from app.transcription.speaker_assigner import diarize_segments_to_frame

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "boundary"
_REAL = ("karpathy_10m", "karpathy_10m_native")
Turn = tuple[float, float, str]


def _midpoint_reference(words: list[dict], turns: list[Turn]) -> list[str | None]:
    """The deleted ``merge_cloud_diarization`` word rule, frozen as the oracle."""
    ordered = sorted(turns, key=lambda t: t[0])
    starts = [t[0] for t in ordered]
    out: list[str | None] = []
    for w in words:
        mid = (w["start"] + w["end"]) / 2.0
        idx = bisect.bisect_right(starts, mid) - 1
        hit = idx >= 0 and ordered[idx][0] <= mid <= ordered[idx][1]
        out.append(ordered[idx][2] if hit else None)
    return out


def _load(name: str) -> tuple[list[dict], list[Turn]]:
    raw = json.loads((_FIXTURES / f"{name}.rawinfer.json").read_text())
    turns = [(r["start"], r["end"], r["speaker"]) for r in raw["diarize_records"]]
    return raw["raw_segments"], turns


def _assign(
    segments: list[dict], turns: list[Turn], granularity: MergeGranularity = "word"
) -> list[dict]:
    transcript = {"segments": [dict(s, words=[dict(w) for w in s["words"]]) for s in segments]}
    frame = diarize_segments_to_frame(DiarizeSegment(a, b, c) for a, b, c in turns)
    out: list[dict] = assign_speakers(frame, transcript, granularity=granularity)["segments"]
    return out


def _overlap_by_speaker(word: dict, turns: list[Turn]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for start, end, speaker in turns:
        overlap = min(word["end"], end) - max(word["start"], start)
        if overlap > 0:
            totals[speaker] = totals.get(speaker, 0.0) + overlap
    return totals


def _agreement(old: list[str | None], new: list[str]) -> float:
    labelled = [(o, n) for o, n in zip(old, new, strict=True) if o is not None]
    assert labelled, "the oracle labelled nothing; the comparison would be vacuous"
    return sum(o == n for o, n in labelled) / len(labelled)


@pytest.mark.parametrize("name", _REAL)
def test_assign_speakers_agrees_with_the_midpoint_merge_on_at_least_99_percent(name):
    segments, turns = _load(name)
    words = [w for s in segments for w in s["words"]]
    assert len(words) > 2000, "the fixture lost its words"

    old = _midpoint_reference(words, turns)
    new = [w["speaker"] for s in _assign(segments, turns) for w in s["words"]]

    assert _agreement(old, new) >= 0.99


@pytest.mark.parametrize("name", _REAL)
def test_every_disagreement_is_a_two_speaker_word_won_by_the_larger_overlap(name):
    segments, turns = _load(name)
    words = [w for s in segments for w in s["words"]]
    old = _midpoint_reference(words, turns)
    new = [w["speaker"] for s in _assign(segments, turns) for w in s["words"]]

    disagreements = [
        (w, o, n) for w, o, n in zip(words, old, new, strict=True) if o is not None and o != n
    ]
    assert disagreements, "the measured disagreements vanished; re-measure the docstring table"
    for word, old_label, new_label in disagreements:
        overlap = _overlap_by_speaker(word, turns)
        assert set(overlap) >= {old_label, new_label}
        assert overlap[new_label] == max(overlap.values())


@pytest.mark.parametrize("name", _REAL)
def test_a_word_in_a_gap_gets_the_nearest_turn_instead_of_no_speaker(name):
    segments, turns = _load(name)
    words = [w for s in segments for w in s["words"]]
    old = _midpoint_reference(words, turns)
    new = [w["speaker"] for s in _assign(segments, turns) for w in s["words"]]

    gap = [(w, n) for w, o, n in zip(words, old, new, strict=True) if o is None]
    assert gap, "no gap words in the fixture; the nearest-turn rule is untested"
    for word, label in gap:
        if _overlap_by_speaker(word, turns):
            continue  # midpoint in a gap, but the word still touches a turn
        mid = (word["start"] + word["end"]) / 2.0
        nearest = min(turns, key=lambda t: abs((t[0] + t[1]) / 2.0 - mid))
        assert label == nearest[2]


@pytest.mark.parametrize("name", _REAL)
def test_the_overlap_rule_matches_the_hand_labelled_reference_at_least_as_often(name):
    segments, turns = _load(name)
    words = [w for s in segments for w in s["words"]]
    old = _midpoint_reference(words, turns)
    new = [w["speaker"] for s in _assign(segments, turns) for w in s["words"]]

    reference = json.loads((_FIXTURES / f"{name}.ref.words.json").read_text())
    truth = {(round(r["start"], 3), round(r["end"], 3)): r["speaker"] for r in reference}
    rows = [
        (truth[key], o, n)
        for w, o, n in zip(words, old, new, strict=True)
        if (key := (round(w["start"], 3), round(w["end"], 3))) in truth
    ]
    assert len(rows) > 2000
    mapping = {
        hyp: Counter(t for t, _, n in rows if n == hyp).most_common(1)[0][0]
        for hyp in {n for _, _, n in rows}
    }
    old_correct = sum(o is not None and mapping.get(o) == t for t, o, _ in rows)
    new_correct = sum(mapping[n] == t for t, _, n in rows)
    assert new_correct >= old_correct


@pytest.mark.parametrize("name", _REAL)
def test_the_oracle_is_the_merge_it_replaces(name):
    """Pins the frozen oracle to the real old merge while both exist: one word per segment,
    so the old merge's segment label is exactly its per-word midpoint label."""
    from app.services.asr.types import ASRResult
    from app.services.asr.types import ASRSegment
    from app.services.asr.types import ASRWord
    from app.services.diarization.types import DiarizeResult
    from app.utils.diarization_merge import merge_cloud_diarization

    segments, turns = _load(name)
    words = [w for s in segments for w in s["words"]]
    asr = ASRResult(
        segments=[
            ASRSegment(
                w["word"], w["start"], w["end"], words=[ASRWord(w["word"], w["start"], w["end"])]
            )
            for w in words
        ],
        language="en",
    )
    diarized = DiarizeResult(
        segments=[DiarizeSegment(a, b, c) for a, b, c in turns], num_speakers=2, provider_name="x"
    )
    real = [s.speaker for s in merge_cloud_diarization(asr, diarized).segments]
    assert real == _midpoint_reference(words, turns)


def test_the_agreement_measure_can_fail():
    """Control: a labeller that swaps the two speakers must score far below the gate."""
    segments, turns = _load(_REAL[0])
    words = [w for s in segments for w in s["words"]]
    old = _midpoint_reference(words, turns)
    swap = {"SPEAKER_00": "SPEAKER_01", "SPEAKER_01": "SPEAKER_00"}
    swapped = [swap[w["speaker"]] for s in _assign(segments, turns) for w in s["words"]]
    assert _agreement(old, swapped) < 0.5


# --- segment timing ------------------------------------------------------------------------

_TWO_TURNS: list[Turn] = [(0.0, 4.0, "SPEAKER_00"), (4.0, 10.0, "SPEAKER_01")]


def _one_segment(with_words: bool) -> list[dict]:
    words = (
        [
            {"word": "a", "start": 0.5, "end": 1.0},
            {"word": "b", "start": 2.0, "end": 3.0},
            {"word": "c", "start": 5.0, "end": 6.0},
            {"word": "d", "start": 8.0, "end": 9.0},
        ]
        if with_words
        else []
    )
    return [{"start": 0.0, "end": 10.0, "text": "a b c d", "words": words}]


def test_segment_timing_gives_the_segment_the_dominant_speaker_and_words_inherit_it():
    out = _assign(_one_segment(with_words=True), _TWO_TURNS, granularity="segment")
    assert out[0]["speaker"] == "SPEAKER_01"  # 6 s of overlap against 4 s
    assert [w["speaker"] for w in out[0]["words"]] == ["SPEAKER_01"] * 4


def test_word_timing_on_the_same_input_keeps_the_change_inside_the_segment():
    """Control for the test above: the difference is the granularity, not the input."""
    out = _assign(_one_segment(with_words=True), _TWO_TURNS, granularity="word")
    assert [w["speaker"] for w in out[0]["words"]] == [
        "SPEAKER_00",
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_01",
    ]


def test_a_segment_without_words_is_assigned_by_overlap_not_by_its_midpoint():
    """The old merge looked up a word-less segment's MIDPOINT; ``assign_speakers`` uses the
    larger overlap. Here the midpoint (5.0) and the overlap agree; below they do not."""
    out = _assign(_one_segment(with_words=False), _TWO_TURNS, granularity="segment")
    assert out[0]["speaker"] == "SPEAKER_01"

    skewed: list[Turn] = [(0.0, 5.5, "SPEAKER_00"), (5.5, 6.0, "SPEAKER_01")]
    segment = [{"start": 0.0, "end": 12.0, "text": "x", "words": []}]
    assert _midpoint_reference([{"start": 0.0, "end": 12.0}], skewed) == ["SPEAKER_01"]
    assert _assign(segment, skewed, granularity="segment")[0]["speaker"] == "SPEAKER_00"


def test_the_plan_merges_whisper_1_at_segment_level_with_the_warning():
    """``whisper-1`` + another provider's speakers: segment merge, and the user is told."""
    from app.services.asr.factory import ASRProviderFactory
    from app.services.pipeline_plan import DiarizerConfigInfo
    from app.services.pipeline_plan import PlanInputs
    from app.services.pipeline_plan import resolve_pipeline_plan

    caps = ASRProviderFactory.get_model_capabilities("openai", "whisper-1")
    plan = resolve_pipeline_plan(
        PlanInputs(
            deployment_mode="full",
            asr_provider="openai",
            asr_model="whisper-1",
            asr_config_uuid=None,
            asr_returns_speakers=caps["supports_diarization"],
            asr_speaker_hints=frozenset(caps["speaker_count_hints"]),
            asr_timing=caps["timestamp_granularity"],
            diarization_source="asr_config",
            diarization_config=DiarizerConfigInfo(
                provider="deepgram", model="nova-3", uuid="u", returns_speakers=True
            ),
        )
    )
    assert plan.diarization.execution == "parallel_cloud"
    assert plan.diarization.merge == "segment"
    assert "segment_timing_only" in {issue.code for issue in plan.issues}
