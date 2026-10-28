<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="250">

  # Data Models Documentation
</div>

This directory contains the SQLAlchemy 2.0 ORM models (typed declarative: `Mapped[...]` + `mapped_column(...)`) that describe the database tables and relationships for OpenTranscribe. The models mirror the schema; **Alembic revisions in `backend/alembic/versions/` are the schema authority**, not these files (see `app/db/README.md`).

## 🗄️ Database Schema Overview

The core entities are users, media files, transcript segments and speakers:

```
┌─────────────┐     ┌─────────────┐     ┌──────────────────┐
│    User     │────▶│  MediaFile  │────▶│ TranscriptSegment│
└─────────────┘     └─────────────┘     └──────────────────┘
                           │                      │
                           ▼                      ▼
                    ┌─────────────┐        ┌─────────────┐
                    │ Comment/Task│        │   Speaker   │──▶ SpeakerProfile
                    │ FileTag/... │        └─────────────┘
                    └─────────────┘
```

Tenancy: user-owned tables (files, tags, collections, speaker profiles, ...) carry a nullable `organization_id` (NULL = personal). The `Organization` / `OrganizationMembership` tables are in `organization.py`.

## Model Files

Every model must be exported from `__init__.py`, the canonical import surface (a few speaker helper tables in `media.py` are imported from `app.models.media` instead).

### Core Models
- `user.py` - `User`: identity, role, auth type and external-identity columns, password/lockout/approval state
- `media.py` - the large core module: `MediaFile`, `TranscriptSegment`, `Speaker`, `SpeakerProfile`, `SpeakerCollection(Member)`, `SpeakerCluster(Member)`, `SpeakerMatch`, `SpeakerCannotLink`, `SpeakerProfileBlacklist`, `SpeakerCorrectionCarryover`, `Comment`, `Tag`, `FileTag`, `Task`, `Analytics`, `Collection`, `CollectionMember`
- `upload_batch.py` - `UploadBatch`: multi-file import tracking
- `topic.py` - `TopicSuggestion`: LLM-suggested tags and collections
- `file_facts.py` - `FileFacts`: deterministic ingest artifacts (facts, digest, keyphrases), a 1:1 sidecar of `media_file`
- `pipeline_timing.py` - `FilePipelineTiming`: end-to-end pipeline timing (epoch milliseconds)
- `custom_vocabulary.py` - `CustomVocabulary`: per-user vocabulary terms for transcription

### Authentication & Security
- `auth_config.py` - `AuthConfig`, `AuthConfigAudit`: DB-backed authentication provider configuration and its change history
- `user_mfa.py` - `UserMFA`: TOTP settings per user
- `refresh_token.py` - `RefreshToken`: a row is a session (idle and absolute expiry columns)
- `password_history.py`, `password_reset.py` - password reuse tracking and reset tokens
- `invitation.py` - `UserInvitation`, `EmailVerificationToken` (token hashes only)
- `scim_token.py` - `SCIMToken`: bearer credentials for the SCIM provisioning surface (digest only)
- `legal_acceptance.py` - `LegalAcceptance`: which user accepted which legal document version

### Organizations, Governance and Compliance
- `organization.py` - `Organization`, `OrganizationMembership`
- `organization_policy.py` - `OrganizationPolicy`: per-organization privacy switches and redaction floor
- `content_hold.py` - `ContentHold`, `ContentHoldFile`: organization legal holds on files or collections
- `takedown.py` - `TakedownNotice`, `TakedownNoticeFile`, `TakedownNotification`: durable takedown history
- `support_access.py` - `SupportAccessGrant`, `SupportAccessUse`: platform-staff access to tenant content
- `erasure.py` - `ErasureLedgerEntry`: GDPR Art. 17 erasure record
- `usage_event.py` - `UsageEvent`: usage event spine

### User Settings & Configuration
- `user_llm_settings.py` - `UserLLMSettings`: per-user LLM provider settings with encrypted API keys
- `user_asr_settings.py` - `UserASRSettings`: per-user ASR provider settings
- `user_diarization_settings.py` - `UserDiarizationSettings`: per-user diarization provider settings
- `user_media_source.py` - `UserMediaSource`: per-user media-source configuration for authenticated downloads
- `prompt.py` - `SummaryPrompt` and `UserSetting` (the per-user key/value settings store)
- `system_settings.py` - `SystemSettings`: system-wide admin key/value settings (coded defaults live in `core/constants.py`)

