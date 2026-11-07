---
sidebar_position: 12
title: Trash
description: Deleted files go to the trash, where they can be restored until their time ends and they are deleted forever.
---

# Trash

Deleting a file does not destroy it straight away. It moves to the **Trash**, where you can
bring it back until its time there ends. Only then is it deleted forever: the media, its
transcript, summaries, speaker data and its search and chat entries.

Open the trash from the **Trash** link in the gallery toolbar, beside the sort control.

## Deleting a file

Select files in the gallery (**Select Files**, tick them), then click **Delete N selected**. The
confirmation says how long the files stay recoverable, for example *Recoverable for 30 days,
then deleted forever.* Click **Move to trash**.

The files leave the gallery, search, chat, exports and every other view at once, for everyone
they were visible to. The message that confirms the move has an **Undo** button that brings them
straight back.

A file still being processed can be deleted too: its processing is cancelled. If you restore it
later it comes back as cancelled; reprocess it to finish the job.

## What the trash shows

| Column | Meaning |
|---|---|
| **File** | The file's title, or its file name |
| **Owner** | Shown when the list holds other people's files (an organization administrator's view) |
| **Deleted by** | Who moved it to the trash (**You** for your own deletes) |
| **Deleted on** | When it was moved to the trash |
| **Deleted forever** | How many days are left, **Being deleted now** once its time has ended, or **Kept while on hold** |

## Restoring a file

Click **Restore** on its row. The file returns to the gallery as it was, with its transcript,
tags, collections and shares. A file whose time has ended is already being deleted and can no
longer be restored.

## Deleting forever and emptying the trash

**Delete forever** on a row, or **Empty trash** at the top, deletes files permanently without
waiting for their time to end. Both ask for confirmation, and neither can be undone. Storage is
freed when a file is deleted forever, not when it is moved to the trash.

## Who can do what

| | Your own files | Files in an organization |
|---|---|---|
| Move to the trash, restore | The owner | The owner, or an organization administrator |
| See them in the trash | The owner | Members see their own; an organization administrator sees the organization's whole trash |
| Delete forever, empty the trash | The owner | An organization administrator only |

So a member's mistake in an organization always stays recoverable by an administrator until the
file's time ends. People a file is shared with cannot delete, restore or delete it forever.

## Holds and takedowns

- A file under a legal or organization hold cannot be moved to the trash.
- A hold placed on a file that is already in the trash keeps it there: it stays restorable and is
  never deleted forever while held, even after its date. Once the last hold is released, a file
  whose time has ended is deleted forever within the hour.
- A file under takedown review is hidden from members in the trash as everywhere else, and is
  kept until the takedown is released.

## How long files stay

30 days by default. An administrator sets it from 1 to 90 days in
**Settings → Data & Storage → File Retention** (the **Trash** block). A new length applies to
files deleted from then on; a file already in the trash keeps the date it was given.

Retention expiry (automatic deletion of old files, configured on the same tab) does **not** use
the trash: a file that reaches the retention age is deleted permanently, as configured. See
[File Retention](./admin-panel.md#file-retention--auto-deletion).

Erasing your account, or an erasure request, deletes files in the trash at once, without
waiting for their time to end.
