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

## Conventions / patterns

- Import via `$components`, i18n via `$t` from `$stores/locale`; speaker labels through
  `translateSpeakerLabel` (`$lib/i18n`).
- Children take props + `createEventDispatcher` — no API calls, no stores-as-state.
- **Coordinator keeps the heavy logic**: `TranscriptDisplay.svelte` owns the download SSE
  (`EventSource` + `downloadStore`), `handleSegmentSpeakerChange` (calls `updateSegmentSpeaker`),
  segment grouping, and bridges child `export`/`download` events to its own handlers.

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
