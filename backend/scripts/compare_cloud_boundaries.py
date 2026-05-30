"""Cloud-vs-local boundary + SPEED comparison on a labeled clip (issue #193, plan §6a).

Scores how each engine handles speaker boundaries against the SAME human-labeled
reference turns (reference.rttm) AND how fast each returns a result:
  - local engine (real GPU run, models warm) — uncorrected (OFF) and smoothed (ON)
  - each configured cloud provider (pyannote.ai, Deepgram) — end-to-end API latency

Every engine has its own word inventory, so words are midpoint-mapped to the reference
turns and scored with WSER + bleed-island count. Speed is reported as wall-clock seconds
and realtime factor (audio_seconds / wall_seconds; higher = faster).

    docker compose exec -T celery-worker python -m scripts.compare_cloud_boundaries \
        --audio /tmp/karpathy_10m.wav --ref-rttm /tmp/karpathy_ref.rttm \
        --cloud 849 --cloud 610 --max-speakers 2
"""

from __future__ import annotations

import argparse
import time
from copy import deepcopy
from typing import Any


def _score(words: list[dict[str, Any]], turns: list) -> dict[str, Any]:
    """WSER + island count + speaker count for hyp words vs reference turns."""
    from app.utils.diarization_metrics import assign_words_from_turns
    from app.utils.diarization_metrics import count_bleed_islands
    from app.utils.diarization_metrics import map_hyp_to_ref
    from app.utils.diarization_metrics import wser

    if not words:
        return {"words": 0, "speakers": 0, "wser": None, "islands": None}
    ref_words = assign_words_from_turns(deepcopy(words), turns)
    w = wser(ref_words, words)
    ref_seq = [rw.get("speaker") for rw in ref_words]
    hyp_seq = map_hyp_to_ref([hw.get("speaker") for hw in words], w.get("perm", {}))
    islands = count_bleed_islands(ref_seq, hyp_seq, max_island=3)
    return {
        "words": len(words),
        "speakers": len({x["speaker"] for x in words if x.get("speaker")}),
        "wser": w["wser"],
        "islands": len(islands),
    }


def _flatten_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "start": x["start"],
            "end": x.get("end", x["start"]),
            "word": x.get("word", ""),
            "speaker": x["speaker"],
        }
        for s in segments
        for x in s.get("words", []) or []
        if "speaker" in x and "start" in x
    ]


