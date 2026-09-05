# backend/app/utils — shared helpers

## Purpose

Low-dependency helpers shared by endpoints, services, and Celery tasks. DB-touching query
builders are allowed here (`db_helpers`), but anything with real domain logic belongs in
`app/services`. These modules load in **all three worker flavors** (API, GPU, CPU/redaction),
so keep heavy imports lazy.

## Key files — single-home rules

- `time_format.py` — **the only home for backend timestamp formatting**
  (`format_timestamp_simple`, `format_srt_timestamp`). Display-level formatting of durations /
  status / sizes belongs to `services/formatting_service.py`.
- `websocket_notify.py` — `send_ws_event(user_id, type, data)` is **THE** way any synchronous
  code (endpoint, task, service) pushes a notification. It publishes to Redis; `api/websockets.py`
  fans out to the connected sockets. Returns bool, never raises.
- `uuid_helpers.py` — the hybrid-ID + permission chokepoint. `get_*_by_uuid`,
  `get_file_by_uuid_with_permission` (admin bypass → takedown 404 → public → tenant gate → owner
  → shares, in that order), `require_resource_owner`.
- `db_helpers.py` — `apply_tenant_scope` (SQL-plane default-deny tenant filter mirroring
  `api/deps_context.scope_to_context`), user file/tag/speaker query builders, tag-cache busting.
- **There are no authorization decorators here, and reintroducing one is the mistake to avoid.**
  `auth_decorators.py` was deleted in issue #450: zero call sites for its four gates, and its only
  importer (`services/transcription_service.py`, a whole parallel copy of the transcription
  routers) was itself imported by nothing. Two reasons not to revive the shape — the gates were
  **kwargs-only**, reading `db`/`current_user`/`file_id` out of `kwargs`, so any positional call
  skipped the check; and `require_verified_user` gated on `is_active` while both its name and its
  403 detail said "verification". Authorization is a FastAPI `Depends`
  (`api/endpoints/auth/dependencies.py` for privilege, `uuid_helpers` for resource access).
- `error_handlers.py` — `handle_database_errors` (rolls back the session in `kwargs["db"]`) and
  `ErrorHandler` builders for opaque 5xx. `pagination.py` — `paginate()` replaces the
  count+offset+limit boilerplate (counts with `order_by(None)`).
- `encryption.py` — AES-256-GCM (v3) with legacy Fernet auto-detect. Every stored secret (ASR/LLM
  keys, S3/SMB creds, OIDC refresh/ID tokens) goes through it.
- `uuid7.py` — RFC 9562 UUIDv7, the `default=` for every model `uuid` column (index locality).
- `scratch_volume.py` — cross-worker WAV handoff at `/scratch/opentranscribe`. **Presence of the
  mount is the feature flag** — there is no enable/disable env var.
- `task_lock.py` — Redis lock preventing overlapping periodic tasks. `task_utils.py` — task
  records, status transitions, stuck-file recovery. `error_classification.py` — permanent vs
  retriable, gating retry decisions.
