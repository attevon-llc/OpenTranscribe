---
sidebar_position: 4
title: Upgrading to 0.6.0
description: What to expect when you upgrade an existing OpenTranscribe install to 0.6.0, and the few things that behave differently afterwards.
---

# Upgrading to 0.6.0

Upgrading to 0.6.0 is a standard upgrade. There is no manual migration step, no required `.env`
change and no reindex to run. Back up, update, and the backend brings the database up to date
on its own when it starts. (The one exception: installs that use organizations run two
one-off commands afterwards, described under [Tenancy mode](#tenancy-mode).)

This page lists what you will notice afterwards, so that nothing surprises you. Most of it is
improvements that need no action. A few items are worth a minute of attention if they apply to
your install. They are marked **Check this if...**.

## Before you upgrade: back up

Take a database backup first. It is the only way back if you need one (see
[Rolling back](#rolling-back)).

```bash
# An installed deployment (the script the installer placed next to your compose files)
./opentranscribe.sh backup

# A git clone of the repository
./opentr.sh backup
```

[Backup & Restore](./backup-restore.md) covers encrypted backups and backing up media and
search data as well.

## Upgrade

```bash
./opentranscribe.sh update                    # pull the new images and restart
./opentranscribe.sh update --version v0.6.0   # or pin this release explicitly
```

`update` restarts in phases and waits for the backend's own health check, so the migrations
below can finish before anything gives up on the backend. See [Upgrading](./upgrading.md) for
the general procedure, manual `docker compose` steps and skipping versions.

## Database migrations

Nineteen migrations run automatically when the backend starts, from `v397` to `v445`. On a
populated database this can take a few minutes on first start. Nearly all of them only add
columns or tables. Where one changes data, the change is small, targeted, and leaves your
recordings, transcripts, speakers and voiceprints alone.

| Migration | What it does | Changes existing data? |
|---|---|---|
| `v397` | Adds `user.platform_super_admin_link_authorized`, default false | No |
| `v420` | Tags belong to one tenant (an organization, or your personal workspace) | Only on installs that use organizations: organization tags are moved, split or merged into the right tenant. Otherwise only index definitions change |
| `v421` | Adds `media_file.playback_path` for browser-playable renditions | No |
| `v422` | Collections belong to one tenant, the same rule as tags | Only on installs that use organizations. Otherwise only index definitions change |
| `v430` | Speaker profile, speaker collection and vocabulary names are unique per tenant | Only on installs that use organizations (unambiguous rows get their organization filled in). Otherwise the new indexes enforce exactly the old rule |
| `v431` | Adds `media_file.duration_source` | No |
| `v432` | Adds the support-access grant tables | No (they stay empty on a single-tenant install) |
| `v433` | Stores each file's own speaker range and skip-speaker-detection choice | Watch sources that hold exactly the old default range 1 to 20 are reset to "use my default range" |
| `v434` | Resets an unusable pyannote.ai speaker-detection choice | Only users who chose pyannote.ai, saved no pyannote.ai key and transcribe locally: their choice becomes **Automatic**, which is what they were already getting |
| `v436` | Lite only: moves a "this server" speaker-detection choice to **Automatic** | Only on lite, and only for users whose transcription provider returns speakers. A no-op on the full image |
| `v437` | Adds `media_file.processing_provenance` | No |
| `v438` | Adds `speaker_correction_carryover` | No (empty until a corrected file is re-run) |
| `v439` | Records `suggestion_source` on legacy speaker suggestions that the database proves came from a voice match, so those speakers keep their names | Data fix only: it writes one column on provable rows and nothing else |
| `v440` | Adds `user.password_last_self_changed_at`, so the first password change an account holder makes is never refused for being too recent | No (empty until a user changes their own password) |
| `v441` | Adds the `legal_acceptance` table, which records an administrator's acknowledgment of the operator terms | No (empty until an administrator acknowledges) |
| `v442` | Adds `media_file.takedown_origin`, recording whether the operator or an organization holds a taken-down file | Marks every file that is already quarantined or under legal hold as held by the operator, which is who placed it |
| `v443` | Adds the `takedown_notice` and `takedown_notice_file` tables, the durable record of each takedown notice | Adds one "operator action" record for every file that is already quarantined or under legal hold, so none is left without a record |
| `v444` | Adds the `takedown_notification` table, a log of who was told about a takedown | No (empty until an owner is emailed) |
| `v445` | Adds the `organization_policy` table: an organization's switches for voiceprint matching and inferred gender, and its redaction floor | No (empty until an organization saves a choice; both switches default on) |

`v434` and `v436` change a single user setting each, and only for users whose current choice
could not run any more. No transcript, speaker, voiceprint or profile is touched by either.

:::note[The migration chain is one-way]
Migrations cannot be undone in place. Rolling the images back to 0.5.x does not roll the
database back, and `./opentranscribe.sh update --rollback` refuses when the older version
cannot read the newer schema. A real rollback is restoring the backup you took before
upgrading. See [Rolling back](#rolling-back).
:::

## What behaves differently afterwards

### Speaker names come from you or a voice match, never from an AI guess

A speaker's name in search, chat, summaries and exports now comes only from:

- a name a person typed or confirmed, or
- a voiceprint match to a speaker profile or promoted cluster at 75% similarity or more.

An AI speaker-name suggestion is still made, stored and offered with its confidence in the
speaker editor and suggestion lists, and accepting it sets the name. It no longer becomes the
displayed name on its own once its confidence reaches 75%, as it used to.

**What you will see:** speakers whose label only ever came from an AI suggestion go back to
their diarizer label (`SPEAKER_01` and so on) until you accept or type a name. This happens
automatically, in the background: the search-index maintenance task re-indexes the affected
files at startup and then every 6 hours, 100 files at a time. No manual reindex is needed.
Names you typed or confirmed are not affected.

### Unassigning a speaker is a correction that sticks

Unassigning a speaker from a cluster now means "this voice is not that person":

- the speaker also leaves the cluster's profile (a name you typed yourself is kept);
- with **Prevent from rejoining this cluster** ticked (the default), that is recorded, and no
  automatic path puts it back: upload-time clustering, **Re-cluster All**, retroactive
  matching, profile propagation, cluster merges and AI suggestions all respect it;
- the correction survives re-transcribing, re-detecting speakers or retrying the file.

To undo a wrong unassignment, name that one speaker explicitly: type the name, assign it to
the profile, or use **Accept** or **Assign to profile** for it on the **Review** tab of the
Speakers page. Untick **Prevent from rejoining this cluster** to unassign without recording
anything.

**Check this if** you have unassigned speakers before: the first **Re-cluster All** after the
upgrade also takes back a profile link that an earlier unassignment left behind.

### `ENABLE_DIARIZATION` is gone

Speaker detection follows each user's own setting, **Settings > Speaker Identification >
Speaker Detection** (default **Automatic (recommended)**), and every run is planned from it.
The variable is no longer read, written by the installer or listed in `.env.example`, and
`GET /api/system/stats` no longer reports `diarization_enabled`. A leftover line in your `.env` is
ignored; you can delete it.

**Check this if** you had `ENABLE_DIARIZATION=false` (for example on a CPU-only server): speaker
detection now runs for every user whose setting is Automatic. To keep it off, choose **Off (one
speaker for the whole recording)** in that setting. The upload API can also skip it for a single
file (`disable_diarization`, API only).

### Lite is cloud-only for transcription and speaker detection

A lite deployment (no GPU) transcribes and detects speakers through cloud providers only.
Local Whisper (including the tiny and base CPU models) and this server's speaker detection are
no longer offered on lite. Voiceprints for speaker matching are still computed on the server.

**Check this if** you run lite:

- Users whose transcription provider returns speakers were moved to **Automatic** by `v436`
  and keep getting speakers from that provider.
- Users with a text-only cloud model, or no cloud provider configured, will see a blocking
  problem in **Settings > Processing pipeline** and a fixed message on new files explaining
  what to choose (pyannote.ai or another provider that returns speakers, or a cloud
  transcription provider). Files already processed keep their speakers.

[Deployment variants](../installation/deployment-variants.md#which-combinations-are-valid)
shows every valid combination, generated from the code.

### Speaker detection does what you chose, including pyannote.ai

Every combination of transcription and speaker detection now runs as selected. In particular,
a user who chose the pyannote.ai cloud service and saved a pyannote.ai key, but transcribes
with local Whisper, used to get this server's speaker detection silently. They now get
pyannote.ai, which bills their key for it. **Settings > Processing pipeline** shows, before
the next upload, which step runs where and which providers receive audio.

### Notifications clear themselves

The notifications panel now keeps itself tidy. Read, successful, informational notifications
are removed 24 hours after you read them, unread ones after 14 days, and the list is capped at
100. Errors, warnings, work in progress and anything that needs your action are never removed
automatically. Choose how long to keep them with **Keep notifications for** at the bottom of
the panel: **24 hours after reading** (default), **7 days after reading**, or **Never
auto-clear** (the 100 cap still applies to routine items). Notifications and this choice are
stored in your browser, so each browser keeps its own.

### Tenancy mode

`TENANCY_MODE` (default `auto`) decides whether administrators can open any user's content.

- **A single-user or single-team install with no organizations** resolves to single-tenant.
  Nothing changes and there is nothing to set.
- **An install that has active organizations** resolves to multi-tenant: `admin` and
  `super_admin` no longer open tenant content by its ID and need a time-boxed, audited
  [support-access grant](./support-access.md) instead.

**Check this if** you created organizations and rely on administrators reaching every user's
files: set `TENANCY_MODE=single` in `.env` and restart to keep the previous behaviour.

**Check this if** you use organizations at all: run two one-off commands in the backend
container after the upgrade, so that search documents and speaker data written before 0.6.0
carry their organization.

```bash
docker compose exec backend python -m app.tasks.tenant_backfill_task
docker compose exec backend python -m app.scripts.backfill_tenant_stamps --sync-voiceprints
```

The first stamps older search and voiceprint documents with their file's organization. The
second copies the organization that migration `v430` filled in onto speaker-profile
voiceprints, and lists the few rows whose organization was ambiguous so an administrator can
decide. An install without organizations has nothing to stamp and can skip both.

### Smaller things you may notice

- **The interface is refreshed.** The navigation reads Gallery, **Transcript Search**, **AI
  Chat**, Speakers; **File status** moved into **Settings > System**; the Speakers page's
  *Inbox* tab is now **Review**. Settings is regrouped: Transcription and Speaker
  Identification are separate tabbed sections (the speaker engine is the **Speaker Engine**
  tab), Privacy and Redaction are one section, and data tools are tabs of **Data & Storage**.
  Old links to settings sections and to `/file-status` still work.
- **Media durations** for files processed before 0.6.0 may be the length of the speech, not
  the recording (trailing silence was cut, #969). New files are correct. A `super_admin` can
  repair existing files with `POST /api/admin/media-duration-backfill` (dry run by default).
- **Formats browsers cannot play** (AIFF, WMA, AVI and similar) get a playable copy from now
  on. Files uploaded before 0.6.0 get one when they are reprocessed.
- **Password policy:** nothing changes on upgrade. An install whose `.env` has no
  `PASSWORD_POLICY_PROFILE` line keeps the `hardened` rules; see
  [Password policy levels](../configuration/password-policy.md) to choose another level.
- **Telemetry:** third-party telemetry and update checks that used to be on by default
  (pyannote metrics, Hugging Face, Deno, MinIO) are now off. The Privacy block in `.env.example`
  shows how to opt back in.
- **New optional `.env` settings** for task recovery, GPU memory admission, search indexing
  retries and worker metrics all have defaults. Compare your `.env` with the new `.env.example`
  after upgrading if you want to tune them.

## After upgrading

1. Check that the backend started and the migrations finished:
   ```bash
   ./opentranscribe.sh logs backend | grep -i -E "alembic|migration|error"
   ```
2. Confirm the version: `curl -s http://localhost:5174/api/version`.
3. Open **Settings > Processing pipeline** and check that it shows no blocking problem for
   your account.
4. Upload a short file and check that it transcribes, gets speakers and becomes searchable.

## Rolling back

Restore the backup you took before upgrading, then return to the previous images:

```bash
./opentranscribe.sh restore ./backups/opentranscribe_backup_<timestamp>.sql
./opentranscribe.sh update --rollback      # or: update --version v0.5.1
```

Restore leaves services stopped when the backup's schema does not match the running image, so
the newer backend does not start over your older database. [Upgrading](./upgrading.md#rolling-back)
has the full procedure.
