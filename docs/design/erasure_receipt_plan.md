# Erasure receipt (GDPR Art. 17 / Art. 30): what was deleted, per store, and proof that nothing is left. Design

Status: **PLAN ONLY.** Nothing here is implemented. Written 2026-11-01 against `master` @ `e6843db20`
(Alembic head `v449_add_speaker_identity_source`). Target release: **v0.7.0**. Line numbers drift:
search for the quoted symbol.

Companion: `docs/design/append_only_audit_plan.md`. The receipt table defined here is insert-only
and is registered there.

## 0. Summary

Erasure already destroys data and records that it was asked for:

- `services/gdpr_erasure_service.py`: `erase_user`, `erase_org_member_data`, `erase_organization`.
- `services/erasure_ledger_service.py`: one ledger row per request, a journal outside the dump, and
  the restore and resurrection checks.
- `tasks/erasure_reconciliation.py`: retries deferred and failed entries.

What it does **not** do is say **what** was destroyed in each store, or **measure afterwards**
that nothing is left. `summary` has a dozen hand-picked counters and an `errors` list. The ledger
keeps the numeric counters only. The OpenSearch transcript and chunk deletes are verified per file
(`file_cleanup_service._count_surviving`), but the account as a whole is never re-measured.

The privacy purge (`services/privacy/purge.py`, `purge_residual.py`) already has the right shape:
exact counts per store group (`GroupedCounts`, what the store REPORTS as deleted), then a residual
measured in a fresh session after a refresh, where "unmeasurable" is `unverified` and never 0
(`Residual`). This plan **extends that machinery** to every store an erasure touches, and persists
the result as an **erasure receipt**:

- One row per measurement, in a new numbers-only table tied to the ledger entry.
- Readable by super_admin, and by an org admin for their own tenant's member erasures.
- It never contains personal data.

`summary["complete"]` becomes "no errors **and** the receipt is `verified`". An erasure that leaves
anything behind, or cannot measure a store, stays open in the ledger and the reconciliation sweep
retries it.

## 1. Store inventory (derived from code)

Dispositions: **D** deleted, **N** de-attributed (row survives, the subject's key NULLed or
detached), **R** retained by design (with the reason the receipt states), **T** TTL-bounded.

### 1.1 Postgres

The authoritative set is the FK graph into `user`, `media_file`, `speaker` and `organization`.
`tests/unit/test_user_deletion_fk_coverage.py` already derives the non-CASCADE part of it from
`pg_constraint`. The receipt inventory (§3.1) must account for **every** table reachable from
those parents (CASCADE edges included), and a test enforces it (R1). Starting classification:

| Group | Tables | Disp. |
|---|---|---|
| File and children | `media_file`, `transcript_segment`, `speaker`, `speaker_match`, `speaker_cluster_member`, `speaker_correction_carryover`, `comment`, `task`, `analytics`, `file_tag`, `collection_member`, `file_facts`, `file_pipeline_timing`, `topic_suggestion`, `watch_source_file` (confirm), `content_hold_file` | D |
| Owner-scoped | `speaker_profile`, `speaker_collection`(+`_member`), `speaker_cluster`, `collection` (personal), `tag` (personal), `chat_project`, `chat_conversation`, `chat_message`, `summary_prompt`, `user_setting`, `custom_vocabulary`, `watch_source`, `watch_source_email`, `upload_batch`, `user_media_source` | D |
| Account (user scope only) | `user`, `user_mfa`, `refresh_token`, `password_history`, `password_reset_token`, `email_verification_token`, `user_*_settings` (asr, llm, diarization), `organization_membership`, `user_group_member`, `collection_share`, `tag_share`, `user_invitation` rows for the address (confirm) | D |
| Tenant vocabulary | org `collection`, org `tag` (`user_id` NULLed, v420/v422) | N |
| Other users' chats citing the subject's files | `chat_message` citations (`file_cleanup_service.scrub_chat_citations`) | N (scrubbed) |
| Compliance records | `erasure_ledger` (surrogate ids only), `support_access_grant` (FKs SET NULL; `reason` is staff text), `support_access_use` (`owner_user_id` int, no FK), `auth_config_audit.changed_by`, `takedown_notice*`/`takedown_notification` (snapshot uuids), `usage_event` (SET NULL), `content_hold` actor keys | R/N: counted, with reason "Art. 17(3)(b)/(e), Art. 30" |
| Legal acceptance records | `legal_acceptance` (v451): `user_id` SET NULL, `ip_address`/`user_agent` blanked by `trg_legal_acceptance_guard`; document, version, hash, time, scope, organization snapshot and the `subject_user_uuid` surrogate kept. Pseudonymised, not anonymous | N (de-attributed): retained to `retain_until` (`legal.acceptance_retention_days`, default 2190), reason "Art. 17(3)(e); the operator states the lawful basis"; counted as `legal_acceptances_pseudonymised` |
| Legal hold | files skipped by `hold_guard.hold_states`, plus the account row they force to survive (`media_file.user_id` NO ACTION) | R: `legal_hold`, ledger `deferred` |

