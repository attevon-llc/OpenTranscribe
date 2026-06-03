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
source card. Expand a source to see its per-file history, including skip reasons (duplicate,
too old, invalid type) and stitched parts.

## Email notifications (experimental)

:::warning Experimental
Email delivery has not yet been verified against a live provider — test before relying on it.
:::

Administrators can add reusable email configurations under **Settings → Watch Sources → Email
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

## Admin: global settings

Administrators see a **Global Settings** panel:

- **Watch Sources enabled** — master on/off.
- **File stability wait (seconds)** — files modified within this many seconds are treated as
  "still being written" and skipped until the next scan (default 30).
- **Max imports per scan** — bounds how many files one scan imports at once.

All of these are stored in the database and take effect on the next scan — no restart.

## Troubleshooting

- **No "Local Folder" option** — start the stack with `--with-watch` and set `WATCH_HOST_PATH`.
- **A just-copied file wasn't imported** — it's within the file-stability window; it will be
  picked up on the next scan.
- **A file shows "skipped (duplicate)"** — the same content already exists in your library or
  another source; the row links to the existing file.
- **Connection test fails** — re-check credentials, endpoint/SSL (S3), and that the server/path
  is reachable from the container network.
