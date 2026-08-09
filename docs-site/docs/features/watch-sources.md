---
sidebar_position: 8
---

# Watch Sources (Auto-Import)

Watch Sources let OpenTranscribe **automatically import and transcribe** new media as it
appears in a location you point it at — a mounted local folder, an S3-compatible bucket, or
an SMB/CIFS network share. New files are copied into OpenTranscribe's storage and run through
the full transcription / diarization / embedding pipeline without anyone having to upload them
by hand.

A core principle: **originals on remote sources are never moved or deleted.** OpenTranscribe
copies the bytes it needs and leaves your source untouched (a local folder can *optionally*
delete-after-import if you ask it to).

## Source types

| Type | Backed by | Typical use |
|------|-----------|-------------|
| **Local Folder** | A directory mounted into the container | A NAS share or a drop folder on the server |
| **S3 Bucket** | `boto3` (works with AWS S3, MinIO, Backblaze B2, Wasabi, …) | Cloud object storage, on-prem MinIO |
| **SMB Share** | `smbprotocol` (pure-Python SMB2/3) | Windows / NAS CIFS shares |

## What it does

- **Scheduled polling.** A background scheduler checks each enabled source on its own interval
  (default 15 minutes). You can also press **Scan Now** for an immediate pass. Adding, editing,
  enabling, or disabling a source takes effect on the next scan — **no restart required**.
- **Three-layer deduplication.** Every candidate file is fingerprinted with a fast
  constant-time content hash (imohash) and checked three ways: against files already seen in
  the same source, across your other watch sources, and against everything already in your
  library (manual uploads, URL imports, prior watch imports). Duplicates are recorded with a
  reason and linked to the existing file — they are never re-imported.
- **Age filter.** "Skip files older than N days" means you can add a folder containing years of
  recordings and only process, say, the last 30 or 90 days. Leave it blank to import everything.
- **Type filtering + validation.** Restrict to specific extensions, or leave it blank for all
  files — every file is magic-byte validated, so only real audio/video is imported (a stray
  `.txt` is recorded as *skipped (invalid)*).
- **Multi-part stitching.** Recordings split by a dropped connection (e.g.
  `meeting_P001.mp4`, `meeting_P002.mp4`, …) are detected by a configurable pattern, grouped
  within a time window, and rejoined into a single file with ffmpeg before transcription.
- **Near-instant pickup for local folders (optional).** Scheduled polling is always the safety
  net, but a local folder can also be watched for filesystem events, so a new recording is
  picked up seconds after it lands rather than at the next scan. An administrator opts in
  globally; the watcher automatically falls back to polling on mounts that don't forward change
  notifications into the container (network shares, Docker Desktop for macOS/Windows).
- **Auto-organize.** Apply tags and collections to every imported file — pick from existing
  ones or create new.
- **Email notifications (experimental).** Optionally send a scan-summary email via SMTP,
  Microsoft 365 (Graph), or on-prem Exchange.

:::warning Email notifications are experimental
Email delivery has not yet been verified against a live mail provider. The configuration and
UI are complete, but **test your setup before relying on it** for production alerts.
:::

## Where it lives

Everything is managed from **Settings → Watch Sources**. Each user manages their own sources;
administrators additionally get an "All Sources" view, the shared email-notification configs,
and the global tuning knobs.

:::note Changed in v0.5.0
The shared email-notification configs and the global tuning knobs (Global Settings panel) now
require the **super_admin** role rather than `admin`. Managing your own sources is unaffected —
that stays open to every user.
:::

The only deployment-time setting is the physical folder mount —
see the [Watch Sources user guide](../user-guide/watch-sources.md) to get started.

## Imported files

An imported file is a normal media file in your library — it appears in the gallery owned by
the source's user, transcribes automatically (unless you turn that off), and supports every
feature any other file does (search, speakers, summaries, export). The watch source keeps a
per-file history showing what was imported, skipped (and why), stitched, or errored.