### 1.2 OpenSearch

Physical names come from `core/config.py` and `core/constants.py`.

| Logical key | Physical | Selector | Disp. |
|---|---|---|---|
| `os.transcripts` | `OPENSEARCH_TRANSCRIPT_INDEX` | `_id` = captured file uuids | D |
| `os.transcript_chunks` | `OPENSEARCH_CHUNKS_INDEX`, both planes (chunk and digest) | `file_uuid` term | D |
| `os.transcript_summaries` | `OPENSEARCH_SUMMARY_INDEX` | captured `file_id`s | D |
| `os.speakers` | alias, `_v3`, `_v4`, `_v3_backup` (`gdpr_erasure_service._speaker_index_names`) | speaker uuids, `profile_<uuid>`, `cluster_<uuid>` (captured), plus `user_id`/`organization_id` stamp (the existing `delete_speaker_docs_by_query`) | D |
| `os.audit` | `audit-logs-*` | events with `user_id` or `target_user_id` = subject | **R**: the audit trail (it includes `target_username`, i.e. the address, by design, `auth/audit.py:log`). Counted, never deleted. See D3 |
| `os.unclassified` | **any other non-system index** (`GET _cat/indices`, excluding names that start with `.`) | count of the subject's docs if the index has `user_id`/`file_uuid` fields, else presence | **Makes the receipt `unverified`**. An index nobody classified is exactly the store an erasure forgets (old versioned chunk indices after a migration, a future documents index) |
| `os.snapshots` | `opensearch_snapshot` repo | number of snapshots, oldest age in days | R: until rotation |

### 1.3 Object storage (`minio_service`, `storage_backend`, both buckets)

| Key | Selector | Disp. |
|---|---|---|
| `s3.media.file_objects` | `user_<uid>/file_<fid>/` for every captured `(uid, fid)` (original, thumbnails, `.playback.m4a`, waveform, masked caches; see the `_FILE_KEY_RE` docstring in `tasks/storage_orphan_sweep_task.py`). User scope: the whole `user_<uid>/` prefix (also covers `youtube_<id>/` thumbnails) | D |
| `s3.media.avatars` | `avatars/<uid>/` and captured avatar paths | D |
| `s3.media.temp` | `temp/preprocess/<file_uuid>*` (`TEMP_PREPROCESS_PREFIX`) | D |
| `s3.media.multipart` | in-progress multipart uploads under the subject's prefix (`list_multipart_uploads`) | D (abort) |
| `s3.cache.derived` | `CACHE_BUCKET_NAME` `derived/<fid>_` (`VideoProcessingService.DERIVED_CACHE_PREFIX`) | D |
| `s3.cache.bulk` | `bulk/` exports (1-day lifecycle `expire-bulk-exports`) | T: report the count still present |
| `s3.*.versions` | for every key above: `list_objects(include_version=True)`. Non-current versions and delete markers are deleted by `version_id`. If `get_bucket_versioning` is Enabled/Suspended and a version delete is refused (object lock) → `unverified` | D |
| `s3.media.erasure_journal` | `ERASURE_JOURNAL_OBJECT_PREFIX` | R: surrogate ids only |
| `mirror.media` | media mirror destination (`media_mirror_engine`, local dir or S3) when enabled | **R today**: the engine **never deletes** on the destination (its stated invariant), so erased media persists there indefinitely. See D2 |
| `backup.pg_dumps` | backup destination (`backup_service`) | R: count and oldest age; restore re-erasure via the ledger journal |

