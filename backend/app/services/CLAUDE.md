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
  system settings, usage, GDPR erasure.

`README.md` in this directory is the older long-form tour; it drifts — trust the code.

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
