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
- `MediaFile.status` is annotated non-Optional but declared `nullable=True` — intentional
  (legacy DDL); the annotation and the kwarg are allowed to disagree, the kwarg drives DDL.
- `MediaFile.is_quarantined` (DMCA/abuse takedown) is **independent of** `status`;
  `pre_quarantine_status` restores the prior value on release. A takedown never deletes rows.
- `TranscriptSegment.text` always holds the **original** text; `redactions`/`toxicity` are
  cached detection spans applied as a read-time transform.