### 1.4 Redis and filesystem

| Key | Selector | Disp. |
|---|---|---|
| `redis.lockout` | `lockout:<email>` (`auth/lockout.py`, keyed by the login identifier) | **D, newly**. Today this key survives erasure for its TTL and holds the address. Delete it while the email is still known (before the user row goes) |
| `redis.task_state` | `task_progress*`, purge records and other keys by file id/uuid | T: counted by SCAN |
| `fs.temp` | `TEMP_DIR` entries named by a captured file uuid | D |
| `fs.audit_fallback` | `AUDIT_LOG_FALLBACK_PATH` lines naming the subject id | R: audit trail |
| `fs.erasure_journal` | `DATA_DIR/gdpr/erasure-journal.jsonl` | R: ids only |

## 2. Decisions for David

- **D1. File-level receipts.** (a) Receipts for the three ledger scopes only (user, org_member,
  organization). (b) Also add a ledger subject type `file` and erasure endpoints
  `POST /admin/gdpr/erase-file/{uuid}` / `POST /org-admin/gdpr/erase-file/{uuid}`, which run
  `purge_media_file` through the ledger and produce a receipt. Plain `DELETE /files/{uuid}` is
  unchanged. Recommendation: **(b)**. A data subject can ask for one recording to be erased, and
  this is the only way that request gets the same evidence.
- **D2. The media mirror.** (a) Add an erasure-only targeted delete to the mirror destination,
  limited to keys named by a ledger entry and audited. This breaks the "never deletes" invariant
  for that path only. (b) Keep never-delete and list it as retained, with the docs telling
  operators the mirror must be rotated or covered by their retention policy. Recommendation:
  **(a)**. Indefinite retention of erased media in a copy that never rotates is hard to defend
  under Art. 17, unlike rotated backups.
- **D3. The subject's audit events** (`os.audit`, `fs.audit_fallback`, which carry the address in
  `target_username`). (a) Retain as the Art. 17(3)(b)/(e) record and count them on the receipt
  (recommended, and consistent with the append-only plan). (b) Pseudonymise at erasure. That
  conflicts with append-only, and the docs would have to say audit can be rewritten.
- **D4. UI.** Keep receipts API-only in v0.7.0, consistent with the curl-only rule for erasure
  (`backend/app/api/CLAUDE.md` Compliance; `admin.lockedAccountsQuarantine.test.ts` forbids
  `gdpr|erase` methods in `AdminApi`). Recommended. Alternative: a read-only viewer in a separate
  API module plus about 15 i18n keys × 12 locales.
- **D5. Strictness.** Should an `os.unclassified` index (an unknown store) block `complete`?
  Recommended: yes. Allow an operator to classify an index with a SystemSettings allow-list
  (`erasure.receipt.ignored_indices`, names only, super_admin, audited).

## 3. Design

### 3.1 Inventory registry (WP-R1)

New package `backend/app/services/erasure_receipt/` (each module under about 300 lines):

- `inventory.py`: `ErasureStore(key: str, group: Literal["pg","os","s3","redis","fs","mirror","backup"],
  disposition: Literal["deleted","de_attributed","retained","ttl"], reason: str | None,
  count: Callable[[Scope, Captured, Ctx], int | None])`. `count` returns `None` when the store
  could not be measured. `STORES: tuple[ErasureStore, ...]`, plus `register_erasure_store()` for an
  edition's own stores (seam).
- `Scope` reuses the shape of `privacy.purge.PurgeScope`: one dataclass with `user`, `org_member`,
  `organization` (and `file` if D1 = b). Do not create a parallel scope type. Either generalize
  `PurgeScope` into `services/privacy/scope.py`, or add an `ErasureScope` that is a thin adapter
  over it. Pick one and delete the other path.
