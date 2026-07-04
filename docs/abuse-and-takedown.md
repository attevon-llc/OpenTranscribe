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

## Configuration reference

| Setting | Default | Purpose |
| --- | --- | --- |
| `ABUSE_CONTACT_EMAIL` | `""` (unset) | Published intake address surfaced in the UI/API and included in owner takedown notices as the counter-notice contact. |

The legal-hold S3 object-lock is best-effort and requires the storage bucket to
be created with object-lock enabled; the DB `legal_hold` flag is always the
source of truth.
