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

- Don't fork `CollectionFormModal` into separate create/edit components — it is intentionally one
  parameterized modal. Add behavior via props, not by duplicating the file.
