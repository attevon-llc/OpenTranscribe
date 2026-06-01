"""Phase-3 end-to-end check (issue #193): exercise the WIRED engine path.

Unlike ``phase3_measure`` (which calls transcribe/diarize/assign/acoustic_recheck by
hand), this drives the real ``Engine.process`` → ``_GpuStage._run_diarization`` so the
env-gated acoustic re-check block in ``stages.py`` is the thing under test. Runs the same
clip twice — flag OFF then ON — and reports that the wired path completes and how many
word labels the re-check moved. GPU worker container only.
"""

from __future__ import annotations

import argparse
import logging
import os


def _run(audio_path: str, min_spk: int, max_spk: int, model: str | None) -> dict[str, int]:
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.engine import Engine
    from app.transcription.engine.job import JobSpec

    overrides: dict[str, object] = {"min_speakers": min_spk, "max_speakers": max_spk}
    if model:
        overrides["model_name"] = model
    cfg = EngineConfig.from_environment(**overrides)
    result = Engine(cfg).process(JobSpec(audio_path=audio_path, task_id="phase3-e2e"))
    counts: dict[str, int] = {}
    for seg in result.segments:
        for w in seg.get("words", []) or []:
            sp = w.get("speaker")
            if sp is not None:
                counts[sp] = counts.get(sp, 0) + 1
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--min-speakers", type=int, default=2)
    ap.add_argument("--max-speakers", type=int, default=2)
    ap.add_argument("--model", default=None, help="override WHISPER_MODEL (e.g. large-v3)")
    args = ap.parse_args()

    # Surface the acoustic_recheck INFO log line so we can see it fire.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    os.environ["ENGINE_BOUNDARY_ACOUSTIC_RECHECK_ENABLED"] = "false"
    off = _run(args.audio, args.min_speakers, args.max_speakers, args.model)
    print(f"OFF  word-label distribution: {off}")

    os.environ["ENGINE_BOUNDARY_ACOUSTIC_RECHECK_ENABLED"] = "true"
    on = _run(args.audio, args.min_speakers, args.max_speakers, args.model)
    print(f"ON   word-label distribution: {on}")

    moved = sum(abs(on.get(k, 0) - off.get(k, 0)) for k in set(off) | set(on))
    print(
        f"=> wired engine path completed both runs; net label movement {moved} "
        f"(watch for 'acoustic_recheck: reassigned N' above to confirm the stage fired)"
    )


if __name__ == "__main__":
    main()
