# backend/app/services/asr — pluggable ASR providers

## Purpose

Ten interchangeable speech-to-text backends behind one interface: `local` (GPU faster-whisper) plus
deepgram, assemblyai, openai, google, azure, aws, speechmatics, gladia, pyannote.ai. The sibling
`../diarization/` uses the same skeleton but has its own traps — see `../diarization/CLAUDE.md`.

## Key files

- `base.py` — the `ASRProvider` ABC plus two shared helpers every provider must use:
  `_normalize_speaker_label` (maps `S1` / `A` / `"0"` / `spk_0` / `speaker_0` / `Guest-1` → `SPEAKER_XX`,
  each with a **different base index** — read its docstring before adding a format) and
  `_sanitize_error` (scrubs keys before they reach logs or the UI).
- `types.py` — normalized DTOs: `ASRConfig` in, `ASRResult`/`ASRSegment`/`ASRWord` out. Plain
  dataclasses, not Pydantic.
- `factory.py` (843 lines) — `ASR_PROVIDER_CATALOG` (the admin UI's whole provider/model/price table,
  incl. per-model `supports_translation` and `status: tested|experimental`) + `ASRProviderFactory`.
- `local_provider.py` — a **stub**: `transcribe()` raises `NotImplementedError`. Local transcription
  runs through `app/transcription/`; this class exists for `validate_connection()` and so
  `provider_name == "local"` can be the routing signal.
- `model_discovery.py` — `resolve_loadable_model_name()` remaps `nyrahealth/CrisperWhisper` (PyTorch
  `model.safetensors`) → `nyrahealth/faster_CrisperWhisper` (CT2); CTranslate2 cannot load the former.
  `discover_local_models()` only surfaces repos present in its short-name map.

## Adding a provider

New `<vendor>_provider.py` subclassing `ASRProvider`, then three edits **all inside `factory.py`**:
an entry in `ASR_PROVIDER_CATALOG` (this alone drives the UI), an `if provider == "<name>"` branch in
**both** `_from_env()` and `create_from_config()`, and the env-var name in `_KEY_REQUIRED` if it needs
a key. Finally add the module to `ASR_PROVIDER_MODULES` in `tests/unit/test_provider_sdk_compat.py` —
providers lazy-import their SDK *inside* methods, so that import smoke test is the only guard against
SDK drift. **There is no decorator/registry**; dispatch is a literal if/elif chain deliberately
confined to the factory. Call sites branch only on `provider_name != "local"`, never on a vendor name.

## Selection & failure

- `create_for_user(user_id, db)`: `UserSetting["active_asr_config_id"]` → `UserASRSettings` row (own
  **or `is_shared`**) → `ASR_PROVIDER` env → `LocalASRProvider`. **Cloud ASR config is per-user**; the
  local model is admin-pinned globally via the `SystemSettings` key **`asr.local_model`**
  (`transcription/config.py:_resolve_model_name` — worker-pinned → DB → `WHISPER_MODEL` → `large-v3-turbo`).
- `tasks/transcription/dispatch.py:_resolve_gpu_queue` routes non-local providers to the **`cloud-asr`**
  queue (service `celery-cloud-asr-worker`, concurrency 16) so network-bound jobs never block the GPU.
- **Failure is asymmetric on purpose** (`tasks/transcription/core.py:2076-2103`): errors *constructing*
  the provider fall back to local; errors from `transcribe()` propagate and the file is marked FAILED
  rather than silently re-running on GPU. Providers wrap failures in `RuntimeError`, which is **not** in
  the task's `autoretry_for=(ConnectionError, TimeoutError)` — a cloud ASR failure is not retried.

## API keys

AES-256-GCM via `utils/encryption.encrypt_api_key` (PBKDF2-SHA256, 600k iters, `v3:` prefix; legacy
Fernet auto-detected on read). Responses expose only `has_api_key` / `has_access_key_id`, never a value
or mask — sole exception is the owner-only `GET /config/{uuid}/api-key` (`api/endpoints/asr_settings.py:477`).
On PUT an empty/null `api_key` means **keep the existing key** (how the UI edits other fields);
changing `provider` **clears** it.

## Gotchas

- **`create_for_user` never forwards `access_key_id` to `create_from_config`** (`factory.py:526-548`),
  though the endpoint does. An AWS config's stored access-key-id is therefore ignored at job time and
  boto3 falls back to `AWS_ACCESS_KEY_ID`/IAM — "Test connection" can pass while the real job runs on
  different credentials.
- Per-vendor: **aws** needs two creds and round-trips through an S3 bucket it may auto-create, wants
  BCP-47 (`en-US`) codes, and its custom vocabulary must pre-exist in AWS. **azure** needs a region, and
  diarization requires `ConversationTranscriber` — `SpeechRecognizer` silently returns no speakers.
  **google** takes a service-account JSON only (applied by mutating `GOOGLE_APPLICATION_CREDENTIALS`)
  and its `speaker_tag` is 1-indexed, converted *before* `_normalize_speaker_label`. **openai** has a
  hard 25 MB limit, no diarization at all, and translation only on `whisper-1`. **assemblyai** must send
  `speech_models` (a list); UI model names are not API ids. **deepgram** v6 reads the whole file into
  memory and its `validate_connection` needs a management-scoped key. **speechmatics** uses the async
  `speechmatics-batch` client via `asyncio.run()` — the legacy `speechmatics-python` SDK silently drops
  speaker labels. **pyannote.ai**'s poll timeout is only **300 s** (vs 1800–7200 s elsewhere), so long
  files fail there first. **azure** and **google** `validate_connection()` make **no network call** — a
  bad credential still "validates".
