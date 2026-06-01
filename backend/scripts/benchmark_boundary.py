#!/usr/bin/env python3
"""Diarization-boundary smoothing benchmark harness (issue #193).

Runs IN the backend container (GPU stage is needed only once per file/model — the
expensive ``run_gpu_stage`` output is cached to ``*.rawinfer.json`` so re-runs are
CPU-only). For each (file, model) it produces speaker-assigned segments via
``Engine.run_cpu_finalize``, then evaluates boundary smoothing OFF vs ON with
:func:`app.utils.segment_postprocess.finalize_segments`.

The headline metric is WSER (word-speaker error rate, alignment-free — words/timings
are frozen so every word has an identity). cpWER (meeteval) and DER (pyannote.metrics)
are cross-checks. A paired bootstrap over the per-file pooled (OFF - ON) improvement
tells us whether smoothing is a real win.

Flow per (file, model):

    cache = <cache_dir>/<fileid>__<model>.rawinfer.json
    if not cache: raw = Engine.run_preprocess -> run_gpu_stage; write raw.serialize()
    assigned = Engine(cfg).run_cpu_finalize(RawInferenceResult.deserialize(cache)).segments
    off = finalize_segments(deepcopy(assigned), BoundarySmoothingConfig(enabled=False))
    on  = finalize_segments(deepcopy(assigned), BoundarySmoothingConfig(enabled=True, ...))

Reference resolution (``--reference auto``):

    1. <ref_dir>/<fileid>__<model>.words.json  -> positional ref words (exact, no mapping)
    2. <ref_dir>/<fileid>.ref.rttm             -> midpoint-mapped via assign_words_from_turns

Usage (in-container)::

    docker compose exec backend python /app/backend/scripts/benchmark_boundary.py \\
        --corpus /app/docs/benchmark-corpus/corpus.json \\
        --models large-v3 --sample 5 --smoothing on --out /app/docs/boundary-benchmark

Refs: issue #193, docs/DIARIZATION_BOUNDARY_FIX_PLAN.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

# Scripts run as ``python /app/backend/scripts/...`` inside the container; make the
# ``app`` package importable the same way the other backend scripts do.
sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_logger = logging.getLogger("benchmark_boundary")


def log(msg: str) -> None:
    """Timestamped stdout line (matches the other benchmark scripts' style)."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# Container guard (same rule as scripts/diarization-der.py:28-32)
# ──────────────────────────────────────────────────────────────────────────────


def require_container() -> None:
    """Refuse to run outside the container (GPU + model stack live there only)."""
    if Path("/.dockerenv").exists() or os.environ.get("OPENTRANSCRIBE_IN_CONTAINER") == "1":
        return
    sys.stderr.write(
        "Refusing to run outside container — needs the GPU/model stack. "
        "Run via: docker compose exec backend python /app/backend/scripts/benchmark_boundary.py\n"
    )
    sys.exit(2)


# ──────────────────────────────────────────────────────────────────────────────
# Smoothing config — ON / OFF presets
# ──────────────────────────────────────────────────────────────────────────────

# ON preset matches the documented production defaults for the smoother.
SMOOTHING_ON_KWARGS: dict[str, Any] = {
    "enabled": True,
    "max_island_words": 3,
    "max_island_duration": 1.5,
    "min_flank_words": 3,
    "min_silent_gap": 0.4,
}


def _make_configs() -> tuple[Any, Any]:
    """Build (off_cfg, on_cfg) BoundarySmoothingConfig instances.

    Imported lazily so the harness still parses/--help even before the smoother
    module is committed by the lead.
    """
    from app.transcription.boundary_resolver import BoundarySmoothingConfig

    off_cfg = BoundarySmoothingConfig(enabled=False)
    on_cfg = BoundarySmoothingConfig(**SMOOTHING_ON_KWARGS)
    return off_cfg, on_cfg


def _smoothing_variants(mode: str) -> list[tuple[str, Any]]:
    """Return labelled (variant, cfg) pairs for the requested --smoothing mode."""
    from app.transcription.boundary_resolver import BoundarySmoothingConfig

    off_cfg, on_cfg = _make_configs()
    if mode == "off":
        return [("off", off_cfg)]
    if mode == "on":
        return [("off", off_cfg), ("on", on_cfg)]
    if mode == "sweep":
        # Sweep max_island_words while keeping the rest at the ON preset.
        variants: list[tuple[str, Any]] = [("off", off_cfg)]
        for k in (1, 2, 3, 4):
            kwargs = dict(SMOOTHING_ON_KWARGS)
            kwargs["max_island_words"] = k
            variants.append((f"on_island{k}", BoundarySmoothingConfig(**kwargs)))
        return variants
    raise ValueError(f"unknown --smoothing mode: {mode!r}")


# ──────────────────────────────────────────────────────────────────────────────
# Corpus + cache + reference IO
# ──────────────────────────────────────────────────────────────────────────────


def load_corpus(corpus_path: Path) -> list[dict[str, Any]]:
    """Load the benchmark corpus and normalize each entry to a file record.

    Accepts both the soak-corpus shape (``{"files": [{"uuid", "filename", ...}]}``)
    and a flat list. Each returned record has ``file_id``, ``audio_path``,
    ``duration_s`` and ``tier`` keys.
    """
    data = json.loads(corpus_path.read_text())
    raw_files = data.get("files", data) if isinstance(data, dict) else data
    audio_root = Path(os.environ.get("BENCHMARK_AUDIO_DIR", "/app/benchmark/test_audio"))
    records: list[dict[str, Any]] = []
    for i, f in enumerate(raw_files):
        filename = f.get("filename") or f.get("audio_path") or f.get("path") or ""
        audio_path = f.get("audio_path") or str(audio_root / filename)
        records.append(
            {
                "file_id": str(f.get("uuid") or f.get("file_id") or Path(filename).stem or i),
                "audio_path": audio_path,
                "filename": filename,
                "duration_s": float(f.get("duration_s", 0.0) or 0.0),
                "tier": f.get("tier", 0),
            }
        )
    return records


def _cache_path(cache_dir: Path, file_id: str, model: str) -> Path:
    safe_model = model.replace("/", "_")
    return cache_dir / f"{file_id}__{safe_model}.rawinfer.json"


def build_or_load_rawinfer(
    file_id: str,
    audio_path: str,
    model: str,
    source_language: str,
    min_speakers: int,
    max_speakers: int,
    cache_dir: Path,
) -> dict[str, Any]:
    """Return the serialized RawInferenceResult dict, building+caching it on a miss.

    Cache hits make the whole benchmark CPU-only (the frozen GPU output is replayed
    through ``run_cpu_finalize``).
    """
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.engine import Engine
    from app.transcription.engine.job import JobSpec

    cache = _cache_path(cache_dir, file_id, model)
    if cache.exists():
        log(f"  cache HIT  {cache.name}")
        cached: dict[str, Any] = json.loads(cache.read_text())
        return cached

    log(f"  cache MISS {cache.name} — running GPU stage for {file_id} ({model})")
    cfg = EngineConfig.from_environment(
        model_name=model,
        source_language=source_language,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
    )
    engine = Engine(cfg)
    spec = JobSpec(audio_path=audio_path, task_id=f"bench-{file_id}-{model}")
    pre = engine.run_preprocess(spec)
    raw = engine.run_gpu_stage(pre)
    payload = raw.serialize()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload))
    log(f"  cached     {cache.name}")
    return payload


