"""Unit tests for the vectorized speaker-assignment core (`speaker_assigner.py`).

`assign_speakers`/`_batch_assign` had zero test references anywhere in this tree despite
being the function that decides who said what in every transcript, called from 4 production
paths (`engine/stages.py` x2, `reprocess.py`, `rediarize_task.py`). It also writes the
transient per-word ``"_overlap_margin"`` key that ``boundary_resolver.py`` reads downstream —
`test_boundary_resolver.py` only ever synthesizes that key itself, so neither the producer's
arithmetic nor the real producer -> consumer contract was ever verified.
"""

from __future__ import annotations

import numpy as np
import pytest

import app.transcription.speaker_assigner as speaker_assigner_module
from app.transcription.boundary_resolver import BoundarySmoothingConfig
from app.transcription.boundary_resolver import smooth_word_speakers
from app.transcription.diarize_result import DiarizeResult
from app.transcription.speaker_assigner import _batch_assign
from app.transcription.speaker_assigner import assign_speakers


def _diarize(intervals: list[tuple[float, float, str]]) -> DiarizeResult:
    """Build a DiarizeResult from (start, end, speaker) triples, matching production shape."""
    starts = np.array([i[0] for i in intervals], dtype=np.float64)
    ends = np.array([i[1] for i in intervals], dtype=np.float64)
    speakers = np.array([i[2] for i in intervals], dtype=object)
    return DiarizeResult(start=starts, end=ends, speaker=speakers)


def _transcript(words: list[tuple[float, float]]) -> dict:
    """Build a transcript_result dict matching the real WhisperX shape consumed in production
    (a single segment spanning all words, each word a dict with start/end/word)."""
    word_dicts: list[dict[str, object]] = [
        {"word": f"w{i}", "start": s, "end": e, "score": 1.0} for i, (s, e) in enumerate(words)
    ]
    seg = {
        "start": words[0][0],
        "end": words[-1][1],
        "text": " ".join(str(w["word"]) for w in word_dicts),
        "words": word_dicts,
    }
    return {"segments": [seg], "language": "en"}


def test_word_assigned_to_max_overlap_speaker():
    """A word squarely inside one speaker's interval, and another squarely inside a second
    speaker's interval, must each be labeled with the interval they actually overlap. A bug
    that picked the wrong index (e.g. an off-by-one in the speaker matrix) or always picked
    the first speaker would flip word 2's label to A and this would catch it."""
    diarize = _diarize([(0.0, 5.0, "SPEAKER_A"), (5.0, 10.0, "SPEAKER_B")])
    transcript = _transcript([(1.0, 2.0), (7.0, 8.0)])

    result = assign_speakers(diarize, transcript)

    words = result["segments"][0]["words"]
    assert words[0]["speaker"] == "SPEAKER_A"
    assert words[1]["speaker"] == "SPEAKER_B"
    assert result["segments"][0]["speaker"] == "SPEAKER_A"


def test_boundary_word_gets_correct_margin_value():
    """A word straddling two speaker intervals near-evenly must (a) be assigned to whichever
    speaker has the larger overlap and (b) carry an "_overlap_margin" equal to the actual
    top1-top2 overlap difference, computed independently here rather than just asserting
    "non-null". This is the exact contract boundary_resolver.py's Phase 2 (disputed-island
    collapse) depends on — a margin computed wrong (e.g. summed instead of subtracted, or
    using seconds vs some other unit) would silently break that feature while every existing
    test (which synthesizes the margin itself) stayed green."""
    # SPEAKER_A owns [0, 5.4), SPEAKER_B owns [5.4, 10). A word spans [5.0, 6.0):
    #   overlap with A = 5.4 - 5.0 = 0.4
    #   overlap with B = 6.0 - 5.4 = 0.6
    # expected margin = |0.6 - 0.4| = 0.2, expected winner = SPEAKER_B
    diarize = _diarize([(0.0, 5.4, "SPEAKER_A"), (5.4, 10.0, "SPEAKER_B")])
    transcript = _transcript([(5.0, 6.0)])

    result = assign_speakers(diarize, transcript)

    word = result["segments"][0]["words"][0]
    assert word["speaker"] == "SPEAKER_B"
    assert word["_overlap_margin"] == pytest.approx(0.2, abs=1e-6)


