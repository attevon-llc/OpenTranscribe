"""Measure speaker-boundary errors on a labeled dataset file vs its ground-truth RTTM.

Transcribes + diarizes one audio file through the engine, scores the per-word speaker
labels against the dataset's authoritative time-based RTTM (midpoint lookup), and reports
WSER + bleed-island counts with the smoother OFF and ON. This is the per-file core of the
issue #193 tuning loop — it answers "does our pipeline bleed at boundaries on real labeled
data, and does the smoother fix it without making things worse?".

Run in the GPU worker container:
    docker compose exec -T celery-worker python -m scripts.measure_dataset_boundaries \
        --audio /tmp/IS1008a.wav --rttm /tmp/IS1008a.rttm [--max-island 3]
"""

from __future__ import annotations

import argparse
import copy
import json
import time

from app.transcription.boundary_resolver import BoundarySmoothingConfig
from app.transcription.boundary_resolver import smooth_word_speakers
from app.utils.diarization_metrics import assign_words_from_turns
from app.utils.diarization_metrics import categorize_errors
from app.utils.diarization_metrics import count_bleed_islands
from app.utils.diarization_metrics import der
from app.utils.diarization_metrics import flatten_words
from app.utils.diarization_metrics import island_histogram
from app.utils.diarization_metrics import map_hyp_to_ref
from app.utils.diarization_metrics import read_rttm
from app.utils.diarization_metrics import speaker_count_match
from app.utils.diarization_metrics import wser


def _transcribe(audio_path: str, min_speakers: int | None, max_speakers: int | None) -> list[dict]:
    """Run the engine end-to-end and return assigned segments (per-word speakers)."""
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.engine import Engine
    from app.transcription.engine.job import JobSpec

    overrides: dict = {}
    if min_speakers:
        overrides["min_speakers"] = min_speakers
    if max_speakers:
        overrides["max_speakers"] = max_speakers
    cfg = EngineConfig.from_environment(**overrides)
    eng = Engine(cfg)
    return eng.process(JobSpec(audio_path=audio_path, task_id="ds-measure")).segments


def _score(assigned: list[dict], turns: list, max_island: int) -> dict:
    """Score per-word speakers vs reference turns: WSER + bleed-island count."""
    hyp_words = flatten_words(assigned)
    ref_words = assign_words_from_turns(hyp_words, turns)  # parallel inventory
    res = wser(ref_words, hyp_words)
    ref_seq = [w["speaker"] for w in ref_words]
    hyp_seq = map_hyp_to_ref([w["speaker"] for w in hyp_words], res["perm"])
    islands = count_bleed_islands(ref_seq, hyp_seq, max_island=max_island)
    cats = categorize_errors(ref_words, hyp_words, res["perm"])
    # DER (literature-comparable): hypothesis diarization turns = one-speaker segments.
    hyp_turns = [
        (s["start"], s["end"], s["speaker"])
        for s in assigned
        if s.get("speaker") and s.get("start") is not None and s.get("end") is not None
    ]
    try:
        der_val: float | None = round(der(turns, hyp_turns, collar=0.25)["der"], 4)
    except Exception:
        der_val = None
    return {
        "wser": round(res["wser"], 4),
        "t_wser": round(res["t_wser"], 4),
        "der": der_val,
        "n_words": len(hyp_words),
        "n_scored": res["n_scored"],
        "n_excluded": res["n_excluded"],
        "islands": len(islands),
        "island_hist": island_histogram(islands),
        "boundary_errors": cats["boundary_errors"],
        "interior_errors": cats["interior_errors"],
        "speaker_count": speaker_count_match(ref_words, hyp_words),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--rttm", required=True)
    ap.add_argument("--max-island", type=int, default=3)
    ap.add_argument("--min-speakers", type=int)
    ap.add_argument("--max-speakers", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    turns = read_rttm(args.rttm)
    t0 = time.time()
    assigned = _transcribe(args.audio, args.min_speakers, args.max_speakers)
    gpu_s = round(time.time() - t0, 1)

    off = _score(assigned, turns, args.max_island)
    on_segs = copy.deepcopy(assigned)
    smooth_word_speakers(on_segs, BoundarySmoothingConfig(enabled=True))
    on = _score(on_segs, turns, args.max_island)

    result = {
        "audio": args.audio,
        "ref_turns": len(turns),
        "gpu_seconds": gpu_s,
        "off": off,
        "on": on,
        "delta_wser": round(off["wser"] - on["wser"], 4),
        "islands_fixed": off["islands"] - on["islands"],
    }
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"\n=== {args.audio.split('/')[-1]} | ref_turns={len(turns)} | gpu={gpu_s}s ===")
    print(
        f"  ref speakers={off['speaker_count']['ref_speakers']}  "
        f"hyp speakers={off['speaker_count']['hyp_speakers']}  "
        f"count_match={off['speaker_count']['match']}  "
        f"DER@0.25={off['der']}"
    )
    print(
        f"  OFF: WSER={off['wser']:.4f}  islands={off['islands']} {off['island_hist']}  "
        f"words={off['n_words']} (excluded={off['n_excluded']})"
    )
    print(
        f"       errors by mode: boundary-bleed(A)={off['boundary_errors']}  "
        f"backchannel/interior(B)={off['interior_errors']}"
    )
    print(f"  ON : WSER={on['wser']:.4f}  islands={on['islands']} {on['island_hist']}")
    print(
        f"  → WSER delta (OFF-ON)={result['delta_wser']:+.4f}  "
        f"islands fixed={result['islands_fixed']}"
    )


if __name__ == "__main__":
    main()
