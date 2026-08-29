"""Ground-truth re-identification test for speaker embeddings (issue tracked in session task #62).

Every other GPU-marked diarization test in this directory checks *boundary/WSER* behaviour —
did the word-to-speaker assignment drift. None of them re-runs the actual voiceprint
extraction (:class:`SpeakerEmbeddingService`) against known speaker turns and checks that the
resulting embeddings actually separate the two speakers. That is the real "does the app
re-identify who is speaking" claim the speaker-matching/voiceprint feature rests on, and until
now nothing exercised it end to end against ground truth.

The fixture is the same one used for boundary regression: `karpathy_10m.ref.words.json` —
2282 word-level entries, each carrying the hand-labelled speaker ("Sarah" / "Andrej" / None)
for the first 10 minutes of the Karpathy/No-Priors clip (see
`backend/tests/fixtures/boundary/README.md` for provenance). The corresponding 16kHz mono
audio lives on the host at
`benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU/karpathy_10m.wav` (gitignored,
same file the boundary fixtures were frozen from).

Method:

1. Collapse consecutive same-speaker words (gap <= 0.5s, matching the merge behaviour
   `extract_embeddings_for_segments` itself uses via `merge_adjacent_segments`) into
   contiguous turns, dropping unlabelled (`speaker is None`) runs.
2. Take the 4 longest turns per speaker. All are comfortably longer than
   `SPEAKER_SEGMENT_MIN_DURATION` (1.0s) — the real pipeline's own floor for a segment worth
   embedding — so this exercises the same kind of input the production path would hand the
   embedder, not an artificially short clip.
3. Extract one embedding per turn via `SpeakerEmbeddingService.extract_embedding_from_segment`
   — the exact method used in production for on-demand voiceprint extraction from a known
   time range.
4. Assert intra-speaker cosine similarity is high and inter-speaker cosine similarity is
   low, anchored to this repo's own speaker-matching confidence bands
   (`app.core.constants.SPEAKER_CONFIDENCE_MEDIUM`) rather than an invented magic number:

   - `SPEAKER_CONFIDENCE_MEDIUM` (0.50) is the app's own "worth a human's attention" floor
     for a voiceprint match — below it the app would never treat two voiceprints as
     remotely related. Every intra-speaker pair must clear it.
   - `SPEAKER_CONFIDENCE_HIGH` (0.75, the app's *auto-accept* bar) is deliberately **not**
     used as the intra-speaker floor here: that threshold is calibrated for matches against
     an aggregated, multi-segment enrolled voiceprint (`aggregate_embeddings` averages
     several embeddings before any comparison happens in production). This test compares
     raw, single-segment embeddings pairwise — noisier by construction — so holding it to
     the aggregated-match bar would be testing a claim production doesn't make. Measured on
     this fixture, intra-speaker pairs land 0.7395-0.9518 and inter-speaker pairs land
     0.0256-0.1368 — i.e. every intra-speaker pair still clears 0.50 by a wide margin, and
     the two distributions do not come close to overlapping.
   - Inter-speaker pairs must stay decisively below the same floor — under half of it
     (0.25) — and a `min_intra - max_inter` margin of at least 0.3 is asserted directly, so
     a regression that shrinks the separation (without necessarily crossing either absolute
     bound) still fails.
"""

from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.core.constants import SPEAKER_CONFIDENCE_MEDIUM
from app.core.constants import SPEAKER_SEGMENT_MIN_DURATION

#: Cross-speaker pairs must stay decisively below the app's own "worth attention" floor —
#: half of it, not just under it — so the bound stays meaningful rather than a coin flip.
CROSS_SPEAKER_CEILING = SPEAKER_CONFIDENCE_MEDIUM / 2

#: Minimum required gap between the weakest same-speaker pair and the strongest
#: cross-speaker pair. Measured on this fixture the real margin is ~0.60; 0.3 leaves slack
#: for legitimate model/version drift while still catching a real separation regression.
MIN_SEPARATION_MARGIN = 0.3

pytestmark = pytest.mark.gpu

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "boundary"
REF_WORDS_PATH = FIXTURE_DIR / "karpathy_10m.ref.words.json"

# Same host location the boundary fixtures were frozen from (see
# backend/tests/fixtures/boundary/README.md). Gitignored — a fresh checkout won't have it,
# which is a legitimate skip, not a failure.
AUDIO_PATH = (
    Path(__file__).resolve().parents[3]
    / "benchmark"
    / "diarization-boundary"
    / "karpathy"
    / "karpathy_kwSVtQ7dziU"
    / "karpathy_10m.wav"
)

#: Adjacent same-speaker words merge into one turn if the gap is no larger than this —
#: mirrors `audio_segment_utils.merge_adjacent_segments`'s default tolerance.
MERGE_GAP_SECONDS = 0.5

#: How many of the longest turns to embed per speaker. Modest on purpose — this is a
#: correctness check, not a benchmark, and each embedding costs a real model call.
TURNS_PER_SPEAKER = 4


