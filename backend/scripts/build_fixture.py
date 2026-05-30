#!/usr/bin/env python
"""Run the engine GPU stage on a WAV and persist the ``RawInferenceResult`` as JSON.

The resulting fixture freezes one model's raw transcription + diarization output (words,
timings, diarize records) so the diarization-boundary benchmark can score many post-process
variants offline without re-running the GPU — the WSER inventory stays identical across runs.

This script needs CUDA + the pyannote GPU fork, so it **refuses to run outside a container**
(same guard as ``scripts/diarization-der.py``). Set ``OPENTRANSCRIBE_IN_CONTAINER=1`` or run
inside Docker (``/.dockerenv`` present). Run from the backend dir with ``PYTHONPATH=.``::

    OPENTRANSCRIBE_IN_CONTAINER=1 PYTHONPATH=. python -m scripts.build_fixture \\
        /data/audio/interview.wav --model large-v3-turbo --out /data/fixtures/interview.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("build_fixture")


def require_container() -> None:
    """Refuse to run outside Docker / without OPENTRANSCRIBE_IN_CONTAINER=1.

    The GPU stage loads CUDA and the pyannote fork; running it on the host can hang or crash
    the display server. Mirrors ``scripts/diarization-der.py``.
    """
    if Path("/.dockerenv").exists() or os.environ.get("OPENTRANSCRIBE_IN_CONTAINER") == "1":
        return
    sys.stderr.write(
        "Refusing to run outside container (needs GPU + pyannote fork). "
        "Run inside Docker or set OPENTRANSCRIBE_IN_CONTAINER=1.\n"
    )
    sys.exit(2)


def build_fixture(
    audio_path: str,
    out_path: str,
    *,
    model_name: str,
    source_language: str = "en",
    min_speakers: int = 1,
    max_speakers: int = 20,
) -> dict:
    """Run preprocess + GPU stage and write the serialized RawInferenceResult to ``out_path``.

    Args:
        audio_path: Path to the source WAV.
        out_path: Destination JSON fixture path.
        model_name: Whisper model to load (e.g. ``large-v3-turbo``).
        source_language: Source language code passed to the engine.
        min_speakers: Diarization minimum speaker count.
        max_speakers: Diarization maximum speaker count.

    Returns:
        The serialized ``RawInferenceResult`` dict that was written.
    """
    from app.transcription.engine import Engine
    from app.transcription.engine import EngineConfig
    from app.transcription.engine.job import JobSpec

    cfg = EngineConfig.from_environment(
        model_name=model_name,
        source_language=source_language,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
    )
    eng = Engine(cfg)

    logger.info("Preprocessing %s", audio_path)
    pre = eng.run_preprocess(JobSpec(audio_path=audio_path, task_id="fixture"))

    logger.info("Running GPU stage (model=%s)", model_name)
    raw = eng.run_gpu_stage(pre)

    payload = raw.serialize()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    logger.info("Wrote fixture to %s", out)
    return payload


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    require_container()

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("audio", help="Path to the source WAV file")
    parser.add_argument("--out", required=True, help="Destination JSON fixture path")
    parser.add_argument("--model", required=True, help="Whisper model name (e.g. large-v3-turbo)")
    parser.add_argument("--source-language", default="en", help="Source language (default: en)")
    parser.add_argument("--min-speakers", type=int, default=1)
    parser.add_argument("--max-speakers", type=int, default=20)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    if not Path(args.audio).is_file():
        logger.error("Audio file not found: %s", args.audio)
        return 2

    build_fixture(
        args.audio,
        args.out,
        model_name=args.model,
        source_language=args.source_language,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
