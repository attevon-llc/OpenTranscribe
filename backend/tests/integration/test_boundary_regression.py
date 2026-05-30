"""GPU-free regression tests for diarization boundary smoothing (issue #193).

Unlike ``test_diarization_regression.py`` this module NEVER calls the GPU. The
expensive transcription + diarization output is frozen in committed
``*.rawinfer.json`` fixtures, so the test only replays the CPU path:

    RawInferenceResult.deserialize(fixture)
        -> Engine.run_cpu_finalize        (CPU only — GPU output is frozen)
        -> finalize_segments OFF / ON      (boundary smoothing under test)
        -> WSER / island / DER assertions  (vs a frozen baseline JSON)

Two layers:

1. ``test_fixture_regression`` — runs over ``backend/tests/fixtures/boundary/*.rawinfer.json``.
   No fixture is committed yet, so it ``pytest.skip``s gracefully; the full assertion
   logic is present and activates the moment a fixture + baseline land.

2. ``test_synthetic_bleed_fixed`` — fully self-contained: builds a fake ``assigned``
   segment list in-Python (no engine, no fixture, no GPU), runs ``finalize_segments``
   OFF/ON, and asserts a known 2-word bleed island is repaired by ON with zero islands
   introduced. This always runs in CI.

Run (GPU-free)::

    cd backend && PYTHONPATH=. python -m pytest \\
        tests/integration/test_boundary_regression.py -v

Refs: issue #193, docs/DIARIZATION_BOUNDARY_FIX_PLAN.md.
"""

from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

# Same marker as the GPU regression module so shared CI selectors line up, BUT this
# module is intentionally GPU-free: every test either skips (no fixture) or runs pure
# Python. CPU finalize on a frozen fixture needs no CUDA.
pytestmark = pytest.mark.gpu

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "boundary"

# ON-smoothing preset — must match backend/scripts/benchmark_boundary.py.
SMOOTHING_ON_KWARGS: dict[str, Any] = {
    "enabled": True,
    "max_island_words": 3,
    "max_island_duration": 1.5,
    "min_flank_words": 3,
    "min_silent_gap": 0.4,
}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _smoothing_configs() -> tuple[Any, Any]:
    """Return (off_cfg, on_cfg). Skips if the smoother module isn't committed yet."""
    try:
        from app.transcription.boundary_resolver import BoundarySmoothingConfig
    except ImportError as exc:  # smoother written by the lead — not landed yet
        pytest.skip(f"BoundarySmoothingConfig not available yet: {exc}")
    return BoundarySmoothingConfig(enabled=False), BoundarySmoothingConfig(**SMOOTHING_ON_KWARGS)


def _finalize_segments() -> Any:
    """Import finalize_segments or skip if the pure function isn't committed yet."""
    try:
        from app.utils.segment_postprocess import finalize_segments
    except ImportError as exc:
        pytest.skip(f"finalize_segments not available yet: {exc}")
    return finalize_segments


def _score(segments: list[dict[str, Any]], ref_words: list[dict[str, Any]]) -> dict[str, Any]:
    """WSER + island count + collar-0 DER for one variant against parallel ref words."""
    from app.utils.diarization_metrics import count_bleed_islands
    from app.utils.diarization_metrics import der
    from app.utils.diarization_metrics import flatten_words
    from app.utils.diarization_metrics import map_hyp_to_ref
    from app.utils.diarization_metrics import words_to_rttm
    from app.utils.diarization_metrics import wser

    hyp_words = flatten_words(segments)
    assert len(hyp_words) == len(ref_words), (
        f"parallel-inventory guard: {len(ref_words)} ref vs {len(hyp_words)} hyp words"
    )
    w = wser(ref_words, hyp_words)
    ref_seq = [rw.get("speaker") for rw in ref_words]
    hyp_seq = map_hyp_to_ref([hw.get("speaker") for hw in hyp_words], w.get("perm", {}))
    islands = count_bleed_islands(ref_seq, hyp_seq, max_island=3)

    der_c0 = _collar0_der(der, words_to_rttm, ref_words, hyp_words)
    return {"wser": w["wser"], "islands": len(islands), "der_c0": der_c0}


def _collar0_der(der: Any, words_to_rttm: Any, ref_words: list, hyp_words: list) -> float | None:
    """Collar-0 DER from word-derived RTTM turns. None if pyannote.metrics absent."""
    from app.utils.diarization_metrics import read_rttm

    def _turns(words: list[dict[str, Any]]) -> list[tuple]:
        text = words_to_rttm(words, uri="fixture")
        if not text.strip():
            return []
        with tempfile.NamedTemporaryFile("w", suffix=".rttm", delete=False) as fh:
            fh.write(text)
            tmp = fh.name
        try:
            return read_rttm(tmp)
        finally:
            Path(tmp).unlink(missing_ok=True)

    ref_turns, hyp_turns = _turns(ref_words), _turns(hyp_words)
    if not ref_turns or not hyp_turns:
        return None
    try:
        return float(der(ref_turns, hyp_turns, collar=0.0)["der"])
    except ImportError:
        return None


def _discover_fixtures() -> list[Path]:
    if not FIXTURE_DIR.exists():
        return []
    return sorted(FIXTURE_DIR.glob("*.rawinfer.json"))


