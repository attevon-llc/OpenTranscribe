# frontend/src/components/sharing

## Purpose

Collection sharing: the share modal plus the small read-only chips that other features reuse to
show a collection is shared and by whom.

## Key files

- `ShareCollectionModal.svelte` — the coordinator: loads `/collections/{uuid}/shares`, stages
  `pendingTargets` (default permission `viewer`), then shares them **one request per target in a
  loop**, accumulating per-target errors.
- `ShareTargetSearch.svelte` — combined user+group picker: users via `GroupsApi.searchUsers`
  (debounced 300 ms, min 2 chars); groups fetched **once** and cached in-component, then filtered
  client-side. Already-shared and pending targets are excluded via `existingShareTargets`.
- `CurrentSharesList.svelte` — existing shares with permission change + revoke-with-confirmation;
  every mutation writes through `sharingStore` so the list stays in sync.
- `PermissionLevelSelect.svelte` — the viewer/editor `<select>`; the only place the permission
  vocabulary lives. Add a level here, not inline.
- `ShareBadge.svelte` / `SharedByAttribution.svelte` — presentational chips consumed by
  `$components/collections/*` (`CollectionsList`, `SharedCollectionsSection`).

## Conventions / patterns

- Import via `$components/sharing/...`; i18n via `$t`. `sharingStore` is the source of truth for the
  open collection's shares — update it (`addShare` / `updateSharePermission` / `removeShare`) right
  after the API call rather than refetching.

## How it connects

- Opened from `$components/CollectionsPanel.svelte`. API: `$lib/api/sharing` (plus
  `$lib/api/groups` for target search). Types: `$lib/types/groups`. Store: `$stores/sharing`.

## Gotchas

- **`canManage` is a caller-supplied prop** (issue #583) — `ShareCollectionModal` no longer
  hardcodes it. `CollectionsPanel.openShareModal` derives it from
  `collection.my_permission === 'owner'` (backend already sends `my_permission` on
  `CollectionWithCount`, mirroring `SharedCollection`). The real rule is
  `_require_collection_owner` in `backend/app/api/endpoints/media_collections.py`: only the
  **direct owner** may list, create, update, or revoke shares, and an `editor` share does **not**
  confer re-sharing — the frontend gate is UX only, the backend still 403s any mutating call from
  a non-owner. Note the "add share" search section above `CurrentSharesList` in the modal is
  **not yet gated** by `canManage`; there is currently no live UI path that opens the modal for a
  non-owned collection (only the owned-collections list wires up the share button), so this is
  latent, not reachable today.
- Sharing N targets is N sequential requests, so **partial success is normal** — the modal reports
  successes and per-target failures separately. Don't collapse the loop into one aborting await.
- No Playwright test selects into this folder — there are no E2E-guarded class names here.
