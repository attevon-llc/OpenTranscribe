---
sidebar_position: 11
title: Notifications and the About Dialog
description: Follow a file through processing in the notifications panel, and check which version you are running
---

# Notifications and the About Dialog

Two small pieces of the top navigation bar answer "what is happening to my file?" and "which
version is this?".

## The notifications panel

The **bell** at the right of the navigation bar opens the notifications panel. A number on the bell
counts unread items.

### One card per file

Everything that happens to one recording is shown on a **single card** headed by the file name
(click the name to open the file). The card has an overall progress bar and one chip for each of
three tiers of work:

| Tier | What it covers |
|---|---|
| **Main processing** | Import, transcription, speaker detection and (if you use it) redaction: the work that produces the transcript |
| **Finishing** | The follow-up steps that run after the transcript exists: search indexing, facts and key phrases, analytics, speaker gender, speaker suggestions, speaker clustering, summary indexing, and tags and collections |
| **AI enrichment** | The AI summary and AI topics, which need a language model provider |

Each chip shows its state and how many of its tasks are done. **Show task details** expands the
card to a per-tier list with timing, and while the card is live it names the tasks still running or
failed and lists what it is **Waiting on**.

### What the card state means

| State | Meaning |
|---|---|
| **Processing** | The main tier is running; the current step (for example *Speech to text* or *Speaker matching*) is shown |
| **Finishing up** | The transcript is ready, but at least one task the plan named is still queued or running |
| **Complete** | Every task the plan named has finished |
| **Needs attention** | A task failed; the card names it. Open the file or **Settings > System > File status** to retry |
| **Transcript ready** | The transcript exists, but the final steps could not be confirmed (**Final steps could not be confirmed**). It is never shown as Complete |

A card that has had no update for a while says so (**No update for a while**) rather than
claiming progress. A card that is still processing cannot be dismissed.

### Other notifications

Besides processing cards, the panel lists one-line notices such as a collection shared with you,
being added to or removed from a group, a search re-index or backup finishing, and (when it
happens) a content takedown or restoration of one of your files.

### Mark as read, clear, and how long items are kept

**Mark all notifications as read** and **Clear all notifications** are at the top of the panel;
each item also has a dismiss control. The panel is an inbox that prunes itself. The **Keep
notifications for** setting chooses how long a read item stays: **24 hours after reading**,
**7 days after reading** or **Never auto-clear**. Whatever you choose:

- Errors and warnings are never removed automatically.
- Items still processing are never removed.
- Unread items are kept for at least 14 days.
- A file's card is kept or removed as one unit: a failure anywhere on it keeps the whole card.

When items were cleared since you last looked, a note says **Older notifications cleared**. The
retention choice is stored in your browser, not on the server.

## The About dialog

Click the **OpenTranscribe logo** at the left of the navigation bar to open **About
OpenTranscribe**. The header shows two versions:

- **App build**: the version (and commit, when known) of the web app loaded in this browser tab.
- **Server**: the version the backend reports.

### Stale-build banner

After an upgrade, a tab that was left open keeps running the old web app against the new server.
When the two builds differ, the dialog shows a banner: *the app build in this tab doesn't match the
server. Reload the page to get the latest version.* Reload the page and the versions agree. The
banner appears only when both sides report a real difference: a development image that carries a
commit instead of a release number never produces a false warning.

### License and source

The dialog's **License and source** section states that OpenTranscribe is free software under the
GNU Affero General Public License, version 3, and, because you are using it over a network, links to
the complete source of the version that is running (the exact commit when the build recorded it, the
release otherwise, and the repository with a note when neither is known). The list of third-party
components and their licenses is in the `NOTICE` file it links to.

The rest of the dialog summarises key features, how processing works and the open-source projects
OpenTranscribe is built on.

## See also

- [Uploading Files](./uploading-files.md): what each processing step does and how to retry a failed file
- [Upgrading](../operations/upgrading.md): moving to a new version
