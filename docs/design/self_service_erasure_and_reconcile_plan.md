# Self-service erasure requests and index-level erasure reconciliation: design and implementation plan

Status: PLAN ONLY (2026-11-01). Nothing here is implemented.
Companions (read first, do not duplicate): `docs/design/erasure_receipt_plan.md` (receipts, store
inventory, WP-R1..R5), `docs/design/append_only_audit_plan.md` (protected tables registry),
`docs/design/legal_records_and_voiceprint_retention_plan.md` (v450, v451, `PurgeScope(kind="files")`).

## 0. Summary

Three work packages, one release train (target **v0.7.0**, alongside the erasure receipt plan):

| WP | What | Migration | Behaviour change |
|---|---|---|---|
| **S** | `POST /api/users/me/erasure-request` with a grace period, `DELETE` to cancel, `GET` for status, and a **request receipt**. SPA: Settings → Account → "Erase my account" | `v452` | New user capability, off by default behind a DB setting |
| **X** | `gdpr.erasure_reconcile` gains an **index-level** resurrection check: after an OpenSearch restore, erased subjects' documents are found and re-deleted per index, driven by the ledger | none (uses v452 columns) | Re-deletes docs that an OpenSearch restore brought back |
| **M** | `org_member` entries are **re-run automatically** after a restore, bounded by a creation-time cutoff (today: counted as `org_member_manual_review` and left) | none | Re-erases org-stamped rows created before the erasure completed |

Migration number: head today is **v449** (`python3 scripts/release-tests/lib/alembic-head.py backend`).
v450 and v451 are reserved by the legal-records plan; the erasure receipt and append-only plans
"derive the number" and may also claim v452. **Re-derive the head immediately before writing the
file and chain after whatever is actually there**; the name in this plan is
`v4NN_add_self_service_erasure_request.py`, written below as `v452` for readability.

## 1. Current state (derived from code)

- Ledger: `backend/app/models/erasure.py` (`ErasureLedgerEntry`, `LEDGER_STATUSES =
  ("pending","complete","deferred","failed")`, `SUBJECT_TYPES = ("user","org_member","organization")`,
  `deferred_reason String(20)`, `sla_due_at`, `resurrections_detected`, `last_resurrection_at`,
  partial index on `status <> 'complete'`). Service: `backend/app/services/erasure_ledger_service.py`
  (`record_request` commits FIRST, `record_outcome`, `record_resurrection`, `open_entries`,
  `completed_entries`, `overdue_entries`, on-disk/object journal + `restore_from_journal`).
- Erasure: `backend/app/services/gdpr_erasure_service.py` `erase_user`, `erase_org_member_data`,
  `erase_organization`, each taking `ledger_entry=`. Speaker-index enumeration lives in ONE place:
  `_speaker_index_names` / `delete_speaker_docs_by_query` (alias, v3, v4, v3_backup).
- Reconcile: `backend/app/tasks/erasure_reconciliation.py`, task `gdpr.erasure_reconcile`
  (UTILITY queue, beat in `backend/app/core/celery.py`). Phase 1 short session decides work; phase 2
  one session per entry. `_subject_is_alive` checks **Postgres only** (user/org row by id AND uuid);
  `org_member` is never auto-re-erased (`manual_review += 1`).
- Gap: an **OpenSearch-only** restore (snapshot restore, `POST /search/repair-indices`, or
  `indices._restore_v3_from_backup`) brings documents back while Postgres is unchanged; nothing
  detects it.
