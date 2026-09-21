# frontend/src/components/transcript

## Purpose

Thin presentational children of `TranscriptDisplay.svelte` (the coordinator), split out of
that oversized file. They render the transcript editing/export UI and dispatch intent back up.

## Key files

- `SpeakerEditorPanel.svelte` — edit/merge speaker labels; emits speaker-rename/merge events.
- `TranscriptActionsBar.svelte` — export + download dropdowns; dispatches `export` / `download`
  (the parent owns the actual logic).
- `TranscriptSegmentList.svelte` — the scrollable segment list: playback sync, inline text
  edit, search highlighting, infinite-scroll pagination sentinel. Two IntersectionObservers:
  one on the pagination sentinel, one over `[data-seg-index]` for the reading-progress bar.
  **`editable` (default `false`, issue #755)** gates the edit button and the speaker dropdown's
  interactivity (`SegmentSpeakerDropdown`'s own `readOnly` prop) — the live file-detail
  transcript passes `editable={true}`; the consolidated "view transcript" modal does not, so a
  viewer-only user opening it never sees an edit affordance they cannot use. An optional
  `segmentClassification: Record<uuid, 'keyword'|'semantic'>` prop (from
  `$lib/transcript/matchClassification`) additively swaps in the search-result classification
  highlight (`.search-keyword-match`/`.search-semantic-segment`, styled in
  `src/styles/search.css`) for a given segment instead of the ordinary query-driven
  `highlightTextWithMatches` — the two never run for the same segment.
- `TranscriptViewModal.svelte` — the consolidated "view transcript" modal (issue #755).
  Replaces both the old `components/TranscriptModal.svelte` (file-detail) and
  `components/search/SearchTranscriptModal.svelte` (search-result), which is why it takes a
  `mode: 'file' | 'search'` prop: `file` mode renders data the page already owns and dispatches
  `loadMore`/`toggleRedaction` up; `search` mode self-fetches its own segment window (J3 — no
  second page-level pager) and classifies them against a server-supplied `occurrences` prop.
  Both modes render through `TranscriptSegmentList` with `editable={false}`.

## Conventions / patterns

- Import via `$components`, i18n via `$t` from `$stores/locale`; speaker labels through
  `translateSpeakerLabel` (`$lib/i18n`).
- Children take props + `createEventDispatcher` — no API calls, no stores-as-state.
- **Coordinator keeps the heavy logic**: `TranscriptDisplay.svelte` owns the download SSE
  (`EventSource` + `downloadStore`), `handleSegmentSpeakerChange` (calls `updateSegmentSpeaker`),
  segment-group resolution, and bridges child `export`/`download` events to its own handlers.
  It takes **`bind:file`** — `handleSegmentSpeakerChange` writes back through
  `$lib/fileDetail/segmentSync`, which returns a new file object rather than mutating in place.
- **Grouping comes from the backend and references segments by uuid.**
  `TranscriptDisplay.mapBackendGroup` resolves `grouped_segments[].segment_uuids` against
  `file.transcript_segments` — the single copy of segment data (#352). There is deliberately no
  client-side grouping fallback any more; don't re-add one.

## How it connects

- Parent: `$components/TranscriptDisplay.svelte`, rendered on `/files/[id]`.
- Stores: `$stores/downloads`, `$stores/toast`, `$stores/locale`. API: `$lib/api/transcripts`.

## Gotchas

- `:global(.highlight-flash)` (playback flash) and `.segment-text :global(span.redacted)`
  (redaction masking style) live in **TranscriptSegmentList.svelte** — redaction text is
  injected as HTML, so these must stay `:global`.
- Don't relocate download/SSE or `handleSegmentSpeakerChange` into children — they belong to
  the coordinator so state stays single-sourced.
- **The segment list is virtualized by CSS (`content-visibility: auto`), not by JS windowing.**
  Don't swap in `$components/gallery/VirtualList.svelte`: it slices a fixed-44px row list, whereas
  segments are variable height (wrapped text, multi-segment overlap groups, the expanded edit
  textarea), and evicting off-screen rows breaks everything that reaches a segment through
  `document.querySelector('[data-segment-id=…]')` — search scroll-to, seek-to-playhead,
  `SpeakerEditorPanel`'s jump, and `.highlight-flash`.
- **No `on:scroll` handler on `.transcript-display`.** Reading progress comes from the
  IntersectionObserver; a scroll handler here previously ran an O(n) `querySelectorAll` plus
  `offsetTop` reads (forced layout) on every scroll event.
