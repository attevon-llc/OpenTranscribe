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

The migrations from `v397` to `v449` run automatically when the backend starts. On a
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
| `v446` | Adds `collection.visibility` (`organization` or `restricted`) so an organization collection can be limited to its creator, the organization admins and the people it is shared with | No: every existing collection stays visible to the whole organization until someone restricts it |
| `v447` | Adds the `content_hold` and `content_hold_file` tables, which record the legal holds an organization administrator places on files and collections | No (empty until an administrator places a hold) |
| `v449` | Adds `speaker.identity_source`, which records whether a speaker's name and profile link came from a person or from a voiceprint match | One data fix: it marks as voiceprint-made the existing identities the database can prove a voiceprint wrote (see [Which existing speaker names count as voiceprint-made](#which-existing-speaker-names-count-as-voiceprint-made)) and records how many it marked. Nothing else is rewritten; no name or link changes |

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

### Privacy controls

Voiceprint matching and inferred gender now each have a restrict-only switch at four levels
(platform, deployment, organization, user). See [Privacy controls](../features/privacy-controls.md).
The upgrade rewrites and deletes nothing: the only new table (`v445`, listed above) is empty, both
deployment switches default **on**, no consent hook is registered and no platform ceiling exists.
No voiceprint, profile or gender value is touched. It is **not** neutral for everyone, though. The
person-level gender switch is the setting that already existed (**Settings > Speaker
Identification > Voice Attributes**, stored as `speaker_attribute_gender_detection_enabled`), so a
person who had **already turned inferred gender off** now has every stored inferred gender hidden
everywhere: in their own views and also for collaborators who see their recordings through sharing
or an organization, because the setting of the recording's owner applies. A gender someone
confirmed by hand stays visible. Nothing is deleted, so turning the setting back on restores the
values.

Several more behaviors change, because they now honor the policy and the user's own gender setting:

- The speaker-name (LLM) step no longer sends its voice-versus-first-name gender comparison to a
  **remote** provider, even with gender inference on. A local model still receives it.
- The admin speaker-attribute (gender) migration and the combined speaker analysis skip files whose
  owner has gender prediction turned off, where before they wrote gender regardless. A forced reset
  leaves those files' values alone.
- The embedding consistency repair no longer re-creates missing voiceprints for a scope whose
  voiceprint switch is off.
- The v4 embedding migration skips files whose scope has voiceprints off and reports
  `skipped_by_policy` at finalize. The index that goes live holds no voiceprints for them.
  `GET /api/embeddings/migration/status` now carries `policy_skipped` so you can see the count
  before you start, and `POST /api/embeddings/migration/start` answers 409 `voiceprints_disabled`
  only when every recording is in a scope with voiceprints off.
- The consistency check reports `skipped_by_policy` and does not count a missing voiceprint in a
  scope with voiceprints off as missing, so it queues no repair for it.
- `GET /api/user-settings/speaker-attributes` now answers for the organization the request works
  in. `gender_detection_enabled` is the effective value and a new `gender_locked_by` names the layer
  (`platform`, `instance`, `organization`) that forbids it, or is `null`.
- A response that embeds a speaker (the file detail, the segment list, and the responses of a
  segment text edit and a speaker reassignment) returns the inferred gender and its name-versus-voice
  alignment as `null` while gender is off for that recording's scope.

**Check this afterwards:**

1. As a super admin, open **Settings > Privacy & Redaction > Policy** and confirm the card
   "Speaker recognition and inferred attributes" shows both switches on, or the choice you intend.
   Turning one off hides data but deletes nothing.
2. If you run organizations, an organization administrator can review **Privacy policy** in the
   organization settings. A new organization inherits the deployment's choice.
3. Tell users that gender is inferred automatically from voice and that each person can turn it off
   under **Settings > Speaker Identification > Voice Attributes**. This is the notice the interface
   now shows next to every gender.
4. Before you run the v4 embedding migration, check that no scope you want to keep has
   voiceprints off.
