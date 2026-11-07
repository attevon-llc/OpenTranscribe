---
sidebar_position: 5
title: Upgrading to 0.7.0
description: What to expect when you upgrade an existing OpenTranscribe install to 0.7.0, and the changes that need your attention first.
---

# Upgrading to 0.7.0

Back up, update, and the backend brings the database up to date on its own when it starts.
This page lists the changes that can stop an existing install from working until you adjust a
setting, so that nothing surprises you. They are marked **Check this if...**.

## Before you upgrade: back up

```bash
# An installed deployment
./opentranscribe.sh backup

# A git clone of the repository
./opentr.sh backup
```

See [Backup & Restore](./backup-restore.md). Then update:

```bash
./opentranscribe.sh update                    # pull the new images and restart
./opentranscribe.sh update --version v0.7.0   # or pin this release explicitly
```

## Hardened startup checks

**Check this if...** your `.env` predates the current installer, or you wrote it by hand.

A production install refuses to start when `FLOWER_PASSWORD` is unset or a default
(`flower`, `changeme`, a `CHANGE_ME` placeholder), or when the bundled object store does not
encrypt objects (`MINIO_KMS_AUTO_ENCRYPTION` is set to anything but `on`). An install made by the current
installer already has both. Otherwise add these lines to `.env` **before** you run the update:

```bash
FLOWER_PASSWORD=<output of: openssl rand -hex 16>
MINIO_KMS_SECRET_KEY=opentranscribe-key:<output of: openssl rand -base64 32>
MINIO_KMS_AUTO_ENCRYPTION=on
```

From 0.7.0 on, `./opentranscribe.sh update` checks these settings and stops before changing
anything when one is missing. The script you upgrade *with* is your current one, which does not
have that check yet, so on this first upgrade add the lines yourself first; otherwise the backend
refuses to start and its log names the missing setting.

A missing or empty `FLOWER_PASSWORD` is now also refused by Docker Compose itself: the
production configuration no longer falls back to the development password `flower`, so every
`./opentranscribe.sh` command that loads it, `stop` included, ends with
`FLOWER_PASSWORD must be set in .env for production` until you add the line.

Back up `MINIO_KMS_SECRET_KEY` somewhere safe: objects written from then on are encrypted with
it, and if it is lost they cannot be read again. Objects stored before encryption was turned on
stay readable. To keep storing objects unencrypted on purpose, set
`MINIO_KMS_AUTO_ENCRYPTION=off` and `ALLOW_UNENCRYPTED_OBJECT_STORE=true` instead of the two
encryption lines. See [environment variables](../configuration/environment-variables.md).

Object-store encryption is now **on by default** in every install, development included:
`MINIO_KMS_AUTO_ENCRYPTION` counts as `on` when `.env` does not set it, and MinIO cannot start
without `MINIO_KMS_SECRET_KEY`. An `.env` with neither line (written by hand, or trimmed) now
needs the key, or the two opt-out lines above. From 0.7.0 on, `./opentranscribe.sh start` (and
`update` on a production install) stops with these instructions before MinIO is started. It never
invents a key for an existing install, because a key you have not backed up puts every object
written with it at risk.

## LDAP certificate verification

**Check this if...** you sign in through LDAP over `ldaps://` or StartTLS.

The directory's TLS certificate is now verified by default. A directory that presents a
private or self-signed certificate stops accepting sign-ins after the upgrade until you tell
OpenTranscribe how to trust it, in Settings, Authentication or in `.env`:

| Setting | `.env` variable | Use it when |
|---|---|---|
| `ldap_ca_cert_file` | `LDAP_CA_CERT_FILE` | The certificate comes from a private CA. Point it at the CA bundle (PEM). |
| `ldap_tls_server_name` | `LDAP_TLS_SERVER_NAME` | You connect by IP address or an alias, but the certificate lists a different name. |
| `ldap_tls_skip_verify` | `LDAP_TLS_SKIP_VERIFY` | Last resort: turns verification off and logs a warning on every connection. |

