#!/usr/bin/env python
"""Convert an Earnings-21 ``.nlp`` reference into ``reference.rttm`` + ``reference.words.json``.

Source dataset: ``github.com/revdotcom/speech-datasets``, files at
``earnings21/transcripts/nlp_references/<id>.nlp``.

Assumed ``.nlp`` layout (override-able)
---------------------------------------
The ``.nlp`` format is a header line followed by one row per token. Columns are
delimited (pipe ``|`` by default; pass ``--delimiter ,`` for comma) and carry the token
text, a speaker id, and timing / tag fields. The canonical Earnings-21 header is::

    token|speaker|ts|endTs|punctuation|case|tags|wer_tags

so the defaults below are::

    --token-col 0  --speaker-col 1  --start-col 2  --end-col 3

These are *configurable* because the public dataset has shifted column order between
releases and this repo cannot clone it to introspect. Inspect the header of your own
``.nlp`` file (printed at ``--log-level DEBUG``) and override the indices if they differ.

Timing fields may be empty strings (Rev marks some tokens without alignment). Such tokens
are emitted with ``start=end=None``; in ``--no-timing`` mode *all* timing is dropped and
every token carries ``start=end=None`` for WSER-by-position scoring.

Outputs
-------
* ``reference.rttm`` — consecutive same-speaker tokens collapsed into ``SPEAKER`` lines
  via :func:`app.utils.diarization_metrics.words_to_rttm` (skipped when no timing exists).
* ``reference.words.json`` — ``[{"start": float|None, "end": float|None, "word": str,
  "speaker": str}, ...]`` in token order.

Run::

    PYTHONPATH=. python -m scripts.nlp_to_rttm \
        path/to/4341191.nlp --out-dir /tmp/ref --uri 4341191
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from app.utils.diarization_metrics import words_to_rttm

logger = logging.getLogger("nlp_to_rttm")

# Default Earnings-21 .nlp column layout: token|speaker|ts|endTs|punctuation|case|tags|...
DEFAULT_TOKEN_COL = 0
DEFAULT_SPEAKER_COL = 1
DEFAULT_START_COL = 2
DEFAULT_END_COL = 3

# Header tokens that signal the first row is a column-name line, not a data row.
_HEADER_HINTS = frozenset({"token", "speaker", "ts", "endts", "punctuation", "case", "tags"})


def _looks_like_header(fields: list[str], token_col: int, start_col: int) -> bool:
    """Return True if a row is the column-name header rather than a data token.

    Detected when the token cell contains a known header keyword, or when the timing cell
    is plainly non-numeric text (e.g. the literal string ``ts``).
    """
    if not fields:
        return True
    tok = fields[token_col].strip().lower() if token_col < len(fields) else ""
    if tok in _HEADER_HINTS:
        return True
    if start_col < len(fields):
        cell = fields[start_col].strip().lower()
        if cell in _HEADER_HINTS:
            return True
    return False


def _parse_float(value: str | None) -> float | None:
    """Parse a timing cell to float, returning None for empty / unparseable cells."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_nlp(
    text: str,
    *,
    delimiter: str = "|",
    token_col: int = DEFAULT_TOKEN_COL,
    speaker_col: int = DEFAULT_SPEAKER_COL,
    start_col: int = DEFAULT_START_COL,
    end_col: int = DEFAULT_END_COL,
    no_timing: bool = False,
) -> list[dict[str, Any]]:
    """Parse ``.nlp`` text into a list of ``{start, end, word, speaker}`` token dicts.

    Args:
        text: Full contents of the ``.nlp`` file.
        delimiter: Column delimiter (``|`` or ``,``).
        token_col: Zero-based index of the token-text column.
        speaker_col: Zero-based index of the speaker-id column.
        start_col: Zero-based index of the start-time column.
        end_col: Zero-based index of the end-time column.
        no_timing: When True, drop all timing — every token gets ``start=end=None``.

    Returns:
        Tokens in file order. The header row and blank lines are skipped. Rows whose token
        cell is missing are dropped; rows missing only timing keep ``start=end=None``.
    """
    words: list[dict[str, Any]] = []
    header_skipped = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        fields = line.split(delimiter)
        if not header_skipped and _looks_like_header(fields, token_col, start_col):
            logger.debug("Skipping header row: %s", line)
            header_skipped = True
            continue
        header_skipped = True  # only the first non-blank row can be a header

        if token_col >= len(fields):
            logger.warning("Row has no token column, skipping: %r", line)
            continue
        token = fields[token_col].strip()
        if not token:
            continue
        speaker = (fields[speaker_col].strip() if speaker_col < len(fields) else "") or "SPEAKER_00"

        if no_timing:
            start: float | None = None
            end: float | None = None
        else:
            start = _parse_float(fields[start_col]) if start_col < len(fields) else None
            end = _parse_float(fields[end_col]) if end_col < len(fields) else None
            if end is None:
                end = start

        words.append({"start": start, "end": end, "word": token, "speaker": speaker})
    return words


def write_outputs(
    words: list[dict[str, Any]],
    out_dir: Path,
    *,
    uri: str = "file",
) -> tuple[Path, Path | None]:
    """Write ``reference.words.json`` and (when timed) ``reference.rttm``.

    Returns:
        ``(words_json_path, rttm_path_or_None)``. The RTTM is skipped when no token carries
        timing (``--no-timing`` mode or a fully un-aligned file).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    words_path = out_dir / "reference.words.json"
    words_path.write_text(json.dumps(words, indent=2), encoding="utf-8")
    logger.info("Wrote %d tokens to %s", len(words), words_path)

    has_timing = any(w.get("start") is not None for w in words)
    rttm_path: Path | None = None
    if has_timing:
        rttm_path = out_dir / "reference.rttm"
        rttm_path.write_text(words_to_rttm(words, uri=uri), encoding="utf-8")
        logger.info("Wrote RTTM to %s", rttm_path)
    else:
        logger.info("No timing present — RTTM emission skipped (WSER-by-position only)")
    return words_path, rttm_path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("nlp_path", type=Path, help="Path to the input <id>.nlp file")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("."),
        help="Directory for reference.rttm + reference.words.json (default: cwd)",
    )
    parser.add_argument("--uri", default="file", help="RTTM uri/recording id (default: file)")
    parser.add_argument(
        "--delimiter",
        default="|",
        help="Column delimiter; '|' (default) or ',' for comma-delimited .nlp",
    )
    parser.add_argument("--token-col", type=int, default=DEFAULT_TOKEN_COL)
    parser.add_argument("--speaker-col", type=int, default=DEFAULT_SPEAKER_COL)
    parser.add_argument("--start-col", type=int, default=DEFAULT_START_COL)
    parser.add_argument("--end-col", type=int, default=DEFAULT_END_COL)
    parser.add_argument(
        "--no-timing",
        action="store_true",
        help="Drop all timing; emit per-token speaker only (start/end = None)",
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG to see the header row")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    if not args.nlp_path.is_file():
        logger.error("Input file not found: %s", args.nlp_path)
        return 2

    text = args.nlp_path.read_text(encoding="utf-8")
    words = parse_nlp(
        text,
        delimiter=args.delimiter,
        token_col=args.token_col,
        speaker_col=args.speaker_col,
        start_col=args.start_col,
        end_col=args.end_col,
        no_timing=args.no_timing,
    )
    if not words:
        logger.error("Parsed zero tokens — check --delimiter and column indices")
        return 1

    write_outputs(words, args.out_dir, uri=args.uri)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
