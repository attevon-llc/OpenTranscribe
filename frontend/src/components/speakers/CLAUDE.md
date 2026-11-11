# frontend/src/components/speakers

## Purpose

Thin presentational children of `routes/speakers/+page.svelte` (the coordinator), split out of
that page. They render the cluster / profile / inbox management UI for cross-file speaker ID.

## Key files

- `ClustersSection.svelte` — everything the Clusters tab does (list, merge / split / unassign /
  promote / delete, reclustering and its websocket progress); stays mounted across tab switches so
  search / page / expanded-card state survives, rendering the tab only while `active`.
  `ProfilesSection.svelte` — the profile edit / delete / avatar / gender flows (the page owns the
  profile loading). `SpeakerPromoteModal.svelte` — the inline-name promote dialog. Their rules are
  in `$lib/speakers/` (see `src/lib/speakers/CLAUDE.md`).
- `ClustersTab.svelte` — clusters view (identified/unidentified split); hosts cards + members.
- `SpeakerClusterCard.svelte` — one cluster card (expand, label edit, actions). Its header row is a
  real `<button aria-expanded>` (WAI-ARIA APG disclosure) holding only text, so nothing focusable
  may go inside it (axe `nested-interactive`, guarded by `nestedInteractive.convention.test.ts`):
  the label `<input>` replaces the button while editing, and the keyboard's way into the editor is
  a sibling "Edit label" button that is `.sr-only` until focused (double-click on the name stays
  the pointer's way). `ClusterHeaderChips.svelte` renders the row's chips in both shapes.
- `ClusterMemberList.svelte` — members of a cluster; outlier analysis, split/unassign selection.
- `ProfilesTab.svelte` — saved speaker profiles (avatar upload, rename, gender confirm).
- `InboxTab.svelte` / `SpeakerInboxItem.svelte` — suggestion inbox with confidence coloring.
- `SpeakerPreviewPlayer.svelte` — clip preview in the floating mini player (`player/MiniPlayer`, dynamic browser-only import).
- `GenderBadge.svelte` — icon + translated word for a predicted gender (issue #756), consumed
  by `ClusterMemberList` (its two identical inline blocks before #756), `SpeakerClusterCard`,
  and `SpeakerInboxItem`. Takes a loosely-typed `gender: string | null | undefined` on purpose
  — the wire type is `string | null`, not a `'male'|'female'` union — and its own
  `=== 'male' | 'female'` guard is what makes an unrecognised value (a future `'unknown'`,
  `'other'`, or empty string) render NOTHING instead of defaulting to "female". Every one of
  the four surfaces this replaces had that bug independently before this component existed.
  `SpeakerEditorPanel.svelte` (the per-file editor, `components/transcript/`) has its own
  near-identical inline copy and is NOT wired to this component — its own outer guard
  (`predicted_gender && !== 'unknown'`) makes its binary `{:else}` exhaustively safe already,
  so leaving it as the one hand-rolled instance was a deliberate choice, not an oversight.

- `components/privacy/SpeakerPrivacyNotice.svelte` is mounted on the speakers page and in the per-file
  `SpeakerEditorPanel` (#1280). It explains an empty suggestion/cluster view (voiceprint matching off, and
  by which layer) and says gender is inferred automatically from voice, with a link to the switch, only
  while inference is on. The SERVER hides the data (empty lists, null inferred gender); this only explains
  it, and a failed read renders nothing. `GenderBadge`'s tooltip says "inferred automatically from voice"
  for an unconfirmed gender.

## Conventions / patterns

- Import via `$components`; types from `$lib/types/speakerCluster`; i18n via `$t`.
- Children take props + `createEventDispatcher`. The page owns the tab bar and the loading of the lists it hands down; `ClustersSection` and `ProfilesSection` own their own mutation API calls and flow state.
- Skeletons/empties come from `ui/` (ListRowSkeleton, CardGridSkeleton, EmptyState).

## How it connects

- Parent/coordinator: `src/routes/speakers/+page.svelte`.
- API: `$lib/api/speakerClusters`. Stores: `$stores/audioPlaybackStore`, `$stores/locale`.

## Gotchas

- `SpeakerPreviewPlayer` dynamically imports `FloatingPreviewPlayer` only when `browser` is
  true: this route has no `ssr = false`, and the player needs the DOM. Keep the guard until a
  refresh of `/speakers` is verified without it (the Video.js modules themselves import cleanly
  in Node, issue #1327).
- Suggestions are never auto-applied — the inbox surfaces them for manual verification only.
- **Gender i18n: `speakers.member.male`/`female` is the ONE namespace (issue #756 J8).**
  `speaker.genderMale`/`genderFemale` (used only by `SpeakerEditorPanel`) held the identical
  English text under a second key and was deleted; `SpeakerEditorPanel` migrated onto
  `speakers.member.*`. Don't reintroduce a second gender-word namespace.
- **"Review" (the inbox tab label) is a i18n VALUE change only — the key and the `Tab` union
  value are both still `'inbox'`** (issue #756). `/speakers?tab=inbox` is a real bookmarkable
  URL; renaming the union value would silently drop it to the Clusters tab.
- **E2E-guarded selectors owned here** (`backend/tests/e2e/test_speaker_gender_clusters.py`):
  `.cluster-card` and `.card-header` (click to expand a cluster) in `SpeakerClusterCard.svelte`;
  `.gender-chip` (`.gender-chip.gender-coherent` / `.gender-chip.gender-conflict`) is rendered
  by its `ClusterHeaderChips.svelte`. `.member-row` (`.member-row.gender-outlier`) and
  `.gender-icon` are `ClusterMemberList.svelte` — the test clicks `.card-header` first, since
  members don't exist in the DOM until the cluster is expanded. `.profile-card` and
  `.gender-toggle-btn` (`.gender-toggle-btn.active` once confirmed) are `ProfilesTab.svelte` —
  this toggle is **profile**-scoped, a different confirmation than the cluster card's
  `.gender-chip` badge, so don't conflate the two when renaming. Renaming any of these breaks
  that suite.
