<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="250">

  # Services Layer Documentation
</div>

The services layer holds the application's business logic, separate from API endpoints (HTTP concerns) and models (data). Endpoints and Celery tasks stay thin and call in here. This directory has roughly 110 top-level modules plus domain subpackages; this README is a map of them. Each large subpackage has its own `CLAUDE.md`, and `services/CLAUDE.md` holds the detailed rules for the trickier modules (erasure, speaker cleanup, storage, LLM features); read those before changing the module they cover.

## 🎯 Service Layer Principles

- **Business logic encapsulation**: keep complex operations out of endpoints
- **Reusability**: share operations between endpoints and tasks
- **One implementation per rule**: many modules exist specifically to be the single place a rule lives (permissions, tenancy, holds, takedown, redaction, provenance); do not add a second copy beside them
- **Transactions**: services take a `Session`; many never commit and leave that to the caller (check the module docstring)
- **Errors**: raise the exceptions in `app/core/exceptions.py` (mapped to HTTP statuses in `main.py`); endpoints raise `HTTPException`

```
API Endpoints / Celery tasks → Service Layer → Data Layer (Models / OpenSearch / MinIO / external APIs)
```

### Shape of a service
Most services are plain modules of functions that take a `Session`; a few are classes with static methods (`AnalyticsService`, `FormattingService`, `ErrorCategorizationService`, `SpeakerStatusService`, ...) and `LLMService` is an instantiated client. There is no shared base class.

## 📁 Module Map

### Subpackages
| Package | What it is |
|---|---|
| `asr/` | ASR provider abstraction: `base.py`, `types.py`, `factory.py`, `local_provider.py` plus one module per cloud vendor (AssemblyAI, AWS, Azure, Deepgram, Gladia, Google, OpenAI, pyannote.ai, Speechmatics). 10 providers (1 local + 9 cloud). Has `CLAUDE.md`. |
| `diarization/` | Diarization provider abstraction (`base`, `factory`, `local_provider`, `pyannote_provider`, `asr_adapter`). Has `CLAUDE.md`. |
| `search/` | Transcript chunking, indexing, hybrid BM25 + neural retrieval, fusion, index health, reindex, ML model management. Has `CLAUDE.md` with the cosine-score gotcha. |
| `opensearch_service/` | The OpenSearch speaker/voiceprint plane and transcript document store (package: `client`, `aliases`, `indices`, `repair`, `speaker_*`, `matching`, `profiles`, `clusters`, `transcripts`). |
| `chat/` | RAG chat: planner, retrieval, reranking, prompting, citations, redaction masking, map-reduce summarization, usage. Has `CLAUDE.md`. |
| `redaction/` | Content redaction (PII, toxicity, profanity): detectors, spans, export policy, reveal audit, LLM egress guard. Has `CLAUDE.md`. |
| `ingest_artifacts/` | Deterministic (no-LLM) per-file facts, extractive digest and keyphrases. Has `CLAUDE.md`. |
| `watch_sources/` | Auto-import from local folders, S3 buckets and SMB shares. Has `CLAUDE.md`. |
| `transcript_document/` | DOCX and PDF transcript documents (layout model plus two renderers). Has `CLAUDE.md`. |
| `captions/` | The single caption-cue segmenter behind the player track and every subtitle download. |
| `local_models/` | Local Whisper model library: cache inspection, downloader, per-worker loaded-model reporting. |
| `privacy/` | Organization privacy policy layer: who may have voiceprints and inferred gender. |
| `protected_media_plugins/` | Plugins for protected-media download providers (for example MediaCMS). |

### Media, storage and files
| Module | Purpose |
|---|---|
| `minio_service.py`, `storage_backend.py`, `storage_presign_identity.py`, `multipart_upload.py`, `storage_recovery_service.py` | Object storage (MinIO or native S3), presigning, browser-side multipart upload, orphan re-ingestion |
| `media_download_service.py`, `youtube_rate_limiter.py`, `protected_media_providers.py` | yt-dlp URL ingestion and rate limiting |
| `media_mirror_engine.py`, `media_mirror_service.py` | Incremental media mirror (MinIO to a configured S3 destination) |
| `playback_rendition.py` | Browser-playable AAC/M4A renditions for formats browsers cannot decode |
| `subtitle_service.py`, `video_processing_service.py`, `transcript_export_*.py`, `summary_export_service.py` | Subtitle files, burned-in subtitles, transcript and summary export |
| `file_cleanup_service.py`, `file_provenance.py`, `file_provenance_view.py`, `imohash_service.py`, `cache_management_service.py` | Stuck-file recovery and purge, per-file processing provenance, fingerprinting, derived-asset caches |
| `audio_segment_utils.py`, `voiceprint_extractor.py`, `voiceprint_orphan_sweep.py` | Audio helpers and voiceprint extraction/sweeps |

