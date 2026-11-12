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

## Migrations run in their own step

**Check this if...** you run OpenTranscribe from your own compose files, or on Kubernetes or ECS.

The compose files now contain a one-shot `migrate` service. It applies the migrations and
exits, and only then do `backend` and the Celery workers start. The backend no longer migrates
itself in the shipped production, lite and offline files (`RUN_MIGRATIONS_ON_STARTUP=false`
there). Nothing changes for you if you use `./opentranscribe.sh update`: it installs the new
compose files and the migration runs first, as before. A failed migration is now visible in
`./opentranscribe.sh logs migrate`, and the backend is not started.

- **Own compose files without a `migrate` service:** they keep working. The backend defaults to
  `RUN_MIGRATIONS_ON_STARTUP=true` and migrates when it starts.
- **Orchestrated deploys:** run `python -m app.startup.migrate` as a Job before the rollout and
  start the API with `RUN_MIGRATIONS_ON_STARTUP=false`. If your Job ran `alembic upgrade head`,
  switch it: the new command also does the post-upgrade work `alembic` alone skips (deferred
  index builds, database role grants) and seeds the initial data.
- **A migration lock that is never released** used to make every backend wait forever. A runner
  now gives up after `MIGRATION_LOCK_TIMEOUT_S` (default 900 s), names the session that holds
  the lock, and exits with code `3`.

