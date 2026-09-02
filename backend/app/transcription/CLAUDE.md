# app/transcription — ASR + diarization engine

## Purpose

Whisper transcription and speaker diarization, plus the hardware-adaptive config that decides
where each stage runs. Called from `app/tasks/transcription/`.

Diarization has **two engines**: the out-of-process `diar-native` sidecar (Rust/ONNX, the coded
default) and the in-process PyAnnote fork (the failover, and the only other registered backend).
Read the diarization section below before assuming which one served a given job — several shipped
topologies silently run the fallback.

## Whisper model selection

Default: `large-v3-turbo`. Resolution order is pinned value → `SystemSettings` `asr.local_model`
→ `WHISPER_MODEL` → the coded default (`TranscriptionConfig._resolve_model_name`).

| Model | Best for | VRAM | Translation |
|---|---|---|---|
| `large-v3-turbo` | English-optimized, speed-critical | ~6 GB | **NO** |
| `large-v3` | Non-English, translation, max accuracy | ~10 GB | Yes |
| `large-v2` | Legacy fallback | ~10 GB | Yes |
| `medium` / `small` | Low-VRAM, hybrid CPU leg | ~5 GB / ~2 GB | Yes |

Those figures and the `supports_translation` flags come from `ASR_PROVIDER_CATALOG` in
`services/asr/factory.py` — the same catalog the settings UI reads. Keep them in sync there,
not here.

**Translation gotcha:** requesting "Translate to English" on a model that cannot translate does
**not** silently transcribe. `transcriber._resolve_task_and_language` consults
`ASRProviderFactory.get_model_capabilities`, logs `does not support translation — falling back
to transcribe`, and downgrades the task. Same safety net forces `language="en"` for
`english_only` models. The user still gets an untranslated transcript, so the UI guard matters —
the log line is the only signal at this layer.

## Diarization — native first, PyAnnote as failover

**One decision point:** `TranscriptionConfig.diarizer_backend`, resolved `SystemSettings`
`engine.diarizer_backend` → env `ENGINE_DIARIZER_BACKEND` → default `"native"`
(`config._resolve_diarizer_backend`), validated against `VALID_DIARIZER_BACKENDS` in
`engine/backends/__init__.py` (`{"native", "pyannote"}`, derived from `_DIARIZER_REGISTRY`).
Unknown values fail safe to `native` with a warning. It is re-read on **every** call — not
pinned like the model name — so an admin toggle and a recovered sidecar both take effect without
a worker restart. The old ad-hoc `DIARIZER_ENGINE` env reads are gone (#58); setting it does
nothing.

- `ModelManager._build_diarizer` constructs `NativeSpeakerDiarizer` (`diarizer_native.py`) for
  `native` and falls back to the in-process `SpeakerDiarizer` on **any** exception, logging
  `Native diarizer unavailable (...); falling back to PyAnnote`.
- `ModelManager._diarizer_current` re-probes `diarizer_native.sidecar_healthy()` per task and
  rebuilds in **both** directions — losing the sidecar mid-queue drops to PyAnnote, and a
  recovered sidecar is picked back up.
- The native client is a duck-typed drop-in: same `diarize()` / `embed_window()` /
  `unload_model()` surface, and `embed_window` never raises into the caller.

**The sidecar** (`docker-compose.diar-native.yml`, service `diar-native`): the *shared* backend
image with its CMD replaced by `diar-server`, listening on `:8701`, reading ONNX/PLDA artifacts
exported from `pyannote/speaker-diarization-community-1` out of `DIAR_NATIVE_MODELS_DIR`. It
holds **~4.1 GB of warm ORT arena** on `DIAR_NATIVE_GPU` (defaulting to `GPU_DEVICE_ID`, never a
bare 0) for as long as the container is up — that is a separate process's footprint, not the
worker's. `opentr.sh` auto-loads the overlay when the backend resolves to `native` and the export
directory exists; `--no-diar-native` suppresses it.

> ⚠️ **"Configured native" is not "running native."** The fallback is silent by design, so a
> deployment can spend its whole life on PyAnnote while every setting says `native`. Verify by
> checking **which container served the diarization**, not by looking for an error. Known live
> gaps: **#654** (nothing in this repo produces `${MODEL_CACHE_DIR}/diar-native`), **#655** (the
> overlay patches only `celery-worker` — gpu-scale, gpu-split, lite, offline, Windows, `--fresh`
> and CPU-only never get `DIAR_NATIVE_URL`), **#665** (`_overlap_diarization_enabled` keys off
> the *configured* backend, so the PyAnnote fallback runs co-resident with Whisper and
> `release_transcriber()` never fires), **#672** (the admin panel misreports the engine).

### PyAnnote fallback specifics

Fork `davidamacey/pyannote-audio@gpu-optimizations`, pinned to a **commit SHA** in
`requirements.txt` (a branch name is not a pin — see `backend/CLAUDE.md`). Embedding batch is
pinned at 16 by `SpeakerDiarizer.EMBEDDING_BATCH_SIZE`; `_configure_embedding_batch_size` *sets*
`PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE` in the process env to bypass the fork's auto-scaler.
`DIARIZATION_EMBEDDING_BATCH_SIZE` overrides it for research only.

