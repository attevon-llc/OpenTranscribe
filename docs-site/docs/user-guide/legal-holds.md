---
sidebar_position: 14
---

# Legal holds

A **legal hold** tells OpenTranscribe to keep a recording exactly as it is, because it may be
needed as evidence. While a hold is in force the recording cannot be deleted, and its content
cannot be changed. Reading, searching, playing, exporting and downloading it still work.

This page is written for two readers: people who use a held recording, and the administrators who
place and release holds. It starts with plain language and then gives the exact behaviour. It is
product documentation, not legal advice. Ask your counsel what your own preservation duty requires.

## Why a hold exists

When a party reasonably expects litigation, an investigation or a regulatory request, it generally
has a duty to preserve the information that may be relevant. In United States federal courts,
[Federal Rule of Civil Procedure 37(e)](https://www.law.cornell.edu/rules/frcp/rule_37) lets a
court impose remedies when electronically stored information that should have been preserved is
lost because a party did not take reasonable steps. The Sedona Conference publishes widely used
guidance on [legal holds](https://thesedonaconference.org/publication/Commentary_on_Legal_Holds)
(when the duty starts, and how to document the hold and its release). A transcript, its speaker
names and its edit state are all electronically stored information.

OpenTranscribe does not keep earlier versions of a transcript. If a held recording could still be
edited, the edit would overwrite the only copy of what was held, and the audit log could say that
an edit happened but could not bring the old text back. So a hold **freezes** the recording
instead of recording edits. Under European data protection law, the right to erasure does not apply
to the extent the data is needed for the establishment, exercise or defence of legal claims
(GDPR Article 17(3)(e)), which is why an erasure request waits while a hold is in force.

## For everyone: what a hold means for a recording

A held recording shows a **Legal hold** badge. If you try to change it, you are told that it is
under a legal hold and who placed it (the service operator, your organization, or both). You are
never shown the reason: only administrators can read it.

**You can still:**

- open, read, search and play the recording;
- export it in every format, and download it, including the transcript document, subtitles and
  bulk export;
- add a **new comment**. Comments on a held recording are allowed and are recorded in the audit log
  as annotations. Existing comments cannot be edited or deleted.

**You cannot:**

- edit the transcript text or its timings, or change who spoke a segment;
- create, rename, delete, merge, verify or link the recording's speakers to a speaker profile;
- change the title, filename, description, recorded date, language or any other detail;
- generate, regenerate, retry or delete the summary, extract, apply or dismiss topics, or run AI
  speaker identification or auto-labelling;
- add or remove tags on it, add it to a collection or remove it from one, or delete a collection
  that contains it;
- rescan it for redaction (the first scan of a recording that was never scanned still happens, so a
  held recording stays readable where masking is required);
- reprocess, retry or recover it, or change its voice identification consent;
- delete it, move it to the trash, or have it removed by retention, by an account deletion or by an
  erasure request.

When a change is refused, the application answers (HTTP 409, error code `FILE_UNDER_LEGAL_HOLD`):

> refused: the file is under an active legal hold, so its content is preserved exactly as it was.
> You can still read, search, play, export and download it, and add a comment. An administrator
> must release the hold before it can be changed.

Delete and reprocess refusals use their own sentence, for example "refused: the file is under an
active legal hold and cannot be deleted. Release the hold before deleting." The code is the same.

Things that only change a label or an access setting, not the recording, still work: renaming a
tag or a collection, sharing a collection, and rebuilding the search index or the waveform, which
are copies of the held record. A hold placed while a recording is still being processed lets that
first run finish; after it, background work (a new automatic summary, topic extraction, voice
matching, auto-labelling) skips the recording.

### Finding held recordings

Held recordings are collected automatically under a **Legal hold** tag and a **Legal hold**
collection. They are not real tags or collections: the application works them out from the holds
that are in force, so they cannot be edited, deleted or shared, and they disappear when the last
hold on a recording is released. You only ever see your own accessible recordings in them, never
another organization's.

- In the gallery, the **Legal hold** filter chip (in the filter panel, among the tags) shows only
  held recordings. It appears only when you have at least one.
- In the collections list, the **Legal hold** entry opens the gallery filtered the same way.
- This is a gallery feature. The search page does not filter by hold.

Through the API, `GET /api/files?held=true` lists held files (`held=false` lists the rest), and
`GET /api/files/metadata-filters` returns a `held_count`. Each listed file carries a `hold` object
saying whether it is held and by whom (`platform`, `organization` or both), never why.

### A recording's ID

You never need a recording's ID to hold or quarantine it: the administrator screens find files by
title or file name (see [Placing a hold in the application](#placing-a-hold-in-the-application)).
When support, a takedown dispute or the audit log asks for a file's ID, open the file, expand
**File details** and use the copy button beside **File ID**. It is also the last part of the file
page's address (the long identifier after `/files/`).

## Hold, quarantine, trash and deletion compared

| | Legal hold | Quarantine (takedown) | Trash | Delete forever / erasure / retention |
|---|---|---|---|---|
| What it is for | Preserve evidence | Remove content from view (abuse, a legal notice) | Recover from a mistaken delete | Destroy the data |
| Who sets it | Administrators only: the service operator, or an organization administrator for the organization's files | The operator, or an organization administrator for its own files | The owner deletes a file; an organization administrator manages the organization's trash | The owner, an administrator, a retention rule or an erasure request |
| The owner can still see and use the file | Yes (read-only) | No, it is hidden everywhere | Not in the gallery; restorable from the Trash page | No |
| Can the content be edited | No, it is frozen | Not reachable, the file is hidden | Not while in the trash | Not applicable |
| Blocks delete, retention and erasure | Yes | Blocks retention only | Trash is the first step of a delete | Refused or deferred while a hold is in force |
| Blocks reprocess and retry | Yes | No | No | Not applicable |
| How it ends | An administrator releases it, with a reason, and the release is recorded | Whoever holds the takedown releases it | Restore, or the retention period ends | It does not end; it is permanent |
| Reversible | Yes, by release | Yes, by release | Yes, until the trash period ends | No |

Precedence when they meet: **a hold wins over the trash purge, which wins over retention.**

- A held file cannot be moved to the trash. A file already in the trash when a hold is placed stays
  there, stays restorable, and is never deleted forever while held. When the last hold is released
  and its time has ended, it is deleted forever within the hour. See
  [Trash and hidden files](./trash-and-hidden-files.md).
- Account deletion and GDPR erasure are deferred, not refused forever: the request is recorded as
  waiting, the recordings are kept, and it finishes when the hold is released. See
  [Erase my account](./erase-my-account.md) and [GDPR erasure](../operations/gdpr-erasure.md).
  An account that has commented on someone else's held recording cannot be deleted until that hold
  ends, because the comment is frozen too.
- Retention (see [File retention](./admin-panel.md#file-retention--auto-deletion)) never deletes a
  held file.
- A quarantine can be placed on a held file. The two are independent: releasing one does not
  release the other.

## For administrators

Only administrators can place or release a hold. Regular users never can and never see a hold's
reason. Who can act depends on the edition:

| Edition | Who places and releases | Where |
|---|---|---|
| Self-hosted (community) | Platform administrators (`admin` or `super_admin`), the **operator hold** | Settings > Legal holds, and `/api/admin/holds` |
| Deployments with organizations | Organization administrators, for their own organization's files and collections | Settings > Legal holds, and `/api/org-admin/holds` |

A hold needs a **reason** (10 to 2,000 characters) and may have a **matter reference** (a case or
matter number, up to 255 characters). A hold names up to 500 files at once, or one collection. A
hold on a collection covers every file in it now **and every file added later**.

### Placing a hold in the application

Where each of these appears is described from the design of the hold interface (see the note at the
end of this section).

- **From the gallery:** select one or more files and choose **Place legal hold** from the selection
  actions. A dialog shows how many files are affected, asks for the reason and an optional matter
  reference, and explains what a hold does. It is offered to administrators only.
- **From Settings:** in **Settings > Legal holds**, choose **Place a hold**. **Specific files** is
  selected: type part of a title or file name, pick the file from the list, and repeat for each
  file (up to 500). The picked files show as chips; remove one with its **x**, or use **Clear
  selection**. Each result shows its owner and upload date (and, for the service operator, its
  organization), and says when the file is in the trash, quarantined or already held. An
  organization administrator searches every file of the organization, whoever owns it; the
  service operator searches every file of every organization. Files in the trash and quarantined
  files are included, because a hold still applies to them.
- **By collection:** in the same dialog choose **A whole collection** and pick the collection.

The file page has no hold button: its header shows the recording, and holds are placed from the
gallery or from Settings.

A held file shows the **Legal hold** badge in the gallery and the collection list. On the file page
a banner below the title says the file is held and by whom, with an expandable **What does a legal hold do?**
explanation, and the controls that would change the file are disabled with a tooltip. The comment
box stays enabled.

### Releasing a hold

Release is done in **Settings > Legal holds** only. Choose **Release** on the hold and give a reason
for releasing; the record is kept (**Show released holds**). A file that another hold, or a
takedown's hold, still covers stays held. Releasing the last hold on a file makes it editable and
deletable again, retries any account deletion or erasure request that was waiting on it, and lets an
overdue file in the trash be deleted forever within the hour.

### Placing and releasing a hold through the API

| Edition | Routes |
|---|---|
| Self-hosted operator hold | `GET /api/admin/holds`, `GET /api/admin/holds/{uuid}`, `POST /api/admin/holds`, `POST /api/admin/holds/{uuid}/release`, `GET /api/admin/holds/files/search` |
| Organization hold | `GET /api/org-admin/holds`, `GET /api/org-admin/holds/{uuid}`, `POST /api/org-admin/holds`, `POST /api/org-admin/holds/{uuid}/release`, `GET /api/org-admin/holds/files/search` |

`GET .../holds/files/search?q=<text>&limit=<1-50>&offset=<n>` finds files whose title or file name
contains `q`, newest first, with the owner, the upload time, whether the file is `available`,
`trashed` or `quarantined`, and who already holds it (never why). The organization route covers
every file of the caller's organization; the operator route covers every tenant, adds each file's
organization and is audited as a cross-tenant metadata read. Both answer 403 to anyone who is not
that kind of administrator and are rate-limited like search.

Place a hold on files:

```json
POST /api/admin/holds
{
  "scope": "file",
  "file_uuids": ["<file id>", "<file id>"],
  "reason": "Preservation notice received 2026-11-07",
  "matter_ref": "Matter 2026-014"
}
```

Place a hold on a collection by sending `"scope": "collection"` with `"collection_uuid"` and no
`file_uuids`. Release with `POST /api/admin/holds/{uuid}/release` and `{"reason": "..."}`. A hold
that is already released answers 409; a reason outside 10 to 2,000 characters, no files, or more than
500 files answers 422; an unknown file or collection answers 404. Placing and releasing share a
rate limit (`ADMIN_HOLD_RATE_LIMIT`, 30 per hour per user by default). Each hold record says
whether it was placed by the `platform` (operator) or an `organization`. Rejections of changes to a
held file are described in [Upgrading to 0.7.0](../operations/upgrading-to-0-7-0.md#a-legal-hold-now-freezes-the-files-content).

### Operator holds in a self-hosted install

On a self-hosted install there are no organizations, so the operator is the only authority. An
operator hold:

- can be placed by any administrator on any file or collection, **without quarantining it**: the
  owner keeps seeing and reading the file;
- is stored as a hold record like any other (with a reason, matter reference, who placed and
  released it and when), so it has history, which the older takedown flag did not;
- is the right tool when you receive a preservation request. Use a quarantine when the content must
  also be hidden.

The takedown has its own hold: when you quarantine a file you can tick **Place a legal hold** in the
quarantine dialog (the default). That hold is released from the quarantine screen
(**Lift legal hold**), not from Settings > Legal holds, and it is unchanged by operator holds. Both
count as "the service operator" to users. The comparison of all the hold kinds is in
[Abuse and takedown](https://github.com/attevon-llc/OpenTranscribe/blob/master/docs/abuse-and-takedown.md).

The reason and matter reference are returned to administrators who manage the hold and nowhere
else: not in a file response, an audit event, a notification or a refusal.

### What the audit log records

| Event | When |
|---|---|
| `content.hold.place` | A hold is placed. Recorded in the same transaction as the hold, whatever the content-auditing switch says. Carries ids and counts, never the reason. |
| `content.hold.release` | A hold is released, the same way. |
| `content.hold.refused` | A change, delete or reprocess of a held file was refused. Carries the action that was tried (for example `segment_edit`, `file_update`, `tag_add`) and who holds it. Best-effort: written outside the refused request. |
| `content.hold.annotation` | A comment was added to a held file. Recorded in the comment's own transaction. |

`content.transcript.edit` was recorded for edits to held files before 0.7.0 and is no longer
written; earlier rows stay and keep their label. To see everything that happened to one file, filter
the audit log by its file ID (see [Audit Log Viewer](./admin-panel.md#audit-log-viewer)).

## Related

- [Admin panel: organization legal holds](./admin-panel.md#organization-legal-holds)
- [Trash and hidden files](./trash-and-hidden-files.md)
- [Collections](./collections.md) and [Tags](./tags.md)
- [Upgrading to 0.7.0](../operations/upgrading-to-0-7-0.md)
