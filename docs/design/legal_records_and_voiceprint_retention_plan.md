# Legal acceptance records, a processing gate, and voiceprint retention: Design

Status: **PLAN ONLY.** Nothing here is implemented. Written 2026-11-01 against `master` @ `e6843db20`
(VERSION `v0.6.0`, Alembic head `v449_add_speaker_identity_source`). Target: the **v0.6.1 / v0.6.2**
patch line. Line numbers drift, so search for the quoted symbol.

Companions, which must be edited when this lands (see §9):
`docs/design/erasure_receipt_plan.md` (store inventory lists `legal_acceptance` as **D**),
`docs/design/append_only_audit_plan.md` (lists `legal_acceptance` as **exempt**, "erasure is meant
to delete it"). Both statements become false under WP-B.

Out of scope (v0.7.0): a consent checkbox per upload, and a voiceprint opt-out per file.

---

## 0. Summary and the release split

| WP | What | Schema | Behaviour change | Ship in |
|---|---|---|---|---|
| **A** | `legal_acceptance` gains `organization_id`, `organization_uuid`, `user_agent`, `locale`, `scope`; the accept endpoint records them; an org admin can accept for their organization | `v450` (additive, nullable) | None for existing callers | **0.6.1** (backend). Org-acceptance UI: 0.6.2 |
| **D** | The audit event for each acceptance is stamped with the org, so it appears in the org audit trail | none | The event becomes visible to org admins | **0.6.1** |
| **E1** | `speaker_profile.last_interaction_at` + backfill + stamping at the interaction sites | `v450` (additive, nullable) | None visible | **0.6.1** (column + stamping only) |
| **B** | Acceptance records outlive the user: FK `CASCADE` → `SET NULL`, pseudonymisation trigger, partial unique indexes, append-only trigger, retention sweep | `v451` | **Yes**: user deletion and GDPR erasure no longer delete these rows | **0.6.2** |
| **C** | Server-side "required legal documents before processing" gate (default OFF), enforced at every entry point that starts processing | none (SystemSettings rows) | Off by default; when on, refuses with a stable code | **0.6.2** |
| **E2** | Voiceprint purge scoped to files (`PurgeScope(kind="files")`) + opt-in retention sweep (24-month schedule) | none | Off by default | **0.6.2** |

### Why the split looks like this

0.6.1 is a security patch and is time-sensitive. It should carry only changes that cannot alter
what happens to existing data or requests:

- **A (columns) / D / E1 are strictly additive.** They add nullable columns, set new values on
  rows written from now on, and stamp an event field that already exists (`organization_id` on
  `audit_logger.log`). No row is deleted or refused that was not deleted or refused before.
  Rollback: the image rolls back cleanly, because the old code ignores the extra columns. The
  extra columns stay.
- **B changes what deletion does.** It touches the GDPR erasure contract
  (`gdpr_erasure_service.erase_user`), the deletion-residue test, the FK-coverage registry
  (`tests/unit/test_user_deletion_fk_coverage.py`), and two companion plans. It needs a legal
  decision (§4.6) that the code cannot make. A wrong answer here is a compliance regression,
  shipped inside a security patch.
- **C touches about 15 entry points** (§5.2), including Celery recovery sweeps. A mistake there
  either leaves a bypass (it fails open) or stops a retry loop (stuck files). It defaults OFF,
  but the shared gate function runs on every dispatch. So it needs the full gate
  (`run-integration-tests.sh` + e2e), not a patch-speed review.
- **E2 deletes biometric data.** It reuses the purge machinery, but adds a new scope kind to
  `PurgeScope`, the class every privacy purge goes through. It needs the deletion-residue and
  purge-residual suites run on hardware.

If 0.6.1 cannot take even WP-A, the smallest useful 0.6.1 is **D alone** (no migration). The
org audit stamp is a one-line change in `legal.py`.

### CLOUD_SEAM_VERSION (`backend/app/auth/constants.py`, currently 12)

- **0.6.1: no bump.** `registry.DocumentProvider`, `set_document_provider`, the dispatch hooks
  and `PurgeScope` are unchanged. `acceptance.record_acceptance` gains **keyword-only** parameters
  with `None` defaults, so existing positional callers keep working.
- **0.6.2: bump to 13.** There are three seam-visible changes. (1) A new core refusal,
  `LegalAcceptanceRequiredError` (a `DispatchBlockedError` subclass whose `detail` is a dict), is
  raised from `_run_pre_dispatch_gate` before hooks fire. An extension that installs its own
  document provider must know that a required key it does not serve makes processing fail closed
  (§5.4). (2) `legal_acceptance` rows now survive user and organization deletion, and an
  extension's own erasure flow can no longer rely on the cascade. (3) `PurgeScope` gains a
  `"files"` kind. Add a comment block above `CLOUD_SEAM_VERSION` in the existing style, and a row
  to the downstream-changes table (`docs/design/v060_downstream_changes.md` or its 0.6.x
  successor).

---

## 1. Current state (derived from code)

- `backend/app/models/legal_acceptance.py`: `LegalAcceptance(id, uuid, user_id NOT NULL FK user
  ON DELETE CASCADE, document_key, version, sha256, accepted_at, ip_address)`,
  `UNIQUE(user_id, document_key, sha256)` = `uq_legal_acceptance_user_doc_sha`.
- `backend/alembic/versions/v441_add_legal_acceptance.py`: raw `UPGRADE_SQL`, module-level for the
  consistency test. The FK is unnamed, so Postgres names it `legal_acceptance_user_id_fkey`.
  **Confirm this with `\d legal_acceptance` before writing v451.**
- `backend/app/legal/acceptance.py`: `find_acceptance`, `record_acceptance(db, user_id, doc,
  ip_address)` (idempotent through the UNIQUE key and an `IntegrityError` retry), and
  `operator_acknowledgment_status`.
- `backend/app/legal/registry.py`: `LegalDocument(key, title, version, status, audience,
  content)`, `sha256` over the exact bytes, the provider seam (`set_document_provider`,
  `is_default_provider`), `requires_acknowledgment` (only the operator acknowledgment, and only
  with the default provider). Vendored docs: `operator-acknowledgment`, `privacy-notice`,
  `terms-of-use`, `data-flows-annex`.
- `backend/app/api/endpoints/legal.py`: `POST /acknowledgments` already reads `user_agent` from
  `_get_client_info` but **does not store it**. It logs `AuditEventType.LEGAL_ACKNOWLEDGED`
  **without `organization_id`**, so `org_admin.org_audit_query` (which filters on the stamp or on
  membership attribution, `auth/audit.py` around line 778) shows it only by attribution.
  `GET /acknowledgments` **inner-joins `User`**, so a row with `user_id` NULL would vanish from it.
- `backend/app/api/endpoints/first_run_wizard.py`: reads `operator_acknowledgment_status`, and
  around line 590 runs a **raw** `DELETE FROM "user" WHERE id = :u`. That is a deletion path that
  bypasses every service, so a DB-level rule is the only one that covers it.
- `backend/app/services/gdpr_erasure_service.py`: `erase_user`, `erase_org_member_data`,
  `erase_organization`. `legal_acceptance` currently disappears through the cascade. Ledger
  precedent: `backend/app/models/erasure.py` names erased subjects by **surrogate keys only**
  (`id` + `uuid`) and rejects free text and email hashes (Recital 26).
- Org/user deletion FK coverage: `backend/tests/unit/test_user_deletion_fk_coverage.py` derives
  every FK into `user` that is **not CASCADE** and requires it to be registered with a reason.
  Changing this FK to `SET NULL` makes that test go red until the FK is registered. That is
  intended.
- Residue: `backend/tests/integration/test_deletion_residue_live.py` with `deletion_residue.py` /
  `deletion_seed.py`. The scans are derived from the schema.
- Dispatch: `backend/app/tasks/transcription/dispatch.py::_run_pre_dispatch_gate` is shared by
  `dispatch_transcription_pipeline` and `dispatch_batch_transcription` (#1169). Hooks in
  `tasks/transcription/hooks.py`: `DispatchBlockedError(HTTPException)`, default 403, `detail: str`.
- Settings: `backend/app/services/system_settings_service.py` (`get_setting`, `get_setting_bool`,
  `get_settings_map`). Coded defaults live in `backend/app/core/constants.py` (see the
  `PRIVACY_*` block around line 1850).
- Refusal convention: `HTTPException(detail={"code": "<snake_case>", ...})`. See
  `api/endpoints/privacy.py` (`policy_locked`, `purge_scope_not_allowed`).
- Retention job pattern **exists**: `backend/app/tasks/chat_retention.py` (`chat.retention_sweep`).
  It is driven by a DB setting (`chat.retention_days`, default 0 = off), deletes a bounded batch
  per tick (`MAX_DELETIONS_PER_RUN`), runs on the `UTILITY` queue, and is scheduled daily in
  `backend/app/core/celery.py` `beat_schedule` (04:10; 04:25/04:40/04:55 are also taken).
  **So E2 builds its sweep on this pattern.**
- Purge machinery: `backend/app/services/privacy/purge.py` (`PurgeScope`, `run_purge_pass`,
  `audit_purge`, two passes, `SECOND_PASS_DELAY_SECONDS`), `purge_voiceprints.py`
  (`purge_voiceprints_postgres`, `purge_voiceprint_docs`, `rebuild_trimmed_clusters`),
  `purge_residual.py` (`measure_voiceprint_residual`), `tasks/privacy_purge_task.py`.
- Speaker profile: `backend/app/models/media.py::SpeakerProfile`. It has `created_at`,
  `updated_at` (`onupdate=now()`, bumped by ANY write, including maintenance), and
  `last_embedding_update`. It has **no** interaction timestamp.
- There is no personal-access-token or API-key upload path in `backend/app/auth` or `api/` today.
  All API clients authenticate as a user and reach the same routes, so "API tokens" are covered
  by the route list in §5.2. SCIM tokens cannot upload.

---

## 2. Sequencing

```
0.6.1:  WP-A0 (v450 migration + model)  →  WP-A1 (record + endpoint)  →  WP-D (audit stamp)
        WP-E1 (column in v450, stamping)        [A1/D and E1 are independent after A0]
0.6.2:  WP-B (v451)  →  WP-B2 (erasure/admin/org listing)  →  WP-B3 (retention sweep)
        WP-C (gate; independent of B, but reads org acceptances from A)
        WP-E2 (files scope + sweep; depends on E1)
        WP-A2 (org-acceptance UI + i18n)
```

Migration numbers: **v450 = 0.6.1, v451 = 0.6.2.** Before writing either, re-derive the head with
`python3 scripts/release-tests/lib/alembic-head.py` (see the lane warning in
`backend/alembic/CLAUDE.md`). If 0.6.1 is cut from a release branch, v450 must be on `master`
**before** any other revision takes 450.

---

## 3. WP-A / WP-D / WP-E1: additive (0.6.1)

### 3.1 Migration `backend/alembic/versions/v450_legal_acceptance_context_and_profile_interaction.py`

`revision = "v450_legal_acceptance_context_and_profile_interaction"`,
`down_revision = "v449_add_speaker_identity_source"`. Module-level `UPGRADE_SQL` /
`DOWNGRADE_SQL`, as in v441, so the consistency test replays the real statements.

```sql
ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS scope VARCHAR(16) NOT NULL DEFAULT 'user';
ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS organization_id INTEGER
    REFERENCES organization(id) ON DELETE SET NULL;
ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS organization_uuid UUID;
ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS user_agent VARCHAR(512);
ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS locale VARCHAR(35);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_legal_acceptance_scope') THEN
    ALTER TABLE legal_acceptance ADD CONSTRAINT ck_legal_acceptance_scope CHECK (
      (scope = 'user') OR (scope = 'organization' AND organization_uuid IS NOT NULL));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_legal_acceptance_organization_id ON legal_acceptance (organization_id);
-- Org-scope idempotency: one acceptance per org per exact text.
CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_acceptance_org_doc_sha
    ON legal_acceptance (organization_uuid, document_key, sha256) WHERE scope = 'organization';

ALTER TABLE speaker_profile ADD COLUMN IF NOT EXISTS last_interaction_at TIMESTAMPTZ;
UPDATE speaker_profile SET last_interaction_at =
    GREATEST(created_at, last_embedding_update, updated_at)
  WHERE last_interaction_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_speaker_profile_last_interaction_at
    ON speaker_profile (last_interaction_at);
```

Notes:
- `scope = 'user'` with `organization_id` set means "a user accepted while acting in org X" (the
  context of a personal acceptance). `scope = 'organization'` means "an org admin accepted **on
  behalf of** org X". The two have different meanings, so they get two columns and are not
  inferred from `organization_id IS NOT NULL`.
- The existing `UNIQUE(user_id, document_key, sha256)` stays in 0.6.1. Consequence: an org admin
  who accepted personally cannot also hold a separate **user**-scope row for the same text. That
  is fine. An org-scope row has its own partial index. But the existing UNIQUE also covers
  org-scope rows, so **an admin of two orgs could not accept the same text for both.** Fix this
  in 0.6.1 by making the user UNIQUE partial: drop `uq_legal_acceptance_user_doc_sha` and create
  `uq_legal_acceptance_user_doc_sha` as a `UNIQUE INDEX ... (user_id, document_key, sha256) WHERE
  scope = 'user'`. The operation is safe on a populated table, because every existing row has
  `scope='user'` and already satisfies it. Do it in a `DO $$` block guarded by
  `pg_constraint`/`pg_indexes`, and do **not** use `CREATE INDEX CONCURRENTLY` (the migration runs
  in a transaction; the table holds admins' rows only, tens at most).
- The backfill is a single UPDATE over `speaker_profile`, normally thousands of rows, so it is
  fine inside the migration. `GREATEST` ignores NULLs in Postgres.
- `ON DELETE SET NULL` for `organization_id` keeps the record when an org is deleted.
  `organization_uuid` is the surrogate snapshot that still names it, on the erasure-ledger model.
- Downgrade (`DOWNGRADE_SQL`): drop the two new indexes, restore the plain UNIQUE constraint
  (`DELETE` duplicate `(user_id, document_key, sha256)` rows **keeping the oldest** first, and
  delete `scope='organization'` rows, which is documented data loss), then drop the columns and
  `speaker_profile.last_interaction_at`. Per `backend/alembic/CLAUDE.md`, there is no executing
  downgrade test. The operational rollback is the image rollback in §3.6.

Detection arm in `backend/app/db/migrations.py::_detect_schema_version()`:
- Add `has_legal_acceptance_context` = column `legal_acceptance.scope` **and** constraint
  `ck_legal_acceptance_scope` **and** column `speaker_profile.last_interaction_at`.
- Insert the arm at the TOP of the ladder, above the v449 arm (around line 777):
  `matches_v449 and has_legal_acceptance_context` → `"v450_..."`. Mirror the existing
  `matches_v442` style and factor `matches_v449` if v449's arm is inline.
- Add the version-context line in `backend/app/db/CLAUDE.md`, next to the v441 entry (line ~99).
- Test: `backend/tests/unit/test_v450_migration_consistency.py`, copied from
  `test_v442_migration_consistency.py` (down_revision assertion, replay of `UPGRADE_SQL` twice for
  idempotency, detection-arm assertion).

### 3.2 Model / schema

- `backend/app/models/legal_acceptance.py`: add `scope`, `organization_id` (FK `organization.id`,
  `ondelete="SET NULL"`), `organization_uuid`, `user_agent`, `locale`. Update `__table_args__` to
  the partial unique `Index(...)` objects (`postgresql_where=text("scope = 'user'")`), plus
  `CheckConstraint`. Update the class docstring: the "removed with the user (CASCADE)" sentence
  stays true **until v451**.
- `backend/app/models/media.py::SpeakerProfile`: `last_interaction_at: Mapped[datetime | None]`.
- `backend/app/schemas/legal.py`:
  - `LegalAcknowledgmentRequest`: optional `scope: Literal["user","organization"] = "user"` and
    `locale: str | None` (validated against BCP 47 shape `^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$`,
    max 35).
  - `LegalAcknowledgmentRecord` and `LegalAcknowledgmentAdminRecord`: add `scope` and
    `organization_uuid`.
  - `LegalDocumentSummary`: add `organization_acknowledged: bool` and
    `organization_acknowledged_at`.

### 3.3 `backend/app/legal/acceptance.py`

- `find_acceptance(db, user_id, doc)` is unchanged, with `LegalAcceptance.scope == "user"` added
  to the WHERE clause.
- New `find_org_acceptance(db, organization_uuid, doc)`.
- `record_acceptance(db, user_id, doc, ip_address, *, user_agent=None, locale=None,
  organization_id=None, organization_uuid=None, scope="user")`. Positional compatibility is kept.
  For the scope-`organization` path, the idempotency lookup uses `find_org_acceptance`, and the
  `IntegrityError` retry handles both partial indexes.
- Truncate `user_agent` to 512 server-side. Never trust its length.

### 3.4 `backend/app/api/endpoints/legal.py`

- Inject `ctx: RequestContext = Depends(get_current_context)` (`api/deps_context.py`).
- `scope == "organization"` requires `ctx.org_id is not None and ctx.org_role == "org:admin"`.
  Otherwise return 403 `detail={"code": "legal_org_acceptance_forbidden"}`.
  `operator-acknowledgment` cannot be accepted at org scope: 422
  `{"code": "legal_scope_not_supported"}`. Look up `organization.uuid` from `ctx.org_id`.
- User scope: store `organization_id = ctx.org_id` (the context) and `organization_uuid`.
- Pass `user_agent` (from `_get_client_info`) and `locale`. If the body omits `locale`, fall back
  to the first `Accept-Language` tag, validated the same way.
- `GET /documents`: populate the `organization_acknowledged*` fields when `ctx.org_id` is set.
- `GET /acknowledgments`: change the `join(User, ...)` to **`outerjoin`** now. It is a no-op until
  v451, and it removes a v451 trap. Org admins (not only platform admins) get an org-filtered
  variant: `GET /acknowledgments?organization=current`, which filters
  `organization_id == ctx.org_id`. Keep `get_current_admin_user` for the instance-wide list.

### 3.5 WP-D: audit event per acceptance

In `acknowledge_legal_document`, when `created`:
```python
audit_logger.log(event_type=AuditEventType.LEGAL_ACKNOWLEDGED, ..., organization_id=ctx.org_id,
    details={"document_key", "version", "sha256", "scope", "acceptance_uuid": str(row.uuid),
             "locale": row.locale})
```
- Do not add a new event type. `LEGAL_ACKNOWLEDGED` with `details.scope` is enough, and
  org-audit export (`api/endpoints/org_admin_audit.py`) picks it up through the stamp.
- Re-accepting (`created=False`) emits nothing. That is today's behaviour, and the test pins it.
- `acceptance_uuid` links the audit event to the row without copying the IP or email again
  (the audit event already carries `source_ip`/`username`, which is existing behaviour).

### 3.6 WP-E1: `last_interaction_at`

**Definition.** An *interaction* is an event that **creates, confirms or changes the link between
the voiceprint and a person, or a person's edit of the profile**:

| Counts | Site (search for the symbol) |
|---|---|
| Human assigns/unassigns a speaker to/from the profile | `api/endpoints/speakers.py` (`assign-profile`, the `speaker.profile_id = ...` sites ~1927/2342/2473/2534), `api/endpoints/speaker_update.py` (~126/171/331) |
| Human edits the profile (name, description, avatar set/delete, gender confirm) | `api/endpoints/speaker_profiles.py`: `PUT /profiles/{uuid}`, `POST/DELETE /profiles/{uuid}/avatar`, `POST /profiles/{uuid}/confirm-gender` |
| Human accepts or rejects a voiceprint suggestion for this profile | `speakers.py` reject path (`REASON_REJECTED`), `speaker_update.py` trigger accept |
| **Automatic** voiceprint match that **writes** a link (identity_source `voiceprint`/`voiceprint_link`) | `services/speaker_matching_service.py` (~631/975/1457), `services/speaker_clustering_service.py` (~1671/2162) — **David decides (§10 Q5)** |

| Does NOT count | Why |
|---|---|
| Viewing or listing a profile, `GET /profiles/{uuid}/occurrences`, showing a suggestion | Passive browsing would make retention indefinite and would turn a read into a write |
| Embedding recompute, cluster rebuild, re-embed or migration tasks, privacy purge, tenant backfill | Maintenance is not use. Today `updated_at` moves on all of these, which is why it cannot be reused |
| A suggestion merely computed (`smart_speaker_suggestion_service`) | No human or link outcome |

Implementation: a new module, `backend/app/services/speaker_profile_activity.py`:
```python
INTERACTION_KINDS = ("human_link", "human_unlink", "human_edit", "suggestion_decision", "auto_link")
def touch_profile_interaction(db: Session, profile_id: int | Sequence[int], kind: str) -> None:
    """Set last_interaction_at = now() (one UPDATE, no commit); the caller commits."""
```
Use a Core `update()` rather than ORM attribute assignment, so it neither bumps nor depends on
`updated_at` semantics. `kind` is validated against `INTERACTION_KINDS`, which documents intent
and lets `auto_link` be switched off by a constant if David says no. New profiles: set
`last_interaction_at = now()` at creation (`speakers.py` ~1927/2534 `new_profile`, and
`speaker_update.py` ~171).

Guard test: `backend/tests/unit/test_profile_interaction_sites.py`. AST-scan `backend/app` for
assignments to `.profile_id` on a `Speaker`, and for profile-mutating route handlers. Require each
enclosing function either to call `touch_profile_interaction` or to be listed in an `EXEMPT` dict
with a reason. Pattern: `test_user_deletion_fk_coverage.py`. Self-test: one must-fire fixture and
one must-stay-clean fixture.

### 3.7 Tests that must fail first (0.6.1)

Write them, watch them fail with the `git archive HEAD` red-check tree described in root
`CLAUDE.md`, then implement:

1. `backend/tests/api/endpoints/test_legal_acceptance_context.py`
   - The accept endpoint stores `user_agent` (truncated to 512) and `locale` (body, then the
     `Accept-Language` fallback; an invalid value gives 422).
   - Org-scope acceptance by `org:admin` creates a row with `scope='organization'`,
     `organization_uuid` set; repeating it is idempotent (same uuid).
   - Org-scope acceptance by an `org:member` gives 403 `legal_org_acceptance_forbidden`.
   - Org-scope `operator-acknowledgment` gives 422.
   - An admin of org X and of org Y can accept the same text for both (fails on the current
     plain UNIQUE).
2. `backend/tests/api/endpoints/test_legal_audit_org_stamp.py`: capture `audit_logger.log` calls.
   `organization_id == ctx.org_id`, `details.scope`, `details.acceptance_uuid`. No event on
   re-accept.
3. `backend/tests/unit/test_v450_migration_consistency.py` (see §3.1).
4. `backend/tests/unit/test_speaker_profile_activity.py`: a human assign sets
   `last_interaction_at`; `GET /profiles` does not; a profile embedding recompute
   (`profile_embedding_service`) does not.
5. `backend/tests/unit/test_profile_interaction_sites.py` (the guard, with its self-test).
6. Extend `frontend/src/lib/api/legal.ts` types only. There is no UI in 0.6.1, so no i18n is
   needed.

### 3.8 Verification and rollback (0.6.1)

- `./opentr.sh start dev` (the migration applies on startup). Check with `/db-shell`:
  `\d legal_acceptance` and `\d speaker_profile`. `./opentr.sh reset dev` **only on a `--fresh`
  stack** (`./opentr.sh start dev --fresh legal450`) to prove the full chain from scratch.
- `scripts/safe-precommit.sh run --all-files` plus `--hook-stage pre-push`;
  `python3 scripts/audit-tests.py backend/tests`; `./scripts/run-integration-tests.sh`.
- Upgrade rehearsal: the `rehearse` stage of `scripts/release.sh` (fresh + upgrade + lite).
- Rollback: deploy the previous image. v0.6.0 code ignores the new columns. The only v0.6.0
  constraint that changed is the user-scope UNIQUE, which became partial (strictly more
  permissive); v0.6.0 never writes `scope='organization'`, so it is unaffected. **No downgrade
  needs to run.**

---

## 4. WP-B: records outlive the user (0.6.2)

### 4.1 What is kept, what goes, and why

When the accepting user is deleted (admin delete, `erase_user`, `erase_org_member_data`, the
first-run wizard's raw delete, or any future path), the row **stays** and is pseudonymised in the
same statement:

| Column | After the user is deleted | Reason |
|---|---|---|
| `id`, `uuid` | kept | Surrogate keys of the record itself |
| `document_key`, `version`, `sha256` | kept | What was accepted. Not personal data |
| `accepted_at` | kept | When. Not identifying by itself |
| `scope`, `organization_id`, `organization_uuid` | kept | The organization is a legal person, so its acceptance is not personal data. For `scope='organization'` this row **is** the org's evidence and must outlive the admin who clicked |
| `locale` | kept | A language tag. Not identifying by itself |
| `user_id` | **NULL** (FK `ON DELETE SET NULL`) | The FK target is gone |
| `subject_user_uuid` (new) | kept, **David decides (Q1)** | The ledger model: a surrogate that is meaningless until a restore brings the row back. **Honest classification:** while any other system (backups, the erasure ledger `subject_user_uuid`, exported audit events) can map it to a person, the row is **pseudonymised personal data, not anonymous** (GDPR Recital 26). Keeping it is lawful only under a retention basis, so it carries a deadline (`retain_until`) |
| `ip_address`, `user_agent` | **NULL** | Device/network identifiers. They do not prove which text was accepted, and the audit event already recorded them for its own retention period |
| `subject_erased_at` (new) | set to `now()` | Starts the retention clock |
| `retain_until` (new) | `subject_erased_at + legal.acceptance_retention_days` | When the sweep deletes the row |

The lawful basis to cite in docs is **Art. 17(3)(e)** (establishment, exercise or defence of legal
claims), together with Art. 6(1)(c)/(f) for proving which terms applied. The project states the
mechanism, and **the operator states the basis in their own privacy notice**. The docs say so
explicitly. This repo does not give legal advice.

Option (simpler, more conservative, David decides, Q1): keep **only** `scope='organization'` rows,
and **delete** user-scope rows with the user (keep the cascade for those). That gives an org's
evidence without retaining anything about a person. Either option is supported by the trigger
below. The difference is one branch.

### 4.2 Migration `v451_legal_acceptance_survives_user_deletion.py`

```sql
ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS subject_user_uuid UUID;
ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS subject_erased_at TIMESTAMPTZ;
ALTER TABLE legal_acceptance ADD COLUMN IF NOT EXISTS retain_until TIMESTAMPTZ;
UPDATE legal_acceptance la SET subject_user_uuid = u.uuid FROM "user" u
  WHERE la.user_id = u.id AND la.subject_user_uuid IS NULL;
ALTER TABLE legal_acceptance ALTER COLUMN user_id DROP NOT NULL;
-- Swap the FK rule. NOT VALID + VALIDATE keeps the lock short on a populated table.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'legal_acceptance_user_id_fkey'
             AND confdeltype = 'c') THEN
    ALTER TABLE legal_acceptance DROP CONSTRAINT legal_acceptance_user_id_fkey;
    ALTER TABLE legal_acceptance ADD CONSTRAINT legal_acceptance_user_id_fkey
      FOREIGN KEY (user_id) REFERENCES "user"(id) ON DELETE SET NULL NOT VALID;
    ALTER TABLE legal_acceptance VALIDATE CONSTRAINT legal_acceptance_user_id_fkey;
  END IF;
END $$;
-- A live row names its user; a pseudonymised one says when.
ALTER TABLE legal_acceptance ADD CONSTRAINT ck_legal_acceptance_subject CHECK (
  (user_id IS NOT NULL AND subject_erased_at IS NULL)
  OR (user_id IS NULL AND subject_erased_at IS NOT NULL
      AND ip_address IS NULL AND user_agent IS NULL));       -- guarded by pg_constraint lookup
```

**Pseudonymisation trigger** (`BEFORE UPDATE ON legal_acceptance`, function
`legal_acceptance_guard()`). This is the core of the design. The FK action `SET NULL` is executed
as an UPDATE, so the trigger sees it:

- If `OLD.user_id IS NOT NULL AND NEW.user_id IS NULL`: force `NEW.ip_address = NULL`,
  `NEW.user_agent = NULL`, `NEW.subject_erased_at = now()`, and `NEW.retain_until = now() +
  make_interval(days => <retention>)`. The retention value is read inside the trigger from
  `system_settings` (`legal.acceptance_retention_days`), with the coded default if the row is
  absent. Allow it.
- Any other UPDATE that changes `document_key, version, sha256, accepted_at, scope,
  organization_uuid, subject_user_uuid, uuid`: `RAISE EXCEPTION` (append-only).
  `organization_id` may only go to NULL (the org FK's `SET NULL`).
- `BEFORE DELETE`: refuse unless `current_setting('opentranscribe.legal_retention_sweep', true) =
  'on'` **and** `OLD.retain_until <= now()`. The sweep `SET LOCAL`s that GUC. In single-role mode
  this is an accident guard, not a security boundary, which is exactly the stance of
  `append_only_audit_plan.md` Layer 1. Register `legal_acceptance` there.
- **If Q1 = "org rows only"**: the DELETE guard also lets through `scope='user'` rows when the
  `user` row is being deleted. Implement that by keeping a separate `BEFORE DELETE ON "user"`
  statement that deletes the user-scope rows first, under the GUC. Prefer Q1 = keep both.

**The first-run wizard's raw `DELETE FROM "user"`, admin delete, and every erasure path are then
correct without code changes.** That is why the rule lives in the DB rather than in
`_delete_owner_scoped_rows`.

Partial unique indexes after v451: the user-scope index from v450 is `(user_id, ...) WHERE scope =
'user'`. Postgres treats NULLs as distinct, so many pseudonymised rows for one document coexist.
That is correct: they are different, erased people.

**Safe on populated tables:** every statement is guarded and idempotent. The UPDATE backfill only
touches existing rows (admins only). `VALIDATE` scans a tiny table. There is no rewrite (`DROP NOT
NULL` and `ADD COLUMN` without a default do not rewrite).

**Reversible?** Only partially, and it must say so in its docstring. Downgrade: drop the triggers
and function, **delete** every row with `user_id IS NULL` (they cannot satisfy `NOT NULL`, so this
is irreversible data loss), restore `NOT NULL` and `ON DELETE CASCADE`, drop the three columns.
Operational rollback to a 0.6.1 image **without** a downgrade works: 0.6.1 code ignores the new
columns. Its `find_acceptance` filters on `user_id`, so it never sees pseudonymised rows, and
`GET /acknowledgments` uses an outer join since §3.4. One hazard: 0.6.1 code that deletes a user
leaves rows, which is the 0.6.2 behaviour anyway, enforced by the DB.

Detection arm: `has_legal_acceptance_survival` = trigger `trg_legal_acceptance_guard` present
**and** `confdeltype = 'n'` (SET NULL) on `legal_acceptance_user_id_fkey` **and**
`ck_legal_acceptance_subject`. Key on the trigger and the FK rule, not on a column. A column
without its trigger can keep an IP after erasure, so such a DB should re-run the revision. This is
the same reasoning as v389's CHECK-keyed arm.

### 4.3 Code changes

- `models/legal_acceptance.py`: `user_id` nullable, `ondelete="SET NULL"`, the three new columns,
  and a docstring rewrite (the "Rows are removed with the user" sentence goes).
- `legal/acceptance.py::record_acceptance`: set `subject_user_uuid` from the user.
- `api/endpoints/legal.py::list_legal_acknowledgments`: `user_email` becomes `str | None`. Add
  `erased: bool` and `retain_until` to `LegalAcknowledgmentAdminRecord`.
- `services/gdpr_erasure_service.py`: **no deletion code changes.** Add a summary counter
  `legal_acceptances_pseudonymised` (count before the user delete: `user_id = :u`). The ledger
  `counters` CHECK only admits numbers, which fits. `erase_organization`: the org FK `SET NULL`
  keeps rows, and `organization_uuid` still names the org. Document this in the docstring.
- `api/endpoints/admin._delete_user_owned_records`: no change (the DB acts).
- `tests/unit/test_user_deletion_fk_coverage.py`: register `legal_acceptance.user_id` with rule
  `SET NULL` and the reason "evidence record; pseudonymised by trg_legal_acceptance_guard
  (v451)". The test goes red first, which is correct.
- Residue (`tests/integration/deletion_residue.py` / `test_deletion_residue_live.py`): the
  **user-scope** residue scan must treat `legal_acceptance` as an expected survivor, with exact
  assertions, not an exemption. After `erase_user(u)`: `count(*) WHERE user_id = u` is 0; the rows
  previously belonging to `u` (selected by `subject_user_uuid`) have `ip_address IS NULL AND
  user_agent IS NULL AND subject_erased_at IS NOT NULL`; and no column of those rows contains the
  user's email, name or seeded IP (a generic string scan of the row's text cast, reusing the
  residue's "seeded marker" approach). Add a must-fire control: a seeded row with an IP left in
  place makes the scan red.
- `docs/design/erasure_receipt_plan.md` §1: change `legal_acceptance` from **D** to **N
  (de-attributed)**, with "retained to `retain_until`".
  `docs/design/append_only_audit_plan.md` table: move it from "exempt" to protected, with the
  lifecycle columns `user_id→NULL, ip_address→NULL, user_agent→NULL, subject_erased_at,
  retain_until, organization_id→NULL`.

### 4.4 Retention sweep `backend/app/tasks/legal_retention.py`

Modelled on `chat_retention.py`:
- Task `legal.acceptance_retention_sweep` on the `UTILITY` queue, `UtilityPriority.BACKGROUND`.
- `MAX_DELETIONS_PER_RUN = 500`. Delete `WHERE retain_until IS NOT NULL AND retain_until <=
  now()` under `SET LOCAL opentranscribe.legal_retention_sweep = 'on'`, ordered by
  `retain_until`, bounded.
- Route it in `core/celery.py` `task_routes`, add `"app.tasks.legal_retention"` to includes
  (around line 263), and add a beat entry daily at **05:10** (04:10–04:55 are taken).
- Setting `legal.acceptance_retention_days`: coded default in `core/constants.py`. **David picks
  the default (Q2).** Proposal: `2190` (6 years, a common limitation-period horizon). `0` would
  mean "delete at erasure", which the CHECK permits (retain_until = now()).
- No audit event per row. Log a count, and emit one `audit_logger.log` with event type
  `LEGAL_RECORDS_EXPIRED` and `details={"deleted": n}` when `n > 0`. Add the enum member in
  `auth/audit.py`.

### 4.5 Tests that must fail first (0.6.2, B)

1. `backend/tests/integration/test_legal_acceptance_survives_deletion.py` (real Postgres, marked
   `integration`):
   - (a) Admin delete of a user who accepted keeps the row, with `user_id` NULL, IP and UA NULL,
     `subject_erased_at` set.
   - (b) The same through `erase_user`, which also yields `summary["legal_acceptances_pseudonymised"] == 1`.
   - (c) The same through a raw `DELETE FROM "user"` (the wizard's path).
   - (d) `UPDATE legal_acceptance SET sha256 = ...` raises.
   - (e) `DELETE` without the GUC raises. With the GUC and `retain_until` in the future it raises.
     With it in the past it deletes.
   - (f) An org-scope row survives the deletion of both the admin and the org.
2. `backend/tests/unit/test_v451_migration_consistency.py`: replay twice, check `confdeltype` and
   the detection arm, and run it on a **populated** fixture with 3 rows, 2 users, 1 already
   accepted twice.
3. `backend/tests/unit/test_legal_retention_sweep.py`: bounded batch, honours `retain_until`, no-op
   when nothing is due.
4. Red first: `test_user_deletion_fk_coverage.py` (registration) and the residue test (expected
   survivor).

---

## 5. WP-C: required legal documents before processing (0.6.2)

### 5.1 Setting (DB-backed, never an env var)

- `legal.processing_required_document_keys` (`core/constants.py`:
  `LEGAL_PROCESSING_REQUIRED_KEYS_KEY`, default `""` = **off**). It holds a comma-separated list of
  document keys the registry serves (the existing `SystemSettings` string convention).
- `legal.processing_acceptance_scope`: `"user" | "organization" | "either"`, default `"either"`.
  In an org context, a matching org-scope acceptance by an org admin satisfies it for every
  member. In the personal workspace, only the owner's own user-scope acceptance does.
- Resolved in one query with `get_settings_map`, behind a short cache with the same TTL as
  `PRIVACY_WRITE_BACKSTOP_TTL_SECONDS`, so dispatch does not add a query per file in a batch.
  Cache **misses** read the DB; a cache **error** reads the DB.
- Admin location: Settings → Legal (`frontend/src/components/settings/LegalSettings.svelte`),
  super_admin only. Endpoint `PUT /api/legal/settings` (super_admin) and `GET /api/legal/settings`
  (admin) in `api/endpoints/legal.py`, which validates every key with `registry.get_document`.
  Writing requires `ROLE_SUPER_ADMIN`. It is audited as `SYSTEM_SETTINGS_CHANGED` or the existing
  settings-change event type (find the one the settings endpoints use).

### 5.2 Every entry point that starts processing

The single check, `backend/app/legal/processing_gate.py`:
```python
REASON_ACCEPTANCE_REQUIRED = "legal_acceptance_required"
REASON_DOCUMENT_UNAVAILABLE = "legal_document_unavailable"
REASON_GATE_UNAVAILABLE = "legal_gate_unavailable"
@dataclass(frozen=True)
class ProcessingRefusal: code: str; document_keys: tuple[str, ...]
def check_processing_allowed(db, *, owner_user_id: int, organization_id: int | None) -> ProcessingRefusal | None
def require_processing_allowed(db, *, owner_user_id, organization_id) -> None  # raises LegalAcceptanceRequiredError
```
- `LegalAcceptanceRequiredError(DispatchBlockedError)` in `tasks/transcription/hooks.py` (next to
  its parent). It uses `status_code=403` and `detail={"code": ..., "documents": [...]}`. Check that
  `DispatchBlockedError.__init__` accepts a dict `detail` (widen the annotation to `str | dict`).
  `HTTPException` already does.
- **The subject is the file's owner (`media_file.user_id`) and its tenant
  (`media_file.organization_id`), never the actor.** An admin retrying a member's file does not
  launder the member's missing acceptance, and an admin's acceptance does not cover a member in
  the personal workspace.

| # | Entry point | File / symbol | Where to check | Behaviour on refusal |
|---|---|---|---|---|
| 1 | Presigned upload, step 1 | `api/endpoints/files/prepare_upload.py` (`@router.post` ~307) | before the row and presigned URL are created | 403 code. No row, no bytes |
| 2 | Presigned upload, step 2 | `files/complete_upload.py::complete_upload` | before `dispatch_upload_pipeline_or_mark_error` | 403. The row is marked ERROR with the code (the bytes were already sent; the setting changed in between) |
| 3 | Direct upload | `files/__init__.py` `POST ""` (~179) → `files/upload.py` (~249 `dispatch_transcription_pipeline`) | at route entry | 403, before reading the body |
| 4 | Multipart | `files/multipart.py` (~76) | init | 403 |
| 5 | Reprocess | `files/__init__.py` `POST /{uuid}/reprocess` (~1303) → `files/reprocess.py` (~187) | route entry | 403 |
| 6 | **Rediarize** | `files/reprocess.py` (~401, `rediarize_task.apply_async`) | **This path bypasses `dispatch_transcription_pipeline`**, so call the gate explicitly | 403 |
| 7 | Retry / recover | `files/management.py` `/{uuid}/retry` (~270, dispatch at ~344), `/{uuid}/recover` (~378, ~668), the dispatch at ~836 | route entry and the dispatch gate | 403 |
| 8 | Bulk actions | `files/management.py::bulk_file_action` → `_process_single_file_action` | per file | that file's `BulkActionResult` carries `success=false`, `error_code=legal_acceptance_required`. Add `error_code` to the schema. The batch continues |
| 9 | Admin cleanup-orphaned | `files/management.py` `/management/cleanup-orphaned` (~1303) | dispatch gate | the file is marked ERROR with the code; it does not loop (see 13) |
| 10 | Task retry endpoints | `api/endpoints/tasks.py` (~547, ~754, ~956) | dispatch gate + route | 403 / per-file |
| 11 | URL ingest (single, playlist) | `files/url_processing.py` (~666 route; `process_youtube_url_task.delay` ~581, `process_youtube_playlist_task.delay` ~310) | **at the route, before yt-dlp downloads anything** | 403 |
| 12 | URL ingest worker | `tasks/youtube_processing.py` (~393 `dispatch_transcription_pipeline`) | dispatch gate (the setting may change while downloading) | file ERROR with the code |
| 13 | Recovery sweeps | `services/task_recovery_service.py` (~353), `utils/task_utils.py` (~700) | dispatch gate | file ERROR with the code. **The recovery selector must not re-select a file whose last error code is `legal_acceptance_required`** (check what it selects; add the exclusion) |
| 14 | Watch sources | `services/watch_sources/processing.py` (`_dispatch_pipeline` ~385, via upload's shared tail) | check **before import**, at scan time per source owner (`source.owner`), plus the dispatch gate | skip the source's scan this tick and log once per source per hour. No files are created that would only error |
| 15 | Batch dispatch | `tasks/transcription/dispatch.py::dispatch_batch_transcription` (~481 already catches `DispatchBlockedError`) | dispatch gate | per-file ERROR |
| 16 | **Backstop** | `tasks/transcription/dispatch.py::_run_pre_dispatch_gate` | **first**, before the duration ceiling and before hooks | raises. Every path in 2–15 that reaches dispatch is covered even if its route check is forgotten |

Decide (Q4): LLM post-processing of an already-transcribed file (`api/endpoints/summarization.py`
~152, `files/management.py` ~893, `files/reprocess.py` ~351 `summarize_transcript_task.delay`,
`files/crud.py` ~747 `redaction_detect_task.delay`, speaker re-identification). The recommendation
is **to gate them too** (rows 17–19), because a remote LLM is a data-egress event. But that makes
the feature bigger.

Explicitly **exempt**, with the reason recorded in the guard test's `EXEMPT` table: maintenance
re-embeds (`search_reembed_task`, `embedding_migration_v4`, `migration_pipeline`), search
reindexing, thumbnail/waveform regeneration, and privacy purges. They do not start processing of
new content for a person, and refusing them would break the stack's own maintenance.

**Guard test** `backend/tests/unit/test_processing_gate_coverage.py`. AST-scan `backend/app` for
calls to `dispatch_transcription_pipeline`, `dispatch_batch_transcription`,
`dispatch_upload_pipeline*`, `rediarize_task.apply_async/delay`, `process_youtube_*_task.delay`,
`summarize_transcript_task.delay` (if Q4 = yes), and every `@router.post/put` in
`api/endpoints/files/`. Each enclosing function must call `require_processing_allowed` /
`check_processing_allowed`, or reach `_run_pre_dispatch_gate`, or be in `EXEMPT` with a reason.
Self-test cases: must-fire and must-stay-clean.

### 5.3 Fail-closed semantics

- Setting empty or absent: the gate is off. **This is the community default. Zero queries beyond
  the cached settings read.**
- Setting non-empty and a listed key is not served by the current provider:
  `legal_document_unavailable`, refuse. A typo or a removed document never silently disables the
  gate.
- Settings or acceptance query raises: `legal_gate_unavailable`, refuse (HTTP 503 at routes; file
  ERROR at dispatch). Never `except Exception: return None`. Run `/fail-open-audit` over
  `processing_gate.py`.
- A document whose text changed (new sha256) needs a fresh acceptance. That is the existing
  `find_acceptance` semantics.
- Ordering inside `_run_pre_dispatch_gate`: the legal gate goes **first**, so a refused file
  neither reserves quota (the cloud hook) nor is marked ERROR for duration.

### 5.4 Interaction with the provider seam

When an extension has installed a provider, the gate uses `registry.get_document`, so it checks
the extension's documents. An extension that runs its own acceptance flow outside
`legal_acceptance` should leave the setting empty. This is the reason for the CLOUD_SEAM_VERSION
13 comment.

### 5.5 Frontend + i18n (12 locales; `ar` is RTL)

- `LegalSettings.svelte`: a "Require acceptance before processing" multi-select of served
  documents, plus a scope select (user / organization / either). Super_admin only.
- An org-acceptance section (WP-A2) for org admins: "Accept for <org>" with the accepted state.
- Upload, URL panel, reprocess, bulk results: map the code `legal_acceptance_required` to a
  message with a link to Settings → Legal. Files: the upload store/components that already handle
  402/403 (search for `QuotaExceeded`/`402` handling in `frontend/src`), plus
  `components/upload/MediaUrlPanel.svelte`.
- New keys under `legal.*` in **all 12** `frontend/src/lib/i18n/locales/*.json`, translated, not
  copied:
  `legal.processingGate.title`, `.description`, `.documentsLabel`, `.scopeLabel`, `.scopeUser`,
  `.scopeOrganization`, `.scopeEither`, `legal.orgAcceptance.title`, `.acceptForOrg`,
  `.acceptedBy`, `legal.errors.acceptanceRequired`, `.documentUnavailable`, `.gateUnavailable`,
  `.orgAcceptanceForbidden`, `legal.retention.erasedRecord` (admin list label for
  pseudonymised rows).
  Run `cd frontend && npm run check:i18n` manually: a commit that only touches locale JSON does
  not fire the hook. Check the `ar` rendering in a browser (the select and the link direction),
  with light and dark mode parity.
- `npm run check`, `npm run build`, vitest for `LegalSettings.test.ts` (new cases), and open it in
  the browser (DISPLAY=:11).

### 5.6 Tests that must fail first (C)

1. `backend/tests/unit/test_processing_gate.py`: off by default (no refusal); on + no acceptance
   gives `legal_acceptance_required` with the document list; on + owner's acceptance passes; org
   acceptance passes only when scope allows and only in that org; changed sha256 refuses; unknown
   key gives `legal_document_unavailable`; DB error gives `legal_gate_unavailable` (inject a
   raising session); the subject is the owner and not the actor.
2. `backend/tests/api/test_processing_gate_routes.py`: rows 1, 2, 3, 5, 6, 7, 8 and 11 each return
   403 with `detail.code`. Row 8 returns per-file `error_code`. Row 6 (rediarize) **fails on
   master**, which proves the bypass.
3. `backend/tests/unit/test_dispatch_legal_gate.py`: `_run_pre_dispatch_gate` raises before
   `fire_before_dispatch` (assert a registered hook was NOT called).
4. `backend/tests/unit/test_recovery_skips_legal_refusals.py` (row 13).
5. `backend/tests/unit/test_watch_source_legal_gate.py` (row 14: no `MediaFile` is created).
6. `backend/tests/unit/test_processing_gate_coverage.py` (the guard and its self-test).
7. E2E `backend/tests/e2e/test_legal_processing_gate.py` (marker `upload`): enable the gate
   through the API, upload, see the message, accept, upload succeeds, **restore the setting in
   `finally`** (e2e must not persist changes).

---

## 6. WP-E2: voiceprint purge by file + retention sweep (0.6.2)

### 6.1 `PurgeScope(kind="files")` in `services/privacy/purge.py`

- Add `"files"` to `ScopeKind`/`_SCOPE_KINDS`, and the field `file_ids: tuple[int, ...] = ()`.
  `__post_init__`: `files` requires a non-empty `file_ids` and no `organization_id`/`user_id`.
  `to_payload`/`from_payload` carry `file_ids` (a list).
- `speaker_filters()`: `[Speaker.media_file_id.in_(file_ids)]` (chunked by the existing `_CHUNK`
  via `_chunks`).
- `sql_filters(model)` for `files`: return `[false()]` for `SpeakerProfile`. **A file purge
  never deletes a profile row or a profile document directly.** The profile's centroid is
  instead **recomputed** from its remaining speakers. Add that step in `run_purge_pass` after
  `purge_voiceprint_docs`, via `services/profile_embedding_service.py`'s existing per-profile
  update (`_process_profile_with_embeddings` / `_process_profile_with_no_speakers`; expose a
  public `recompute_profile_embedding(db, profile_id)` if none exists). Affected profiles are
  `SELECT DISTINCT profile_id FROM speaker WHERE media_file_id IN (...)`, captured **before** the
  relational purge, into a new `VoiceprintTargets.recompute_profile_ids`.
- `org_stamp_clause`: return `None` for `files` (id-scoped only, like `user`).
- `targets_still_enabled` / `purge_allowed`: a `files` purge is a **retention destruction**. It
  runs whether or not matching is enabled, so callers of the files scope bypass the "policy
  still enabled" check. Make that explicit with a `reason="retention"` parameter, and do not
  special-case `kind`.
- `measure_voiceprint_residual` (`purge_residual.py`) must work for the files scope. Its queries
  go through `scope.speaker_filters()`, so check each one. Residual > 0 or `unverified` keeps the
  pass unfinished, as today.
- `audit_purge`: event details include `scope.kind="files"`, `file_count`, and
  `reason="retention"`. Do not include file names.

### 6.2 Sweep `backend/app/tasks/voiceprint_retention.py`

Following `chat_retention.py`, task `privacy.voiceprint_retention_sweep` (UTILITY queue, beat
daily at **05:25**):
- Setting `privacy.voiceprint_retention_months`, default `0` = off. The product's schedule is 24,
  set by the operator, and **David decides whether 24 becomes the coded default (Q6)**.
- Selection (Q7, recommendation): a file is due when **every** profile linked to one of its
  speakers has `last_interaction_at < cutoff` (an unlinked speaker counts as due) **and**
  `media_file.upload_time < cutoff`. A profile left with zero contributing speakers after a purge
  has its embedding cleared (recompute with no speakers) and is **listed**, not deleted. Deleting
  a named person record is a human decision. A profile with `last_interaction_at < cutoff`
  surfaces in a new admin count on the privacy page.
- Legal hold: skip files under an active hold (reuse the hold check used by
  `gdpr_erasure_service._purge_files`).
- Bounded: at most `MAX_FILES_PER_RUN = 200` files per tick, one `run_purge_pass` with the files
  scope, then the second pass after `SECOND_PASS_DELAY_SECONDS`, by reusing
  `tasks/privacy_purge_task.py`'s two-pass dispatch (call it, do not copy it).
- Idempotent: a purged file has no voiceprints, so the selection excludes files with no speaker
  embedding documents or rows (`speaker.embedding` markers; reuse what
  `measure_voiceprint_residual` counts).

### 6.3 Tests that must fail first (E2)

1. `backend/tests/unit/test_purge_scope_files.py`: validation; `speaker_filters` selects only those
   files; `sql_filters(SpeakerProfile)` matches nothing; payload round-trip.
2. `backend/tests/integration/test_voiceprint_file_purge.py` (OpenSearch + Postgres live, isolated
   `--fresh` stack): a purge of file A where profile P links speakers from A and B removes A's
   speaker docs, recomputes P's centroid from B only (assert that its vector equals B's
   embedding), leaves P's row, and leaves B untouched. Residual is 0.
3. `backend/tests/unit/test_voiceprint_retention_sweep.py`: off at 0; selection rule (fresh
   profile protects the file; legal hold skips it); bounded batch.
4. Extend `test_deletion_residue_live.py` only if a new store is touched (none is expected).

---

## 7. Docs-site pages

- `docs-site/docs/legal/legal-documents.md`: org acceptance, captured fields (IP, UA, locale), the
  retention of records after account deletion (exactly the table in §4.1), and the operator's
  responsibility to state the lawful basis.
- `docs-site/docs/features/privacy-controls.md`: voiceprint retention (definition of
  interaction, the 24-month setting, what a files purge deletes and what it recomputes).
- A new `docs-site/docs/configuration/legal-processing-gate.md`, or a section in the existing
  admin settings page under `configuration/` (check the sidebar). It covers the setting, the
  scope, the reason codes, and the entry-point list summarised from §5.2. Add it to
  `docs-site/sidebars*`.
- Developer guide: the extension-seam page (find it with `rg -l CLOUD_SEAM docs-site/docs`). Add
  seam 13.
- Run the docs-site drift checks per `docs-site/CLAUDE.md`.

## 8. CHANGELOG (`CHANGELOG.md`, Keep a Changelog, under `## [Unreleased]`)

- 0.6.1 — **Added**: legal acceptance records store organization, user agent and locale; org
  admins can accept for their organization (API); the acceptance audit event appears in the
  organization audit log; speaker profiles track their last interaction.
- 0.6.2 — **Changed**: legal acceptance records are kept, pseudonymised, after account deletion,
  until a configurable retention period (**a behaviour change; call it out**). **Added**: an
  optional server-side requirement to accept legal documents before processing, and opt-in
  voiceprint retention with destruction by file. **Fixed**: rediarize bypassed dispatch-time gates
  (if confirmed by test 5.6.2).

## 9. Repo bookkeeping

- `backend/app/db/CLAUDE.md`: version-context entries for v450 and v451.
- `backend/app/models/README.md`: the `legal_acceptance` line.
- `backend/app/services/privacy/` docstrings (`purge.py`'s `ScopeKind`).
- `backend/app/tasks/CLAUDE.md`: the two new beat tasks and the dispatch-gate ordering.
- `docs/design/erasure_receipt_plan.md`, `docs/design/append_only_audit_plan.md` (§4.3).
- `backend/app/auth/constants.py`: CLOUD_SEAM_VERSION 13 (0.6.2 only), plus the comment.
- Issues: one per WP, labels `security`/`backend`/`frontend`/`testing`, added to the Roadmap
  project, milestones `v0.6.1` / `v0.6.2` (root CLAUDE.md, Conventions).

## 10. Risks

1. **The FK rename assumption.** If `legal_acceptance_user_id_fkey` is not the live name, v451's
   guarded block silently does nothing and the cascade stays. Mitigation: look the constraint up
   by `conrelid`/`confrelid`/`conkey`, not by name, and the detection arm plus test 4.5.2 assert
   `confdeltype='n'`.
2. **The trigger reads `system_settings`.** If that read fails inside a user delete, the delete
   fails. Mitigation: `COALESCE` to the coded default, and never raise from the lookup.
3. **Recovery loops** when a file is refused by the gate (row 13). Mitigation: test 5.6.4.
4. **Cache staleness**: a setting just turned on lets work through for at most the TTL (10 s).
   This is documented and the same as the privacy backstop.
5. **Pseudonymised is not anonymous** (§4.1). The docs must not call these rows anonymous.
6. **Auto-link counted as interaction** (Q5) keeps frequently re-matched voices alive forever.
   That is the intended "in use" semantics, but say it in the docs.
7. **Profile centroid recompute after a file purge** could resurrect stale vectors if the
   recompute reads OpenSearch speaker docs that the purge is deleting in the same pass. Run the
   recompute **after** `purge_voiceprint_docs` and after a refresh. Test 6.3.2 asserts the vector.
8. **Concurrency of lanes** (root CLAUDE.md): `core/celery.py`, `constants.py`, the locale files
   and `migrations.py` are shared edit surfaces. Assign one writer per surface.

## 11. Decisions David must make

- **Q1**: After user deletion, keep **both** user-scope and org-scope acceptances (pseudonymised,
  with `subject_user_uuid`), **or** keep only org-scope rows and let user-scope rows go with the
  user? (Recommendation: both, with `subject_user_uuid`. Keeping it is honest only if the docs call
  it pseudonymised.)
- **Q2**: The default `legal.acceptance_retention_days` (proposal: 2190 = 6 years) and whether
  operators may set it to 0.
- **Q3**: Does 0.6.1 take WP-A + D + E1 (one additive migration), or D alone (no migration)?
- **Q4**: Does the processing gate also cover LLM post-processing (summaries, redaction detect,
  re-identification) of already-transcribed files? (Recommendation: yes.)
- **Q5**: Does an **automatic** voiceprint match that writes a link count as an interaction?
  (Recommendation: yes; views do not.)
- **Q6**: Is the 24-month voiceprint retention a coded default (on for everyone) or opt-in (0 =
  off, operator sets 24)? (Recommendation: opt-in in community.)
- **Q7**: The selection rule for the sweep (§6.2: every linked profile stale **and** the file
  older than the cutoff), and whether empty stale profiles are deleted or only listed.
  (Recommendation: listed.)
