# src/lib/search — client-side fuzzy search (Settings only)

## Purpose

**This is the Settings search, not the media search.** Everything here powers the search box
inside `SettingsModal`. Media/transcript search is server-side hybrid keyword+semantic
(OpenSearch) and lives in `routes/search/+page.svelte` + `$components/search/` + `$stores/search.ts`
— see `frontend/src/components/search/CLAUDE.md`. Never rank, filter, or paginate those results here.

## Key files

- `settingsSearchIndex.ts` — builds the searchable corpus **from the flat i18n dictionary**
  (`i18next.getResourceBundle`), not from component source. That is why search works in all 8
  locales for free and picks up enumerated option labels built from template-literal keys.
- `fuzzyMatcher.ts` — thin fuse.js wrapper. The only custom part is NFD case/diacritic folding
  applied to both the indexed values and the query, so "vídeo" ⇄ "video".
- Each module ships a colocated `*.test.ts` (Vitest) that locks its behavior.

## Conventions / patterns

- Every module is **pure and side-effect free**: no stores, no DOM, no fetch. Callers own scroll,
  seek, flash-the-control, and navigation, and drive them from the returned data.
- `SECTION_NAMESPACES` maps each `SettingsSection` → its i18n key prefixes (sub-panel namespaces are
  folded into their parent section so results always land somewhere reachable). It is seeded by hand
  from `SettingsModal`'s `sidebarSections` and **nothing enforces the sync** — add a settings section
  or sub-panel without adding its namespace and every setting in it is silently unsearchable.
- `STOP_LEAVES` drops UI chrome leaves (save/cancel/retry/…) and `.toast.`/`.errors.` keys.
  `*Help|*Hint|*Description|…` leaves are folded into their base setting as extra `keywords`,
  not indexed as separate rows.
- Field weights live in `createSettingsFuzzyIndex`: `label` 3, `sectionLabel` 2, `keywords` 1.

## How it connects

- `SettingsModal.svelte` builds the index (only while the modal is open, rebuilt on `$locale` change)
  and passes it to `$components/settings/SettingsSearch.svelte`, which renders the ranked list.
- The modal passes only **capability/edition-filtered visible sections**, so a result can never point
  at a panel the user cannot open. Preserve that when changing the call site.

## Gotchas

- **Do NOT derive highlight ranges from fuse.js match indices.** They are computed against the
  accent-folded normalized string and misalign with the original display label. Highlight separately
  with `$lib/utils/searchHighlight`, and every `{@html}` must go through `sanitizeHighlightHtml`
  (`$lib/utils/sanitizeHtml`) — `SettingsSearch.svelte` does this.
- In-document find lives in `TranscriptSearch.svelte`'s `computeMatches` (plain-text, indexOf over
  loaded segment text + speaker labels) and `search/SearchTranscriptModal.svelte`'s
  `buildMatchPositions` (time-range: classifies keyword-vs-semantic matches by overlap against
  segment start/end times). These solve genuinely different problems — one indexes raw text
  offsets, the other resolves against timed transcript segments — and are not duplicates of each
  other. A third module (`findInText.ts`, literal Ctrl+F-style find) was extracted here but never
  wired to either caller and was deleted (H3). Do not add a generic `findInText`-style module
  without a concrete second consumer that actually needs it.