- `Captured`: the identifiers that are gone after the destroy and must be named for the
  post-check, captured **before** destroying: file ids, file uuids, `(user_id, file_id)` pairs,
  speaker/profile/cluster uuids, avatar paths, and the email (held **in memory only**, for the
  lockout key, never persisted). Build it from `file_cleanup_service.load_account_purge_plans` /
  `_load_purge_plan` and `purge_voiceprints.VoiceprintTargets`. Do not write a third plan loader.

### 3.2 Shared measurement types (WP-R2): extend, do not duplicate

- Move `Residual` (from `privacy/purge_residual.py`), `GroupedCounts` and `merge_grouped` (from
  `privacy/purge.py`) into `backend/app/services/privacy/measure.py`. Update every import
  (`rg "Residual|GroupedCounts|merge_grouped" backend`). **No re-export shims.**
- Widen `GroupedCounts` to the store groups `postgres`, `opensearch`, `s3`, `redis`, `fs`,
  `mirror`, `backup`. The purge keeps emitting only its two groups, and its API response shape is
  unchanged (`tests/api/test_privacy_purge.py` pins it).
- Reuse, do not re-implement: `gdpr_erasure_service.delete_speaker_docs_by_query`,
  `count_speaker_docs_by_ids`, `_speaker_index_names`, `file_cleanup_service._count_surviving`,
  `purge_voiceprints.org_stamp_clause`.

### 3.3 Flow inside `erase_*` (WP-R3)

