# #1091 data-safety cluster: org trash, per-user hide, content transfer, delayed erase

**Status:** PLAN ONLY. Nothing here is implemented.
**Written:** 2026-11-07 against `master` @ `62a60c6da` (Alembic head `v461_add_privacy_purge_pending`,
derived with `python3 scripts/release-tests/lib/alembic-head.py backend/alembic/versions`).
**Issues:** #1091 (umbrella), #1101 (trash, restore, purge), #1102 (per-user hide), #1104 (transfer
org content on member removal), #1105 (owner-only delayed org/member erase). #1103 (delete
permissions) is **closed** (landed in #1119) and is a dependency, not work.
**Milestone:** v0.7.0, due 2026-11-30. Section 8 recommends a cut line.

Written for an implementation agent with **no** conversation history. Paths are relative to the
repo root; `app/` means `backend/app/`. Line numbers drift: search for the quoted symbol before
editing. Read these first, in this order, and do not duplicate them:

- `backend/app/services/erasure_receipt/CLAUDE.md` (ledger, receipts, self-service erasure rules)
- `docs/design/v060_org_content_governance_plan.md` §D-5..D-7 (holds; `hold_guard` is the one answer)
- `docs/design/self_service_erasure_and_reconcile_plan.md` §2 (D1 grace cap, D4 re-auth, D6 "ledger
  reuse, no second state machine")
- `backend/app/db/CLAUDE.md` "Adding a schema change" and `backend/alembic/CLAUDE.md`
- `backend/app/auth/CLAUDE.md` "Privilege tiers" (platform access matrix rule)

---

## 0. TL;DR

Four primitives, each built **once**; the four issues hang off them.

1. **One availability rule** (`app/services/file_availability.py`, new): "may this file appear on a
   normal read surface?" = not quarantined (unless the platform role reached the row) **and not
   trashed**. It replaces the quarantine-only predicates in `services/takedown_predicates.py` at
   every one of the ~60 call sites, so every surface that already hides a taken-down file hides a
   trashed one in the same change, and a guard test stops a new site reading the flags directly.
2. **Trash is three columns on `media_file`** (`deleted_at`, `deleted_by_user_id`, `purge_after`,
   plus `trash_origin`), not a table and not a status value. Purge stays `purge_media_file`. A
   restore/purge race is closed by one invariant on `purge_after` (§3.4), not by locks held across
   storage calls.
3. **OpenSearch learns trash through one indexed field** (`availability`, keyword) stamped by
   `update_by_query` and filtered inside `access_filter_clause`, the single ACL builder every
   transcript-plane reader already uses. The quarantine approach (a Postgres-resolved `must_not`
   set capped at 10,000 and failing closed with 503) is **not** reusable for trash: a 30-day org
   trash passes 10,000 rows and would put search into a permanent 503. The existing page-sized
   post-filter stays as the belt.
4. **Delayed erasure is the existing ledger grace mechanism, generalised.** v455 already gave
   `erasure_ledger` `scheduled_for`, `cancelled_at`, the `cancelled` status and the `grace_period`
   deferral for self-service erasure. #1105 needs a widened unique index, an executor that branches
   on `subject_type`, and the re-auth helper moved to a shared module. No new ledger columns.

Per-user hide is a separate small table (`user_hidden_media`) with a per-user cap enforced at write
time, so its OpenSearch exclusion set can never exceed the cap.

---

## 1. Verified current state, and which issue premises are wrong

### 1.1 What exists (reuse, do not rebuild)

| Concern | Where (verified at 62a60c6da) | Notes |
|---|---|---|
| Delete permission (#1103, done) | `app/services/delete_permissions.py:39` `can_delete_file(user, file, *, organization_id, is_org_admin, bypass)`; `get_deletable_file` `:99-155` | owner, `org:admin` of the file's org, or `bypass.allows(need="write")`. Denials audited as `FILE_DELETE_DENIED` |
| Single delete | `app/api/endpoints/files/cancel_upload.py:23` `DELETE /files/{uuid}` (204) → `app/api/endpoints/files/crud.py:240` `delete_media_file` | PENDING own file: light cancel + `purge_media_file` (`:90-130`). Else `delete_media_file` |
| Force delete | `app/api/endpoints/files/management.py:366-398` | `current_user.is_admin` only; does not pass `is_org_admin` |
| Bulk delete | `app/api/endpoints/files/bulk_actions.py` → `bulk_action_handlers.py:48-112` `_handle_delete_action` | per-file `FORBIDDEN` / `FILE_UNDER_LEGAL_HOLD` results |
| Canonical destroy | `app/services/file_cleanup_service.py:333` `purge_media_file` | refuses held files (`:399-412`); three phases with no transaction across storage; never raises |
| Holds | `app/services/hold_guard.py` (`hold_state`, `assert_not_held`, `held_pred`/`not_held_pred`, `refusal_detail`, `LEGAL_HOLD_ERROR_CODE`) | platform `media_file.legal_hold` + org `content_hold`/`content_hold_file` (v447). `tests/unit/test_hold_guard_single_source.py` forbids other reads of `.legal_hold` |
| Quarantine read rule | `app/services/takedown_predicates.py` (`exclude_quarantined` :52, `is_hidden_for` :75, `quarantined_among` :92, `quarantine_exempt`/`is_quarantine_hidden`/`quarantine_list_exempt` :118-156, notification suppression :159-260) | ~60 call sites, hand-placed; `test_quarantine_single_read_rule.py` |
| Search quarantine set | `app/services/search/quarantine_exclusion.py:21` `_QUARANTINED_UUID_CAP = 10_000`, raises `QuarantineExclusionUnavailableError` → 503 | global set, not per tenant |
| Search ACL | `app/services/search/access_filter.py:23` `access_filter_clause(user_id, organization_id)` | every transcript-plane reader builds its ACL here (`search/filters.py:133`, `suggestions.py:58,179`, `chat/aggregation.py:330`) |
| Retention | `app/tasks/cleanup.py:432` `_select_expired_files` (skips held + quarantined), `:502` `_purge_expired_files` (one session per file), `:551` `cleanup_expired_files` | beat `cleanup-expired-files` hourly in `app/core/celery_beat.py:72-75` |
| Beat + routes | `app/core/celery_beat.py` `BEAT_SCHEDULE` + `BEAT_ENTRY_EFFECT` (:218-245, `test_inert_checker.py`); `app/core/celery_routing.py` `TASK_ROUTES` (`test_task_routes_complete.py`) | issue text says `core/celery.py`; it moved |
| In-flight cancel | `app/utils/task_cancel.py:40` `cancel_active_task(db, file_id)` | cooperative, #823 |
| Erasure | `app/services/gdpr_erasure_service.py` `erase_user` :181, `erase_org_member_data` :354, `erase_organization` :541, `erase_file` :718 | skip held files, defer the ledger entry |
| Ledger | `app/models/erasure.py`: `SUBJECT_TYPES=(user, org_member, organization, file)`, `LEDGER_STATUSES` incl. `cancelled`, `DEFERRAL_REASONS` incl. `grace_period`; `scheduled_for`, `cancelled_at`, `request_digest`; `uq_erasure_ledger_open_self_request` (:229-234), `ix_erasure_ledger_due_grace` (:236) | append-only trigger; mutable columns in `app/db/audit_protection.py:89-107` |
| Self-service delayed erase | `app/services/self_erasure_service.py` (`request_self_erasure` :135, `cancel_self_erasure` :181, `_execute_one` :242, `execute_due_requests` :270); `app/api/endpoints/self_erasure.py` | executor phase 0 of `gdpr.erasure_reconcile` (daily 04:40) |
| Re-auth | `app/services/self_erasure_reauth.py` (`can_reauthenticate`, `verify_reauth(db, user, *, password, mfa_code)`) | password or TOTP in the request body; SSO-only without MFA → `reauth_unavailable` |
| Erasure receipt | `app/services/erasure_receipt/` (`stores_pg.py` PgStores + `EXEMPT_TABLES`; `stores_external.INDEX_PATTERNS`) | `tests/unit/test_erasure_inventory_coverage.py` derives the FK closure from `user`, `media_file`, `speaker`, `organization` |
| Audit outbox | `app/services/audit_outbox.py` `stage` :104 / `deliver_staged` :125; `app/services/content_audit.py:241` `stage_content_event(db, request, *, event, actor, resource, details, bypass)` | events in `app/auth/audit_events.py:16`; org read via `GET /org-admin/audit-logs` (`org_admin.py:142`, `auth/audit_query.py:34` scope clause) |
| Org model | `app/models/organization.py:33` (`external_org_id`, `is_active`; no owner), `:59` membership roles `org:admin`/`org:member` | `RequestContext.is_org_admin` (`app/services/request_context.py:33`), `require_org_admin` (`app/api/deps_context.py:214`) |
| Org erase endpoints | `app/api/endpoints/org_admin.py:222` `POST /org-admin/gdpr/erase-user/{uuid}`, `:262` `POST /org-admin/gdpr/erase-organization?confirm=true` | **immediate**, any `org:admin`, no re-auth |
| OpenSearch restamp helpers | `app/tasks/tenant_backfill_task.py:287` `_update_by_query`, `:269` `_speaker_indices`; `app/tasks/search_file_field_tasks.py:27` `update_file_access_index` | reuse for `availability` stamping and for #1104 |
| Platform access matrix | `docs/security/platform-access-matrix.tsv`, `scripts/generate-platform-access-matrix.py --write`, `tests/unit/test_platform_access_matrix.py` | every new route gets a row |

### 1.2 Issue premises that are wrong (correct the issue bodies when the lanes land)

1. **#1105: "erasure_ledger has no scheduling/cancel".** False since v455 (#1354): `scheduled_for`,
   `cancelled_at`, `cancelled` status and `grace_period` deferral exist. The proposed new columns
   (`cancelled_by`, `requested_by_reauth_at`) are **not** needed and would add actor data to a table
   whose rule is "no personal data"; the cancel actor goes in the audit event. What is actually
   missing:
   - `self_erasure_service._execute_one` (:242-267) calls `erase_user()` for **every** due grace
     entry without checking `subject_type`. A delayed `org_member` or `organization` entry would
     run as a whole-account erasure. The executor must branch.
   - `uq_erasure_ledger_open_self_request` is unique on `subject_user_id` with no `subject_type`
     filter, so a delayed member erasure collides with the same user's self-erasure request, and
     with the same person's erasure in a second org.
   - `scheduled_for` is **not** a ledger mutable column, so "reschedule" is refused by the trigger.
     Do not add rescheduling: cancel and re-request instead.
2. **#1105: "default 30 d for account deletion".** Conflicts with the shipped D1 decision: the
   self-erasure grace is capped at 21 days so grace + retry stays inside Art. 12(3)'s one month,
   whose clock (`sla_due_at`) starts at the request. Section 7, OD-4.
3. **#1104 conflates "member removed" with "account deleted".** A member removed from an
   organization keeps their account and their personal data; only the account deletion path
   erases personal data. And **core has no member-removal path at all**: `OrganizationMembership`
   mirrors an external IdP, nothing in core deletes it (`rg "OrganizationMembership\("` finds only
   the model), and `organization_service.py`'s docstring states that in the community edition the
   membership table is empty. So #1104 and the org parts of #1105 are reachable **only** in the
   managed edition; core supplies the service, the routes and a hook. Deactivation never touched
   data (`directory_sync_service._disable_user`, `account_lifecycle_service._disable_inactive_user`,
   SCIM `DELETE` is a soft disable), so no deactivation hook is needed.
4. **#1104 `media_file.original_owner_id`.** Not needed. "Uploader may restore" equals "owner may
   restore" until a transfer, and after a transfer the uploader is no longer a member. Attribution
   of the transfer lives in the audit event. Keeping the departed member's id on every file
   indefinitely is data the minimisation principle says not to keep. Dropped.
5. **#1091/#1102: "`get_accessible_file_ids_subquery` is the central choke point" and "hidden set
   applied the same way accessible ids already are".** It does **not** exclude quarantine (each of
   its ~10 callers adds the rule by hand), there is no single choke point (292 `MediaFile` query
   lines in ~165 files), and search access is **index-side** (`accessible_user_ids` +
   `collection_ids` in the documents), not a Postgres subquery.
6. **#1091 §6/#1101: "reuse the fail-closed cap behaviour of the quarantine exclusion".** Unfit for
   trash (TL;DR item 3). Use the indexed field; the issue itself named it as the follow-up.
7. **#1101 index `(organization_id, deleted_at)`.** The purge selector filters on `purge_after`;
   the index is `(purge_after) WHERE deleted_at IS NOT NULL`.
8. **Paths in the issue bodies have moved:** `utils/uuid_helpers.py` → `services/uuid_helpers.py`;
   `management.py:492` bulk delete → `files/bulk_action_handlers.py`; beat/routes → `core/celery_beat.py`
   / `core/celery_routing.py`; `users.py:808` user delete → `users_admin_edit.py:283` +
   `services/user_deletion_service.py:402`; "latest migration v397" → v461.
9. **"Audit delete/restore/purge"** presumes a successful delete is audited today. It is not: only
   `FILE_DELETE_DENIED` exists.

### 1.3 Defects found while mapping (fix in the lane that touches them; CHANGELOG, no new issue)

Quarantine read gaps the availability conversion (L1) fixes for free, because L1 routes these sites
through the new predicate:

- `files/filtering.py:455-511` `get_accessible_owners` (`GET /files/owners`) lists owners of
  quarantined files.
- `media_collections_listing.py:198-240` `list_collections_on_files` loads files by bare `get_by_uuid`.
- `api/endpoints/files/crud.py:80` `get_media_file_by_id` (used by `management.py:422`) has no
  quarantine check.
- `speaker_profiles.py:59` `_profile_media_instance_counts` and `clustering/queries.py:140` count
  quarantined files' speakers.

Trash-specific traps (each has a test in its lane):

- `utils/file_hash.py:83` `_only_live_files` and `url_video_import.py:76-92` would report a trashed
  file as a duplicate and block the re-upload (L2).
- `tasks/search_maintenance_task.py:379-382` re-indexes any completed file with segments and no
  documents. Trash therefore must **not** delete documents (it stamps them), or maintenance would
  re-add them unstamped (L3).
- The kNN speaker reads listed in §2.5 filter neither quarantine nor trash. L3 adds the trash
  filter; quarantine's index-side gap is recorded as a follow-up issue (it needs its own stamping
  on quarantine/release; §8).

---

## 2. Data model (one model for the whole cluster)

### 2.1 Decision: trash is a lifecycle state on the row, hide is a per-user relation

- **Trash is not a `status` value.** `status` is the processing state (`FileStatus`), and a
  quarantine already keeps `pre_quarantine_status` to restore it; overloading it a second time would
  lose the processing state on restore and break every `status IN (...)` filter.
- **Trash is not a separate table.** Every read path already loads `media_file`; a column predicate
  is index-friendly and needs no join. It also keeps the erasure inventory unchanged (new columns
  need no registration, new tables do).
- **Hide is a relation, not a column.** It is per user; a column cannot hold it.
- **Holds, quarantine and trash stay three independent states.** A file can be trashed and held,
  or trashed and quarantined; precedence rules are in §4.

### 2.2 Migrations (reserve v462-v466 for this cluster)

Re-derive the head immediately before writing each file and chain after whatever is there; the
numbers below assume nothing else lands first. L1 adds a row to the lane table in
`backend/alembic/CLAUDE.md` reserving v462-v466 for `v070/dsafety-*`, so parallel v0.7.0 lanes do
not fork the chain (the failure mode that file describes).

**v462_add_media_file_trash** (lane L1)

```sql
-- every statement idempotent (IF NOT EXISTS / DO $$ guards), SET LOCAL lock_timeout = '30s'
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL;
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS deleted_by_user_id integer NULL;
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS purge_after timestamptz NULL;
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS trash_origin varchar(16) NULL;
-- FK added NOT VALID then VALIDATE (scripts/guards/check_migration_safety.py rule)
ALTER TABLE media_file ADD CONSTRAINT fk_media_file_deleted_by_user
    FOREIGN KEY (deleted_by_user_id) REFERENCES "user"(id) ON DELETE SET NULL NOT VALID;
ALTER TABLE media_file VALIDATE CONSTRAINT fk_media_file_deleted_by_user;
ALTER TABLE media_file ADD CONSTRAINT ck_media_file_trash_origin
    CHECK (trash_origin IS NULL OR trash_origin IN ('user','retention')) NOT VALID;
ALTER TABLE media_file VALIDATE CONSTRAINT ck_media_file_trash_origin;
-- all-or-nothing: a trashed row has all three, a live row none (deleted_by may be NULL: system or SET NULL)
ALTER TABLE media_file ADD CONSTRAINT ck_media_file_trash_state CHECK (
    (deleted_at IS NULL AND purge_after IS NULL AND trash_origin IS NULL AND deleted_by_user_id IS NULL)
 OR (deleted_at IS NOT NULL AND purge_after IS NOT NULL AND trash_origin IS NOT NULL)
) NOT VALID;
ALTER TABLE media_file VALIDATE CONSTRAINT ck_media_file_trash_state;
```

- Partial index `ix_media_file_trash_due ON media_file (purge_after) WHERE deleted_at IS NOT NULL`:
  built **after** the upgrade with `CREATE INDEX CONCURRENTLY` through the shared
  `concurrent_indexes.ensure_concurrently` helper, exactly as v458 does (`migrations._retry_speaker_indexes`
  pattern; definition module beside `speaker_indexes.py`). The detection arm must **not** require it
  (`schema_detect/governance._OWN_RUNG_ONLY` precedent from v459).
- No backfill: every existing row is live (all four NULL).
- Detection marker: `ck_media_file_trash_state` (created last).
- `tests/unit/test_user_deletion_fk_coverage.py`: register `media_file.deleted_by_user_id` (SET NULL)
  with the reason "actor of a trash; the trashed row must outlive the actor".
- Downgrade: drop the constraints, index and columns. **Destroys the trash state** (trashed files
  become live again). Say so in the docstring; the consistency test may execute it (it drops only
  what this revision added, no user rows).

**v463_add_user_hidden_media** (lane L5)

```sql
CREATE TABLE IF NOT EXISTS user_hidden_media (
    user_id        integer NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    media_file_id  integer NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
    hidden_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_user_hidden_media PRIMARY KEY (user_id, media_file_id)
);
CREATE INDEX IF NOT EXISTS ix_user_hidden_media_media_file ON user_hidden_media (media_file_id);
```

- New table, so no CONCURRENTLY concern. Detection marker: `pk_user_hidden_media`.
- Erasure inventory: add a `PgStore("pg.user_hidden_media", ...)` keyed by `user_id` in
  `erasure_receipt/stores_pg.py` (it is the user's own data, reachable from `user`), unless the
  existing convention in that file puts pure CASCADE children in `EXEMPT_TABLES`; follow the
  convention `test_erasure_inventory_coverage.py` enforces and write the reason.
- Not an audit-protected table: classify in `app/db/audit_protection.EXEMPT_TABLES` if the registry
  test requires every table to be classified ("per-user view preference, no governance value").

**v464_generalise_erasure_grace_index** (lane L6)

```sql
DROP INDEX IF EXISTS uq_erasure_ledger_open_self_request;   -- downgrade re-creates it
CREATE UNIQUE INDEX IF NOT EXISTS uq_erasure_ledger_open_grace_subject
    ON erasure_ledger (subject_type, COALESCE(subject_user_id, 0), COALESCE(subject_organization_id, 0))
    WHERE deferred_reason = 'grace_period' AND status = 'deferred';
```

- `erasure_ledger` is small; a plain build is fine. `check_migration_safety.py` forbids DROP in
  `upgrade()` by default: this one replaces an index with a strictly wider equivalent, so either
  create the new index under the new name first and drop the old one in the same revision with a
  baseline entry and a written reason in `scripts/guards/baselines/migration_safety.txt`, or keep
  the old index and only add the new one if the guard refuses. Prefer keeping the old index
  **only if** it cannot collide (it can: org_member vs self request), so the drop is required.
- The append-only trigger is untouched (no new mutable column).
- Detection marker: `uq_erasure_ledger_open_grace_subject`.
- `self_erasure_service.get_open_request` and the model's `Index(...)` declaration change to the new
  shape; the race-safety of "one open request" is preserved per (subject type, user, org).

**v465_add_organization_owner** (lane L7)

```sql
ALTER TABLE organization ADD COLUMN IF NOT EXISTS owner_user_id integer NULL;
ALTER TABLE organization ADD CONSTRAINT fk_organization_owner_user
    FOREIGN KEY (owner_user_id) REFERENCES "user"(id) ON DELETE SET NULL NOT VALID;
ALTER TABLE organization VALIDATE CONSTRAINT fk_organization_owner_user;
-- backfill: earliest org:admin membership (created_at, then id) per org with no owner
UPDATE organization o SET owner_user_id = m.user_id
FROM (SELECT DISTINCT ON (organization_id) organization_id, user_id
      FROM organization_membership WHERE role = 'org:admin'
      ORDER BY organization_id, created_at, id) m
WHERE o.id = m.organization_id AND o.owner_user_id IS NULL;
```

- Community edition: no organizations, the UPDATE touches nothing.
- An org with no admin keeps `owner_user_id NULL`; the org-erase rule for that case is §5.3.
- Register the FK in `test_user_deletion_fk_coverage.py` (SET NULL, "an organization outlives its
  owner's account; ownership falls back per §5.3").
- Detection marker: `fk_organization_owner_user`.

**v466_add_org_content_transfer** (lane L8, see cut line)

```sql
CREATE TABLE IF NOT EXISTS org_content_transfer (
    id                 serial PRIMARY KEY,
    uuid               uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    organization_id    integer NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    from_user_id       integer NOT NULL,          -- NOT a FK: must outlive the account (ledger precedent)
    to_user_id         integer NULL REFERENCES "user"(id) ON DELETE SET NULL,
    actor_user_id      integer NULL REFERENCES "user"(id) ON DELETE SET NULL,
    status             varchar(16) NOT NULL,
    counts             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    index_restamped_at timestamptz NULL,
    CONSTRAINT ck_org_content_transfer_status CHECK (status IN ('index_pending','complete','failed')),
    CONSTRAINT ck_org_content_transfer_counts_numeric CHECK (
        jsonb_typeof(counts) = 'object'
        AND NOT jsonb_path_exists(counts, '$.* ? (@.type() != "number")'))
);
CREATE INDEX IF NOT EXISTS ix_org_content_transfer_pending
    ON org_content_transfer (created_at) WHERE status = 'index_pending';
```

- Numbers-only counts, enum status, no free text: the same rule as the erasure ledger.
- Erasure inventory: reachable from `organization` and `user`; classify (likely `EXEMPT_TABLES`
  "compliance record of a transfer, ids and counts only", mirroring `erasure_ledger`'s treatment).
- Detection marker: `ck_org_content_transfer_counts_numeric` (created last).

### 2.3 Schema detection, per revision

For each revision, in the same commit (steps from `backend/app/db/CLAUDE.md` §"Adding a schema change"):

1. Facts field + probe keyed on the marker above. `schema_detect/governance.py` is 521 lines
   (`check_file_size.py` CODE_SOFT=500, CODE_CAP=1000). L1 starts a new era module
   `app/db/schema_detect/data_safety.py` for v462 onward, prepended to `LADDER` in `ladder.py`, with
   the governance era's docstring range closed at v461. Every rung requires every earlier probe
   except the concurrently-built index (v462's partial index is post-upgrade).
2. Add the step to `_MODERN_CHAIN` in `tests/unit/test_schema_detection_ladder.py` and bump its
   length assertion and "last probe" anchors.
3. `tests/unit/test_v46N_migration_consistency.py` modelled on `test_v461_migration_consistency.py`
   (`down_revision`, vendor neutrality, `assert_detected_at_or_after`; never `== REVISION`).
4. Version-context entry in `backend/app/db/CLAUDE.md` and model/schema updates.

### 2.4 Settings (DB-backed `SystemSettings`, coded defaults in `app/core/constants.py`)

| Key | Default | Range | Who edits | Lane |
|---|---|---|---|---|
| `files.trash_retention_days` | 30 | 1-90 | admin (Settings → Data Management → Retention; extends `system_settings_service.get_retention_config`/`update_retention_config`) | L2 |
| `files.hidden_per_user_cap` (constant only, not a setting) | 10,000 | n/a | n/a | L5 |
| `gdpr.org_erasure_delay_hours` | 72 | 24-168 | super_admin (beside `gdpr.self_erasure_*` in `self_erasure.py`'s admin router) | L6 |
| `gdpr.org_member_erasure_delay_hours` | 72 | 24-168 | super_admin | L6 |

`purge_after` is stamped at trash time (`deleted_at + retention`), so lowering the setting never
retroactively purges files already in the trash, and raising it never extends them. The 168 h cap
keeps request + delay + one daily retry well inside the 30-day `sla_due_at` the ledger computes at
request time; do not change the SLA computation.

### 2.5 Every read path and what changes (T = exclude trashed, H = exclude hidden for this user)

The lane column says who changes it. "Predicate" means the call is converted to the
`file_availability` function that replaces the quarantine one; trash then comes for free.

**Per-resource choke points (T via predicate; 404 for trashed except on trash routes)**

| Site | Change | Lane |
|---|---|---|
| `services/uuid_helpers.py:248` `get_file_by_uuid_with_permission` (~70 callers: detail, stream, download, thumbnail, waveform, subtitles, transcript/doc export, summaries, topics, comments, tags, reprocess) | `is_hidden_for` → `is_unavailable_for(bypass, file)`; trashed is unavailable to **everyone** including a platform bypass (the trash routes use their own lookup, §3.3) | L1 |
| `services/uuid_helpers.py:495` `require_speaker_access` (:534) | same | L1 |
| `files/media_delivery.py:466-500` thumbnail route; `files/detail_response.py:149-169` `set_file_urls`; public-file branch `uuid_helpers.py:321` | same; public link to a trashed file → 404 | L1 |
| `comments.py:238/255`, `transcript_segments.py:286/302`, `watch_sources_files.py:93`, `delete_permissions.py:121/132` | same | L1 |
| `files/crud.py:80` `get_media_file_by_id` (gap §1.3) | add the predicate | L1 |

**Lists, counts, facets (T via predicate, H in L5)**

| Site | T | H |
|---|---|---|
| `files/listing.py:242` gallery list (+ count `:92`) | L1 | L5 (`hidden=exclude` default) |
| `files/filtering.py:619` `get_metadata_filters` (inline) | L1 | L5 |
| `files/filtering.py:455-511` `get_accessible_owners` (gap) | L1 | no (owner facet, counts not hidden-aware) |
| `user_files.py:106,155,204` dashboard counts/problem/recent | L1 | L5 (recent + problem lists; counts unchanged) |
| `tasks.py:101,141,425` task list | L1 | no |
| `media_collections_listing.py:68` `_visible_media_counts` | L1 | no (counts are shared facts) |
| `media_collections_listing.py:198-240` `list_collections_on_files` (gap) | L1 | no |
| `media_collections_membership.py:357` collection file list | L1 | L5 (same `hidden` param) |
| `media_collections.py:199` collection detail members | L1 | L5 |
| `tags/discovery.py:131`, `tag_collisions.py:355`, `tag_scope.py:198` (inline) | L1 | L5 for `list_files_for_tag` only |
| `utils/db_helpers.py:84,249,269`, `services/request_context.py:37` | appear unused: L1 runs `dead-code` check; delete if unused (CLAUDE.md: never two paths), else predicate | n/a |

**Search (T index-side in L3 + post-filter via predicate in L1; H in L5)**

| Site | Change |
|---|---|
| `search/access_filter.py:23` `access_filter_clause` | L3: return `{"bool": {"filter": [<acl>], "must_not": [{"term": {"availability": "trashed"}}]}}`. One edit covers `hybrid_search_service.py:247,512`, `chunk_retrieval.py:412,565`, `summary_plane_search.py:79`, `suggestions.py:58,179`, `chat/aggregation.py:330` |
| `search.py:217-221` → `search_helpers.py:198` `_drop_quarantined_search_hits` | L1: `quarantined_among` → `unavailable_among` (quarantined OR trashed), belt for the window before the stamp lands |
| `quarantine_exclusion.py` `_quarantined_file_uuids` users (`hybrid_search_service.py:534-545`, `suggestions.py:78,203`, `summary_search.py:203`, `search_helpers.py:181`) | unchanged for quarantine; trash is covered by the `access_filter_clause` edit |
| `HybridSearchService` process-local response cache | L2: trash/restore call `bump_corpus_version()` like `takedown_service.py:364` |
| H: all of the above | L5: `must_not {"terms": {"file_uuid": <caller's hidden uuids>}}` added beside the ACL in the search/suggestion/count builders (bounded by the per-user cap, so it cannot overflow `index.max_terms_count`), plus the post-filter. Not applied when the request is explicitly scoped to files the user picked |

**Chat / RAG (T via predicate + L3 index field; H for corpus-wide scope only)**

| Site | Change |
|---|---|
| `chat/context_resolver.py:50,84` `_visible_files_query` (inline); `:109` explicit files (choke point); tags `:188`, count `:283` | L1 predicate. L5: corpus-wide and tag scopes exclude H; explicitly chosen files are kept (hide is not access control) |
| `chat/context_prep.py:341-378` → `chat/evidence.py:63` `_drop_quarantined_hits` (chunks + digests) | L1 `unavailable_among`; L5 adds H for corpus-wide scope |
| `chat/aggregation_queries.py:116,156` `_accessible_scoped_files`; `aggregation_service.py:199`, `aggregation_speakers.py:86` (`_quarantined_among` :261/289) | L1 |
| `mapreduce/file_summaries.py:345`, `mapreduce/speaker_map.py:222`, `chat/speaker_roster.py:222` (inline) | L1 |
| `chat/citation_takedown.py:36,62` (persisted citations; `messages.py:175`, `export.py:264`) | L1: a citation to a trashed file renders as unavailable exactly like a quarantined one; on restore it comes back (the citation row is untouched) |
| `chat/language.py:262`, `aggregation_speakers.py:199` | over already-resolved uuids; no change |

**Exports (T via predicate)**: `transcript_document/build.py:114`, `subtitle_service.py:465`,
`tasks/media_download.py:63` (bulk download worker), plus the choke-point exports above. L1.

**Speakers, clustering, voiceprints**

| Site | Change |
|---|---|
| `speakers_list.py:395`, `speakers_list_filters.py:212`, `speakers_occurrences.py:124,188` (inline), `speaker_matching/profiles.py:119`, `speaker_profiles.py:468`, `clustering/verification.py:91`, `speaker_clusters.py:255` | L1 predicate |
| Gaps: `speaker_profiles.py:59`, `clustering/queries.py:140` + detail/members, `clustering/similarity.py:265`, `recluster.py:45`, `recluster_profiles.py:229`, `cluster_identity.py:94`, `centroids.py`, `smart_speaker_suggestion_service.py:55`, `speaker_matching_service.py:80`, `speaker_profiles_matching.py:344` | L1 predicate (fixes quarantine too) |
| kNN: `opensearch_service/matching.py:54`, `speaker_matching/match_storage.py:226`, `similarity_service.py:79`, `opensearch_service/speaker_metadata.py:22`, `speaker_collections.py:152`, `opensearch_service/profiles.py:337`, `clusters.py:131`, `speaker_read.py:261` | L3: add `must_not availability=trashed` to each query body via one helper `speaker_query_filters.exclude_unavailable_clause()` (new, in `opensearch_service/`), and an AST guard that every `knn` body in that package calls it |
| `profile_embedding_service.py:373` (profile centroid over every speaker) | **Not recomputed on trash.** A trashed file's voice stays in a profile centroid until purge, where `purge_media_file` step 5 already recomputes it. Rationale: recomputing on trash and again on restore doubles voiceprint churn for a reversible state, and a centroid exposes no content. Documented in the trash docs page. Matching against the trashed file's own speaker docs is excluded (row above) |
| `speakers_debug.py` | admin debug; no change |

**Notifications / WebSocket (T)**: `utils/websocket_notify.py:96` `send_ws_event_for_file` and
`services/notification_service.py:67` dispatch to the notification predicates, which L1 extends to
"quarantined or trashed" (a trashed file's processing events are dropped for everyone, including the
owner: the in-flight task was cancelled at trash time). Direct callers (`tasks/transcription/notifications.py:90,179,251,314`,
`tasks/thumbnail.py:116`, `video_processing_service.py:121`, `tasks/rename_digest_task.py:173`) need
no edit once the predicate changes. `test_ws_event_quarantine_discipline.py` keeps guarding raw
`send_ws_event`. The trash/restore events themselves (L2) are sent with the plain `send_ws_event`
to the owner and the org admins, added to that test's allowlist with the reason "announces the
trash state itself". Hide sends nothing.

**Workers and maintenance (T)**

| Site | Change |
|---|---|
| Per-file workers (`tasks/summarization.py:397`, `summarization_inputs.py:184`, `analytics.py:39`, `topic_extraction.py:94/142/242`, speaker tasks, `rediarize.py:223/532`, `waveform_generation.py:51`, `transcription/preprocess.py:201/608`, `pipeline_error.py`, `migration_pipeline.py:138`, `youtube_processing.py:270`) | No per-site edit. Trash cancels the active task (`cancel_active_task`), reprocess/retry go through the choke point (404), and `recovery_tasks.py:328` (already on the predicate) stops recovery re-dispatching a trashed file |
| `tasks/search_maintenance_task.py:379-382`, `services/search/reindex_scope.py:66,105`, `tasks/reindex_task.py`, `search/index_retry.py:132,164`, `api/endpoints/search_reindex.py:270,374` | L3: every document writer stamps `availability` from the row (`trashed` when `deleted_at IS NOT NULL`), so a reindex of a trashed file writes stamped documents instead of live ones |
| `tasks/opensearch_integrity_task.py:62-155` | **No change and a test that pins it**: a trashed row exists, so its documents are not orphans. L3 adds a regression test that a trashed file's documents survive the integrity sweep |
| `services/voiceprint_orphan_sweep.py`, `tasks/storage_orphan_sweep_task.py` | row-existence based; no change |
| Retention `tasks/cleanup.py:432` | L2, §3.5 |
| Duplicate detection `utils/file_hash.py:83` `_only_live_files`, `url_video_import.py:76-92` | L2: exclude trashed rows (a re-upload of a trashed file creates a new file). Watch sources: L2 verifies with a test that a trashed watch-source file is not re-imported on the next scan if a hard-deleted one is not re-imported today (parity with current behaviour), and fixes whichever way differs |

**Analytics / admin stats**: `utils/stats_helpers.py:97-130` `get_file_stats` (admin totals),
`tasks_system_health.py:71`: count trashed separately (`trashed_files`, `trashed_bytes`) rather than
excluding them, because trashed files still occupy storage until purge. L2. There is no per-user
storage quota query today; quota accounting is out of scope.

**Erasure, account deletion, transfer (must INCLUDE trashed rows)**: `gdpr_erasure_service`,
`user_deletion_service`, `erasure_receipt` capture and `load_account_purge_plans` select by owner/org
with no availability predicate, which is correct: an erasure must destroy trashed files too and must
not wait for the trash window. L1 adds a guard test (§6) that these modules never call the
availability predicate.

---

## 3. Trash (#1101)

### 3.1 Service (`app/services/trash_service.py`, new, one concern)

```python
def trash_files(db, files: Sequence[MediaFile], *, actor: User, origin: Literal["user","retention"],
                request: Request | None, bypass: PlatformBypass) -> TrashResult
def restore_files(db, files, *, actor, request, bypass) -> RestoreResult
def mark_for_immediate_purge(db, files, *, actor, request, bypass) -> list[int]   # sets purge_after = now()
def purge_due(limit: int = 200) -> PurgeSummary                                    # beat body
```

- **Trash** (one transaction for the batch): refuse held files via `hold_guard.assert_not_held`
  (same `FILE_UNDER_LEGAL_HOLD` refusal as today's delete; governance D-7 says a hold blocks
  deletion and the trash is the deletion entry point). Quarantined files may be trashed (the
  quarantine stays; the takedown record is untouched). Set `deleted_at = now()`,
  `deleted_by_user_id = actor.id` (NULL for retention), `purge_after = now() + trash_retention_days`,
  `trash_origin`. Stage `FILE_TRASH` per file through `content_audit.stage_content_event` in the
  same transaction. After commit: `deliver_staged`, `cancel_active_task` per file with an active
  task, stamp OpenSearch (L3 hook, a no-op until L3 lands), `bump_corpus_version()`, invalidate the
  owner's Redis file caches (the same calls `purge_media_file` step 4 makes), send the trash WS
  event.
- **Restore**: `UPDATE media_file SET deleted_at=NULL, deleted_by_user_id=NULL, purge_after=NULL,
  trash_origin=NULL WHERE id = ANY(:ids) AND deleted_at IS NOT NULL AND (purge_after > now() OR
  <held> OR is_quarantined)` (§3.4). Rows the UPDATE did not touch come back as per-file
  `NOT_RESTORABLE` results. Stage `FILE_RESTORE`. After commit: unstamp OpenSearch, bump corpus
  version, WS event. A restored file returns with its previous `status` (trash never touched it);
  a file whose processing was cancelled at trash time comes back `CANCELLED` and is reprocessed by
  the normal reprocess action.
- **Purge now** (single "delete forever" and empty trash): `mark_for_immediate_purge` commits
  `purge_after = now()` for trashed, unheld rows the caller may purge, stages `FILE_PURGE_REQUESTED`,
  then dispatches `files.purge_trash_now(file_ids)` (utility queue). The task calls the same per-file
  body as the beat. The API answers 202 with the count; purge itself is the existing
  `purge_media_file`, unchanged.
- **Beat body** (`purge_due`): modelled on `_select_expired_files`/`_purge_expired_files`. Phase 1,
  one short session: select `(id, uuid)` where `deleted_at IS NOT NULL AND purge_after <= now() AND
  not_held_pred() AND is_quarantined IS FALSE`, `ORDER BY purge_after LIMIT 200`. Phase 2, one
  session per file: re-load with `FOR UPDATE`, re-check the same predicate (a restore or a new hold
  may have landed), then `purge_media_file(db, file)`; stage `FILE_PURGE` with `actor=system` in the
  same session before its final commit is not possible (purge commits internally), so record the
  event right after with `record_content_event` (non-raising; the purge already happened and must
  be reported, not rolled back). Per-file failures are counted and retried next tick.

### 3.2 Beat and routes

- `files.purge_trash` in `app/tasks/trash.py`, `@celery_app.task(bind=True, name="files.purge_trash",
  priority=UtilityPriority.BACKGROUND)`, hourly at minute :45 (`crontab(minute=45)`; avoids the
  crowded 04:10-05:25 daily slots and `cleanup-expired-files` at :00).
- `files.purge_trash_now` same module, no beat.
- Route both to `CeleryQueues.UTILITY` in `core/celery_routing.py` (`test_task_routes_complete.py`),
  add `app.tasks.trash` to `include=[...]` in `core/celery.py`, and a `BeatEffect` in
  `core/celery_beat.BEAT_ENTRY_EFFECT` (`test_inert_checker.py`).

### 3.3 API (all under `app/api/endpoints/files/`; new module `trash.py` for the trash routes)

| Method + path | Who | Behaviour |
|---|---|---|
| `DELETE /api/files/{uuid}` (existing, `cancel_upload.py:23`) | `can_delete_file` | PENDING own file keeps the light cancel + hard purge (an aborted upload has nothing to recover). Everything else **trashes**. 204 unchanged. `delete_media_file`'s "not safe to delete → 409 unless force" logic is replaced by "cancel the active task and trash": trash is reversible, so the in-flight refusal is no longer needed. Delete `delete_media_file`'s purge branch rather than leaving it beside the trash path |
| `DELETE /api/files/{uuid}/force` (existing) | platform admin | trashes too (force no longer means irreversible). One destroy path: purge only from trash |
| `POST /api/files/management/bulk-action` `action=delete` | per file `can_delete_file` | trashes; per-file `FORBIDDEN` / `FILE_UNDER_LEGAL_HOLD` unchanged |
| bulk-action `action=restore` / `action=purge` | restore: `can_delete_file`; purge: §3.6 | new actions in `bulk_action_handlers.py` |
| `GET /api/files/trash` | §3.6 | paged list: uuid, filename, owner display name, `deleted_at`, `purge_after`, `trash_origin`, deleted-by display name, `held` (bool), `can_restore`, `can_purge`. Uses a dedicated query (`trash_service.trash_query(ctx)`), never the gallery query |
| `POST /api/files/{uuid}/restore` | `can_delete_file` | 200 with the file; 409 `NOT_RESTORABLE` when past due and not held |
| `DELETE /api/files/{uuid}/purge` | §3.6 | 202; 409 if not trashed (`FILE_NOT_IN_TRASH`) or held |
| `POST /api/files/trash/empty` | §3.6 | 202 with count; empties the caller's purgeable scope |
| `GET/PUT /api/admin/settings/retention` (existing retention config) | admin | gains `trash_retention_days` |

The trash routes resolve a file with a new `trash_service.get_trashed_file(db, uuid, ctx)`: loads by
uuid, 404 unless trashed, tenant gate via the same `_resource_in_tenant_scope`, quarantine still
hides it from non-exempt callers (so a quarantined trashed file is invisible in the trash list to a
member, exactly as it is in the gallery). Every new route gets a platform-access-matrix row
(`scripts/generate-platform-access-matrix.py --write`; trash routes are `content`, the retention
setting is `platform-op:admin:settings`) and passes `tests/unit/test_route_privilege_tiers.py`.

### 3.4 The restore/purge race: one invariant, no lock across storage

`purge_media_file` commits before its storage phase and holds no transaction across it, so a row
lock cannot span the destroy. Instead:

- Purge only ever selects rows with `purge_after <= now()`; explicit purge first commits
  `purge_after = now()`.
- Restore only ever updates rows with `purge_after > now()` (or held/quarantined rows, which purge
  never selects), using the database's `now()` inside one `UPDATE ... WHERE`.
- Time only moves forward, so once a row is due it can never be restored, and a restorable row is
  never due. The `FOR UPDATE` re-check in the beat body handles a hold placed between phase 1 and
  phase 2. PostgreSQL docs, "Row-Level Locks" and "UPDATE": a concurrent UPDATE on the same row
  waits for the lock and re-evaluates its WHERE clause against the committed row (READ COMMITTED),
  which is what makes the conditional UPDATE race-safe.

### 3.5 Retention (`cleanup_expired_files`)

Recommended default (OD-1): a retention-expired file goes to the **trash** (`origin='retention'`,
`deleted_by_user_id NULL`) and is purged by `files.purge_trash` when its trash window ends.
`_select_expired_files` adds `deleted_at IS NULL` (an already-trashed file is not re-trashed) and
calls `trash_files` in batches instead of `_purge_expired_files`, which is then deleted.
`auto_delete_media_file` (`file_cleanup_service.py:460`) loses its retention caller; delete it if it
has no other caller. If the owner chooses OD-1 option B, retention keeps hard-purging but must still
skip trashed rows (so it never shortens a trash window).

### 3.6 Who may do what (consistent with `backend/app/auth/CLAUDE.md` tiers)

| Action | Personal scope (`organization_id IS NULL`, the community edition) | Organization scope | Platform admin / super_admin |
|---|---|---|---|
| Trash | owner | owner, or `org:admin` of the file's org | single-tenant: `bypass.allows(need="write")`; multi-tenant: only under a write support grant |
| Restore | owner | owner, or `org:admin` | as above |
| List trash | own trashed files | members: own trashed files; `org:admin`: the whole org trash | single-tenant admin: all (`bypass.sees_all_in_scope`); multi-tenant: grant only |
| Delete forever / empty trash | owner | **`org:admin` only** (a member's mistake stays recoverable by an admin for the window) | as above |
| Change trash retention | n/a | n/a | admin (`get_current_admin_user`): "manages users and their content" |
| Purge a held or quarantined file | never | never | never (the purge selector and `purge_media_file` both refuse; release the hold first) |

### 3.7 Audit events (new `AuditEventType` members in `app/auth/audit_events.py`)

`FILE_TRASH "file.trash"`, `FILE_RESTORE "file.restore"`, `FILE_PURGE_REQUESTED
"file.purge.requested"`, `FILE_PURGE "file.purge"`, `TRASH_EMPTY "file.trash.empty"`. Each carries
`organization_id`, `tenant_scope`, `resource_type="media_file"`, `resource_uuid`, and `details` with
ids, counts and `origin` only (no filename: `test_content_audit_events` pins the details shape).
Staged through the outbox in the change's own transaction (§3.1). Check whether `content_audit`'s
`GATED_EVENTS` should include them: they must **not** be gated (governance events, always recorded).

---

## 4. Interaction rules (the precedence every lane implements)

1. **Holds win over everything that destroys.** Held files cannot be trashed (refused, as delete is
   today), are never purged (selector + `purge_media_file`), and a hold placed on an already-trashed
   file freezes it there: it stays in the trash, stays restorable, and is never purged while held.
   Releasing the hold of an overdue trashed file makes it eligible on the next beat tick; the hold
   release screen says so (L4 copy).
2. **Quarantine wins over trash for visibility and over purge for evidence.** A quarantined file is
   invisible to members in the gallery and in the trash list; the purge selector skips it
   (takedown evidence is kept until the platform releases it, the same rule retention applies).
3. **Erasure wins over trash.** GDPR erasure and account deletion destroy trashed files immediately
   (they never wait for the window: Art. 17 "without undue delay"); holds still defer them exactly as
   today. The receipt counts trashed files in `pg.media_file` like any other; no receipt change.
4. **Trash wins over hide.** A trashed file is gone from everyone's view regardless of hide rows;
   hide rows are kept, so a restored file is still hidden for whoever hid it. Purge removes hide rows
   by CASCADE.
5. **Transfer moves trashed files too** (#1104): ownership of org content includes the org's trash;
   the trash window is unchanged.
6. **Every state change is audited through the outbox** (§3.7, §5.4, §6.4); hide is not audited
   (a personal view preference with no governance value, and recording it would itself be data
   about the person).
7. **Platform roles reach tenant content only through a support grant in multi-tenant mode**
   (`services/platform_bypass.py`); trash, restore and purge use `bypass.allows(need="write")` and
   the trash list uses `bypass.sees_all_in_scope`, never the bare admin role
   (`test_quarantine_single_read_rule.py` extends to the new predicate).

---

## 5. Delayed, owner-only erase (#1105)

### 5.1 Scheduling (lane L6)

- `app/services/scheduled_erasure_service.py` (new) owns "request / cancel / execute due" for **all**
  grace-period ledger entries. `self_erasure_service` keeps its user-facing rules (feature switch,
  14/21-day grace, last-super_admin refusal) and calls into it; `_execute_one`/`execute_due_requests`
  move there and branch on `subject_type`: `user` → `erase_user`, `org_member` →
  `erase_org_member_data(user_id, org_id)`, `organization` → `erase_organization(org_id)`. One state
  machine, per D6 of the self-erasure plan.
- `POST /org-admin/gdpr/erase-user/{uuid}` and `POST /org-admin/gdpr/erase-organization` stop
  erasing. They require re-auth (§5.2), create a ledger entry with `status='deferred'`,
  `deferred_reason='grace_period'`, `scheduled_for = now() + delay`, `actor_kind='org_admin'`, and
  answer **202** `{ledger_uuid, scheduled_for}`. `confirm=true` stays required for the org route.
  The response schema changes (summary → receipt): note it in the upgrade guide.
- `GET /org-admin/gdpr/erasures`: open grace entries for the caller's org (uuid, subject type,
  subject uuid for members, `scheduled_for`, requested-by display name resolved at read time from
  the audit event, never stored in the ledger).
- `POST /org-admin/gdpr/erasures/{ledger_uuid}/cancel`: any `org:admin` of the org may cancel (a
  protective act; more people able to stop a destructive act is the safe direction), refused once
  `scheduled_for` has passed (`already_executing`), via `erasure_ledger_service.cancel_request`.
- Executor cadence: add beat `gdpr.erasure_execute_due` hourly at :35 (utility), running only the
  due-grace phase; `gdpr.erasure_reconcile`'s phase 0 calls the same function, so the daily sweep
  stays the guarantee and the hourly tick is the latency (same "schedule is the guarantee" rule as
  `notify_hold_released`).
- Notifications: WebSocket event to every org admin of the org (and the subject member, for a member
  erase) on request, cancel and completion; email through the existing notification email path when
  SMTP is configured. The T-24 h reminder uses a Redis `SET NX` key with a TTL as its de-dup (no
  ledger column; the ledger is append-only) and is the first thing to drop if time is short.
- Super-admin immediate path: whatever immediate erasure route exists for platform operators today is
  unchanged (DSAR deadlines). The org-admin routes have no immediate option.

### 5.2 Re-auth

Move `services/self_erasure_reauth.py` to `services/reauth.py` (same two functions; update the one
importer, `self_erasure.py`; no shim). Org routes take `{password?, mfa_code?}` in the body, return
409 `reauth_unavailable` for an account with neither route (SSO-only without MFA), and 403
`reauth_failed` on a bad credential. Rate-limit like `self_erasure.py`, and apply
`refuse_under_support_grant` (a support grant must never schedule an org's erasure).

### 5.3 Owner (lane L7)

- `organization.owner_user_id` (v465). `RequestContext` gains `is_org_owner` (computed in
  `resolve_org_context`, `deps_context.py:48-96`); new dependency `require_org_owner`.
- `POST /org-admin/gdpr/erase-organization` requires `require_org_owner` (member erase stays
  `require_org_admin`).
- `POST /org-admin/ownership/transfer {to_user_uuid}`: current owner only; target must be an active
  `org:admin` of the same org; audited `ORG_OWNER_CHANGE`.
- Owner NULL (left, or never backfilled): org erase is refused for org admins with 409
  `org_owner_required`; the operator's super_admin path can still act. The managed edition can set
  the owner through the same service function (`organization_owner.set_owner`), which is the seam.
- Audit: `ERASURE_SCHEDULED "gdpr.erasure.scheduled"`, `ERASURE_CANCELLED "gdpr.erasure.cancelled"`
  (executed erasures keep emitting `ADMIN_USER_DELETE` with `details.action`, as today),
  `ORG_OWNER_CHANGE "org.owner.change"`.

### 5.4 Account-deletion grace

Out of scope for v0.7.0 (OD-4). Admin `DELETE /users/{uuid}` stays immediate; self-service erasure
already has its 14-day grace.

---

## 6. Hide (#1102) and transfer (#1104)

### 6.1 Hide (lane L5)

- Service `app/services/hidden_media_service.py`: `hide(db, user, files)`, `unhide(db, user, files)`,
  `hidden_ids_subquery(user_id)`, `hidden_uuids(db, user_id)`, `exclude_hidden_for(query, user_id)`
  (`NOT EXISTS` anti-join on `user_hidden_media`).
- Who: any caller who can read the file (`get_file_by_uuid_with_permission`, default `viewer`),
  including the owner. Insert with `ON CONFLICT DO NOTHING`. Refuse with 409 `HIDDEN_LIMIT` past
  10,000 hidden rows per user (the cap that keeps the OpenSearch `terms` set bounded).
- API: `PUT /api/files/{uuid}/hidden` (204), `DELETE /api/files/{uuid}/hidden` (204), bulk-action
  `hide`/`unhide`, `GET /api/files?hidden=exclude|include|only` (default `exclude`), the same param
  on collection member lists and `GET /api/tags/{uuid}/files`. List items gain `is_hidden`.
- Read paths: the H column of §2.5. Direct link, explicitly chosen chat files and file detail are
  unaffected.
- Tenant: hiding is scoped by the read check, so a file outside the caller's tenant cannot be hidden.

### 6.2 Transfer (lane L8, recommended for v0.8.0)

- `app/services/org_content_transfer.py` `transfer_org_content(db, *, org_id, from_user_id,
  to_user_id, actor) -> OrgContentTransfer`. Target must be an active `org:admin` (or the
  `default` = org owner, else the longest-standing active admin); refuse otherwise.
- One Postgres transaction re-stamps `user_id` from → to on rows with `organization_id = org_id` for:
  `media_file` (including trashed), `speaker`, `speaker_profile`, `speaker_collection`,
  `speaker_cluster`, `collection`, `tag`, `summary_prompt`, `custom_vocabulary`, `watch_source`,
  `user_group.owner_id`. **Not transferred** (OD-5): `chat_project`, `chat_conversation`,
  `user_setting`, comments (authored content stays with its author and goes with the account).
  Shares and memberships reference collections/tags by id, so they survive unchanged. Writes the
  `org_content_transfer` row (`index_pending`) and stages `ORG_CONTENT_TRANSFER` in the same
  transaction.
- After commit: `update_by_query` `user_id` from → to on `transcripts`, `transcript_chunks` (chunks,
  digests, summary leaves) and the speaker indices (via `tenant_backfill_task._speaker_indices`)
  filtered by `organization_id` and `user_id`, then `update_file_access_index(file_ids)` to recompute
  `accessible_user_ids`. Mark `complete` with `index_restamped_at`. Beat `orgs.content_transfer_reconcile`
  (utility, every 15 min) retries `index_pending` rows; idempotent because the query keys on the old
  `user_id`.
- Why the index step is mandatory, not cosmetic: `opensearch_erasure` deletes by `user_id`, so a
  later erasure of the departed member's account would delete the transferred files' documents
  (maintenance would rebuild them within 6 h, but search would be wrong meanwhile). `erase_user` must
  therefore defer (`deferred_reason='error'`, existing value) while an `index_pending` transfer names
  the subject.
- Hooks: `register_member_removed_hook` is not needed in core; the managed edition calls
  `transfer_org_content` directly before it deletes the membership mirror row. Core wires it into
  `user_deletion_service.delete_user_account` (for every org the user is a member of, before the
  destroy), which is a no-op in the community edition (no memberships).
- Last admin: account deletion of the last `org:admin` is refused unless a transfer target exists.

---

## 7. Owner decisions (only the genuinely owner-only ones; defaults follow "most secure / best practice")

| # | Decision | Options | Recommended default |
|---|---|---|---|
| OD-1 | What retention expiry does | A: move to trash, purge after the trash window (expiry becomes N + 30 days of storage). B: keep hard-purge at N days, skip trashed rows | **A.** Recoverable by default is the #1101 acceptance and matches Workspace/Box. An operator who needs exact N-day deletion lowers `files.trash_retention_days` (min 1). Documented in the upgrade guide as a behaviour change |
| OD-2 | Cut line for 2026-11-30 | see §8 | **Ship L1-L6 (+L7 if green by 11-24); move #1104 (L8) to v0.8.0** |
| OD-3 | Org owner model | A: `organization.owner_user_id`, backfilled from the earliest admin, org erase owner-only, NULL owner → only the operator can erase. B: no owner; any admin with delay + re-auth + notify-all-admins | **A.** The issue's acceptance requires owner-only; NULL refusing is the fail-closed choice |
| OD-4 | Admin-initiated account deletion grace (#1105 "30 d") | A: defer to v0.8.0 with #1104 (it needs the transfer). B: 30 d now, which exceeds the shipped 21-day self-erasure cap whose purpose is the Art. 12(3) clock | **A**, and when built, cap it like D1 (default 14, max 21) because the clock starts at the request |
| OD-5 | What #1104 transfers | A: org files, speakers, collections, tags, prompts, vocabulary, watch sources, groups; not chats, personal settings, comments. B: everything org-stamped including chats | **A.** Mirrors Workspace's Drive transfer (files move, mail/chat do not); a departed person's conversations are not the org's to read |

Not owner decisions (decided here, cite if challenged): trash window 30 d (issue text), org/member
erase delay 72 h (issue text), any org admin may cancel a scheduled erase, members cannot delete
forever in org scope (issue text), hide is unaudited, centroids are not recomputed on trash.

---

## 8. Lanes, sequence and cut line

Each lane: its own worktree `.claude/worktrees/<lane>` on the branch named, one commit per concern,
hooks on, targeted tests only (red first, then green), no full suites, no `--all-files` (the
coordinator runs one full sweep per wave). Red-first uses a `git archive HEAD` tree named
`ot-redcheck.*` (root CLAUDE.md). Every lane finishes with
`cd backend && python3 ../scripts/audit-tests.py tests` at 0 open findings, and, if it adds routes,
`scripts/generate-platform-access-matrix.py --check`.

Common targeted commands (adapt the paths):

```bash
cd backend && venv/bin/pytest -v -n 1 <new test files>                      # lane tests
scripts/test-in-worktree.sh -- tests/<db tests> -v -n 1                       # DB-backed tests without the dev stack
venv/bin/ruff check <touched files> && venv/bin/mypy <touched files>
python3 scripts/release-tests/lib/alembic-head.py backend/alembic/versions   # exactly one head
```

### Wave 1 (parallel)

**L1 `v070/dsafety-l1-availability`** (#1101 foundation). Files: `alembic/versions/v462_add_media_file_trash.py`,
`app/models/media.py`, `app/db/schema_detect/data_safety.py` + `ladder.py`, `app/db/concurrent_indexes`
consumer for the partial index, `app/services/file_availability.py` (new), `app/services/takedown_predicates.py`
(quarantine-only names removed; keep the takedown-specific helpers), every call site in §2.5's
"via predicate" rows, `backend/alembic/CLAUDE.md` (reserve v462-v466), `backend/app/db/CLAUDE.md`.
- `file_availability.py` API: `exclude_unavailable(query, *, include_quarantined=False)` (always adds
  `deleted_at IS NULL`), `unavailable_among(db, uuids)`, `is_unavailable_for(bypass, file)`,
  `unavailable_list_exempt(bypass)`, and the notification functions renamed likewise. Delete the old
  names and update every importer in the same lane (`rg -n "exclude_quarantined|quarantined_among|is_hidden_for|is_quarantine_hidden|quarantine_list_exempt|is_notification_suppressed|filter_suppressed_file_uuids" backend/`);
  no re-export shim. Convert the ~14 inline `is_quarantined.is_(False)` predicates in §2.5 too.
- Red-first tests: (a) `tests/unit/test_file_availability_single_source.py`, an AST scan modelled on
  `test_hold_guard_single_source.py`: no read of `MediaFile.deleted_at` or `MediaFile.is_quarantined`
  in `app/` outside `file_availability.py`, `trash_service.py`, `takedown_service.py`,
  `takedown_authority.py`, the models, schemas, migrations, the erasure/purge modules and the review
  surfaces (`admin_quarantine.py`, `org_admin_takedown.py`), each allowlisted with a reason; with a
  `--selftest` must-fire and must-stay-clean case. (b) Behaviour: a file with `deleted_at` set is
  absent from gallery, metadata filters, owners list, dashboard, collection lists, tag lists, chat
  scope, speaker lists/occurrences, exports and WS notifications, and 404s through the choke point;
  extend the existing `test_*_quarantine.py` fixtures with a `trashed` parametrisation rather than
  copying 40 files. (c) The §1.3 quarantine gaps each get one test that is red on HEAD.
  (d) `test_v462_migration_consistency.py`, ladder test. (e) A guard that `gdpr_erasure_service`,
  `user_deletion_service`, `erasure_receipt/` never import `file_availability`.
- Merge first: the rename touches ~60 files and every later lane builds on it. Announce the rename
  to the other v0.7.0 lanes so they rebase.

**L6 `v070/dsafety-l6-scheduled-erase`** (#1105a, independent of L1). Files: `v464_generalise_erasure_grace_index.py`,
`app/models/erasure.py`, `app/services/scheduled_erasure_service.py` (new), `self_erasure_service.py`,
`app/services/reauth.py` (moved), `app/api/endpoints/self_erasure.py`, `app/api/endpoints/org_admin.py`
(erase routes; consider a new `org_admin_erasure.py` if the file grows a second concern),
`app/tasks/erasure_reconciliation.py`, `core/celery_beat.py`, `core/celery_routing.py`,
`app/auth/audit_events.py`, `app/core/constants.py`, schema detection, matrix TSV.
- Red-first: executor runs `erase_org_member_data` (not `erase_user`) for a due `org_member` entry
  (red on HEAD: it calls `erase_user`); a member grace entry and the same user's self request
  coexist (red on HEAD: unique violation); org-admin erase returns 202 and deletes nothing; cancel
  before `scheduled_for` works, after → 409; non-admin 403; no re-auth → 409/403; other org's entry
  invisible and uncancellable; frozen-time sweep executes and closes the ledger; journal line per
  state.

### Wave 2 (after L1 merges)

**L2 `v070/dsafety-l2-trash-api`** (#1101 behaviour). Files: `app/services/trash_service.py`,
`app/tasks/trash.py`, `app/api/endpoints/files/trash.py` (new), `cancel_upload.py`, `crud.py`
(delete path), `management.py` (force), `bulk_action_handlers.py`, `app/tasks/cleanup.py`
(retention), `file_cleanup_service.py` (remove dead `auto_delete_media_file` if orphaned),
`utils/file_hash.py`, `url_video_import.py`, `utils/stats_helpers.py`, `system_settings_service.py`,
constants, audit events, beat/routes, matrix TSV.
- Red-first: DELETE leaves the row with `deleted_at` set and storage intact; restore within window;
  restore after due → 409; held file trash → 409; trashed + held never purged; quarantined trashed
  never purged; purge beat with frozen time destroys row + storage + all OpenSearch docs (reuse the
  existing purge integration fixtures); restore racing purge (two sessions, assert exactly one
  outcome); permission matrix of §3.6 (owner, editor-share, org:member, org:admin, other org,
  platform admin single + multi-tenant with/without grant); member cannot empty org trash; retention
  trashes (OD-1 A); re-upload of a trashed file is not a duplicate; GDPR `erase_user` destroys a
  trashed file immediately; audit outbox rows staged in the same transaction (rollback leaves none).

**L5 `v070/dsafety-l5-hide`** (#1102, backend + UI). Files: `v463_add_user_hidden_media.py`,
`app/models/` (new model), `app/services/hidden_media_service.py`, listing/filtering, search builders
(§2.5 H column), chat scope, collection/tag lists, bulk handlers, erasure inventory, schema detection;
frontend: `components/gallery/GallerySelectionActions.svelte` (bulk Hide/Unhide), the file card menu,
`components/FilterSidebar.svelte` (a "Show hidden" chip using `ui/FilterChipButton.svelte`, values
exclude/include/only), `stores/gallery.ts` filter state, an "Hidden" badge, 12 locales.
- Red-first: hidden file absent for that user in gallery, search, corpus-wide chat; present for
  another user; present on direct link and explicit chat selection; unhide restores; cannot hide a
  file outside the tenant (404/403); 10,001st hide → 409; purge removes hide rows; trash wins over
  hide (§4.4); vitest for the chip and bulk action.

### Wave 3 (after L2 merges)

**L3 `v070/dsafety-l3-index-lifecycle`** (#1101 search/RAG/speakers). Files:
`services/search/index_schema.py` (chunks mapping), `services/opensearch_service/indices.py`
(transcripts + speakers v3/v4 mappings; additive `put_mapping` on startup like
`ensure_transcript_tenant_mapping`), every document writer (find with
`rg -n "\"accessible_user_ids\"" backend/app` and the speaker doc builders), `search/access_filter.py`,
`opensearch_service/speaker_query_filters.py` (new) + the 8 kNN sites, `trash_service` stamp/unstamp
hooks (`update_by_query` with `refresh=True`, reusing `tenant_backfill_task._update_by_query`), a
retry task for a failed stamp (the post-filter covers the gap; the task makes it converge),
erasure receipt `INDEX_PATTERNS` unchanged (no new index).
- Red-first (integration, real OpenSearch, auto-enabled when the stack is reachable): after trash, the
  file is absent from search hits, counts, suggestions, facets, summary plane, chat chunk + digest
  retrieval, and speaker kNN matching; after restore it is back; a reindex of a trashed file writes
  stamped docs; `opensearch_integrity_task` keeps a trashed file's docs; an org with 10,001 trashed
  files still searches (no 503: the regression the quarantine-set approach would cause); AST guard
  that every kNN body in `opensearch_service/` calls the filter helper.

**L4 `v070/dsafety-l4-trash-ui`** (#1101 UI). Files: `frontend/src/routes/trash/+page.svelte` (new
route; list, restore, delete forever, empty trash, days remaining, deleted by, `HoldBadge` for held
rows), `frontend/src/lib/api/` typed client, `routes/+page.svelte` `deleteSelectedFiles` copy, delete
confirmation dialogs ("Move to trash. Recoverable for N days."), gallery header link to Trash, admin
Data Management → Retention tab field for `trash_retention_days`
(`components/settings/RetentionSettings`), 12 locales, light/dark, `ar` RTL checked in a browser,
`docs-site` page.
- Red-first: vitest for the page states (empty, member, org admin, held row, past-due row); a
  Playwright e2e `tests/e2e/test_trash.py` (delete → Trash → restore; must clean up after itself,
  never touch dev data: create its own file, purge it at the end).

**L7 `v070/dsafety-l7-org-owner`** (#1105b; after L6). Files: `v465_add_organization_owner.py`,
`app/models/organization.py`, `app/services/organization_owner.py` (new), `deps_context.py`
(`is_org_owner`, `require_org_owner`), `request_context.py`, erase-organization route, ownership
transfer route, audit events, matrix TSV, org-admin Settings: a "Scheduled erasures" panel
(list + Cancel) in the org-admin section group (`stores/settingsModalStore.ts` org section ids,
`lib/settings/sectionLoaders.ts`), 12 locales. Requesting an erasure stays API-only (the documented
"no one-click erase button" decision, `backend/app/api/CLAUDE.md` § Compliance).
- Red-first: non-owner admin → 403 on org erase; owner + re-auth → 202; NULL owner → 409; backfill
  picks the earliest admin; transfer to a non-admin refused; cross-org refused.

### Wave 4 / v0.8.0

**L8 `v080/dsafety-l8-content-transfer`** (#1104), §6.2, plus OD-4's account-deletion grace.

### Cut line (recommended, OD-2)

- **Must ship in v0.7.0:** L1, L2, L3, L4 (#1101 complete), L5 (#1102), L6 (#1105 delay + cancel +
  re-auth + audit). These remove the irreversible-by-mistake paths that exist in the **community**
  edition (delete, bulk delete, retention) and the immediate org erase.
- **Ship if green by 2026-11-24:** L7 (owner-only org erase). Without it, L6 already makes org erase
  delayed, re-authenticated, cancellable by any admin and announced to every admin.
- **Move to v0.8.0:** #1104 (L8) and admin account-deletion grace. Organization membership exists only
  in the managed edition, so community users lose nothing, and the transfer is the largest
  multi-store change in the cluster (Postgres + three index families + an erasure interlock).
  Re-milestone #1104 with this plan's §6.2 as its body.
- **Follow-up issue (file when L3 lands):** stamp `availability=quarantined` on quarantine/release so
  quarantine leaves the capped Postgres `must_not` set and the kNN speaker reads exclude quarantined
  files too (the gap in §1.3 that L3 fixes only for trash).

---

## 9. UI and docs per feature

| Feature | Where | Notes |
|---|---|---|
| Trash | new `/trash` route linked from the gallery header (beside the ownership chips) and from the delete toast ("Moved to trash. Undo"); Undo calls restore | members see their own trash; org admins see the org trash with an owner column; personal-scope owners see "Delete forever" and "Empty trash" |
| Trash retention | Settings → Data Management → Retention (admin) | one number field, 1-90, help text about storage being freed at purge |
| Hide | gallery bulk bar + file card menu "Hide from my view"; FilterSidebar chip "Hidden: Exclude / Include / Only"; card badge "Hidden" with Unhide | copy must say it does not affect anyone else and is not a delete |
| Scheduled erasures | org-admin Settings → "Scheduled erasures" (list + Cancel, countdown) | request remains API-only |
| i18n | every new key in all 12 locale files, translated (parity is the floor); plurals via `_one`/`_other` ("Purged in {count} days"); `ar` checked RTL in a browser, light and dark | run `cd frontend && npm run check:i18n` by hand: locale-only commits do not fire the hook |

Docs (each lane writes its own section; the coordinator does not batch them):

- New `docs-site/docs/user-guide/trash-and-hidden-files.md` (L2/L4/L5): what trash keeps, the window,
  who can restore/purge, holds freezing a trashed file, hide semantics, centroid note.
- `docs-site/docs/user-guide/admin-panel.md`: retention now trashes (OD-1), trash retention setting.
- `docs-site/docs/operations/gdpr-erasure.md` (L6/L7): org/member erase is scheduled, re-auth, cancel,
  owner-only, the 202 response.
- `docs-site/docs/operations/upgrading-to-0-7-0.md`: DELETE is recoverable (storage freed at purge),
  force delete trashes, retention trashes, org erase response is 202, `v462`-`v465` notes.
- `docs-site/docs/faq.md:333` (automatic deletion answer), `docs/security/tenant-boundaries.md`
  (trash list and purge rows), `docs/security/platform-access-matrix.*` (generated).
- `CHANGELOG.md` per lane (Keep a Changelog), including the §1.3 quarantine fixes.

---

## 10. Risks

1. **A missed read path leaks trashed content.** Mitigation: the rename in L1 makes the old
   quarantine-only names disappear, so every existing site is forced through the new predicate;
   the single-source AST guard blocks new direct flag reads; L3 moves search/RAG to an index field
   filtered in the one ACL builder.
2. **L1 is a wide mechanical diff that conflicts with other v0.7.0 lanes.** Merge it first and
   early in the wave; other lanes rebase onto it (import errors are loud, not silent).
3. **Stamp/post-filter window.** Between the trash commit and the `update_by_query`, counts and
   facets may include the file for a moment; hits never do (post-filter). The stamp runs
   synchronously in the trash request with `refresh=True`, and a retry task converges failures.
4. **Storage cost.** Trashed files hold storage for the window (and, with OD-1 A, retention adds
   the window). Admin stats show trashed bytes separately.
5. **Behaviour change for API clients.** `DELETE` no longer frees storage immediately; force delete
   no longer destroys. Documented; `/purge` is the explicit destroy.
6. **Migration safety guard.** v464 drops an index in `upgrade()`; needs a baseline entry with a
   reason or the new-index-then-drop shape in the same revision.
7. **Executor timing.** The daily reconcile alone could run a 72 h erase up to 24 h late; the hourly
   `gdpr.erasure_execute_due` tick fixes the latency, the daily sweep stays the guarantee.
8. **Managed-edition coupling.** Owner and transfer seams are called by the managed edition; keep the
   core functions edition-neutral (no vendor names: the seam guard greps for them).

## 11. Out of scope for v0.7.0

#1104 transfer and admin account-deletion grace (v0.8.0, OD-2/OD-4); per-organization trash
retention (could live in `organization_policy` later); per-user storage quotas; quarantine index
stamping (follow-up issue, §8); profile-centroid recompute on trash (by design, §2.5); a UI button
to request an erasure (existing decision).
