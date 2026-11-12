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
| A member inside one organization | `POST /api/org-admin/gdpr/erase-user/{user_uuid}` | organization admin, scheduled (see below) | Only the rows stamped with that organization. The account and personal data stay |
| One recording | `POST /api/admin/gdpr/erase-file/{file_uuid}` or `POST /api/org-admin/gdpr/erase-file/{file_uuid}` | super_admin, or an organization admin for a file of their own organization | The recording, its transcript and index documents, objects and caches |
| An organization | `POST /api/org-admin/gdpr/erase-organization?confirm=true` | the organization's owner, scheduled (see below) | Everything the organization owns, then the organization. Members keep their accounts |

Every call is recorded in the erasure ledger before anything is destroyed, comes back with a
`ledger_uuid` and a `receipt`, and is idempotent. A request that cannot finish (a legal hold, an
unreachable store) stays open and the reconciliation sweep retries it.

## Organization erasures are scheduled

An organization admin's member or organization erasure does not run when it is asked for. It is
recorded and runs after a delay, so a mistaken or hostile request can be stopped:

- **Who.** A member erasure is open to any organization admin. Erasing the whole organization is
  for its **owner** alone: another admin gets `403 org_owner_only`, and an organization without
  an owner (its owner's account was deleted) gets `409 org_owner_required` until a new owner is
  named. The owner is the earliest organization admin at the time the feature was added, and can
  hand the role to another admin of the same organization:

  ```bash
  curl -s -b cookies.txt -X POST "$API/api/org-admin/ownership/transfer" \
    -H 'Content-Type: application/json' -d '{"to_user_uuid": "..."}'
  ```

  The target must be an admin of the same organization (`409 not_an_org_admin` otherwise,
  including for an unknown account), and the hand-over is recorded as `org.owner.change`. The
  platform operator can name an owner where there is none.
- **Re-authentication.** The request body carries the admin's password, or a current code from
  their authenticator app: `{"password": "..."}` or `{"mfa_code": "123456"}`. A wrong value is
  `403 reauth_required`; an account that has neither (single sign-on without MFA) gets
  `409 reauth_unavailable` and has to ask the operator. The routes are refused under a support
  access grant.
- **Delay.** The answer is `202` with the request receipt (`ledger_uuid`, `requested_at`,
  `scheduled_for`, `sla_due_at`, `request_digest`). Nothing is destroyed yet. The delay is
  `gdpr.org_member_erasure_delay_hours` / `gdpr.org_erasure_delay_hours`, 72 hours by default,
  24 to 168, set by a super_admin with `PUT /api/admin/self-erasure`. No scheduled erasure may
  wait longer than 21 days (the self-service cap), so a delay plus one retry stays inside the
  one-month deadline, which starts at the request.
- **Cancel.** Any admin of the organization can list the open requests and withdraw one before
  `scheduled_for`; after that the answer is `409 already_executing`. The same list, with a Cancel
  button, is in Settings, Scheduled erasures (requesting one is API-only):

  ```bash
  curl -s -b cookies.txt "$API/api/org-admin/gdpr/erasures"
  curl -s -b cookies.txt -X POST "$API/api/org-admin/gdpr/erasures/$LEDGER_UUID/cancel"
  ```

- **Execution.** A job checks every hour (and the daily reconciliation again) and runs each
  request whose time has come, as the scope it names: a member erasure never deletes the
  person's account. A second request for the same member or organization while one is open is
  `409 request_exists`; a member erasure and the same person's own account-erasure request can
  both be open.
- **Notices.** The organization's admins, and for a member erasure the member, are told at
  four moments: when it is requested, about 24 hours before it runs (once; skipped when the whole
  delay is 24 hours or less), when it is cancelled and when it has completed. Each is an in-app
  notification (kept in the bell until dismissed) and, when a mail transport is configured, an
  email. A notice that cannot be sent never blocks the erasure. Self-service requests and the
  super_admin routes are not covered.
- **Audit.** The organization's audit log shows `gdpr.erasure.scheduled`,
  `gdpr.erasure.cancelled` and `gdpr.erasure.execution_started`, plus `org.owner.change` when the
  owner changes, then the erasure's own
  `admin.user.delete` records. The super_admin routes still erase at once.

## When a member leaves: organization content stays

Organization content belongs to the organization. When a member's account is deleted, or an
organization administrator hands it over, the organization-stamped rows the person owns are
re-owned by an administrator first, so deleting the account no longer destroys them. Personal
content (no organization) is not touched and goes with the account.

What moves (trashed files included): files, speaker profiles, speaker collections and clusters,
collections, tags, summary prompts, custom vocabulary, watch sources and the groups the person
owns. The rows that belong to a moved file go with it: its speakers (so the transcript keeps its
speaker names and the voiceprints are kept), its topic suggestions and its processing records.
What does not: chats, personal settings and comments (they stay with their author).
Shares and collection or tag memberships refer to rows by id, so they survive unchanged, and the
search index is re-pointed at the new owner, so search and chat keep finding the content.

- **Account deletion** (`DELETE /api/users/{uuid}` and the admin route) does it automatically for
  every organization the person is a member of. The receiving administrator is the organization's
  owner, else its longest-standing active admin. If the person is the only admin and still owns
  organization content, the deletion is refused with `409 no_transfer_target` until another admin
  exists. In the community edition nobody has an organization membership, so nothing changes.
- **Personal files wait out a grace period.** After the hand-over, an account that belongs to at
  least one organization is switched off (it cannot sign in and its sessions end) and the erase of
  its personal files is scheduled on the same ledger as the scheduled erasures above, after
  `gdpr.removed_member_grace_days` (default 14, 0 to 21; 0 erases at once). A super_admin sets it with
  `PUT /api/admin/self-erasure` (`removed_member_grace_days`) or under Settings, Privacy &
  Redaction, Removed members. The cap is the self-service one because the one-month deadline
  starts at the removal. While it runs, other people cannot reach the member's personal files
  through collection shares. An administrator can restore the account inside the period
  (Settings, Removed members, Restore, or `POST /api/admin/removed-members/{ledger_uuid}/restore`),
  which cancels the erase and switches the account back on if it was on when it was removed (an
  account an administrator had already switched off stays off); what was handed to the
  organization stays with it. Only a super_admin can restore a super_admin's account, or an
  account a super_admin removed. After the period the hourly job runs the account erasure; a file
  under a legal hold is never erased (the request is refused up front while one exists, and a
  hold placed during the wait leaves that file and keeps the account). Quarantine alone does not
  stop the erase, exactly as for an immediate account deletion: quarantine is a review state, the
  usual reason to quarantine an account's uploads is the abuse it is being removed for, and it is
  not a ground for keeping personal data under GDPR Art. 17(3). A takedown that must keep its
  evidence places a legal hold (the takedown default), and that wins. A notice that cannot be
  sent, or whose recipients cannot be looked up, never blocks the erase. The same four notices as
  above go to the administrator who removed the account and to the admins of its organizations.
  An account in no organization (every community-edition account) is still erased at once.
- **Member removal** (managed edition): the platform calls the same transfer before it removes the
  membership; core has no member-removal path of its own.
- **Legal holds.** A file under a hold is never re-owned: it is counted in `skipped_held`, and
  account deletion is still refused while the person owns a held file. Quarantine is left as it is.
- **Name conflicts.** A row whose name the recipient already uses (for example a speaker profile
  with the same name) stays with its owner and is counted in `skipped_conflict`; it is never renamed.
- **Search follows.** After the commit a background task (`org_content_transfer.restamp`, utility
  queue) re-points the search documents at the new owner. It retries with exponential backoff
  (up to 8 times, 15 minutes at most between tries) on OpenSearch errors and survives a lost
  worker, and a re-run changes nothing already moved.
- **Audit.** The organization's audit log shows `org.content.transfer` with the counts, never a title.
- **Hand-over time.** Each moved row records when it changed hands (`owner_since`). If the
  receiving administrator's own organization data was erased earlier, the daily erasure
  reconciliation counts handed-over rows from the hand-over, so they are not taken for a
  restore of the erased data and are not re-erased.
- **Stored objects do not move.** A recording's objects keep their keys (`user_<uploader id>/file_<id>/...`)
  and a profile photo keeps `avatars/<uploader id>/...`. Erasing the uploader's account later
  destroys only the objects of the files and profiles that erasure covers: it removes the whole
  `user_<id>/` and `avatars/<id>/` prefixes only when nothing under them belongs to another account
  or is held. Otherwise it removes each erased file's own directory, wherever its keys live, and
  every other object under those prefixes except the surviving files' directories and the
  surviving profiles' photos: objects no recording names (an old `user_<id>/youtube_<video id>/`
  thumbnail, a directory a failed upload left, a replaced photo) are the subject's data too. The
  receipt counts the same objects, and the same goes for unfinished uploads and the media mirror.

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
only. Every read is audited as `gdpr.receipt.view` with
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
| A recording under a legal hold (see [Legal holds](../user-guide/legal-holds.md)), and the account that owns it | Art. 17(3)(e). The erasure is `deferred` and retried when the hold lifts | `pg.media_file.legal_hold`, `pg.user` |
| The voiceprints of a held recording's speakers, the speaker profiles linked to one of them (with their photos) and the voice clusters that contain one | Art. 17(3)(e). The hold freezes the recording, and rebuilding a voiceprint means reprocessing it, which the hold refuses. Each kept profile also defers the erasure; they go when it is retried after the hold lifts | not counted as residual in `os.speakers` |
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