1. Open the ledger entry (unchanged), then `captured = capture(db, scope)` and
   `before = measure(scope, captured)` in a **short session** that is closed afterwards (issue
   #440: no session across the S3/OpenSearch calls).
2. Destroy, unchanged, plus the new pieces: Redis `lockout:<email>`, S3 noncurrent versions and
   multipart uploads, `temp/preprocess`, and (D2 = a) mirror keys.
3. Refresh the OpenSearch indices, then `after = measure(scope, captured)` in a **fresh** session.
4. Build the receipt. `deleted[k] = before[k] - after[k]` for D stores; `residual[k] = after[k]`
   for D stores, which must be 0; `retained[k]` / `de_attributed[k]` / `ttl[k]` are counted;
   `unverified[k] = 1` for every `None`. `outcome` is `verified` when there is no residual and
   nothing unverified, `residual` when something was left behind, `unverified` when a store could
   not be measured.
5. Persist it (§3.4), append a numeric receipt line to the journal (`_append_journal`, `kind:
   "receipt"`), and set `summary["receipt"] = {uuid, outcome, residual_total, unverified_keys}`.
   `summary["complete"] = not errors and outcome == "verified"` (in `_resolve_outcome`). The ledger
   status follows: not verified → `failed`, so the sweep retries.
6. Schedule `erasure.receipt_reverify` (Celery, utility queue, `countdown=60`, the same reason as
   `purge.SECOND_PASS_DELAY_SECONDS`: catch a write that raced the erasure, such as a debounced
   reindex). It writes a second receipt row (`phase = "delayed"`). A non-verified delayed receipt
   re-opens the ledger entry (`status = failed`).

`erasure_reconciliation` retries call the same path, so every attempt gets receipts. The
resurrection check (`record_resurrection`) also produces a receipt after its re-erasure.

### 3.4 Storage: `erasure_receipt` table (WP-R4, migration)

New revision. **Derive the number**: `python3 scripts/release-tests/lib/alembic-head.py backend`,
name it `v<next>_add_erasure_receipt`. If the append-only plan's revision is in flight, chain after
it. Idempotent SQL.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `uuid` | uuid UNIQUE NOT NULL (uuid7) | the receipt reference |
| `ledger_entry_id` | int NOT NULL FK `erasure_ledger.id` **ON DELETE RESTRICT** | a ledger entry cannot vanish under its receipts |
| `attempt` | int NOT NULL | the ledger's `attempts` at generation |
| `phase` | varchar(10) CHECK IN (`immediate`,`delayed`,`resurrection`) | |
| `generated_at` | timestamptz NOT NULL default now() | |
| `outcome` | varchar(12) CHECK IN (`verified`,`residual`,`unverified`) | |
| `deleted`, `de_attributed`, `retained`, `ttl`, `residual`, `unverified` | jsonb NOT NULL default `'{}'` | each under two CHECKs (below) |
| `inventory_version` | int NOT NULL | bump when `STORES` changes meaning |
| `digest` | char(64) NOT NULL CHECK `digest ~ '^[0-9a-f]{64}$'` | sha256 of the canonical JSON of the row's data fields; tamper evidence and a value to quote to a regulator |

CHECKs per jsonb column, mirroring `models/erasure.COUNTERS_NUMERIC_SQL` (keep the exact
normalized spelling, see that constant's comment and `test_orm_ddl_divergence.py`):

- Values are numbers: `NOT jsonb_path_exists(col, '$.*?(@.type() != "number")')`.
- Keys are store keys: `NOT jsonb_path_exists(col, '$.keyvalue()?(!(@.key like_regex "^(pg|os|s3|redis|fs|mirror|backup)\\.[a-z0-9_.]{1,80}$"))')`.
  Verify the normalized form with `pg_get_constraintdef` and copy it back.

**No free-text column, no file uuid, no email, no physical index or bucket name** (logical keys
only). Model in `backend/app/models/erasure.py` (beside the ledger, add it to `__init__.py`).
Detection arm keyed on the last CHECK created. Insert-only: registered in the append-only plan's
`PROTECTED_TABLES` with `mutable_columns=()`. Add it to `test_user_deletion_fk_coverage.py` if the
derivation reaches it (it has no FK to `user`, so it should not).

### 3.5 API (WP-R5)

Pydantic schemas in `backend/app/schemas/erasure_receipt.py`: `ErasureReceiptResponse` (uuid,
ledger_uuid, subject_type, attempt, phase, generated_at, outcome, the six maps, inventory_version,
digest, plus a fixed `retained_reasons: dict[key, reason_code]` from the registry). No subject ids
are exposed except the ledger uuid.

| Route | Authority |
|---|---|
| `GET /api/admin/gdpr/erasures?status=&page=&per_page=` | super_admin. Ledger entries plus the latest receipt outcome, server-paginated |
| `GET /api/admin/gdpr/erasures/{ledger_uuid}/receipts` | super_admin. All receipts, newest first |
| `GET /api/org-admin/gdpr/erasures/{ledger_uuid}/receipts` | `require_org_admin` + `require_capability("organizations")`. Only `subject_type = org_member` (and `file` with an org) where `subject_organization_id == ctx.org_id`; anything else is one 404. A whole-organization erasure removes the org, so its receipt is super_admin only (it is also returned in the erase response) |
| existing `POST .../gdpr/erase-*` | response gains `receipt` (additive) |

Every read is audited (`AuditEventType.GDPR_RECEIPT_VIEW`, `gdpr.receipt.view`, ids only), rate
limited like the other admin reads, and refuses a support-access grant (`/admin` already does by
prefix; check the org-admin route). Regenerate `docs/security/platform-access-matrix.tsv` and keep
`test_route_privilege_tiers.py` green. **The data subject** has no account afterwards: the
controller forwards the receipt JSON (the erasure response already returns `ledger_uuid`).

### 3.6 UI and i18n

D4 = API-only: **no frontend change, no i18n keys.** If D4 changes, the viewer goes in a new
`frontend/src/lib/api/erasureReceipts.ts` (not `AdminApi`), plus all 12 locales.

## 4. Sequencing

1. WP-R2 (move the measure types; purge suite green, no behaviour change).
2. WP-R1 inventory plus the completeness test R1, red until every reachable table is classified.
3. WP-R4 migration plus model.
4. WP-R3 flow, including the new deletes (lockout key, versions, multipart, temp, mirror if D2 = a).
5. WP-R5 API.
6. D1 = b: widen `ck_erasure_ledger_subject` (`SUBJECT_TYPES` += `file`, `SUBJECT_IDENTIFIED_SQL`
   arm) in the same or a following revision, `erase_file` in `gdpr_erasure_service`, and the two
   endpoints. `erasure_reconciliation._rerun` gains the `file` branch.
7. Docs and CHANGELOG.

## 5. Tests (each seen red first, in a `git archive HEAD` tree under `mktemp -d -t ot-redcheck.XXXXXX`)

| # | Test | Proves | Red against |
|---|---|---|---|
| R1 | `unit/test_erasure_inventory_coverage.py::test_every_table_reachable_from_a_subject_is_classified` | Derives the FK closure from `pg_constraint` (all rules, all depths, from `user`, `media_file`, `speaker`, `organization`). Each table is in `STORES` or the exemption table with a reason. Guard: the closure has at least 30 tables | HEAD: module missing |
| R2 | `::test_every_non_system_index_pattern_is_classified` | Against live OpenSearch: every existing index maps to a logical key or `os.unclassified` | |
| R3 | `unit/test_erasure_receipt_schema.py` | Insert as owner: a string value → CheckViolation; key `"alice@example.com"` → CheckViolation; key `pg.media_file` with value 3 → ok. No textual column other than the enums (introspect `information_schema.columns`, like `test_erasure_ledger.py`) | HEAD: table missing |
| R4 | `unit/test_privacy_measure_move.py` | `Residual`, `GroupedCounts` and `merge_grouped` have exactly one definition (`rg`/AST); the purge still reports the same shape | |
| R5 | `integration/test_erasure_receipt_e2e.py::test_user_erasure_receipt_is_verified` (**the end-to-end test**; live Postgres, MinIO and OpenSearch, marker `integration`) | Seed a user with 2 files: real objects (original, thumbnail, derived cache object, avatar, a noncurrent version where versioning is enabled on a throwaway test bucket), transcript, chunks and summary docs, speaker/profile/cluster docs, chat, comment on another user's file, a `lockout:` key. Run `erase_user`. Assert: `deleted` counts equal the seeded counts per key; `residual` is all 0; `outcome == "verified"`; an **independent** probe (direct `list_objects(prefix=f"user_{uid}/", include_version=True)`, direct `count` per index, direct `SELECT count(*)` per table, `EXISTS lockout:`) finds nothing; the receipt row and its journal line contain no email, filename or file uuid (scan the serialized row) | HEAD: no receipt |
| R6 | `::test_residual_makes_the_erasure_partial` | Make one store refuse (monkeypatch the transcript delete to no-op): `outcome = residual`, `complete = False`, ledger `failed`, the audit outcome PARTIAL | Mutant: compute residual from `before` |
| R7 | `::test_unmeasurable_store_is_unverified_not_zero` | OpenSearch client unavailable during measure → `unverified` contains `os.*`, never a 0 | Mutant: `None → 0` |
| R8 | `::test_unclassified_index_blocks_verification` | Create `ot-test-stray-<uuid>` with a `user_id` doc → `unverified['os.unclassified']`; with the D5 allow-list → verified | |
| R9 | `::test_delayed_reverify_catches_a_racing_write` | Re-insert a transcript doc after the immediate receipt; run the reverify task → second receipt `residual`, ledger re-opened | |
| R10 | `::test_org_member_and_organization_receipts` | Both scopes, incl. de-attributed org tags and collections counted under `de_attributed` | |
| R11 | `::test_legal_hold_is_retained_not_residual` | Held file → `retained['pg.media_file']=1` with reason `legal_hold`; ledger `deferred` (existing semantics kept) | |
| R12 | `api/test_erasure_receipts_api.py` | super_admin list and read; org admin reads its own member's receipt, gets 404 for another org's and for an organization subject; 401/403 matrix; read audited | |
| R13 | (D2 = a) `integration/test_media_mirror_erasure.py` | A mirrored key named by the ledger is deleted and counted; an unrelated key is untouched (the never-delete invariant still holds for every other path; the existing invariant test stays green) | |
| R14 | (D1 = b) `api/test_erase_file_receipt.py` | File erasure produces a ledger entry `file` plus a verified receipt | |
| R15 | `unit/test_vNNN_migration_consistency.py` | Chain, detection arm, idempotency, downgrade (drops the table; docstring states this destroys compliance evidence, like v389) | |

Then `python3 scripts/audit-tests.py backend/tests` to 0 open findings.

## 6. Verification

1. `./opentr.sh start dev` (or a `--fresh` stack when other agents are active, with the `*_PORT`
   vars exported); run R5 to R11 with `-v` and confirm the new test names are in the output.
2. Manual curl drill on a fresh stack: create a throwaway user, upload a file, `POST
   /api/admin/gdpr/erase-user/{uuid}`, then `GET .../receipts`. Check `outcome: verified`, and that
   MinIO (`mc ls --versions`) and OpenSearch (`_count`) agree. Never against dev data (root CLAUDE.md).
3. `./scripts/run-integration-tests.sh` green. Pre-commit on both stages.

## 7. Upgrade and rollback

- Upgrade: the migration adds an empty table. Existing ledger entries have no receipts; the list
  endpoint shows `receipt: null` for them. Optional (decide during review): the reconciliation sweep
  produces a `resurrection`-phase receipt for completed entries when it next checks them. That
  gives every historic erasure a measured "nothing left" receipt with no destruction needed.
- Behaviour change to call out in the release notes: erasures that used to report `complete: true`
  while a store was unreachable or left something behind now report `false` and are retried.
  Ledger overdue alarms can appear on installs whose OpenSearch is routinely down.
- Rollback: `alembic downgrade` by one revision drops the receipts (compliance evidence; the
  journal's receipt lines survive). Code on v0.6.x ignores the journal `kind: "receipt"` lines
  (verify that `restore_from_journal` skips unknown kinds; if not, fix the reader in this release
  before writing the new kind).

## 8. Docs, CHANGELOG, seam

- New `docs-site/docs/operations/gdpr-erasure.md` (sidebar next to `operations/support-access`):
  the three or four scopes, the curl calls, the receipt fields and outcomes, what is retained and
  why (audit trail, ledger, journal, backups and snapshots until rotation, mirror per D2), how a
  restore is reconciled. Shorten `security-hardening.md`'s GDPR section to link it.
  `backend/app/services/CLAUDE.md` GDPR section: add rule 4, "an erasure is complete only when its
  receipt is verified; every new store gets an inventory entry".
- CHANGELOG `[Unreleased]`, **Added**: erasure receipts with per-store counts and post-erasure
  verification. **Fixed**: login lockout records, noncurrent object versions, pending multipart
  uploads and preprocessing temp objects now go with an erased account; (D2) erased media is
  removed from the media mirror.
- **Cloud seam: bump `CLOUD_SEAM_VERSION` 12 → 13** (or share 13 with the append-only plan if both
  land together; describe both in the v13 comment). Reasons: the `erase_*` summary gains `receipt`
  and `complete` is stricter (a downstream deletion webhook that treated `complete` as advisory will
  now see `false` more often); new hook `erasure_receipt.inventory.register_erasure_store` for stores
  an edition owns, without which the edition's stores show up as unclassified or are missed.

## 9. Risks

- **Measurement cost** on large accounts: counting per prefix with versions, plus OpenSearch
  counts. Batch as `count_speaker_docs_by_ids` does, and run the verification outside any open
  session.
- **False residuals** from concurrent legitimate writes (a colleague commenting on the org's files
  during an org-member erasure). The selectors must be scoped to the subject, not the tenant. R10
  covers it.
- **The key-shape CHECK only bounds shape.** Content is bounded by the registry plus R3/R5's PII
  scan of the row.
- **Unknown stores block completion by design (D5).** A noisy operator index needs the allow-list.
- **Capturing the email in memory** for the lockout key: it must never reach `summary`, the
  receipt, logs or the journal. R5 scans for it.

## Decisions (owner delegated to industry best practice, 2026-11-01)

These are fixed; implement as written.

7. Single-file erasure with a receipt (new erase-file endpoints): yes.
8. Media mirror: allow an erasure-only targeted delete; the normal mirror stays never-delete.
9. Audit events naming the erased subject: keep them and count them on the receipt (pseudonymising conflicts with append-only).
10. Receipts stay API-only, no viewer; the receipt table holds counts keyed by store name and a digest, never personal data.
11. An unclassified OpenSearch index blocks completion (receipt unverified), with an admin allow-list.

Cloud seam: `CLOUD_SEAM_VERSION` 12 to 13 in v0.7.0 (one bump if both land together).

Known residue to fix first, found while planning: the `lockout:<email>` Redis key, older object versions and unfinished uploads, and the never-deleting media mirror. The 0.6.1 erasure-residue test is expected to expose these.