A value set in the Settings panel wins over the `.env` variable. A setting left empty in the
panel keeps the `.env` value, and saving other LDAP fields no longer clears it.

LDAP referrals are no longer followed. See [LDAP authentication](../authentication/ldap.md)
for the full setting list.

## MediaCMS sources and plain HTTP

**Check this if...** you import from a MediaCMS server that is served over plain `http://`.

A media source now records its scheme, and sign-in credentials are only ever sent to the
scheme, host and port you configured. An address without a scheme means `https`. A source that
was saved without a scheme and is really served over plain HTTP stops signing in after the
upgrade: edit it in Settings, Media Sources and enter the address as `http://host[:port]`. The
form warns while you type an `http://` address, and the saved source shows an
"Unencrypted (http)" badge.

If two older sources point at the same server, the address of either can only be changed after
one of them is deleted. Their other fields stay editable.

## Which network addresses the ports listen on

**Check this if...** another machine reaches OpenTranscribe through port 5174 (the API),
5183 (the docs) or 5173 (the web UI) instead of the HTTPS address.

In production, the API and docs ports now listen on the server itself (`127.0.0.1`). Browsers
are not affected: they reach both through the web UI's `/api` and `/docs/` paths. When the
HTTPS proxy is set up (`NGINX_SERVER_NAME`), the plain web UI port 5173 also listens on the
server only, so the HTTPS address is the one way in. Without HTTPS, port 5173 keeps listening
on every interface.

| You need, from another machine | Set in `.env` |
|---|---|
| Scripts or tools calling the API port 5174 directly | `BACKEND_BIND_HOST=0.0.0.0` |
| The docs port 5183 directly | `DOCS_BIND_HOST=0.0.0.0` |
| Plain HTTP on port 5173 while HTTPS is also set up | `FRONTEND_BIND_HOST=0.0.0.0` |

Then restart with `./opentranscribe.sh restart`. Only open what you need: these ports skip the
HTTPS proxy, and over plain HTTP sign-in passwords cross the network unencrypted.

An `.env` created from an older template carries `BACKEND_BIND_HOST=0.0.0.0`, which keeps the
API port open on the network. `./opentranscribe.sh update` lists it as a stale value; change
it to `127.0.0.1` unless you need the access above.

New installs ask "Expose directly on your LAN without HTTPS?" when HTTPS is not set up. The
answer defaults to no, which keeps the web UI on the server only; answer yes, or set
`FRONTEND_BIND_HOST=0.0.0.0` later, to use plain HTTP from other devices.

## Governance audit events are kept in the database until delivered

**Check this if...** you alert on Prometheus metrics, or you rely on the audit log as the record
of legal holds.

Placing or releasing a legal hold, editing the transcript of a held file, and changing a
collection's visibility or membership now write their audit event in the same database
transaction as the change (a new `audit_outbox` table, created automatically on upgrade). Before
0.7.0 these events were written after the change, and an audit log outage lost them. Now the
event waits in the database and is delivered when the audit log is back, by the existing
background scheduler (`celery-beat`) and the utility worker, so keep both running.

Two things you may notice:

- If the database cannot store the event, the change itself fails too, instead of succeeding
  without a record.
