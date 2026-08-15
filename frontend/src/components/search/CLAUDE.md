# frontend/src/components/search

## Purpose

Thin presentational children of `routes/search/+page.svelte` (the hybrid keyword+semantic search
coordinator), plus the full-transcript match browser opened from a result card.

## Key files

- `SearchResultCard.svelte` — one file's hit: title/meta/match badges + occurrences (3 visible when
  `has_both_match_types`, else 2). Dispatches `preview` (a toggle — re-clicking the active
  occurrence emits `null`) and `viewTranscript`. No API calls.
- `SearchOccurrence.svelte` — one snippet row; renders the backend's `<mark>` /
  `<mark class="semantic">` highlight markup, so its `mark` styling must stay `:global`.
- `SearchAutocomplete.svelte` — the query input: `GET /search/suggestions` debounced 200 ms with
  AbortController cancellation and arrow/Enter/Escape nav. A `title`-type suggestion `goto`s
  `/files/<uuid>` directly instead of dispatching `select`.
- `SearchTranscriptModal.svelte` — ~1.4k lines: loads the transcript 200 segments at a time, groups
  consecutive same-speaker segments, classifies keyword vs semantic by **time-range overlap** with
  the hit's occurrences, and drives prev/next match (Enter / Shift+Enter), paging in more segments
  until the target match resolves.
- `SearchSortDropdown.svelte` — sort field + direction; `relevance` is `noDirection` (always desc).
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
  `.clear-btn` in `SearchAutocomplete`. The card's root must also stay a direct child of the page's
  `.results-list` — the test counts `.results-list > *`. That is why the page's
  `RetrievalQualityNotice` (#461) sits in `.quality-notice-slot` **above** `.results-list` rather
  than inside it; it is also gated to `searchMode === 'hybrid'`, since Exact mode is literal BM25
  and none of the fusion ranking #461 measured applies to it.
- `SearchTranscriptModal` re-fetches on the redaction toggle (`?redact=false`, owner-only) and
  rebuilds the loaded range **in place** — don't flip `loading` there or the view flickers.
- `SearchResultCard` re-implements `formatDuration`/`formatFileSize`/`formatDate` locally instead of
  using `$lib/utils/formatting` — don't copy that into new cards.
- `SearchSortDropdown` hand-rolls its menu (with `$lib/actions/clickOutside`) rather than using
  `$components/ui/Dropdown.svelte`.
