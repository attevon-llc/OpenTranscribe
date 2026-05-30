"""Unit tests for the boundary smoother (issue #193, §8.2/§8.8)."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

import numpy as np

from app.transcription.boundary_resolver import BoundarySmoothingConfig
from app.transcription.boundary_resolver import acoustic_recheck
from app.transcription.boundary_resolver import smooth_word_speakers


def _cfg(**kw: Any) -> BoundarySmoothingConfig:
    base: dict[str, Any] = dict(
        enabled=True,
        max_island_words=3,
        max_island_duration=10.0,
        min_flank_words=3,
        min_silent_gap=0.4,
        margin_threshold=0.0,
    )
    base.update(kw)
    return BoundarySmoothingConfig(**base)


def _segments(
    spec: Sequence[tuple[str, float, float]], margins: list[float] | None = None
) -> list[dict]:
    """Build one segment from (speaker, start, end) triples."""
    words = []
    for i, (spk, start, end) in enumerate(spec):
        w: dict[str, Any] = {"word": f"w{i}", "start": start, "end": end, "speaker": spk}
        if margins is not None:
            w["_overlap_margin"] = margins[i]
        words.append(w)
    return [{"start": spec[0][1], "end": spec[-1][2], "text": "", "words": words}]


def _speakers(segments: list[dict]) -> list[str]:
    return [w["speaker"] for s in segments for w in s["words"]]


def test_collapses_two_word_island():
    # A A A | B B | A A A  (contiguous, no gaps) → island collapses to A
    spec = [("A", i, i + 1) for i in range(3)]
    spec += [("B", 3, 4), ("B", 4, 5)]
    spec += [("A", i, i + 1) for i in range(5, 8)]
    segs = _segments(spec)
    smooth_word_speakers(segs, _cfg())
    assert _speakers(segs) == ["A"] * 8


def test_refuses_backchannel_with_silent_gap():
    # gap of 1.0s before and after the lone B → real backchannel, left alone
    spec = [("A", 0, 1), ("A", 1, 2), ("A", 2, 3), ("B", 4, 5)]
    spec += [("A", 6, 7), ("A", 7, 8), ("A", 8, 9)]
    segs = _segments(spec)
    smooth_word_speakers(segs, _cfg())
    assert "B" in _speakers(segs)  # untouched


def test_refuses_when_flanks_differ():
    # A A A | B B | C C C → flanks are different speakers, not a bleed
    spec = [("A", i, i + 1) for i in range(3)]
    spec += [("B", 3, 4), ("B", 4, 5)]
    spec += [("C", i, i + 1) for i in range(5, 8)]
    segs = _segments(spec)
    smooth_word_speakers(segs, _cfg())
    assert _speakers(segs).count("B") == 2  # untouched


def test_respects_max_island_words():
    # 4-word B island with N=3 → not collapsed
    spec = [("A", i, i + 1) for i in range(3)]
    spec += [("B", 3, 4), ("B", 4, 5), ("B", 5, 6), ("B", 6, 7)]
    spec += [("A", i, i + 1) for i in range(7, 10)]
    segs = _segments(spec)
    smooth_word_speakers(segs, _cfg(max_island_words=3))
    assert _speakers(segs).count("B") == 4  # untouched


def test_respects_min_flank_words():
    # left flank only 2 words (< min_flank_words=3) → refuse
    spec = [("A", 0, 1), ("A", 1, 2), ("B", 2, 3), ("A", 3, 4), ("A", 4, 5), ("A", 5, 6)]
    segs = _segments(spec)
    smooth_word_speakers(segs, _cfg(min_flank_words=3))
    assert "B" in _speakers(segs)


def test_idempotent():
    spec = [("A", i, i + 1) for i in range(3)]
    spec += [("B", 3, 4), ("B", 4, 5)]
    spec += [("A", i, i + 1) for i in range(5, 8)]
    once = _segments(spec)
    smooth_word_speakers(once, _cfg())
    twice = copy.deepcopy(once)
    smooth_word_speakers(twice, _cfg())
    assert _speakers(once) == _speakers(twice)


def test_phase2_collapses_disputed_long_island():
    # 4-word island exceeds N=3 but every word is disputed (tiny margin) → Phase 2 collapses
    spec = [("A", i, i + 1) for i in range(3)]
    spec += [("B", 3, 4), ("B", 4, 5), ("B", 5, 6), ("B", 6, 7)]
    spec += [("A", i, i + 1) for i in range(7, 10)]
    margins = [1.0] * 3 + [0.01] * 4 + [1.0] * 3  # island words ambiguous
    segs = _segments(spec, margins=margins)
    smooth_word_speakers(segs, _cfg(max_island_words=3, margin_threshold=0.05))
    assert _speakers(segs) == ["A"] * 10


def test_phase2_keeps_confident_long_island():
    # same shape but island words are confident (large margin) → left alone
    spec = [("A", i, i + 1) for i in range(3)]
    spec += [("B", 3, 4), ("B", 4, 5), ("B", 5, 6), ("B", 6, 7)]
    spec += [("A", i, i + 1) for i in range(7, 10)]
    margins = [1.0] * 10
    segs = _segments(spec, margins=margins)
    smooth_word_speakers(segs, _cfg(max_island_words=3, margin_threshold=0.05))
    assert _speakers(segs).count("B") == 4


# ── Phase 3: acoustic re-check (issue #193 backchannel absorption) ──────────────


def _centroids() -> dict[str, Any]:
    return {"A": np.array([1.0, 0.0]), "B": np.array([0.0, 1.0])}


def test_acoustic_recheck_reassigns_absorbed_backchannel():
    # A short "yeah" assigned to A, in an overlap region, but acoustically B.
    words = [
        {"word": "talking", "start": 0.0, "end": 2.0, "speaker": "A"},  # long → not a candidate
        {"word": "yeah", "start": 5.0, "end": 5.3, "speaker": "A"},  # short + in overlap → recheck
    ]

    def embed_fn(s: float, e: float) -> Any:
        return np.array([0.0, 1.0]) if 4.0 < s < 6.0 else np.array([1.0, 0.0])

    n = acoustic_recheck(
        words, _centroids(), embed_fn, overlap_regions=[{"start": 5.0, "end": 5.4}]
    )
    assert n == 1
    assert words[1]["speaker"] == "B"  # absorbed backchannel reassigned by voiceprint
    assert words[0]["speaker"] == "A"  # long word untouched
    assert len(words) == 2  # NEVER fabricates/drops words


def test_acoustic_recheck_no_change_when_already_correct():
    words = [{"word": "yeah", "start": 5.0, "end": 5.3, "speaker": "A", "_overlap_margin": 0.0}]
    n = acoustic_recheck(words, _centroids(), lambda s, e: np.array([1.0, 0.0]))
    assert n == 0 and words[0]["speaker"] == "A"


def test_acoustic_recheck_skips_confident_non_overlap_words():
    # High margin + not in overlap → not a candidate, so it is NOT rechecked even though
    # the embedding would say B. (We only touch disputed/overlap short words.)
    words = [{"word": "word", "start": 5.0, "end": 5.3, "speaker": "A", "_overlap_margin": 1.0}]
    n = acoustic_recheck(words, _centroids(), lambda s, e: np.array([0.0, 1.0]))
    assert n == 0 and words[0]["speaker"] == "A"


def test_acoustic_recheck_noop_without_centroids():
    words = [{"word": "yeah", "start": 5.0, "end": 5.3, "speaker": "A", "_overlap_margin": 0.0}]
    assert acoustic_recheck(words, {}, lambda s, e: np.array([0.0, 1.0])) == 0


def test_acoustic_recheck_respects_max_word_dur():
    # A long word (2 s) is never a backchannel candidate even if disputed + in overlap.
    words = [{"word": "however", "start": 5.0, "end": 7.0, "speaker": "A", "_overlap_margin": 0.0}]
    n = acoustic_recheck(
        words,
        _centroids(),
        lambda s, e: np.array([0.0, 1.0]),
        overlap_regions=[{"start": 5.0, "end": 7.0}],
        max_word_dur=1.0,
    )
    assert n == 0 and words[0]["speaker"] == "A"


# ── from_db_env: acoustic flags resolve from env / defaults ─────────────────────


def test_acoustic_config_defaults_off():
    cfg = BoundarySmoothingConfig()
    assert cfg.acoustic_recheck_enabled is False
    assert cfg.acoustic_cosine_margin == 0.05
    assert cfg.acoustic_max_word_dur == 1.0


def test_acoustic_config_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("ENGINE_BOUNDARY_ACOUSTIC_RECHECK_ENABLED", "true")
    monkeypatch.setenv("ENGINE_BOUNDARY_ACOUSTIC_COSINE_MARGIN", "0.12")
    monkeypatch.setenv("ENGINE_BOUNDARY_ACOUSTIC_MAX_WORD_DUR", "0.8")
    cfg = BoundarySmoothingConfig.from_db_env(None)
    assert cfg.acoustic_recheck_enabled is True
    assert cfg.acoustic_cosine_margin == 0.12
    assert cfg.acoustic_max_word_dur == 0.8