### Collaboration & Integrations
- `group.py` - `UserGroup`, `UserGroupMember`, `GroupMapping` (directory group to in-app group/privilege mapping)
- `sharing.py` - `CollectionShare`, `TagShare`
- `chat.py` - `ChatProject`, `ChatConversation`, `ChatMessage` (RAG chat)
- `watch_source.py` - `WatchSource`, `WatchSourceFile` (auto-import from local / S3 / SMB)
- `email_notification_config.py` - `EmailNotificationConfig`, `WatchSourceEmail`

### Mixins
- `mixins.py` - `UUIDMixin` and `TimestampMixin` (no model currently uses `UUIDMixin`; each declares its own `uuid` column)

## 👤 User (`user.py`)

Table `user`. Highlights (see the file for the full column list):
- **Identity**: `id` (integer PK), `uuid`, `email` (unique), `hashed_password`, `full_name`
- **Authorization**: `role` is one of `user`, `admin`, `super_admin` and is the single source of truth; `is_superuser` is a derived mirror enforced by a DB CHECK. `is_active` deactivates an account.
- **Auth method**: `auth_type` (`local`, `ldap`, `oidc`, `pki`, `proxy`, `saml`; CHECK-constrained), `allow_local_fallback`, and per-method identity columns (`ldap_uid`, `oidc_subject`, `saml_subject`, `pki_*`)
- **Account state**: `approval_status`, `email_verified`, `must_change_password`, `account_expires_at`, `last_login_at`, password timestamps
- **Relationships**: files, comments, speakers and profiles, collections, prompts, per-user settings (LLM/ASR/diarization), refresh tokens, MFA, group and organization memberships; most user-owned settings rows cascade on delete

## 📁 Media Models (`media.py`)

### MediaFile
Table `media_file`: one uploaded or imported recording (roughly 90 columns). Column groups:
- **Identity and ownership**: `id`, `uuid`, `user_id`, `organization_id`, `filename`, `storage_path`, `file_size`, `content_type`, `is_public`
- **Processing state**: `status` (`FileStatus`), `active_task_id`, `retry_count`, `last_error_message`, `error_category`, recovery fields, `cancellation_requested`
- **Media metadata**: `duration` (+ `duration_source`), `language`, `metadata_raw` / `metadata_important` (ExifTool output), video/audio technical fields (`resolution_*`, `frame_rate`, `codec`, `audio_*`), recorded-date fields with provenance, `title` / `author` / `description` / `source_url`
- **Derived assets**: `thumbnail_path`, `playback_path` (browser-playable rendition), `waveform_data`, `file_hash`, `imohash`
- **AI results**: `summary_data` (structured JSON summary), `summary_status`, `summary_opensearch_id`, `translated_text`, redaction status and coverage
- **How it was processed**: `whisper_model` / `requested_*` options, `asr_provider`, `asr_model`, `diarization_provider`, `diarization_model`, `embedding_mode`, `processing_provenance` (JSONB, deferred; written only by `services/file_provenance`)
- **Governance**: `is_quarantined` (+ `quarantine_*`, `pre_quarantine_status`), `takedown_origin`, `legal_hold`

`FileStatus` (`app/core/enums.py`): `pending`, `queued`, `downloading`, `processing`, `completed`, `error`, `cancelling`, `cancelled`, `orphaned`, `quarantined`. Quarantine is tracked by the independent `is_quarantined` flag; the enum value is the surfaced display status.

### TranscriptSegment
Table `transcript_segment`: `media_file_id`, `speaker_id`, `start_time`, `end_time`, `text` (always the original text), `words` (word-level timing JSON), `confidence`, overlap fields (`is_overlap`, `overlap_group_id`, `overlap_confidence`), and cached `redactions` / `toxicity` detection spans applied as a read-time transform.

