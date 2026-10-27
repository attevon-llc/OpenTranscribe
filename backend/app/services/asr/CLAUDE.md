# backend/app/services/asr — pluggable ASR providers

## Purpose

Ten interchangeable speech-to-text backends behind one interface: `local` (GPU faster-whisper) plus
deepgram, assemblyai, openai, google, azure, aws, speechmatics, gladia, pyannote.ai. The sibling
`../diarization/` uses the same skeleton but has its own traps — see `../diarization/CLAUDE.md`.

## Key files

- `base.py` — the `ASRProvider` ABC plus two **module-level** helpers every provider must use:
  `normalize_speaker_label` (maps `S1` / `A` / `"0"` / `spk_0` / `speaker_0` / `Guest-1` → `SPEAKER_XX`,
  each with a **different base index** — read its docstring before adding a format; the
  `SPEAKER_`-prefixed branch re-pads, so `SPEAKER_1` → `SPEAKER_01`) and `sanitize_provider_error`
  (scrubs keys before they reach logs or the UI). The `_normalize_speaker_label` / `_sanitize_error`
  methods are thin delegates, and `../diarization/base.py` delegates to the same two functions —
  they are module-level precisely so neither hierarchy needs an instance of the other's ABC (#299).
- `types.py` — normalized DTOs: `ASRConfig` in, `ASRResult`/`ASRSegment`/`ASRWord` out. Plain
  dataclasses, not Pydantic.