def finalize_from_cache(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Replay a cached RawInferenceResult through CPU finalize -> assigned segments."""
    from app.transcription.engine.config import EngineConfig
    from app.transcription.engine.engine import Engine
    from app.transcription.engine.job import RawInferenceResult

    raw = RawInferenceResult.deserialize(payload)
    cfg = EngineConfig.from_snapshot(raw.config_snapshot)
    return Engine(cfg).run_cpu_finalize(raw).segments


def _resolve_reference(ref_dir: Path, file_id: str, model: str) -> dict[str, Any]:
    """Locate the reference for a file. Returns {kind, path} or {kind: 'none'}."""
    safe_model = model.replace("/", "_")
    words_path = ref_dir / f"{file_id}__{safe_model}.words.json"
    if words_path.exists():
        return {"kind": "words", "path": words_path}
    rttm_path = ref_dir / f"{file_id}.ref.rttm"
    if rttm_path.exists():
        return {"kind": "rttm", "path": rttm_path}
    # Tolerate a model-suffixed RTTM as well.
    rttm_model = ref_dir / f"{file_id}__{safe_model}.ref.rttm"
    if rttm_model.exists():
        return {"kind": "rttm", "path": rttm_model}
    return {"kind": "none", "path": None}


# ──────────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────────


def _ref_words_for(
    variant_words: list[dict[str, Any]],
    reference: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Build the per-word reference parallel to ``variant_words``.

    Returns (ref_words, error). On length mismatch in the words.json path returns
    (None, "<reason>") so the caller can log+skip rather than raise.
    """
    from app.utils.diarization_metrics import assign_words_from_turns
    from app.utils.diarization_metrics import read_rttm

    if reference["kind"] == "words":
        ref_words = json.loads(Path(reference["path"]).read_text())
        if isinstance(ref_words, dict):
            ref_words = ref_words.get("words", [])
        if len(ref_words) != len(variant_words):
            return None, (
                f"ref words.json length {len(ref_words)} != hyp {len(variant_words)} "
                f"({reference['path']})"
            )
        return ref_words, None

    if reference["kind"] == "rttm":
        turns = read_rttm(str(reference["path"]))
        return assign_words_from_turns(variant_words, turns), None

    return None, "no reference available"


def score_variant(
    segments: list[dict[str, Any]],
    reference: dict[str, Any],
    duration_s: float,
) -> dict[str, Any] | None:
    """Compute the full metric block for one smoothing variant.

    Returns None on a fatal reference mismatch (caller logs + skips the file).
    """
    from app.utils.diarization_metrics import boundary_prf
    from app.utils.diarization_metrics import count_bleed_islands
    from app.utils.diarization_metrics import cpwer
    from app.utils.diarization_metrics import der
    from app.utils.diarization_metrics import flatten_words
    from app.utils.diarization_metrics import island_histogram
    from app.utils.diarization_metrics import map_hyp_to_ref
    from app.utils.diarization_metrics import speaker_count_match
    from app.utils.diarization_metrics import words_to_rttm
    from app.utils.diarization_metrics import wser

    hyp_words = flatten_words(segments)
    ref_words, err = _ref_words_for(hyp_words, reference)
    if ref_words is None:
        return {"_error": err}

    w = wser(ref_words, hyp_words)

    # Map hyp speakers into the ref label space for island / boundary diagnostics.
    ref_seq = [rw.get("speaker") for rw in ref_words]
    hyp_seq = map_hyp_to_ref([hw.get("speaker") for hw in hyp_words], w.get("perm", {}))
    islands = count_bleed_islands(ref_seq, hyp_seq, max_island=3)

    # cpWER is optional (meeteval): null it out cleanly if the dep is absent.
    try:
        cpwer_val: float | None = cpwer(ref_words, hyp_words)
    except ImportError:
        cpwer_val = None

    # DER cross-check at collar 0.25 and 0.0, from word-derived RTTM turns.
    ref_rttm = _rttm_turns_from_words(ref_words, words_to_rttm)
    hyp_rttm = _rttm_turns_from_words(hyp_words, words_to_rttm)
    der_c025 = _safe_der(der, ref_rttm, hyp_rttm, collar=0.25)
    der_c0 = _safe_der(der, ref_rttm, hyp_rttm, collar=0.0)

    return {
        "wser": w["wser"],
        "t_wser": w["t_wser"],
        "n_scored": w["n_scored"],
        "n_excluded": w["n_excluded"],
        "n_word_errors": w["n_word_errors"],
        "perm": {str(k): str(v) for k, v in w.get("perm", {}).items()},
        "islands": len(islands),
        "island_hist": island_histogram(islands),
        "cpwer": cpwer_val,
        "der_c025": der_c025,
        "der_c0": der_c0,
        "boundary_prf": boundary_prf(ref_seq, hyp_seq),
        "speaker_count": speaker_count_match(ref_words, hyp_words),
        "n_words": len(hyp_words),
        "duration_s": duration_s,
    }


def _rttm_turns_from_words(words: list[dict[str, Any]], words_to_rttm: Any) -> list[tuple]:
    """Collapse words -> RTTM text -> (start, end, speaker) turns."""
    from app.utils.diarization_metrics import read_rttm

    text = words_to_rttm(words, uri="bench")
    if not text.strip():
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".rttm", delete=False) as fh:
        fh.write(text)
        tmp = fh.name
    try:
        return read_rttm(tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _safe_der(der: Any, ref_turns: list, hyp_turns: list, collar: float) -> float | None:
    """DER wrapper that returns None if pyannote.metrics is unavailable or empty."""
    if not ref_turns or not hyp_turns:
        return None
    try:
        return float(der(ref_turns, hyp_turns, collar=collar)["der"])
    except ImportError:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Per-file regression guard
# ──────────────────────────────────────────────────────────────────────────────


def regression_flags(off: dict[str, Any], on: dict[str, Any]) -> list[str]:
    """Flag a (file, model) if ON regressed vs OFF.

    Conditions: ON WSER worse than OFF by >5%; ON introduced bleed islands; or DER
    at collar 0 moved at all (ON should never perturb a correct boundary).
    """
    flags: list[str] = []
    if "_error" in off or "_error" in on:
        return ["scoring-error"]
    if on["wser"] > off["wser"] * 1.05:
        flags.append(f"wser-regression (off={off['wser']:.4f} on={on['wser']:.4f})")
    introduced = on["islands"] - off["islands"]
    if introduced > 0:
        flags.append(f"islands-introduced (+{introduced})")
    if (
        off.get("der_c0") is not None
        and on.get("der_c0") is not None
        and abs(on["der_c0"] - off["der_c0"]) > 1e-6
    ):
        flags.append(f"der_c0-moved (off={off['der_c0']:.6f} on={on['der_c0']:.6f})")
    return flags


# ──────────────────────────────────────────────────────────────────────────────
# Aggregation + reporting
# ──────────────────────────────────────────────────────────────────────────────


def aggregate(per_file: list[dict[str, Any]], n_boot: int) -> dict[str, Any]:
    """Pooled per-dataset + overall WSER and the paired bootstrap CI."""
    from app.utils.diarization_metrics import paired_bootstrap_wser

    by_dataset: dict[Any, list[dict[str, Any]]] = {}
    for rec in per_file:
        by_dataset.setdefault(rec.get("tier", 0), []).append(rec)

    def _pool(records: list[dict[str, Any]]) -> dict[str, Any]:
        triples: list[tuple[int, int, int]] = []
        off_err = on_err = scored = isl_off = isl_on = 0
        for r in records:
            off, on = r.get("off"), r.get("on")
            if not off or not on or "_error" in off or "_error" in on:
                continue
            triples.append((off["n_word_errors"], on["n_word_errors"], off["n_scored"]))
            off_err += off["n_word_errors"]
            on_err += on["n_word_errors"]
            scored += off["n_scored"]
            isl_off += off["islands"]
            isl_on += on["islands"]
        boot = paired_bootstrap_wser(triples, n_boot=n_boot)
        return {
            "n_files": len(triples),
            "off_wser": off_err / scored if scored else 0.0,
            "on_wser": on_err / scored if scored else 0.0,
            "islands_off": isl_off,
            "islands_on": isl_on,
            "bootstrap": boot,
        }

    return {
        "overall": _pool(per_file),
        "by_dataset": {
            str(k): _pool(v) for k, v in sorted(by_dataset.items(), key=lambda x: str(x[0]))
        },
    }


def write_summary_md(out_dir: Path, agg: dict[str, Any], flagged: list[dict[str, Any]]) -> Path:
    """Render the human-readable summary.md (per-dataset table + bootstrap CI)."""
    lines: list[str] = [
        "# Diarization Boundary Smoothing — Benchmark Summary",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M:%S}. Headline metric: WSER (lower is better).",
        "Bootstrap = paired CI on pooled (OFF - ON) improvement; significant iff ci_low > 0.",
        "",
        "## Per-dataset",
        "",
        "| dataset | files | OFF WSER | ON WSER | Δ (OFF-ON) | islands OFF→ON | bootstrap 95% CI | sig |",
        "|---|---:|---:|---:|---:|---:|---|:---:|",
    ]

    def _row(label: str, p: dict[str, Any]) -> str:
        b = p["bootstrap"]
        delta = p["off_wser"] - p["on_wser"]
        ci = f"[{b['ci_low']:+.4f}, {b['ci_high']:+.4f}]"
        sig = "yes" if b["significant"] else "no"
        return (
            f"| {label} | {p['n_files']} | {p['off_wser']:.4f} | {p['on_wser']:.4f} | "
            f"{delta:+.4f} | {p['islands_off']}→{p['islands_on']} | {ci} | {sig} |"
        )

    for ds, p in agg["by_dataset"].items():
        lines.append(_row(f"tier {ds}", p))
    lines.append(_row("**overall**", agg["overall"]))
    lines.append("")

    lines.append("## Regression flags")
    lines.append("")
    if flagged:
        lines.append("| file | model | flags |")
        lines.append("|---|---|---|")
        for f in flagged:
            lines.append(f"| {f['file_id']} | {f['model']} | {'; '.join(f['flags'])} |")
    else:
        lines.append("None — no (file, model) regressed under ON smoothing.")
    lines.append("")

    out = out_dir / "summary.md"
    out.write_text("\n".join(lines) + "\n")
    return out


