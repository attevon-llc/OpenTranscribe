# Abuse, DMCA & Safe-Harbor Takedown Policy

OpenTranscribe lets users ingest media from arbitrary URLs (yt-dlp, 1800+
platforms) and upload arbitrary files. Any service that accepts third-party
content needs a published intake address and a working takedown path to qualify
for safe-harbor protection (DMCA §512 in the US, the e-Commerce Directive /
DSA in the EU, and equivalents elsewhere). This document describes the intake,
the Acceptable Use Policy summary, the repeat-infringer policy, the response
SLA, and the technical enforcement mechanism.

> **Self-host operators:** this is a template. You are the service operator for
> your deployment — set `ABUSE_CONTACT_EMAIL`, publish your own contact address,
> and adapt the SLAs/jurisdiction to your situation. The technical enforcement
> (admin quarantine/takedown) ships in the product; the policy text is yours.

## Intake — `abuse@`

Reports of abuse, copyright infringement (DMCA notices), and other illegal or
policy-violating content go to the **abuse contact address**, configured via the
`ABUSE_CONTACT_EMAIL` environment variable (surfaced in the UI/API; empty = not
configured). A typical convention is `abuse@<your-domain>`.

A valid **DMCA takedown notice** should include:

1. A physical or electronic signature of the copyright owner (or authorized agent).
2. Identification of the copyrighted work claimed to be infringed.
3. Identification of the material to be removed and information reasonably
   sufficient to locate it (the OpenTranscribe **file UUID** or share URL is
   ideal).
4. The complainant's contact information (address, phone, email).
5. A good-faith statement that the use is not authorized.
6. A statement, under penalty of perjury, that the information is accurate and
   the complainant is authorized to act.

