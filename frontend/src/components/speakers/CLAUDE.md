# frontend/src/components/speakers

## Purpose

Thin presentational children of `routes/speakers/+page.svelte` (the coordinator), split out of
that page. They render the cluster / profile / inbox management UI for cross-file speaker ID.

## Key files

- `ClustersTab.svelte` — clusters view (identified/unidentified split); hosts cards + members.
- `SpeakerClusterCard.svelte` — one cluster card (expand, gender-conflict badge, actions).
- `ClusterMemberList.svelte` — members of a cluster; outlier analysis, split/unassign selection.
- `ProfilesTab.svelte` — saved speaker profiles (avatar upload, rename, gender confirm).
- `InboxTab.svelte` / `SpeakerInboxItem.svelte` — suggestion inbox with confidence coloring.
- `SpeakerPreviewPlayer.svelte` — Plyr mini-player for clip preview (dynamic browser-only import).

## Conventions / patterns

- Import via `$components`; types from `$lib/types/speakerCluster`; i18n via `$t`.
- Children take props + `createEventDispatcher`; the page owns all loading, API calls, and state.
- Skeletons/empties come from `ui/` (ListRowSkeleton, CardGridSkeleton, EmptyState).

## How it connects

- Parent/coordinator: `src/routes/speakers/+page.svelte`.
- API: `$lib/api/speakerClusters`. Stores: `$stores/audioPlaybackStore`, `$stores/locale`.

## Gotchas

- `SpeakerPreviewPlayer` dynamically imports `PlyrMiniPlayer` only when `browser` is true —
  Plyr breaks SSR/hydration on refresh; keep the guard.
- Suggestions are never auto-applied — the inbox surfaces them for manual verification only.
- **E2E-guarded selectors owned here** (`backend/tests/e2e/test_speaker_gender_clusters.py`):
  `.cluster-card` and `.card-header` (click to expand a cluster) in `SpeakerClusterCard.svelte`,
  which also renders `.gender-chip` (`.gender-chip.gender-coherent` /
  `.gender-chip.gender-conflict`). `.member-row` (`.member-row.gender-outlier`) and
  `.gender-icon` are `ClusterMemberList.svelte` — the test clicks `.card-header` first, since
  members don't exist in the DOM until the cluster is expanded. `.profile-card` and
  `.gender-toggle-btn` (`.gender-toggle-btn.active` once confirmed) are `ProfilesTab.svelte` —
  this toggle is **profile**-scoped, a different confirmation than the cluster card's
  `.gender-chip` badge, so don't conflate the two when renaming. Renaming any of these breaks
  that suite.