5. If you use organizations, run the two backfill commands under [Tenancy mode](#tenancy-mode).
   Earlier releases rewrote some voiceprint documents without their organization: a cluster
   document whenever its centroid was updated, and a speaker's own document when the embedding
   consistency repair, the v4 embedding migration, a speaker merge or the speaker index rebuild
   rewrote it. Such a document reads as personal, so it stops matching its organization's
   searches, and an organization's **Delete stored voiceprints** would not find it. The first
   command stamps each such document again with the organization of its recording (a cluster only
   when every member belongs to one organization; a cluster whose members span scopes is left
   unstamped and the next re-cluster rebuilds it). Run it before you delete stored voiceprints.
   This is a manual step: nothing runs it for you, on upgrade or later. An install without
   organizations is unaffected.

#### Which existing speaker names count as voiceprint-made

Switching voiceprints off hides, and a purge deletes, the names and profile links a voiceprint
match wrote, and never a name a person typed or confirmed. Telling them apart needs a record of who
wrote each one, and releases before 0.6.0 did not keep it: an automatic match and a person's
accept leave identical rows. Migration `v449` adds the record (`speaker.identity_source`: NULL for
unknown, `voiceprint`, `voiceprint_link`, or `human`) and fills it in for existing rows only where
the database can prove a voiceprint wrote them:

- A speaker is marked `voiceprint` when it is verified, linked to a profile, shows a name equal to
  both its stored suggestion and that profile's name, and that suggestion's source is a profile
  match. The matcher only stores such a suggestion at 75% similarity or more, and it is the same
  comparison that auto-accepts, so every such row was auto-accepted when it was written. A
  later rename by a person clears the suggestion, so a row that still shows the shape has not
  been renamed.
- **Everything else is treated as a person's**, because the database cannot tell it apart: an
  automatic match to another speaker (not to a profile), a link added by propagation, a retroactive
  match, and a cluster join made before the upgrade. Those names stay visible while voiceprints are
  off and a purge does not touch them. From 0.6.0 on, every one of these paths records its origin.
- The rule can mark a few rows that a person later confirmed by assigning the identical profile in a
  batch, or rows `v439` stamped from data older than the suggestion column. Such a name is hidden
  while voiceprints are off and reverted by a purge, and it is restored by turning the switch back
  on.

**How many rows were marked** is recorded when the migration runs, in the upgrade log and in the
database, so you can read it back at any time:

```sql
-- The count the migration recorded (JSON: revision, stamped, recorded_at)
SELECT value FROM system_settings WHERE key = 'privacy.identity_source_backfill';

-- The state now: names recognised as voiceprint-made, and legacy rows that look
-- machine-confirmed but could not be proven (these are treated as a person's)
SELECT count(*) FILTER (WHERE identity_source = 'voiceprint') AS recognised,
       count(*) FILTER (WHERE identity_source IS NULL AND verified IS TRUE
                        AND suggestion_source IN ('voice_match','profile_match')) AS unknown
FROM speaker;
```

The log line reads `v449: recognised N existing speaker identities as voiceprint-made`. If you
expected more, check `unknown`: that is the number of verified rows whose suggestion came from a
voice match but that the upgrade could not prove were not confirmed by a person.

**What does not change.** Summaries, topics and other text a language model already wrote may
contain a name that a voiceprint supplied. That text is model output, not a label, so it is not
rewritten. Regenerating a summary while voiceprints are off produces one without the name.

Deleting stored voiceprints or inferred gender is a separate action, available only after the
switch is off, and it cannot be undone. Backups and search snapshots keep their copies until they
rotate. A gender purge does not clear a speaker profile's gender, because only a person confirming
it sets that value.

An organization's audit log (managed editions) records `org.policy.change`,
`privacy.voiceprints.purge` and `privacy.gender.purge`. The organization's redaction floor can be
read and set through `/api/org-admin/policy`, but its screen is not part of this release.
A community install has no organization layer: that route answers 404 there.

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

The first stamps older search and voiceprint documents with their file's organization (it is also
a Celery task, `tenant_backfill_organization_id`, and is safe to repeat). The
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
