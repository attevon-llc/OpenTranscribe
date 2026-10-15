# #1246 — Download and activate local transcription models from Settings (plan)

Status: PLAN ONLY, nothing implemented. Written 2026-10-15 against `feat/v0.6.0-frontend-ux`
@ `8b27ab8d`. Issue: attevon-llc/OpenTranscribe#1246 (milestone v0.6.0, labels enhancement,
frontend, backend, asr). Depends on #1245 (HF token resolver, branch `feat/v060-hf-token-ui`,
uncommitted at the time of writing in worktree `.claude/worktrees/agent-ac9861ca40ce3306c`).

This plan is meant to be executed by an implementation agent with no conversation memory.
Every file path is relative to the repo root unless absolute. `file:line` citations are
against `8b27ab8d`; re-check them before editing, the branch moves fast.

---

## 0. Owner decisions (fixed, do not re-open)

1. Model list = the list in `VALID_LOCAL_WHISPER_MODELS` (`backend/app/schemas/media.py:19-37`,
   13 ids). No free-form repo ids anywhere (UI, API, script).
2. Download logic moves out of `scripts/download-models.py` into backend modules. The script
   stays for initial setup and becomes a thin caller of the same modules. ONE catalog
   (id, HF repo, approx size, VRAM hint, language/translation flags) feeds the Settings list,
   the backend allow-list and the script.
