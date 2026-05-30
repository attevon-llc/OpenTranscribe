"""End-to-end model comparison for the boundary fix (issue #193).

For each Whisper model, runs the FULL engine (transcribe + diarize + assign), then:
  1. **Validates word timestamps** — catches "time-processing" errors (start>end,
     non-monotonic/backwards words, implausible gaps) that would corrupt boundary logic.
     This is the guard for swapping models (esp. CrisperWhisper's unusual tokenization).
  2. Applies the smoother (finalize_segments) and scores WSER + bleed-islands vs a
     ground-truth RTTM, smoother OFF vs ON.

Run in the GPU worker:
    python -m scripts.compare_models_boundary --audio /tmp/clip.wav --rttm /tmp/ref.rttm \
        --models large-v3-turbo,large-v3,crisperwhisper --min-speakers 2 --max-speakers 2
"""

from __future__ import annotations

import argparse
import copy
import time

from app.transcription.boundary_resolver import BoundarySmoothingConfig
from app.transcription.engine.config import EngineConfig
from app.transcription.engine.engine import Engine
from app.transcription.engine.job import JobSpec
from app.utils.diarization_metrics import assign_words_from_turns
from app.utils.diarization_metrics import count_bleed_islands
from app.utils.diarization_metrics import flatten_words
from app.utils.diarization_metrics import map_hyp_to_ref
from app.utils.diarization_metrics import read_rttm
from app.utils.diarization_metrics import wser
from app.utils.segment_postprocess import finalize_segments


def _validate_timestamps(words: list[dict]) -> dict:
    """Flag time-processing errors in the per-word timestamps."""
    bad_order = 0  # start > end
    backwards = 0  # word starts before previous word ended (beyond small tolerance)
    big_gap = 0  # implausible silent gap inside a single speaker run
    prev_end = None
    for w in words:
        s, e = w.get("start"), w.get("end")
        if s is None or e is None:
            continue
        if s > e + 1e-3:
            bad_order += 1
        if prev_end is not None and s < prev_end - 0.05:
            backwards += 1
        if prev_end is not None and s - prev_end > 30.0:
            big_gap += 1
        prev_end = e
    return {
        "bad_order": bad_order,
        "backwards": backwards,
        "big_gap": big_gap,
        "ok": bad_order == 0 and backwards == 0,
    }


def _score(segs: list[dict], turns: list) -> dict:
    hyp = flatten_words(segs)
    ref = assign_words_from_turns(hyp, turns)
    res = wser(ref, hyp)
    hyp_seq = map_hyp_to_ref([w["speaker"] for w in hyp], res["perm"])
    isl = count_bleed_islands([w["speaker"] for w in ref], hyp_seq)
    return {"wser": res["wser"], "islands": len(isl), "n_words": len(hyp)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--rttm", required=True)
    ap.add_argument("--models", default="large-v3-turbo")
    ap.add_argument("--min-speakers", type=int)
    ap.add_argument("--max-speakers", type=int)
    args = ap.parse_args()

    turns = read_rttm(args.rttm)
    base: dict = {}
    if args.min_speakers:
        base["min_speakers"] = args.min_speakers
    if args.max_speakers:
        base["max_speakers"] = args.max_speakers

    on_cfg = BoundarySmoothingConfig(enabled=True)
    print(
        f"\n{'model':<26} {'gpu_s':>6} {'words':>6} {'ts_ok':>6} {'WSER_off':>9} {'WSER_on':>8} "
        f"{'isl_off':>7} {'isl_on':>6}"
    )
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        try:
            eng = Engine(EngineConfig.from_environment(model_name=model, **base))
            t0 = time.time()
            assigned = eng.process(JobSpec(audio_path=args.audio, task_id="cmp")).segments
            gpu_s = time.time() - t0
            ts = _validate_timestamps(flatten_words(assigned))
            off = _score(finalize_segments(copy.deepcopy(assigned), None), turns)
            on = _score(finalize_segments(copy.deepcopy(assigned), on_cfg), turns)
            flag = "OK" if ts["ok"] else f"BAD({ts['bad_order']},{ts['backwards']})"
            print(
                f"{model:<26} {gpu_s:>6.1f} {off['n_words']:>6} {flag:>6} "
                f"{off['wser']:>9.4f} {on['wser']:>8.4f} {off['islands']:>7} {on['islands']:>6}"
            )
        except Exception as e:  # noqa: BLE001 - report per-model failure, keep going
            print(f"{model:<26} FAILED: {str(e)[:60]}")


if __name__ == "__main__":
    main()
