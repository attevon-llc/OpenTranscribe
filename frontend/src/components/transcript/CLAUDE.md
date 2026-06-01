# frontend/src/components/transcript

## Purpose

Thin presentational children of `TranscriptDisplay.svelte` (the coordinator), split out of
that oversized file. They render the transcript editing/export UI and dispatch intent back up.

## Key files

- `SpeakerEditorPanel.svelte` — edit/merge speaker labels; emits speaker-rename/merge events.
- `TranscriptActionsBar.svelte` — export + download dropdowns; dispatches `export` / `download`
  (the parent owns the actual logic).
- `TranscriptSegmentList.svelte` — the scrollable segment list: playback sync, inline text
  edit, search highlighting, infinite-scroll pagination sentinel.

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
