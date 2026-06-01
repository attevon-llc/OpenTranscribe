#!/usr/bin/env python
"""Audacity seam-label tooling for the diarization-boundary benchmark.

Two directions:

``emit``
    Read a transcript JSON (segments → words carrying ``speaker``/``start``/``end``) and
    write an Audacity label track marking every speaker-change *seam*. Each label is a
    ``±pad`` second window centred on a turn-change boundary so a human can open the audio
    in Audacity, listen at each seam, and drag-correct the boundary speaker. Line format::

        <seam_start>\\t<seam_end>\\tSEAM <spkA>-><spkB>

``to-rttm``
    Read a *corrected* Audacity label track — lines of ``start\\tend\\tSPEAKER`` where the
    label text is the true speaker over that span — back into ``(start, end, speaker)``
    turns and emit ``reference.turns.rttm``.

Audacity label tracks are tab-separated ``start<TAB>end<TAB>label`` with times in seconds.

Run::

    PYTHONPATH=. python -m scripts.make_seam_labels emit transcript.json --out seams.txt
    PYTHONPATH=. python -m scripts.make_seam_labels to-rttm corrected.txt --out ref.rttm
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from app.utils.diarization_metrics import flatten_words

logger = logging.getLogger("make_seam_labels")

DEFAULT_PAD = 2.0


def find_seams(words: list[dict[str, Any]], pad: float = DEFAULT_PAD) -> list[dict[str, Any]]:
    """Locate speaker-change boundaries in an ordered word list.

    A seam sits between word ``i-1`` (speaker A) and word ``i`` (speaker B) when ``A != B``
    and both speakers are known. The boundary time is the midpoint between A's end and B's
    start; the seam window is ``[boundary - pad, boundary + pad]`` (clamped at 0).

    Args:
        words: Ordered word dicts with ``speaker``/``start``/``end`` (e.g. from
            :func:`flatten_words`).
        pad: Half-width of the seam window in seconds.

    Returns:
        ``[{"start": float, "end": float, "from": spkA, "to": spkB}, ...]`` in time order.
    """
    seams: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for w in words:
        spk = w.get("speaker")
        if spk is None:
            continue
        if prev is not None and prev["speaker"] != spk:
            prev_end = prev.get("end")
            cur_start = w.get("start")
            if prev_end is not None and cur_start is not None:
                boundary = (float(prev_end) + float(cur_start)) / 2.0
            elif cur_start is not None:
                boundary = float(cur_start)
            elif prev_end is not None:
                boundary = float(prev_end)
            else:
                prev = w
                continue
            seams.append(
                {
                    "start": max(0.0, boundary - pad),
                    "end": boundary + pad,
                    "from": prev["speaker"],
                    "to": spk,
                }
            )
        prev = w
    return seams


def load_transcript_words(path: Path) -> list[dict[str, Any]]:
    """Load a transcript JSON and flatten its segments into an ordered word list.

    Accepts either a top-level ``{"segments": [...]}`` object or a bare list of segments.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = data.get("segments", data) if isinstance(data, dict) else data
    if not isinstance(segments, list):
        raise ValueError(f"Transcript {path} has no segment list")
    return flatten_words(segments)


def emit_seam_labels(words: list[dict[str, Any]], pad: float = DEFAULT_PAD) -> str:
    """Render seams as Audacity label-track text (one tab-separated line per seam)."""
    lines = [
        f"{s['start']:.3f}\t{s['end']:.3f}\tSEAM {s['from']}->{s['to']}"
        for s in find_seams(words, pad=pad)
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def turns_from_seam_labels(labels_path: Path | str) -> list[tuple[float, float, str]]:
    """Read a corrected Audacity label track into ``(start, end, speaker)`` turns.

    Each non-empty line is ``start<TAB>end<TAB>SPEAKER``. The label text is taken verbatim
    as the speaker id (whitespace-stripped). Lines that still carry a ``SEAM a->b`` marker
    (i.e. uncorrected) are skipped — only resolved single-speaker labels become turns.
    Whitespace-delimited fallback is supported for tracks saved without tabs.
    """
    turns: list[tuple[float, float, str]] = []
    path = Path(labels_path)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            parts = line.split()
        if len(parts) < 3:
            logger.warning("Skipping malformed label line: %r", raw)
            continue
        try:
            start, end = float(parts[0]), float(parts[1])
        except ValueError:
            logger.warning("Skipping line with non-numeric times: %r", raw)
            continue
        label = " ".join(parts[2:]).strip()
        if label.upper().startswith("SEAM ") or "->" in label:
            logger.debug("Skipping uncorrected seam marker: %r", raw)
            continue
        if end > start and label:
            turns.append((start, end, label))
    return turns


def turns_to_rttm(turns: list[tuple[float, float, str]], uri: str = "file") -> str:
    """Serialize ``(start, end, speaker)`` turns to RTTM ``SPEAKER`` lines."""
    lines = [
        f"SPEAKER {uri} 1 {start:.3f} {max(0.0, end - start):.3f} <NA> <NA> {spk} <NA> <NA>"
        for start, end, spk in turns
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _cmd_emit(args: argparse.Namespace) -> int:
    if not args.transcript.is_file():
        logger.error("Transcript not found: %s", args.transcript)
        return 2
    words = load_transcript_words(args.transcript)
    text = emit_seam_labels(words, pad=args.pad)
    n_seams = text.count("\n") if text.strip() else 0
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        logger.info("Wrote %d seam labels to %s", n_seams, args.out)
    else:
        print(text, end="")  # noqa: T201 — explicit stdout for piping
    return 0


def _cmd_to_rttm(args: argparse.Namespace) -> int:
    if not args.labels.is_file():
        logger.error("Label track not found: %s", args.labels)
        return 2
    turns = turns_from_seam_labels(args.labels)
    if not turns:
        logger.error("No corrected turns parsed from %s", args.labels)
        return 1
    rttm = turns_to_rttm(turns, uri=args.uri)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rttm, encoding="utf-8")
        logger.info("Wrote %d turns to %s", len(turns), args.out)
    else:
        print(rttm, end="")  # noqa: T201 — explicit stdout for piping
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point with ``emit`` and ``to-rttm`` subcommands."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p_emit = sub.add_parser("emit", help="Transcript JSON → Audacity seam label track")
    p_emit.add_argument("transcript", type=Path, help="Transcript JSON (segments with words)")
    p_emit.add_argument("--out", type=Path, help="Output label-track path (default: stdout)")
    p_emit.add_argument(
        "--pad", type=float, default=DEFAULT_PAD, help="Seam half-width in seconds (default: 2.0)"
    )
    p_emit.set_defaults(func=_cmd_emit)

    p_rttm = sub.add_parser("to-rttm", help="Corrected Audacity label track → reference RTTM")
    p_rttm.add_argument("labels", type=Path, help="Corrected label track (start\\tend\\tSPEAKER)")
    p_rttm.add_argument("--out", type=Path, help="Output RTTM path (default: stdout)")
    p_rttm.add_argument("--uri", default="file", help="RTTM uri/recording id (default: file)")
    p_rttm.set_defaults(func=_cmd_to_rttm)

    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
