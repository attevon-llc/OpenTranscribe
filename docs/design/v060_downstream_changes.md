# v0.6.0: changes relevant to derived deployments

For anyone who packages, extends or operates OpenTranscribe as the base of their own deployment
(custom images, an overlay of extension modules, a capability resolver, orchestration on
Kubernetes or ECS, automation against the API). This lists what changed between `v0.5.1` and
`v0.6.0` at the boundaries such a deployment touches: environment variables, capabilities,
extension seams, API contracts, migrations and stored settings. User-facing release notes are in
`CHANGELOG.md`; the operator walkthrough is `docs-site/docs/operations/upgrading-to-0-6-0.md`.

Derived from `git diff v0.5.1..HEAD` of the v0.6.0 branch, with each item checked against the
code. File references are to the v0.6.0 tree.

## 1. Extension seam version: 3 -> 11

`CLOUD_SEAM_VERSION` (`backend/app/auth/constants.py`) moved from `3` to `11`. The comment block
above it is the authoritative description of each step; in summary:

| Version | Change | Breaking for an extension? |
|---|---|---|
| 4 | `tasks.transcription.hooks.DispatchBlockedError`: a before-dispatch hook can now block a dispatch on purpose (other hook exceptions are still contained) | No, but a hook that raised its own exception type meaning "block" was silently ignored before and should be audited |
| 4 | `services.usage_service.record_event` no longer swallows write failures: `True`/`False` mean recorded / already recorded, a database failure raises | **Yes**, for a caller that treated `False` as "contained" |
| 4 | `core.tenant_limits.set_redaction_floor_resolver` / `resolve_redaction_floor(db, org_id)`: a per-organization redaction floor that can only add to the global floor | No (additive) |
| 5 | `user.platform_super_admin_link_authorized` (Alembic `v397`): an opt-in escape hatch for JIT-linking an external identity onto a `super_admin` row. Default false; core never sets it | No |
| 6 | The capability resolver result fails **closed** (#868): an omitted known key, a non-bool value, a non-dict result or a raising resolver reads as denied | **Yes**, for a resolver that relied on omission meaning "community default". Return every key you grant, e.g. start from `{**COMMUNITY_CAPABILITIES, ...}` |
| 7 | External identity-provider session timeouts (#1106): `ExternalIdentity.auth_time`; with `EXTERNAL_SESSION_ABSOLUTE_TIMEOUT_ENFORCED` (default on) a token older than `SESSION_ABSOLUTE_TIMEOUT_MINUTES` is refused with 401 `session_expired`. Two optional frontend seam exports: `getExternalSessionAuthTime()` and `externalReauthenticate(reason)` | No; a verifier that supplies no `auth_time` is simply not enforced (one warning per provider) |
| 8 | Platform-admin content access (#1122): in multi-tenant mode `admin`/`super_admin` have no implicit access to tenant content and need a support-access grant carried in `X-Support-Access-Grant`. `RequestContext` gained `bypass`; `get_current_context` was split from `get_base_context` | **Yes**, for staff tooling that read or edited tenant content through the platform role |
| 9 | Legal document provider seam: `app.legal.registry.set_document_provider(provider)` replaces the document set served by `/api/legal/*`. Installing a provider turns `requires_acknowledgment` off for the operator acknowledgment; Alembic `v441` adds `legal_acceptance` | No, additive |
| 10 | Viewer-aware `GET /api/system/capabilities` (#1274): the endpoint resolves the caller optionally and, for platform staff (an active `is_superuser`), every platform-audience key outside `NO_PLATFORM_BYPASS` reads `True` even when the resolver denied or omitted it, or raised. Tenants and anonymous callers get the resolver's answer unchanged. The resolver may now find `request.state.user_id` / `external_identity` there (external tokens only). The response is `Cache-Control: private, no-store` with `Vary: Cookie, Authorization`. `require_capability(..., platform_admin_bypass=False)` raises `ValueError` at import for a key not declared in `NO_PLATFORM_BYPASS`. `get_optional_current_user` now honours the per-user revocation epoch | No for the response shape. **Review** any edge-cache rule that cached the endpoint, any resolver that assumed `request.state` carries no user there, and any extension that registers its own `platform_admin_bypass=False` gate (declare the key) |
| 11 | Privacy policy layer (#1280, #1279): `app.services.privacy.consent.register_voiceprint_consent_hook(hook)` (a `(Session, VoiceprintConsentRequest) -> bool` hook must return the literal `True`; a raise or any other value denies; no hook means allow) and `app.services.privacy.policy.set_privacy_ceiling_resolver(resolver)` (`(Session, organization_id \| None) -> PrivacyCeiling \| None`; can only switch voiceprint matching and gender inference off, a raising or malformed answer denies both, runs in Celery tasks with no request). Alembic `v445` adds `organization_policy`; the organization redaction floor stored there is unioned with the floor resolver's result (strictest wins). Instance switches `privacy.voiceprint_matching_enabled` and `privacy.gender_inference_enabled` default on | No, additive; an upgrade changes no behaviour until a switch is turned off |

Frontend extension slots added in this range: a user-menu extras slot in
`components/navbar/UserDropdown.svelte`, and a legal-gate modal mounted from the root layout
(`routes/+layout.svelte`). Both render nothing in the stock build.

## 2. Tenancy mode

New `TENANCY_MODE` (`backend/app/core/config.py`, passed through by `docker-compose.yml`):

| Value | Behaviour |
|---|---|
| `auto` (default) | Multi-tenant when `DEPLOYMENT_EDITION` is not `community` **or** any active organization exists; otherwise single-tenant. A multi result latches for the process |
| `single` | Instance-wide admin access as before (audited, with a startup warning, when organizations exist) |
| `multi` | Forced tenant isolation |
| anything else | Logged, treated as `multi` (fail closed) |

It is an environment variable on purpose, so it cannot be switched off from the admin UI. A
derived deployment with a non-`community` `DEPLOYMENT_EDITION` is **multi-tenant by default**
from v0.6.0. Support-access routes answer 404 in single-tenant mode.

## 3. Capabilities

New capability keys, all `True` in `COMMUNITY_CAPABILITIES` (`backend/app/core/capabilities.py`),
each enforced on the server (not only hidden in the UI). There is no platform-admin bypass, and
the task-time checks run without a request, so a resolver should answer them per deployment:

| Key | Audience | Effect when false |
|---|---|---|
| `transcription.model_choice` | user | A per-file `whisper_model` on prepare/complete/reprocess is ignored |
| `transcription.diarization_source` | user | The user's speaker-detection source (and `diarization_asr_config_uuid`) is ignored; the deployment default applies, including values stored before the lock |
| `transcription.advanced` | user | VAD, hallucination threshold and repetition penalty overrides are ignored |
| `media_sources` | user | Per-user media-source routes answer 404 and are not used for downloads |
| `audio_extraction` | user | Audio-extraction routes answer 404 |
| `speaker_attributes.migration` | platform | Bulk speaker-attribute re-detection is unavailable |
| `admin.flower` | platform | The task-dashboard link is hidden and its `auth_request` probe denies |
| `admin.takedown` | platform | The quarantine review queue is unavailable |

Enforcement newly added to existing keys:

- `url_ingest` was declared but read by nothing; it now disables URL import.
- `system.hardware_stats` now gates `GET /api/system/stats` (404 when off) and the WebSocket GPU
  telemetry.

The lock helper for transcription settings is `backend/app/core/locked_settings.py`.

## 4. Environment variables

### Removed

| Variable | Notes |
|---|---|
| `ENABLE_DIARIZATION` | No longer read anywhere; a leftover value is ignored silently. Speaker detection follows each user's `transcription_diarization_source` (default `provider`, Automatic) and the run's pipeline plan. Use the `transcription.diarization_source` capability to pin it per deployment |

### Added (all optional, with defaults)

Task recovery and broker:

| Variable | Default |
|---|---|
| `CELERY_WORKER_SOFT_SHUTDOWN_TIMEOUT` | `30` |
| `CELERY_WORKER_SOFT_SHUTDOWN_ON_IDLE` | `true` |
| `CELERY_REDIS_SOCKET_TIMEOUT` | `30` |
| `CELERY_REDIS_CONNECT_TIMEOUT` | `10` |
| `CELERY_WORKER_MAX_TASKS_PER_CHILD` | `0` (off) |
| `CELERY_WORKER_MAX_MEMORY_PER_CHILD_KB` | `0` (off) |
| `TRANSCRIPTION_QUEUE_MAX_WAIT_SECONDS` | `604800` |
| `TRANSCRIPTION_HEARTBEAT_INTERVAL_SECONDS` | `15` (was 30) |
| `TRANSCRIPTION_HEARTBEAT_TTL_SECONDS` | `90` (was 300) |
| `BROKER_ORPHAN_SWEEP_INTERVAL_SECONDS` | `60` |
| `BROKER_ORPHAN_STALE_SECONDS` | `120` |
| `TRANSCRIPTION_RECLAIM_GRACE_SECONDS` | `120` |
| `BROKER_ORPHAN_UNTRACKED_STALE_SECONDS` | `600` |
| `BROKER_ORPHAN_MAX_REQUEUES` | `5` |
| `TRANSCRIPTION_MAX_INFRA_REQUEUES` | `5` |
| `TRANSCRIPTION_INFRA_REQUEUE_ALERT_THRESHOLD` | `3` |
| `TASK_MAX_DURATION_TRANSCRIPTION_SECONDS` | `3600` |
| `TASK_MAX_DURATION_DEFAULT_SECONDS` | `1800` |
| `TASK_RECOVERY_STALENESS_SECONDS` | `300` |
| `TASK_RECOVERY_ORPHANED_HOURS` | `1` |
| `TASK_HEARTBEAT_TTL_SECONDS` | `120` (not in `.env.example`) |
| `TASK_REPLAY_MAX_ATTEMPTS` | `2` (not in `.env.example`) |

GPU admission and engine:

| Variable | Default |
|---|---|
| `GPU_VRAM_ADMISSION` | `true` |
| `GPU_VRAM_BUDGET_FRACTION` | `0.85` |
| `GPU_VRAM_ADMISSION_TIMEOUT_S` | `1800` |
| `GPU_STAGE_VRAM_MB_ASR`, `GPU_STAGE_VRAM_MB_DIARIZATION` | measured estimates |
| `GPU_OOM_MAX_HALVINGS` | `2` |
| `GPU_POISONED_MAX_REQUEUES` | `2` |
| `GPU_HOST_MEMORY_ADMISSION` | `true` (`false` = log only) |
| `GPU_HOST_BASELINE_MB` | `2560` |
| `GPU_PER_TASK_HOST_MB` | `2048` |
| `ENGINE_SHARED_VOLUME_ENABLED` | unset = follows `PIPELINE_SCRATCH_SHARED`; set false for multi-node |
| `SPEAKRS_CUDA_MEM_LIMIT_MB`, `SPEAKRS_CUDA_CONV_SEARCH`, `SPEAKRS_CUDA_CONV_MAX_WORKSPACE` | passed through to the diar-native sidecar (0.3.3) when set |
| `SPEAKER_ATTRIBUTE_MAX_CLIP_SECONDS` | `20` (not in `.env.example`) |
| `SPEAKER_ATTRIBUTE_MAX_CONCURRENCY` | `2` (not in `.env.example`) |

API, storage, search and metrics:

| Variable | Default |
|---|---|
| `API_THREADPOOL_SIZE` | `0` (match the DB pool, never below 40) |
| `API_MEDIATED_UPLOAD_ENABLED` | `true`; `false` makes `POST /api/files` answer 404 and forces presigned uploads |
| `CACHE_BUCKET_NAME` | `processed-videos` (bucket for regenerable derived assets) |
| `OPENSEARCH_INDEX_TIMEOUT_S` / `OPENSEARCH_INDEX_TIMEOUT_RETRIES` | `60` / `2` |
| `SEARCH_INDEX_RETRY_HORIZON_S` / `_BASE_DELAY_S` / `_MAX_DELAY_S` | `21600` / `30` / `600` |
| `SEARCH_INDEX_SWEEP_BATCH_SIZE` / `_COOLDOWN_S` / `_LOOKBACK_HOURS` | `25` / `1800` / `72` |
| `WORKER_METRICS_PORT` | unset = off; per-worker Prometheus `/metrics` |
| `ERASURE_JOURNAL_BACKEND` / `ERASURE_JOURNAL_OBJECT_PREFIX` | `file` / `gdpr/erasure-journal/`; `object_storage` for read-only root filesystems |
| `BEDROCK_RETRY_MODE` / `BEDROCK_MAX_ATTEMPTS` | `adaptive` / `8` |
| `OPENROUTER_HTTP_REFERER` | the project URL |

Security and identity:

| Variable | Default |
|---|---|
| `TENANCY_MODE` | `auto` (section 2) |
| `EXTERNAL_SESSION_ABSOLUTE_TIMEOUT_ENFORCED` | `true` |
| `RATE_LIMIT_AUTH_METHODS_PER_MINUTE` | `60` |
| `MFA_REQUIRED_FOR_ADMINS` | `false` |
| `PASSWORD_POLICY_PROFILE` | code default `hardened`; `.env.example` ships `standard` for new installs. Values `basic`, `standard`, `hardened`, `custom` (`nist`/`stig` remain aliases) |
| `PASSWORD_MAX_LENGTH`, `PASSWORD_BLOCKLIST_ENABLED`, `PASSWORD_BLOCKLIST_PATH`, `PASSWORD_CONTEXT_WORDS`, `PASSWORD_MIN_AGE_HOURS` | level defaults |
| `PASSWORD_HIBP_ENABLED` / `PASSWORD_HIBP_URL` / `PASSWORD_HIBP_TIMEOUT_SECONDS` | `false` / Pwned Passwords range API / `3` |

Privacy (set in compose with `${VAR:-...}` so `.env` can opt back in): `PYANNOTE_METRICS_ENABLED=false`,
`HF_HUB_DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=1`, `DENO_NO_UPDATE_CHECK=1`, `MINIO_UPDATE=off`,
`MINIO_CALLHOME_ENABLE=off`.

### Changed meaning

- `CORS_ORIGINS`: unset in a hardened (production) deployment now allows **no** cross-origin
  callers (it used to default to the Vite dev origins). Set it explicitly if a frontend is served
  from a different origin.
- `GPU_CONCURRENT_REQUESTS` now also sets the GPU worker's `celery --concurrency`; the two must
  agree.
- `PIPELINE_SCRATCH_SHARED=false` now also turns off the engine WAV handoff.
- `FFMPEG_THREADS` accepts `auto`; `REDACTION_PII_WORKERS` auto-sizing follows the container CPU
  quota.

## 5. Migrations

Twelve revisions after `v393_add_overlap_timing_columns`, applied at backend start unless
`RUN_MIGRATIONS_ON_STARTUP=false` (then a migrate job must run `alembic upgrade head`, and
readiness answers 503 until the schema is at head). The chain is one-way.

| Revision | Kind | Notes |
|---|---|---|
| `v397_add_platform_super_admin_link_authorized` | additive | `user.platform_super_admin_link_authorized BOOLEAN NOT NULL DEFAULT FALSE` |
| `v420_add_tag_organization_id` | schema + backfill | `tag.organization_id`; uniqueness per tenant. Backfill moves/splits/merges tags into organizations; nothing fires without organizations. Partial downgrade |
| `v421_add_media_playback_path` | additive | `media_file.playback_path` |
| `v422_add_collection_tenancy` | schema + backfill | `collection.user_id` nullable with `ck_collection_owner_or_org`; per-kind unique indexes; backfill as `v420` |
| `v430_per_tenant_speaker_and_vocab_names` | schema + fill-only backfill | Stamps NULL `organization_id` on speaker profiles, speaker collections and vocabulary when unambiguous; unique names per tenant. Ambiguous rows: `python -m app.scripts.backfill_tenant_stamps` |
| `v431_add_media_duration_provenance` | additive | `media_file.duration_source` (`container` / `transcript_extent` / `none`); repair via `POST /api/admin/media-duration-backfill` |
| `v432_support_access_grant` | additive | `support_access_grant`, `support_access_use`. Downgrade destroys grant evidence |
| `v433_add_requested_transcription_options` | additive + small data fix | `media_file.requested_{min,max,num}_speakers`, `requested_disable_diarization`; `watch_source` 1/20 defaults dropped and exact 1/20 rows set to NULL |
| `v434_reset_unusable_pyannote_source` | data only | `transcription_diarization_source` `pyannote` -> `provider` for users with no pyannote key and local transcription (reads `ASR_PROVIDER` at migration time). Downgrade no-op |
| `v436_lite_local_speaker_detection_to_automatic` | data only, lite only | `local` -> `provider` for lite users whose active ASR config returns speakers (reads `DEPLOYMENT_MODE` at migration time). Downgrade no-op |
| `v437_add_media_processing_provenance` | additive | `media_file.processing_provenance JSONB`; written only by `services/file_provenance.record_stage` |
| `v438_add_speaker_correction_carryover` | additive | `speaker_correction_carryover` |
| `v439_infer_legacy_voiceprint_suggestion_source` | data fix | `speaker.suggestion_source` on rows proven voice-matched |

`v434` and `v436` read environment variables at migration time, so a migrate job must run with
the same `ASR_PROVIDER` (and provider key variables) and `DEPLOYMENT_MODE` as the application.

Deployments with organizations also run two one-off jobs after the upgrade (no-ops without
organizations):

- `python -m app.tasks.tenant_backfill_task` (also a Celery task, `tenant_backfill_organization_id`):
  stamps `organization_id` onto search and voiceprint documents indexed before the field
  existed, including the legacy `transcripts` index whose mapping now gains the field at startup
  (#1115).
- `python -m app.scripts.backfill_tenant_stamps --sync-voiceprints`: reports rows `v430` left
  unstamped because their tenant was ambiguous (exit status 4 when there are any) and copies
  profile stamps onto voiceprint documents. `--apply --created-before <deploy time>` re-runs the
  stamping on its own.

## 6. API and wire contracts

`backend/openapi.json` is the full record. No path was removed.

### New routes

- Support access: `/api/support-access/*`, `/api/org-admin/support-access/*`,
  `/api/users/me/support-access/*` (16 operations; 404 in single-tenant mode).
- `GET /api/user-settings/pipeline`; `GET|PUT|DELETE /api/user-settings/diarization/pyannote`,
  `POST /api/user-settings/diarization/pyannote/test`.
- `GET /api/files/{uuid}/captions` (WebVTT, `speaker_placeholder` optional); `GET /api/files/owners`.
- `GET /api/admin/locked-accounts`, `POST /api/admin/locked-accounts/{identifier}/reset`.
- `GET /api/admin/data-integrity/storage/counts`, `GET .../storage/status`, `POST .../storage`.
- `POST /api/admin/media-duration-backfill`.
- `GET /api/llm-settings/local-endpoints`.

### Status code changes

| Route | Before | Now |
|---|---|---|
| `GET /api/files/{uuid}` for a file the caller cannot access | 403 | 404 |
| `DELETE /api/asr-settings/config/{uuid}`, `DELETE /api/asr-settings/all` | 204 | 200 with the resulting pipeline |
| `GET /api/files/{uuid}/suggestions` with none yet | 404 | 204 |
| `GET /api/system/stats` with `system.hardware_stats` off | 200 | 404 |
| `GET /api/auth/saml/metadata` with SAML disabled | 500 | 404 (503 on invalid config) |
| `POST /api/files` with `API_MEDIATED_UPLOAD_ENABLED=false` | n/a | 404 before reading the body |
| `PUT /api/user-settings/transcription` with a speaker-detection choice the plan cannot run | accepted | 409 with an issue code |
| Any route with an expired external IdP session (section 1, v7) | accepted | 401 `session_expired` |

### Shape changes

- **Removed field:** `GET /api/system/stats` no longer has `diarization_enabled`.
- **Changed shape:** the admin quarantine list (`QuarantinedFile`) replaces integer `user_id`,
  `organization_id` and `quarantined_by` with `owner_uuid`, `owner_email`, `organization_uuid`
  and `quarantined_by_email`.
- **New fields:** `processing_steps` on `GET /api/files/{uuid}` and
  `GET /api/my-files/{uuid}/status`; `suggestion_source` on a segment's speaker;
  `session_idle_timeout_minutes` and `session_absolute_timeout_minutes` on `GET /api/auth/methods`;
  `api_mediated_upload_enabled` on `/api/system/capabilities`; `supports_diarization`,
  `timestamp_granularity` and speaker-count hints per model in the ASR catalog and
  `/asr-settings/status`; `pipeline_issues` on ASR switch responses and `auto_activated` on
  `POST /api/asr-settings`; `blocked_speaker_uuids` on batch verify and cluster merge; `playback`
  on `/stream-url`; `duration_seconds` on completion notifications.
- **New request inputs:** the `X-Support-Access-Grant` header; `override_unassignment` on
  speaker batch verify (exactly one speaker, else 422); `group=` on
  `DELETE /api/user-settings/transcription`; `owner=` on the file list; a new speaker-detection
  source value `asr_config` with `diarization_asr_config_uuid`.
- **Semantics:** a speaker's canonical label (index documents, exports, chat, `resolved_display_name`)
  no longer comes from an LLM suggestion; only `display_name` or a voiceprint suggestion
  (`voice_match`, `profile_match`) at confidence >= 0.75. Clients that read
  `resolved_display_name` will see diarizer labels where an LLM name used to appear.
- **Removed task:** the legacy single-task `transcription.process_file` Celery task is gone;
  dispatch goes through the plan-driven pipeline only.
- **New Celery header** `x-ot-published-at` on every task message (queue-wait metrics).

## 7. Stored settings

New `UserSetting` keys: `auto_llm_speaker_identification`, `auto_topic_extraction` (default on),
`transcription_diarization_asr_config_uuid`; new allowed value `asr_config` for
`transcription_diarization_source`.

New `SystemSettings` keys: `auto_llm.speaker_identification`, `auto_llm.topic_extraction`
(system-tier defaults for the switches above); `speaker_label_provenance_backfill_after_id` and
`speaker_label_provenance_backfill_complete` (progress of the one-time label re-index run by
`search_index_maintenance`).

Changed coded default: chat context expansion (`DEFAULT_CHAT_CONTEXT_EXPANSION_ENABLED`) is now
on. Deleted flag: `chat.rag.map_tier_hybrid`.

## 8. Behaviour a derived deployment should re-test

- Lite (`DEPLOYMENT_MODE=lite`) refuses every local transcription and local speaker-detection
  path (409 with a fixed issue code); voiceprints are still computed locally.
- Speaker detection runs exactly as configured, including cloud speaker detection after local
  transcription. A provider used only for its speakers still performs (and charges for) a full
  transcription at that provider.
- Unassigning a speaker records a block that every automatic assignment path honours and that
  survives re-processing the file.
- The capability resolver must return every key it grants (section 1, v6).
- Images no longer contain `pip`; anything that installed packages into a running container
  must move to the image build.
- Third-party telemetry is off by default (section 4).

## 9. Adoption checklist

Order for a derived deployment moving from `v0.5.1` to `v0.6.0`. Move the base pin to the release
tag, not to a branch.

1. Take a database backup; the migrations (`v397` to `v439`) run on backend start and are one-way.
2. Read section 1 and fix the two breaking seam steps: `record_event` now raises on a write
   failure (v4), and the capability resolver fails closed (v6). Check the other steps too.
3. Set `TENANCY_MODE` explicitly (section 2) and confirm platform staff reach tenant content only
   through a support-access grant (v8).
4. Apply the removed, added and changed environment variables (section 4); every addition is optional.
5. Installs with organizations: run the two one-off backfills named in the upgrade page.
6. Re-run any automation against the API contracts (section 6), including the status-code changes.
7. Re-test everything in section 8.
8. Run a role matrix on a multi-tenant stack: user, organization admin, admin and platform
   super admin, each in community, `TENANCY_MODE=multi` and lite deployments.
