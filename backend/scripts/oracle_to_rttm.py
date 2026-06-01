#!/usr/bin/env python
"""Convert a cloud provider's diarized JSON output into an RTTM turn track.

These "oracle" RTTMs are the high-quality third-party diarizations used as candidate
references (or cross-checks) for the diarization-boundary benchmark. Each ``--provider``
maps to the response shape that provider's ``*_provider.py`` already parses, so this script
mirrors that shape rather than guessing:

* ``deepgram`` — ``results.channels[].alternatives[].words[]``; each word has an integer
  ``speaker`` plus ``start``/``end``. Mirrors ``deepgram_provider.py``.
* ``aws`` — AWS Transcribe ``results.speaker_labels.segments[]``; each segment has
  ``start_time``/``end_time``/``speaker_label``. Mirrors the ``spk_map`` build in
  ``aws_provider.py`` (the segment block is the authoritative turn track).
* ``pyannote`` — pyannote.ai STT Orchestration ``output``; uses ``diarization`` when present,
  else ``turnLevelTranscription`` (each entry = one speaker turn). Mirrors
  ``pyannote_provider.py::_parse_response``.

Speaker ids are normalized to canonical ``SPEAKER_XX`` via the shared ASR base method so
labels line up with the engine's own output.

Run::

    PYTHONPATH=. python -m scripts.oracle_to_rttm --provider deepgram out.json --out oracle.rttm
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from app.services.asr.base import ASRProvider

logger = logging.getLogger("oracle_to_rttm")

# Merge consecutive same-speaker turns whose gap is below this (seconds) into one turn.
_MERGE_GAP = 0.5


class _Normalizer(ASRProvider):
    """Minimal concrete ASRProvider used only for its ``_normalize_speaker_label`` helper."""

    @property
    def provider_name(self) -> str:
        return "oracle"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return False

    def supports_translation(self) -> bool:
        return False

    def validate_connection(self) -> tuple[bool, str, float]:  # pragma: no cover - unused
        return True, "n/a", 0.0

    def transcribe(self, audio_path, config, progress_callback=None):  # pragma: no cover - unused
        raise NotImplementedError


_NORM = _Normalizer()


def _norm(label: Any) -> str | None:
    """Normalize a raw provider speaker label to canonical ``SPEAKER_XX``."""
    return _NORM._normalize_speaker_label(label)


def _collapse(raw: list[tuple[float, float, str | None]]) -> list[tuple[float, float, str]]:
    """Sort by start and merge adjacent same-speaker spans separated by a small gap."""
    spans = sorted((s, e, spk) for s, e, spk in raw if spk is not None and e > s)
    out: list[tuple[float, float, str]] = []
    for start, end, spk in spans:
        if out and out[-1][2] == spk and start - out[-1][1] <= _MERGE_GAP:
            ps, _pe, pspk = out[-1]
            out[-1] = (ps, max(out[-1][1], end), pspk)
        else:
            out.append((start, end, spk))
    return out


def parse_deepgram(data: dict[str, Any]) -> list[tuple[float, float, str]]:
    """Parse Deepgram ``results.channels[0].alternatives[0].words[]`` into turns.

    Each word carries an integer ``speaker`` (0-indexed) and ``start``/``end``; consecutive
    same-speaker words become a turn. Mirrors the diarized path of ``deepgram_provider.py``.
    """
    results = data.get("results", data)
    channels = results.get("channels") or []
    if not channels:
        logger.warning("Deepgram JSON has no channels")
        return []
    alternatives = channels[0].get("alternatives") or []
    if not alternatives:
        return []
    words = alternatives[0].get("words") or []
    raw: list[tuple[float, float, str | None]] = []
    for w in words:
        spk = w.get("speaker")
        start, end = w.get("start"), w.get("end")
        if spk is None or start is None or end is None:
            continue
        raw.append((float(start), float(end), _norm(spk)))
    return _collapse(raw)


def parse_aws(data: dict[str, Any]) -> list[tuple[float, float, str]]:
    """Parse AWS Transcribe ``results.speaker_labels.segments[]`` into turns.

    The ``speaker_labels`` block is the authoritative diarization in AWS output; each
    segment carries ``start_time``/``end_time``/``speaker_label``. Mirrors the source
    block ``aws_provider.py`` reads to build its ``spk_map``.
    """
    results = data.get("results", data)
    speaker_labels = results.get("speaker_labels") or {}
    segments = speaker_labels.get("segments") or []
    raw: list[tuple[float, float, str | None]] = []
    for seg in segments:
        start = seg.get("start_time")
        end = seg.get("end_time")
        label = seg.get("speaker_label")
        if start is None or end is None or label is None:
            continue
        raw.append((float(start), float(end), _norm(label)))
    return _collapse(raw)


def parse_pyannote(data: dict[str, Any]) -> list[tuple[float, float, str]]:
    """Parse pyannote.ai STT Orchestration output into turns.

    Prefers ``output.diarization`` (pure diarization segments); falls back to
    ``output.turnLevelTranscription`` (one entry per speaker turn). Each entry has
    ``start``/``end``/``speaker``. Mirrors ``pyannote_provider.py::_parse_response``.
    Accepts either a full job payload (``{"output": {...}}``) or a bare ``output`` dict.
    """
    output = data.get("output", data)
    segments = output.get("diarization") or output.get("turnLevelTranscription") or []
    raw: list[tuple[float, float, str | None]] = []
    for seg in segments:
        start = seg.get("start")
        end = seg.get("end")
        spk = seg.get("speaker")
        if start is None or end is None or spk is None:
            continue
        raw.append((float(start), float(end), _norm(spk)))
    return _collapse(raw)


_PARSERS = {
    "deepgram": parse_deepgram,
    "aws": parse_aws,
    "pyannote": parse_pyannote,
}


def turns_to_rttm(turns: list[tuple[float, float, str]], uri: str = "file") -> str:
    """Serialize ``(start, end, speaker)`` turns into RTTM ``SPEAKER`` lines."""
    lines = [
        f"SPEAKER {uri} 1 {start:.3f} {max(0.0, end - start):.3f} <NA> <NA> {spk} <NA> <NA>"
        for start, end, spk in turns
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("input", type=Path, help="Provider diarized output JSON")
    parser.add_argument(
        "--provider", required=True, choices=sorted(_PARSERS), help="Input JSON shape"
    )
    parser.add_argument("--out", type=Path, help="Output RTTM path (default: stdout)")
    parser.add_argument("--uri", default="file", help="RTTM uri/recording id (default: file)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    if not args.input.is_file():
        logger.error("Input file not found: %s", args.input)
        return 2

    data = json.loads(args.input.read_text(encoding="utf-8"))
    turns = _PARSERS[args.provider](data)
    if not turns:
        logger.error("Parsed zero turns from %s (provider=%s)", args.input, args.provider)
        return 1

    rttm = turns_to_rttm(turns, uri=args.uri)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rttm, encoding="utf-8")
        logger.info("Wrote %d turns to %s", len(turns), args.out)
    else:
        print(rttm, end="")  # noqa: T201 — explicit stdout for piping
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