See [How migrations run](./upgrading.md#how-migrations-run).

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

## Speaker list API is paged

`GET /api/speakers` without `file_uuid` (an account's whole speaker list) now returns at most
1,000 speakers per call, ordered by speaker number. Pass `skip` and `limit` (up to 5,000) to read
further pages. Before 0.7.0 it returned every speaker in one response, which took minutes on
large libraries. Calls with `file_uuid`, and the `for_filter` picker, are unchanged.

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

Administrators also see a **Spend per user** table on that page: each person's tokens today
and this month (UTC) and an estimated cost, heaviest first. A `*` marks a cost that leaves out
models with no known price. It shows counts and money only, never conversation content. The
same data is at `GET /api/admin/llm-budget/users` (administrators only).

## Celery worker database pools are sized per process

**Check this if...** your `.env` sets `WORKER_DB_MAX_OVERFLOW`, or you raised a worker's
concurrency (for example `CLOUD_ASR_CONCURRENCY`) or `PG_MAX_CONNECTIONS`.

A database pool belongs to one process, not one container. A prefork Celery worker (download,
cpu, cloud-asr, nlp, embedding) is a main process plus one child per `--concurrency`, and each
builds its own pool, so with the old defaults the services could open up to 285 connections
against PostgreSQL's 200. The last process to connect, the API included, then failed with
"sorry, too many clients already".

In 0.7.0:

- `WORKER_DB_MAX_OVERFLOW` now defaults to `0` (was `3`). It applies to every process of the
  prefork workers and to beat; each keeps `WORKER_DB_POOL_SIZE` (2) connections.
- The new `THREADED_WORKER_DB_MAX_OVERFLOW` (default `3`) applies to the threads-pool workers
  (the GPU workers and redaction), whose task threads share one process's pool.

The worst case with the defaults is now about 165 of the 197 connections PostgreSQL allows
ordinary roles. If your `.env` was copied from an older `.env.example`, it still says
`WORKER_DB_MAX_OVERFLOW=3`, which overrides the new default: change it to `0`, and add
`THREADED_WORKER_DB_MAX_OVERFLOW=3`. Each extra prefork child costs two connections, so raise
`PG_MAX_CONNECTIONS` with any concurrency you raise. See
[Performance Tuning](./performance-tuning.md#connection-budget).

## Transcriptions are shared fairly between users

**Check this if...** one user uploads many files at once, or you rely on strictly
first-uploaded, first-transcribed order.

Before 0.7.0 every transcription was queued at the same priority, so a user who uploaded a
folder of 500 files put every other user's file behind all 500. Now each user's first 5 queued or
running transcriptions keep the normal priority and later ones are queued one step lower, so
another user's file starts as soon as those first 5 are through. A user's own files still run in
upload order, and automatic retries always go first.

The limit is set in **Settings -> File status -> Recovery -> Fair sharing between users**
(default 5). Set it to 0 to keep the old strict first-come order. Runs already queued when you
upgrade keep their place. See
[Performance Tuning](./performance-tuning.md#fair-sharing-between-users).

## Multi-track files: choose the audio track

**Check this if...** you transcribe videos that carry more than one audio track (dubs,
commentary).

Revision `v469_add_media_file_audio_streams` adds two nullable columns to `media_file`:
`audio_streams` (what ffprobe found) and `audio_stream_index` (the track a user chose). It
rewrites nothing and needs no action. Existing files have no track list until they are next
processed; they keep transcribing the first audio track, which is what they always did.

One behaviour change: speaker voiceprints and gender estimates on a multi-track file are now
computed from the same track that was transcribed (the first, unless chosen). Before, they could
be taken from a different track than the transcript. Nothing is recomputed automatically; re-run
**Re-diarize Only** on an affected file if its speakers matter. See
[Files with more than one audio track](../user-guide/uploading-files.md#files-with-more-than-one-audio-track).

## Forcing the transcription language on reprocess

**Check this if...** you want to know what changed in the database for the forced-language option.

Revision `v470_add_requested_source_language` adds one nullable `media_file.requested_source_language`
column (an ISO code or `auto`) with a `CHECK` on its shape. It rewrites nothing and needs no
action: NULL means "use the user's saved language", which is what every existing file did. The
column is part of the file's stored request, so retries and stuck-file recovery replay a forced
language. Downgrading drops the column and discards stored choices. See
[Forcing the transcription language](../user-guide/uploading-files.md#forcing-the-transcription-language).

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


### Removing a member now waits before erasing their personal files

`DELETE /api/users/{uuid}` and `DELETE /api/admin/users/{uuid}` still hand an organization's
content to an administrator first. For an account that belongs to an organization they now switch
the account off and schedule the erase of its personal files `gdpr.removed_member_grace_days` later
(default 14, 0 to 21) instead of erasing at once; the answer is still `204` (admin route: `200` with
`scheduled_for`). Restore the account in that window under Settings, Privacy & Redaction, Removed
members. Set the setting to 0 to keep the old immediate erase. Accounts in no organization, which is
every community-edition account, are unchanged. No migration.

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

To run that check now instead of waiting for the next tick, a super admin can call
`POST /api/admin/privacy-policy/purge-reverify`. The optional body `{"older_than_seconds": 3600}`
sets how old a purge's record must be before it is touched (at least 300; the default is the
scheduled check's hour), so a purge that is still running is not marked interrupted. It answers
202 with a task id, and each purge it handles is audited under its own purge id. Repeating it is
safe. Only one check runs at a time: a run that starts while another is still going (the
scheduled one and yours, say) does nothing and returns `skipped: 1`, so no purge is handled,
audited or marked interrupted twice. The lock is held in Redis; while Redis is unreachable the
check does not run and the unfinished work waits for the next tick.

The upgrade itself needs nothing from you. A purge interrupted before the upgrade is not in the
new table, so the scheduled check cannot find it. The voiceprint orphan sweep removes those
leftovers: it deletes any voice-cluster entry in the search index whose cluster no longer exists
in the database, for personal and organization purges alike. It does not run on a schedule; run
it once after upgrading, from a backend container:
`python -m app.scripts.sweep_orphan_voiceprints` for a dry run that only counts, then
`python -m app.scripts.sweep_orphan_voiceprints --apply` to delete. If the database cannot be
read, the sweep stops without deleting anything. It never deletes a voiceprint written while it runs:
before deleting it reads the database again and skips any entry that has gained a row, or that
was written after the sweep started (reported as `skipped_recent`).

## Re-check the speaker names the 0.6.0 upgrade marked as voiceprint-made

The 0.6.0 upgrade (migration `v449`) marked an existing speaker name as made by a voiceprint
only when the database held positive evidence (see
[Which existing speaker names count as voiceprint-made](upgrading-to-0-6-0.md#which-existing-speaker-names-count-as-voiceprint-made)).
One kind of name a person chose can still carry that mark, and a voiceprint purge clears a
marked name for good. 0.7.0 adds a command that re-checks every mark the upgrade left, against
the data as it is now. Run it from a backend container before you purge voiceprints on an install
that predates 0.6.0:

```bash
# Dry run (the default): report only
python -m app.scripts.reverify_identity_sources
# Only one organization, or one user
python -m app.scripts.reverify_identity_sources --organization-id 3
python -m app.scripts.reverify_identity_sources --user-id 42
# Return the marks whose evidence no longer holds to "unknown" (read as a person's)
python -m app.scripts.reverify_identity_sources --apply
# Return EVERY mark the upgrade made to "unknown"
python -m app.scripts.reverify_identity_sources --apply --include-evidenced
```

It prints a JSON report. In `counts`:

| Field | Meaning |
|---|---|
| `backfill_stamped` | Names that still carry the upgrade's mark. Any later rename, confirmation or match replaced the mark, so these are the names nobody has touched since |
| `evidence_holds` / `evidence_lost` | Of those, the ones whose evidence still holds today, and the ones it no longer holds for (for example, the earlier recording the match relied on was deleted). The speaker ids of each are listed |
| `shape_left_unmarked` | Unmarked names that look like an automatic match but were never proven (the upgrade left them as a person's). Reported, never marked |
| `shape_left_unmarked_with_evidence` | Of those, the ones that would pass the evidence test today. Also reported only: marking on less than proof is how a person's name gets purged |
| `summaries_to_review` | Recordings with a stored summary and a speaker name a voiceprint wrote. Their file ids are listed in `summary_file_uuids`; see below |
| `cleared` | Names returned to unknown by this run (0 for a dry run) |
| `skipped_protected` | Names on a recording under a legal hold or in quarantine. They are never changed (a hold freezes the recording as it stands); a dry run counts the ones an apply would skip |

`recorded` repeats what the upgrade itself recorded in `privacy.identity_source_backfill`, which
the command never changes.

`--apply` changes only the mark: the name, the profile link and the verified tick stay as they
are. A name returned to unknown is shown while voiceprints are off and is never cleared by a
purge, the same as any name a person typed. Each one gets an entry in the speaker's identity
history (origin `identity_reverify`), and its recording is queued for a search reindex so the name
becomes searchable again. Use `--include-evidenced` if you would rather keep every name from
before 0.6.0 than let a purge clear one that might have been a person's.

Every run, dry or not, writes a `privacy.identity_source.reverify` audit event with the scope, the
counts and the ids of the speakers it changed (never a name). An organization-scoped run is
stamped with the organization, so its administrators see it too. Exit status 3 means the names
were changed but some recordings could not be queued for a reindex (listed in
`reindex_failed_file_uuids`); while voiceprints are off their search results still hide the name
until those recordings are re-indexed (a library reindex from the admin search settings does it).

**Summaries.** A summary a language model wrote is stored as written, so it can contain a name a
voiceprint supplied. Switching voiceprints off masks that name in search and chat, not in the
stored text, and nothing rewrites it. `summary_file_uuids` lists the recordings where this can
apply. To remove the name from a stored summary, turn voiceprints off and regenerate that
recording's summary.

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

For what a hold means for users and administrators, see [Legal holds](../user-guide/legal-holds.md).

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

## Outages now answer with an error, not an empty result

**Check this if...** a script or monitor reads status codes from the API.

0.7.0 stops reading a dependency outage as "nothing there". Each case below used to succeed or
come back empty; it now fails visibly and can be retried.

| Situation | Answer |
|---|---|
| The database has no free connection, a statement runs too long, or a request exceeds its deadline | `503` with `Retry-After` and `{"code": "server_busy"}`; at sign-in too, never `401` |
| A stored secret cannot be decrypted (`ENCRYPTION_KEY` was changed or is wrong, or the stored value is damaged) | `409` with code `stored_value_unreadable`. Re-enter the credential in its settings page. Nothing connects without credentials or overwrites the stored value |
| Revealing redacted content (transcript view, subtitles, transcript and document export) when its `transcript.view_unredacted` audit record cannot be written | `503`; the content is not shown |
| `POST /api/files/complete` when the uploaded file's header cannot be read for the content check | `503` with `Retry-After`; the upload row is kept, so retry the call. The file is not accepted unchecked |
| `GET /api/admin/timing/{task_id}` while Redis is down | `503` (was `404` "no timing data") |
| ASR local-model restart while the task broker is unreachable | `503` `worker_broker_unavailable` (was `409`) |
| `POST /embedding-migration/start` when the migration lock cannot be read | `503`; an unreadable lock counts as held, so a second migration never starts |
| An OpenSearch outage reached a speaker, voiceprint, cluster or index lookup | `503` with a fixed message (it never quotes the cluster's error text); was `500` |
| A sign-in through OpenID Connect whose ID token has no `sub` claim | Refused as an invalid token (the claim is now required); was an internal error |
| A Microsoft 365 email source whose token request is refused | Reported as a failed send or connection test; was an unhandled error |

Two response bodies gained fields: the three migration `stop` endpoints add
`"warnings": ["batch_task_revocation_failed"]` to a `stopped` answer when in-flight batch tasks
could not be revoked, and the embedding-migration status adds `stalled_check_failed: true` when
the stalled check could not read the database or OpenSearch. The web app retries a read answered
`503` with `Retry-After` once and shows "The server is busy" for writes; it never signs the user
out for a `503`. It also replays a read once when the request got no response at all (the
browser reports a network error when the host's network interfaces change while a request is
queued); writes are never replayed, so an upload or an edit is never sent twice.

`GET /api/admin/engine-settings/metrics` now answers `{"metrics_collected": false, "workers": {}}`
with an explanation instead of an unexplained `{}`; live worker metrics are the Prometheus
endpoints (see [Monitoring](./monitoring.md)).

## `opentr.sh` is stricter

Development and operator scripting only. `./opentr.sh` now exits with the status of the command
it ran (it used to end in `exit 0`, so `healthcheck-all` always looked healthy). An unknown flag
to `start` or `reset` is an error rather than being ignored (a mistyped `--fresh` used to run
against the live stack), `rebuild-frontend` and `rebuild-backend` refuse options they do not
implement, and a declined `reset`, `remove` or `purge` confirmation exits `1`. `start` and `reset`
warn when a GPU device id you exported in your shell (for example `GPU_SCALE_DEVICE_ID=2`) is
replaced by `.env` or by a flag, and name `--gpu-device` and `--diar-native-gpu`; before, the
workers started silently on the card `.env` named. `--dry-run` writes
nothing, including with `--with-pki`, and does not build or pull images.

## API capacity and overload limits

**Check this if...** your host has less than 8 GB of memory, or you saw 503 answers under load.

The API now runs `UVICORN_WORKERS` processes (default 2; each loads its own models, roughly
1 to 1.8 GB). Set `UVICORN_WORKERS=1` on a small host. An overloaded API answers 503 rather than
stalling: it waits at most 5 s for a database connection (`DB_POOL_TIMEOUT_SECONDS`), bounds each
statement to 30 s (`DB_STATEMENT_TIMEOUT_MS`), gives up on a request after 55 s of work
(`REQUEST_DEADLINE_SECONDS`, 0 turns it off), and accepts at most `UVICORN_LIMIT_CONCURRENCY`
open connections per process (512). Chat has a deployment-wide cap on replies streaming at once
(`CHAT_MAX_CONCURRENT_STREAMS_GLOBAL`, 64). See [Scaling and Sizing](./scaling-and-sizing.md).

## Optional: anonymous usage report, demo mode

Both are off after the upgrade and need nothing from you. The images ship `DO_NOT_TRACK=1`; see
[Telemetry](./telemetry.md) to opt in. `DEMO_MODE=true` turns an install into a public read-only
demo (writes are refused with `403 demo_read_only`); see [Public demo](./public-demo.md). Do not set
it on an install people use.

## Stored secrets that cannot be decrypted

If `ENCRYPTION_KEY` was rotated or a stored secret is corrupt, reading it now fails with
`SecretDecryptionError` instead of acting as if no secret was set. Provider screens list the
entry without its credential; re-enter the key. Back up `ENCRYPTION_KEY` with the rest of `.env`.

## Smaller changes

- The video and audio player is Video.js 10 (Plyr is gone); the frontend builds on SvelteKit 3.
  Custom CSS that targeted `.plyr` selectors no longer applies.
- `./opentr.sh backup` and `restore` accept `--fresh <name>` and then touch only that
  deployment; `remove`, `purge`, `health` and `build` refuse arguments they ignore.
- Non-English interface languages are labelled machine-translated in Settings > Language.
- **Managed editions:** deleting an account now hands the person's organization content to an
  administrator first. Code that creates organizations must call
  `services.organization_owner.set_owner`, or organization erasure answers 409
  `org_owner_required`.

## Extensions that import OpenTranscribe modules

**Check this if...** you run your own Python package on top of the backend image (extra routers,
a capability resolver, custom sign-in) and it imports from `app.*`.

`CLOUD_SEAM_VERSION` (`backend/app/auth/constants.py`) is now `14` (it was `13`). Several large
modules were split by concern and no compatibility aliases were kept, so an extension that
imports a moved name fails when it is imported, not when a request arrives.

| Old import | New import |
|---|---|
| `app.core.constants.CeleryQueues` | `app.core.queues.CeleryQueues` |
| `app.services.task_detection_service.RECOVERED_AFTER_STUCK` | `app.core.task_config.RECOVERED_AFTER_STUCK` |
| `app.core.constants.DEFAULT_CHAT_*` | `app.core.chat_defaults.DEFAULT_CHAT_*` |
| `app.auth.audit.query_audit_logs` | `app.auth.audit_query.query_audit_logs` |
| `app.core.capabilities.require_capability` | `app.api.capability_dependency.require_capability` |
| `app.core.security.create_access_token` | `app.auth.direct_auth.create_access_token` (signature changed) |

The second access-token issuer was removed. Call
`app.auth.direct_auth.create_access_token(data, expires_delta)` and put your own claims in
`data` beside `sub`. The issuer always sets `exp`, `iat`, `jti` and `type` itself and ignores
your values for them, and tokens no longer carry an `alg_version` claim. Code that read the
client address from `app.api.endpoints.auth.lifecycle_gates._get_client_info` should call
`app.utils.client_ip.resolve_client_ip(request)` instead.

After updating the imports, run `python -c "import <your_extension>"` in the new backend image
before you deploy. The names an extension may rely on are listed in
`backend/tests/fixtures/extension_surface.txt`; a test fails when one of them moves.
