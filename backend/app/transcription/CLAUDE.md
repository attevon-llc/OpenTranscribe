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
- `ModelManager._diarizer_current` re-probes `diarizer_native.sidecar_ready()` per task and
  rebuilds in **both** directions — losing the sidecar mid-queue drops to PyAnnote, and a
  recovered sidecar is picked back up. (This was documented here as `sidecar_healthy()` — that
  is the liveness probe, not the readiness one the routing decision actually gates on; fixed
  per issue #672's audit.)
- The native client is a duck-typed drop-in: same `diarize()` / `embed_window()` /
  `unload_model()` surface, and `embed_window` never raises into the caller.

**The sidecar** (`docker-compose.diar-native.yml`, service `diar-native`): the *shared* backend
image with its CMD replaced by `diar-server`, listening on `:8701`, reading ONNX/PLDA artifacts
exported from `pyannote/speaker-diarization-community-1` out of `DIAR_NATIVE_MODELS_DIR`. It
holds **~2.2 GB of warm ORT arena** on `DIAR_NATIVE_GPU` (defaulting to `GPU_DEVICE_ID`, never a
bare 0) once it has served a request — a separate process's footprint, not the worker's.
⚠️ That figure was **~4.1 GB** everywhere in this repo until it was measured: `diar-server 0.3.1`
holds 2,248 MiB idle where the pre-0.3.1 binary held 4,762 MiB, both with
`SPEAKRS_LAZY_SESSIONS=1`, so the halving is the binary, not the flag. Re-measure with
`nvidia-smi --query-compute-apps` rather than trusting any number written here.

`docker-compose.diar-native.yml` is **CPU-safe on its own** — no device reservation, `DIAR_MODE`
defaults to `cpu`. The nvidia reservation and the `cuda` override live in
`docker-compose.diar-native-gpu.yml`, loaded alongside the GPU overlay, so a CPU-only or `--lite`
host can run the sidecar (#660). One binary serves both devices (`/healthz` advertises
`supported_devices: ["cuda","cpu"]`).

**Per-request device routing (issue #679).** `/diarize` and `/embed_window` both accept an
optional `"device": "cpu"|"cuda"` field, but the sidecar's request structs have no
`deny_unknown_fields` — an OLD sidecar (pre this field) silently ignores an unrecognised
`device` key and answers 200 having run on CUDA anyway, indistinguishable from success. So
**nothing may send it without first checking `diarizer_native.sidecar_supports_cpu_device()`**
(reads `/healthz`'s `supported_devices`, TTL-cached the same way as `sidecar_ready`/
`sidecar_healthy` — one cache, not a second one per capability). Implemented for the
embedding-only path only: `services/native_embedding_client.py`'s `_embed_window` routes to
CPU when advertised, since every call there is a lightweight 256-d ONNX forward pass and
never a full diarize job — freeing the sidecar's GPU slot for the diarize jobs sharing it.

⚠️ **CPU and CUDA are NOT bit-identical — and neither is CUDA with itself.** An upstream
claim of "max centroid delta 0.0 across every clip tested" was repeated in five places in
this repo until it was measured here (2026-09-04) against two real sidecars, one
`DIAR_MODE=cuda` and one `DIAR_MODE=cpu`, same binary digest, same `/models` export, same
10 s clip:

| comparison | max delta |
|---|---|
| CUDA vs CUDA — same sidecar, same input, twice | **2.86e-04** |
| CPU vs CPU — same sidecar, same input, twice | **0.0** |
| CUDA vs CPU | 4.11e-04 (cosine **0.999999816**) |

**CUDA is not deterministic with itself** (cuDNN algorithm selection varies run to run), so
the cross-device gap is barely larger than CUDA's own variance and byte-equality was never
achievable on any device pair. CPU is the bit-reproducible one. Routing `/embed_window` to
CPU is still a win — cosine 0.99999982 against a same-speaker mean of 0.85 and a
different-speaker mean of 0.09 means a voiceprint embedded on CPU matches identically — but
it is an *equivalence* win, not a bit-identity one. **Never assert byte-equality between two
embeddings, not even two CUDA runs; compare by cosine.**
Full `/diarize` output is NOT bit-identical: device choice can shift a segment boundary by
up to one segmentation frame (measured 0.016875 s on a 30 s clip), because a posterior
sitting on the binarisation threshold can land on either side depending on CPU vs CUDA
float arithmetic. That is below anything a transcript renders, but **never assert
byte-equality between a CPU-diarized and a CUDA-diarized run of the same file** — not in a
test fixture, a cache key, or a "did this change?" comparison; compare with a
≥one-frame tolerance, or pin the device. This is also why `/diarize` itself is NOT routed to
CPU by anything in this codebase: it is the heavy job CPU routing exists to protect *other*
work from, not a candidate to move there itself.

**Provisioning.** The ONNX/PLDA set is exported by the backend's FastAPI lifespan
(`native_provision.ensure_native_models`, called from `main.py`), because those graphs are gated
and non-redistributable. It is idempotent behind `diar-provision.json` — a valid marker makes
startup a `stat` pass; a marker-less directory (what every pre-0.3.0 install looks like) is
simply invalid, so the same call re-exports it. Measured cold: 483,882,939 bytes in 137 s. The
sidecar waits on `depends_on: backend: service_healthy`, which is what stops it starting against
an empty `/models` and crash-looping on exit 8. A failure here is never fatal — `/readyz` returns
503 and diarization falls back to PyAnnote.

> ⚠️ **"Configured native" is not "running native."** The fallback is silent by design, so a
> deployment can spend its whole life on PyAnnote while every setting says `native`. Verify by
> checking **which container served the diarization**, not by looking for an error. This was the
> normal case until #654/#655/#665/#672 were fixed together: nothing produced the models
> directory, and the overlay wired only `celery-worker`, so under gpu-scale and gpu-split
> PyAnnote was the *de-facto* engine. Still-open relatives: **#656** (no retry policy before the
> fallback is removed), **#703** (`celery-worker-gpu-transcribe` consumes a queue nothing
> publishes to).

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
- **Overlapped diarization requires a REACHABLE sidecar**, not merely a configured one.
  `engine/stages._overlap_diarization_enabled` gates on `diarizer_native.sidecar_ready()`
  (`DIAR_OVERLAP=0` still forces the sequential order). This matters because when overlap is on,
  `_collect_diarization` skips `_make_room_for_local_diarizer` — the **only**
  `release_transcriber()` call site — so keying it off configuration alone left Whisper and the
  in-process PyAnnote fallback co-resident on one GPU (#665, fixed). The probe is TTL-cached in
  `diarizer_native.py`; the cache is deliberately short so an admin toggle and a recovered
  sidecar are still picked up without a worker restart.
- Changing `EMBEDDING_BATCH_SIZE` changes VRAM predictability, not accuracy. Don't "optimize"
  it back to auto-scaling without re-running the VRAM profile.
- `DiarizationProviderFactory` (`app/services/diarization/`) is a **different axis** from
  `diarizer_backend`: it selects *where diarization happens* per user (ASR provider / local /
  pyannote.ai cloud / off). This file's `native` vs `pyannote` choice only applies once the
  answer is "locally, on our GPU". Read that package's CLAUDE.md before conflating them.