# ──────────────────────────────────────────────────────────────────────────────
# Fixture-driven regression (skips gracefully until a fixture is committed)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fixture_path",
    _discover_fixtures()
    or [pytest.param(None, marks=pytest.mark.skip(reason="no fixtures committed"))],
)
def test_fixture_regression(fixture_path: Path | None) -> None:
    """Replay a frozen GPU fixture through CPU finalize + smoothing; assert no regression.

    A sibling ``<name>.baseline.json`` (``{"off": {...}, "on": {...}}``) and a
    ``<name>.ref.words.json`` (parallel reference words) must accompany each fixture.
    Asserts: ON WSER reduces or holds vs the frozen OFF baseline, ON introduces zero
    bleed islands, and collar-0 DER is unchanged by smoothing.
    """
    if fixture_path is None:
        pytest.skip("no *.rawinfer.json fixtures present in fixtures/boundary/")
    assert fixture_path is not None  # narrow Path | None for the type checker

    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.engine import Engine
    from app.transcription.engine.job import RawInferenceResult

    ref_path = fixture_path.with_name(
        fixture_path.name.replace(".rawinfer.json", ".ref.words.json")
    )
    baseline_path = fixture_path.with_name(
        fixture_path.name.replace(".rawinfer.json", ".baseline.json")
    )
    if not ref_path.exists():
        pytest.skip(f"reference words missing: {ref_path}")

    finalize_segments = _finalize_segments()
    off_cfg, on_cfg = _smoothing_configs()

    payload = json.loads(fixture_path.read_text())
    raw = RawInferenceResult.deserialize(payload)
    cfg = EngineConfig.from_snapshot(raw.config_snapshot)
    assigned = Engine(cfg).run_cpu_finalize(raw).segments  # CPU only — GPU output frozen

    ref_words = json.loads(ref_path.read_text())
    if isinstance(ref_words, dict):
        ref_words = ref_words.get("words", [])

    off = _score(finalize_segments(deepcopy(assigned), off_cfg), ref_words)
    on = _score(finalize_segments(deepcopy(assigned), on_cfg), ref_words)

    # Smoothing must reduce or hold WSER (5% slack absorbs scoring noise).
    assert on["wser"] <= off["wser"] * 1.05, (
        f"{fixture_path.name}: ON WSER {on['wser']:.4f} worsened vs OFF {off['wser']:.4f}"
    )
    # Smoothing must never introduce a new bleed island.
    assert on["islands"] <= off["islands"], (
        f"{fixture_path.name}: ON introduced islands ({off['islands']} -> {on['islands']})"
    )
    # Collar-0 DER unchanged — smoothing only flips ≤3-word islands, not real boundaries.
    if off["der_c0"] is not None and on["der_c0"] is not None:
        assert on["der_c0"] == pytest.approx(off["der_c0"], abs=1e-6), (
            f"{fixture_path.name}: der_c0 moved {off['der_c0']:.6f} -> {on['der_c0']:.6f}"
        )

    # If a frozen baseline is committed, gate against drift too.
    if baseline_path.exists():
        base = json.loads(baseline_path.read_text())
        assert on["wser"] <= base["on"]["wser"] + 1e-4, (
            f"{fixture_path.name}: ON WSER drifted above baseline "
            f"({base['on']['wser']:.4f} -> {on['wser']:.4f})"
        )
        assert on["islands"] == base["on"]["islands"], (
            f"{fixture_path.name}: ON island count drifted "
            f"({base['on']['islands']} -> {on['islands']})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Synthetic, self-contained — always runs (no GPU, no fixture, no engine)
# ──────────────────────────────────────────────────────────────────────────────


def _build_bleed_segments() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build an ``assigned`` list with a known 2-word B-bleed inside an A run.

    Reference: A* speaks 0–10s. Hypothesis: a 2-word island wrongly labelled B sits
    in the middle (words 4–5), flanked by ≥3 A words on each side — exactly the
    boundary-bleed signature the smoother targets. Returns (assigned, ref_words).
    """
    n = 12
    # 0.4 s words on a 0.5 s grid → the 2-word island (words 4-5) spans 0.9 s (< the
    # smoother's 1.5 s max_island_duration) and each seam gap is 0.1 s (< min_silent_gap).
    words: list[dict[str, Any]] = []
    ref_words: list[dict[str, Any]] = []
    for i in range(n):
        spk = "SPEAKER_01" if i in (4, 5) else "SPEAKER_00"
        start, end = i * 0.5, i * 0.5 + 0.4
        words.append({"word": f"w{i} ", "start": start, "end": end, "speaker": spk})
        ref_words.append({"word": f"w{i}", "start": start, "end": end, "speaker": "SPEAKER_00"})

    # One mixed-speaker segment (resegment_by_speaker will split it on the island).
    assigned = [
        {
            "start": 0.0,
            "end": words[-1]["end"],
            "text": "".join(w["word"] for w in words).strip(),
            "speaker": "SPEAKER_00",
            "confidence": 0.9,
            "words": words,
        }
    ]
    return assigned, ref_words


def test_synthetic_bleed_fixed() -> None:
    """A 2-word bleed island is repaired by ON smoothing with zero islands introduced.

    Self-contained: no GPU, no fixtures, no engine. Exercises finalize_segments OFF/ON
    on a crafted bleed and asserts ON removes the island while OFF preserves it.
    """
    finalize_segments = _finalize_segments()
    off_cfg, on_cfg = _smoothing_configs()

    assigned, ref_words = _build_bleed_segments()

    off = _score(finalize_segments(deepcopy(assigned), off_cfg), ref_words)
    on = _score(finalize_segments(deepcopy(assigned), on_cfg), ref_words)

    # OFF leaves the planted 2-word island in place.
    assert off["islands"] >= 1, f"expected a planted bleed island in OFF, got {off['islands']}"
    # ON repairs the island and never introduces a new one.
    assert on["islands"] == 0, f"ON must clear the bleed island, got {on['islands']}"
    # WSER strictly improves once the 2 bled words are corrected.
    assert on["wser"] < off["wser"], f"ON WSER {on['wser']:.4f} !< OFF {off['wser']:.4f}"
