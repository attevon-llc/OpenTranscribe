# frontend/src/components/collections

## Purpose

Thin presentational children of `CollectionsPanel.svelte` (the coordinator), split out of that
file. They render owned-collection management, shared-collection browsing, and the create/edit form.

## Key files

- `CollectionsList.svelte` — list of the user's collections (exports a module-context `Collection`
  type); dispatches select/edit/delete intents.
- `SharedCollectionsSection.svelte` — collections shared with the user (`manage` vs `add` view modes).
- `CollectionFormModal.svelte` — **one parameterized modal** for both create and edit (driven by
  props: `idPrefix`, `title`, `submitLabel`, etc.); dispatches `submit`.

## Conventions / patterns

- Import via `$components/collections/...`; i18n via `$t`; share badges/attribution from
  `$components/sharing/*`.
- Children take props + `createEventDispatcher`; the panel owns all API calls and state.

## How it connects

- Parent/coordinator: `$components/CollectionsPanel.svelte`. It binds the form modal twice (once
  for `createCollection`, once for `updateCollection`) with different props.

## Gotchas

- **Visibility (#1275).** `CollectionFormModal` takes `showVisibility` + `bind:visibility` (an organization
  collection the caller may restrict: always on create inside an organization, and on edit only when the server
  says `can_change_visibility`). The panel saves it through `CollectionsApi.setVisibility` (`PATCH
/collections/{uuid}/visibility`), never through `PUT`. `CollectionsList` marks `visibility === 'restricted'`
  with the lock badge; `ShareCollectionModal` takes `restricted` for its explainer; the
  `collection_access_changed` WebSocket event (uuid only) drops the row.
- Don't fork `CollectionFormModal` into separate create/edit components — it is intentionally one
  parameterized modal. Add behavior via props, not by duplicating the file.
