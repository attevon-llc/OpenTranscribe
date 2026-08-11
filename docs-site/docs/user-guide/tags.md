---
sidebar_position: 9
---

# Tags

Tags are free-form labels on your media. Where a [collection](./collections.md) is a
place you put a file, a tag is a word you attach to it — a file can carry as many as
you like, and filtering by one narrows the gallery immediately.

## Opening tag management

Tags live behind the **Tags** button in the gallery toolbar, next to **Collections**.
There is no separate page: a tag is metadata about your library, not a place to
navigate to.

What opens depends on what you have selected:

| Selection | What opens | What you can do |
|---|---|---|
| Nothing | The **tag manager** | Create, rename, merge, delete, share |
| One file | The **tag editor** | Add tags, and remove them as chips |
| Several files | **Bulk apply** | Add a tag to all of them |

With files selected, Tags also appears in the **Organize** menu beside
*Add to collection* — both attach metadata to a file.

### Why several files is add-only

With one file selected you see its tags as chips and can remove any of them. With
several, the chips are read-only and show their coverage instead — `on 3 of 5`.
Removing a tag that sits on three of five files has no single obvious meaning, while
adding one does. Long lists are capped, with `and N more tags` carrying the rest.

## Creating and applying tags

Type a name in the manager's **New tag** field, or in the editor when files are
selected. Names resolve to an existing tag whenever one matches: matching ignores
case, hyphens, underscores and repeated spaces, so `Q3 Review`, `q3-review` and
`q3_review` are all the same tag rather than three near-duplicates.

A *near* match is never applied automatically. Typing `q4-earnings` when `q3-earnings`
exists creates a new tag, because nothing can split two tags apart once they are
combined. The auto-labeler is the one exception — it may attach a close match on its
own, and those tags show **Added by AI** in the Origin column.

## Finding a tag

The manager's tools combine:

- **Search** narrows the list as you type.
- **Sort** by most-used or by name.
- **Views** — All, Unused (no file you can see carries it), and Collisions (tags that
  normalize to the same name, grouped with a suggested survivor).
- **Tag ownership** filters by who the tag belongs to (below).

## Ownership: who can change a tag

Every tag reports one of three states, which is also what the **Tag ownership** filter
selects on:

| State | What it means | What you can do |
|---|---|---|
| *(unmarked)* — **Mine** | You created it | Rename, merge, delete, share |
| **Shared** | Part of the shared vocabulary every account sees | Filter and apply; only an admin can change it |
| **Shared with me** | Belongs to someone else | Filter and apply; only they can change it |

Tag names are unique **per person**, so you and a colleague can each have an
`interview` tag without colliding. Where a tag is not yours to change, the manager
does not offer Rename or Delete — the badge explains why.

## Sharing a tag

**Share…** gives a tag to specific people or groups. They can see it, filter by it and
apply it — so they use your word instead of coining a duplicate — while renaming,
merging and deleting stay with you. Revoking a share leaves every file tagged; only
reaching the tag by name in the picker goes away.

**Share with everyone** (admin only) publishes a tag into the shared vocabulary and
folds other people's identically-named tags into it, so the deployment converges on
one `Interview` instead of accumulating one per person. Their files keep the tag.

Every install also seeds a small shared vocabulary — `Important`, `Meeting`,
`Interview`, `Personal` — and typing one of those names attaches the shared tag rather
than forking a private copy.

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
tag rather than creating a second one, so a shared file never carries the same word
twice.

## Renaming, merging and deleting

Select a tag to see what it touches: its usage, its origin, and the files carrying it,
each clickable through to the file.

Rename, merge and delete each show what they would touch **before** they act,
including a count of files beyond the ones you can see — a shared tag can reach
further than your own library. Deletes use the same confirmation dialog as the rest of
the app.

Renaming to a name that already belongs to another tag is a **merge**, and says so
before applying anything. Merging is the least reversible action here, so when you
merge a collision cluster the surviving name is always chosen explicitly.

## Tags and search

Filtering by a tag and searching for it return the same files. If they ever disagree
after a large merge, the search index refresh is still catching up; it converges on
its own.

## Related

- [Collections](./collections.md) — grouping files rather than labeling them
- [Search and Filters](./search-and-filters.md) — combining tags with other filters
- [Uploading Files](./uploading-files.md#organizing-during-upload) — tagging at upload time
