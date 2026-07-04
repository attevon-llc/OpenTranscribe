"""Diarization-boundary accuracy metrics (issue #193).

Headline metric is WSER (Word Speaker Error Rate): because Whisper words+timings are
held fixed across reference and hypothesis, every word has an identity, so the metric
is exact, alignment-free, and has no collar — a 1-3 word boundary bleed is counted as
exactly 1-3 word errors. cpWER (meeteval) and DER (pyannote.metrics) are cross-checks.

All heavy/optional dependencies (pyannote.metrics, meeteval) are lazily imported inside
the functions that need them, so this module stays importable in non-GPU contexts and in
fast unit tests. numpy + scipy are the only module-level deps.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

logger = logging.getLogger(__name__)

# Reference speaker labels treated as un-scorable: genuinely-ambiguous overlap, unknown,
# or words flagged as Whisper-timing defects by the seam audit. Excluded from WSER.
EXCLUDE: frozenset = frozenset({"OVERLAP", "?", "", None})


# ──────────────────────────────────────────────────────────────────────────────
# Word helpers
# ──────────────────────────────────────────────────────────────────────────────


def flatten_words(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten segment word lists into an ordered list of word dicts with a speaker.

    Returns words in transcript order. Each word carries at least ``speaker``; ``start``
    and ``end`` are preserved when present (defaulting end→start).
    """
    out: list[dict[str, Any]] = []
    for seg in segments:
        for w in seg.get("words", []) or []:
            if "speaker" not in w:
                continue
            start = w.get("start")
            out.append(
                {
                    "word": w.get("word", ""),
                    "start": start,
                    "end": w.get("end", start),
                    "speaker": w.get("speaker"),
                }
            )
    return out


def assign_words_from_turns(
    words: list[dict[str, Any]],
    turns: list[tuple[float, float, str]],
) -> list[dict[str, Any]]:
    """Assign each word a reference speaker by midpoint lookup into time-based turns.

    ``turns`` is a list of ``(start, end, speaker)`` (e.g. parsed from a reference RTTM).
    A word whose midpoint falls in no turn (silence / non-speech) gets speaker ``None``
    (→ excluded from WSER). This decouples the reference from any model's tokenization so
    every model in the size sweep is scored against the same authoritative turns.
    """
    if not turns:
        return [{**w, "speaker": None} for w in words]
    t_starts = np.array([t[0] for t in turns], dtype=np.float64)
    t_ends = np.array([t[1] for t in turns], dtype=np.float64)
    t_spk = [t[2] for t in turns]
    out: list[dict[str, Any]] = []
    for w in words:
        start = w.get("start")
        end = w.get("end", start)
        if start is None:
            out.append({**w, "speaker": None})
            continue
        mid = (float(start) + float(end if end is not None else start)) / 2.0
        hits = np.nonzero((t_starts <= mid) & (mid < t_ends))[0]
        out.append({**w, "speaker": t_spk[int(hits[0])] if len(hits) else None})
    return out


def words_to_rttm(words: list[dict[str, Any]], uri: str = "file") -> str:
    """Collapse consecutive same-speaker words into RTTM ``SPEAKER`` lines."""
    lines: list[str] = []
    run_start: float | None = None
    run_end: float | None = None
    run_spk: Any = None
    for w in words:
        spk = w.get("speaker")
        raw_start = w.get("start")
        if spk is None or raw_start is None:
            continue
        start = float(raw_start)
        raw_end = w.get("end")
        end = float(raw_end) if raw_end is not None else start
        if spk == run_spk and run_end is not None and start - run_end <= 0.5:
            run_end = max(run_end, end)
        else:
            if run_spk is not None and run_start is not None and run_end is not None:
                lines.append(_rttm_line(uri, run_start, run_end, run_spk))
            run_spk, run_start, run_end = spk, start, end
    if run_spk is not None and run_start is not None and run_end is not None:
        lines.append(_rttm_line(uri, run_start, run_end, run_spk))
    return "\n".join(lines) + ("\n" if lines else "")


def _rttm_line(uri: str, start: float, end: float, speaker: Any) -> str:
    dur = max(0.0, end - start)
    return f"SPEAKER {uri} 1 {start:.3f} {dur:.3f} <NA> <NA> {speaker} <NA> <NA>"


