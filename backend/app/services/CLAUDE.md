# app/services — business-logic layer

## Purpose

Everything between the API/Celery entry points and the data layer. ~110 modules; endpoints and
tasks should stay thin and call in here. `interfaces.py` declares the structural `Protocol`
contracts (`StorageService`, `SearchService`, `CacheService`, `NotificationService`) that
`minio_service`, `opensearch_service`, `redis_cache_service`, and `utils/websocket_notify`
already satisfy — depend on the Protocol, not the concrete module, at new seams.

## Where things live

- **Search / retrieval** — `search/` (transcript chunks + hybrid/neural; **has its own
  CLAUDE.md** with the critical `cosinesimil` score gotcha), `opensearch_service/` (the
  speaker/voiceprint kNN plane + file docs; a package since #284 A3.5 — `client` owns the
  singleton, `aliases`/`indices`/`repair` the index plane, `speaker_*` the documents, and
  `matching`/`profiles`/`clusters` the kNN reads. Its `__init__` re-exports every name the
  old flat module exported), `opensearch_summary_service.py`, `opensearch_snapshot.py`,
  `similarity_service.py`.
- **Speakers** — `speaker_*_service.py`, `profile_embedding_service.py`,
  `smart_speaker_suggestion_service.py`, `optimized_embedding_service.py`,
  `embedding_mode_service.py`, `metadata_speaker_extractor.py`.
- **Providers** — `asr/` and `diarization/`: `base.py` + `types.py` + `factory.py` + one file
  per vendor. Add a provider by adding a module and registering it in the factory; never
  branch on provider name at a call site.
- **Redaction** — `redaction/` (**own CLAUDE.md**). **Watch sources** — `watch_sources/`
  (**own CLAUDE.md**).
- **Media in/out** — `media_download_service.py` (yt-dlp), `media_mirror_*.py`,
  `protected_media_providers.py` + `protected_media_plugins/`, `minio_service.py` +
  `storage_backend.py` (**see below**), `subtitle_service.py`, `formatting_service.py`.
- **Ops** — backup/recovery, cleanup, migration lock+progress, task detection/filtering/recovery,
  system settings, usage, GDPR erasure (`gdpr_erasure_service.py` +
  `erasure_ledger_service.py` — **see below**).
- **Identity / account security** — see below. `auth_config_service.py` (DB > .env > coded
  default, AES-256-GCM at rest), `account_security_service.py`,
  `idp_group_mapping_service.py`, `directory_sync_service.py`, `auth_mail_config_service.py`,
  `scim_service.py` + `scim_token_service.py` (the SCIM 2.0 write path and its credential).
  The auth *methods* themselves live in `app/auth/` (its own CLAUDE.md), not here.

## Identity services — the single-implementation rules

Five modules, each of which exists to be the **only** implementation of its rule. Adding a
second is the specific failure mode they were written to prevent.

- **`account_security_service.py`** — every credential or privilege change goes through it:
  it applies the password policy and reuse history, **revokes sessions**, and writes the audit
  event. Those three used to be applied inconsistently across `users.py` / `admin.py` /
  `password_reset.py`. Note the two revocation entry points are split by contract —
  `revoke_all_user_tokens_in_transaction` (commits nothing, propagates) vs
  `revoke_all_user_tokens` (best-effort, commits, unsafe mid-transaction).
- **`idp_group_mapping_service.py`** — `resolve_grants` + `reconcile_user` turn a directory's
  claim list into in-app group membership and a role. Three callers share it: login
  (`auth/ldap_auth.py`, `auth/oidc/provisioning.py`, `auth/proxy/provisioning.py`) and the
  sweep below. `reconcile_memberships=False` / `apply_role=False` exist for the proxy path,
  where an absent groups header and an unset role header both mean "the source asserts
  nothing" — reconciling against that would strip memberships and demote admins. **`super_admin` is
  refused before anything is persisted** (`assert_grantable_role`) *and* by
  `ck_group_mapping_role_capped`; a `super_admin` is never demoted here either. Only
  directory-sourced memberships are ever removed. A privilege change revokes sessions.
