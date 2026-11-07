# Issue #1336: append-only identity history for speakers

Status: plan, written before the code (v0.7.0 lane `v070/identity-history`).

## Goal

`speaker.identity_source` and its record columns (#1280) describe the CURRENT name, link and
recommendation only. Every change must also leave an append-only history row: who (a person's
`user.id`, or the system), when, which model and version when a model made it, the before and
after value, the change kind, and the tenant stamp. No transcript text. Readable per speaker,
org-scoped, with a small history view in the speaker UI.

## Where the rows live, and why not the audit outbox

The governance audit outbox (`audit_outbox`, v459) is a delivery queue for an external audit
sink: the relay updates and deletes its rows, and it is exempt from the audit guard. Identity
history is product data that a user reads next to the speaker, that goes with the file when the
file goes, and that a voiceprint purge must be able to remove. So it gets its own table,
`speaker_identity_event`, written in the SAME transaction as the change (the outbox's
"recorded with the change" rule), and it is protected like the audit tables rather than queued
like the outbox.

## Table (`v462_add_speaker_identity_event`)

| column | type | notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `uuid` | UUID UNIQUE NOT NULL | the API key |
| `speaker_id` | INT NOT NULL FK `speaker` ON DELETE CASCADE | history goes with the speaker (and so with the file) |
| `organization_id` | INT NULL FK `organization` ON DELETE CASCADE | tenant stamp, copied from the speaker |
| `kind` | TEXT NOT NULL, CHECK closed set | `rename`, `name_cleared`, `link`, `unlink`, `suggested`, `suggestion_accepted`, `suggestion_rejected`, `suggestion_cleared`, `confirmed`, `skipped`, `merge`, `purge` |
| `actor_kind` | TEXT NOT NULL, CHECK `user`/`system` | |
| `actor_user_id` | INT NULL FK `user` ON DELETE SET NULL | de-attributed on erasure; CHECK: `system` rows have none |
| `origin` | TEXT NULL | the `origin` of the detail (`typed`, `voice_match`, `llm`, ...) |
| `model` | TEXT NULL | embedding model id or LLM model id, from the detail |
| `confidence` | DOUBLE NULL | |
| `voiceprint` | BOOLEAN NOT NULL DEFAULT false | the row records something a voiceprint produced |
| `before`, `after` | JSONB NULL, CHECK object | only the changed fields: `name`, `profile_id`, `suggested_name`, `suggestion_source`, `identity_source` |
| `detail` | JSONB NULL, CHECK object | copy of `identity_source_detail` / `suggestion_detail` (ids only, by construction in `speaker_identity_detail`) |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Index `ix_speaker_identity_event_speaker_created (speaker_id, created_at)`. Detection marker:
the guard trigger `trg_speaker_identity_event_guard`, created last.

A value a voiceprint made is never copied into a NON-voiceprint row: a person's rename of a
voiceprint-written name stores `before = {"withheld": "voiceprint"}` instead of the name. The
machine row that wrote it holds the value. That keeps the purge a plain DELETE of
`voiceprint = true` rows and keeps the hide rule a plain filter.

## Append-only in the database

Mechanism copied from the audit tables (v453 `audit_row_guard`, registry
`app/db/audit_protection.py`) with the own-guard shape of `legal_acceptance` (v451
`trg_legal_acceptance_guard`, registry `own_guard=..., app_delete=True`):

* registered in `PROTECTED_TABLES` with `own_guard="trg_speaker_identity_event_guard"`,
  `nullable_on_cascade=("actor_user_id",)`, `app_delete=True`, so `roles.reconcile` revokes
  UPDATE/TRUNCATE from the optional app role and `role_check` reports it;
* trigger function `speaker_identity_event_guard()` (BEFORE UPDATE OR DELETE per row, plus a
  BEFORE TRUNCATE statement trigger):
  - maintenance (`audit_maintenance_allowed`, v453) passes everything, owner only;
  - UPDATE: refused unless the only change is `actor_user_id` going to NULL (the user
    deletion's `ON DELETE SET NULL`);
  - DELETE: allowed when cascaded from the speaker / organization being deleted
    (`pg_trigger_depth() > 1`: the RI cascade runs the delete from inside its own trigger), or
    for `voiceprint` rows while `opentranscribe.voiceprint_purge = 'on'` (set LOCAL by the
    purge, the same switch shape as v451's `opentranscribe.legal_retention_sweep`); refused
    otherwise;
  - TRUNCATE: refused.

## One seam

All writes of a speaker's name, link and suggestion already go through the helpers of
`app/utils/speaker_identity_source.py`, enforced per write by
`tests/unit/test_speaker_identity_source_discipline.py`. The history hangs off the same seam:

1. **ORM writes.** The helpers stay pure (no session), and each one now also leaves a small
   attribution note on the row's `InstanceState.info` (actor, kind override). A Session
   `before_flush` listener (`app/services/speaker_identity_history.py`, registered from
   `app/models/speaker.py` exactly like `core/infra_requeue_reset.py` is from `models/media.py`)
   reads each dirty or new `Speaker`'s attribute history (SQLAlchemy's documented
   "versioning with a history table" pattern: `before_flush` + `attributes.get_history`) and
   adds one `SpeakerIdentityEvent` per speaker per flush, in the same transaction. The tracked
   columns get `active_history=True` so the old value is loaded even when the writer never read
   it. An unannotated write is still recorded (as `system`), never lost.
2. **Bulk writes** (`update(Speaker)...values(...)` in the purge) bypass the ORM, so they call
   `record_bulk_identity_events(db, speaker_ids, kind=..., detail=...)` in the same function;
   the discipline test is extended so a bulk/raw-SQL write of a tracked field without that call
   in a covering branch fails.
3. The helpers that do not restamp the source (`after_unlink`, `after_name_cleared`,
   `clear_suggestion`, `mark_review_skipped`) take a REQUIRED keyword `user_id` so every caller
   states the actor; `record_profile_rename(speaker, user_id=...)` is new for the profile-rename
   propagation, which used to be exempt with "the profile's own change history is issue #1336";
   `record_speaker_merge(target, user_id=..., merged_speaker_uuid=...)` records a speaker merge
   on the surviving speaker.

Write sites (verified against the code, `rg` + the discipline test's scan): see the commit
body of the seam commit for the per-file list.

## Erasure and privacy

* `services/erasure_receipt/stores_pg.py`: `speaker_identity_event` goes in `EXEMPT_TABLES` as
  "removed by ON DELETE CASCADE with the speaker; the actor key is FK SET NULL" (neighbours:
  `speaker_correction_carryover`, `organization_policy`). An event the subject made on another
  account's file stays with that file, de-attributed.
* Voiceprint purge: deletes the scope's `voiceprint` events under the switch, then records one
  `purge` event (no values) per speaker it changed.
* Read: while a file's voiceprints are off, `voiceprint` rows are withheld (same rule as
  `hides_provenance`); the actor is shown only to the viewers who see `identity.by`
  (`speaker_provenance_view`), and an erased account reads as `erased`.

## API

`GET /api/speakers/{speaker_uuid}/identity-history` (new sub-router `speakers_history.py`,
registered before the catch-all): `require_speaker_access` (tenant, quarantine, share, platform
grant), newest first, `limit` (default 50, max 200). Added to
`docs/security/platform-access-matrix.tsv` by the generator; frontend caller satisfies
`test_route_has_a_caller`.

## UI

A "History" item in the speaker menu opens a small modal listing the events (kind, when, who,
model, before -> after). 12 locales with real translations, `ar` RTL, light/dark.

## Verification

* Unit: kind derivation (pure), discipline test (new must-fire and must-stay-clean cases),
  registry tests.
* DB (throwaway Postgres): flush writes one row per change with before/after; bulk purge
  events; the guard refuses UPDATE/DELETE/TRUNCATE, allows cascade, SET NULL and the purge
  switch; migration consistency in a private scratch DB (idempotent re-run, downgrade);
  detection ladder rung; erasure inventory coverage; audit registry.
* API: history endpoint returns events, 404 across tenants, hides voiceprint rows when off.
* Frontend: vitest for the modal, `npm run check`, i18n parity.