def read_rttm(path: str) -> list[tuple[float, float, str]]:
    """Parse an RTTM file into a list of ``(start, end, speaker)`` turns."""
    turns: list[tuple[float, float, str]] = []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if not parts or parts[0] != "SPEAKER":
                continue
            start, dur = float(parts[3]), float(parts[4])
            turns.append((start, start + dur, parts[7]))
    return turns


# ──────────────────────────────────────────────────────────────────────────────
# WSER (headline)
# ──────────────────────────────────────────────────────────────────────────────


def wser(ref_words: list[dict[str, Any]], hyp_words: list[dict[str, Any]]) -> dict[str, Any]:
    """Word Speaker Error Rate over a shared, parallel word inventory.

    ``ref_words`` and ``hyp_words`` must be the SAME words in the SAME order (identical
    inventory — words/timings frozen); only the per-word ``speaker`` differs. Returns a
    dict with ``wser`` (count-weighted, the gate), ``t_wser`` (duration-weighted),
    ``n_scored``, ``n_excluded``, ``perm`` (hyp→ref label map), and the raw error/word
    counts used for pooled bootstrap aggregation.
    """
    if len(ref_words) != len(hyp_words):
        raise ValueError(
            f"WSER needs a parallel inventory: {len(ref_words)} ref vs {len(hyp_words)} hyp words"
        )

    ref_labels = sorted({w["speaker"] for w in ref_words if w["speaker"] not in EXCLUDE})
    hyp_labels = sorted({w["speaker"] for w in hyp_words if w["speaker"] not in EXCLUDE})
    ri = {lab: i for i, lab in enumerate(ref_labels)}
    hi = {lab: i for i, lab in enumerate(hyp_labels)}

    n_excluded = sum(1 for w in ref_words if w["speaker"] in EXCLUDE)
    n_scored = len(ref_words) - n_excluded
    if not ref_labels or n_scored == 0:
        return {
            "wser": 0.0,
            "t_wser": 0.0,
            "n_scored": n_scored,
            "n_excluded": n_excluded,
            "n_word_errors": 0,
            "perm": {},
            "ref_labels": ref_labels,
            "hyp_labels": hyp_labels,
        }

    counts = np.zeros((len(ref_labels), len(hyp_labels)), dtype=np.float64)
    durs = np.zeros_like(counts)
    t_total = 0.0
    for rw, hw in zip(ref_words, hyp_words, strict=True):
        if rw["speaker"] in EXCLUDE:
            continue
        dur = _word_dur(rw)
        t_total += dur
        j = hi.get(hw["speaker"])
        if j is None:  # hyp word excluded/unknown → wrong, contributes no correct mass
            continue
        counts[ri[rw["speaker"]], j] += 1
        durs[ri[rw["speaker"]], j] += dur

    rows, cols = linear_sum_assignment(-counts)  # maximize matched diagonal
    correct = float(counts[rows, cols].sum())
    t_correct = float(durs[rows, cols].sum())
    perm = {hyp_labels[c]: ref_labels[r] for r, c in zip(rows, cols, strict=True)}

    return {
        "wser": 1.0 - correct / n_scored,
        "t_wser": 1.0 - (t_correct / t_total if t_total else 1.0),
        "n_scored": n_scored,
        "n_excluded": n_excluded,
        "n_word_errors": int(round(n_scored - correct)),
        "perm": perm,
        "ref_labels": ref_labels,
        "hyp_labels": hyp_labels,
    }


def _word_dur(w: dict[str, Any]) -> float:
    start, end = w.get("start"), w.get("end")
    if start is None or end is None:
        return 0.0
    return max(0.0, float(end) - float(start))


def map_hyp_to_ref(hyp_speakers: list[Any], perm: dict[Any, Any]) -> list[Any]:
    """Apply a WSER hyp→ref permutation to a per-word hyp speaker sequence."""
    return [perm.get(s, s) for s in hyp_speakers]


# ──────────────────────────────────────────────────────────────────────────────
# Bleed-island count (direct bug signature)
# ──────────────────────────────────────────────────────────────────────────────