- **`directory_sync_service.py`** — the periodic LDAP reconciliation/deprovisioning pass.
  Fail closed on *ambiguity*, not on error: "the directory says gone" acts, "I could not ask"
  aborts the pass. Never touches `super_admin` or `local` accounts, disables rather than
  deletes, and is bounded by `directory_sync.max_disables_per_run`. **Disabling without
  revoking would leave the refresh token rotating**, so it revokes too. Settings are six
  `SystemSettings` rows (`directory_sync.*`, defaults in `core/constants.py`) — there are no
  directory-sync `.env` vars, no endpoint, and no admin panel.
- **`auth_mail_config_service.py`** — which `EmailNotificationConfig` carries transactional auth
  mail (`SystemSettings` key `email.auth_config_uuid`). The read half is
  `email_service.load_auth_mail_config`. A designation naming a missing or disabled row is
  rejected at **write** time, and deleting/disabling the designated row is refused — the read
  path degrades quietly enough that a bad designation would only surface as undelivered password
  resets.
- **`scim_service.py`** — every write `/scim/v2` performs. Deactivation revokes sessions,
  `super_admin` is untouchable, roles are never written at all, and `DELETE /Users/{id}` is a
  soft-disable. `scim_token_service.py` owns the credential: SHA-256 at rest, revoked and
  expiry checked on the same read that finds the row, revocation one-way.

`README.md` in this directory is the older long-form tour; it drifts — trust the code.

## GDPR Art. 17 erasure — the three rules that are not obvious (issue #442)

`gdpr_erasure_service.py` destroys the data; `erasure_ledger_service.py` records that it
was asked for; `tasks/erasure_reconciliation.py` finishes what could not finish. All
three are needed, and dropping any one reproduces a defect that shipped.

- **The ledger must not contain the personal data it records the destruction of.** "We
  erased alice@example.com" *containing* the address is a copy of the thing that was
  supposed to be destroyed, in a table designed to outlive it. So `erasure_ledger` has
  **no free-text column at all** — every textual column is a short enum with a CHECK —
  and the one JSONB column is nailed shut by `ck_erasure_ledger_counters_numeric`
  (`jsonb_path_exists`, IMMUTABLE, so it is legal in a CHECK where a subquery is not).
  There is deliberately **no email hash** either: a hash of a value from a guessable
  space is pseudonymous personal data (Recital 26), not anonymous. Subjects are named by
  `id` + `uuid` only, which are meaningless once the row they point at is gone — and
  meaningful again exactly when a restore brings it back, which is the property the
  resurrection check relies on. `record_outcome` drops `summary["errors"]` entirely: its
  entries carry file UUIDs and driver messages that quote filenames and storage paths.
- **`subject_user_id` / `subject_organization_id` are NOT foreign keys, on purpose.**
  They name the rows the table records the destruction of. A real FK would either block
  the delete (`NO ACTION`) or null the column (`SET NULL`) — and nulling it destroys the
  only key the reconciliation sweep has. Referential integrity is the property these
  columns must not have. `actor_user_id` **is** an FK, `ON DELETE SET NULL` like every
  actor FK since v387; `tests/unit/test_user_deletion_fk_coverage.py` records the
  disposition.
- **The ledger cannot only be a table.** It lives in Postgres, so restoring a dump taken
  *before* an erasure destroys the record of the erasure along with the erasure — it
  would have to survive its own failure mode. Every entry is therefore also appended to
  a line-delimited journal under `DATA_DIR/gdpr/`, outside the dump;
  `restore_from_journal` re-opens anything the database has lost, keeping the ORIGINAL
  `requested_at`/`sla_due_at` so a restore cannot buy another month of Art. 12(3) time.
  Its limit is honest: one file on one volume. Off-host replication is deployment
  config; the audit stream is the second copy that already leaves the host.

Two judgement calls worth not re-litigating blindly:

- **A legal hold retains the ACCOUNT, not just the file, and that is forced by the
  schema** — `media_file.user_id` is a plain `NO ACTION` FK, so `DELETE FROM "user"`
  raises while a held file exists. Art. 17(3)(e) only justifies retaining the *file*, so
  the account's survival is a side effect, made temporary by the ledger entry and the
  sweep. Making it genuinely file-granular means anonymising the account instead —
  a design change, not a patch.