### Speaker and SpeakerProfile
- `Speaker` (table `speaker`): a per-file speaker from diarization. `name` (e.g. `SPEAKER_01`), `display_name`, `suggested_name` + `suggestion_source`, `verified`, `confidence`, optional `profile_id` and `cluster_id`, and predicted gender/age attributes. Voice embeddings are stored in OpenSearch, not in this table.
- `SpeakerProfile` (table `speaker_profile`): a named identity that persists across files (`name`, `embedding_count`, `avatar_path`, attribute predictions). Speakers link to it through `profile_id`.
- `SpeakerCluster` / `SpeakerClusterMember`, `SpeakerMatch`, `SpeakerCollection(Member)`, `SpeakerCannotLink`, `SpeakerProfileBlacklist` and `SpeakerCorrectionCarryover` support clustering, matching, grouping and corrections.

### Task
Table `task`: background task tracking. Primary key `id` is a **string** (the Celery task ID); `task_type`, `status`, `progress`, `error_message`, `user_id`, `media_file_id`, timestamps. A partial unique index allows only one `pending` / `in_progress` row per `(media_file_id, task_type)`; writers resolve the conflict through `services/task_row_supersede.py` (see `models/CLAUDE.md`).

### Comment, Tag, FileTag, Analytics
- `Comment`: `media_file_id`, `user_id`, `text`, optional `timestamp` into the media
- `Tag`: tenant-owned label (`name`, `normalized_name`, `user_id`, `organization_id`, `source`); `name` is **not** globally unique. `FileTag` links tags to files and records `source` / `ai_confidence`.
- `Analytics`: one row per file with `overall_analytics` (JSON), `computed_at`, `version`
- `Collection` / `CollectionMember`: tenant-owned collections (`visibility`: `organization` or `restricted`), shared through `CollectionShare`

## 🔧 Model Patterns & Best Practices

### Identifiers
- **Hybrid ID system**: integer `id` for primary keys, foreign keys and joins; an indexed time-ordered UUIDv7 `uuid` (`app/utils/uuid7.py`) is the identifier exposed through the API
- **String PK** for `Task` (the Celery task ID)

### Foreign Keys & Relationships
- Explicit foreign key columns with constraints; SQLAlchemy `relationship()` for navigation
- When two foreign keys point at the same table, pass `foreign_keys=` on both sides or mapper configuration fails at import
- Cross-module model imports go under `if TYPE_CHECKING:` with string relationship targets
- Delete behavior is per table (`CASCADE`, `SET NULL`); read the model before assuming

### Timestamps
- `created_at` / `updated_at` where present; domain-specific times such as `upload_time`, `completed_at`

### JSON Fields
- Flexible structures (summary, metadata, analytics) use JSON/JSONB columns

### Secrets
- Stored in `encrypted_*` columns through `app/utils/encryption.py`

### Database Constraints
- NOT NULL, UNIQUE (often partial, per tenant), foreign keys and CHECK constraints; the DDL is owned by Alembic

## 🔍 Query Patterns

Queries live in `app/utils/db_helpers.py` and `app/services/`, not in the models. Prefer the helpers over hand-written filters so tenant and permission rules are applied (for example `get_user_files_query`, `apply_tenant_scope`, `PermissionService`).

```python
from app.models.media import FileStatus
from app.models.media import MediaFile

# A user's completed files (SQLAlchemy 2.0 select style)
files = db.scalars(
    select(MediaFile)
    .where(MediaFile.user_id == user_id)
    .where(MediaFile.status == FileStatus.COMPLETED)
).all()
```

### Performance Optimization
- Eager loading: `joinedload()` / `selectinload()` for related data
- Pagination for large result sets
- Indexes on frequently queried columns (declared in the Alembic revisions)

## 🧪 Testing Models

Model and migration tests live in `backend/tests/` (see `backend/README.md` for how to run the suite against the live stack). Prefer asserting against the database over constructing objects in memory: constraints, defaults and triggers only exist there.

## Migration Considerations

### Schema Authority
Alembic migrations (`backend/alembic/versions/`) are the sole authority for schema in all environments. The bootstrap migration (`v010_baseline.py`) creates the original schema and later revisions evolve it. (`database/init_db.sql` at the repo root is legacy reference only.)

### Schema Changes
The checklist is in `app/db/README.md` and `app/db/CLAUDE.md`: write an idempotent Alembic revision, update the model here (and export it in `__init__.py`), update the Pydantic schema in `app/schemas/`, add a detection guard in `app/db/migrations.py`, and test with `./opentr.sh reset dev` (deletes all data) as well as a restart over an existing database.

### Data Migration
- Additive changes are generally safe
- Destructive changes need careful planning
- Back up before major migrations