- Add an alert on `audit_outbox_stuck_events > 0` (see
  [Monitoring](./monitoring.md#governance-audit-outbox)). The backend has no new setting.

## Settings that moved into the admin UI

Some settings moved from `.env` to the admin UI (**Settings**), so an administrator can change
them without editing a file or restarting. Nothing you configured changes on upgrade.

### What happens to your existing `.env` value

The first time 0.7.0 starts, each variable below that is **set** in your environment is copied
into the database once, and a warning naming the variable appears in the backend log
("`<NAME>` is deprecated"). After that the stored value is the one in use and the variable is
ignored: change it in the UI, not in `.env`. A variable that is not set keeps the built-in
default. The environment read is removed completely in v0.8.0, so delete these lines from
`.env` at your convenience.

| Variable (deprecated) | Now set in | Default |
|---|---|---|
| `YOUTUBE_USER_RATE_LIMIT_ENABLED` | Settings > File status > Recovery > YouTube downloads | on |
| `YOUTUBE_USER_RATE_LIMIT_PER_HOUR` | same | 50 |
| `YOUTUBE_USER_RATE_LIMIT_PER_DAY` | same | 500 |
| `YOUTUBE_AUTO_RETRY_ENABLED` | same | on |
| `YOUTUBE_RECOVERY_BATCH_SIZE` | same | 3 |

Transcript chunk sizing (`SEARCH_CHUNK_TARGET_WORDS`, `SEARCH_CHUNK_OVERLAP_WORDS`) is an
expert setting that changes search results for the whole library; it stays an advanced
environment setting.

### Variables removed because they never had any effect

These were read, then replaced by the per-user or per-deployment value on every transcription,
so setting them did nothing. They are gone from `.env.example` and the compose files; delete
them from your `.env`.

- `VAD_THRESHOLD`, `VAD_MIN_SILENCE_MS`, `VAD_MIN_SPEECH_MS`, `VAD_SPEECH_PAD_MS`: speech
  detection is set per user under Settings > Transcription, and the deployment-wide defaults
  an administrator can change in the same panel (new in 0.7.0).
- `WHISPER_HALLUCINATION_THRESHOLD`, `WHISPER_REPETITION_PENALTY`: same panel.
- `SOURCE_LANGUAGE`: the language is chosen per user and per file.
- `MODELS_DIRECTORY`: no code read it (the model cache is `MODEL_CACHE_DIR`).
- The `FLOWER_USER` and `REDACTION_CONCURRENCY` lines inside the compose `environment:` blocks.
  Both variables still work: they are read by the compose file itself (Flower's login and the
  redaction worker's concurrency).
- The `engine.gpu_split`, `engine.precompute_vad` and `engine.shared_volume_path` database keys
  were never writable from the UI and are no longer read. `ENGINE_GPU_SPLIT` (environment) is
  what decides the split-GPU layout.

### Boolean variables now accept one set of spellings

Every on/off environment variable accepts `true`, `1`, `yes`, `on` and `false`, `0`, `no`,
`off` (any case). Before, about twenty-five of them (for example `ENABLE_OVERLAP_DETECTION`,
`USE_NATIVE_SPEAKER_EMBEDDINGS`, `PIPELINE_SCRATCH_SHARED`, `PRELOAD_GPU_MODELS`,
`ENABLE_API_DOCS`) accepted only the exact word `true` or `false`, so `1` or `no` silently did
the opposite of what was meant. An unrecognised value now logs a warning that names the variable
and uses the documented default. Two keep a deliberately strict reading: `ENGINE_GPU_SPLIT` is
on only for `true` (the start script that adds the split-GPU workers reads the same word), and
`MEDIACMS_VERIFY_SSL` turns certificate verification off only for `false`, `0` or `no`.

### Offline installs: documentation port

`docker-compose.offline.yml` now publishes the documentation on `DOCS_PORT` with default
`5183`, like every other deployment (it was `3030`). Set `DOCS_PORT=3030` to keep the old port.

### Media sources on a private network

A MediaCMS server on a private address (for example `http://10.0.0.5:8080`) is refused by
default, so a user cannot use the server to probe your network. A **super administrator** can
list the exact origins to allow under Settings > Media Sources > Private media hosts. The list
is empty after upgrade, and the cloud-metadata addresses stay blocked whatever it contains. The
old `MEDIACMS_ALLOWED_HOSTS` environment variable only chose which hosts count as MediaCMS
sources (a value without a scheme means `https`); it never permitted a private address and still
does not.

## AI token limits

**Check this if...** you want to limit how much your AI features can spend, or you see "token
limit" messages after an administrator sets one.

0.7.0 adds limits on the AI tokens used by every AI feature (chat, summaries, topics, speaker
identification and AI redaction): one per user per day and one for the whole server per month,
both in UTC. They are off after the upgrade (0 means unlimited), so nothing changes until an
administrator sets them in **Settings -> AI usage**. Models running on your own server are
counted too; tick "Do not limit models that run on this server" to exempt them.

A request that would go over a limit is refused before it is sent. Chat shows a message, and a
summary, topic or speaker-identification job is marked failed with the reason. Set the daily
limit higher than one long summary, or long recordings can never be summarized.

The same page shows each user their tokens today and over the last 30 days by feature, with an
estimated cost where the model's price is known. Models on your own server show as free; a
`vllm` or `ollama` address on another company's server now shows as "not priced" rather than
free.

## Organization erasures are scheduled, not immediate

**Check this if...** you run organizations and call `POST /api/org-admin/gdpr/erase-user/{uuid}`
or `POST /api/org-admin/gdpr/erase-organization` from a script.

Both routes used to erase at once and answer `200` with the erasure summary. In 0.7.0 they
answer `202` with a request receipt and the erasure runs 72 hours later (24 to 168, set by a
super_admin), unless an organization admin cancels it with
`POST /api/org-admin/gdpr/erasures/{ledger_uuid}/cancel`. The request now needs the admin's
password or authenticator code in the body (`{"password": "..."}` or `{"mfa_code": "..."}`) and
is refused under a support access grant. Read the result later from the erasure receipt. The
super_admin erasure routes are unchanged.

Migration `v464_generalise_erasure_grace_index` replaces the "one open request per user" index
with "one open request per subject", so a member erasure and the same person's own erasure
request no longer block each other. It needs nothing from you. Before downgrading past it,
cancel any open organization or member erasure; the downgrade refuses otherwise.

## Voiceprint purges that were interrupted

**Check this if...** you run voiceprint or gender purges (Settings -> Privacy & redaction, or Settings -> Speaker identification for your own voiceprints), or a purge you
started earlier still shows as in progress.

A voiceprint purge deletes in two passes a minute apart. Before 0.7.0, if the worker running a
purge was stopped between the two passes (a crash, an out-of-memory kill, a restart at the wrong
moment), some voice-cluster data in the search index could be left behind with nothing to retry
it. A later purge of the same scope then reported success while that data was still there.

0.7.0 records that unfinished work in the database (migration `v461_add_privacy_purge_pending`)
at the same moment the first pass commits. A scheduled check every 15 minutes finishes any purge
left more than an hour: it deletes and re-measures what was left, records an audit event for it,
and marks a purge that never reached its second pass as failed with the reason `interrupted`
instead of leaving it in progress. If you see that reason, run the purge again.

The upgrade itself needs nothing from you. A purge interrupted before the upgrade is not in the
new table, so the scheduled check cannot find it. The voiceprint orphan sweep removes those
leftovers: it deletes any voice-cluster entry in the search index whose cluster no longer exists
in the database, for personal and organization purges alike. It does not run on a schedule; run
it once after upgrading, from a backend container:
`python -m app.scripts.sweep_orphan_voiceprints` for a dry run that only counts, then
`python -m app.scripts.sweep_orphan_voiceprints --apply` to delete. If the database cannot be
read, the sweep stops without deleting anything.

## Deleting a file now moves it to the trash

**Check this if...** you delete files through the API or a script, rely on a delete freeing
storage straight away, or use `DELETE /api/files/{uuid}/force`.

Before 0.7.0 a delete destroyed the file at once: the media in object storage, its search and
chat documents, and its database row, with no way back. From 0.7.0 every delete a person makes
moves the file to the trash instead (migration `v463_add_media_file_trash` added the columns).
The file disappears from the gallery, search, chat, exports and every other view straight away,
but nothing is destroyed until its time in the trash ends: 30 days by default. Change it in
**Settings -> Data & Storage -> File Retention** (1 to 90 days). The new length applies to files
deleted from then on; a file already in the trash keeps the date it was given. Deleted files are
listed, restored and deleted forever on the new **Trash** page, linked from the gallery toolbar
(see [Trash](../user-guide/trash-and-hidden-files.md)).

What changes for API clients and scripts:

- `DELETE /api/files/{uuid}` keeps its route and its `204` answer, but now moves the file to the
  trash. Storage is freed when the file is destroyed, not when it is deleted. A file whose
  processing is still running is no longer refused with `409 FILE_NOT_SAFE_TO_DELETE`: the
  processing is cancelled and the file goes to the trash; if it is restored it comes back as
  cancelled, and reprocessing it finishes the job.
- `DELETE /api/files/{uuid}/force` (administrators) also moves the file to the trash; it no
  longer destroys anything.
- The bulk action `delete` moves each file to the trash. Its `force` field is no longer used.
- To destroy a file now, delete it and then call `DELETE /api/files/{uuid}/purge`. It answers
  `202`, and the file is destroyed within moments by a background job. The bulk actions
  `restore` and `purge` and the routes below do the same for many files.
- New: `GET /api/files/trash` (list), `POST /api/files/{uuid}/restore`,
  `DELETE /api/files/{uuid}/purge` and `POST /api/files/trash/empty`.
- An upload you cancel while it is still uploading is destroyed at once, as before.

Who may do what: the file's owner may move it to the trash, restore it and destroy it. In an
organization, the owner and the organization's administrators may move it to the trash and
restore it, but only an organization administrator may destroy it, so a member's mistake stays
recoverable. A shared file cannot be deleted, restored or destroyed by the people it is shared
with.

Legal holds and takedowns come first. A file under a legal or organization hold cannot be moved
to the trash. A file placed under a hold while it is in the trash stays there, can still be
restored, and is never destroyed while the hold lasts; once the hold is released it is destroyed
on the next hourly check if its time is up. A file under takedown review is never destroyed from
the trash either.

What does not change:

- Retention (**Settings -> Data & Storage -> File Retention**) still deletes expired files
  permanently at the end of the retention period, without passing through the trash. It skips
  files that are already in the trash, which are destroyed when their own time ends.
- A GDPR erasure or an account deletion destroys the person's files at once, including any in
  the trash. It does not wait for the trash period.

Trashed files keep their storage until they are destroyed. The administrator dashboard's file
statistics now show how many files are in the trash and how much space they use
(`trashed_files`, `trashed_bytes`).

## A legal hold now freezes the file's content

**Check this if...** you edit files through the API or a script, or anyone edits recordings that
an administrator has placed under a legal hold (an organization hold, or the hold that comes with
a takedown).

Before 0.7.0 a legal hold blocked deleting and reprocessing a file, but its transcript, speakers,
title, tags and the rest could still be edited; a transcript edit was recorded as
`content.transcript.edit`. This deployment keeps no earlier versions of a transcript, so such an
edit overwrote the only copy of what was held. From 0.7.0 a hold preserves the file exactly as it
was: every change to a held file's content is refused with `409` and
`detail.error = "FILE_UNDER_LEGAL_HOLD"` (the same answer a delete of a held file already gave),
and each refusal is recorded as `content.hold.refused` with the action that was tried.

Routes that used to succeed on a held file and now answer `409 FILE_UNDER_LEGAL_HOLD`:

- Transcript: `PUT /api/files/{uuid}/transcript/segments/{segment_uuid}`,
  `PUT /api/transcripts/segments/{segment_uuid}/speaker`.
- Speakers: `POST /api/speakers`, `PUT /api/speakers/{uuid}`,
  `POST /api/speakers/{uuid}/merge/{target_uuid}` (either speaker's file held),
  `POST /api/speakers/{uuid}/verify`, `POST /api/speakers/{uuid}/confirm-gender`,
  `POST /api/speaker-profiles/speakers/{uuid}/assign-profile`, and
  `DELETE /api/speaker-profiles/profiles/{uuid}` when a speaker linked to the profile is on a
  held file.
- File details: `PUT /api/files/{uuid}` (title, filename, recorded date, language and every other
  field).
- AI features: `POST /api/files/{uuid}/summarize`, `POST /api/files/{uuid}/retry-summary`,
  `POST /api/files/{uuid}/identify-speakers`, `POST /api/files/{uuid}/extract`,
  `POST /api/files/{uuid}/apply`, `DELETE /api/files/{uuid}/suggestions`,
  `POST /api/files/{uuid}/auto-label`. `POST /api/files/batch-extract` skips held files (its
  answer gains `held_skipped`) and answers 409 only when every file asked for is held.
- Tags: `POST /api/tags/files/{uuid}/tags`, `DELETE /api/tags/files/{uuid}/tags/{name}`, and
  `POST /api/tags/{uuid}/merge`, `DELETE /api/tags`, `POST /api/tags/promote` and a rename with
  `confirm_merge` (`PATCH /api/tags/{uuid}`) when a tag they would remove or fold is on a held
  file. A plain tag rename still works.
- Collections: `POST /api/collections/{uuid}/media` and `DELETE /api/collections/{uuid}/media`
  for a held file (before, only a hold on the collection itself refused the removal), and
  `DELETE /api/collections/{uuid}` when the collection contains a held file.
- Comments: `PUT /api/comments/{uuid}` and `DELETE /api/comments/{uuid}` on a held file.
- Recovery: `POST /api/files/{uuid}/recover`, `POST /api/tasks/retry/{uuid}` and
  `POST /api/my-files/{uuid}/retry` now answer 409 up front instead of failing later.
- The bulk action (`POST /api/files/management/bulk-action`) answers `200` as before; a held file's
  row now carries `error: "FILE_UNDER_LEGAL_HOLD"` for `summarize`, `identify_speakers`, `redact`,
  `add_tag`, `remove_tag` and `recover`, as it already did for `retry` and `reprocess`.

Operations that span many files leave a held file's speaker as it was instead of refusing: renaming
a speaker profile (`PUT /api/speaker-profiles/profiles/{uuid}`, whose answer gains
`held_skipped`), confirming a profile's gender, and the speaker-cluster batch verify (answer gains
`held_skipped`), unassign, promote, merge and split.

Background work skips a held file once its first processing run has finished: an automatic or
queued summary, topic extraction and auto-labelling, LLM speaker identification, speaker gender
detection, voice matching from another file, a redaction rescan and the admin redaction reindex.
A file that is held while it is still being processed finishes that run. Its first redaction scan
still runs, so a held file stays readable where masking is required.

Still allowed on a held file: reading, searching, playing, exporting and downloading it, rebuilding
its search index and other derived copies, and adding a new comment, which is recorded as the new
`content.hold.annotation` event. `content.transcript.edit` is no longer written; earlier rows stay
in the audit log and keep their label.

An account cannot be deleted (`DELETE /api/admin/users/{uuid}`, `DELETE /api/users/{uuid}`, a SCIM
delete) while it has commented on someone else's held file: the answer is the existing
`409 FILE_UNDER_LEGAL_HOLD`, with a new `commented_files_under_legal_hold` count. A GDPR erasure of
such an account keeps those comments and the account row, and finishes once the hold is released,
as it already did for the account's own held files.

**What to do:** a script that changes a held file must have an administrator release the hold
first. Handle `409` with `FILE_UNDER_LEGAL_HOLD` as "this file is preserved", not as a transient
error to retry.
