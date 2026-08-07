# backend/app/models — SQLAlchemy 2.0 ORM models

## Purpose

Table/relationship definitions only. Queries live in `app/utils/db_helpers.py` and
`app/services`; Alembic (`backend/alembic/versions/`) — not these files — is the schema
authority. See `backend/app/db/CLAUDE.md`.

## Key files

- `media.py` (~925 lines — the deliberate exception to the ~300-line rule): `MediaFile`,
  `TranscriptSegment`, `Speaker`/`SpeakerProfile`/`SpeakerCluster`/`SpeakerMatch`, `Collection`,
  `Tag`, `Task`, `Analytics`, `Comment`. `SpeakerCannotLink` and `SpeakerProfileBlacklist` are
  defined here but **not re-exported** from `__init__.py` — import them from `app.models.media`.
- `sharing.py` — `UserGroup`, `UserGroupMember`, `CollectionShare`. Sharing is per *collection*,
  never per file; `PermissionService.get_accessible_file_ids_subquery` is the single query that
  turns those grants into a file-id set (and applies the org gate).
- `user.py` — `role ∈ {user, admin, super_admin}` is the **sole authorization truth**;
  `is_superuser` is a derived mirror kept in sync on every write and enforced by a DB CHECK
  (migration v369). Never set it independently of `role`.
- `system_settings.py` — the key/value table behind admin-tunable config. Coded defaults live in
  `core/constants.py` (`DEFAULT_*`), **not** in `.env`.
- `prompt.py` — `SummaryPrompt` plus `UserSetting`, the per-user key/value settings store.
- `pipeline_timing.py` — `FilePipelineTiming`; every `*_ms` column is epoch **milliseconds** so
  durations are plain subtraction.
- `organization.py`, `usage_event.py` — cloud-edition seam tables; empty in self-host.
  Authorization reads `OrganizationMembership`, never the token's org claim alone.
- `mixins.py` — `TimestampMixin` and `UUIDMixin`; UUIDMixin is currently unadopted (each model
  declares its own `uuid` column).
- `chat.py` — `ChatProject`, `ChatConversation`, `ChatMessage`. `ChatConversation.project_id`
  is **nullable** (NULL = ungrouped, which is every conversation created before `v376`) and its
  FK is **ON DELETE SET NULL, not CASCADE** — deleting a project must leave its conversations
  behind. The relationship therefore uses `passive_deletes=True` and deliberately NOT
  `delete-orphan`. `ChatProject.default_scope` / `has_scope` mirror `ChatConversation.scope` so
  the resolver reads either shape without a second code path.
- `__init__.py` — the canonical import surface. A new model must be added here.

## Conventions / patterns

- SQLAlchemy 2.0 typed declarative: `Mapped[...]` + `mapped_column(...)`. `Base` lives in
  `app/db/base.py` and intentionally has **no `naming_convention`** — adding one would rename
  every existing constraint, i.e. a schema change.
- **Hybrid ID system**: integer `id` for PKs/FKs/joins; an indexed UUIDv7 `uuid`
  (`app/utils/uuid7.py`, time-ordered for B-tree locality) is the *only* identifier exposed
  through the API.
- Enums come from `app/core/enums.py`; `media.py` re-exports `FileStatus` for back-compat only.
- Cross-module model imports go under `if TYPE_CHECKING:` with string relationship targets.
- Tenancy: nullable `organization_id` FK (NULL = personal) on user-owned tables. Filter through
  `utils/db_helpers.apply_tenant_scope` or `api/deps_context.scope_to_context` — never by hand.
- Secrets are stored in `encrypted_*` columns via `app/utils/encryption.py` (AES-256-GCM).

## Gotchas

- **Two FKs to the same table require an explicit `foreign_keys=` on BOTH sides**, or mapper
  configuration crashes at import time and the whole app fails to start. Current cases:
  `MediaFile.user_id`+`quarantined_by`, `SummaryPrompt.user_id`+`shared_by`,
  `CollectionShare.shared_by_id`+`target_user_id`, `WatchSource.user_id`+`created_by`,
  `AuthConfig.created_by`+`updated_by`, `SpeakerMatch.speaker1_id`+`speaker2_id`.
- **`Tag` is per-owner, and `Tag.name` is NOT unique** (migration `v374_add_tag_user_id`).
  `user_id IS NULL` = *system tag* (the seeded `Important`/`Meeting`/`Interview`/`Personal`,
  visible to everyone); non-NULL = that user's private tag. Uniqueness is two **partial** unique
  indexes — `uq_tag_user_name` on `(user_id, name) WHERE user_id IS NOT NULL` and
  `uq_tag_system_name` on `(name) WHERE user_id IS NULL` — because a plain composite `UNIQUE`
  would let duplicate system rows through (Postgres treats NULLs as distinct). Consequences:
  **never look a tag up by name alone** — scope by owner (`Tag.user_id == uid | Tag.user_id
  IS NULL`, ordered `Tag.user_id` so an owned row beats the system row) or join through
  `FileTag` for a specific file; and any tag you create in a background task must be attributed
  to the **file owner**, since an ownerless row is published to every account.
  `tag.user_id` is a plain FK, so user deletion must remove the rows (`admin._delete_user_owned_records`,
  `gdpr_erasure_service._delete_owner_scoped_rows`) before the `user` row goes.
- `MediaFile.status` is annotated non-Optional but declared `nullable=True` — intentional
  (legacy DDL); the annotation and the kwarg are allowed to disagree, the kwarg drives DDL.
- `MediaFile.is_quarantined` (DMCA/abuse takedown) is **independent of** `status`;
  `pre_quarantine_status` restores the prior value on release. A takedown never deletes rows.
- `TranscriptSegment.text` always holds the **original** text; `redactions`/`toxicity` are
  cached detection spans applied as a read-time transform.
