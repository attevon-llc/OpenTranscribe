# app/services — business-logic layer

## Purpose

Everything between the API/Celery entry points and the data layer. ~110 modules; endpoints and
tasks should stay thin and call in here. `interfaces.py` declares the structural `Protocol`
contracts (`StorageService`, `SearchService`, `CacheService`, `NotificationService`) that
`minio_service`, `opensearch_service`, `redis_cache_service`, and `utils/websocket_notify`
already satisfy — depend on the Protocol, not the concrete module, at new seams.

## Where things live

- **Search / retrieval** — `search/` (transcript chunks + hybrid/neural; **has its own
  CLAUDE.md** with the critical `cosinesimil` score gotcha), `opensearch_service.py` (the
  speaker/voiceprint kNN plane + file docs), `opensearch_summary_service.py`,
  `opensearch_snapshot.py`, `similarity_service.py`.
- **Speakers** — `speaker_*_service.py`, `profile_embedding_service.py`,
  `smart_speaker_suggestion_service.py`, `optimized_embedding_service.py`,
  `embedding_mode_service.py`, `metadata_speaker_extractor.py`.
- **Providers** — `asr/` and `diarization/`: `base.py` + `types.py` + `factory.py` + one file
  per vendor. Add a provider by adding a module and registering it in the factory; never
  branch on provider name at a call site.
- **Redaction** — `redaction/` (**own CLAUDE.md**). **Watch sources** — `watch_sources/`
  (**own CLAUDE.md**).
- **Media in/out** — `media_download_service.py` (yt-dlp), `media_mirror_*.py`,
  `protected_media_providers.py` + `protected_media_plugins/`, `minio_service.py`,
  `subtitle_service.py`, `formatting_service.py`.
- **Ops** — backup/recovery, cleanup, migration lock+progress, task detection/filtering/recovery,
  system settings, usage, GDPR erasure.

`README.md` in this directory is the older long-form tour; it drifts — trust the code.

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