### Pipeline, tasks and progress
| Module | Purpose |
|---|---|
| `pipeline_plan.py`, `pipeline_plan_inputs.py`, `asr_switch_plan.py` | The one decision point for how a recording is processed (engines, queues, validity) |
| `asr_provider_policy.py` | Which cloud ASR providers users may choose |
| `task_detection_service.py`, `task_filtering_service.py`, `task_recovery_service.py`, `task_row_supersede.py`, `transcription_retry.py`, `post_processing_status.py` | Task health, listing, recovery, supersede rules, retry policy |
| `progress_tracker.py`, `notification_service.py`, `download_events.py`, `pipeline_timing_service.py` | EWMA ETA progress tracking, WebSocket notifications (`send_task_notification()`), download events, pipeline timing |
| `error_categorization_service.py` | User-facing error classification (fixed sentences; the raw exception never reaches a client) |
| `migration_lock_service.py`, `migration_progress_service.py` | Redis lock and progress for batch migrations |

### Speakers
`speaker_*_service.py`, `profile_embedding_service.py`, `speaker_matching_service.py`, `smart_speaker_suggestion_service.py`, `speaker_status_service.py`, `speaker_clustering_service.py`, `similarity_service.py`, `embedding_mode_service.py`, `native_embedding_client.py`, `metadata_speaker_extractor.py`, `speaker_attribute_*.py`, `speaker_cleanup.py`, `speaker_correction_carryover.py`, `speaker_label_backfill.py`, `speaker_profile_rename.py`, `speaker_rename_tracker.py`. Anything that deletes a speaker or moves one out of a cluster or profile must go through `speaker_cleanup.py` (see `services/CLAUDE.md`).

### LLM
| Module | Purpose |
|---|---|
| `llm_service.py` | Synchronous multi-provider client (OpenAI, vLLM, Ollama, Anthropic, OpenRouter, Bedrock, custom OpenAI-compatible); summarization, speaker identification, topic extraction, section chunking for long transcripts |
| `llm_bedrock.py`, `bedrock_settings.py`, `llm_stream.py` | Bedrock provider, its connection settings, streaming parsers |
| `llm_reasoning.py`, `llm_context_window.py` | Measured per-model reasoning off-switch and context window |
| `auto_llm_settings.py`, `ai_insights_companion.py`, `auto_label_service.py`, `topic_extraction_service.py` | Per-user automatic AI steps, auto-labelling, topic suggestions |

### Tenancy, access, governance and compliance
| Module | Purpose |
|---|---|
| `permission_service.py`, `collection_visibility.py`, `delete_permissions.py`, `platform_access.py`, `platform_bypass.py`, `organization_service.py` | Sharing, collection visibility, tenancy mode and platform-admin bypass |
| `hold_guard.py`, `content_hold_service.py`, `content_hold_queries.py` | Legal holds: platform and organization (`hold_guard` is the one answer to "is this file held") |
| `takedown_*.py` | Abuse/DMCA quarantine, takedown notice records and owner notifications |
| `content_audit.py`, `org_audit_view.py` | Content-access and governance audit events and the organization audit view |
| `support_access_*.py` | Support-access grants |
| `gdpr_erasure_service.py`, `erasure_ledger_service.py` | GDPR Art. 17 erasure and its ledger |
| `account_lifecycle_service.py`, `session_cap_service.py` | FedRAMP account-expiry and concurrent-session controls |
| `tenant_stamp_backfill.py` | Stamp legacy rows with a tenant |

