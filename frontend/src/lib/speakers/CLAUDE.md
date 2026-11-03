# src/lib/speakers — speakers page and speaker-menu logic

## Purpose

Pure rules and small stateful helpers behind `routes/speakers/+page.svelte`, its section components
(`components/speakers/ClustersSection.svelte`, `ProfilesSection.svelte`) and the per-segment speaker
menu (`components/SegmentSpeakerDropdown.svelte`).

## Key files

- `pageHelpers.ts` — `CLUSTER_PAGE_SIZE`, page clamping (clamp BEFORE reloading after a removal),
  collapse-state persistence, `avatarFileError`, member toggling.
- `loaders.ts` — `fetchClusterPage` and friends; the default first page may be consumed from the
  route prefetch (`apiCache`).
- `inboxActions.ts` — `performInboxAction`: a per-speaker Inbox action is the user's explicit
  decision, so accept/assign override an earlier unassignment (#1233); a refusal arrives as
  `failed_count` and is thrown, never read as success.
- `previewCache.ts` (short-lived hover/play preview cache), `renamePropagation.ts` (tracks renames
  whose digest regeneration is still running server-side: optimistic `begin()`, `settle()` on the
  completion frame or a timeout).
- `speakerMenu.ts` (pure rules: what the trigger shows, whether a typed name may be saved, which
  profile names to suggest, list order/labels) and `speakerMenuDom.ts` (DOM builders for the
  `document.body` portal menu; every string goes in via `textContent`/attributes, never `innerHTML`).
  `SegmentNewSpeakerModal.svelte` is the create-speaker step.

## Conventions / patterns

- Pages and section components own loading and state; these modules hold rules, not stores of their
  own (the tracker and cache are factories).
