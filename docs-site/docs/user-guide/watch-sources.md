---
sidebar_position: 7
---

# Watch Sources

This guide walks through setting up automatic media import from a local folder, an S3 bucket,
or an SMB share. For a conceptual overview see [Watch Sources (feature)](../features/watch-sources.md).

## Before you start: mounting a local folder

Local-folder watching is the only part that needs a deployment step, because the container must
be able to see the folder. Start the stack with the watch overlay and point it at your host
folder:

```bash
# WATCH_HOST_PATH is the only watch-related environment variable.
WATCH_HOST_PATH=/path/to/your/media ./opentr.sh start dev --with-watch
```

This mounts your host folder to `/watch` inside the containers and creates it if needed. Set
`WATCH_HOST_PATH` in your `.env` to make it permanent. If you start **without** `--with-watch`,
the **Local Folder** type is hidden and only S3 and SMB sources are available (those need no
mount).

S3 and SMB need no special startup — just the connection details below.

## Adding a watch source

Open **Settings → Watch Sources** and click **Add Watch Source**. The editor is a short wizard;
use **Next** / **Back** to move through the steps so nothing gets missed.

### Step 1 — Connection

Give the source a **Name**, choose a **Source Type**, and fill in the connection fields:

- **Local Folder** — use **Browse** to pick a subfolder under the mount (or leave the path
  blank to watch the whole mount). Optional **Delete originals after import** removes each file
  from the folder once it has been imported successfully.
- **S3 Bucket** — endpoint URL (leave blank for AWS), bucket, optional prefix, region, access
  key ID, secret access key, and whether to use SSL/TLS. Works with MinIO, Backblaze B2, and
  Wasabi by setting the endpoint URL.
- **SMB Share** — server, share, path, username, password, optional domain, and port (445).

Click **Test Connection** (available once the source is saved) to confirm OpenTranscribe can
reach it.

### Step 2 — Processing

- **Scan interval (minutes)** — how often this source is polled.
- **Skip files older than (days)** — only import files modified within this window. For
  example, set `30` to add a large archive folder but only process the last month. Leave it
  blank to import all ages.
- **File extensions** — comma-separated (e.g. `.mp4,.mp3`). Leave blank for all files; every
  file is still validated, so only real audio/video is imported.
- **Min / Max speakers**, **Scan subfolders recursively**, and **Transcribe automatically on
  import** (turn this off to import without running transcription yet).

### Step 3 — Advanced (multi-part stitching)

If recordings arrive split into parts because a connection dropped (e.g.
`meeting_P001.mp4`, `meeting_P002.mp4`), enable **Stitch multi-part recordings**. The default
part-name pattern matches a `_P###` suffix; use the built-in tester to check a filename. You
can also set the time window for grouping parts and how many scans to wait for missing parts.

### Step 4 — Organize

Optionally choose **Tags** and **Collections** to apply to every imported file. Pick from
existing ones or type a new name to create it. Use **Enabled** to turn the source on.

Click **Save**. The new source appears as a card showing its type, last-scan status, and live
counts (imported / skipped / errors).

## Running scans

Sources scan automatically on their interval. To import right away, click **Scan Now** on a
source card.

### Near-instant pickup (local folders)

A local folder can also be watched for filesystem events, so a new recording is picked up
seconds after it lands instead of at the next scan. An administrator turns on **Allow
file-system event watching** in Global Settings; each local source then gets a **Watch for
file-system events** checkbox.

The source card shows which method is actually in use, so this is never a silent promise:

| Badge | Meaning |
| --- | --- |
| **FS events** | Native OS notifications — the fastest path. |
| **FS polling** | The folder is re-checked every few seconds instead. This is normal and expected on a NAS/network share, and on Docker Desktop for macOS or a Windows drive, where the operating system does not forward change notifications into the container. |
| **Watch failed** / **Watch unavailable** | The watcher could not start; hover for the reason. |
| **Every N min** | Nothing is watching — the scheduled scan is the only mechanism. |

The scheduled scan keeps running in every case, so imports still happen even if watching is
unavailable.

## Per-file history: what a source imported, skipped, or failed on

:::note New in v0.6.0
Previously the source card showed only aggregate counts, so there was no way to see *which*
file failed or why. Earlier versions of this page described expanding a source for its
per-file history; that screen did not exist until now.
:::

Click **Files** on a source card to open its import history. Each row is a *tracking record* —
what the scanner observed at a path — not a library file:

| Column | What it tells you |
| --- | --- |
| **File** | The file name; hover for its full path in the source. An imported row links straight to the library entry it produced. |
| **Status** | `Imported`, `Error`, a `Skipped — …` variant, or a multipart state. |
| **Reason** | The error message, or a plain-language skip reason (already in your library, too old, unsupported type…). |
| **Attempts** | How many import attempts failed. On a row waiting for multi-part siblings this column reads **scans waited** instead — it is a different counter, not a failure count. |
| **First seen** | When the scanner first observed the file. |

Use the search box to find a file by name and the status dropdown to narrow the list; both
filter on the server, so they work on a source tracking thousands of files.

### Retrying a file

**Retry** re-queues a file for import. It is available on failed and skipped rows, and is the
only way to bring back a file that was skipped — a skipped record is otherwise final, so
fixing the underlying problem alone would never re-import it. Retry is deliberately not
offered on a file that already imported (that would duplicate it), on one currently in flight,
or on a part already folded into a stitched recording.

