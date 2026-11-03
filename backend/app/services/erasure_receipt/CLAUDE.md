# services/erasure_receipt — GDPR Art. 17 erasure

Moved intact from `backend/app/services/CLAUDE.md`. The erasure orchestration lives beside this package in `gdpr_erasure_service.py`, `gdpr_erasure_steps.py`, `gdpr_voiceprint_erasure.py`, `opensearch_erasure.py`, `media_mirror_erasure.py` and `self_erasure_service.py`.

## GDPR Art. 17 erasure — the three rules that are not obvious (issue #442)

`gdpr_erasure_service.py` destroys the data; `erasure_ledger_service.py` records that it
was asked for; `tasks/erasure_reconciliation.py` finishes what could not finish. All
three are needed, and dropping any one reproduces a defect that shipped.

- **The ledger must not contain the personal data it records the destruction of.** "We
  erased alice@example.com" *containing* the address is a copy of the thing that was
  supposed to be destroyed, in a table designed to outlive it. So `erasure_ledger` has
  **no free-text column at all** — every textual column is a short enum with a CHECK —
  and the one JSONB column is nailed shut by `ck_erasure_ledger_counters_numeric`
  (`jsonb_path_exists`, IMMUTABLE, so it is legal in a CHECK where a subquery is not).
  There is deliberately **no email hash** either: a hash of a value from a guessable
  space is pseudonymous personal data (Recital 26), not anonymous. Subjects are named by
  `id` + `uuid` only, which are meaningless once the row they point at is gone — and
  meaningful again exactly when a restore brings it back, which is the property the
  resurrection check relies on. `record_outcome` drops `summary["errors"]` entirely: its
  entries carry file UUIDs and driver messages that quote filenames and storage paths.
- **`subject_user_id` / `subject_organization_id` are NOT foreign keys, on purpose.**
  They name the rows the table records the destruction of. A real FK would either block
  the delete (`NO ACTION`) or null the column (`SET NULL`) — and nulling it destroys the
  only key the reconciliation sweep has. Referential integrity is the property these
  columns must not have. `actor_user_id` **is** an FK, `ON DELETE SET NULL` like every
  actor FK since v387; `tests/unit/test_user_deletion_fk_coverage.py` records the
  disposition.
- **The ledger cannot only be a table.** It lives in Postgres, so restoring a dump taken
  *before* an erasure destroys the record of the erasure along with the erasure — it
  would have to survive its own failure mode. Every entry is therefore also appended to
  a line-delimited journal under `DATA_DIR/gdpr/`, outside the dump;
  `restore_from_journal` re-opens anything the database has lost, keeping the ORIGINAL
  `requested_at`/`sla_due_at` so a restore cannot buy another month of Art. 12(3) time.
  Its limit is honest: one file on one volume. Off-host replication is deployment
  config; the audit stream is the second copy that already leaves the host.
  `ERASURE_JOURNAL_BACKEND=object_storage` writes one object per entry to the media
  bucket instead, for containers with no durable writable volume (read-only root fs) —
  never "fix" an unwritable `DATA_DIR` with an ephemeral mount; the journal would die
  with the pod.

- **An erasure is complete only when its receipt is `verified`; every new store gets an inventory
  entry** (issue #1353). `services/erasure_receipt/` counts every store before the destroy and
  again after it in a fresh session (`flow.begin` / `flow.finish`, around the destroy in each
  `erase_*`), stores the numbers-only `erasure_receipt` row (`receipt.persist_receipt`), schedules a
  60 s re-check (`tasks/erasure_receipt_reverify.py`) and puts a non-`verified` outcome on
  `summary["errors"]`, which is what keeps `complete` false and the ledger entry open. A store the
  registry cannot count is `unverified`, never 0 (`measure.measure_all` maps a raise to `None`). A
  new Postgres table reachable from a user, file, speaker or organization needs a `PgStore` in
  `stores_pg.py` or an `EXEMPT_TABLES` reason (`tests/unit/test_erasure_inventory_coverage.py`
  derives the foreign-key closure and fails otherwise); a new OpenSearch index needs a pattern in
  `stores_external.INDEX_PATTERNS`; an edition's own stores use `register_erasure_store`. The
  deletes the canonical per-file destroy never made (lockout record, noncurrent versions, the
  account's whole object prefix, multipart uploads, temp objects, derived cache, mirror keys) are
  `erasure_receipt/destroy_extras.py`; files under a legal hold are excluded from capture, so they
  are `retained`, never `residual`. `ErasureScope` is the erasure's own scope type (it is not
  `privacy.purge.PurgeScope`, whose `user` kind is one tenant); `Residual` and `GroupedCounts` live
  in `privacy/measure.py` for both. Media-mirror deletes go through `media_mirror_erasure.py` only.

Two judgement calls worth not re-litigating blindly:

- **A legal hold retains the ACCOUNT, not just the file, and that is forced by the
  schema** — `media_file.user_id` is a plain `NO ACTION` FK, so `DELETE FROM "user"`
  raises while a held file exists. Art. 17(3)(e) only justifies retaining the *file*, so
  the account's survival is a side effect, made temporary by the ledger entry and the
  sweep. Making it genuinely file-granular means anonymising the account instead —
  a design change, not a patch.
- **`org_member` entries are re-run after a restore, but only up to the erasure time** (#1355).
  That scope never deletes the `user` row, and "does the member have org rows again?" alone is
  what a legitimate upload the next day looks like. So `erasure_org_member_rerun.rerun_state`
  counts only rows created **at or before** the entry's `completed_at`
  (`erase_org_member_data(created_before=)`; media files use `upload_time`, the rest
  `created_at`), a NULL timestamp is reported (`org_member_needs_review`) and never deleted, and
  a time-bounded run takes no erasure receipt (the receipt measures the whole scope, which
  legitimately holds newer rows). **`completed_at` doubles as that bound once the entry has been
  resurrected**: `_completion_time` keeps it through a deferral and through the re-run's own
  completion, so a retry is still bounded; do not "refresh" it.
- **Self-service erasure is a ledger entry, not a second state machine** (#1354):
  `self_erasure_service` (request / cancel / `execute_due_requests`), `self_erasure_reauth`
  (password or TOTP), `deferred` / `grace_period` / `scheduled_for`, terminal status `cancelled`.
  `open_entries` excludes every grace-period entry (phase 0 of the sweep runs the due ones) and
  `overdue_entries` excludes `cancelled`. Journal lines carry the grace fields and the **last line
  per uuid wins** on restore, so a cancelled request is never revived.
- **Index-level resurrection** (#1355): `erasure_index_reconcile` reuses the receipt registry
  (`stores_external.classify_index`) and the single speaker-index enumerator; one `filters`
  aggregation per index for up to 500 entries (`index_checked_at` cursor); an unreachable cluster
  is `index_unverified` and does not advance the cursor; a live user row with the same id makes the
  subject ambiguous and it is skipped.