- `factory.py` — `ASR_PROVIDER_CATALOG` (the admin UI's whole provider/model/price table,
  incl. per-model `supports_translation` and `status: tested|experimental`) + `ASRProviderFactory`.
  Every provider entry (and a model entry where it differs) also carries the two capabilities
  the pipeline plan (`services/pipeline_plan.py`, #1224) is built on, each **pinned to the
  provider module's code, never to vendor docs**:
  - `speaker_count_hints` — which of min/max/num the module actually sends.
    `tests/unit/test_asr_catalog_speaker_hints.py` derives the set from the module source.
    AssemblyAI sends `speakers_expected` for a fixed count, otherwise the range as
    `speaker_options` (`SpeakerOptions` in the installed SDK, #1226). Speechmatics and Gladia
    send none: their documented parameters are not wired because nothing in this repo verifies
    them (no typed SDK field, no recorded response).
  - `timestamp_granularity` — `word` | `segment` | `none`.
    `tests/unit/test_asr_catalog_timestamp_granularity.py` feeds each parser a fixture response
    and fails when the parsed result cannot honour the declared value. OpenAI is `none` at
    provider level (`gpt-4o-transcribe`/`-mini` are asked for `json`, which has no times) with
    `whisper-1` and `-diarize` overriding to `segment`; Azure is `segment` until a live
    response proves `NBest[0].Words` is filled (word times are never requested).
  `get_model_capabilities` returns both plus `supports_diarization` (model wins over provider,
  so OpenAI's `-diarize` model is the only OpenAI entry that returns speakers); that dict is what
  `/asr-settings/status.active_model_capabilities` carries (typed by `ASRModelCapabilities`). A
  new provider needs both fields, or `get_model_capabilities` fails closed to "no speakers, no
  timing" and the plan refuses speaker detection for it.
- `local_provider.py` — a **stub**: `transcribe()` raises `NotImplementedError`. Local transcription
  runs through `app/transcription/`; this class exists for `validate_connection()` and so
  `provider_name == "local"` can be the routing signal.
- `model_discovery.py` — `resolve_loadable_model_name()` remaps `nyrahealth/CrisperWhisper` (PyTorch
  `model.safetensors`) → `nyrahealth/faster_CrisperWhisper` (CT2); CTranslate2 cannot load the former.
  `discover_local_models()` only surfaces repos present in its short-name map **and complete**
  (`services/local_models/cache.is_complete`: `refs/main` -> snapshot with `config.json` and
  `model.bin`, no `*.incomplete` blob). The short-name map is not an allow-list.
- `core/local_whisper_catalog.py` (#1246) is the ONE list of local Whisper models: 13 ids with
  repo, measured size, VRAM hint, language/translation flags. `local.models` in the provider
  catalog, `schemas/media.VALID_LOCAL_WHISPER_MODELS`, `POST /asr-settings/local-model/set` and
  `scripts/download-models.py` all read it. `/local-model/set` accepts only a catalog id (or an
  alias such as `crisperwhisper`) whose repo is complete in the hub cache: 400
  `unknown_local_model` / 409 `local_model_not_downloaded`. A model already configured outside
  the catalog keeps loading (resolution is unchanged); it just cannot be newly selected.

## Adding a provider

New `<vendor>_provider.py` subclassing `ASRProvider`, then three edits **all inside `factory.py`**:
an entry in `ASR_PROVIDER_CATALOG` (this alone drives the UI), an `if provider == "<name>"` branch in
**both** `_from_env()` and `create_from_config()`, and the env-var name in `_KEY_REQUIRED` if it needs
a key. Finally add the module to `ASR_PROVIDER_MODULES` in `tests/unit/test_provider_sdk_compat.py` —
providers lazy-import their SDK *inside* methods, so that import smoke test is the only guard against
SDK drift. **There is no decorator/registry**; dispatch is a literal if/elif chain deliberately
confined to the factory. Call sites branch only on `provider_name != "local"`, never on a vendor name.

**Any provider that returns speakers can also be the speaker detector for another
transcription** (#1226): `services/diarization/asr_adapter.py` wraps a saved configuration and
keeps only its turns. Nothing in this package changes for it; a new provider becomes usable that
way as soon as its catalog entry says `supports_diarization` and its parser fills
`ASRSegment.speaker` (add it to `RUNNERS` in `tests/unit/test_asr_diarization_adapter.py`).

## Selection & failure

- `create_for_user(user_id, db)`: `UserSetting["active_asr_config_id"]` → `UserASRSettings` row (own
  **or `is_shared`**) → `ASR_PROVIDER` env → `LocalASRProvider`. The "which row" half is
  `resolve_active_config(user_id, db) -> (provider, model, cfg | None)`: no provider built,
  nothing decrypted. `create_for_user`, `get_active_model_capabilities` and the pipeline plan's
  `load_plan_inputs` all resolve through it (they were three copies of the lookup). The env
  fallback's model per provider lives in one table, `_ENV_MODEL`, read by both `_from_env` and
  `resolve_active_config`. **Cloud ASR config is per-user**; the
  local model is admin-pinned globally via the `SystemSettings` key **`asr.local_model`**
  (`transcription/config.py:_resolve_model_name` — worker-pinned → DB → `WHISPER_MODEL` → `large-v3-turbo`).
- The pipeline plan's transcription step routes non-local providers to the **`cloud-asr`**
  queue (service `celery-cloud-asr-worker`, concurrency 16) so network-bound jobs never block the
  GPU; `tasks/transcription/dispatch.py:_plan_for_dispatch` resolves it once per run.
- **`UserASRSettings.is_active` is read (#1207).** `_active_config_row` treats an inactive row exactly like a
  dangling pointer (env / local fallback, a warning that says why), so `create_for_user`, `resolve_active_config`
  and the pipeline plan agree; `get_diarizer_asr_config` returns `None` for it too. `POST /set-active` refuses it
  (409 `asr_config_inactive`), a first config created inactive is not auto-activated, the promotion on deleting
  the active config skips inactive rows, and `/status` and the list do not present an inactive pointer as active.
  The pointer itself is kept, so switching the row back on resumes it.
- `resolve_config(cfg)` is the same env / local fallback for ANY row (or `None`): it lets
  `services/asr_switch_plan` plan a switch before making it. Every ASR-side switch
  (`set-active`, `clear-active`, first-config auto-activation, the promotion on delete)
  goes through `assess_asr_switch` and refuses (409 `{code, message}`) a diarization-step
  error the switch introduces (#1225), and ANY transcription-step error (#1228: on lite every
  switch onto local Whisper is `local_asr_unavailable`, whatever came before and whatever the
  #1109 lock says). Deleting the active config asks `active_delete_refusal` first (lite: 409
  when no remaining config can transcribe) and `choose_after_active_delete` after; the delete
  returns the switch shape (`config_uuid` + `pipeline_issues`, #1236). On lite a `local`
  config cannot be created or updated into, and the super-admin local-model endpoints 409.
- **A cloud plan never falls back to local Whisper (#1236).** `create_for_user` still degrades a
  config it cannot load to the env default or local (other callers rely on that), but the
  transcription stage (`tasks/transcription/core.py:_cloud_asr_provider_or_none`) refuses any
  provider other than the one the plan names: a construction error, a degrade to local, or a
  degrade to another vendor raises `CloudASRUnusableError`, whose text is
  `CLOUD_ASR_UNUSABLE_MESSAGE` (reason `pipeline_not_runnable`, permanent
  `CONFIGURATION_REQUIRED`); the raw cause goes to the ERROR log only. It used to transcribe on
  this server against the user's choice (full) or die inside the engine (lite). Errors from
  `transcribe()` propagate and the file is marked FAILED. Providers wrap failures in
  `RuntimeError`, which is **not** in the task's `autoretry_for=(ConnectionError, TimeoutError)`
  — a cloud ASR failure is not retried.

## API keys

AES-256-GCM via `utils/encryption.encrypt_api_key` (PBKDF2-SHA256, 600k iters, `v3:` prefix; legacy
Fernet auto-detected on read). Responses expose only `has_api_key` / `has_access_key_id`, never a value
or mask, on any route (the owner-only `GET /config/{uuid}/api-key` was removed in #1246).
Which cloud providers a plain user may pick, and whether they may save a key of their own, is
`services/asr_provider_policy.py` (`SystemSettings` `asr.cloud_providers_for_users`,
`asr.users_may_add_own_keys`; super_admin `GET/PUT /admin/asr-providers`). The pipeline plan
enforces it (`asr_provider_not_allowed`, `diarizer_provider_not_allowed`), so dispatch and every
ASR switch refuse without a check of their own; creating/re-pointing a config is checked in
`asr_settings.py` via `refuse_config_change`. Admins and the `ASR_PROVIDER` env default are exempt.
On PUT an empty/null `api_key` means **keep the existing key** (how the UI edits other fields);
changing `provider` **clears** it.

## Gotchas

- **Build providers from a saved config only via `ASRProviderFactory.create_from_db_config(cfg)`.**
  It is the one place that decrypts `api_key` *and* `access_key_id`; both `create_for_user` (job path)
  and the saved-config test-connection endpoint call it. Constructing `create_from_config(...)` directly
  from a row is how #300 happened — the job path dropped `access_key_id`, so AWS jobs silently ran under
  whatever boto3 resolved from `AWS_ACCESS_KEY_ID`/IAM while "Test connection" passed. Decryption
  failure raises `ValueError`; `create_for_user` catches it and degrades to env/local.
- Per-vendor: **aws** needs two creds and round-trips through an S3 bucket it may auto-create, wants
  BCP-47 (`en-US`) codes, and its custom vocabulary must pre-exist in AWS. **azure** needs a region, and
  diarization requires `ConversationTranscriber` — `SpeechRecognizer` silently returns no speakers.
  **google** takes a service-account JSON only (applied by mutating `GOOGLE_APPLICATION_CREDENTIALS`)
  and its `speaker_tag` is 1-indexed, converted *before* `_normalize_speaker_label`. **openai** has a
  hard 25 MB limit, speakers only from `gpt-4o-transcribe-diarize` (segment-timed
  `diarized_json`), and translation only on `whisper-1`. **assemblyai** must send
  `speech_models` (a list); UI model names are not API ids. **deepgram** v6 reads the whole file into
  memory and its `validate_connection` needs a management-scoped key. **speechmatics** uses the async
  `speechmatics-batch` client via `asyncio.run()` — the legacy `speechmatics-python` SDK silently drops
  speaker labels. **pyannote.ai**'s poll timeout is only **300 s** (vs 1800–7200 s elsewhere), so long
  files fail there first. **azure** and **google** `validate_connection()` make **no network call** — a
  bad credential still "validates".
- **`UserASRSettings.base_url` reaches the real request for `gladia` only** (issue #594).
  `create_from_config` takes a `base_url` parameter but only the Gladia branch forwards it to its
  constructor; every other provider still silently drops a configured `base_url` exactly as before.
  `GladiaProvider._resolve_base_url` applies an SSRF guard (`ASR_ALLOW_PRIVATE_ENDPOINTS`, a
  deliberately separate flag from `LLM_ALLOW_PRIVATE_ENDPOINTS` — mirrors `app/utils/url_validation.py`,
  same as the LLM path) and raises `ASRConfigurationError` — a **deliberate refusal**, propagated by
  `create_for_user` rather than silently degraded to local — for a private/loopback/RFC1918/link-local/
  metadata/docker-compose-hostname target unless explicitly allowed. The shipped default
  (`https://api.gladia.io`) and any value equal to it are exempt from the guard, so an unconfigured
  deployment pays no DNS lookup at construction. All ten providers' catalog entries still say
  `supports_custom_url: False` (`ASR_PROVIDER_CATALOG`), so the admin UI does not surface the field for
  any provider today — a config can still be set directly via the API (as
  `register_mock_gladia_asr_config`'s test fixture does), just not through Settings → AI ASR.