### Identity and configuration
`auth_config_service.py`, `auth_mail_config_service.py`, `account_security_service.py`, `idp_group_mapping_service.py`, `directory_sync_service.py`, `scim_service.py`, `scim_group_service.py`, `scim_token_service.py`, `email_service.py`, `watch_email_service.py`, `watch_settings_service.py`, `system_settings_service.py`, `huggingface_token.py`, `usage_service.py`, `backup_service.py`, `backup_recovery.py`, `backup_alerts.py`, `opensearch_snapshot.py`, `redis_cache_service.py`. The auth methods themselves are in `app/auth/`.

### Tags, groups and analytics
`tag_service.py`, `tag_operations.py`, `tag_bulk.py`, `tag_collisions.py`, `tag_sharing.py`, `group_file_index_service.py`, `analytics_service.py`, `formatting_service.py`, `interfaces.py`.

## 🧩 Notable Modules

### Protocol interfaces (`interfaces.py`)
`Protocol` classes `StorageService`, `SearchService`, `CacheService` and `NotificationService` describe the service boundaries that `minio_service`, `opensearch_service`, `redis_cache_service` and `utils/websocket_notify` satisfy. Depend on the Protocol for new seams and for type hints in tests.

### Notification service (`notification_service.py`)
`send_task_notification()` is the unified wrapper used by tasks and services. Import it instead of calling the WebSocket layer directly. If an event names a media file, route it through `utils/websocket_notify.send_ws_event_for_file` so quarantined files are not disclosed.

### Progress tracker (`progress_tracker.py`)
Preferred progress tracking, using an EWMA (exponentially weighted moving average) for ETA estimation. Use it in new tasks rather than hand-rolled percentages.