- `hardware_detection.py` — the CUDA/MPS/CPU authority (`should_use_hybrid_mode`), and the
  **single home for "which CUDA ordinal is ours"** (`resolve_cuda_device_index`, issue #719).
  Read the rule below before adding any `cuda:N` / `get_device_properties(N)` call anywhere.
- `text_preprocessing.py` — **topics/collections extraction ONLY**; never use it for
  summarization or speaker ID (it strips the grammar those need).
- `segment_dedup.py`, `segment_postprocess.py`, `diarization_merge.py`, `diarization_metrics.py`
  — pipeline math; see `backend/app/transcription/CLAUDE.md`.
- `benchmark_timing.py` / `vram_profiler.py` / `nvml_monitor.py` — instrumentation gated on
  `ENABLE_BENCHMARK_TIMING` / `ENABLE_VRAM_PROFILING`, so production pays zero overhead.

## Conventions / patterns

- No `app.api` imports from here (`db_helpers` types `RequestContext` under `TYPE_CHECKING`).
  Import-linter also forbids `cloud` and managed-edition vendor imports — see
  `backend/app/core/CLAUDE.md`.
- Optional heavy deps (pyannote.metrics, meeteval, torch) are imported **inside** the function
  that needs them so these modules stay importable on CPU-only workers and in fast unit tests.
- Best-effort side paths (cache invalidation, WS publish, metrics) log and swallow — they must
  never break the write they accompany.

## GPU device selection — ordinal 0 is right almost everywhere, and that is deliberate

**Docker picks the card, not the app.** Every GPU service is reserved with
`device_ids: ['${GPU_DEVICE_ID}']`; the NVIDIA container runtime resolves that in
nvidia-smi/PCI order and exposes it to the container as its **only** device, at ordinal 0.
Measured on this host: a container reserved `device=1` sees exactly one device and it is the
RTX 3080 Ti nvidia-smi calls 1. So `cuda:0` inside a worker is already the operator's card.

⚠️ **Do NOT "fix" that by setting `CUDA_VISIBLE_DEVICES=$GPU_DEVICE_ID`.** Measured:
`device_ids:['1']` + `CUDA_VISIBLE_DEVICES=1` gives `cuInit` -> 100 (`CUDA_ERROR_NO_DEVICE`)
and `torch.cuda.device_count() == 0` — every GPU worker silently drops to CPU while still
reporting healthy. `scripts/release-tests/lib/env-template.sh` did exactly this into a
generated `.env` that every service reads; that is why the line is now a warning comment.

**The one exception is a `count: all` reservation** — today `celery-cpu-worker`, which needs
NVML visibility of every card for `system.update_gpu_stats`. It sees them all, so ordinal 0 is
an arbitrary card the deployment may not own. `resolve_cuda_device_index()` is the whole rule:
one visible device -> `0`; several -> `GPU_DEVICE_ID`, validated against the visible count and
falling back to `0` (never raising, never guessing).

`HardwareConfig.device_index` holds the resolved ordinal and **every** cuda call in that class
uses it — selection *and* VRAM sizing. Those are the same bug twice: reading device 0's VRAM
while computing on device 1 sizes Whisper batches against the wrong card (measured: 47.4 GiB
-> batch 16 vs 11.6 GiB -> batch 8).

**`CUDA_DEVICE_ORDER=PCI_BUS_ID` is pinned as image ENV** in all four backend Dockerfiles, which
is what makes a host index equal a CUDA ordinal. CUDA's default `FASTEST_FIRST` does not match
nvidia-smi (measured here: nvidia-smi `0=A6000/1=RTX3080Ti/2=A6000` vs CUDA
`0=A6000/1=A6000/2=RTX3080Ti`). It is image ENV rather than compose `environment:` because it
must be set before the first CUDA call, and because the API, every Celery worker and the
diar-native sidecar all run from that one image with only their CMD replaced.

**Check, never assume, which card you got:** `scripts/gpu-device-order-probe.py` prints the
ordinal -> PCI bus mapping using the driver API only — no context, no VRAM, safe against cards
other work owns (unlike `torch.cuda.get_device_name()`, which opens a ~300 MiB context on every
device it touches). NVML indices (`nvml_monitor.py`) are always PCI-ordered and are **not**
affected by `CUDA_VISIBLE_DEVICES`, which is why pinning CUDA does not break the stats task.

## NLTK is NOT in the transcription critical path — never let it fail a job

**Catch `NLTK_CORPUS_UNAVAILABLE` (`utils/nltk_offline.py`), never `LookupError`.**

NLTK powers sentence splitting, RAG chunking and topic extraction. ASR and diarization do
not use it at all. Every one of those three has a working degraded mode — coarser segments,
regex splitting, regex tokens — so an unusable corpus is a reason to produce a slightly worse
transcript, never a reason to produce none.

That principle is issue #491's, and its guards caught **`LookupError` only**, which is the
resource-MISSING case. The resource-PRESENT-but-UNREADABLE case raises **`OSError`**: wrong
ownership on the model cache (exactly what `scripts/fix-model-permissions.sh` repairs), a
truncated pickle, a full volume — and since nltk 3.10, its **pathsec** CWE-59 hardening, which
raises `PermissionError` for any corpus file with `st_nlink > 1`. A hardlinked model cache
therefore failed **every transcription** with a `Security Violation [pathsec.open]` naming
nothing an operator could act on. Same class of failure as #491, second route in.

```python
from app.utils.nltk_offline import NLTK_CORPUS_UNAVAILABLE   # (LookupError, OSError)
```

- Deliberately **not** `Exception` — a `TypeError`/`AttributeError` from our own code is a
  defect and must keep propagating.
- **`KeyError` and `IndexError` are subclasses of `LookupError`**, so any guard catching a
  missing NLTK resource necessarily catches them too. Pre-existing, unavoidable while NLTK
  raises `LookupError`, and pinned in `test_nltk_corpus_unavailable_degrades.py` so nobody
  later concludes the `OSError` widening introduced it.
- **Always log `type(exc).__name__` and the message.** The old warnings said only "NLTK punkt
  unavailable", which is what made a security refusal indistinguishable from a missing download.

### ⚠️ Repairing the corpus on disk needs a WORKER RESTART

Two of the three sites cache their verdict for the life of the process, so fixing permissions
or breaking hardlinks does **not** take effect until the worker restarts. "I fixed it and
nothing changed" is the expected symptom, not a second bug:

| site | on failure | picks up a repaired corpus? |
|---|---|---|
| `segment_dedup.split_sentences_nltk` | returns segments unsplit | **yes** — retried per call |
| `text_preprocessing._get_stopwords` | empty stopword set | **no** — `@lru_cache(maxsize=1)` |
| `search/chunking_service` | regex splitter | **no** — `_nltk_load_failed` latches (#449) |

The chunking latch is **deliberate and must not be removed**: a retry lets one re-index chunk
its early files with the regex and its later files with punkt, which disagree on abbreviations
— one corpus chunked two ways in a single pass. `reset_sentence_splitter_state()` is for tests
only. Restart with `./opentr.sh restart-backend` (Celery workers do **not** hot-reload the way
the dev API does).

## Gotchas

- **Both dedup columns now hold the same *kind* of value, and neither is collision-resistant.**
  `MediaFile.imohash` (via `services/imohash_service.py`) is the server-computed sampled
  fingerprint; `MediaFile.file_hash` is the *client-declared* one, and since issue #342 the
  browser computes it with the same imohash algorithm rather than SHA-256 — whole-file SHA-256
  threw `NotReadableError` above ~4 GB and the swallowed error silently disabled duplicate
  detection on the largest uploads. `file_hash.py:check_duplicate_by_fingerprint` matches
  **either** column, so rows predating the change (SHA-256 in `file_hash`, imohash in `imohash`)
  keep deduplicating. Never use either for security-sensitive equality.
- `encryption.py` transparently decrypts legacy Fernet ciphertext — don't assume a single format.