- **`org_member` entries are never auto-re-erased after a restore.** That scope never
  deletes the `user` row, so "is the subject present" is always true; the only other
  signal ("does the member have org rows again?") is indistinguishable from the member
  legitimately uploading to that tenant the next day, and acting on it would destroy
  data nobody asked to erase. They are counted as `org_member_manual_review` instead.

## Object storage: `storage_backend.py` + `minio_service.py`

`minio_service` is the API (upload/download/presign/lifecycle, ~60 call sites plus the
`minio_client` singleton). `storage_backend` is the *policy* layer underneath it and owns
every difference between the two backends (issue #284 A1.11/A1.12):

| | `STORAGE_BACKEND=minio` (default) | `STORAGE_BACKEND=s3` |
|---|---|---|
| Endpoint | `MINIO_HOST:MINIO_PORT` | `S3_ENDPOINT_URL`, else `s3.<S3_REGION>.amazonaws.com` |
| Credentials | static `MINIO_ROOT_*` | AWS chain (env → IRSA → ECS → IMDS) unless `S3_USE_IAM_ROLE=false` |
| Addressing | path-style | virtual-host (minio-py switches on AWS hostnames) |
| Presigned host | rewritten to `STORAGE_PUBLIC_URL`/`MINIO_PUBLIC_URL`, else `/s3` | **not rewritten** |
| Single-PUT ceiling | 5 TiB | 5 GiB (`supports_single_put`) |
| Abandoned-multipart expiry | MinIO's own scan (24 h) | lifecycle rule (`ensure_abort_incomplete_lifecycle`) |
| Bucket CORS | implicit | opt-in `S3_CONFIGURE_BUCKET_CORS` (boto3 — minio-py has no CORS API) |

- **One SDK for both.** The client is always `minio.Minio`; minio-py is a generic S3 SDK,
  so switching backends changes construction, not the 60 call sites. boto3 appears in
  exactly one place — `ensure_bucket_cors`.
- **`clamp_presigned_expiry` gates every presigned URL** (`PRESIGNED_URL_MAX_SECONDS`,
  6 h). A presigned URL cannot outlive the credentials that signed it, and IAM-role STS
  sessions expire well inside 24 h, so a longer URL just starts 403-ing. `get_file_url`'s
  old 24 h default arg is gone.
- **Large uploads go browser-side multipart** (`multipart_upload.py`, issue #327).
  `build_upload_plan` is the single decision point `/files/prepare` calls: multipart at or
  above `multipart_threshold_bytes()` (`MULTIPART_THRESHOLD_MB`, 512 MB, clamped to the
  single-PUT ceiling so >5 GiB on `s3` is *always* multipart), one presigned PUT below it,
  `None` → the client falls back to `POST /files`. Part URLs are signed **8 at a time**
  (`PART_URL_BATCH`), not once for the whole object: they take the same
  `clamp_presigned_expiry` as everything else and a 15 GB upload can outlive a 6 h clamp.
  `/files/multipart/parts` signs the next batch and, on resume, lists the parts storage
  already holds. `/files/complete` assembles them (client ETags, else read back).
- **Abandoned multipart uploads must be aborted** — S3 and MinIO both bill for the parts,
  and they never appear in an object listing. `cancel_upload` (`DELETE /files/{uuid}`) calls
  `abort_uploads_for_object`, which finds the uploads by key because the `upload_id` is
  client state. `ensure_abort_incomplete_lifecycle` adds the storage-side backstop and is
  **native-S3 only**: MinIO's ILM rejects an `AbortIncompleteMultipartUpload` rule
  (`InvalidArgument`; it silently drops the action from a mixed rule) and does not need one —
  it purges stale uploads itself via `api.stale_uploads_expiry` (24 h).
- **minio-py's multipart primitives are underscore-prefixed** (`_create_multipart_upload`,
  `_complete_multipart_upload`, `_abort_multipart_upload`, `_list_parts`,
  `_list_multipart_uploads`). Driving them keeps the one-SDK rule above; boto3 would mean a
  second client with its own copy of the endpoint/credential policy.
  `tests/unit/test_multipart_upload.py` asserts they still exist so an SDK bump fails in CI.
  Part *signing* is the public `get_presigned_url(..., extra_query_params=...)`.
- Don't reintroduce a second host-rewrite. `MinIOService.get_presigned_url` used to
  hardcode `http://minio:9000` → `localhost:5178`/`EXTERNAL_MINIO_URL`; it now shares
  `rewrite_public_host` like everything else.

## LLM features (optional)

`llm_service.py` is a **synchronous** client on purpose (Celery tasks; no asyncio conflicts).
`LLMProvider` = openai · vllm · ollama · anthropic · openrouter · custom (`claude` is a
deprecated alias for `anthropic`).

- **Resolution order**: `create_from_user_settings(user_id)` → falls back to
  `create_from_system_settings()` (env `LLM_PROVIDER` + provider keys/endpoints). Empty
  `LLM_PROVIDER` and no user config = transcription-only; `create_from_system_settings`
  returns `None` and callers must handle it. `custom` is **user-config only** — it always
  returns `None` from system settings.
- Per-user keys are AES-encrypted (`utils/encryption.encrypt_api_key`) and never returned;
  edit mode reuses the stored key when the request omits `api_key`.
- Summarization: BLUF, speaker analysis with talk time, action items, decisions, follow-ups,
  multi-section stitching for long transcripts (`_chunk_transcript_intelligently` →
  `_summarize_section` → `_combine_sections`). Output languages: `core/constants.py:
  LLM_OUTPUT_LANGUAGES` (12: en es fr de it pt **nl** ru zh ja ko ar — no Hindi).
- **Speaker suggestions are never auto-applied.** `identify_speakers` returns confidence-scored
  predictions stored for manual verification (`tasks/speaker_identification_task.py`). Only
  tags/collections have an auto-apply path (`auto_label_service.auto_apply_suggestions`).

## User transcription settings

Per-user prefs (Settings → Transcription) are `UserSetting` key/value rows shaped by
`schemas/transcription_settings.py` and served from `api/endpoints/user_settings.py`
(`GET/PUT /user-settings/transcription`): source language + translate-to-English, LLM output
language, speaker behavior (`always_prompt` | `use_defaults` | `use_custom`), min/max speakers,
garbage-segment cleanup + threshold, VAD tuning; recording/audio-extraction live on sibling
routes. **Per-file overrides win** — `tasks/transcription/dispatch.py` takes `source_language`,
`translate_to_english`, and speaker counts at upload/reprocess time.

## Media URL ingestion (yt-dlp)

`media_download_service.py`. 1800+ platforms, no extra config — yt-dlp and a Deno runtime ship
in the backend image (`js_runtimes` at `/usr/local/bin/deno`, required since yt-dlp 2025.11 for
YouTube PO tokens; `_YOUTUBE_EXTRACTOR_ARGS` rotates player clients).

- Limits: **15 GB** (`max_filesize`, matches the upload limit) and **4 h** (`duration > 14400`).
- `create_user_friendly_error` maps raw yt-dlp errors → guidance via `AUTH_ERROR_PATTERNS` +
  `PLATFORM_GUIDANCE`. `RECOMMENDED_PLATFORMS = ["YouTube", "Dailymotion", "Twitter/X"]`.
  Vimeo / Instagram / Facebook / LinkedIn / Patreon usually need auth.

## Gotchas

- Settings that look like env vars are frequently **DB-backed** (`SystemSettings` /
  `UserSetting`) with coded defaults in `core/constants.py`. Check before adding an env var.
- GPU worker, CPU worker, and `celery-redaction` load different models — importing a
  model-loading service into a task on the wrong queue pulls weights onto the wrong device.
- Docs: `docs-site/docs/features/llm-integration.md`,
  `docs-site/docs/features/transcription.md`,
  `docs-site/docs/configuration/neural-search-setup.md`.