3. UI: per-model Download button; a Celery task using the UI-stored HF token via
   `get_hf_token()` (#1245); progress over the WebSocket; Cancel; free-disk check; one
   download at a time (deployment-wide).
4. Activation: wait for in-flight jobs, then switch the worker using the existing
   `POST /asr-settings/local-model/set` + `POST /asr-settings/local-model/restart` drain flow.
   The UI says the transcription worker restarts.
5. Cloud ASR: the admin chooses which providers users may pick and enters each provider's
   credentials in the UI (write-only, encrypted, `is_set`-style flags, same shape as the
   Bedrock work). Extend existing structures before inventing new ones.
6. Admin/super_admin only; token never shown or logged; fixed-sentence errors; 12 locales;
   light/dark parity; docs + CHANGELOG; files under ~300 lines.
7. Answers to the §10 questions (owner: "use all the best practice", 2026-10-15): the script and
   `/set` refuse non-catalog models; new routes are super_admin only with panels locked for
   plain admins; the key-reveal route is removed; model deletion is a follow-up after 0.6.0;
   multi-tenant mode needs per-organisation shared provider credentials.

---

## 1. What the code does today (verified), and what the issue gets wrong

### 1.1 Facts the design rests on

| Fact | Where |
|---|---|
| `VALID_LOCAL_WHISPER_MODELS` = tiny, tiny.en, base, base.en, small, small.en, medium, medium.en, large-v1, large-v2, large-v3, large-v3-turbo, nyrahealth/faster_CrisperWhisper | `backend/app/schemas/media.py:19-37` |
| It is used only as the per-file (upload/reprocess) validator and as part of the `/local-model/set` allow-list | `schemas/media.py:114,240`, `api/endpoints/asr_settings.py:1166-1176` |
| The provider catalog's `local.models` has a DIFFERENT, shorter list (8 entries: large-v3-turbo, large-v3, large-v2, medium, small, base, tiny, faster_CrisperWhisper) with VRAM text in `description` | `backend/app/services/asr/factory.py:95-192` |
| Discovery has a THIRD list (`_SHORT_NAME_TO_REPO`, incl. distil-*, `large`, `turbo`, `crisperwhisper`) | `backend/app/services/asr/model_discovery.py:21-47` |
| Discovery calls a repo "downloaded" when its `blobs/` dir is non-empty — a half-finished download (only `*.incomplete` blobs) counts as downloaded | `model_discovery.py:104-117` |
| `POST /asr-settings/local-model/set` (super_admin) stores `SystemSettings["asr.local_model"]`; accepts the reprocess list, every discovery short name/repo AND any string containing `/`; never checks the model is on disk | `asr_settings.py:1124-1197` |
| `POST /asr-settings/local-model/restart` (super_admin, 5/min) pings workers, keeps names containing `gpu`, counts `inspect().active()`, sends `control.broadcast("shutdown", destination=gpu_workers)` | `asr_settings.py:1200-1307` |
| If no worker name contains `gpu`, it broadcasts `shutdown` to EVERY worker (download, cpu, nlp, embedding, redaction, cloud-asr) | `asr_settings.py:1236-1259` |
| Remote `shutdown` raises `WorkerShutdown` (`celery/worker/control.py:588-591`, celery 5.6.3) and does NOT emit `worker_shutting_down` (only `celery/apps/worker.py:309`, the SIGTERM/SIGINT handler sends it). So the #809 cooperative abort flag (`core/worker_shutdown.py:62-70`, wired at `core/celery.py:1329-1341`) is NOT armed: running tasks run to completion, the consumer stops taking new ones, warm shutdown joins the pool, then the container exits 0 and `restart: always` (`docker-compose.yml:354`) brings it back. The docstring claim "finishes in-progress tasks" is therefore TRUE for this path. A SIGTERM path (`docker stop`, `./opentr.sh stop`) behaves differently (stands down at stage boundaries and requeues) |
| Unstarted prefetched messages are `acks_late` (transcription stages) and are restored to the queue when the worker's channel closes; `worker_prefetch_multiplier=1` | `core/celery.py:309`, `core/broker_orphans.py:5` |
| On restart, `preload_models` (`worker_ready`, MainProcess) reads the model (pin → DB → env), loads it, then `TranscriptionConfig.pin_model(...)`; a preload exception is swallowed at DEBUG and the worker starts unloaded | `core/celery.py:1090-1153`, `transcription/config.py:291-343` |
| Worker hostnames: `gpu-transcription@` (celery-worker), `gpu-transcribe@`, `gpu-diarize@`, `gpu-scaled@` (gpu-scale overlay) | `docker-compose.yml:390,959,1033`, `docker-compose.gpu-scale.yml:64` |
| `init_worker_process` runs inside a fork against a kill timer: no network, no DB, no locks | `core/celery.py:919-963`, `core/CLAUDE.md` |
| `celery-download-worker`: queue `download`, prefork, concurrency 5, max-tasks-per-child 10, mounts `${MODEL_CACHE_DIR}/huggingface:/home/appuser/.cache/huggingface` READ-WRITE | `docker-compose.yml:458-510` |
| Backend API container mounts the same HF dir read-write | `docker-compose.yml:240` |
| Image sets `HF_HOME=/home/appuser/.cache/huggingface`; `appuser` is uid 1000, gid from `groupadd -r` (999 today) | `backend/Dockerfile.prod:281-282,389` |
| Offline overlay sets `HF_HUB_OFFLINE=1` on every worker incl. the download worker, but NOT on `backend` | `docker-compose.offline.yml:39-70` |
| faster-whisper downloads with `snapshot_download(repo, allow_patterns=["config.json","preprocessor_config.json","model.bin","tokenizer.json","vocabulary.*"])` | venv `faster_whisper/utils.py:91-116` (faster_whisper 1.2.1) |
| huggingface_hub 0.36.2 writes every in-flight file to `blobs/<etag>.incomplete` (HTTP and xet paths), resumes HTTP downloads from it, then renames | venv `huggingface_hub/file_download.py:1176,1305,1673-1752` |
| `snapshot_download(tqdm_class=...)` counts FILES, not bytes ("not passed to each individual download") | venv `huggingface_hub/_snapshot_download.py:113-116` |
| A precedent for running a model provisioner as a SUBPROCESS with the token in the ENV (never argv) | `backend/app/transcription/native_provision.py:172-186` |
| #1245 interface: `app.services.huggingface_token.get_hf_token() -> str | None` (saved token > `HUGGINGFACE_TOKEN` env > None, 60 s cache, no request needed, must NOT be called from fork hooks); router `/admin/hf-token`, capability `engine.settings`, super_admin | worktree `agent-ac9861ca40ce3306c`: `backend/app/services/huggingface_token.py:106-117`, `backend/app/api/endpoints/admin_hf_token.py`, `backend/app/api/router.py:250-255` |
| Cloud ASR today: per-user `UserASRSettings` rows; `api_key`/`access_key_id` AES-encrypted; responses expose `has_api_key`/`has_access_key_id` only; a config can be `is_shared` (tenant-scoped via `owner_in_tenant`); users activate own or shared configs | `asr_settings.py:256-280,498-547,582-679`, `services/asr/CLAUDE.md` "API keys" |
| There is NO provider allow-list for users anywhere (`rg allowed_provider` = 0 hits) | — |
| `GET /asr-settings/config/{uuid}/api-key` returns the DECRYPTED key to the owner; the frontend defines `ASRSettingsApi.getApiKey` (`frontend/src/lib/api/asrSettings.ts:386`) but no component calls it | `asr_settings.py:562-579` |
| `asr_settings` router is gated by capability `asr.user_providers` | `backend/app/api/router.py:243-248` |
| `send_ws_event` direct call sites are AST-gated by an allow-list with reasons | `backend/tests/unit/test_ws_event_quarantine_discipline.py:56` |
| Bedrock pattern (#branch `feat/v060-llm-modal-bedrock`): credentials live inside the existing encrypted column, responses carry `has_aws_credentials`, PUT has `clear_aws_credentials`, no migration | worktree `agent-afa0fa79cb91283ce`: `backend/app/schemas/llm_settings.py:65-121,173,230-234` |
| Alembic head is `v439_infer_legacy_voiceprint_suggestion_source` | `backend/alembic/versions/` |

### 1.2 Where the issue (or its premises) is wrong — tell the owner

1. **"The list the download script supports today (`VALID_LOCAL_WHISPER_MODELS`)" — the script
   has no list.** `scripts/download-models.py:121-159` downloads whatever `WHISPER_MODEL` names
   through `whisperx.load_model` (any faster-whisper short name incl. `distil-*`, `turbo`, or
   any HF repo id). Adopting `VALID_LOCAL_WHISPER_MODELS` as the catalog NARROWS three
   surfaces: the script (an operator with `WHISPER_MODEL=distil-large-v3` will be refused),
   `/local-model/set` (today accepts any `org/repo`), and `test_asr_settings.py:1028-1042`
   (sets `distil-large-v3`). This plan applies the owner decision and lists the break.
   Already-configured deployments keep working: the model RESOLUTION path
   (`TranscriptionConfig._resolve_model_name`) is not touched, only what can be newly chosen.
2. **"Wait for in-flight jobs to finish" is already what `/restart` does** — but only on the
   happy path. Two real defects to fix in the same wave (fold-in rule):
   - the no-GPU-worker fallback broadcasts `shutdown` to every worker in the deployment
     (`asr_settings.py:1236-1259`) — it would kill an in-flight model download, yt-dlp jobs,
     redaction, embeddings. Replace with a 409 and no broadcast.
   - `/set` accepts a model that is not on disk; the worker then downloads it inside
     `preload_models` on the MainProcess thread or fails at DEBUG and starts unloaded.
3. **Discovery reports partial downloads as downloaded** (§1.1). With a Download button this
   becomes user-visible (Activate offered on a half-written model). Fix it.
4. **"Credentials write-only" is contradicted by `GET /config/{uuid}/api-key`.** It is an
   owner-only decrypting read with no UI caller. The owner decision requires write-only;
   this plan removes it (API surface removal — call it out in the CHANGELOG `Removed`).
5. **The download does not need the HF token for any catalog model** (all 13 repos are
   public). The token is passed when set (rate limits, a private `HF_ENDPOINT` mirror) and
   never required. The dependency on #1245 is about using the UI-stored token, not access.
6. **Two catalog figures look wrong**: `factory.py:185` says CrisperWhisper is "~3 GB VRAM"
   while it is a large-v3-sized CT2 model (expect ~10 GB at float16, like large-v3), and
   `large-v3-turbo` "~6 GB" vs large-v3 "~10 GB" are hand-typed, not measured. The catalog
   below carries them as hints and marks them for measurement (§3.1).

---

## 2. Design overview

```
Settings (super_admin)                    API (backend)                     download worker (queue "download")
  LocalModelLibrary  --GET-->  /admin/local-models        (catalog + state + disk)
        |           --POST-->  /admin/local-models/download {model_id}
        |                         disk check, lock (Redis SET NX), dispatch --> asr.download_local_model
        |                                                                       get_hf_token()  (#1245)
        |                                                                       HfApi.model_info -> total bytes, revision, blob names
        |                                                                       spawn: python -m app.services.local_models.runner
        |                                                                         (HF_TOKEN in ENV, snapshot_download)
        |  <--WS "local_model_download" ----- progress every 1 s (bytes on disk / total) ---|
        |           --POST-->  /admin/local-models/download/cancel  -> Redis cancel flag --> terminate child, drop *.incomplete
        |
  Activate (confirm: "the transcription worker restarts")
        |           --POST-->  /asr-settings/local-model/set     (catalog id, must be fully downloaded)
        |           --POST-->  /asr-settings/local-model/restart (warm shutdown of gpu workers; running jobs finish)
        |           --GET--->  /asr-settings/local-model/active  (polls loaded_models until the worker reports the new model)
```

Cloud ASR: two `SystemSettings` keys (provider allow-list for users; whether users may add
their own keys). Credentials are the EXISTING shared `UserASRSettings` configs an admin
creates and shares; a new admin panel shows, per provider, the toggle and whether a shared
credential exists. Enforcement goes through the pipeline plan (`services/pipeline_plan.py`),
so dispatch, set-active and the speaker-source PUT all refuse without new checks of their own.

### 2.1 Queue / worker choice, and why

- **`download` queue (`celery-download-worker`)**. It is network-bound by design (yt-dlp),
  mounts the HF cache read-write, has no GPU, and is not the API process (a multi-GB
  download inside a request or in the API's threadpool would stall the API).
- Not `cpu`/`utility` (`celery-cpu-worker`): it also mounts the cache, but it owns
  preprocessing; a 3 GB download occupying a slot competes with transcription preprocessing.
- Not `gpu`: the GPU worker is the one being swapped; it is a threads pool whose slots are the
  engine slots (#1072).
- The download worker is prefork with concurrency 5, so one model download occupies one of
  five slots; media downloads keep flowing.
- `init_worker_process` is NOT touched. Everything (token, DB, Redis, network) happens inside
  the task body. `get_hf_token()` is called in the task, never in a fork hook (the rule in
  `services/huggingface_token.py:13`).

### 2.2 Why a subprocess does the actual download

`huggingface_hub.snapshot_download` is a blocking call with no cancel hook. Options rejected:
- `celery revoke(terminate=True)` SIGTERMs the whole prefork child; the task cannot record
  `cancelled`, clean up `.incomplete` files or release the lock.
- Running it in a thread and "abandoning" it on cancel leaves it downloading for the life of
  the prefork child (`max-tasks-per-child=10`).

So the task spawns `python -m app.services.local_models.runner --model <id> --revision <sha>`
(same pattern as `native_provision.py:172-186`: token only in the child ENV, never argv) and
supervises it. Cancel = `Popen.terminate()` (SIGTERM to the task's OWN child, a CPU-only,
network-only Python process — no CUDA), `wait(15)`, then `Popen.kill()` only if it ignored
SIGTERM. This is code managing its own child, not the operator-level "never kill processes"
rule in root `CLAUDE.md`, which is about agents signalling CUDA processes; say so in the
module docstring so a reviewer does not conflate them.

Pipe-deadlock rule: the child runs with `HF_HUB_DISABLE_PROGRESS_BARS=1`, its stdout carries
exactly one JSON line, and its stderr goes to a temp FILE (not `PIPE`). tqdm writing to an
unread stderr pipe would fill the 64 KB buffer and block the child forever.

### 2.3 Progress (exact bytes, not a guess)

Before spawning, the task calls
`HfApi(endpoint=constants.ENDPOINT).model_info(repo_id, files_metadata=True, token=token, timeout=30)`
and keeps the siblings matching `ALLOW_PATTERNS` (fnmatch). From them:
- `revision = info.sha` (passed to the child so the bytes counted are the bytes fetched);
- `expected = {blob_name: size}` where `blob_name = sibling.lfs.sha256 if sibling.lfs else sibling.blob_id`
  (huggingface_hub names a blob after its etag: sha256 for LFS files, git blob id otherwise);
- `bytes_total = sum(size)`.

Every second the supervisor computes, under `<HF_HUB_CACHE>/models--<org>--<name>/blobs/`:
`bytes_done = Σ size(blobs/<name>) if it exists else size(blobs/<name>.incomplete) if it exists else 0`
— capped at `bytes_total`. Blobs that were already complete count from the first tick, so a
resumed or partially cached download shows its true position. ⚠️ Verify the etag naming once
against a real download on the review stack (§9 step 3) before relying on it; if a blob name
does not match, progress silently reads 0 for that file. The integration test (§8.2) pins it
against the mock server's own etags.

### 2.4 State, lock and cancel in Redis (no migration)

All keys live under a namespace passed to a small store class so tests never touch the live
keys. Production namespace: `asr:local_model_download`.

| Key | Value | TTL |
|---|---|---|
| `<ns>:lock` | JSON `{model_id, task_id, user_id, started_at}` | 900 s at dispatch (the task may queue behind media downloads), then refreshed to 120 s every tick |
| `<ns>:state` | JSON `{model_id, status, bytes_done, bytes_total, percent, error_code, task_id, user_id, updated_at}`; status ∈ `queued·downloading·verifying·completed·failed·cancelled` | 24 h after a terminal status; refreshed while active |
| `<ns>:cancel:<task_id>` | `"1"` | 1 h |

- Acquire with `SET <ns>:lock <json> NX EX 900`. Release with a Lua compare-and-delete on
  `task_id` (never a bare `DEL`: a stale task must not release a newer lock).
- A dead worker frees the lock by TTL within 120 s of its last tick. A task that never
  started (queued) can be cancelled: the API sets the cancel flag, revokes the Celery id
  (`celery_app.control.revoke(task_id)` WITHOUT `terminate`) and marks state `cancelled`; if
  the task starts anyway it sees the flag first and exits.
- `get_redis()` (`core/redis.py:43`) is the client; failures in the task's progress write are
  logged and do not fail the download, but a failed lock ACQUIRE in the API is a 503 (fail
  closed: two concurrent multi-GB downloads into one cache is the thing the lock prevents).

### 2.5 Cache dir, permissions, and what workers see

- One resolver: `huggingface_hub.constants.HF_HUB_CACHE` (honours `HF_HUB_CACHE` → `HF_HOME/hub`,
  i.e. `/home/appuser/.cache/huggingface/hub` in every container). Replace
  `model_discovery._get_hf_cache_dir()` with a call to the new `cache.hub_cache_dir()` so
  discovery and the downloader can never look at different directories. The current
  `MODEL_CACHE_DIR` branch (`model_discovery.py:76-79`) is a HOST path that leaks into
  containers via `env_file: .env`; it only fires when neither HF var is set, which never
  happens in the image — drop it.
- Host side: `${MODEL_CACHE_DIR}/huggingface`, owned by `$CONTAINER_UID_GID` (default
  `1000:999`, `scripts/fix-model-permissions.sh`, issue #580). The download worker runs as
  `appuser` (uid 1000) and creates files with umask 022 → world-readable, which the GPU worker
  (same uid) and the API need.
- Writable check: `os.access(hub_dir, os.W_OK)` in the API (same mount) before dispatch and
  again in the task; failure → `cache_not_writable` (fixed sentence; the admin-detail string
  names `./scripts/fix-model-permissions.sh`).
- Workers do NOT need to "see" a new model until activation. The API sees it immediately:
  `GET /admin/local-models` scans the shared cache on every call. A model counts as
  downloaded only when it is COMPLETE (§4 WP1, `is_complete()`): `refs/main` exists, the
  snapshot it names holds `config.json` and `model.bin` (symlinks resolving to blobs), and no
  `*.incomplete` file exists for that repo. The GPU worker keeps its pinned model
  (`TranscriptionConfig.pin_model`) until restarted; the restart's `preload_models` loads the
  new one from the cache with no network.

### 2.6 Disk check

- API (fast, no network): `free = shutil.disk_usage(hub_dir).free`;
  `need = entry.approx_bytes - present_bytes(entry) + 1 GiB headroom` (present = sizes of
  that repo's complete blobs). Refuse with 409 `insufficient_disk_space` and structured
  `required_bytes`, `free_bytes` fields (the message itself is a fixed sentence; the UI
  formats the numbers).
- Task (exact): after `model_info`, `need = bytes_total - bytes_done_at_start + 512 MiB`;
  same code on failure.
- `ENOSPC` during the download → `disk_full`; the task deletes that repo's `*.incomplete`
  files (they can never complete) and leaves complete blobs.

### 2.7 Failure and resume

| Child outcome | `error_code` | `.incomplete` files |
|---|---|---|
| exit 0, all expected blobs complete, snapshot has the files | `completed` | none left |
| 401/403, `GatedRepoError` | `hf_auth_failed` | kept |
| `RepositoryNotFoundError`, `RevisionNotFoundError`, 404 | `hf_repo_not_found` | kept |
| connection error, timeout, 5xx | `hf_unreachable` | kept (next attempt resumes over HTTP) |
| `ENOSPC` | `disk_full` | deleted |
| `EACCES`/`EPERM` | `cache_not_writable` | n/a |
| `HF_HUB_OFFLINE=1` in the worker | `downloads_disabled_offline` (no child spawned) | n/a |
| soft time limit (4 h) | `download_timed_out` | kept |
| anything else | `download_failed` | kept |
| user cancel | status `cancelled` | deleted (only this repo's expected `*.incomplete`) |

The child prints `{"ok": true}` or `{"ok": false, "code": "<one of the above>"}` on stdout
and logs only `type(exc).__name__` and the HTTP status. The parent logs the stderr file's
last 20 lines at WARNING after replacing the token value with `***` (defence in depth; the
token is never in a URL). The UI renders `error_code` through i18n; the raw text never
reaches a client.

Retry = press Download again (same lock rules). No automatic retry (`acks_late=False`, no
`autoretry_for`): a silent second multi-GB attempt is not something to do unasked.

---

## 3. The catalog

### 3.1 New module `backend/app/core/local_whisper_catalog.py` (stdlib only)

`core/` because `schemas/media.py` must import it without pulling in
`app.services.asr.__init__` (which imports the provider factory), and because the script
imports it inside the backend image.

```python
@dataclass(frozen=True)
class LocalWhisperModel:
    id: str                    # what is stored in asr.local_model and sent by the UI
    repo_id: str               # Hugging Face repo, CTranslate2 build
    display_name: str
    approx_bytes: int          # sum of ALLOW_PATTERNS files; measured, see table
    vram_hint_gb: float        # float16 inference, hint only
    language_support: Literal["multilingual", "english_only", "english_optimized"]
    supports_translation: bool
    is_default: bool = False

ALLOW_PATTERNS: tuple[str, ...] = ("config.json", "preprocessor_config.json", "model.bin",
                                   "tokenizer.json", "vocabulary.*")   # == faster_whisper.utils.download_model
LOCAL_WHISPER_MODELS: tuple[LocalWhisperModel, ...] = (...)
LOCAL_WHISPER_MODEL_IDS: frozenset[str] = frozenset(m.id for m in LOCAL_WHISPER_MODELS)
ALIASES: dict[str, str] = {"crisperwhisper": "nyrahealth/faster_CrisperWhisper",
                           "nyrahealth/CrisperWhisper": "nyrahealth/faster_CrisperWhisper"}

def get(model_id: str) -> LocalWhisperModel | None: ...
def canonical_id(name: str) -> str | None:  # applies ALIASES, then membership; None if unknown
```

Entries (repo ids are faster_whisper's own `_MODELS` map; sizes are ESTIMATES the
implementer must replace with `sum(sibling.size for allowed siblings)` measured once with
`HfApi().model_info(repo, files_metadata=True)` on a host with network, recorded in a comment
with the date; VRAM hints come from the existing catalog text and must be marked as hints):

| id | repo_id | approx size (estimate) | VRAM hint | language | translation |
|---|---|---|---|---|---|
| tiny | Systran/faster-whisper-tiny | ~75 MB | 1 | multilingual | yes |
| tiny.en | Systran/faster-whisper-tiny.en | ~75 MB | 1 | english_only | no |
| base | Systran/faster-whisper-base | ~145 MB | 1 | multilingual | yes |
| base.en | Systran/faster-whisper-base.en | ~145 MB | 1 | english_only | no |
| small | Systran/faster-whisper-small | ~485 MB | 2 | multilingual | yes |
| small.en | Systran/faster-whisper-small.en | ~485 MB | 2 | english_only | no |
| medium | Systran/faster-whisper-medium | ~1.5 GB | 5 | multilingual | yes |
| medium.en | Systran/faster-whisper-medium.en | ~1.5 GB | 5 | english_only | no |
| large-v1 | Systran/faster-whisper-large-v1 | ~3.1 GB | 10 | multilingual | yes |
| large-v2 | Systran/faster-whisper-large-v2 | ~3.1 GB | 10 | multilingual | yes |
| large-v3 | Systran/faster-whisper-large-v3 | ~3.1 GB | 10 | multilingual | yes |
| large-v3-turbo (default) | mobiuslabsgmbh/faster-whisper-large-v3-turbo | ~1.6 GB | 6 | english_optimized | no |
| nyrahealth/faster_CrisperWhisper | nyrahealth/faster_CrisperWhisper | ~3.1 GB (verify) | 10 (verify; old catalog says 3) | english_only | no |

`supports_translation`/`language_support` values must equal the current
`factory.py:112-190` values for the 8 overlapping ids (large-v3-turbo is `english_optimized`,
no translation; CrisperWhisper `english_only`). The `.en` and `large-v1` rows are new to the
provider catalog.

### 3.2 Consumers rewired to the catalog (single source)

- `schemas/media.py:19-37`: `VALID_LOCAL_WHISPER_MODELS = LOCAL_WHISPER_MODEL_IDS`
  (keep the name; it is imported by tests and `asr_settings.py`).
- `services/asr/factory.py`: build `ASR_PROVIDER_CATALOG["local"]["models"]` from the
  catalog (id, display_name, description = f"~{vram_hint_gb:g} GB VRAM" style fixed text,
  `supports_diarization: True`, `supports_translation`, `language_support`, `is_default`,
  `price_per_min_batch: 0`, plus new `approx_bytes`, `vram_hint_gb`). Delete the hand-typed
  8 dicts. Re-run `scripts/generate-pipeline-docs.py --write` (it reads the ASR catalog) and
  `scripts/generate-openapi.py --write` if any schema changes.
- `services/asr/model_discovery.py`: `_SHORT_NAME_TO_REPO` keeps extra aliases only for
  DISPLAY of legacy-present models (`distil-*`, `large`, `turbo`); add a docstring line that
  it is not an allow-list. `discover_local_models()` uses `is_complete()`.
- `scripts/download-models.py`: whisper group calls the catalog/downloader (WP2).

---

## 4. Work packages (each independently mergeable, in this order)

Branch per WP off `feat/v0.6.0-frontend-ux` (or one branch `feat/1246-local-models` with one
commit per WP if the owner prefers; follow root `CLAUDE.md`: worktree → branch → PR, merge
commits). Conventional commit scope `asr`. Run both pre-commit tiers
(`scripts/safe-precommit.sh run --all-files` and `... --hook-stage pre-push`) and
`cd backend && python3 ../scripts/audit-tests.py tests` (0 open findings) before each commit.

```
WP1 catalog + discovery + /set hardening  ─┬─> WP2 downloader + script ──> WP3 task + admin API + WS ──> WP5 frontend library + e2e
                                           │                                   ^ needs #1245 merged
                                           ├─> WP4 restart fix + loaded-model report ───────────────────┘
                                           └─> WP6 cloud policy backend ──> WP7 cloud policy frontend
WP8 docs + CHANGELOG + CLAUDE.md: a slice in each WP, final pass last
```

### WP1 — One catalog; discovery that does not lie; `/set` restricted to downloaded catalog models

Files:
- NEW `backend/app/core/local_whisper_catalog.py` (§3.1).
- NEW `backend/app/services/local_models/__init__.py` (empty docstring only; do NOT import submodules).
- NEW `backend/app/services/local_models/cache.py`:
  - `hub_cache_dir() -> Path` (`huggingface_hub.constants.HF_HUB_CACHE`).
  - `repo_dir(repo_id: str) -> Path` (`models--` + `repo_id.replace("/", "--")`).
  - `is_complete(repo_id: str) -> bool` (refs/main → snapshots/<sha>/config.json and model.bin exist and resolve; no `blobs/*.incomplete`).
  - `has_partial(repo_id: str) -> bool` (any `blobs/*.incomplete`).
  - `present_bytes(repo_id: str) -> int` (sum of complete blob sizes).
  - `cache_writable() -> bool`; `free_bytes() -> int`.
  - `remove_incomplete(repo_id: str, blob_names: Iterable[str] | None) -> int` (only `*.incomplete`; never a complete blob, never another repo; returns count).
- MODIFY `backend/app/schemas/media.py:19-37` (derive from catalog).
- MODIFY `backend/app/services/asr/factory.py:95-192` (derive `local.models`).
- MODIFY `backend/app/services/asr/model_discovery.py` (use `cache.hub_cache_dir`, `cache.is_complete`; drop the `MODEL_CACHE_DIR` branch; keep `resolve_loadable_model_name` unchanged).
- MODIFY `backend/app/api/endpoints/asr_settings.py:1124-1197` (`set_active_local_model`):
  - `model_id = local_whisper_catalog.canonical_id(body.model_name.strip())`; `None` →
    400 `{"code": "unknown_local_model", "message": "This model is not in the list of supported local models."}`.
  - not `cache.is_complete(entry.repo_id)` → 409 `{"code": "local_model_not_downloaded", "message": "Download this model before activating it."}`.
  - store `entry.id` (unchanged storage contract; CrisperWhisper stays the repo id).
  - keep `refuse_on_lite`, rate limit, super_admin.
  - audit: `ADMIN_SETTINGS_CHANGE` `{"setting": "local_whisper_model", "action": "set", "model": entry.id}` (same helper shape as `admin_hf_token._audit`).
- MODIFY `backend/tests/test_asr_settings.py:875-1042`: keep the CrisperWhisper alias tests;
  change `distil-large-v3` (`:1033`) to a catalog id; mark the model downloaded by pointing
  `HF_HUB_CACHE` at a `tmp_path` fixture that builds a complete fake repo layout
  (helper `tests/fixtures/hf_cache.py::make_complete_repo(root, repo_id)` and
  `make_partial_repo(...)`).
- The frontend `ASRSettings.svelte` select (`:436-447`) shows discovered models; until WP5
  replaces it, it may offer a non-catalog legacy model that `/set` now refuses with a fixed
  message — acceptable for one WP; WP5 removes the select.

Tests (write first, watch red against `8b27ab8d` via `git archive HEAD` per root CLAUDE.md):
- `backend/tests/unit/test_local_whisper_catalog.py`: ids == `VALID_LOCAL_WHISPER_MODELS`;
  every entry has a non-empty repo id, `approx_bytes > 0`, `vram_hint_gb > 0`; every short id
  maps to the same repo as `faster_whisper.utils._MODELS` (`pytest.importorskip("faster_whisper")`);
  `ALLOW_PATTERNS` equals the list in `faster_whisper.utils.download_model` (read via
  `inspect.getsource`, so a faster-whisper bump that changes the patterns fails here);
  provider catalog `local.models` ids == catalog ids, in catalog order; `canonical_id` table.
- `backend/tests/unit/test_local_model_cache.py`: `is_complete` false for blobs-only,
  for an `.incomplete` present, for refs without snapshot; true for the full layout;
  `remove_incomplete` deletes only `.incomplete` of that repo (assert a neighbour repo and a
  complete blob survive). Red-first: `discover_local_models()` currently reports the
  blobs-only repo as downloaded.
- `backend/tests/api/test_local_model_set_guard.py`: unknown id → 400 code; arbitrary
  `org/repo` → 400; not downloaded → 409 code; downloaded → 200 and stored; alias remap
  still stored as repo id; plain admin → 403; lite → 409 (extend
  `tests/api/test_lite_settings_refusals.py` only if its table needs the new shape).

### WP2 — Downloader module; the script becomes a thin caller

Files:
- NEW `backend/app/services/local_models/downloader.py` (<300 lines):
  - `class DownloadPlan(NamedTuple): repo_id, revision, expected: dict[str, int], bytes_total: int`.
  - `resolve_plan(entry: LocalWhisperModel, token: str | None, *, timeout: float = 30.0) -> DownloadPlan`
    (HfApi `model_info(files_metadata=True)`, fnmatch against `ALLOW_PATTERNS`, blob names per §2.3).
  - `bytes_on_disk(repo_id: str, expected: dict[str, int]) -> int` (§2.3).
  - `download_blocking(entry, token, revision=None) -> str` (`snapshot_download(entry.repo_id, revision=revision, allow_patterns=list(ALLOW_PATTERNS), token=token)`; returns the snapshot path). Used by the runner child AND the script.
  - `classify_error(exc: BaseException) -> str` (§2.7 codes; HTTP status from `exc.response.status_code` when present; `OSError.errno` for ENOSPC/EACCES).
  - `ERROR_MESSAGES: dict[str, str]` fixed English sentences, one per code (the backend's
    "fixed sentence" for logs/API; the UI uses its own i18n by code).
- NEW `backend/app/services/local_models/runner.py`: `main(argv)`; args `--model <id>`
  (validated with `canonical_id`; unknown → exit 2), `--revision <sha>`; reads token from
  `os.environ.get("HF_TOKEN")`; calls `download_blocking`; prints one JSON line; exit 0/1.
  Entry point `if __name__ == "__main__": raise SystemExit(main(sys.argv[1:]))`.
  It must import nothing from `app.db`, `app.core.celery`, torch, or faster_whisper.
- MODIFY `scripts/download-models.py`:
  - replace `download_whisperx_models()` (`:121-159`) with a function that:
    `from app.core.local_whisper_catalog import canonical_id, get` and
    `from app.services.local_models.downloader import download_blocking, classify_error, ERROR_MESSAGES`;
    resolves `WHISPER_MODEL` (default `large-v3-turbo`) through `canonical_id`; unknown →
    print the catalog ids and return `{'whisperx': {'status': 'failed', 'error': 'unknown_local_model'}}`;
    else `download_blocking(entry, os.environ.get('HUGGINGFACE_TOKEN'))`.
    On `ImportError` (an older downloader image): print one fixed instruction
    ("This downloader image predates the model catalog; use the image matching this script's
    release (OT_IMAGE_TAG)") and fail the group. Do NOT keep the whisperx path as a fallback
    (two paths doing one job).
  - delete `resolve_whisper_device` (`:50-68`) — its only caller goes away. Keep
    `guard_mps_empty_cache` (the pyannote group still needs it).
  - keep the `DOWNLOAD_GROUPS` key `whisperx` (callers pass `--only whisperx`); its
    docstring now says "local Whisper model from the catalog".
- MODIFY `scripts/download-models.sh:315-328`: replace the CrisperWhisper remap with a
  comment that the Python side applies the catalog aliases (one implementation). Keep
  passing `WHISPER_MODEL`. GPU flags remain only for the pyannote group.
- MODIFY `backend/tests/unit/test_download_models_cpu_only.py`: delete
  `test_resolve_whisper_device` and `test_whisperx_step_uses_the_resolver` (the code is
  gone); add `test_whisperx_group_delegates_to_the_catalog_downloader` (AST/source check
  that the group calls `download_blocking` and contains no `whisperx.load_model`, mirroring
  `test_download_models_diar_native_delegation.py`).

Tests:
- NEW `backend/tests/fixtures/mock_hf_hub.py`: a stdlib `ThreadingHTTPServer` on
  `127.0.0.1:0` implementing the three calls huggingface_hub 0.36 makes:
  `GET /api/models/<repo>/revision/<rev>` (and `/api/models/<repo>` with
  `?blobs=true`/files_metadata) returning `sha`, `siblings[{rfilename, size, blobId, lfs?}]`;
  `HEAD`/`GET /<repo>/resolve/<rev>/<file>` with `ETag`, `X-Repo-Commit`, `Content-Length`,
  `Range` support, and a configurable per-request delay and failure mode
  (`ok`, `401`, `404`, `500`, `slow`). Files are generated bytes (e.g. 3 files, 2 MiB
  `model.bin`) so tests are fast and need no network. The test sets `HF_ENDPOINT` to it,
  `HF_HUB_CACHE`/`HF_HOME` to `tmp_path`, `HF_HUB_DISABLE_XET=1` (the mock serves plain
  HTTP), and resets `huggingface_hub.constants` by reloading or by passing `endpoint=`
  explicitly (`HfApi(endpoint=...)`, and `snapshot_download(endpoint=...)` — add an
  optional `endpoint: str | None = None` parameter to `resolve_plan`/`download_blocking`
  that defaults to `None` = library default; the runner reads `HF_ENDPOINT` from env).
  Register it in the root conftest `pytest_plugins` like `fixtures.mock_asr`.
- NEW `backend/tests/unit/test_local_model_downloader.py`: `resolve_plan` totals and blob
  names match the mock's siblings; `download_blocking` produces a layout `is_complete`
  accepts; `bytes_on_disk` counts `.incomplete` mid-download (use the `slow` mode, sample
  while running in a thread); `classify_error` for 401/404/500/ENOSPC/EACCES; the runner
  as a REAL subprocess (`sys.executable -m app.services.local_models.runner`) exits 0 and
  prints `{"ok": true}`; with `HF_TOKEN` set, assert the mock received
  `Authorization: Bearer <token>` and that the token does not appear in the child's argv
  (`/proc` is not needed: assert on the argv list the supervisor builds).
- No test downloads from the real Hub: an autouse fixture in that module asserts
  `HF_ENDPOINT` points at `127.0.0.1` before any download call.

### WP3 — Celery task, Redis state/lock, admin API, WebSocket (needs #1245 merged)

Files:
- NEW `backend/app/services/local_models/state.py` (<300 lines):
  `class DownloadStore` with `__init__(redis, namespace="asr:local_model_download")`;
  `try_acquire(model_id, task_id, user_id, ttl=900) -> bool`;
  `refresh(task_id, ttl=120) -> bool`; `release(task_id) -> bool` (Lua compare-and-delete);
  `current_lock() -> dict | None`; `write_state(**fields)`; `read_state() -> dict | None`;
  `request_cancel(task_id)`; `cancel_requested(task_id) -> bool`.
- NEW `backend/app/services/local_models/supervisor.py` (<300 lines):
  `run_download(entry, *, task_id, user_id, store, token, notify) -> str` (returns final
  status). Steps: offline check (`hf_offline_requested()` → `downloads_disabled_offline`);
  writable check; `resolve_plan`; exact disk check; spawn the runner with
  `env = {**os.environ, "HF_TOKEN": token or "", "HF_HUB_DISABLE_PROGRESS_BARS": "1"}`
  (pop `HF_TOKEN` when empty; pop a blank `HF_ENDPOINT` exactly like
  `native_provision._subprocess_env`), `stdout=PIPE`, `stderr=<tempfile>`;
  loop every 1.0 s: cancel? → terminate/kill + `remove_incomplete` + `cancelled`; else
  `bytes_on_disk`, `store.refresh`, `store.write_state(status="downloading", ...)`,
  `notify(...)` at most once per second or per 1% change. On exit: parse the JSON line;
  `verifying` → `is_complete(repo)`; `completed` or `failed` with code. Always
  `store.release(task_id)` in `finally`. `notify` is injected so the supervisor has no
  Celery or WS import (testable).
- NEW `backend/app/tasks/local_model_download.py`:
  `@celery_app.task(name="asr.download_local_model", bind=True, soft_time_limit=14400, time_limit=15300)`
  `def download_local_model_task(self, model_id: str, user_id: int) -> dict`:
  `entry = get(model_id)` (unknown → failed `unknown_local_model`); cancel flag set before
  start → `cancelled`, release lock, return; `from app.services.huggingface_token import get_hf_token`;
  `token = get_hf_token()`; `run_download(...)` with `notify=lambda data: send_ws_event(user_id, NOTIFICATION_TYPE_LOCAL_MODEL_DOWNLOAD, data)`;
  `SoftTimeLimitExceeded` → supervisor terminates the child, `download_timed_out`.
  Never log `token`; the payload carries no file identity.
- MODIFY `backend/app/core/celery.py`: add `"app.tasks.local_model_download"` to `include`
  (`:217-274`) and `"asr.download_local_model": {"queue": CeleryQueues.DOWNLOAD}` to
  `task_routes` (`:441+`, next to the `download.*` routes). `_validate_task_routes`
  (`:1260`) must pass.
- MODIFY `backend/app/core/constants.py` near `:533`:
  `NOTIFICATION_TYPE_LOCAL_MODEL_DOWNLOAD = "local_model_download"`.
- MODIFY `backend/tests/unit/test_ws_event_quarantine_discipline.py:56`: add
  `"tasks/local_model_download.py::download_local_model_task": (1, "super_admin model-download progress for the initiating admin; carries a catalog model id and byte counts, never a MediaFile identity")`.
  If the `send_ws_event` call ends up inside the lambda/helper, key the entry to the
  function that contains the call (the test reports the exact key on failure).
- NEW `backend/app/schemas/local_models.py` (Pydantic v2):
  - `LocalModelDownloadState`: `model_id: str`, `status: Literal[...]`, `bytes_done: int`,
    `bytes_total: int | None`, `percent: float | None`, `error_code: str | None`,
    `updated_at: datetime`.
  - `LocalModelEntry`: `id, display_name, repo_id, approx_bytes, vram_hint_gb,
    language_support, supports_translation, is_default, downloaded: bool, partial: bool,
    present_bytes: int, is_active: bool` (server computes `is_active` against
    `deployment_model_name()` from `utils/whisper_model_choice.py:18-20`, so the SPA does not).
  - `LocalModelCatalogResponse`: `models: list[LocalModelEntry]`, `download: LocalModelDownloadState | None`,
    `free_bytes: int`, `cache_writable: bool`, `active_model: str`.
  - `LocalModelDownloadRequest`: `model_id: str = Field(max_length=100)`.
- NEW `backend/app/api/endpoints/local_models.py` (<300 lines), router mounted at
  `/admin/local-models`, capability `engine.settings`, ALL routes `get_current_active_superuser`
  (deployment change, §6), each calling `refuse_on_lite("local_asr_unavailable")` first:
  - `GET ""` → `LocalModelCatalogResponse` (filesystem + Redis reads only, no network).
  - `POST "/download"` (`@limiter.limit("5/minute")`) → 202 `LocalModelDownloadState`:
    `canonical_id` (400 `unknown_local_model`); already complete → 409
    `local_model_already_downloaded`; `cache_writable()` false → 409 `cache_not_writable`;
    disk check → 409 `insufficient_disk_space` + `required_bytes`, `free_bytes`;
    generate `task_id = str(uuid4())`; `store.try_acquire` false → 409
    `model_download_in_progress` (+ `model_id` of the running one); write state `queued`;
    `download_local_model_task.apply_async(args=[entry.id, user.id], task_id=task_id)`; on
    dispatch failure release the lock and 503 `model_download_unavailable`; audit
    `{"setting": "local_whisper_model", "action": "download_started", "model": entry.id}`.
  - `POST "/download/cancel"` → 200 state: no lock → 404 `no_model_download`; set cancel
    flag; if state is `queued` also `celery_app.control.revoke(task_id)` (no terminate) and
    write `cancelled` + release; audit `download_cancelled`.
  - Error bodies are `detail={"code": ..., "message": ERROR_MESSAGES[code]}` (the shape the
    pipeline refusals already use, `asr_settings.py:212-222`).
- MODIFY `backend/app/api/router.py`: `include_router_with_consistency(local_models.router, prefix="/admin/local-models", tags=["admin"], capability="engine.settings")`.
- MODIFY `backend/tests/unit/test_route_privilege_tiers.py`: add `"/api/admin/local-models"`
  to `SUPER_ADMIN_PREFIXES`.
- Run `backend/venv/bin/python scripts/generate-openapi.py --write`.

Tests:
- `backend/tests/unit/test_local_model_download_state.py` against a throwaway
  `redis:7-alpine` (reuse `tests/docker_ops.py::run_container` exactly as
  `unit/test_migration_progress_service.py` does; label `ot-test-artifact=1`; remove in
  `finally`): second acquire refused; release by a stale task id is a no-op; TTL refresh;
  cancel flag round trip; namespace isolation (two namespaces never collide).
- `backend/tests/unit/test_local_model_supervisor.py` (mock HF server + throwaway Redis +
  `tmp_path` cache, real subprocess): completed path emits monotonic `bytes_done` ending at
  `bytes_total` and a `completed` notify; cancel mid-download (mock `slow` mode) → child
  gone (`proc.poll() is not None`), status `cancelled`, no `.incomplete` left, complete
  blobs of a neighbour repo untouched, lock released; 401 → `hf_auth_failed` and lock
  released; `HF_HUB_OFFLINE=1` → `downloads_disabled_offline` and no child spawned;
  insufficient disk (monkeypatch `cache.free_bytes`) → `insufficient_disk_space`, no child.
- `backend/tests/api/test_local_models_endpoints.py` (task's `apply_async` is already
  patched by the root conftest; patch `get_redis` to the throwaway Redis client):
  super_admin 200 / admin 403 / user 403 on all three routes; lite 409; unknown id 400 code;
  second POST while locked 409 `model_download_in_progress`; already-downloaded 409;
  cancel without lock 404; cancel while queued revokes (assert the revoke call AND the
  state is `cancelled`, not only the mock); disk-full 409 with numeric fields; the response
  never contains the token (set a saved token via `huggingface_token.save_token` and grep
  the JSON).
- Route-has-caller: WP5 adds the literal frontend call sites; until then add the three
  routes to `NO_FRONTEND_CALLER` in `test_route_has_a_caller.py` with reason
  "#1246 UI lands in WP5", and remove those entries in WP5.

### WP4 — Activation: restart that cannot take down unrelated workers; the worker reports what it loaded

Files:
- MODIFY `backend/app/api/endpoints/asr_settings.py:1200-1307` (`restart_gpu_worker`):
  - Delete the broadcast-to-all branch. No GPU worker answered the ping → 409
    `{"code": "gpu_worker_not_found", "message": "No transcription worker answered. It may already be restarting."}`
    and NO broadcast.
  - Restrict the destination to workers that load the Whisper model: hostnames starting
    `gpu-transcription@`, `gpu-transcribe@`, `gpu-scaled@` (constant
    `WHISPER_WORKER_PREFIXES` in `core/constants.py`). `gpu-diarize@` loads no Whisper; do
    not restart it.
  - Keep `active_tasks`; add `restart_requested_at` (UTC ISO) to the response.
  - The fixed English `message` stays for API users; the UI renders by fields.
- MODIFY `backend/app/core/celery.py` `preload_models` (`:1141-1149`, right after
  `pin_model`): call new `app.services.local_models.worker_report.report_loaded_model(hostname, model_name)`,
  which does `get_redis().set(f"asr:worker_model:{hostname}", json.dumps({"model": ..., "ready_at": ...}), ex=7*86400)`
  inside `try/except Exception` logged at WARNING. This is `worker_ready` (MainProcess),
  not the fork hook; the call is one bounded Redis SET (the client has
  `socket_timeout`, `core/redis.py`). Hostname from `kwargs["sender"].hostname`.
  Also write it from the CPU-lightweight preload? No: that worker serves tiny/base only.
- MODIFY `GET /asr-settings/local-model/active` (`asr_settings.py:1059-1117`): add
  `loaded_models: list[{worker: str, model: str, ready_at: str}]` read with one `SCAN`
  over `asr:worker_model:*` (bounded; at most a handful of workers) — a Redis failure
  yields `[]`, never a 500. Keep every existing field.
- NEW `backend/app/services/local_models/worker_report.py` (`report_loaded_model`,
  `read_loaded_models`).

Activation semantics to document in the module docstring and the UI copy (verified §1.1):
`/set` writes the DB only; running workers are pinned and unaffected. `/restart` sends a
remote warm shutdown to the Whisper workers: each stops consuming, finishes the jobs it is
running (no cooperative abort on this path), returns unstarted prefetched messages to the
queue, exits, is restarted by Docker, loads the new model in `preload_models`, reports it.
Jobs submitted meanwhile queue and run on the new model. The page polls
`GET /asr-settings/local-model/active` every 5 s until every Whisper worker reports
`model == target` with `ready_at > restart_requested_at`, or 20 minutes pass (then a fixed
"has not reported back yet" notice, not an error).

Tests:
- `backend/tests/api/test_local_model_restart.py` (patch `celery_app.control` with a
  recording double — this is the boundary; assert on what was SENT):
  no gpu workers → 409 code and `broadcast` NOT called (red against `8b27ab8d`, which
  broadcasts to all); workers `gpu-transcription@a`, `gpu-diarize@a`, `media-downloader@a`
  → destination is exactly `[gpu-transcription@a]`; `gpu-scaled@b` included.
- `backend/tests/unit/test_worker_report.py`: `preload_models` source calls
  `report_loaded_model` after `pin_model` (AST order check); `read_loaded_models` against
  the throwaway Redis; Redis down → `[]`.
- Extend `backend/tests/test_asr_settings.py::test_get_active_local_model` to assert
  `loaded_models` is a list.

### WP5 — Frontend: model library, download progress, activation (+ i18n, vitest, e2e)

Files:
- NEW `frontend/src/lib/api/localModels.ts` (literal paths for the caller scanner):
  `getLocalModelCatalog()` → `axiosInstance.get('/admin/local-models')`;
  `startLocalModelDownload(modelId)` → `axiosInstance.post('/admin/local-models/download', { model_id })`;
  `cancelLocalModelDownload()` → `axiosInstance.post('/admin/local-models/download/cancel')`.
  Types mirror `schemas/local_models.py`.
- MODIFY `frontend/src/lib/api/asrSettings.ts:476-519`: `getActiveLocalModel` type gains
  `loaded_models`; `restartGpuWorker` type gains `restart_requested_at`. Delete
  `getApiKey` (`:386-389`) in WP6.
- NEW `frontend/src/lib/settings/localModelView.ts` (pure, no Svelte): given an entry, the
  current download state and `isSuperAdmin`, return `{ action: 'download'|'cancel'|'activate'|'active'|'none', labelKey, progress: number|null, errorKey: string|null }`;
  `errorKeyFor(code)` maps every backend code to `settings.localModels.error.<code>` with
  `settings.localModels.error.download_failed` as the fallback (never server text).
  `formatBytes` uses `Intl.NumberFormat` with the active locale.
- NEW `frontend/src/components/settings/localModels/LocalModelLibrary.svelte`
  (coordinator, <300 lines): props `isSuperAdmin`, `isAdmin`; when `!isSuperAdmin` renders
  locked (disabled, `title={$t('settings.nav.requiresSuperAdmin')}`) and makes NO API calls
  (matches `components/settings/CLAUDE.md` "Lock rule"); loads the catalog; listens to
  `window` event `local-model-download` (from websocket.ts) and refreshes state from its
  `detail`; while state is `queued|downloading|verifying` ALSO polls the catalog every 3 s
  (survives reload and a dropped socket); toasts on `completed`/`failed`/`cancelled`;
  free-space line; handles Activate: opens the dialog, then calls `setLocalModel` then
  `restartGpuWorker`, then polls `getActiveLocalModel` per WP4 and shows
  "Restarting the transcription worker…" → "Running <model>".
- NEW `frontend/src/components/settings/localModels/LocalModelRow.svelte` (presentational):
  name, size, VRAM hint, `CapabilityBadges` (catalog `local` entry via `modelCapability`,
  per `components/settings/CLAUDE.md`), translation/English-only pills, the action button,
  a `ui/ProgressBar` with percent and "x of y", inline error text by key.
- NEW `frontend/src/components/settings/localModels/ActivateModelDialog.svelte`: wraps
  `ConfirmationModal` (`components/ConfirmationModal.svelte:6-12`) with title
  `settings.localModels.activate.title` and message
  `settings.localModels.activate.message` ("Activating {{model}} restarts the transcription
  worker. Jobs that are running finish first; new jobs wait and then use the new model.").
- MODIFY `frontend/src/components/settings/ASRSettings.svelte`: replace the admin block
  `:422-477` (select, Save Model, Restart GPU Worker, downloaded count) with
  `<LocalModelLibrary {isSuperAdmin} {isAdmin} on:activated={loadData} />`; delete
  `selectedLocalModel`, `modelChangeInProgress`, `restartInProgress`,
  `handleSetLocalModel`, `handleRestartGpuWorker`, `localModelChanged` and their CSS
  (`:92-94,329-357` and the `.model-control-row` styles). Keep the read-only model card
  (`:394-420`) visible to everyone. No second path to set/restart remains.
- MODIFY `frontend/src/stores/websocket.ts` near `:377`: `else if (data.type === 'local_model_download') { window.dispatchEvent(new CustomEvent('local-model-download', { detail: data.data })); return; }`.
- i18n: add keys under `settings.localModels.*` (title, intro, size, vram, download,
  cancel, activate, active, downloading `{{percent}}`, ofBytes `{{done}}`/`{{total}}`,
  freeSpace `{{free}}`, restarting, running `{{model}}`, notReportedYet, activate.title,
  activate.message `{{model}}`, activate.confirm, toast.completed/failed/cancelled, and
  `error.<code>` for every code in §2.7 plus `unknown_local_model`,
  `local_model_not_downloaded`, `local_model_already_downloaded`,
  `model_download_in_progress`, `no_model_download`, `insufficient_disk_space`
  `{{required}}`/`{{free}}`, `cache_not_writable`, `model_download_unavailable`,
  `gpu_worker_not_found`) in ALL 12 locale files with real translations; `ar` checked RTL
  (logical CSS properties only). Remove now-unused keys `settings.asrProvider.saveModel`,
  `restartGpuWorker`, `restarting`, `downloadedLocally`, `localModelSet`,
  `setLocalModelFailed`, `restartWorkerFailed`, `notDownloaded`, `modelSource`,
  `sourceDatabase`, `sourceEnvironment` only if `rg` shows no other user. Run
  `cd frontend && npm run check:i18n`.
- Light/dark: tokens only (`--primary-on-surface`, `--text-secondary`, no literal colours,
  no purple — `noPurple.test.ts`); dark selector `:global([data-theme='dark'])` if needed.
- Remove the WP3 `NO_FRONTEND_CALLER` entries.

Tests:
- `frontend/src/lib/settings/localModelView.test.ts`: every status × downloaded/active
  combination → action; every backend code in a list copied from
  `backend/app/services/local_models/downloader.py::ERROR_MESSAGES` keys maps to an
  existing en.json key (read the JSON; a code without copy fails).
- `frontend/src/components/settings/localModels/LocalModelLibrary.test.ts` (mock
  `$lib/api/localModels` and `asrSettings`): locked for admin and no API call;
  Download click calls `startLocalModelDownload('large-v3')`; a `local-model-download`
  window event with 40% renders the progress and Cancel; Cancel calls the API; a 409
  `insufficient_disk_space` renders the translated sentence with formatted numbers, not
  the server message; Activate opens the dialog whose text contains the restart sentence,
  and confirm calls `setLocalModel` THEN `restartGpuWorker` (assert call order);
  polling stops after `completed`.
- `frontend/src/stores/websocket.test.ts`: the new type dispatches the window event and
  creates no notification.
- `frontend/src/lib/api/localModels.test.ts`: the three literal paths and bodies.
- E2E `backend/tests/e2e/test_local_models.py` (marker `settings`): logs in as the dev
  super_admin, opens Settings → Transcription → Provider, and uses `page.route` (precedent:
  `tests/e2e/test_file_detail_transcript.py`) to fulfil `**/api/admin/local-models`,
  `**/download`, `**/download/cancel`, `**/asr-settings/local-model/set`,
  `**/asr-settings/local-model/restart` and `**/local-model/active` with scripted
  responses: queued → downloading 40% → completed (driven by the 3 s poll), Cancel visible
  while downloading, Activate dialog text contains the restart sentence, confirm issues
  set then restart (record request order in the route handlers), then "Running large-v3".
  Nothing reaches the backend for these routes, so no dev data, cache or worker changes
  (root CLAUDE.md e2e rule) and no network. Screenshot light + dark for the review.
  The real worker path (real subprocess, real HF protocol, real cache layout, real cancel)
  is covered by WP3's supervisor test against the mock HF server; the live stack is
  covered by the manual verification in §9.

### WP6 — Cloud ASR provider policy (backend)

Existing structures reused: credentials = an admin's `UserASRSettings` row with
`is_shared=True` (already encrypted, `has_api_key`/`has_access_key_id`, write-only on PUT
where blank means keep, `asr_settings.py:738-770`). New: what users may pick.

Files:
- MODIFY `backend/app/core/constants.py`: `ASR_CLOUD_PROVIDERS_FOR_USERS_KEY = "asr.cloud_providers_for_users"`
  (JSON list; ABSENT means all providers allowed = today's behaviour) and
  `ASR_USERS_OWN_KEYS_KEY = "asr.users_may_add_own_keys"` (default `True`).
- NEW `backend/app/services/asr_provider_policy.py` (<200 lines):
  `CLOUD_PROVIDER_IDS` (catalog ids minus `local`); `load_policy(db) -> ProviderPolicy(allowed: frozenset[str] | None, own_keys: bool)`;
  `save_policy(db, allowed: list[str] | None, own_keys: bool)` (validates ids against the
  catalog; uses `system_settings_service.set_setting`, which busts the settings cache);
  `user_may_use(user, provider, policy) -> bool` (`local` always True; admin/super_admin
  always True — they configure shared credentials; else `policy.allowed is None or provider in policy.allowed`);
  `user_may_store_own_key(user, policy) -> bool`.
- MODIFY `backend/app/services/pipeline_plan.py`: `PlanInputs` gains
  `asr_provider_allowed: bool = True` and `diarizer_provider_allowed: bool = True`;
  `resolve_pipeline_plan` adds error `asr_provider_not_allowed` (step `transcription`) when
  the engine is cloud and not allowed, and error `diarizer_provider_not_allowed` (step
  `diarization`) when the source is `asr_config` or `pyannote` and not allowed;
  `ISSUE_MESSAGES` gets the two fixed sentences ("Your administrator has not enabled this
  transcription provider." / "...this speaker detection provider."). Pure function stays
  pure.
- MODIFY `backend/app/services/pipeline_plan_inputs.py:75-162`: load the policy once and
  the user's role; set `asr_provider_allowed` from the ACTIVE CONFIG's provider when a
  config row is active (`cfg is not None`; the env `ASR_PROVIDER` default is the
  deployment's own choice and is never blocked), and `diarizer_provider_allowed` from the
  diarizer config's provider or `"pyannote"`.
  Consequences that come for free (verify, do not re-implement): dispatch refuses
  (`tasks/transcription/dispatch.py:146-156`), set-active/clear-active refuse a switch that
  introduces a transcription-step error (`services/asr_switch_plan`), the speaker-source PUT
  refuses a NEW diarization-step error, `GET /user-settings/pipeline` shows it.
- MODIFY `backend/app/api/endpoints/asr_settings.py`:
  - `get_providers` (`:373-398`): for a plain user, drop cloud providers not allowed; for
    admins return all with `allowed_for_users: bool` on each provider.
  - `create_asr_config` (`:582`) and `update_asr_config` (`:682`, when provider changes or
    a key is set): plain user + provider not allowed → 409 `asr_provider_not_allowed`;
    plain user + `own_keys` false + any cloud provider → 409 `own_asr_keys_disabled`
    (they can still activate shared configs).
  - DELETE `get_asr_config_api_key` (`:562-579`) — write-only credentials (§1.2 item 4).
- NEW `backend/app/api/endpoints/asr_provider_policy.py`: router at `/admin/asr-providers`,
  capability `asr.user_providers`, super_admin:
  - `GET ""` → `{providers: [{id, display_name, allowed_for_users, shared_credentials: [{config_uuid, name, has_api_key, has_access_key_id, owner_name}]}], users_may_add_own_keys}`
    (shared configs owned by admins in the caller's tenant, `owner_in_tenant`; never a
    key value).
  - `PUT ""` body `{allowed_for_users: list[str] | null, users_may_add_own_keys: bool}` →
    same shape; unknown id → 422; audit `{"setting": "asr_provider_policy", "action": "update"}`
    with the id list (ids are not secrets).
- MODIFY `backend/app/api/router.py` (mount) and `test_route_privilege_tiers.py`
  (`"/api/admin/asr-providers"` in `SUPER_ADMIN_PREFIXES`).
- Frontend part of the pipeline issue (`PIPELINE_ISSUE_KEYS` in
  `frontend/src/lib/settings/pipelineKeys.ts`, `settings.pipeline.issue.<code>` in 12
  locales, `FIX_SECTIONS` in `pipelineIssues.ts`) lands in WP7 together with the panel, but
  `keyMaps.test.ts` will fail until then if it enumerates backend codes — if it does, ship
  the two keys + translations in WP6.
- `generate-openapi.py --write`.

Tests:
- `backend/tests/unit/test_pipeline_plan.py`: new rows — cloud engine not allowed → error
  on transcription; `asr_config`/`pyannote` source not allowed → error on diarization;
  allowed → unchanged plan (control); env-default cloud provider never blocked.
- `backend/tests/api/test_asr_provider_policy.py`: tiers (super 200, admin 403, user 403);
  absent policy = all allowed (non-regression); after PUT `["deepgram"]`: user's
  `/providers` lists local + deepgram only, admin still sees all with flags; user creating
  an `assemblyai` config → 409 code; user `set-active` on an admin-shared `assemblyai`
  config → 409 `asr_provider_not_allowed` (through the plan); upload dispatch with an
  active disallowed config → file ERROR with that code, not transcribed (assert no
  `transcription.*` task was sent); `own_keys=false` → user create 409, activating a shared
  config still 200; `GET /asr-settings/config/{uuid}/api-key` → 404/405.
- `rg -n "api-key" backend/tests` after the removal; update `test_route_has_a_caller.py`
  and any test that called the reveal route.

### WP7 — Cloud ASR provider policy (frontend)

Files:
- NEW `frontend/src/lib/api/asrProviderPolicy.ts`: literal `'/admin/asr-providers'` GET/PUT.
- NEW `frontend/src/components/settings/asr/CloudProviderPolicy.svelte` (<300 lines),
  rendered in the Transcription → Provider tab (`tx-provider`) under the provider list,
  super_admin (locked for admin, absent for users; never an API call while locked):
  per provider a toggle "Users may choose this provider" and the credential status
  ("Shared credentials: <name>, key set" / "No shared credentials"); a button "Add shared
  credentials" opens the existing `ASRConfigModal` (`components/settings/ASRConfigModal.svelte:24-31`)
  with a new optional prop `presetProvider` and `presetShared = true`; a toggle "Users may
  add their own keys". Save = PUT. Key inputs stay in `ASRConfigModal`, which already never
  pre-fills a key.
- MODIFY `frontend/src/components/settings/ASRConfigModal.svelte`: add `presetProvider`,
  `presetShared` props (create mode only).
- MODIFY `frontend/src/components/settings/ASRSettings.svelte`: mount the panel for
  admins; render a 409 `asr_provider_not_allowed`/`own_asr_keys_disabled` through
  `pipelineFailureKey` / new keys (never the server message).
- MODIFY `frontend/src/lib/api/asrSettings.ts`: delete `getApiKey`; add
  `allowed_for_users?: boolean` to `ASRProviderInfo`.
- MODIFY `frontend/src/lib/settings/pipelineKeys.ts` + `pipelineIssues.ts` (`FIX_SECTIONS`:
  both new codes → `tx-provider`).
- i18n: `settings.asrPolicy.*` and `settings.pipeline.issue.asr_provider_not_allowed`,
  `...diarizer_provider_not_allowed`, `settings.asrProvider.error.own_asr_keys_disabled`
  in 12 locales.

Tests: `CloudProviderPolicy.test.ts` (locked for admin with no call; toggling and saving
sends the exact body; "Add shared credentials" opens the modal with the provider preset and
sharing on); `pipelineKeys`/`keyMaps` tests cover the new codes; `ASRSettings.badges.test.ts`
still green.

### WP8 — Docs, CHANGELOG, CLAUDE.md

- `docs-site/docs/features/transcription.md`: new section "Local models: download and
  activate" (catalog table generated from or copied with a test against the catalog,
  download, cancel, disk space, activation restarts the transcription worker, offline
  installs use `./opentranscribe.sh download-models`). Screenshot via the
  `docs-screenshots` skill.
- `docs-site/docs/installation/huggingface-setup.md`: the token is optional for Whisper
  models; it is used when set (#1245 owns the page; add one paragraph).
- `docs-site/docs/installation/offline-installation.md`: `WHISPER_MODEL` must be a catalog
  id now; list them.
- `docs-site/docs/features/` ASR provider page or `transcription.md`: the provider policy
  and shared credentials; the removed api-key reveal route.
- `CHANGELOG.md` `[Unreleased]`: Added (download/activate local models; provider policy),
  Changed (`/local-model/set` accepts catalog ids only and requires a downloaded model;
  `/restart` no longer broadcasts to every worker; script `WHISPER_MODEL` must be a catalog
  id), Fixed (discovery reported partial downloads), Removed
  (`GET /asr-settings/config/{uuid}/api-key`), Security (credentials write-only; provider
  egress policy enforced at dispatch). Keep a Changelog format; the existing section
  structure is at `CHANGELOG.md:8-1336`.
- `docs/design/v060_downstream_changes.md`: add the public-safe API changes (new routes,
  removed route, new plan issue codes).
- `backend/app/services/asr/CLAUDE.md`: "Local models" section (catalog in
  `core/local_whisper_catalog.py`, downloader, completeness rule, activation semantics);
  remove "sole exception is the owner-only GET /config/{uuid}/api-key".
- `backend/app/api/CLAUDE.md`: the two new admin routers; restart no longer broadcasts.
- `scripts/CLAUDE.md` "Models" bullet: whisper group uses the catalog downloader.
- `backend/app/tasks/CLAUDE.md`: `asr.download_local_model` on `download`, subprocess
  supervision, why not revoke(terminate).

---

## 5. DB / migration

No Alembic revision. Everything persistent is a `SystemSettings` key
(`backend/app/models/system_settings.py:20-39`, `value: Text`):
`asr.local_model` (exists), `asr.cloud_providers_for_users` (JSON list, absent = all),
`asr.users_may_add_own_keys` ("true"/"false", absent = true). Download state, lock and
worker reports are Redis (ephemeral by nature; losing them on a Redis restart only drops
the progress display — the cache on disk is the source of truth for "downloaded"). If a
later change does need a table, the next revision is `v440_<name>` with
`down_revision = "v439_infer_legacy_voiceprint_suggestion_source"`; follow
`backend/alembic/CLAUDE.md` and never autogenerate inside the hot-reload container.

## 6. Security review

| Concern | Control |
|---|---|
| Who can download/activate | super_admin only (`get_current_active_superuser`), matching the existing local-model routes and the privilege rule in `test_route_privilege_tiers.py` (deployment change). Admins see the panels locked; users never see them. `/admin/*` prefix also gets the support-grant refusal. |
| Arbitrary repo / SSRF / path traversal | Only catalog ids are accepted (`canonical_id`); the repo id and filesystem paths are derived from the catalog, never from input. `HF_ENDPOINT` comes only from the operator's environment. `remove_incomplete` deletes only `*.incomplete` files inside the derived repo dir. |
| Token exposure | Read with `get_hf_token()` inside the task; passed to the child via ENV only (not argv, not Redis, not the Celery message, not WS, not logs); child stderr scrubbed before logging; API responses never include it (test greps the JSON). |
| Error disclosure | Fixed sentences by code (`ERROR_MESSAGES`); exception text only in server logs as the class name; `scripts/audit-error-disclosure.py` must pass. |
| Resource abuse | One download at a time (Redis NX lock); `5/minute` on POST download; disk check before and during; 4 h soft limit. |
| Worker disruption | `/restart` targets only Whisper workers and never broadcasts to all; remote shutdown is warm (running jobs finish). |
| Cache poisoning | Downloads go through `huggingface_hub` against a pinned `revision` from `model_info`; a model is "downloaded" only when the snapshot is complete. |
| Credentials (cloud) | Stored through the existing AES-256-GCM path; returned only as `has_*` flags; the decrypting reveal route is removed; shared configs are tenant-scoped (`owner_in_tenant`). |
| Data egress (cloud) | A provider the admin disabled is refused at plan time, which blocks dispatch, set-active and the speaker-source PUT; admins/super_admins and the env default are exempt by design (they own the deployment's choice). |
| Audit | `ADMIN_SETTINGS_CHANGE` for download start/cancel, set, policy update; details carry ids only. |
| Lite | All local-model routes 409 `local_asr_unavailable`. |

## 7. Concurrency guard summary

Deployment-wide single download: Redis `SET NX` lock acquired in the API before dispatch
(409 when held), refreshed by the running task, released by compare-and-delete, expiring
by TTL if the worker dies. Activation while a download runs is allowed (different worker,
different model); activating the downloading model is refused because it is not complete.
Two admins pressing Download simultaneously: exactly one gets 202 (test it with two
threads against the throwaway Redis).

## 8. Test plan summary

1. **Red first**: every new behaviour test is written before the code and run against a
   `git archive HEAD` tree (root CLAUDE.md recipe) to watch it fail: partial-download
   discovery, `/set` refusing non-catalog and not-downloaded, `/restart` broadcasting to
   all, provider policy (no such thing today).
2. **Backend unit/API** (`./scripts/run-backend-tests.sh` or `pytest -v` in
   `backend/venv`): WP1–WP4, WP6 test files above. Real Redis = throwaway container
   (`tests/docker_ops.py`), never the dev stack's keys. Real HF protocol = the mock HF
   server fixture on `127.0.0.1`; an autouse guard refuses any other `HF_ENDPOINT`. Real
   subprocess for the runner.
3. **Route-has-a-caller**: literal `'/admin/local-models'`, `'/admin/local-models/download'`,
   `'/admin/local-models/download/cancel'`, `'/admin/asr-providers'` strings in
   `frontend/src/lib/api/*.ts`; `scripts/audit-route-coverage.py --prefix /api/admin/local-models`
   reports them covered by the API tests (evidence is the URL argument of a real client call).
4. **Frontend**: `cd frontend && npm run test`, `npm run test:audit`, `npm run check`,
   `npm run check:i18n`, `npm run build`.
5. **E2E**: `./scripts/e2e/run-e2e.sh -m settings` (or just the new file) against the
   review stack ports (`E2E_BACKEND_URL=http://localhost:5374`, frontend 5373); route-mocked,
   so it changes nothing.
6. **Gate**: `./scripts/run-dev-tests.sh --fast` on the branch before the PR; both
   pre-commit tiers; `python3 scripts/audit-tests.py backend/tests` = 0 open findings;
   `generate-openapi.py --check`, `generate-pipeline-docs.py --check`.
7. **How no test downloads for real**: catalog ids are real repo ids, so the ONLY thing
   between a test and huggingface.co is `HF_ENDPOINT`; the fixture sets it and the guard
   asserts it. The API tests never run the task (root conftest patches `apply_async`). The
   e2e is route-mocked. Only §9 step 3 touches the real Hub, by hand, on the review stack.

## 9. Verification on the review stack (otfresh-v060, FE 5373 / BE 5374)

Check `./opentr.sh fresh-list` first; the deployment name is `v060` with offset 200.
Never touch GPU 0. Check `nvidia-smi` before step 5.

1. Rebuild/restart the review stack from the branch checkout:
   `./opentr.sh start dev --fresh v060 --port-offset 200 --with-mock-llm --with-mock-asr`
   (Celery workers do not hot-reload: restarting via `start` recreates them). Confirm
   `./opentr.sh status --fresh v060`.
2. Log in at `http://10.10.10.20:5373` as `admin@example.com` / `password` (super_admin).
   Settings → Transcription → Provider: the model library lists 13 models with sizes and
   VRAM hints; the active one is marked; free space shown. Light, dark and `ar`.
3. Download `tiny` (~75 MB, real Hub): progress moves, finishes, row shows Downloaded.
   On the host: `ls ${MODEL_CACHE_DIR}/huggingface/hub/models--Systran--faster-whisper-tiny/{refs,snapshots,blobs}`,
   no `*.incomplete`, files owned `1000:999`. This also verifies the §2.3 etag naming.
4. Download `medium` and press Cancel at ~20%: status cancelled, no `*.incomplete` left
   for that repo, lock gone (a new Download is accepted). Press Download twice quickly in
   two tabs: one 409.
5. Activate `tiny` (cheap on VRAM): the dialog says the worker restarts; with a file
   transcribing on the review stack, the running job finishes; the page shows Restarting
   then "Running tiny"; `./opentr.sh logs celery-worker` (fresh) shows the warm shutdown,
   the preload of `tiny`, and no `cooperative abort`. Re-activate the previous model
   afterwards (owner's review stack must end as it started).
6. Provider policy: as super_admin allow only Gladia (mock ASR); as a plain user
   (create a throwaway user with an `e2e-`-registered prefix, delete it after) the provider
   list shows Local + Gladia; the shared Gladia config can be activated; a disallowed shared
   config cannot. Restore the policy to "all".
7. Offline behaviour (optional): with `HF_HUB_OFFLINE=1` on the download worker the
   download fails with the offline sentence.
8. Delete anything created in steps 3-6 that the owner did not ask to keep (a downloaded
   `medium` is not created because it was cancelled; `tiny` is harmless to keep — ask).

## 10. Open risks and questions for the owner

1. **Catalog narrows existing behaviour** (§1.2 item 1): `WHISPER_MODEL=distil-*` in the
   script and `/set` with an `org/repo` will be refused. A deployment already running a
   non-catalog model keeps running it; it just cannot be re-selected in the UI. Confirm.
2. **Etag/blob naming for progress** (§2.3) is based on huggingface_hub 0.36 internals;
   verify on the review stack (§9 step 3). A hub version bump can break progress silently;
   the mock-server test pins our expectation, not the library's.
3. **Xet-backed repos**: if a catalog repo is served via Xet, partial-file resume may not
   apply (it re-downloads). Progress still works (same `.incomplete` path). Low impact.
4. **Script vs image skew**: a newer `download-models.py` run against an older downloader
   image fails the whisper group with an instruction (no fallback). Installer-pinned
   installs ship both at one tag, so this only affects a mixed git checkout.
5. **Offline overlay**: the backend has no `HF_HUB_OFFLINE`, so the API cannot pre-refuse;
   the task fails fast with `downloads_disabled_offline`. Optionally add
   `HF_HUB_OFFLINE=1` to `backend` in `docker-compose.offline.yml` and refuse in the API —
   check nothing else in the API needs the Hub first.
6. **Multi-tenant shared credentials**: a super_admin's shared config is visible only in
   the tenant they are in (`owner_in_tenant`). In multi-tenant mode each org needs its own
   shared config. Single-tenant (the business deployment this targets) is unaffected.
7. **Removing `GET /asr-settings/config/{uuid}/api-key`** breaks any external script that
   read keys back. Required by the write-only decision; CHANGELOG `Removed`.
8. **Admin read access**: this plan makes the two admin panels super_admin end to end
   (admins see them locked), following the privilege rule. If the owner wants plain admins
   to READ status, split GET to `get_current_admin_user` and remove the prefix from
   `SUPER_ADMIN_PREFIXES` in favour of per-route tiers.
9. **VRAM hints are not measured** (`CrisperWhisper ~3 GB` is almost certainly wrong).
   Measure on GPU 1 or idle GPU 2 with the existing VRAM profiling (`ENABLE_VRAM_PROFILING`)
   in a follow-up; the UI labels them as estimates.
10. **No model deletion** in scope: downloading several large models fills the disk; the
    library shows sizes and free space but cannot remove a model. Suggest a follow-up
    (`DELETE /admin/local-models/{id}`, refusing the active model).
11. **Restart scope with gpu-split**: `gpu-diarize@` is excluded from the restart (it loads
    no Whisper). If a future topology puts Whisper there, update `WHISPER_WORKER_PREFIXES`.
12. **#1245 merge order**: WP3 imports `app.services.huggingface_token`; it must land after
    #1245. WP1, WP2, WP4, WP6 do not depend on it.
