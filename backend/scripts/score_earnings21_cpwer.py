#!/usr/bin/env python3
"""Earnings-21 cpWER scorer (issue #193) — speaker-attributed WER with no timing needed.

Earnings-21 (``github.com/revdotcom/speech-datasets``) is interview-style earnings calls,
the closest public analogue to the reporter's 2-speaker case. Its ``.nlp`` references carry
token + speaker but the ``ts``/``endTs`` columns are *empty*, so per-word timing-based
metrics (WSER, DER) are unavailable. The right metric here is **cpWER** (concatenated
minimum-permutation WER via ``meeteval``), which groups words by speaker and scores the
optimal speaker permutation — it needs no word timing at all.

For one ``.nlp`` reference + its ``media/<id>.mp3``:

    1. Transcribe + diarize via the engine:
       ``Engine(EngineConfig.from_environment(...)).process(JobSpec(...)).segments``
    2. Build the hypothesis per-word ``{speaker, word}`` list with ``flatten_words``.
    3. Build the reference per-word ``{speaker, word}`` from the ``.nlp`` (token + int
       speaker), reusing ``scripts.nlp_to_rttm.parse_nlp`` in ``--no-timing`` mode.
    4. Normalize both sides (lowercase, strip punctuation/diacritics) — Earnings-21 tokens
       carry mixed case and digits, so raw cpWER is dominated by surface mismatch, not
       attribution; normalization brings it in line with how cpWER is reported.
    5. Compute **cpWER** via ``app.utils.diarization_metrics.cpwer`` — with the boundary
       smoother OFF, then ON (``finalize_segments(segs, BoundarySmoothingConfig(enabled=True))``).

Prints, per file: ref_speakers / hyp_speakers, cpWER OFF, cpWER ON.

In-container only (same guard as ``scripts/diarization-der.py:28``) — the GPU/model stack
lives in the worker container. Run via::

    docker compose exec celery-worker \\
        python /app/scripts/score_earnings21_cpwer.py \\
            --nlp   /tmp/earnings21/4341191.nlp \\
            --media /tmp/earnings21/4341191.mp3 \\
            --models large-v3

Refs: issue #193, docs/DIARIZATION_BOUNDARY_FIX_PLAN.md, docs/diarization-boundary-results/.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

# Scripts run as ``python /app/scripts/...`` inside the container; make both the ``app``
# package and the sibling ``scripts`` package importable the same way benchmark_boundary.py
# does (it lives one level deeper, under backend/scripts/, so its parent.parent is /app).
sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_logger = logging.getLogger("score_earnings21_cpwer")


def log(msg: str) -> None:
    """Timestamped stdout line (matches the other benchmark scripts' style)."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# Container guard (same rule as scripts/diarization-der.py:28-32)
# ──────────────────────────────────────────────────────────────────────────────


def require_container() -> None:
    """Refuse to run outside the container — needs the GPU/model stack."""
    if Path("/.dockerenv").exists() or os.environ.get("OPENTRANSCRIBE_IN_CONTAINER") == "1":
        return
    sys.stderr.write(
        "Refusing to run outside container — needs the GPU/model stack. "
        "Run via: docker compose exec celery-worker "
        "python /app/scripts/score_earnings21_cpwer.py ...\n"
    )
    sys.exit(2)


# ──────────────────────────────────────────────────────────────────────────────
# Text normalization
# ──────────────────────────────────────────────────────────────────────────────

# Strip everything except letters, digits, and apostrophes (kept so "culp's" stays one
# token). cpWER is raw text WER under the optimal speaker permutation, so without this the
# rate is dominated by surface differences — Earnings-21 tokens carry mixed case and digits
# ("Good", "Culp's", "2020"), while WhisperX lowercases/renders differently — not by
# attribution errors. This light normalizer (lowercase, drop punctuation, collapse
# whitespace) brings the metric in line with how cpWER is reported in the literature.
_PUNCT_RE = re.compile(r"[^\w']", flags=re.UNICODE)
_DROP_RE = re.compile(r"[^a-z0-9']")


def normalize_token(token: str) -> str:
    """Lowercase + strip punctuation/diacritics from one token (may return "").

    Returns the empty string for punctuation-only tokens; callers drop those so they do
    not become spurious word-count mass in cpWER.
    """
    token = unicodedata.normalize("NFKD", token).lower()
    token = "".join(c for c in token if not unicodedata.combining(c))
    token = _PUNCT_RE.sub("", token)
    token = _DROP_RE.sub("", token)
    return token.strip("'")


def normalize_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply :func:`normalize_token` to every word, dropping ones that normalize to ""."""
    out: list[dict[str, Any]] = []
    for w in words:
        norm = normalize_token(str(w.get("word", "")))
        if norm:
            out.append({**w, "word": norm})
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Reference builder (reuse scripts.nlp_to_rttm.parse_nlp, --no-timing)
# ──────────────────────────────────────────────────────────────────────────────


def build_reference_words(nlp_path: Path) -> list[dict[str, Any]]:
    """Parse an Earnings-21 ``.nlp`` into per-word ``{word, speaker}`` (timing dropped).

    Reuses :func:`scripts.nlp_to_rttm.parse_nlp` in ``no_timing`` mode so the column
    layout (``token|speaker|ts|endTs|...``) and header handling stay in one place. The
    integer speaker id from the ``.nlp`` is kept as a string label — cpWER only cares
    about per-speaker text grouping, not the label values. Tokens are NOT normalized here;
    callers apply :func:`normalize_words` to both ref and hyp before scoring.

    Returns:
        Tokens in file order, each ``{"word": str, "speaker": str, "start": None,
        "end": None}``. ``start``/``end`` are present (None) for parity with
        ``flatten_words`` output, but cpWER ignores them.
    """
    from scripts.nlp_to_rttm import parse_nlp

    text = nlp_path.read_text(encoding="utf-8")
    return parse_nlp(text, no_timing=True)


# ──────────────────────────────────────────────────────────────────────────────
# Hypothesis builder (engine transcribe + diarize)
# ──────────────────────────────────────────────────────────────────────────────


def transcribe_segments(
    media_path: str,
    model: str,
    source_language: str,
    min_speakers: int,
    max_speakers: int,
) -> list[dict[str, Any]]:
    """Run the full transcribe+diarize pipeline and return word-labeled segments.

    Uses the combined engine entrypoint (``Engine.process``) so the result is identical
    to the production transcription path. Each returned segment carries a ``words`` list
    with a per-word ``speaker``.
    """
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.engine import Engine
    from app.transcription.engine.job import JobSpec

    cfg = EngineConfig.from_environment(
        model_name=model,
        source_language=source_language,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
    )
    engine = Engine(cfg)
    spec = JobSpec(audio_path=media_path, task_id=f"e21-{Path(media_path).stem}-{model}")
    return engine.process(spec).segments


def _smoothed_hyp_words(
    segments: list[dict[str, Any]],
    smoothing_enabled: bool,
) -> list[dict[str, Any]]:
    """Run ``finalize_segments`` (smoother OFF/ON) then flatten to per-word dicts."""
    from app.transcription.boundary_resolver import BoundarySmoothingConfig
    from app.utils.diarization_metrics import flatten_words
    from app.utils.segment_postprocess import finalize_segments

    cfg = BoundarySmoothingConfig(enabled=smoothing_enabled)
    smoothed = finalize_segments(deepcopy(segments), cfg)
    return flatten_words(smoothed)


# ──────────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────────


def _distinct_speakers(words: list[dict[str, Any]]) -> int:
    """Count distinct (non-excluded) speaker labels in a per-word list."""
    from app.utils.diarization_metrics import EXCLUDE

    return len({w.get("speaker") for w in words if w.get("speaker") not in EXCLUDE})


def score_file(
    nlp_path: Path,
    media_path: Path,
    model: str,
    source_language: str,
    min_speakers: int,
    max_speakers: int,
) -> dict[str, Any]:
    """Transcribe + score one (``.nlp``, ``.mp3``) pair, smoother OFF then ON.

    Returns a result dict with ref/hyp speaker counts and cpWER OFF/ON. cpWER is computed
    via the shared :func:`app.utils.diarization_metrics.cpwer` (meeteval).
    """
    from app.utils.diarization_metrics import cpwer

    file_id = nlp_path.stem
    ref_raw = build_reference_words(nlp_path)
    ref_words = normalize_words(ref_raw)
    log(f"[{file_id}] reference: {len(ref_words)} tokens, {_distinct_speakers(ref_words)} speakers")

    log(f"[{file_id}] transcribing+diarizing ({model})…")
    segments = transcribe_segments(
        str(media_path), model, source_language, min_speakers, max_speakers
    )

    hyp_off = normalize_words(_smoothed_hyp_words(segments, smoothing_enabled=False))
    hyp_on = normalize_words(_smoothed_hyp_words(segments, smoothing_enabled=True))

    # Normalized cpWER (lowercase, no punctuation) — comparable to the literature range.
    cpwer_off = cpwer(ref_words, hyp_off)
    cpwer_on = cpwer(ref_words, hyp_on)

    result = {
        "file_id": file_id,
        "model": model,
        "ref_speakers": _distinct_speakers(ref_words),
        "hyp_speakers_off": _distinct_speakers(hyp_off),
        "hyp_speakers_on": _distinct_speakers(hyp_on),
        "ref_tokens": len(ref_words),
        "hyp_tokens_off": len(hyp_off),
        "hyp_tokens_on": len(hyp_on),
        "cpwer_off": cpwer_off,
        "cpwer_on": cpwer_on,
    }
    log(
        f"[{file_id}] ref_spk={result['ref_speakers']} "
        f"hyp_spk(off/on)={result['hyp_speakers_off']}/{result['hyp_speakers_on']}  "
        f"cpWER OFF={cpwer_off:.4f}  cpWER ON={cpwer_on:.4f}  "
        f"Δ(OFF-ON)={cpwer_off - cpwer_on:+.4f}"
    )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--nlp", required=True, type=Path, help="Path to the Earnings-21 <id>.nlp")
    p.add_argument("--media", required=True, type=Path, help="Path to the matching media/<id>.mp3")
    p.add_argument(
        "--models",
        default="large-v3",
        help="comma-separated Whisper model name(s); scored sequentially",
    )
    p.add_argument("--source-language", default="en", help="source language hint (default: en)")
    p.add_argument("--min-speakers", type=int, default=1)
    p.add_argument("--max-speakers", type=int, default=20)
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="optional JSON path to append the per-file result(s)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    require_container()
    args = parse_args(argv)

    if not args.nlp.is_file():
        _logger.error("nlp not found: %s", args.nlp)
        return 2
    if not args.media.is_file():
        _logger.error("media not found: %s", args.media)
        return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    results: list[dict[str, Any]] = []
    for model in models:
        try:
            results.append(
                score_file(
                    args.nlp,
                    args.media,
                    model,
                    args.source_language,
                    args.min_speakers,
                    args.max_speakers,
                )
            )
        except ImportError as exc:
            _logger.error("cpWER needs meeteval (pip install meeteval): %s", exc)
            return 1

    if args.out is not None:
        existing: list[dict[str, Any]] = []
        if args.out.exists():
            existing = json.loads(args.out.read_text())
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(existing + results, indent=2))
        log(f"appended {len(results)} result(s) to {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