**Counter-notices** (from the uploader, disputing a takedown) should reference
the same file UUID and include the uploader's contact information and a
good-faith statement. Uploaders learn of a takedown through an **in-app
notification** (see [Owner notice](#owner-notice-dmca-512g) below) that carries
the takedown reason, the file UUID to reference, and the abuse-contact address
to send the counter-notice to. Counter-notices are handled out-of-band by the
operator; once resolved, an admin restores the file via the release endpoint
and the uploader is notified that access is restored.

## Acceptable Use Policy (AUP) summary

Users may **not** upload, ingest, or process content that:

- Infringes a third party's copyright, trademark, or other IP rights.
- Is unlawful, defamatory, or violates another person's privacy or publicity
  rights (including non-consensual intimate imagery or unlawful surveillance
  recordings).
- Contains CSAM or any content that sexually exploits minors — this is reported
  to the relevant authorities and the account is terminated immediately.
- Promotes terrorism, violence, or illegal activity.
- They do not have the lawful right to record, store, transcribe, or share.

> **Note:** content **redaction** (PII / profanity / toxicity masking) masks
> *displayed text* — it is a read-time transform, **not** a takedown. Removing
> content from public availability is the **quarantine/takedown** mechanism
> below.

## Repeat-infringer policy

Per DMCA §512(i), the service terminates the accounts of **repeat infringers**
in appropriate circumstances:

- Each substantiated takedown is recorded against the uploading account (the
  takedown action is written to the audit log, keyed by `quarantined_by` and the
  file's owner).
- An account that accrues **three (3) substantiated infringement strikes**
  within a rolling 12-month window is subject to suspension or termination, at
  the operator's discretion.
- Strikes are removed if the corresponding takedown is reversed (e.g. a
  successful counter-notice or a withdrawn complaint).

## Response SLA

| Action | Target |
| --- | --- |
| Acknowledge receipt of a complete abuse/DMCA notice | **48 hours** |
| Quarantine clearly-infringing or illegal content once identified | **promptly**, typically within 24 hours of triage |
| Forward a valid counter-notice to the complainant | **48 hours** |
| Restore content after a valid counter-notice (absent a court action) | **10–14 business days** (per DMCA §512(g)) |

CSAM and imminent-harm reports are handled immediately, outside the standard
queue.

## Technical enforcement — admin quarantine / takedown

The enforcement mechanism is a per-file **quarantine** (takedown) on
`MediaFile`, independent of the processing status, so even a fully-transcribed
file can be taken down and later restored to exactly its prior state.

**State** (added in migration `v370_add_media_file_quarantine`):

- `is_quarantined` — the authoritative takedown flag (default `false`).
- `quarantine_reason` — free-text reason (DMCA notice ref, AUP clause, report id).
- `quarantined_at` / `quarantined_by` — when, and which admin applied it.
- `legal_hold` — source-of-truth flag that the object must not be deleted while a
  dispute/notice is open. Mirrored best-effort onto the S3/MinIO object as an
  object **legal-hold** (requires object-lock on the bucket; degrades gracefully
  when unavailable, e.g. the dev MinIO).

**Effect:** a quarantined file is **excluded from every read surface** for
non-admins — gallery list, file detail, search results/snippets, streaming,
download, and thumbnail all return *not found* (404). The original media and
transcript are **never deleted** by a takedown — hiding is a read-time transform,
so the row survives for the audit and appeal trail. Admins retain visibility to
review and release.

### Owner notice (DMCA §512(g))

Because the quarantined file 404s for its owner on every surface, the owner is
told about the takedown through a **persistent in-app notification** (WebSocket
event `file_takedown`, kept in the notification panel) sent when the file is
quarantined. The notice contains:

- **which file** was taken down (media title, falling back to the filename) and
  its **file UUID** (to reference in a counter-notice);
- the **admin-recorded takedown reason** (`quarantine_reason` — the DMCA notice
  ref / AUP clause the admin entered);
- **counter-notice instructions** pointing at the deployment's
  `ABUSE_CONTACT_EMAIL`; when that variable is unset, the notice directs the
  owner to contact the service operator.

The identity of the acting admin is **never disclosed** to the owner, and the
file itself **stays hidden** (the 404 gate above is unchanged) — the
notification is the owner's §512(g) surface. When an admin releases the file
(e.g. after a successful counter-notice), the owner receives a second
notification (`file_takedown_released`) that access is restored, with a link to
the file. Notification delivery is best-effort: a delivery failure is logged
and **never blocks the takedown or the release**.

**Admin endpoints** (admin / super-admin; every action is audit-logged):

| Method & path | Purpose |
| --- | --- |
| `GET  /api/admin/files/quarantined` | List taken-down files for review (newest first). |
| `POST /api/admin/files/{uuid}/quarantine` | Take a file down (`reason`, optional `legal_hold`). |
| `POST /api/admin/files/{uuid}/release` | Release a file (restore access, optional clear legal-hold). |

Audit event types: `admin.file.quarantine`, `admin.file.release` (in the FedRAMP
AU-2/AU-3 audit log).

## Presigned URL revocation (issue #907, FIXED)

A takedown revokes **access going forward** — the file 404s on every list/detail/stream/
download/search-snippet surface, immediately. Historically it did **not** revoke a presigned
MinIO URL that had already been handed out before the takedown: every presigned media URL is
valid for `MEDIA_URL_EXPIRE_SECONDS` (6 hours, default) from the moment it was minted, so a URL
a viewer opened minutes before an admin quarantined the file kept working for the rest of that
window regardless of the takedown. **This is now fixed for the bundled MinIO backend.**

### The mechanism

Browser-facing GET presigns — `GET /api/files/{uuid}/stream-url` (video/audio/thumbnail,
`backend/app/api/endpoints/files/__init__.py`) and the download/preview equivalents
(`minio_service.get_presigned_download_url`, `MinIOService.get_presigned_url`) — are signed by
a dedicated, **non-root MinIO service-account identity** (`backend/app/services/
storage_presign_identity.py`) instead of the root credential. That identity's inline policy
**Denies `s3:GetObject`** on any object carrying the object tag
`STORAGE_QUARANTINE_TAG_KEY=true` (`ot-quarantine`, `backend/app/core/constants.py`), with a
`StringEquals` condition — MinIO evaluates the Deny against the object's *current* tags on every
request, not at signing time, so:

- **Quarantining a file tags it** (`takedown_service.quarantine_file` →
  `minio_service.set_object_quarantine_tag`, both the primary object and the thumbnail). A URL
  minted **before** the takedown 403s the instant the tag lands — same URL, same signature, no
  re-mint needed.
- **Releasing a file untags it** (`takedown_service.release_file`), which restores the SAME
  pre-existing URL to 200. Because a still-tagged object also 403s a *brand-new* presigned URL,
  the untag is retried up to 3 times with a short backoff before the release logs an ERROR (not
  a warning) — a failed untag leaves the file released in the database but unplayable until it
  is retried and succeeds.
- The identity is least-privilege by construction: it cannot list the bucket and cannot clear
  the quarantine tag itself (no `s3:ListBucket`/`s3:PutObjectTagging`/`s3:DeleteObjectTagging`
  in its policy) — the identity a Deny is keyed on must not be able to remove its own key.

### Honest limitations

- **MinIO-only.** There is no admin API on native AWS S3 (`STORAGE_BACKEND=s3`): the quarantine
  tag is still written to the object (harmless), but nothing enforces a Deny on it. An operator
  running against real S3 must attach an equivalent bucket policy by hand:

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::<your-media-bucket>/*",
        "Condition": {
          "StringEquals": { "s3:ExistingObjectTag/ot-quarantine": "true" }
        }
      }
    ]
  }
  ```

  Note this Denies the **whole bucket policy**, including root-signed requests — S3 bucket
  policies (unlike root-signed requests under a MinIO service-account Deny) apply regardless of
  which principal signed the request, so this is actually *stronger* than the MinIO mechanism on
  that one axis, at the cost of needing to be applied by hand rather than shipped automatically.

- **Fails open, visibly.** If the restricted identity cannot be provisioned (MinIO admin API
  unreachable, `STORAGE_PRESIGN_IDENTITY_ENABLED=false`, or a non-MinIO S3-compatible backend
  with no admin API), presigning silently falls back to the root client — today's pre-#907
  behavior, no regression, but inert. This is made visible: an ERROR is logged once per process,
  and the takedown/release audit event records whether revocation actually happened
  (`presign_revoked` / `presign_tag_cleared` in the event's `extra`).

- **Admin review sees the same 403.** Admins presign through the same restricted identity, so an
  admin reviewing a quarantined file's *media* also gets a 403 on its presigned URL — this is a
  deliberate decision, not a gap: a root-signed bypass for admin review would reopen a signed URL
  that outlives the review window. Admins review via the transcript text, which this mechanism
  does not touch.

- **Reads only, and only going forward.** This revokes a presigned URL's ability to be used
  again; it cannot un-download bytes a client already fetched before the tag landed.

Four cheaper mitigations were evaluated and rejected before this design: shortening the TTL
(breaks long-recording playback for legitimate viewers), a bucket-policy Deny alone (bypassed —
root-signed URLs ignore bucket policy, which is exactly why this uses a restricted *identity*
instead), rotating the signing credential (invalidates every other user's unrelated in-flight
URLs, not just the taken-down file's), and renaming/moving the object key (conflicts with
`legal_hold` blocking deletes, and isn't cleanly reversible on release).

## Configuration reference

| Setting | Default | Purpose |
| --- | --- | --- |
| `ABUSE_CONTACT_EMAIL` | `""` (unset) | Published intake address surfaced in the UI/API and included in owner takedown notices as the counter-notice contact. |

The legal-hold S3 object-lock is best-effort and requires the storage bucket to
be created with object-lock enabled; the DB `legal_hold` flag is always the
source of truth.