def test_batch_assign_chunk_boundary_matches_unchunked_reference():
    """The vectorized core chunks queries at `_CHUNK_SIZE` (5000) to bound memory. This
    constructs > 5000 words such that a real assignment spans the chunk boundary (words
    4990-5010 straddle multiple alternating speakers right at index 5000), runs it chunked
    (real `_CHUNK_SIZE`), then re-runs the identical input with `_CHUNK_SIZE` patched to a
    value far larger than the input (a single, effectively-unchunked pass), and asserts the
    two outputs are value-identical. A chunk-boundary bug (e.g. losing state across chunks,
    an off-by-one in `chunk_end`, or a margin computed only within a chunk's local top-2
    rather than globally per-query) would only manifest in the chunked run and would be
    invisible to any test using fewer than 5000 words."""
    n_words = 5200
    # Alternate speakers every ~3 words for the whole range so words land at every possible
    # offset relative to the 5000-boundary, and make every word straddle two intervals so
    # margins are non-trivial everywhere, especially right around the boundary.
    n_diar = 400
    diar_intervals = []
    t = 0.0
    for i in range(n_diar):
        dur = 3.0 + (i % 5) * 0.37  # varied durations, deterministic
        speaker = f"SPEAKER_{i % 4}"
        diar_intervals.append((t, t + dur, speaker))
        t += dur
    diarize = _diarize(diar_intervals)

    total_dur = t
    words = []
    step = total_dur / n_words
    for i in range(n_words):
        start = i * step
        end = start + step * 1.6  # overlaps into the next word's span -> ambiguous overlaps
        words.append((start, min(end, total_dur)))

    d_starts = diarize.start.astype(np.float64)
    d_ends = diarize.end.astype(np.float64)
    unique_speakers = np.unique(diarize.speaker)
    speaker_to_idx = {s: i for i, s in enumerate(unique_speakers)}
    n_speakers = len(unique_speakers)
    d_speaker_indices = np.array([speaker_to_idx[s] for s in diarize.speaker])
    speaker_matrix = np.zeros((len(d_starts), n_speakers), dtype=np.float32)
    speaker_matrix[np.arange(len(d_starts)), d_speaker_indices] = 1.0

    q_starts = np.array([w[0] for w in words], dtype=np.float64)
    q_ends = np.array([w[1] for w in words], dtype=np.float64)

    chunked_speakers, chunked_margins = _batch_assign(
        q_starts, q_ends, d_starts, d_ends, speaker_matrix, unique_speakers, return_margins=True
    )

    original_chunk_size = speaker_assigner_module._CHUNK_SIZE
    try:
        speaker_assigner_module._CHUNK_SIZE = n_words * 10  # effectively unchunked
        unchunked_speakers, unchunked_margins = speaker_assigner_module._batch_assign(
            q_starts,
            q_ends,
            d_starts,
            d_ends,
            speaker_matrix,
            unique_speakers,
            return_margins=True,
        )
    finally:
        speaker_assigner_module._CHUNK_SIZE = original_chunk_size

    assert chunked_speakers == unchunked_speakers
    assert len(chunked_margins) == len(unchunked_margins) == n_words
    for i, (a, b) in enumerate(zip(chunked_margins, unchunked_margins, strict=True)):
        assert abs(a - b) < 1e-4, f"margin mismatch at word {i}: chunked={a} unchunked={b}"

    # Sanity: the boundary region (around global index 5000, i.e. chunk_start=5000 in the
    # chunked run) is actually exercised and not degenerate for every word there.
    boundary_slice = slice(4980, 5020)
    assert any(m > 0 for m in chunked_margins[boundary_slice])


def test_boundary_resolver_consumes_real_margin_from_assign_speakers():
    """Exercises the real producer -> consumer chain: run the real `assign_speakers` (not a
    synthesized margin) to produce word dicts, then feed them into
    `boundary_resolver.smooth_word_speakers` with Phase 2 (margin-gated) collapse enabled.
    A B-speaker island whose words all carry a genuinely small overlap margin (because the
    diarization intervals for A/B nearly tie under it) must collapse into the flanking A run;
    if `assign_speakers` computed margins on the wrong axis/sign, or boundary_resolver read a
    different key, the island would NOT collapse and this test would fail."""
    # SPEAKER_A owns [0, 10.1) and [10.5, 20.0); SPEAKER_B wedges in at [10.1, 10.5). Every
    # word inside [10.0, 10.6) straddles the A/B boundary and gets a small overlap margin,
    # but B narrowly wins both -> a genuinely "disputed" 2-word B island flanked by A.
    diarize = _diarize(
        [
            (0.0, 10.1, "SPEAKER_A"),
            (10.1, 10.5, "SPEAKER_B"),
            (10.5, 20.0, "SPEAKER_A"),
        ]
    )
    # Flank left: 8 words ending exactly at the island start (no silent-gap refusal).
    flank_left = [(2.0 + i, 3.0 + i) for i in range(8)]  # ends at 10.0
    island = [(10.0, 10.3), (10.3, 10.6)]  # straddles the A/B/A boundary, B narrowly wins
    # Flank right: 8 words starting exactly at the island end.
    flank_right = [(10.6 + i, 11.6 + i) for i in range(8)]
    words = flank_left + island + flank_right
    transcript = _transcript(words)

    result = assign_speakers(diarize, transcript)
    result_words = result["segments"][0]["words"]

    # Confirm assign_speakers actually wrote the real margin key (not synthesized).
    for w in result_words:
        assert "_overlap_margin" in w

    cfg = BoundarySmoothingConfig(
        enabled=True,
        max_island_words=1,  # island is 2 words -> Phase 1 alone would NOT collapse it
        max_island_duration=1.5,
        min_flank_words=3,
        min_silent_gap=0.4,
        margin_threshold=0.5,  # generous vs the ~0.1s margins here, well below full-overlap size
    )
    smooth_word_speakers(result["segments"], cfg)

    speakers = [w["speaker"] for w in result["segments"][0]["words"]]
    # The disputed island (indices 8, 9) must have collapsed to SPEAKER_A via Phase 2, which
    # only fires by reading the real "_overlap_margin" produced by assign_speakers.
    assert speakers[8] == "SPEAKER_A"
    assert speakers[9] == "SPEAKER_A"