def _flatten_asr(result: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in result.segments:
        for w in seg.words or []:
            out.append({"start": w.start, "end": w.end, "word": w.word, "speaker": seg.speaker})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--ref-rttm", required=True)
    ap.add_argument("--cloud", type=int, action="append", default=[], help="user_asr_settings id")
    ap.add_argument("--min-speakers", type=int, default=2)
    ap.add_argument("--max-speakers", type=int, default=2)
    args = ap.parse_args()

    from app.transcription.audio import load_audio
    from app.utils.diarization_metrics import read_rttm

    audio = load_audio(args.audio)
    audio_seconds = len(audio) / 16000.0
    turns = read_rttm(args.ref_rttm)
    rows: list[tuple[str, dict[str, Any], float]] = []  # (name, score, wall_seconds)

    # ── Local engine: real warm GPU run, timed ───────────────────────────────────
    off, on, local_seconds = _run_local_timed(args.audio, args.min_speakers, args.max_speakers)
    rows.append(("local (uncorrected/OFF)", _score(_flatten_segments(off), turns), local_seconds))
    rows.append(("local (smoothed/ON)", _score(_flatten_segments(on), turns), local_seconds))

    # ── Cloud providers: end-to-end API latency ──────────────────────────────────
    for config_id in args.cloud:
        name, words, secs = _run_cloud(config_id, args.audio, args.min_speakers, args.max_speakers)
        rows.append((name, _score(words, turns), secs))

    # ── Report ───────────────────────────────────────────────────────────────────
    print(f"\naudio duration: {audio_seconds:.0f} s ({audio_seconds / 60:.1f} min)\n")
    print(f"{'engine':26s} {'words':>6} {'spk':>4} {'WSER':>7} {'isl':>4} {'time':>9} {'xRT':>7}")
    print("-" * 70)
    for name, r, secs in rows:
        wser_s = f"{r['wser'] * 100:.2f}%" if r["wser"] is not None else "n/a"
        isl_s = str(r["islands"]) if r["islands"] is not None else "n/a"
        rtf = f"{audio_seconds / secs:.1f}x" if secs > 0 else "n/a"
        print(
            f"{name:26s} {r['words']:>6} {r['speakers']:>4} {wser_s:>7} {isl_s:>4} "
            f"{secs:>7.1f}s {rtf:>7}"
        )
    print("\nWSER vs your labeled reference.rttm (lower = better boundaries).")
    print("time = wall clock to result; xRT = realtime factor (higher = faster).")
    print("local time is warm-model GPU processing; cloud time is end-to-end API latency.")


def _run_local_timed(
    audio_path: str, min_speakers: int, max_speakers: int
) -> tuple[list, list, float]:
    """Run the real local engine warm and time the GPU stage. Returns (off, on, seconds)."""
    from app.transcription.boundary_resolver import BoundarySmoothingConfig
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.engine import Engine
    from app.transcription.engine.job import JobSpec
    from app.utils.segment_postprocess import finalize_segments

    cfg = EngineConfig.from_environment(
        min_speakers=min_speakers, max_speakers=max_speakers, source_language="en"
    )
    eng = Engine(cfg)
    pre = eng.run_preprocess(JobSpec(audio_path=audio_path, task_id="cmp-warm"))

    eng.run_gpu_stage(pre)  # warmup — load + pin models so timing reflects production

    t0 = time.perf_counter()
    raw = eng.run_gpu_stage(pre)
    assigned = eng.run_cpu_finalize(raw).segments
    seconds = time.perf_counter() - t0

    off = finalize_segments(deepcopy(assigned), BoundarySmoothingConfig(enabled=False))
    on = finalize_segments(deepcopy(assigned), BoundarySmoothingConfig(enabled=True))
    return off, on, seconds


def _run_cloud(
    config_id: int, audio: str, min_speakers: int, max_speakers: int
) -> tuple[str, list[dict[str, Any]], float]:
    from app.db.session_utils import session_scope
    from app.models.user_asr_settings import UserASRSettings
    from app.services.asr.types import ASRConfig
    from app.utils.encryption import decrypt_api_key

    with session_scope() as db:
        cfg = db.query(UserASRSettings).filter(UserASRSettings.id == config_id).first()
        if cfg is None:
            return (f"cloud#{config_id} (missing)", [], 0.0)
        provider_name, model_name = str(cfg.provider), str(cfg.model_name)
        api_key = decrypt_api_key(str(cfg.api_key)) if cfg.api_key else None
    if not api_key:
        return (f"{provider_name} (no key)", [], 0.0)

    if provider_name == "pyannote":
        from app.services.asr.pyannote_provider import PyAnnoteProvider

        provider: Any = PyAnnoteProvider(api_key, model_name)
    elif provider_name == "deepgram":
        from app.services.asr.deepgram_provider import DeepgramProvider

        provider = DeepgramProvider(api_key, model_name)
    elif provider_name == "assemblyai":
        from app.services.asr.assemblyai_provider import AssemblyAIProvider

        provider = AssemblyAIProvider(api_key, model_name)
    elif provider_name == "gladia":
        from app.services.asr.gladia_provider import GladiaProvider

        provider = GladiaProvider(api_key, model_name)
    else:
        return (f"{provider_name} (unsupported)", [], 0.0)

    t0 = time.perf_counter()
    result = provider.transcribe(
        audio,
        ASRConfig(
            language="en",
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            enable_diarization=True,
        ),
    )
    seconds = time.perf_counter() - t0
    return (f"{provider_name}/{model_name}", _flatten_asr(result), seconds)


if __name__ == "__main__":
    main()