- Measured (A6000, 2026-04-20, `docs/diarization-vram-profile/README.md`): ~950 MB process
  footprint at bs=16 fp32 plus ~500 MB CUDA context ≈ **1.5 GB** per pipeline; an A6000 hosts
  ~20+ concurrent. Above bs=16 the auto-scaler spends 3–7 GB for ~3% throughput.
- DER is invariant across fp32 batch ∈ {1..128}. **fp16 is not safe to ship**: it systematically
  merges speakers and drops 30–44% of segments at every batch size.
- `MIN_SPEAKERS=1` / `MAX_SPEAKERS=20` env defaults; raise `MAX_SPEAKERS` for conferences. On the
  **native** path these largely do not bind — the sidecar runs community-1 auto speaker counting,
  and an explicit `num_speakers` is logged as a warning and ignored.

Diagnostics: `python -m app.scripts.diarization_diag`. Upstream PR: pyannote-audio#1992.

## Hybrid mode (CPU transcription + GPU diarization)

`hardware_detection.should_use_hybrid_mode`, overridable with `WHISPER_HYBRID_MODE=true|false|auto`.
On CUDA it activates when the model's bs=2 peak (`_min_peak_mb`: 3893 MB for large variants,
3829 medium, 2933 small) exceeds 80% of total VRAM — i.e. below ~4.9 GB / ~4.8 GB / ~3.7 GB of
card. Transcription then moves to CPU (`WHISPER_HYBRID_CPU_MODEL`, default `small`, int8, bs=4)
while diarization stays on `hw.device`.

⚠️ **The MPS branch is unreachable in every shipped deployment.** `HardwareConfig._detect_optimal_device`
gates MPS on `platform.system().lower() == "darwin"`, and every documented install path runs the
backend in a **Linux** container, where that is `"linux"`. So on Apple Silicon the detector
returns `cpu`, hybrid mode does not activate, and `_maybe_warn_cpu_mode_misconfigured` fires
instead. Treat the MPS code paths here and in `README.md` as aspirational (issue #48), not as
behaviour you can rely on.

Key code: `app/utils/hardware_detection.py`, `config.py`. Benchmarks:
`docs/whisper-vram-profile/README.md`.

## Boundary correction (issue #193)

Two post-processing stages fix speaker mislabeling at turn boundaries. **All settings are
DB-backed and live in the admin UI → Settings → Engine Configuration** (no restart); env
(`ENGINE_BOUNDARY_*`) is fallback-only — there are no required `.env` vars.

- **Boundary smoothing** (default ON, pure-CPU): collapses 1–3 word "wrong-speaker islands"
  flanked by the same speaker with no real pause. Runs at the `finalize_segments()` chokepoint in
  `app/utils/segment_postprocess.py`, called from **`app/tasks/transcription/finalize.py`** (not
  `core.py`). −32% WSER on the reporter's clip, AMI-regression-safe. Code: `boundary_resolver.py`
  (`smooth_word_speakers`, `BoundarySmoothingConfig.from_db_env`). Key: `boundary_smoothing_enabled`.
- **Acoustic backchannel re-check** (default OFF, experimental, GPU): re-embeds short
  disputed/overlap words and reassigns by voiceprint cosine — relabels existing words only,
  never invents speech. Code: `acoustic_recheck` (`boundary_resolver.py`) driven through
  `manager.get_diarizer(tc).embed_window`, wired in `engine/stages.py`; carried on `EngineConfig`
  to keep the engine DB-free. Keys: `boundary_acoustic_recheck_enabled`,
  `boundary_acoustic_cosine_margin` (0.05), `boundary_acoustic_max_word_dur` (1.0). Because it
  goes through `get_diarizer`, it inherits whichever engine is live — the sidecar's
  `/embed_window` on the native path, which has its own window-length trap documented in
  `app/services/CLAUDE.md`.
- Settings API: `app/api/endpoints/engine_settings.py`; UI:
  `frontend/src/components/settings/EngineSettings.svelte`. Metrics (WSER/island/DER):
  `app/utils/diarization_metrics.py`. Benchmark: `backend/scripts/benchmark_boundary.py` (under
  `backend/`, not the repo-root `scripts/`). GPU-free regression:
  `backend/tests/integration/test_boundary_regression.py` (fixtures: `backend/tests/fixtures/boundary/`).

Docs: `docs-site/docs/features/boundary-correction.md`,
`docs-site/docs/developer-guide/diarization-boundary-correction.md`.

## Gotchas

- `EngineConfig` is deliberately **DB-free** — settings are resolved and passed in, not read
  from the DB inside the engine. Keep it that way so the engine stays unit-testable.
- **Overlapped diarization requires the sidecar.** `engine/stages._overlap_diarization_enabled`
  runs diarization concurrently with transcription only for `diarizer_backend == "native"`
  (`DIAR_OVERLAP=0` disables). When it is on, `_collect_diarization` skips
  `_make_room_for_local_diarizer` — which is where `release_transcriber()` lives. See #665.
- Changing `EMBEDDING_BATCH_SIZE` changes VRAM predictability, not accuracy. Don't "optimize"
  it back to auto-scaling without re-running the VRAM profile.
- `DiarizationProviderFactory` (`app/services/diarization/`) is a **different axis** from
  `diarizer_backend`: it selects *where diarization happens* per user (ASR provider / local /
  pyannote.ai cloud / off). This file's `native` vs `pyannote` choice only applies once the
  answer is "locally, on our GPU". Read that package's CLAUDE.md before conflating them.
