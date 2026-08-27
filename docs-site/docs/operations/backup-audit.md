---
sidebar_position: 5
title: Backup Completeness Audit
description: A store-by-store assessment of what OpenTranscribe backs up, where the gaps are, and how to reach a real 3-2-1 posture
---

# Backup Completeness Audit

This page is an honest, store-by-store assessment of OpenTranscribe's backup coverage
**as currently shipped**. It complements the how-to in
[Backup & Restore](./backup-restore.md): that page tells you *how* to run a backup; this
page tells you *what is and isn't protected* and where you must act yourself.

:::danger The one thing most people get wrong
A database backup is **worthless without the encryption keys**. OpenTranscribe encrypts
secrets (user API keys, the S3 backup secret, watch-source credentials, email passwords,
MFA secrets) into the database using a key that lives in **`.env`, not in the database**.
If you back up the database but lose `.env`, those columns are **permanently
undecryptable** — and if your scheduled backups are gpg-encrypted, the backup itself is
unrecoverable without its passphrase. **Back up `.env` (and any gpg passphrase) separately
from, and as carefully as, the database.** See [Configuration & Secrets](#secrets-gap)
below.
:::

## At a glance

| Store | What's protected today | Gap | Severity | Recommendation |
|---|---|---|---|---|
| **PostgreSQL** (users, transcripts, segments, speakers, settings) | In-app scheduled `pg_dump -Fc` (GFS retention, optional gpg) to a local mount **or** S3-compatible bucket; manual `./opentr.sh backup [--encrypt]`; `restore` covers both plain-SQL and `-Fc`/S3 artifacts, with a real integration-test round-trip (#600) | Restore is not automatically *verified on a schedule* (no periodic restore drill / checksum) | **Low** | Run the quarterly restore drill in [Backup & Restore](./backup-restore.md#testing-backups). Good as shipped. |
| **MinIO media** (~484 GB, irreplaceable originals) | **Addressed (#242):** in-app scheduled **Media Mirror** — incremental, never-deleting copy of the media bucket to a mounted folder or S3-compatible bucket, with metrics + failure alerting | Default-OFF (must be enabled + given a destination); mirror is single-copy (pair with offsite for 3-2-1) | **Low** (was High) | Enable the mirror and point it off-host; optionally add bucket versioning as a deployment-level extra. See [MinIO media](#2-minio-media--the-484-gb-gap). |
| **OpenSearch** (search + vector indices) | Optional in-app `fs` snapshot alongside each dump (`backup.include_opensearch`); fully **rebuildable** from Postgres via reindex | None that matters — derived data | **Low** | Leave snapshots off unless you want to skip reindex time on restore. Confirmed adequate. |
| **Configuration & Secrets** (`.env`: `ENCRYPTION_KEY`, `JWT_SECRET_KEY`, DB/MinIO creds; gpg passphrase) | **Addressed (#243):** encrypted runs write `opentranscribe-recovery.env.gpg` (the essential keys, same passphrase) beside the dumps; unencrypted runs write a no-secrets `RECOVERY-README.txt` + a one-time admin warning | With encryption off, keys must still be preserved separately (by design — no plaintext keys beside a plaintext dump) | **Low** (was Critical) | Keep the gpg passphrase in a password manager and verify keys in every restore drill. See [§4](#secrets-gap). |
| **Redis** (Celery broker/cache) | Nothing — by design | None | **None** | Ephemeral. Tasks re-queue (acks-late). No backup needed. Confirmed. |
| **Model cache** (~2.5 GB AI weights) | Nothing — by design | None | **None** | Re-downloaded on first use. Back up only for air-gapped installs. |
| **Backup-failure visibility** | **Addressed (#244):** failed runs send an admin WebSocket notification and land in `backup.last_result`; Prometheus exposes `backup_last_success_timestamp_seconds`, `backup_last_status`, and `backup_runs_total{result}` for staleness alerting | Alert rules are not pre-provisioned in Grafana — add one on last-success staleness | **Low** (was Medium) | Alert on `time() - backup_last_success_timestamp_seconds > N`. See [§6](#6-backup-failure-visibility). |

## 1. PostgreSQL — adequate

The relational store (every user, transcript, segment, speaker, and setting) is the
authoritative state of the system and is well covered:

- **Scheduled, in-app:** `backend/app/services/backup_service.py` runs `pg_dump
  --format=custom` from the worker on the existing `celery-beat` schedule (no host cron),
  applies grandfather-father-son retention, optionally gpg-encrypts (AES-256), and writes
  to either a **mounted folder** or an **S3-compatible bucket** — the latter already gets
  the dump **off the host**.
- **Manual:** `./opentr.sh backup [--encrypt]` and `./opentr.sh restore <file>`.
- **Restore covers both dump formats through one command**, `./opentr.sh restore`, which
  dispatches on the file's magic bytes: plain SQL/gzip via `psql`, custom-format (`-Fc`,
  what the scheduled/S3 backup produces) via `pg_restore` — reusing the same confirm /
  safety-dump / drop-recreate / verify sequence either way, plus `--from-s3` to fetch an
  S3-destination artifact before anything destructive (issue #600). Proven by a real
  integration-test round-trip against a throwaway Postgres, not just documented.

⚠️ **This row previously read "restore is documented" at Severity Low, and that assurance
was false.** The documented custom-format restore command was missing its stdin redirect
(failed outright) — and fixing that naively would have reproduced issue #599's silent
data-corruption bug (drifted data survives a restore that reports success). Nothing tested
the custom-format path at all before #600. "Documented" was doing the work "verified"
should have been doing, and that mismatch is very likely why nobody caught it.

**Gap:** restore is not automatically *verified on a schedule* — an untested-in-production
backup is still a hypothesis until you've actually run the drill. **Recommendation:**
schedule the quarterly restore drill in [Testing Backups](./backup-restore.md#testing-backups),
which now exercises both dump formats. **Severity: Low.**

## 2. MinIO media — the ~484 GB gap

The uploaded audio/video originals in MinIO are **irreplaceable** — unlike OpenSearch they
cannot be rebuilt from anything else. They are **co-critical with PostgreSQL**: losing
either leaves you with half a system.

The in-app scheduler does **not** back up media (it is a `pg_dump` + optional OpenSearch
snapshot only). Today the media is protected solely by host-level RAID/NAS. **RAID is not a
backup** — it survives a disk failure but not an accidental/malicious delete, a bad
migration, ransomware, bit-rot, or loss of the whole machine.

**Options (assessed, not yet built):**

- **`mc mirror` to a second location** — incremental copy of the media bucket to another
  machine, an external drive, or a remote S3 endpoint. Simplest path to an **off-host**
  copy; for write-once media the steady-state delta is tiny. This is the most direct fix
  and pairs naturally with the existing celery-beat schedule.
- **S3 bucket versioning** — turns deletes/overwrites into recoverable previous versions.
  Near-zero steady-state cost for write-once video. (David is still evaluating this; it is
  complementary to — not a substitute for — an off-host copy, since versioning still lives
  in one bucket on one machine.)
- **S3 replication** — bucket-to-bucket replication to a second provider/region for a true
  offsite second copy.

**Recommendation:** add an automated **off-host media mirror** (mirror and/or replication),
and turn on **versioning** for deletion protection. Until that ships, mirror manually with
`mc mirror` per [Backup & Restore → MinIO](./backup-restore.md#minio--storage-backup).
**Severity: High.**

**Status: CLOSED by issue #242.** The in-app **Media Mirror** now covers this gap:
a scheduled (celery-beat, DB-configured, default-OFF) **incremental** copy of the
media bucket to a **separate destination** — a mounted folder
(`BACKUP_MIRROR_HOST_PATH` → `/media-mirror`) or an **S3-compatible bucket**
(encrypted write-only secret, Test Connection) for a true off-host copy. Objects
are compared by size + ETag so nightly steady-state deltas are tiny; regenerable
prefixes (temp audio; the `processed-videos` derived/bulk caches) are excluded;
per-object failures never abort a run; a Redis lock prevents overlapping runs.
**The mirror never deletes at the destination** (tested invariant) — a source-side
mass delete or ransomware event cannot propagate into the mirror. Failure alerting
and Prometheus staleness metrics follow the #244 pattern
(`media_mirror_last_success_timestamp_seconds`, `media_mirror_runs_total{result}`,
per-outcome object counts). Bucket **versioning** remains an optional
deployment-level extra, not a requirement. Setup + restore-from-mirror:
[Backup & Restore → Media Mirror](./backup-restore.md#media-mirror-in-app-incremental).
Residual note: the mirror is one additional copy — for full 3-2-1, point it (or a
second replica) offsite.

## 3. OpenSearch — adequate (derived data)

Every search and vector index is **rebuildable from PostgreSQL** via the reindex tasks, so
OpenSearch is not a data-safety concern. The in-app scheduler can *optionally* take an `fs`
snapshot beside each dump (`backup.include_opensearch`) purely to **skip reindex time** on
restore. Leave it off and nothing is lost. **Confirmed adequate. Severity: Low.**

## 4. Configuration & Secrets — the sneaky-critical gap {/* #secrets-gap */}

This is the audit's most important finding.

**How keys are sourced.** `backend/app/core/config.py` reads `ENCRYPTION_KEY` and
`JWT_SECRET_KEY` from the **environment** (i.e. `.env`), with insecure built-in defaults
that only trigger a warning:

```python
JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "this_should_be_changed_in_production")
ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "this_should_be_changed_in_production_for_api_key_encryption")
```

**What the encryption key protects.** `backend/app/utils/encryption.py` derives an
AES-256-GCM key (PBKDF2-SHA256, 600k iterations) from `ENCRYPTION_KEY` and encrypts every
sensitive column into the database, including:

- user-configured **LLM / ASR API keys**,
- the **S3 backup secret key** (`backup.s3_secret_key` — yes, the backup destination's own
  credential),
- **watch-source** S3 secrets and SMB passwords (`encrypted_s3_secret_key`,
  `encrypted_smb_password`),
- **email** SMTP / M365 / Exchange passwords,
- **MFA** secrets.

**The trap.** These ciphertexts live in the database, but the key that decrypts them lives
in `.env`. **A database backup does not contain the key.** If you restore a database onto a
new host with a different (or default) `ENCRYPTION_KEY`, **every encrypted column is
permanently undecryptable** — users must re-enter every API key and credential, and any
data that depended on those secrets is lost. The same applies to `JWT_SECRET_KEY` for
session continuity. And if your scheduled backups are gpg-encrypted, the **gpg passphrase**
is a second key with the same property: lose it and the backup file itself is unrecoverable.

**Is any of this backed up?** No. The keys are not part of any backup artifact the product
produces. The how-to docs *mention* copying `.env`, but there is no automated protection and
no prominent warning that the DB backup is **inert without it**.

**Recommendation.** Treat `.env` (specifically `ENCRYPTION_KEY` and `JWT_SECRET_KEY`) and
any gpg backup passphrase as **first-class backup artifacts**: store them in a password
manager or secrets vault, **separately** from the database dumps (so a single compromised
location can't expose both), and verify them as part of every restore drill. **Severity:
Critical** — this is almost always the biggest real-world gap.

**Status: addressed by issue #243.** Every successful scheduled run now writes a recovery
companion beside the dumps: with backup encryption on, `opentranscribe-recovery.env.gpg`
carries `ENCRYPTION_KEY` / `JWT_SECRET_KEY` (and `MINIO_KMS_SECRET_KEY` when set) under
the same gpg passphrase, making the destination self-sufficient for restore; with
encryption off, a no-secrets `RECOVERY-README.txt` (key names + SHA-256 fingerprints)
documents what to preserve separately, and admins get a one-time warning notification.
The **gpg passphrase itself** remains the one secret you must keep off the backup media.
See [Backup & Restore](./backup-restore.md#recovery-keys-travel-with-the-backup).

## 5. Redis — no backup needed (confirmed)

Redis is the Celery broker and a cache. Tasks are dispatched with **acks-late**, so
in-flight work re-queues after a restart; cached values regenerate. Redis state is
**ephemeral by design** and intentionally excluded from backups. **No action. Severity:
None.**

## 6. Backup-failure visibility

The scheduled backup records its outcome in `backup.last_result` (status, error, duration),
which the admin Backups page reads on demand. **But a failing scheduled backup is not
surfaced anywhere proactively** — no Prometheus metric, no notification, no UI banner. A
silently failing backup is **worse than no backup**, because it creates false confidence.

**Recommendation:** on each scheduled run, (a) emit a Prometheus gauge (e.g.
`opentranscribe_backup_last_success_timestamp_seconds` / `..._last_status`) from
`backend/app/core/metrics.py` so the existing Grafana/Prometheus stack can alert on
"no successful backup in N hours", and (b) send a notification when `last_result.ok` is
false. **Severity: Medium.**

**Status: addressed by issue #244.** The backend now exposes
`backup_last_success_timestamp_seconds`, `backup_last_status`, and
`backup_runs_total{result="success"|"failure"}` (persisted by the run task, projected onto
`/metrics` at scrape time so they survive restarts), and every failed run — plus any
success-with-warnings (prune / OpenSearch snapshot / recovery companion) — sends a
`backup_status` WebSocket notification to all admins. Alerting example:
`time() - backup_last_success_timestamp_seconds > 2 * 86400`. See
[Backup & Restore](./backup-restore.md#failure-alerting-metrics--notifications).

## 3-2-1 for OpenTranscribe

The industry baseline is **3-2-1**: **3** copies of your data, on **2** different media,
with **1** copy **offsite**. Mapped onto OpenTranscribe:

| 3-2-1 element | How to satisfy it |
|---|---|
| **3 copies** | (1) live data in Postgres + MinIO; (2) the scheduled `pg_dump` + a media mirror; (3) a second, independent copy of both (e.g. the S3 backup destination on a different box, plus an `mc mirror` target). |
| **2 media** | Don't keep every copy on the same RAID array. Use the host volume **and** a different machine / external drive / object store. |
| **1 offsite** | Point the in-app **S3 destination** (and a media mirror/replica) at a bucket on a **different machine or provider**, so a fire/theft/ransomware event on the primary host can't take the backups with it. The in-app S3 destination already makes this one config change away for Postgres. |

**Plus the cross-cutting keys.** 3-2-1 covers your *data*; it does **not** automatically
cover the **encryption keys** that make that data usable. Back up `.env`
(`ENCRYPTION_KEY` + `JWT_SECRET_KEY`) and any gpg passphrase **alongside** your 3-2-1
strategy, in a separate secure location. A perfect 3-2-1 of an undecryptable database is
still total data loss.

**Where OpenTranscribe stands today:** the in-app S3 backup destination gets you most of
the way to **1 offsite** for the database, and the in-app **Media Mirror** (§2, issue
#242) does the same for the media — point both at off-host destinations and the product
covers 3-2-1's mechanics for data. The remaining deliberate act is a separate backup of
the **keys** (§4) — and, as always, running the restore drill.