:::info Retry queues; it does not import immediately
The row moves to **Pending** and a scan is requested. That scan may wait behind one already
running, may not reach your file if a lot is queued ahead of it (see **Max imports per scan**),
and can only re-import a file still present in the source. The list refreshes itself as soon as
a scan finishes, so the real outcome replaces **Pending** on its own — you do not need to
reload.
:::

Select several rows to **Retry selected** or **Delete selected** in one go. A batch reports per
file, so if one row is refused the rest still proceed.

### Deleting a record

**Delete record** removes only the tracking row. The file stays in the source and anything
already imported stays in your library; the next scan simply re-checks the file. Use it to
clear noise, not to delete media.

## Email notifications (experimental)

:::warning Experimental
Email delivery has not yet been verified against a live provider — test before relying on it.
:::

Super admins can add reusable email configurations under **Settings → Watch Sources → Email
Notifications**, then link them to a source. Click the info (ⓘ) icon for setup guidance:

- **SMTP (Gmail, Outlook, Yahoo, …)** — host, port, username, password. If your account uses
  two-factor authentication, create an **App Password** and use that instead of your login
  password. Typical Gmail settings: `smtp.gmail.com`, port `587`, STARTTLS.
- **Microsoft 365** — for tenants where SMTP is disabled. Register an Azure AD application with
  the **Mail.Send** application permission (admin-consented), then enter its tenant ID, client
  ID, and a client secret. Mail is sent via the Microsoft Graph API.
- **Exchange (on-prem)** — server host, optional domain, and a mailbox username/password
  (authenticated SMTP submission).

Use **Test** on a saved email config to validate the connection.

### Choosing which sources a config notifies

:::note New in v0.6.0
Configurations could be created but not attached to a particular source, so "email me only when
*this* source has a problem" could not be expressed.
:::

Click **Notifications** on a source card. You do **not** need to be a super admin — creating a
configuration holds mailbox credentials and stays super-admin work, but attaching an existing
one to a source you own is yours to do. Pick a configuration, attach it, and set per link:

- **Notify on a successful scan** and **Notify when a scan has errors** — these are evaluated
  **per scan, not per file**: a scan counts as an error if any file in it failed.
- **Additional recipients** — a comma-separated list, sent *in addition to* the configuration's
  own default recipients. A malformed address is rejected as you save rather than silently
  dropped at send time.

The panel warns you when a link is configured but would send nothing — both options switched
off, a disabled configuration, or no recipients on either side. Each of those looks complete on
its own, and only the combination is empty.

Deleting a configuration detaches it from every source using it, so those sources stop being
notified. The count of sources using a configuration is shown next to it before you delete.

## Admin: global settings

:::note Changed in v0.5.0
The Global Settings panel and the email-notification configs above now require the
**super_admin** role rather than `admin`. Managing your own watch sources is unaffected.
:::

Super admins see a **Global Settings** panel:

- **Watch Sources enabled** — master on/off.
- **File stability wait (seconds)** — files modified within this many seconds are treated as
  "still being written" and skipped until the next scan (default 30).
- **Max imports per scan** — bounds how many files one scan imports at once.
- **Allow file-system event watching** — lets local sources opt into near-instant pickup. When
  enabled, two more controls appear:
  - **Watch method** — *Automatic* (default) checks whether the operating system really
    delivers change notifications for each folder and quietly switches to periodic re-checking
    when it does not. *Native events only* / *Polling observer only* force one method; *Off*
    disables watching entirely.
  - **Fallback sweep interval (seconds)** — how often a folder is re-checked when native events
    are unavailable. Lower means faster pickup; raise it for very large or network folders.

All of these are stored in the database and take effect on the next scan — no restart.

## Troubleshooting

- **No "Local Folder" option** — start the stack with `--with-watch` and set `WATCH_HOST_PATH`.
- **A just-copied file wasn't imported** — it's within the file-stability window; it will be
  picked up on the next scan.
- **A file shows "skipped (duplicate)"** — the same content already exists in your library, in
  another source, or under a different name in **this** source. Open **Files** on the card to
  see which. If it really is content you want, **Retry** it after removing the copy that is
  shadowing it.
- **I retried a file and nothing happened** — retry queues the file; the scan it requests may be
  waiting behind one already running, or the file may be beyond **Max imports per scan** for
  this pass. Leave the Files list open: it refreshes when a scan completes.
- **A retried file goes straight back to "skipped — too old"** — the source still limits imports
  by age. Clear **Skip files older than** on the source, then retry again; the Files list warns
  about this on the row.
- **I attached an email configuration but receive nothing** — check the warnings in the
  **Notifications** panel: both notify options may be off, the configuration may be disabled, or
  neither the configuration nor the link may name a recipient.
- **A source shows "FS polling" instead of "FS events"** — expected on network shares and on
  Docker Desktop for macOS/Windows: the host does not forward change notifications into the
  container, so the folder is re-checked on a short interval instead. Hover the badge for the
  exact reason.
- **A local source shows "Every N min" although the box is ticked** — nothing is watching it.
  Check that the stack was started with `--with-watch` (celery-beat needs the folder mounted
  too) and look at `./opentr.sh logs celery-beat`.
- **Connection test fails** — re-check credentials, endpoint/SSL (S3), and that the server/path
  is reachable from the container network.