- Erasure routes are operator-only (`backend/app/api/CLAUDE.md` § Compliance: "deliberately without
  a one-click UI button"); `admin.lockedAccountsQuarantine.test.ts` forbids `gdpr|erase` methods on
  `AdminApi`.

## 2. Decisions (recommendations; owner to confirm in §11)

- **D1 Grace period.** DB-backed `SystemSettings` `gdpr.self_erasure_grace_days`, coded default
  **14** in `backend/app/core/constants.py`, allowed range **0..21**. The cap keeps grace + retry
  time inside the Art. 12(3) one-month SLA, because the SLA clock starts at the request.
- **D2 Feature switch.** `gdpr.self_erasure_enabled`, default **false** (operator opts in; many
  deployments are employer-run and erasure there is the controller's process). Super-admin only.
- **D3 During grace the account stays usable**, with a persistent banner and a Cancel button.
  Logging in does NOT cancel (explicit cancel only, otherwise an automated session refresh would
  cancel silently). New sessions are allowed so the user can cancel.
- **D4 Step-up required**: password re-entry (local accounts) or a fresh MFA/IdP re-auth within
  5 minutes (reuse the existing step-up dependency if one exists; `rg -n 'step_up|reauth'
  backend/app/auth` first). SSO-only accounts without step-up support: request allowed only via
  the IdP re-auth path; otherwise 409 `reauth_unavailable` and the docs point to the operator.
- **D5 Refusals** (409 with a code, no partial state): last active super_admin
  (`last_super_admin`), an active legal hold over any owned file is **not** a refusal (erasure
  already skips held files and the receipt reports them), an open request already exists
  (`request_exists`, return it).
- **D6 Ledger reuse, no second state machine.** The request IS a ledger entry created at request
  time with `status='deferred'`, `deferred_reason='grace_period'`, `actor_kind='subject'`, plus a
  new `scheduled_for` column. Cancel sets the new status `cancelled`. This keeps one SLA clock and
  one journal. Rejected alternative: a separate `erasure_request` table (two sources of truth for
  "is this subject being erased").
- **D7 Request receipt.** Not the erasure receipt (that is produced after destruction by WP-R of the
  receipt plan). The request receipt is the ledger entry's uuid + `requested_at` + `scheduled_for` +
  a sha256 `request_digest` over those canonical fields, returned on create and emailed if SMTP is
  configured. After execution the erasure receipt (receipt plan) is linked by `ledger_uuid`.
- **D8 Org-member re-run: AUTOMATE, bounded by time.** Reasons: (1) the manual-review counter has no
  UI and no owner, so in practice it means "never"; (2) the ambiguity the code comment names (new
  legitimate uploads look like resurrection) is resolved by a **creation-time cutoff**: only rows
  and documents whose creation timestamp is `<= entry.completed_at` are erased; anything the member
  created after the erasure is untouched; (3) a restore can only bring back rows that existed
  before the erasure, so the cutoff loses nothing. Stores without a reliable creation timestamp
  are listed in §5.3; for those the entry stays in a reported `org_member_needs_review` bucket
  (smaller, named per store, surfaced on the receipt as `unverified`). Keep manual as the fallback
  only where the cutoff cannot be applied.
- **D9 Index-level detection is ledger-driven and batched**: one `terms` query per index over the
  subject ids of a page of completed entries, not one query per entry per index.

## 3. WP-S: self-service erasure request

### 3.1 Migration `backend/alembic/versions/v452_add_self_service_erasure_request.py`

Follow `backend/alembic/CLAUDE.md` (idempotent SQL, detection arm keyed on the LAST object created,
`down_revision` = re-derived head). Steps:

1. `ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS scheduled_for timestamptz NULL;`
2. `ADD COLUMN IF NOT EXISTS cancelled_at timestamptz NULL;`
3. `ADD COLUMN IF NOT EXISTS request_digest char(64) NULL CHECK (request_digest IS NULL OR request_digest ~ '^[0-9a-f]{64}$');`
4. `ADD COLUMN IF NOT EXISTS index_checked_at timestamptz NULL;` (WP-X cursor, see 4.2)
5. Replace `ck_erasure_ledger_status` with one including `'cancelled'` (drop + add in one
   transaction; copy the normalized spelling back from `pg_get_constraintdef`).
6. Add `actor_kind` value `'subject'` to its CHECK if `actor_kind` is constrained (verify with
   `\d erasure_ledger`; `_actor_kind` in `gdpr_erasure_service.py` currently returns staff kinds).
7. `CREATE UNIQUE INDEX IF NOT EXISTS uq_erasure_ledger_open_self_request ON erasure_ledger
   (subject_user_id) WHERE deferred_reason = 'grace_period' AND status = 'deferred';` (one open
   request per user; race-safe).
8. Partial index for the executor: `ix_erasure_ledger_due_grace ON erasure_ledger (scheduled_for)
   WHERE status='deferred' AND deferred_reason='grace_period'`.
9. Detection arm: existence of index 8.

Downgrade: refuse if any row has `status='cancelled'` or `scheduled_for IS NOT NULL` with an open
request (raise with a message telling the operator to complete or cancel them); otherwise drop
indexes, restore the old CHECK, drop columns.

Model: add the four columns and the new status to `models/erasure.py`; keep `LEDGER_STATUSES` the
single source for both the CHECK and the ORM (`test_orm_ddl_divergence.py` must stay green).
If the append-only plan has registered `erasure_ledger` in `PROTECTED_TABLES`, add the new columns
to its `mutable_columns` (`cancelled_at`, `index_checked_at`, `status`).

### 3.2 Service `backend/app/services/self_erasure_service.py` (new, one concern)

- `request_self_erasure(db, user, *, now) -> ErasureLedgerEntry`: checks D2, D5; computes
  `scheduled_for = now + grace_days`; calls `erasure_ledger_service.record_request(...,
  actor_kind="subject", status="deferred", deferred_reason="grace_period", scheduled_for=...)`
  (extend `record_request` with keyword-only `status`, `deferred_reason`, `scheduled_for`; default
  behaviour unchanged). Computes `request_digest`. Journal record includes the new fields (so a DB
  restore does not lose a pending request: `restore_from_journal` must re-open it as deferred, not
  pending; test S6). Audits `gdpr.self_erasure.request` (ids only).
- `cancel_self_erasure(db, user, entry_uuid)`: only `status='deferred' AND
  deferred_reason='grace_period' AND now < scheduled_for`; sets `status='cancelled'`,
  `cancelled_at`; journals and audits `gdpr.self_erasure.cancel`. After `scheduled_for`: 409
  `already_executing`.
- `execute_due_requests(limit=50)`: called from the reconcile task (3.4). For each due entry flips
  to `pending` and calls `erase_user(db, user_id, ledger_entry=entry)`.
- Reconcile's `open_entries` must **exclude** `deferred_reason='grace_period'` with
  `scheduled_for > now` and all `cancelled` rows (test S3); `overdue_entries` must exclude
  `cancelled`.

### 3.3 API `backend/app/api/endpoints/self_erasure.py` mounted at `/users/me/erasure-request`

Mount before any `/users/{uuid}` router (see the `support_access.me_router` comment in
`backend/app/api/router.py`). `get_current_active_user`; refuse support-access grants; rate limit
like `/privacy/me/purge`.

| Method | Body / response |
|---|---|
| `GET` | `SelfErasureStatus{enabled, grace_days, request: SelfErasureRequestReceipt \| null}` |
| `POST` | body `{confirm: "ERASE", reauth...}` → 201 `SelfErasureRequestReceipt{ledger_uuid, requested_at, scheduled_for, sla_due_at, request_digest, status}`; 404 when D2 off (do not advertise), 409 codes per D5/D4 |
| `DELETE /{ledger_uuid}` | 200 receipt with `status="cancelled"`; 404 for another user's uuid |

Schemas in `backend/app/schemas/self_erasure.py`. Update `backend/app/api/CLAUDE.md` § Compliance
(new row; note this is the **subject's** own action, which is why it has UI while operator erasure
does not). Regenerate `docs/security/platform-access-matrix.tsv`; keep
`test_route_privilege_tiers.py` green. Admin settings: expose the two settings on the existing
admin privacy/settings page via `GET|PUT /admin/privacy-policy` (or the general settings route,
whichever owns `SystemSettings` writes for `privacy.*`), audited `admin.settings.change`.

### 3.4 Executor

No new beat entry: `gdpr.erasure_reconcile` gains a phase 0 that calls
`self_erasure_service.execute_due_requests` (short session to pick ids, one session per entry,
same pattern as phase 2). Reconcile runs on its existing schedule; worst-case execution lag is one
beat interval (state it in the docs).

### 3.5 SPA

- New API module `frontend/src/lib/api/selfErasure.ts` (NOT `AdminApi`; the existing guard test
  stays as is).
- New component `frontend/src/components/settings/AccountErasureCard.svelte`, placed beside
  `UserPrivacyCard.svelte` in the user settings; hidden when `enabled=false`. Flow: explanation →
  typed confirmation + re-auth → receipt view (uuid, scheduled date, digest, "download receipt as
  JSON") → Cancel button until `scheduled_for`.
- Global banner while a request is open: extend the existing app-level banner/notice component
  (find it with `rg -n 'banner' frontend/src/components | head`) rather than a new one.
- Use `$lib` / `$components` aliases; light/dark parity; dates via the existing locale formatter.
- i18n: about 20 keys under `settings.accountErasure.*` and `banner.accountErasure.*` in all 12
  locales (`ar de en es fr it ja ko nl pt ru zh`), real translations, not English copies. Check
  `ar` renders RTL (date and the uuid/digest monospace blocks must stay LTR via `dir="ltr"`).
  Run `cd frontend && npm run check:i18n` explicitly (locale-only commits do not fire the hook).
- Vitest: `AccountErasureCard.test.ts` (hidden when disabled; cancel visible only before
  `scheduled_for`; confirm button disabled until typed confirmation). Playwright e2e
  `backend/tests/e2e/test_account_erasure_request.py` uses a **throwaway user created by the test**,
  requests then cancels; never touches `admin@example.com`.

## 4. WP-X: index-level resurrection detection

### 4.1 Which indices, which key

Reuse the store inventory from the erasure receipt plan (WP-R1, `STORES`, logical keys
`os.transcripts`, `os.transcript_chunks`, `os.transcript_summaries`, `os.speakers`). **If WP-R1 has
landed, iterate its registry; if not, land WP-R1 first** (do not create a second enumeration). The
speaker set comes from `gdpr_erasure_service._speaker_index_names` (the single place).

Per index the subject key is the owner field stamped on documents: `user_id` (+ `organization_id`
for `org_member`). Before coding, verify every index mapping carries `user_id` (`GET
<index>/_mapping`); a store without it is listed as `unverified` for index checks and named in
the docs. `os.audit` is retained by design (receipt plan D3) and excluded.

### 4.2 Algorithm (`backend/app/tasks/erasure_reconciliation.py`, new helper module
`backend/app/services/erasure_index_reconcile.py` to keep the task file cohesive)

1. Phase 1 (short session) selects up to `MAX_INDEX_CHECK_ENTRIES = 500` completed entries ordered
   by `index_checked_at NULLS FIRST`, returning plain tuples `(id, subject_type, user_id,
   organization_id, completed_at)`.
2. Per index, one `count`/`terms` aggregation: `bool.filter: terms user_id: [...]` aggregated by
   `user_id` (and `organization_id`). Unavailable OpenSearch → no action, no cursor advance, counter
   `index_unverified += 1` (never treated as zero).
3. For a hit on a `user` entry: confirm the user row is **absent** (or present with a different
   uuid, i.e. id reuse impossible without a DB restore, which `_subject_is_alive` already handles).
   If a live user with the same id AND uuid exists, it is a DB resurrection and the existing path
   owns it. Otherwise `delete_by_query` on that index for `user_id = X` (speaker indices via
   `delete_speaker_docs_by_query`, the single enumerator).
4. For `organization`: same with `organization_id`.
5. For `org_member`: `user_id = X AND organization_id = O AND <created field> <= completed_at`.
   The created field per index must be confirmed from the mapping (e.g. `created_at` / upload time);
   an index lacking one is not touched and counted in `org_member_needs_review` with its logical key.
6. Each re-delete: `ledger.record_resurrection(db, entry)` (extend with keyword `store_key`) and,
   if the receipt plan is in, a receipt row with `phase="resurrection"` (that enum value already
   exists in the receipt plan). Stamp `index_checked_at = now()` after a clean check.
7. Result dict gains `index_resurrections_redeleted`, `index_unverified`, `org_member_rerun`,
   `org_member_needs_review` (rename of `org_member_manual_review`; keep the old key out, no shim,
   and update `backend/app/tasks/CLAUDE.md` and any dashboard/alert reading it:
   `rg -n org_member_manual_review`).

### 4.3 Load bound

At most `len(indices)` aggregation queries per tick plus one delete per hit; 500 entries per tick
cycles the whole ledger within ledger_size/500 ticks. Document it in `docs-site`.

## 5. WP-M: automatic org-member re-run after restore

### 5.1 Database side

Replace the `if entry.subject_type == "org_member": manual_review += 1; continue` branch with
`_org_member_resurrected(db, entry)`: any org-stamped row for `(user_id, organization_id)` in the
tables `erase_org_member_data` deletes, with creation timestamp `<= entry.completed_at`. Then call
`erase_org_member_data(db, user_id, org_id, ledger_entry=entry, created_before=entry.completed_at)`.

### 5.2 `erase_org_member_data(..., created_before: datetime | None = None)`

New keyword-only parameter; `None` keeps today's behaviour (direct erasure requests). When set,
every per-table selection adds `created_at <= created_before` (media files: `upload_time`). Because
media files cascade most dependent rows, filtering at the file level covers their children.

### 5.3 Tables without a reliable creation timestamp

Enumerate them while implementing (`rg -n 'organization_id' backend/app/models` against the list of
tables `erase_org_member_data` touches) and list each in a module constant
`ORG_MEMBER_RERUN_UNTIMED` with a reason. Those are skipped in re-runs and reported. Do not add
`created_at` columns in this plan unless the list is short and the backfill is exact (it is not:
a backfilled timestamp is invented data).

## 6. Sequencing

1. Land receipt plan WP-R1 (registry) if not already merged. Hard prerequisite for WP-X.
2. v452 migration + model (S).
3. Ledger service extensions (`record_request` kwargs, `open_entries`/`overdue_entries` filters,
   journal fields, `record_resurrection(store_key=)`).
4. `self_erasure_service` + executor phase 0.
5. API + settings + access matrix.
6. SPA + i18n.
7. WP-M (`created_before`), then WP-X (index reconcile). X and M are independent of S after step 3.
8. Docs, CHANGELOG, seam bump.

## 7. Tests that must fail first

Watch each red in a `git archive HEAD` tree (`mktemp -d -t ot-redcheck.XXXXXX`), per root CLAUDE.md.

| # | File::test | Asserts |
|---|---|---|
| S1 | `backend/tests/unit/test_self_erasure_service.py::test_request_creates_deferred_ledger_entry_with_schedule` | status deferred, reason grace_period, scheduled_for = now+grace, digest matches canonical fields |
| S2 | `::test_grace_days_clamped_to_21` | setting 60 → 21 |
| S3 | `backend/tests/unit/test_erasure_reconcile_grace.py::test_reconcile_does_not_erase_during_grace` | a grace entry is NOT passed to `erase_user` before `scheduled_for`; is after (freeze time) |
| S4 | `::test_cancelled_entry_never_executes_and_is_not_overdue` | |
| S5 | `backend/tests/api/test_self_erasure_api.py` | 404 when disabled; 409 last super_admin; 409 duplicate returns existing; other user's uuid 404 on DELETE; support grant refused; step-up required |
| S6 | `backend/tests/integration/test_self_erasure_journal.py::test_db_restore_reopens_request_as_deferred` | journal round-trip keeps grace semantics |
| S7 | migration tests in the existing pattern (`rg -n 'def test_.*migration' backend/tests | head`): upgrade idempotent twice; downgrade refuses with open requests | |
| X1 | `backend/tests/integration/test_erasure_index_reconcile.py::test_opensearch_restore_of_erased_user_is_redeleted` (live OpenSearch; marker `integration`; isolated `--fresh` stack) | erase user; re-index their docs directly (simulates snapshot restore); sweep → count 0 in each index; ledger `resurrections_detected` +1 |
| X2 | `::test_opensearch_unavailable_is_unverified_not_clean` | cursor not advanced, counter set |
| X3 | `::test_index_check_is_one_query_per_index` | query count bound (spy on client) |
| M1 | `backend/tests/integration/test_org_member_rerun.py::test_restored_rows_before_erasure_are_reerased` | |
| M2 | `::test_rows_created_after_erasure_survive` | the core safety property; mutant `<=` → no filter must fail it |
| M3 | `::test_untimed_tables_are_reported_not_deleted` | |

Run `python3 scripts/audit-tests.py backend/tests` to 0 findings; `npm run test:audit` for vitest.

## 8. Upgrade and rollback

- Upgrade: additive columns, widened CHECKs; no data rewrite. Feature off by default.
- Behaviour change on upgrade: the reconcile task begins re-deleting resurrected index documents
  and re-running org-member erasures for **every historical completed entry** on its first ticks.
  Release notes must say so; the first pass is bounded per tick.
- Rollback: disable `gdpr.self_erasure_enabled`, cancel open requests (or let them run), then
  downgrade (refuses while requests are open). Code rollback without downgrade is safe: the old
  code ignores the new columns, but would treat `deferred` grace entries as retryable and erase
  them early, so **cancel or complete open requests before rolling code back** (runbook step).

## 9. Docs, CHANGELOG, seam

- docs-site: new `docs-site/docs/user-guide/erase-my-account.md`; update
  `docs-site/docs/features/privacy-controls.md` (setting, grace, receipt),
  `docs-site/docs/operations/backup-restore.md` (what reconcile does after a database or OpenSearch
  restore, timing, counters), `docs-site/docs/operations/runbooks.md` (rollback step in §8). Run
  the docs-site drift checks (`docs-site/CLAUDE.md`).
- `CHANGELOG.md` `## [Unreleased]`: Added (self-service erasure request with grace period and
  receipt; index-level reconciliation), Changed (org-member erasures re-run automatically after a
  restore; result key `org_member_manual_review` replaced by `org_member_needs_review`).
- Seam: `backend/app/auth/constants.py` `CLOUD_SEAM_VERSION` is shared with the receipt and
  append-only plans (they bump 12→13). Changes here that an extension sees: new ledger status
  `cancelled`, new columns, `record_request` keyword-only kwargs, `erase_org_member_data(created_before=)`,
  new reconcile result keys. All additive except the renamed result key. Ride the same bump if
  released together; otherwise bump once more. Document in the seam changelog the project keeps
  beside the constant.
- Update `backend/app/tasks/CLAUDE.md` (reconcile phases) and `backend/app/api/CLAUDE.md`.

## 10. Risks

1. **Early erasure on code rollback** (§8). Mitigation: runbook + downgrade refusal.
2. **Index owner field missing or inconsistent** on some documents (old v3 docs): such docs are
   invisible to the check. Mitigation: mapping verification step, `unverified` reporting.
3. **First-run load** across a large historical ledger. Bounded per tick.
4. **SSO/SCIM re-provisioning** recreates an erased user on next IdP sync. A new account gets a new
   uuid, so reconcile does not treat it as resurrection, which is correct, but users may be
   surprised. Document it; deprovisioning at the IdP is the controller's step.
5. **Creation-time cutoff trusts clocks.** Skew between app and DB hosts of seconds is irrelevant
   at erasure granularity; document that timestamps are DB-side `now()`.

## 11. Decisions for the product owner

1. Is self-service erasure **off by default** (recommended) or on?
2. Grace default 14 days, max 21: acceptable?
3. Automate org-member re-runs with the creation-time cutoff (recommended) versus keeping manual
   review?
4. Should a pending request block **new uploads** during grace (recommended: no)?
