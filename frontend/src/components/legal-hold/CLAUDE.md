# frontend/src/components/legal-hold

The legal-hold UI shared by Settings, the gallery and the file page
(`docs/design/legal_hold_ux_freeze_plan.md` §10, Lane D). Logic lives in `$lib/legalHold/`.

## Key files

- `PlaceHoldDialog.svelte` — the ONE "Place legal hold" dialog. `fileUuids` fixes the target
  (gallery selection, file page); `null` lets the person pick a collection or paste file ids
  (Settings). `placer` (`'organization' | 'platform'`) picks the API through
  `holdsApiFor` (`$lib/legalHold/holdsApi`): `/org-admin/holds` or `/admin/holds`. Never add a
  second place form.
- `HoldExplainer.svelte` — "What does a legal hold do?" (`ExpandableSection`) plus an
  `InfoTip` with `holds.tooltip`. The tip sits BESIDE the disclosure, never in its header (a
  button inside a button). Used by the dialog, the panel and the banner. Takes no reason.
- `HeldFileBanner.svelte` — the file page's banner: who holds it (`HOLD_BANNER_KEYS`), never
  why. Its text has the fixed id `held-file-banner-text` that the title edit points at.
- `FrozenNote.svelte` — the visible reason printed beside a control a hold disabled. Pass a fixed
  `id` and set the disabled control's `aria-describedby` to it: a disabled button's tooltip is
  often never shown, so the tooltip alone does not count (the comment-form fix of 410ca7bce).
- `FilePlaceHoldAction.svelte` — the file header's "Place legal hold" button and dialog.
- `FrozenContextTestHost.svelte` — test double only (renders a child under the frozen context).

## Rules

- **Who sees place/release:** `holdAuthorityForCaller` (`$lib/legalHold/authority`) is the one
  decision. An org admin with `org.legal_hold` (managed edition) places organization holds; an
  instance admin places operator holds; everyone else gets `null` and no control. Release lives
  in Settings > Legal holds only (`settings/LegalHoldsPanel.svelte`, `scope` prop).
- **Frozen controls:** the file page calls `provideFileFrozen()` and sets it from
  `file.hold.held`; children read `fileFrozen()` (`$lib/legalHold/fileHoldContext`). Outside the
  page the store is always false. A frozen control is disabled, not hidden, with a `FrozenNote`.
  New comments stay enabled (`holds.commentHint`); existing comments are frozen.
- **A 409 after load:** catch blocks call `reportHoldRefusal(error)` (`$lib/legalHold/refusal`)
  before their own toast. It shows the translated refusal and publishes `held_by`, and the page
  (`onHoldRefused`) switches to the frozen view without a reload.
- **The computed "Legal hold" tag/collection** is `gallery/GalleryHeldFilter.svelte` and
  `collections/HeldCollectionCard.svelte`, both over `heldCount` (`$lib/legalHold/heldCount`,
  `held_count` from `/files/metadata-filters`), shown only above zero, driving `held=true`
  through `galleryStore.setHeldFilter`. Nothing to rename, share or delete.
