# frontend/src/components/tags — the tag manager

## Purpose

The tag library surface: list, filter, rename, merge, delete, review auto-labeled
tags, and promote one into the shared vocabulary. Bulk *apply* across a gallery
selection is not here — that is `gallery/BulkTagModal.svelte`.

## Key files

- `TagManagerModal.svelte` — the coordinator, and the only component here that
  fetches. Owns the selection, the mutation lifecycle and every API call; the
  rest are presentational and dispatch intent up. Opened from the gallery's
  **Tags** button when nothing is selected (a selection opens the bulk flow
  instead), mirroring `CollectionsPanel`'s `viewMode`.
- `TagList.svelte` — the listbox, in flat or collision-cluster mode. Exports
  `TagListEntry`, `TagSelectDetail` and `tagOriginKey` from its module script.
  Owns roving tabindex, Shift-range and cluster-group selection.
- `TagDetailPanel.svelte` — one selected tag. `TagBulkSummary.svelte` — several,
  including the survivor picker that fronts a merge.
- `TagFilterBar.svelte` — the four view tabs plus the ownership scope picker.

## Conventions / patterns

- **Nothing here recomputes what the backend ships.** Usage counts, collision
  clustering, ranked near matches, the suggested survivor and `awaiting_review`
  all arrive decided and are rendered as received.
- The coordinator owns state; children take props and `dispatch`. Don't move
  fetches down into a child.

## Gotchas

- **Three ownership values, not a boolean** (`$lib/types/tag`): `mine`,
  `system` (the shared vocabulary), `shared_with_me` (someone else's, visible
  only because they shared the media it sits on). `canMutateTag()` mirrors the
  backend's `_writable_tag_ids` — a `shared_with_me` tag answers **404** to every
  mutation, so the UI must not render Rename/Delete/Accept for it. The badge
  explains the absence instead.
- **`is_shared` means the opposite elsewhere in this app** (`CollectionWithCount`
  uses it for "shared *with* me"). That collision is why tags carry `ownership`
  rather than a boolean — don't reintroduce one.
- **The scope picker's values ARE the ownership values.** `GET /tags?scope=` takes
  the same four strings, so a scoped request returns rows reporting that
  ownership. Renaming one side without the other silently breaks the filter;
  `TagManagerModal.test.ts` pins the pairing.
- **Cluster members must carry `ownership` through `buildModel`**, or the same
  tag is gated one way in the flat list and another in cluster view.
- **A typed name may not be the applied tag** — resolution is normalized-exact,
  so `Q3 Review` can land on `q3-review`. Surfaces that report success name the
  tag that was actually applied, never the string the user typed.
- Promotion is admin-only and only ever offered for an `ownership === 'mine'`
  tag: a system tag is already shared, and a `shared_with_me` one is not yours
  to publish.
