---
sidebar_position: 4
---

# Diarization Boundary Correction (Internals)

This page documents the architecture of the diarization boundary-correction feature
([issue #193](https://github.com/attevon-llc/OpenTranscribe/issues/193)): the two
post-processing stages, where they are wired into the pipeline, the configuration plumbing
that keeps the engine database-free, and the metrics / benchmark / regression tooling.

For the user-facing explanation, see
[Diarization Boundary Correction](../features/boundary-correction.md).

## Overview

| Stage | Default | Where it runs | What it touches |
|---|---|---|---|
| **Boundary smoothing** | On | `finalize_segments()` chokepoint (CPU) | Per-word speaker labels only — no audio, no model |
| **Acoustic re-check** | Off | `_GpuStage._run_diarization()` (GPU) | Re-embeds short disputed words and reassigns by voiceprint |

Both stages **only relabel words that already exist**. Neither adds, removes, or invents
speech.

## Stage 1 — Boundary smoothing

**Module:** `backend/app/transcription/boundary_resolver.py`

The core is `smooth_word_speakers(segments, cfg)`. It flattens the per-word speaker labels,
builds maximal same-speaker runs, and collapses a short "island" run of speaker B back into
speaker A when:

- both flanks are the **same** other speaker A (covers 3+ speaker cases),
- each flank is itself a genuine run (`min_flank_words`, default 3),
- there is **no real pause** at either seam (`min_silent_gap`, default 0.4 s — a pause is
  evidence of a true turn), and
- the island is short enough (`max_island_words` = 3, `max_island_duration` = 1.5 s).

A second, optional phase (`margin_threshold > 0`) also collapses a *longer* island when
every word in it is "disputed" — i.e. carries a small top1–top2 overlap margin attached by
`assign_speakers()` under the transient `_overlap_margin` key. It is disabled by default.

The function mutates the same word dicts referenced by `segments` and is **idempotent**:
re-running on already-smoothed output is a no-op.

### Config: `BoundarySmoothingConfig`

Resolved via `BoundarySmoothingConfig.from_db_env(db)` with precedence
**DB `SystemSettings` → `ENGINE_BOUNDARY_*` env → dataclass defaults**. Passing `db=None`
(the benchmark harness and unit tests do this) uses env/defaults only, so the module stays
importable in non-GPU contexts.

Defaults live in the dataclass:

| Field | Default | DB key | Env var |
|---|---|---|---|
| `enabled` | `True` | `engine.boundary_smoothing_enabled` | `ENGINE_BOUNDARY_SMOOTHING_ENABLED` |
| `max_island_words` | `3` | `engine.boundary_max_island_words` | `ENGINE_BOUNDARY_MAX_ISLAND_WORDS` |
| `max_island_duration` | `1.5` | `engine.boundary_max_island_duration` | `ENGINE_BOUNDARY_MAX_ISLAND_DURATION` |
| `min_flank_words` | `3` | `engine.boundary_min_flank_words` | `ENGINE_BOUNDARY_MIN_FLANK_WORDS` |
| `min_silent_gap` | `0.4` | `engine.boundary_min_silent_gap` | `ENGINE_BOUNDARY_MIN_SILENT_GAP` |
| `margin_threshold` | `0.0` | `engine.boundary_margin_threshold` | `ENGINE_BOUNDARY_MARGIN_THRESHOLD` |

(`acoustic_recheck_enabled`, `acoustic_cosine_margin`, `acoustic_max_word_dur` also live on
this dataclass but are consumed by Stage 2 via `EngineConfig` — see below.)

### The `finalize_segments` chokepoint

**Module:** `backend/app/utils/segment_postprocess.py`

`finalize_segments(segments, smoothing_config)` is the **single** post-diarization
chokepoint that every transcription path routes through (legacy pipeline, combined engine,
split engine, multi-GPU). It runs:

```
smooth_word_speakers (if enabled) → resegment_by_speaker → merge_consecutive_segments
```

With smoothing disabled or `smoothing_config=None` it is byte-identical to the previous
`merge_consecutive_segments(resegment_by_speaker(segments))`. Smoothing runs **before**
resegmentation, so the corrected labels drive how segments are split at speaker boundaries.

It is called from the post-processing path in `backend/app/tasks/transcription/core.py`,
which resolves the config once per file inside a DB session:

```python
from app.transcription.boundary_resolver import BoundarySmoothingConfig
from app.utils.segment_postprocess import finalize_segments

with session_scope() as db:
    smoothing_cfg = BoundarySmoothingConfig.from_db_env(db)
result["segments"] = finalize_segments(result["segments"], smoothing_cfg)
```

## Stage 2 — Acoustic backchannel re-check

The boundary smoother cannot fix **backchannel absorption**: a short "yeah"/"mm-hmm" by the
listening speaker that diarization absorbed into the dominant speaker's turn is *not* an
island, so there is no rule-based signal. The acoustic re-check resolves it by listening to
the word again.

**Function:** `acoustic_recheck(...)` in `backend/app/transcription/boundary_resolver.py`.

For each candidate word it embeds the word's audio window and reassigns it to the speaker
whose centroid it is cosine-closest to. Candidates are short words
(≤ `max_word_dur`, default 1.0 s) that are *either* Phase-2 disputed (small
`_overlap_margin`) *or* fall inside a diarization overlap region — the two places absorption
happens. A word is reassigned **only** when another speaker's centroid is cosine-closer by
at least `cosine_margin` (default 0.05). It returns the number of words reassigned and
**only mutates the `speaker` field** of existing words.

### Embedding a window

**Method:** `SpeakerDiarizer.embed_window(audio, start, end)` in
`backend/app/transcription/diarizer.py`.

It embeds an audio window with the **same** WeSpeaker model that produced the speaker
centroids (`native_embeddings`), returning a 256-d vector or `None`. Sub-second windows are
center-padded to the model's minimum embeddable length (fallback ~0.8 s) because embeddings
of very short clips are unreliable. It **never raises into the pipeline** — any failure
returns `None`, and the caller keeps the original max-overlap label.

### Wiring in the GPU engine stage

**Module:** `backend/app/transcription/engine/stages.py` (`_GpuStage._run_diarization`).

After `assign_speakers()`, while `audio` and `native_embeddings` are still in memory:

```python
if native_embeddings and config is not None and config.boundary_acoustic_recheck_enabled:
    from app.transcription.boundary_resolver import acoustic_recheck
    words = [w for s in result["segments"] for w in s.get("words", []) or []
             if "speaker" in w and "start" in w]
    try:
        acoustic_recheck(
            words, native_embeddings,
            lambda s, e: diarizer.embed_window(audio, s, e),
            overlap_regions=overlap_info.get("regions"),
            cosine_margin=config.boundary_acoustic_cosine_margin,
            max_word_dur=config.boundary_acoustic_max_word_dur,
        )
    except Exception:
        logger.exception("acoustic_recheck failed; keeping max-overlap labels")
```

The corrected labels then flow to the `finalize_segments` chokepoint in `core.py`, so the
re-checked assignment drives segmentation.

### Keeping the engine DB-free: `EngineConfig` injection

**Module:** `backend/app/transcription/engine/config.py`.

The engine stage must not perform database reads (it runs in GPU workers, and the multi-GPU
split serializes config across processes). The three acoustic settings are therefore carried
as plain fields on `EngineConfig`:

- `boundary_acoustic_recheck_enabled` (bool, default `False`)
- `boundary_acoustic_cosine_margin` (float, default `0.05`)
- `boundary_acoustic_max_word_dur` (float, default `1.0`)

They are resolved **once** at job-build time by `EngineConfig.from_db_with_env_fallback(db)`
(DB → env → default), and they round-trip through `to_snapshot()` / `from_snapshot()` so the
split-pipeline stages reconstruct them from pinned values without re-reading the DB.

## Configuration surface

All boundary settings are DB-backed `SystemSettings` and exposed through the admin API and
UI. No `.env` variables are *required* — env is fallback-only.

- **API:** `backend/app/api/endpoints/engine_settings.py` — `GET ""`, `POST "/update"`,
  `DELETE "/{key}"` (reset to env/default). Each value is returned with its `source`
  (`db` / `env` / `default`). The update model validates `boundary_acoustic_cosine_margin`
  ∈ [0, 1] and `boundary_acoustic_max_word_dur` ∈ [0.1, 5.0].
- **UI:** `frontend/src/components/settings/EngineSettings.svelte` — Settings → Engine
  Configuration. Toggles for smoothing and acoustic re-check, plus the cosine-margin and
  max-word-duration inputs.

The admin-facing keys (`engine.boundary_smoothing_enabled`,
`engine.boundary_acoustic_recheck_enabled`, `engine.boundary_acoustic_cosine_margin`,
`engine.boundary_acoustic_max_word_dur`) are the ones surfaced in the UI; the advanced
smoother knobs (`engine.boundary_max_island_words`, etc.) are DB-overridable but not shown.

## Metrics module

**Module:** `backend/app/utils/diarization_metrics.py`.

The headline metric is **WSER (Word Speaker Error Rate)**. Because Whisper words and timings
are held fixed across reference and hypothesis, every word has an identity, so WSER is exact,
**alignment-free, and collar-free** — a 1–3 word bleed counts as exactly 1–3 word errors.
Heavy/optional deps (`pyannote.metrics`, `meeteval`) are imported lazily so the module loads
in fast unit tests.

Key functions:

- `wser(ref_words, hyp_words)` — count- and duration-weighted WSER over a parallel word
  inventory, with the optimal hyp→ref label permutation (`linear_sum_assignment`).
- `count_bleed_islands(ref_seq, hyp_seq, max_island=3)` — the direct bug signature: spurious
  wrong-speaker islands flanked by the same correct speaker. Plus `island_histogram()`.
- `categorize_errors(...)` — splits per-word errors into **boundary** (the smoother's
  target) vs **interior / backchannel-absorption** (the acoustic re-check's target).
- `der(...)` — Diarization Error Rate via `pyannote.metrics` (cross-check).
- `cpwer(...)` — concatenated min-permutation WER via `meeteval` (cross-check).
- `boundary_prf(...)`, `speaker_count_match(...)`, and `paired_bootstrap_wser(...)` for a
  paired bootstrap CI on the pooled (OFF − ON) improvement (significant only if `ci_low > 0`).

## Benchmark harness

**Script:** `backend/scripts/benchmark_boundary.py`.

Runs inside the backend container. The expensive GPU stage runs **once per (file, model)**;
its `RawInferenceResult` is cached to `*.rawinfer.json`, so re-runs are CPU-only. For each
(file, model) it produces speaker-assigned segments via `Engine.run_cpu_finalize`, then
evaluates `finalize_segments` with smoothing **OFF vs ON**, reporting WSER (headline), cpWER
and DER (cross-checks), and a paired bootstrap over the pooled improvement.

```bash
docker compose exec backend python /app/backend/scripts/benchmark_boundary.py \
    --corpus /app/docs/benchmark-corpus/corpus.json \
    --models large-v3 --sample 5 --smoothing on --out /app/docs/boundary-benchmark
```

Reference resolution (`--reference auto`): a positional `*.words.json` is used exactly
(no mapping); otherwise a `*.ref.rttm` is midpoint-mapped via `assign_words_from_turns`.

## GPU-free regression test

**Test:** `backend/tests/integration/test_boundary_regression.py`.
**Fixtures:** `backend/tests/fixtures/boundary/` (committed `*.rawinfer.json` + sibling
`*.ref.words.json` / `*.baseline.json`).

This module **never calls the GPU** — the expensive transcription/diarization output is
frozen in committed fixtures, so the test replays only the CPU path:

```
RawInferenceResult.deserialize(fixture)
  → Engine.run_cpu_finalize          (CPU only — GPU output frozen)
  → finalize_segments OFF / ON       (boundary smoothing under test)
  → WSER / island / DER assertions   (vs a frozen baseline)
```

Two layers:

1. `test_fixture_regression` — parametrized over `fixtures/boundary/*.rawinfer.json`. A real
   fixture **is committed**: `karpathy_10m` (the reporter's clip, 10-min slice) ships as a
   trio — `karpathy_10m.rawinfer.json` (frozen GPU output, ~200 KB), `karpathy_10m.ref.words.json`
   (per-word ground truth, derived from the committed `reference.rttm` turns), and
   `karpathy_10m.baseline.json` (frozen OFF/ON WSER + island + DER numbers). For each fixture
   the test asserts ON WSER does not worsen vs the frozen OFF baseline (5% slack), ON introduces
   **zero** new bleed islands, word-derived collar-0 DER does not worsen, and — against the
   committed baseline — ON WSER and island count have not drifted. This is the gate that stops a
   future change to `assign_speakers`, the margin logic, or `smooth_word_speakers` from silently
   regressing the fix; it runs **GPU-free in CI** (the GPU output is frozen in the fixture).
   Regenerate the trio after an intentional change with
   `backend/scripts/build_regression_fixture.py` (GPU worker).
2. `test_synthetic_bleed_fixed` — fully self-contained (no GPU, no fixture, no engine).
   Builds a crafted 2-word bleed island in Python, runs `finalize_segments` OFF/ON, and
   asserts ON clears the island and improves WSER while OFF preserves it. Always runs in CI.

Run it:

```bash
cd backend && PYTHONPATH=. python -m pytest \
    tests/integration/test_boundary_regression.py -v
```

Unit tests for the resolver itself live in
`backend/tests/unit/test_boundary_resolver.py`.
