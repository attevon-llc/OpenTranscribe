"""Build a committed boundary-regression fixture trio (issue #193).

Produces the three files ``test_boundary_regression.test_fixture_regression`` consumes,
all from real downloaded data so future finalize/smoother changes are validated GPU-free:

    <name>.rawinfer.json   frozen GPU output (Engine.run_gpu_stage().serialize())
    <name>.ref.words.json  parallel reference words (turn-midpoint lookup into ref RTTM)
    <name>.baseline.json   {"off": {...}, "on": {...}} frozen WSER/island/der_c0 baseline

The reference RTTM is time-based (model-independent), so the hypothesis word inventory is
derived from THIS fixture's CPU-finalized output and each word inherits the reference
speaker of the turn its midpoint lands in — guaranteeing a parallel inventory for WSER.

Run in the GPU worker container, e.g.::

    docker compose exec -T celery-worker python -m scripts.build_regression_fixture \
        --audio /tmp/karpathy_10m.wav --ref-rttm /tmp/karpathy_ref.rttm \
        --name karpathy_10m --out /tmp/fixtures
"""

from __future__ import annotations

import argparse
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

# Must match backend/tests/integration/test_boundary_regression.py SMOOTHING_ON_KWARGS.
SMOOTHING_ON_KWARGS: dict[str, Any] = {
    "enabled": True,
    "max_island_words": 3,
    "max_island_duration": 1.5,
    "min_flank_words": 3,
    "min_silent_gap": 0.4,
}


def _score(segments: list[dict[str, Any]], ref_words: list[dict[str, Any]]) -> dict[str, Any]:
    """Mirror of the test's _score: WSER + island count + collar-0 DER."""
    from app.utils.diarization_metrics import count_bleed_islands
    from app.utils.diarization_metrics import der
    from app.utils.diarization_metrics import flatten_words
    from app.utils.diarization_metrics import map_hyp_to_ref
    from app.utils.diarization_metrics import read_rttm
    from app.utils.diarization_metrics import words_to_rttm
    from app.utils.diarization_metrics import wser

    hyp_words = flatten_words(segments)
    if len(hyp_words) != len(ref_words):
        raise SystemExit(f"inventory mismatch: {len(ref_words)} ref vs {len(hyp_words)} hyp")
    w = wser(ref_words, hyp_words)
    ref_seq = [rw.get("speaker") for rw in ref_words]
    hyp_seq = map_hyp_to_ref([hw.get("speaker") for hw in hyp_words], w.get("perm", {}))
    islands = count_bleed_islands(ref_seq, hyp_seq, max_island=3)

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

    der_c0: float | None = None
    ref_turns, hyp_turns = _turns(ref_words), _turns(hyp_words)
    if ref_turns and hyp_turns:
        try:
            der_c0 = float(der(ref_turns, hyp_turns, collar=0.0)["der"])
        except (ImportError, KeyError):
            der_c0 = None
    return {"wser": w["wser"], "islands": len(islands), "der_c0": der_c0}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--ref-rttm", required=True)
    ap.add_argument("--name", required=True, help="fixture base name, e.g. karpathy_10m")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--model", default=None, help="override WHISPER_MODEL for the fixture")
    ap.add_argument("--min-speakers", type=int, default=2)
    ap.add_argument("--max-speakers", type=int, default=2)
    args = ap.parse_args()

    from app.transcription.boundary_resolver import BoundarySmoothingConfig
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.engine import Engine
    from app.transcription.engine.job import JobSpec
    from app.utils.diarization_metrics import assign_words_from_turns
    from app.utils.diarization_metrics import flatten_words
    from app.utils.diarization_metrics import read_rttm
    from app.utils.segment_postprocess import finalize_segments

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    overrides: dict[str, Any] = {
        "min_speakers": args.min_speakers,
        "max_speakers": args.max_speakers,
        "source_language": "en",
    }
    if args.model:
        overrides["model_name"] = args.model
    cfg = EngineConfig.from_environment(**overrides)
    eng = Engine(cfg)

    pre = eng.run_preprocess(JobSpec(audio_path=args.audio, task_id="regfix"))
    raw = eng.run_gpu_stage(pre)
    rawinfer = raw.serialize()

    raw_path = out / f"{args.name}.rawinfer.json"
    raw_path.write_text(json.dumps(rawinfer))
    print(f"wrote {raw_path} ({raw_path.stat().st_size // 1024} KB)")

    # Canonical hypothesis inventory = OFF-finalized words (ON has identical words).
    assigned = eng.run_cpu_finalize(raw).segments
    off_cfg = BoundarySmoothingConfig(enabled=False)
    on_cfg = BoundarySmoothingConfig(**SMOOTHING_ON_KWARGS)
    off_segs = finalize_segments(deepcopy(assigned), off_cfg)
    hyp_words = flatten_words(off_segs)

    # Reference words: same inventory, speaker from turn-midpoint lookup into the ref RTTM.
    turns = read_rttm(args.ref_rttm)
    ref_words = assign_words_from_turns(deepcopy(hyp_words), turns)
    ref_path = out / f"{args.name}.ref.words.json"
    ref_path.write_text(json.dumps(ref_words))
    print(f"wrote {ref_path} ({len(ref_words)} words)")

    baseline = {
        "off": _score(off_segs, ref_words),
        "on": _score(finalize_segments(deepcopy(assigned), on_cfg), ref_words),
    }
    base_path = out / f"{args.name}.baseline.json"
    base_path.write_text(json.dumps(baseline, indent=2))
    print(f"wrote {base_path}")
    print(f"BASELINE off={baseline['off']}  on={baseline['on']}")


if __name__ == "__main__":
    main()
