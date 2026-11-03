---
sidebar_position: 11
title: GDPR Erasure and Receipts
description: How to erase a user, a member, a recording or an organization, what the erasure receipt proves, and what is kept on purpose
---

# GDPR Erasure and Receipts

An erasure (GDPR Art. 17) removes a subject's data from every store and then **measures** that
nothing is left. The measurement is stored as an **erasure receipt**: counts per store, a verdict
and a digest. A receipt never contains personal data, so it can be forwarded to the data subject
or a regulator as it is.

There is no button for any of this. Erasure is irreversible and is driven by API calls.

## What can be erased

| Scope | Call | Who | What goes |
|---|---|---|---|
| A user (account-wide) | `POST /api/admin/gdpr/erase-user/{user_uuid}` | super_admin | Every recording, profile, voiceprint, chat and setting the user owns in every tenant, then the account |
| A member inside one organization | `POST /api/org-admin/gdpr/erase-user/{user_uuid}` | organization admin | Only the rows stamped with that organization. The account and personal data stay |
| One recording | `POST /api/admin/gdpr/erase-file/{file_uuid}` or `POST /api/org-admin/gdpr/erase-file/{file_uuid}` | super_admin, or an organization admin for a file of their own organization | The recording, its transcript and index documents, objects and caches |
| An organization | `POST /api/org-admin/gdpr/erase-organization?confirm=true` | organization admin | Everything the organization owns, then the organization. Members keep their accounts |

Every call is recorded in the erasure ledger before anything is destroyed, comes back with a
`ledger_uuid` and a `receipt`, and is idempotent. A request that cannot finish (a legal hold, an
unreachable store) stays open and the reconciliation sweep retries it.

## The receipt

`summary.receipt` carries `uuid`, `outcome`, `residual_total`, `unverified_keys` and `digest`.
The full record is read with:

```bash
curl -s -b cookies.txt "$API/api/admin/gdpr/erasures?status=deferred"
curl -s -b cookies.txt "$API/api/admin/gdpr/erasures/$LEDGER_UUID/receipts"
# an organization admin, for a member or file erasure of their own organization:
curl -s -b cookies.txt "$API/api/org-admin/gdpr/erasures/$LEDGER_UUID/receipts"
```

A whole-organization erasure removes the organization, so its receipt is readable by super_admin
only (and is returned in the erase response). Every read is audited as `gdpr.receipt.view` with
ids only.

Each receipt has six maps of counts, keyed by store name such as `pg.media_file`,
`os.transcripts`, `s3.media.file_objects` or `redis.lockout`:

| Map | Meaning |
|---|---|
| `deleted` | Found before, gone after (the difference of two counts, not the number of ids asked for) |
| `de_attributed` | The row stays with the subject's key removed (an organization's tags and collections) |
| `retained` | Kept on purpose, counted, with a fixed reason in `retained_reasons` |
| `ttl` | Expires on its own |
| `residual` | Still there after the erasure, for a store that should be empty. Must be 0 |
| `unverified` | Could not be measured. **Never read as 0** |

`outcome` is `verified` (nothing left, everything measured), `residual` (something was left) or
`unverified` (a store could not be asked). Only `verified` makes `summary.complete` true. The
ledger entry of any other outcome stays open and the sweep retries it. `digest` is the sha256 of
the receipt's data, a value to quote.

The counting runs in a fresh database session after the stores have refreshed, with identifiers
captured before the destroy. A second receipt (`phase: delayed`) is taken a minute later to catch
a write that raced the erasure, such as a debounced reindex; if it finds anything the entry is
re-opened. A receipt taken after a restore re-erasure has `phase: resurrection`.

### Stores it counts

PostgreSQL tables reachable from a user, file, speaker or organization by a foreign key; the
OpenSearch transcript, chunk, summary and speaker (voiceprint) indices; the media bucket
(originals, thumbnails, renditions, `temp/preprocess`, avatars, **every noncurrent version and
delete marker**, unfinished multipart uploads) and the derived cache; the login-lockout record in
Redis (which holds the address); temporary files; and the media mirror. A test derives the
foreign-key closure from the live schema and fails when a table is neither counted nor exempted
with a reason, so a new table cannot silently become a store an erasure forgets.

### An unclassified OpenSearch index blocks verification

An OpenSearch index that no store covers (an old versioned index, a plugin's, a future one) makes
the receipt `unverified`: it is exactly where an erasure forgets data. Classify it in code, or
declare that it holds no personal data:

```bash
curl -s -b cookies.txt -X PUT "$API/api/admin/gdpr/erasure-receipt/ignored-indices" \
  -H 'Content-Type: application/json' -d '{"names": ["my-index-without-personal-data"]}'
```

The list holds names only. System indices (leading `.`) and the Query Insights plugin's rolling
`top_queries-*` index are not application data and are not listed here; the plugin rotates that
index on its own retention.

## What is kept on purpose

| Kept | Why | Receipt key |
|---|---|---|
| The audit trail: audit events that name the subject, in OpenSearch and in the fallback file | Art. 17(3)(b)/(e): the record that the erasure and other actions happened. Counted, never deleted | `os.audit` |
| The erasure ledger and its journal | Surrogate ids only, no address or name; it must outlive the data to prove the erasure | `pg.erasure_ledger` |
| Acceptance records of legal documents | Pseudonymised (the account link, address and browser string are removed; document, version, hash, time, scope and organization remain) and kept until the retention date, `legal.acceptance_retention_days`. The operator states the lawful basis | `pg.legal_acceptance` (counted as de-attributed) |
| A recording under a legal hold, and the account that owns it | Art. 17(3)(e). The erasure is `deferred` and retried when the hold lifts | `pg.media_file.legal_hold`, `pg.user` |
| Database backups and OpenSearch snapshots | Until they rotate. A restore is reconciled: the ledger journal re-erases a subject that comes back | `backup.pg_dumps` |
| An organization's tags and collections the subject created | They belong to the tenant and sit on colleagues' files. They stay with the subject removed | `pg.tag.org`, `pg.collection.org` |

### The media mirror

The media mirror never deletes on its destination during a mirror run. An erasure is the one
exception: it deletes the erased subject's keys (and only those, derived from the erasure's own
ledger entry) from the mirror destination, and counts them. Without that, erased media would
persist indefinitely in a copy that never rotates.

## Limits worth knowing

- A retry of an already-erased subject has nothing left to name, so its receipt covers only what
  can still be addressed (object prefixes of an account, the lockout record is not re-checked).
- The address used for the lockout key is held in memory for the duration of the call and is never
  written to the receipt, the ledger, the journal, a log line or the scheduled re-check.
- Rolling back to an older release is safe: the journal's receipt lines are skipped by the reader.
  Downgrading the database drops the receipts (compliance evidence), so do not do that after any
  erasure has been issued.