### Transcription has no service module
There is no `transcription_service.py` (deleted in issue #450; it was imported by nothing). Dispatch is `app/tasks/transcription/dispatch.py`, the engine is `app/transcription/`, the providers are `services/asr/` and `services/diarization/`, and which stages run is decided by `pipeline_plan.py`.

### LLM service (`llm_service.py`)
A synchronous client on purpose (Celery tasks, no asyncio conflicts). `create_from_user_settings(user_id)` resolves a user's configuration and falls back to `create_from_system_settings()`; both can return `None` when no provider is configured, and callers must handle that. `LLMService.is_configured_for_user(db, user_id)` is the one answer to "is an LLM configured". Summaries use the BLUF format (action items, decisions, speaker analysis) with multi-section stitching for long transcripts, in the 12 output languages of `LLM_OUTPUT_LANGUAGES` (`core/constants.py`). Speaker-identification suggestions are never auto-applied. Details: `services/CLAUDE.md`.

### MinIO / object storage (`minio_service.py`)
Module-level functions (not a class) for upload, download, delete, presigned URLs and object tagging, plus a small `MinIOService` class for bucket-oriented operations. Media playback and downloads use presigned URLs: clients get them from `GET /files/{uuid}/stream-url` (playback) or `POST /files/{uuid}/prepare-download`, and the browser reads directly from storage (HTTP range requests included), not through an API byte proxy.

```python
from app.services import minio_service

minio_service.upload_file(file_content, file_size, object_name, content_type)
url = minio_service.get_file_url(object_name)            # presigned, inline (playback/thumbnails)
url = minio_service.get_presigned_download_url(          # presigned, forces download
    object_name, download_filename=..., content_type=...
)
minio_service.delete_file(object_name)
```

`storage_backend.py` owns the differences between bundled MinIO and native AWS S3 (`STORAGE_BACKEND`), and `storage_presign_identity.py` provides the least-privilege identity that signs browser-facing GET URLs. Every presigned URL expiry is clamped by `clamp_presigned_expiry`.

### File cleanup service (`file_cleanup_service.py`)
`FileCleanupService` detects files stuck in processing or pending states and recovers them (`run_cleanup_cycle`, `force_cleanup_orphaned_files(db, dry_run=...)`, `get_cleanup_statistics`), with recovery-attempt limits that mark a file `ORPHANED` and force-delete eligible. It uses `check_for_stuck_files()` and `recover_stuck_file()` from `utils/task_utils.py`. The module also holds the full file purge path (`purge_media_file`).

### Analytics service (`analytics_service.py`)
`AnalyticsService` (static methods `compute_analytics`, `save_analytics`, `refresh_analytics`, `compute_and_save_analytics`) computes per-file analytics: speaker talk time, interruptions, turn-taking, question frequency, speaking pace and silence ratio. `POST /files/{uuid}/analytics/refresh` triggers a recompute.

### Formatting service (`formatting_service.py`)
`FormattingService` turns raw rows into display-ready fields (`format_duration`, `format_bytes_detailed`, `format_speaker_name`, `format_transcript_segment` with read-time redaction, `get_status_badge_class`, ...). The API sends these pre-formatted so the SPA only renders.

### Speaker services
- `SmartSpeakerSuggestionService` consolidates profile and individual voice-match suggestions (profile matches win, duplicate names merge, unlabeled `SPEAKER_XX` names are filtered, refused identities are removed with `without_refused`).
- `ProfileEmbeddingService` maintains profile-level voice embeddings (`update_profile_embedding`, `add_speaker_to_profile_embedding`, `calculate_profile_similarity`).
- `SpeakerStatusService` computes verification status fields for display (`compute_speaker_status`, `add_computed_status`).

### Error categorization (`error_categorization_service.py`)
`ErrorCategorizationService.classify_failure()` is called once at the failure site; it stores a fixed user-facing sentence plus a retry category and logs the raw text server-side only. `UserErrorReason` is the user-facing vocabulary.

### Task filtering (`task_filtering_service.py`)
`TaskFilteringService.filter_tasks_by_criteria` applies status, age and date-range filters to task listings and adds computed display fields.

### OpenSearch (`opensearch_service/`, `search/`)
- **Cosine score conversion**: OpenSearch `cosinesimil` returns `(1 + cosine) / 2`, not raw cosine. Every kNN score read converts with `raw_cosine = 2.0 * hit["_score"] - 1.0`, and thresholds sent *into* OpenSearch need the inverse. Both directions are named functions in `utils/cosine_space.py`; call them instead of inlining the arithmetic.
- **Speaker index aliases**: speaker voiceprints live in versioned indices (`speakers_v3`, 512-dim; `speakers_v4`, 256-dim) behind a `speakers` alias. Use `get_write_index()` and `get_active_versioned_index()` from `opensearch_service/aliases.py`; do not reference the versioned names directly outside that package.
- **Transcript search** is `search/hybrid_search_service.py` over the `transcript_chunks` index (hybrid BM25 + neural with RRF). OpenSearch 3.4 has hybrid-query quirks documented in `search/CLAUDE.md`.

## 🔄 Service Integration Patterns

Cross-service operations are composed in the caller: an endpoint or task calls the permission-aware lookup, then the relevant services in order, and commits once. Deleting a file, for example, is `file_cleanup_service.purge_media_file` (storage objects, search documents, speaker aggregates, rows), guarded by `hold_guard`. Prefer these existing composites over re-implementing the sequence.

## 🧪 Testing Services

Service tests live under `backend/tests/` (`tests/unit/`, `tests/services/`, `tests/api/`) and run against the live stack's PostgreSQL (see `backend/README.md`). Test the real behaviour; mock only external network services (LLM providers, cloud ASR, SMTP), never the database. `./opentr.sh start dev --with-mock-llm` provides a canned OpenAI-compatible LLM for chat and summarization paths.

## 🔧 Adding a New Service

1. **Define the purpose**: one responsibility; check whether an existing module is already the single home for that rule
2. **Signature**: take a `Session` (and a tenant/user argument where relevant) rather than opening your own, unless it runs in a Celery task with no request session, in which case use `session_scope()`
3. **Errors**: raise `app/core/exceptions.py` types; never leak raw exception text to clients
4. **Tenancy and permissions**: go through `PermissionService`, `collection_visibility` and `utils/db_helpers` instead of hand-written filters
5. **Queues**: a service imported by a Celery task must not load models onto the wrong queue's worker (GPU, CPU and redaction workers load different models)
6. **Tests**: add unit tests with a failing case, then run `./scripts/run-backend-tests.sh tests/unit`

```python
from sqlalchemy.orm import Session

from app.models.user import User


def create_resource(db: Session, data: CreateSchema, user: User) -> Resource:
    """Create a new resource with validation."""
    resource = Resource(**data.model_dump(), user_id=user.id)
    db.add(resource)
    db.commit()
    db.refresh(resource)
    return resource
```
