# frontend/src/components/search

## Purpose

Thin presentational children of `routes/search/+page.svelte` (the hybrid keyword+semantic search
coordinator).

## Key files

- `SearchResultsBody.svelte` — the results area once a query has run: the Summaries and
  Transcripts sections (collapsible `ui/ExpandableSection` with `headingLevel={2}`; the
  Transcripts toggle exists only when both sections show) and, when the page passes
  `panelMode`, the sticky knowledge-panel `<aside>` beside a 44rem results column. The panel
  shows the pinned hit, else the hovered/focused one, else the top result. The page decides
  `panelMode` from viewport width (1440px with the filter sidebar open, 1180px collapsed).
  Design and rationale: `docs/design/search_results_ux_audit.md`.
- `SearchResultCard.svelte` — one file's hit as a compact search-engine result: title, one
  muted meta line (date, duration, relevance), 1 snippet (2 when `has_both_match_types`),
  "Show N more matches" and "Details" disclosures. Speakers, tags, size and match sources are
  NOT in the card; they live in `SearchResultInfoCard.svelte` (`panel` in the aside,
  `inline` inside the card on narrow layouts). Dispatches `preview` (start a clip),
  `togglePlayback` (pause/resume the clip already loaded), `viewTranscript`, `inspect`
  (hover/focus) and `toggleDetails` (pin). No API calls.
- `SearchPreviewButton.svelte` — the per-occurrence play control, state
  `idle | playing | paused`. `activePreview.playing` follows the floating player's Plyr
  `play`/`pause` events (`ended` reports `pause`), so the glyph never claims "playing" for a
  paused clip. Primary colour when active, never `--error-color`.
- `SearchOccurrence.svelte` — one snippet row; renders the backend's `<mark>` /
  `<mark class="semantic">` highlight markup, so its `mark` styling must stay `:global`.
  The card title is a block of text, not a flex row: as `inline-flex` each title `<mark>` was
  its own flex item and a narrow viewport split a word across two of them. Type rules:
  `frontend/CLAUDE.md` "Type and weight rules".
- `SearchAutocomplete.svelte` — the query input: `GET /search/suggestions` debounced 200 ms with
  AbortController cancellation and arrow/Enter/Escape nav. A `title`-type suggestion `goto`s
  `/files/<uuid>` directly instead of dispatching `select`.
- `SearchTranscriptModal.svelte` was **deleted in issue #755**. "View transcript" now opens
  `$components/transcript/TranscriptViewModal.svelte` (`mode="search"`) — the same consolidated
  modal the file-detail page uses for its own "View transcript" button (`mode="file"`, the
  default). It self-fetches 200 segments at a time (issue #755 J3 — a self-contained fetch, the
  search page owns no pagination for it), classifies keyword vs semantic by **time-range
  overlap** with the hit's occurrences via `$lib/transcript/matchClassification`
  (`classifySegments`/`countByType`), and renders through `TranscriptSegmentList` in read-only
  (`editable=false`) mode rather than a second hand-rolled renderer. Prev/next walks the
  occurrence list and scrolls; there is no third highlighter or third reading-progress bar.
- Sort control is `$components/ui/SortDropdown.svelte` (shared with gallery, H2), consumed
  directly by `routes/search/+page.svelte` with `relevance` as a `noDirection` option (always desc).
- `SearchPagination.svelte` — windowed pager (1–5, then current ±2). **Also used by
  `$components/fileStatus/TasksGrid.svelte`** — keep it search-agnostic.

## Conventions / patterns

- Children take props + `createEventDispatcher`; the page owns the `/search` fetch, `searchStore`,
  URL sync, and the floating preview player. i18n via `$t`; import via `$components/search/...`.
- **State is URL-driven**: the page mirrors `q`/`page`/`sort`/`sort_order`/`mode`/`speakers`/`tags`
  into `URLSearchParams` and re-queries. Filtering, sorting, and paging are server-side — never
  filter or sort the results array here.
- **Every `{@html}` goes through `sanitizeHighlightHtml`** (`$lib/utils/sanitizeHtml`, DOMPurify;
  allows `mark`/`span` + `class`). Snippets, titles, and speaker names are all backend-derived.

## How it connects

- Parent/coordinator: `src/routes/search/+page.svelte`. Store + types: `$stores/search`. Prefetch:
  `$lib/prefetch`. Backend: `GET /search`, `/search/suggestions`, `/files/{uuid}` (paged segments).

## Gotchas

- **E2E-guarded selectors owned here** (`backend/tests/e2e/test_search.py`): `.search-input` and
  `.clear-btn` in `SearchAutocomplete`. The card's root (`article.result-card`, with `.result-title` and `.view-transcript-btn` always visible) must also stay a direct child of `SearchResultsBody`'s
  `.results-list` — the test counts `.results-list > *`. That is why the page's
  `RetrievalQualityNotice` (#461) sits in `.quality-notice-slot` **above** `.results-list` rather
  than inside it; it is also gated to `searchMode === 'hybrid'`, since Exact mode is literal BM25
  and none of the fusion ranking #461 measured applies to it.
- `TranscriptViewModal` (search mode) re-fetches on the redaction toggle (`?redact=false`,
  owner-only) and rebuilds the loaded range **in place** — don't flip the loading state there
  or the view flickers.
- The card and info card format through `$lib/utils/formatting` (`formatClock`, `formatDate`,
  `formatLanguageNames`) and `$lib/utils/metadataMapper`'s `formatFileSize`; the old local
  copies are gone, don't reintroduce them.
- The info `<aside>` and every notice stay OUTSIDE `.results-list` (see the E2E note above).
- The page marks a clicked occurrence active before its stream URL resolves, and drops a URL
  fetch superseded by a newer click (`previewRequest`), so two cards never both show "pause".
