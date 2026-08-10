---
sidebar_position: 9
---

# Tags

Tags are free-form labels on your media. Where a [collection](./collections.md) is a
place you put a file, a tag is a word you attach to it — a file can carry as many as
you like, and filtering by one narrows the gallery immediately.

## Opening tag management

Tags live behind the **Tags** button in the gallery toolbar, next to **Collections**.
There is no separate page: a tag is metadata about your library rather than a place to
navigate to.

The button does one of two things depending on what you have selected:

| Selection | What opens |
|---|---|
| Nothing selected | The **tag manager** — rename, merge, delete, review |
| One or more files | The **bulk apply** flow — add or remove a tag across the selection |

You can also reach the bulk flow from **Organize → Add tag / Remove tag**.

## Applying tags

Type a name and it resolves to an existing tag whenever one matches. Matching ignores
case, hyphens, underscores and repeated spaces, so `Q3 Review`, `q3-review` and
`q3_review` are all the same tag rather than three near-duplicates.

A *near* match is never applied automatically. Typing `q4-earnings` when `q3-earnings`
exists creates a new tag, because nothing can split two tags apart once they are
combined. The only exception is the auto-labeler, which may attach a close match on its
own — and those are flagged for review (below).

## Ownership: who can change a tag

Every tag shows one of three states, which is also what the manager's **Tag ownership**
filter selects on:

| State | What it means | What you can do |
|---|---|---|
| *(unmarked)* — **Mine** | You created it | Rename, merge, delete, share |
| **Shared** | Part of the shared vocabulary every account sees | Filter and apply; only an admin can change it |
| **Shared with me** | Belongs to someone who shared media with you | Filter and apply; only they can change it |

Tag names are unique **per person**, so you and a colleague can each have an
`interview` tag without colliding. Where a tag is not yours to change, the manager
simply does not offer Rename or Delete — the badge explains why.

## Tags on shared media

**Tags travel with the media they are on.** Share a collection and its files' tags
become visible to everyone you shared with: on the file's detail page, in their tag
picker, in the gallery filter, and in search.

Nothing is copied. Visibility is worked out from the file each time, which means:

- Tagging a shared file later shows up for recipients immediately.
- Unsharing removes those tags from their list again, with no cleanup step.
- Usage counts only ever count files *you* can see, so a shared tag never reveals how
  much its owner uses it elsewhere.

A recipient tagging the same file with a word already on it attaches the **existing**
tag rather than creating a second one, so a shared file never ends up carrying the same
word twice.

### The shared vocabulary

Every install seeds a few shared tags (`Important`, `Meeting`, `Interview`, `Personal`).
These are visible to everyone, and typing one of those names attaches the shared tag
instead of forking a private copy.

Admins can promote any tag into that shared vocabulary with **Share with everyone**.
Promotion also folds other people's identically-named tags into the promoted one, so a
deployment converges on a single `Interview` rather than accumulating one per person.
Files keep the tag either way.

## Managing the library

With nothing selected, the manager offers four views, which combine with the ownership
filter:

- **All** — everything you can see.
- **Awaiting review** — tags the auto-labeler created that nobody has confirmed.
  **Accept** endorses one; **Reject** removes only the associations the AI added, so a
  tag you have also applied by hand survives with that work intact.
- **Unused** — tags no file you can see is carrying.
- **Collisions** — tags that normalize to the same name, grouped with a suggested
  survivor. Merging is the least reversible action here, so the surviving name is always
  chosen explicitly.

Rename, merge and delete each show what they would touch **before** they act, including
a count of files beyond the ones you can see — a shared tag can reach further than your
own library.

## Tags and search

Filtering by a tag and searching for it return the same files. If they ever disagree
after a large merge, the search index refresh is still catching up; it converges on its
own.

## Related

- [Collections](./collections.md) — grouping files rather than labeling them
- [Search and Filters](./search-and-filters.md) — combining tags with other filters
- [Uploading Files](./uploading-files.md#organizing-during-upload) — tagging at upload time
