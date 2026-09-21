# frontend/src/components/transcript

## Purpose

Thin presentational children split out of the file-detail page. They render the transcript
viewing/editing/export UI and dispatch intent back up.

## Key files

- `SpeakerEditorPanel.svelte` — edit/merge speaker labels; emits speaker-rename/merge events.
  **Issue #748**: renders directly on `routes/files/[id]/+page.svelte` now (in `.video-column`,
  replacing Tags/Collections/File-details while open), not inside `TranscriptDisplay`.
- `TranscriptActionsBar.svelte` — export + download dropdowns; dispatches `exportTranscript` /
  `toggleSpeakerEditor` / `download` (the parent owns the actual logic). **Issue #748**: also
  renders directly on the page now, in `.video-column` right below the waveform. Its
  `toggleSpeakerEditor` event flips the page's `isEditingSpeakers` inline
  (`() => (isEditingSpeakers = !isEditingSpeakers)`) — there is no longer a prop-forwarding hop
  through `TranscriptDisplay` to lose the update in (see Gotchas — that hop is exactly what was
  broken before #748).
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
  **Issue #748**: exports `scrollToCurrentSegment(uuid)` — scrolls a segment into view and
  flashes it, with NO side effect on playback. This is what the deleted `ScrollbarIndicator`
  should have been: its click handler both scrolled AND dispatched `seekToPlayhead` up to the
  page, which re-seeked the player to `currentTime - 0.5s` — a silent playback rewind on every
  click. `TranscriptDisplay`'s "Jump to current" button (`.transcript-header`, always visible,
  not gated behind the collapsed find bar) calls this directly via `bind:this`; there is no
  `seekToPlayhead` event any more.
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
- **Coordinator keeps the heavy logic, but "the coordinator" is now the PAGE for anything
  above the transcript list itself.** `routes/files/[id]/+page.svelte` owns the download SSE
  (via `$lib/fileDetail/downloadStream.ts`'s `createDownloadStreamManager()`, issue #748 J5)
  and `isEditingSpeakers`. `TranscriptDisplay.svelte` still owns
  `handleSegmentSpeakerChange` (calls `updateSegmentSpeaker`) and segment-group resolution for
  the segment list itself, and still takes **`bind:file`** for that —
  `handleSegmentSpeakerChange` writes back through `$lib/fileDetail/segmentSync`, which returns
  a new file object rather than mutating in place.
- **Grouping comes from the backend and references segments by uuid.**
  `$lib/transcript/resolveGroupedSegments.ts` (shared with the search-result "view transcript"
  modal, issue #755) resolves `grouped_segments[].segment_uuids` against
  `file.transcript_segments` — the single copy of segment data (#352). There is deliberately no
  client-side grouping fallback any more; don't re-add one.

## How it connects

- Parents: `$components/TranscriptDisplay.svelte` (segment list + search, still coordinates
  those two) and `routes/files/[id]/+page.svelte` directly (`TranscriptActionsBar`,
  `SpeakerEditorPanel`, issue #748), both rendered on `/files/[id]`.
- Stores: `$stores/downloads`, `$stores/toast`, `$stores/locale`. API: `$lib/api/transcripts`.

## Gotchas

- `:global(.highlight-flash)` (playback flash) and `.segment-text :global(span.redacted)`
  (redaction masking style) live in **TranscriptSegmentList.svelte** — redaction text is
  injected as HTML, so these must stay `:global`.
- Don't relocate download/SSE or `handleSegmentSpeakerChange` off their respective owners
  (the page and `TranscriptDisplay`, per above) — they belong there so state stays
  single-sourced.
- **`isEditingSpeakers` was a broken one-way prop before issue #748 — do not reintroduce the
  pattern.** It used to be passed one-way into `TranscriptDisplay`, which toggled its OWN local
  copy on click; the page's copy was never set to `true`, so a later `isEditingSpeakers = false`
  after a successful bulk save was a dead statement and the editor panel never actually closed.
  Both `TranscriptActionsBar` and `SpeakerEditorPanel` now render directly on the page, which
  owns the boolean outright — there is no prop-forwarding hop left to lose an update in. If you
  ever need a component between them and the page again, either `bind:` the flag or bubble the
  toggle event all the way up; never let a child own a copy of state the parent also reads.
- **The segment list is virtualized by CSS (`content-visibility: auto`), not by JS windowing.**
  Don't swap in `$components/gallery/VirtualList.svelte`: it slices a fixed-44px row list, whereas
  segments are variable height (wrapped text, multi-segment overlap groups, the expanded edit
  textarea), and evicting off-screen rows breaks everything that reaches a segment through
  `document.querySelector('[data-segment-id=…]')` — search scroll-to, `scrollToCurrentSegment`,
  `SpeakerEditorPanel`'s jump, and `.highlight-flash`.
- **No `on:scroll` handler on `.transcript-display`.** Reading progress comes from the
  IntersectionObserver; a scroll handler here previously ran an O(n) `querySelectorAll` plus
  `offsetTop` reads (forced layout) on every scroll event.
