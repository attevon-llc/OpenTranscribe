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
- `group.py` — `UserGroup`, `UserGroupMember`, and (since `v376`) `GroupMapping`.
  `MAPPING_SOURCES` / `MEMBERSHIP_SOURCES` are the CHECK bodies' single source of truth
  (`*_SQL` built from the tuples); `v380` widened both to add `proxy` and `scim`.
  `sharing.py` holds `CollectionShare`. Sharing is per *collection*, never per file;
  `PermissionService.get_accessible_file_ids_subquery` is the single query that turns those
  grants into a file-id set (and applies the org gate).
- `user.py` — `role ∈ {user, admin, super_admin}` is the **sole authorization truth**;
  `is_superuser` is a derived mirror kept in sync on every write and enforced by a DB CHECK
  (migration v369). Never set it independently of `role`. `auth_type` is likewise
  CHECK-constrained (`v375`, value set swapped by `v378`).
- `invitation.py` — `UserInvitation` and `EmailVerificationToken`. Both store a **SHA-256 hash**
  of the token, never the token; both are single-use and expiring.
- `scim_token.py` — `SCIMToken` (`v380`): one row per provisioning integration, storing the
  **SHA-256 digest** of the bearer token and never the token. `created_by` is
  `ON DELETE SET NULL` so provisioning survives the issuing admin's departure.
- `refresh_token.py` — **a row here IS a session.** `last_activity_at` (idle),
  `absolute_expires_at` (hard ceiling, carried forward through rotation, never recomputed) and
  `oidc_id_token` (encrypted, for RP-initiated logout) were added by `v375`/`v378`. There is no
  second session store — a Redis `SessionManager` existed with zero call sites and was deleted
  rather than wired up.
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
- `file_facts.py` — `FileFacts` (`v390`), a **1:1 sidecar** on `media_file` holding the
  deterministic ingest artifacts (#383 Phase 2): `facts`, `digest`, `keyphrases` JSONB plus
  `generator_version` / `source_fingerprint` lifecycle state. A sidecar rather than columns on
  `media_file` because that row is ~70 columns and is loaded whole by every gallery page,
  while these have two readers — and because Stage 3 needs a narrow "which digests are stale"
  scan. Its FK is the schema's one deliberate `ON DELETE CASCADE` on a derived row, and it is
  **named explicitly** so the ORM declares the object Postgres actually enforces.
- `erasure.py` — `ErasureLedgerEntry` (`v389`, issue #442): one row per GDPR Art. 17
  erasure request. **Its schema is a security control, not a convenience.** It has no
  free-text column at all (every textual column is a short CHECK-constrained enum) and
  `counters` is JSONB behind `ck_erasure_ledger_counters_numeric`, which rejects any
  value that is not a JSON number — because a ledger holding the personal data it
  records the destruction of is not erasure. `subject_user_id` /
  `subject_organization_id` are **deliberately not foreign keys**: they name the rows
  being destroyed, and a `SET NULL` would erase the only key the reconciliation sweep
  has. Full rationale in `app/services/CLAUDE.md`.
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
- **`user.oidc_subject` is an OIDC `sub`, which is unique only per ISSUER.** The UNIQUE index on
  it is sound only while exactly one provider is configured; multi-provider means keying on
  `(iss, sub)`. The old column name asserted a global identifier, which is why `v378` renamed it
  rather than leaving it alone.
- **Two different "email verified" concepts, do not conflate them.** `user.email_verified` is
  proof that *this deployment* mailed the address and someone holding it came back — it gates
  local login when `require_email_verification` is on. `ExternalIdentity.email_verified`
  (`auth/external_sync.py`) records an *IdP's assertion* about an address and is what gates
  email-match account linking.
- **Nullable-and-un-backfilled is a deliberate pattern on the auth columns.**
  `refresh_token.last_activity_at` / `absolute_expires_at` and `user.password_changed_at` all
  treat NULL as "not recorded" rather than as "expired", so an upgrade does not sign everyone out
  or force every account through a password change.
- **`user_group_member.source`** ∈ `manual` | `scim` | `ldap` | `oidc` | `proxy` (`v376`,
  widened by `v380`), defaulting to `manual` — so the default *is* the backfill.
  `MEMBERSHIP_SOURCES_PROTECTED` (`manual`, `scim`) is never removed and never converted by a
  directory pass; the SCIM router likewise only removes `scim` rows. Whoever wrote the row
  owns it.
- `MediaFile.status` is annotated non-Optional but declared `nullable=True` — intentional
  (legacy DDL); the annotation and the kwarg are allowed to disagree, the kwarg drives DDL.
- `MediaFile.is_quarantined` (DMCA/abuse takedown) is **independent of** `status`;
  `pre_quarantine_status` restores the prior value on release. A takedown never deletes rows.
- `TranscriptSegment.text` always holds the **original** text; `redactions`/`toxicity` are
  cached detection spans applied as a read-time transform.
