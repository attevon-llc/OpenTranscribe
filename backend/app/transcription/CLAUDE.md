# app/transcription — ASR + diarization engine

> ⚠️ **STALE — do not trust the diarization sections below.** This file predates the native
> Rust/ONNX `diar-native` sidecar becoming the coded default (`ffd49433` / `a15e94c2`). It has
> **zero** mentions of it, still calls PyAnnote "the engine", claims "PyAnnote MPS via our fork is
> solid" for macOS (MPS is unreachable inside Docker on every platform), and its diarization VRAM
> figures are wrong by ~4× — the sidecar's warm floor is 4 136 MiB, not ~1 GB, and it does not
> shrink. The Whisper/transcription sections are unaffected. Rewrite tracked in **#671**; current
> architecture and the removal roadmap in
> [this gist](https://gist.github.com/attevon-admin/a99819c7ec5e8ab8df0eb8e3e8e668e8) and **#572**.

## Purpose

WhisperX transcription and PyAnnote speaker diarization, plus the hardware-adaptive config
that decides where each stage runs. Called from `app/tasks/transcription/`.

## Whisper model selection

Default: `large-v3-turbo` (6× faster, ~6 GB VRAM). Override via `WHISPER_MODEL`.

| Model | Best for | VRAM | Translation |
|---|---|---|---|
| `large-v3-turbo` | English, most major languages, speed-critical | ~6 GB | **NO** — not trained for it |
| `large-v3` | Non-English (Thai/Cantonese/Vietnamese), translation, max accuracy | ~10 GB | Yes |
| `large-v2` | Legacy fallback | ~10 GB | Yes |

**Gotcha:** if a user enables "Translate to English" they must be on `large-v3` or `large-v2`.
Turbo will silently not translate.

## Hybrid mode (CPU transcription + GPU/MPS diarization)

Auto-activates for low-VRAM CUDA GPUs and always on macOS (faster-whisper MPS is unreliable;
PyAnnote MPS via our fork is solid). Diarization needs only ~1.3 GB VRAM.

CUDA trigger: minimum batch=2 model peak > 80% of total VRAM (turbo/v3: < ~4.9 GB GPU,
medium: < ~4.8 GB, small: < ~3.7 GB).

```bash
WHISPER_HYBRID_MODE=auto       # auto (default) | true | false
WHISPER_HYBRID_CPU_MODEL=small # small | medium | base
```

Key code: `app/utils/hardware_detection.py` (`should_use_hybrid_mode`), `config.py`,
`diarizer.py`. Benchmarks: `docs/whisper-vram-profile/README.md`.

## Diarization (PyAnnote — optimized fork)

Fork: `davidamacey/pyannote-audio@gpu-optimizations` (pip-installable). Embedding batch is
**fixed at 16** in `SpeakerDiarizer.EMBEDDING_BATCH_SIZE`, which forces the fork's auto-scaler
off via `PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE=16`.

- Diarization peak ≈ 1 GB VRAM over process baseline → an A6000 hosts ~25 concurrent pipelines.
- DER vs reference is invariant across batch ∈ {1..128} fp32; fixing the batch trades 3%
  wall-time for predictable VRAM.
- `MIN_SPEAKERS=1`, `MAX_SPEAKERS=20` defaults; raise `MAX_SPEAKERS` to 30–50+ for conferences
  (no hard cap — sklearn `AgglomerativeClustering`).

Diagnostics: `python -m app.scripts.diarization_diag`. Raw data:
`docs/diarization-vram-profile/README.md`. Upstream PR: pyannote-audio#1992.

## Boundary correction (issue #193)

Two post-processing stages fix speaker mislabeling at turn boundaries. **All settings are
DB-backed and live in the admin UI → Settings → Engine Configuration** (no restart); env
(`ENGINE_BOUNDARY_*`) is fallback-only — there are no required `.env` vars.

- **Boundary smoothing** (default ON, pure-CPU): collapses 1–3 word "wrong-speaker islands"
  flanked by the same speaker with no real pause. Runs at the `finalize_segments()` chokepoint.
  −32% WSER on the reporter's clip, AMI-regression-safe. Code: `boundary_resolver.py`
  (`smooth_word_speakers`, `BoundarySmoothingConfig`), `app/utils/segment_postprocess.py`,
  called from `app/tasks/transcription/core.py`. Key: `boundary_smoothing_enabled`.
- **Acoustic backchannel re-check** (default OFF, experimental, GPU): re-embeds short
  disputed/overlap words and reassigns by voiceprint cosine — relabels existing words only,
  never invents speech. +~15% WSER on top of the smoother, ~1.9 s / 10-min file. Code:
  `acoustic_recheck` (`boundary_resolver.py`), `diarizer.embed_window`, wired in
  `engine/stages.py`; carried on `EngineConfig` to keep the engine DB-free. Keys:
  `boundary_acoustic_recheck_enabled`, `boundary_acoustic_cosine_margin` (0.05),
  `boundary_acoustic_max_word_dur` (1.0).
- Settings API: `app/api/endpoints/engine_settings.py`; UI:
  `frontend/src/components/settings/EngineSettings.svelte`. Metrics (WSER/island/DER):
  `app/utils/diarization_metrics.py`. Benchmark: `scripts/benchmark_boundary.py`. GPU-free
  regression: `backend/tests/integration/test_boundary_regression.py` (fixtures:
  `backend/tests/fixtures/boundary/`).

Docs: `docs-site/docs/features/boundary-correction.md`,
`docs-site/docs/developer-guide/diarization-boundary-correction.md`.

## Gotchas

- `EngineConfig` is deliberately **DB-free** — settings are resolved and passed in, not read
  from the DB inside the engine. Keep it that way so the engine stays unit-testable.
- Changing `EMBEDDING_BATCH_SIZE` changes VRAM predictability, not accuracy. Don't "optimize"
  it back to auto-scaling without re-running the VRAM profile.