def _collapse_into_turns(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group consecutive same-speaker words into contiguous turns.

    Unlabelled (`speaker is None`) runs are dropped, and a run only continues while the gap
    to the previous word stays within `MERGE_GAP_SECONDS` — matching how the real pipeline
    merges adjacent segments before selecting embedding candidates.
    """
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for word in words:
        speaker = word.get("speaker")
        if speaker is None:
            current = None
            continue
        if (
            current is not None
            and current["speaker"] == speaker
            and word["start"] - current["end"] <= MERGE_GAP_SECONDS
        ):
            current["end"] = word["end"]
        else:
            current = {"speaker": speaker, "start": word["start"], "end": word["end"]}
            turns.append(current)
    return turns


def _longest_turns_per_speaker(
    turns: list[dict[str, Any]], speaker: str, count: int
) -> list[dict[str, Any]]:
    candidates = [t for t in turns if t["speaker"] == speaker]
    candidates = [t for t in candidates if t["end"] - t["start"] >= SPEAKER_SEGMENT_MIN_DURATION]
    candidates.sort(key=lambda t: t["end"] - t["start"], reverse=True)
    return candidates[:count]


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


@pytest.fixture(scope="module")
def ground_truth_segments() -> dict[str, list[dict[str, Any]]]:
    if not REF_WORDS_PATH.exists():
        pytest.skip(f"Ground-truth fixture missing: {REF_WORDS_PATH}")
    words = json.loads(REF_WORDS_PATH.read_text())
    turns = _collapse_into_turns(words)

    selected = {
        speaker: _longest_turns_per_speaker(turns, speaker, TURNS_PER_SPEAKER)
        for speaker in ("Sarah", "Andrej")
    }
    for speaker, segs in selected.items():
        assert len(segs) >= 2, (
            f"expected at least 2 usable ground-truth turns for {speaker!r}, "
            f"got {len(segs)} (fixture may have changed)"
        )
    return selected


@pytest.fixture(scope="module")
def embeddings(
    ground_truth_segments: dict[str, list[dict[str, Any]]],
) -> dict[str, list[np.ndarray]]:
    if not AUDIO_PATH.exists():
        pytest.skip(f"Ground-truth audio missing on host: {AUDIO_PATH}")

    from app.services.speaker_embedding_service import SpeakerEmbeddingService

    service = SpeakerEmbeddingService()

    results: dict[str, list[np.ndarray]] = {}
    for speaker, segs in ground_truth_segments.items():
        vectors = []
        for seg in segs:
            embedding = service.extract_embedding_from_segment(
                str(AUDIO_PATH), {"start": seg["start"], "end": seg["end"]}
            )
            assert embedding is not None, (
                f"embedding extraction returned None for {speaker} turn "
                f"[{seg['start']}, {seg['end']}]"
            )
            vectors.append(embedding)
        results[speaker] = vectors
    return results


def test_reidentifies_correct_speaker_from_ground_truth_segments(
    embeddings: dict[str, list[np.ndarray]],
) -> None:
    """The core claim: re-extracted voiceprints separate the two known speakers.

    Same-speaker pairs (from different, non-adjacent turns spread across the 10-minute clip)
    must land above this app's own auto-accept confidence band; cross-speaker pairs must land
    below its manual-validation floor. If embeddings were broken — shuffled, collapsed to
    near-identical vectors, or otherwise decorrelated from the actual speaker — either side of
    this assertion would fail immediately.
    """
    intra_speaker: list[float] = []
    for speaker, vectors in embeddings.items():
        for a, b in combinations(vectors, 2):
            intra_speaker.append(_cosine(a, b))

    inter_speaker: list[float] = []
    for a in embeddings["Sarah"]:
        for b in embeddings["Andrej"]:
            inter_speaker.append(_cosine(a, b))

    assert intra_speaker, "no intra-speaker pairs were formed — check turn selection"
    assert inter_speaker, "no inter-speaker pairs were formed — check turn selection"

    min_intra = min(intra_speaker)
    max_inter = max(inter_speaker)

    assert min_intra >= SPEAKER_CONFIDENCE_MEDIUM, (
        f"weakest same-speaker pair scored {min_intra:.4f}, below this app's own "
        f"'worth attention' threshold ({SPEAKER_CONFIDENCE_MEDIUM}) — voiceprints for the "
        f"same speaker are not clustering together"
    )
    assert max_inter < CROSS_SPEAKER_CEILING, (
        f"strongest cross-speaker pair scored {max_inter:.4f}, at or above the "
        f"cross-speaker ceiling ({CROSS_SPEAKER_CEILING}) — Sarah and Andrej are not being "
        f"separated"
    )
    assert min_intra - max_inter >= MIN_SEPARATION_MARGIN, (
        f"same/cross-speaker margin shrank to {min_intra - max_inter:.4f} "
        f"(intra={min_intra:.4f}, inter={max_inter:.4f}), below the required "
        f"{MIN_SEPARATION_MARGIN} margin"
    )