def count_bleed_islands(
    ref_seq: list[Any],
    hyp_seq: list[Any],
    max_island: int = 3,
) -> list[tuple[int, int, int]]:
    """Count spurious wrong-speaker islands in a per-word hyp sequence.

    A bleed island = a maximal hyp run of speaker B with length ≤ ``max_island`` flanked
    on BOTH sides by speaker A, where the reference says the run AND both flank words
    belong to A. ``hyp_seq`` must already be mapped to ref labels (see :func:`map_hyp_to_ref`).
    Returns ``[(start_idx, end_idx, length), ...]`` (end exclusive).
    """
    n = len(hyp_seq)
    out: list[tuple[int, int, int]] = []
    i = 0
    while i < n:
        j = i
        while j < n and hyp_seq[j] == hyp_seq[i]:
            j += 1
        if 0 < i < j < n and (j - i) <= max_island:
            left, right, isl = hyp_seq[i - 1], hyp_seq[j], hyp_seq[i]
            if (
                left == right
                and left != isl
                and ref_seq[i - 1] == left
                and ref_seq[j] == left
                and all(ref_seq[k] == left for k in range(i, j))
            ):
                out.append((i, j, j - i))
        i = j
    return out


def island_histogram(islands: list[tuple[int, int, int]]) -> dict[str, int]:
    """Bucket islands by length into {'1','2','3','4+'}."""
    hist = {"1": 0, "2": 0, "3": 0, "4+": 0}
    for _, _, length in islands:
        hist["4+" if length >= 4 else str(length)] += 1
    return hist


def categorize_errors(
    ref_words: list[dict[str, Any]],
    hyp_words: list[dict[str, Any]],
    perm: dict[Any, Any],
    boundary_window: int = 2,
) -> dict[str, Any]:
    """Split per-word speaker errors into the two failure modes (issue #193).

    Given a parallel ref/hyp inventory and the WSER hyp→ref permutation:
      - **boundary** errors: a mislabeled word within ``boundary_window`` words of a true
        (reference) speaker change → turn-boundary bleed (the island smoother's target).
      - **interior** errors: a mislabeled word NOT near any reference boundary → a short
        utterance absorbed into a long same-speaker stretch, i.e. **backchannel
        absorption** (the host's "mm-hmm"/laugh given the wrong speaker). The smoother
        cannot fix these; they need the acoustic re-check.

    Returns counts plus the interior errors' text/time so the absorption cases can be
    inspected. ``EXCLUDE`` ref words are skipped.
    """
    hyp_seq = [perm.get(w["speaker"], w["speaker"]) for w in hyp_words]
    ref_seq = [w["speaker"] for w in ref_words]
    n = len(ref_seq)
    ref_bound = {i for i in range(1, n) if ref_seq[i] != ref_seq[i - 1]}

    def _near_boundary(i: int) -> bool:
        return any(
            (i + d) in ref_bound or (i - d + 1) in ref_bound for d in range(boundary_window + 1)
        )

    boundary = 0
    interior: list[dict[str, Any]] = []
    for i in range(n):
        if ref_seq[i] in EXCLUDE or hyp_seq[i] == ref_seq[i]:
            continue
        if _near_boundary(i):
            boundary += 1
        else:
            interior.append(
                {
                    "start": hyp_words[i].get("start"),
                    "word": hyp_words[i].get("word", ""),
                    "hyp": hyp_seq[i],
                    "ref": ref_seq[i],
                }
            )
    return {
        "boundary_errors": boundary,
        "interior_errors": len(interior),
        "interior_examples": interior[:50],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Boundary precision / recall / F1 (diagnostic)
# ──────────────────────────────────────────────────────────────────────────────


def boundary_prf(ref_seq: list[Any], hyp_seq: list[Any]) -> dict[str, float]:
    """Speaker-change-point precision/recall/F1 over the shared word index.

    A "boundary" is an index i where speaker[i] != speaker[i-1]. hyp_seq should be
    perm-mapped to ref labels. Diagnoses misplaced vs mislabeled boundaries.
    """
    ref_b = {i for i in range(1, len(ref_seq)) if ref_seq[i] != ref_seq[i - 1]}
    hyp_b = {i for i in range(1, len(hyp_seq)) if hyp_seq[i] != hyp_seq[i - 1]}
    tp = len(ref_b & hyp_b)
    prec = tp / len(hyp_b) if hyp_b else 1.0
    rec = tp / len(ref_b) if ref_b else 1.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"precision": prec, "recall": rec, "f1": f1, "ref_boundaries": len(ref_b)}


# ──────────────────────────────────────────────────────────────────────────────
# DER (cross-check) — pyannote.metrics, lazily imported
# ──────────────────────────────────────────────────────────────────────────────


