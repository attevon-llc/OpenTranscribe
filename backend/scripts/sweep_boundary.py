"""Threshold sweep for the boundary smoother against a labeled reference (issue #193).

Transcribes the audio ONCE (the expensive GPU step), then sweeps the smoother's
N/T/M/G knobs over the cached assignment (CPU-only, fast) and scores each config's
per-word speakers against the ground-truth RTTM. Reports WSER + bleed-island count +
the A/B error split per config so we can pick the config that maximizes the fix without
eating backchannels. Run in the GPU worker container.
"""

from __future__ import annotations

import argparse
import copy

from app.transcription.boundary_resolver import BoundarySmoothingConfig
from app.transcription.engine.config import EngineConfig
from app.transcription.engine.engine import Engine
from app.transcription.engine.job import JobSpec
from app.utils.diarization_metrics import assign_words_from_turns
from app.utils.diarization_metrics import categorize_errors
from app.utils.diarization_metrics import count_bleed_islands
from app.utils.diarization_metrics import flatten_words
from app.utils.diarization_metrics import map_hyp_to_ref
from app.utils.diarization_metrics import read_rttm
from app.utils.diarization_metrics import wser
from app.utils.segment_postprocess import finalize_segments

# (max_island_words, max_island_duration, min_flank_words, min_silent_gap)
GRID: list[tuple[int, float, int, float]] = [
    (1, 0.5, 3, 0.4),
    (2, 1.0, 3, 0.4),
    (3, 1.5, 3, 0.4),
    (3, 1.5, 2, 0.4),
    (3, 1.5, 3, 0.3),
    (3, 1.5, 3, 0.6),
    (3, 2.0, 3, 0.5),
]


def _score(segs: list[dict], turns: list, max_island: int = 3) -> dict:
    hyp = flatten_words(segs)
    ref = assign_words_from_turns(hyp, turns)
    res = wser(ref, hyp)
    hyp_seq = map_hyp_to_ref([w["speaker"] for w in hyp], res["perm"])
    ref_seq = [w["speaker"] for w in ref]
    isl = count_bleed_islands(ref_seq, hyp_seq, max_island=max_island)
    cats = categorize_errors(ref, hyp, res["perm"])
    return {
        "wser": res["wser"],
        "islands": len(isl),
        "A": cats["boundary_errors"],
        "B": cats["interior_errors"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--rttm", required=True)
    ap.add_argument("--min-speakers", type=int)
    ap.add_argument("--max-speakers", type=int)
    args = ap.parse_args()

    overrides: dict = {}
    if args.min_speakers:
        overrides["min_speakers"] = args.min_speakers
    if args.max_speakers:
        overrides["max_speakers"] = args.max_speakers
    eng = Engine(EngineConfig.from_environment(**overrides))
    assigned = eng.process(JobSpec(audio_path=args.audio, task_id="sweep")).segments  # GPU once
    turns = read_rttm(args.rttm)

    off = _score(finalize_segments(copy.deepcopy(assigned), None), turns)
    print(f"\n{'config (N,T,M,G)':<24} {'WSER':>8} {'Δrel':>7} {'islands':>8} {'A':>4} {'B':>5}")
    print(
        f"{'OFF':<24} {off['wser']:>8.4f} {'—':>7} {off['islands']:>8} {off['A']:>4} {off['B']:>5}"
    )
    best: tuple[str | None, float] = (None, off["wser"])
    for n, t, m, g in GRID:
        cfg = BoundarySmoothingConfig(
            enabled=True,
            max_island_words=n,
            max_island_duration=t,
            min_flank_words=m,
            min_silent_gap=g,
        )
        on = _score(finalize_segments(copy.deepcopy(assigned), cfg), turns, max_island=n)
        rel = (off["wser"] - on["wser"]) / off["wser"] if off["wser"] else 0.0
        tag = f"N={n},T={t},M={m},G={g}"
        print(
            f"{tag:<24} {on['wser']:>8.4f} {rel:>6.0%} {on['islands']:>8} {on['A']:>4} {on['B']:>5}"
        )
        if on["wser"] < best[1]:
            best = (tag, on["wser"])
    print(f"\nBEST: {best[0]}  WSER={best[1]:.4f}  (OFF={off['wser']:.4f})")


if __name__ == "__main__":
    main()