def compare_baseline(agg: dict[str, Any], baseline_path: Path, out_dir: Path) -> None:
    """Compare overall ON WSER against a committed baseline JSON and log drift."""
    if not baseline_path.exists():
        # First run with this --baseline: write it so future runs can compare.
        baseline_path.write_text(json.dumps(agg, indent=2))
        log(f"baseline written: {baseline_path}")
        return
    base = json.loads(baseline_path.read_text())
    base_on = base.get("overall", {}).get("on_wser")
    new_on = agg["overall"]["on_wser"]
    if base_on is None:
        log("baseline has no overall.on_wser — skipping comparison")
        return
    delta = new_on - base_on
    verdict = "REGRESSION" if delta > 1e-4 else ("IMPROVED" if delta < -1e-4 else "unchanged")
    log(f"baseline comparison: ON WSER {base_on:.4f} -> {new_on:.4f} (Δ={delta:+.4f}) [{verdict}]")
    (out_dir / "baseline-compare.json").write_text(
        json.dumps(
            {
                "baseline_on_wser": base_on,
                "current_on_wser": new_on,
                "delta": delta,
                "verdict": verdict,
            },
            indent=2,
        )
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--corpus", required=True, type=Path, help="corpus.json (soak-corpus shape)")
    p.add_argument("--models", default="large-v3", help="comma-separated Whisper model names")
    p.add_argument(
        "--datasets", default="", help="comma-separated tier filter (e.g. '1,2'); empty=all"
    )
    p.add_argument(
        "--sample", type=int, default=0, help="cap files PER MODEL (0=all). Never silent."
    )
    p.add_argument("--smoothing", choices=("off", "on", "sweep"), default="on")
    p.add_argument("--bootstrap", type=int, default=2000, help="bootstrap resamples")
    p.add_argument("--baseline", type=Path, default=None, help="baseline JSON to write/compare")
    p.add_argument("--reference", default="auto", help="reference resolution mode (only 'auto')")
    p.add_argument(
        "--out", type=Path, required=True, help="output dir for per-file.json + summary.md"
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="rawinfer cache dir (default: <out>/cache)",
    )
    p.add_argument(
        "--ref-dir",
        type=Path,
        default=Path("/app/docs/boundary-benchmark/reference"),
        help="dir holding <fileid>__<model>.words.json or <fileid>.ref.rttm",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    require_container()
    args = parse_args(argv)

    if args.reference != "auto":
        log(f"WARNING: --reference={args.reference!r} unsupported; using 'auto'")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    tier_filter = {t.strip() for t in args.datasets.split(",") if t.strip()}

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir: Path = args.cache_dir or (out_dir / "cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    ref_dir: Path = args.ref_dir

    records = load_corpus(args.corpus)
    if tier_filter:
        records = [r for r in records if str(r.get("tier")) in tier_filter]
    log(f"corpus: {len(records)} files after tier filter {sorted(tier_filter) or 'ALL'}")

    variants = _smoothing_variants(args.smoothing)
    log(f"smoothing variants: {[v[0] for v in variants]}")

    source_language = os.environ.get("BENCHMARK_SOURCE_LANGUAGE", "en")
    min_speakers = int(os.environ.get("BENCHMARK_MIN_SPEAKERS", "1"))
    max_speakers = int(os.environ.get("BENCHMARK_MAX_SPEAKERS", "20"))

    per_file: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    for model in models:
        run_records = records[: args.sample] if args.sample > 0 else records
        if args.sample > 0:
            ran = [r["file_id"] for r in run_records]
            log(f"--sample {args.sample}: model={model} running {len(ran)} files: {ran}")
        else:
            log(f"model={model} running all {len(run_records)} files")

        for rec in run_records:
            file_id, audio_path = rec["file_id"], rec["audio_path"]
            log(f"[{model}] {file_id}  ({rec.get('filename', '')})")
            try:
                payload = build_or_load_rawinfer(
                    file_id,
                    audio_path,
                    model,
                    source_language,
                    min_speakers,
                    max_speakers,
                    cache_dir,
                )
                assigned = finalize_from_cache(payload)
            except Exception as exc:  # noqa: BLE001 — log and continue the corpus
                log(f"  ERROR building/finalizing {file_id} ({model}): {exc}")
                continue

            reference = _resolve_reference(ref_dir, file_id, model)
            if reference["kind"] == "none":
                log(f"  SKIP no reference for {file_id} ({model}) in {ref_dir}")
                continue

            scored: dict[str, dict[str, Any]] = {}
            skip_file = False
            for variant, cfg in variants:
                from app.utils.segment_postprocess import finalize_segments

                smoothed = finalize_segments(deepcopy(assigned), cfg)
                block = score_variant(smoothed, reference, rec.get("duration_s", 0.0))
                if block is None or "_error" in block:
                    msg = (block or {}).get("_error", "unknown") if block else "None"
                    log(f"  SKIP {file_id} ({model}) variant={variant}: {msg}")
                    skip_file = True
                    break
                scored[variant] = block
            if skip_file:
                continue

            entry: dict[str, Any] = {
                "file_id": file_id,
                "model": model,
                "tier": rec.get("tier", 0),
                "filename": rec.get("filename", ""),
                "reference_kind": reference["kind"],
            }
            entry.update(scored)
            # OFF/ON regression guard (only meaningful when both present).
            if "off" in scored and "on" in scored:
                flags = regression_flags(scored["off"], scored["on"])
                if flags:
                    flagged.append({"file_id": file_id, "model": model, "flags": flags})
                    log(f"  FLAG {file_id} ({model}): {'; '.join(flags)}")
            per_file.append(entry)

    (out_dir / "per-file.json").write_text(json.dumps(per_file, indent=2))
    log(f"wrote {out_dir / 'per-file.json'} ({len(per_file)} entries)")

    if any("off" in e and "on" in e for e in per_file):
        agg = aggregate(per_file, n_boot=args.bootstrap)
        (out_dir / "aggregate.json").write_text(json.dumps(agg, indent=2))
        summary = write_summary_md(out_dir, agg, flagged)
        log(f"wrote {summary}")
        b = agg["overall"]["bootstrap"]
        log(
            f"OVERALL: OFF={agg['overall']['off_wser']:.4f} ON={agg['overall']['on_wser']:.4f} "
            f"bootstrap mean={b['mean']:+.4f} CI=[{b['ci_low']:+.4f},{b['ci_high']:+.4f}] "
            f"significant={b['significant']}"
        )
        if args.baseline is not None:
            compare_baseline(agg, args.baseline, out_dir)
    else:
        log("no OFF+ON pairs scored — skipping aggregate/summary (off-only or all-skipped run)")

    if flagged:
        log(f"DONE with {len(flagged)} regression flag(s) — see summary.md")
        return 1
    log("DONE — no regressions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
