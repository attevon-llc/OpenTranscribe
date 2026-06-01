"""Phase-3 effect measurement (issue #193): does acoustic_recheck cut backchannel error?

Replicates the pipeline (transcribe → diarize → assign) once, then scores WSER + the
boundary/backchannel error split against a labeled RTTM, comparing smoother-only vs
acoustic-recheck-then-smoother. Tells us whether the acoustic pass on short
disputed/overlap words actually helps before wiring it into the engine. GPU worker only.
"""

from __future__ import annotations

import argparse
import copy


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--rttm", required=True)
    ap.add_argument("--min-speakers", type=int, default=2)
    ap.add_argument("--max-speakers", type=int, default=2)
    ap.add_argument("--cosine-margin", type=float, default=0.05)
    ap.add_argument("--max-word-dur", type=float, default=1.0)
    args = ap.parse_args()

    from app.transcription.audio import load_audio
    from app.transcription.boundary_resolver import BoundarySmoothingConfig
    from app.transcription.boundary_resolver import acoustic_recheck
    from app.transcription.config import TranscriptionConfig
    from app.transcription.model_manager import ModelManager
    from app.transcription.speaker_assigner import assign_speakers
    from app.utils.diarization_metrics import assign_words_from_turns
    from app.utils.diarization_metrics import categorize_errors
    from app.utils.diarization_metrics import count_bleed_islands
    from app.utils.diarization_metrics import flatten_words
    from app.utils.diarization_metrics import map_hyp_to_ref
    from app.utils.diarization_metrics import read_rttm
    from app.utils.diarization_metrics import wser
    from app.utils.segment_postprocess import finalize_segments

    audio = load_audio(args.audio)
    tc = TranscriptionConfig.from_environment(
        min_speakers=args.min_speakers, max_speakers=args.max_speakers
    )
    mgr = ModelManager.get_instance()
    transcript = mgr.get_transcriber(tc).transcribe(audio)
    diarizer = mgr.get_diarizer(tc)
    diarize_df, overlap_info, centroids = diarizer.diarize(audio)
    if not centroids:
        raise SystemExit("no speaker centroids produced — cannot run acoustic re-check")
    base_segs = assign_speakers(diarize_df, transcript)["segments"]
    turns = read_rttm(args.rttm)
    smoother = BoundarySmoothingConfig(enabled=True)

    def score(segs: list[dict]) -> tuple[float, int, int, int]:
        final = finalize_segments(copy.deepcopy(segs), smoother)
        hyp = flatten_words(final)
        ref = assign_words_from_turns(hyp, turns)
        res = wser(ref, hyp)
        hyp_seq = map_hyp_to_ref([w["speaker"] for w in hyp], res["perm"])
        isl = count_bleed_islands([w["speaker"] for w in ref], hyp_seq)
        cats = categorize_errors(ref, hyp, res["perm"])
        return res["wser"], len(isl), cats["boundary_errors"], cats["interior_errors"]

    w0, i0, a0, b0 = score(base_segs)
    print(f"smoother only:     WSER={w0:.4f}  islands={i0}  A(boundary)={a0}  B(backchannel)={b0}")

    ar_segs = copy.deepcopy(base_segs)
    words = [
        w for s in ar_segs for w in s.get("words", []) or [] if "speaker" in w and "start" in w
    ]
    import time

    t0 = time.perf_counter()
    n = acoustic_recheck(
        words,
        centroids,
        lambda s, e: diarizer.embed_window(audio, s, e),
        overlap_regions=overlap_info.get("regions"),
        cosine_margin=args.cosine_margin,
        max_word_dur=args.max_word_dur,
    )
    recheck_ms = (time.perf_counter() - t0) * 1000.0
    print(
        f"acoustic_recheck wall time: {recheck_ms:.0f} ms for {len(words)} words ({n} reassigned)"
    )
    w1, i1, a1, b1 = score(ar_segs)
    print(
        f"acoustic+smoother: WSER={w1:.4f}  islands={i1}  A(boundary)={a1}  B(backchannel)={b1}"
        f"   (reassigned {n} words)"
    )
    print(f"=> WSER delta {w0 - w1:+.4f}   backchannel-errors {b0} -> {b1}   ({n} words moved)")


if __name__ == "__main__":
    main()