def der(
    ref_turns: list[tuple[float, float, str]],
    hyp_turns: list[tuple[float, float, str]],
    collar: float = 0.25,
    skip_overlap: bool = False,
    uri: str = "file",
) -> dict[str, float]:
    """Diarization Error Rate + components via pyannote.metrics.

    ``ref_turns``/``hyp_turns`` are ``(start, end, speaker)`` lists. Reports total DER plus
    missed-detection, false-alarm, and speaker-confusion components.
    """
    from pyannote.core import Annotation
    from pyannote.core import Segment
    from pyannote.metrics.diarization import DiarizationErrorRate

    def _ann(turns: list[tuple[float, float, str]]) -> Annotation:
        ann = Annotation(uri=uri)
        for start, end, spk in turns:
            if end > start:
                ann[Segment(start, end)] = spk
        return ann

    ref, hyp = _ann(ref_turns), _ann(hyp_turns)
    metric = DiarizationErrorRate(collar=collar, skip_overlap=skip_overlap)
    total = float(metric(ref, hyp))
    comp = metric(ref, hyp, detailed=True)
    denom = comp.get("total", 0.0) or 1.0
    return {
        "der": total,
        "missed": comp.get("missed detection", 0.0) / denom,
        "false_alarm": comp.get("false alarm", 0.0) / denom,
        "confusion": comp.get("confusion", 0.0) / denom,
        "ref_speakers": len({t[2] for t in ref_turns}),
        "hyp_speakers": len({t[2] for t in hyp_turns}),
    }


# ──────────────────────────────────────────────────────────────────────────────
# cpWER (cross-check) — meeteval, lazily imported
# ──────────────────────────────────────────────────────────────────────────────


def cpwer(
    ref_words: list[dict[str, Any]],
    hyp_words: list[dict[str, Any]],
) -> float:
    """Concatenated minimum-permutation WER via meeteval.

    Groups words by speaker, concatenates per-speaker text, and computes the optimal
    speaker-permutation WER. Because text is held fixed here, this isolates attribution
    churn. Raises ImportError with a clear message if meeteval is not installed.
    """
    try:
        from meeteval.wer import cp_word_error_rate
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise ImportError("meeteval is required for cpWER (pip install meeteval)") from exc

    def _by_speaker(words: list[dict[str, Any]]) -> dict[str, str]:
        acc: dict[str, list[str]] = {}
        for w in words:
            spk = w.get("speaker")
            if spk in EXCLUDE:
                continue
            acc.setdefault(str(spk), []).append(str(w.get("word", "")).strip())
        return {spk: " ".join(t).strip() for spk, t in acc.items()}

    res = cp_word_error_rate(_by_speaker(ref_words), _by_speaker(hyp_words))
    return float(res.error_rate)


# ──────────────────────────────────────────────────────────────────────────────
# Speaker-count + paired bootstrap
# ──────────────────────────────────────────────────────────────────────────────


def speaker_count_match(ref_words: list[dict[str, Any]], hyp_words: list[dict[str, Any]]) -> dict:
    """Compare distinct speaker counts (separates count errors from boundary errors)."""
    ref_n = len({w["speaker"] for w in ref_words if w["speaker"] not in EXCLUDE})
    hyp_n = len({w["speaker"] for w in hyp_words if w["speaker"] not in EXCLUDE})
    return {"ref_speakers": ref_n, "hyp_speakers": hyp_n, "match": ref_n == hyp_n}


def paired_bootstrap_wser(
    per_file: list[tuple[int, int, int]],
    n_boot: int = 2000,
    seed: int = 12345,
) -> dict[str, float]:
    """Paired bootstrap CI on the pooled (OFF − ON) WSER improvement.

    ``per_file`` is a list of ``(off_errors, on_errors, n_scored)`` per file. Resamples
    files with replacement, recomputes POOLED WSER (sum errors / sum words — pooled at the
    word level, not mean-of-rates), and returns the mean improvement and 95% CI. The
    improvement is "real" only if ``ci_low > 0``.
    """
    if not per_file:
        return {"mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "significant": False}
    rng = np.random.default_rng(seed)
    off = np.array([f[0] for f in per_file], dtype=np.float64)
    on = np.array([f[1] for f in per_file], dtype=np.float64)
    tot = np.array([f[2] for f in per_file], dtype=np.float64)
    n = len(per_file)
    deltas = np.empty(n_boot, dtype=np.float64)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        denom = tot[idx].sum() or 1.0
        deltas[b] = (off[idx].sum() - on[idx].sum()) / denom
    lo, hi = np.percentile(deltas, [2.5, 97.5])
    return {
        "mean": float(deltas.mean()),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "significant": bool(lo > 0),
    }
