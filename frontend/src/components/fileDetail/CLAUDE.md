# src/components/fileDetail

## Purpose

Presentational children extracted from the file-detail route `src/routes/files/[id]/+page.svelte`
(which stays the coordinator). Import via `$components/fileDetail/...`.

## Key files

- `TxtExportOptionsModal.svelte` — the TXT-export options dialog (include timestamps / speakers /
  comments + redaction note). Markup + scoped CSS only.
- `FileActionButtons.svelte` — the video-header action row (view transcript · AI summary
  view/generating/generate · reprocess). Emits `viewTranscript`, `showSummary`,
  `generateSummary`, `openReprocess`.
- `RedactionControls.svelte` — the show-original / rescan footer under the transcript. Renders
  nothing when the viewer can neither toggle nor rescan. Emits `rescan`, `toggleOriginal`.
- `RedactionPendingPanel.svelte` — the "detection still running" placeholder that replaces the
  transcript column while redaction is pending.
- `SpeakerProfileConfirmModal.svelte` — update-globally / create-new confirmation for a renamed
  speaker. Emits `updateProfile`, `createNewProfile`, `cancel`.

## Conventions / patterns

- The route page owns the state and orchestration; these children take props and dispatch events.
- `TxtExportOptionsModal`: `bind:show` + the `include*` toggles; emits `confirm`/`close`.
  **Scroll-lock stays in the page** (`_prevTxtExport` + a `$:` lock/unlock block) — the child only
  sets `show=false` + dispatches so the page's reactive block sequences lock/unlock exactly as before.
- The modal-chrome CSS (`.modal-overlay`/`.modal-dialog`/`.modal-*`/`.btn*`) is duplicated between
  `TxtExportOptionsModal` and `SpeakerProfileConfirmModal` on purpose — Svelte scopes `<style>` per
  component, so each dialog needs its own copy. The page no longer carries it (#284 A3.5).
- `SpeakerProfileConfirmModal`: **scroll-lock stays in the page**, same as `TxtExportOptionsModal` —
  the page's `$:` block keys off `showSpeakerProfileConfirmation`, and the child is rendered inside
  that `{#if}`.

## How it connects

- Parent: `routes/files/[id]/+page.svelte`. Export content is generated server-side
  (`GET /files/{uuid}/export`, issue #673); `$lib/export/txtExportPrefs` only persists the
  TXT options dialog's toggles.
- The transcript itself is rendered by `TranscriptDisplay` + `components/transcript/*`.
- **Issue #748 layout**: the left (`.video-column`) column order is player, waveform,
  `TranscriptActionsBar` (Export · Edit speakers · Download — moved OUT of `TranscriptDisplay`,
  renders directly on the page now), then either `SpeakerEditorPanel` (while editing speakers)
  or `TagsSection`/`CollectionsSection` (otherwise), then `AnalyticsSection` (always visible,
  including while editing speakers — it's the speaker context being labelled against), then
  `MetadataDisplay` (also moved — was in `.file-header` above the grid, now the last item in
  this column, hidden while editing speakers). `CommentSection` moved to the bottom of the
  RIGHT (`.transcript-column`) column, below `RedactionControls`. The page owns
  `isEditingSpeakers` directly (`TranscriptActionsBar`'s `toggleSpeakerEditor` event flips it
  inline) — there is no longer a prop-forwarding hop through `TranscriptDisplay`.
- The download SSE stream `TranscriptActionsBar` triggers lives in
  `$lib/fileDetail/downloadStream.ts` (`createDownloadStreamManager()`, issue #748 J5) — a
  factory returning `{ downloadMedia, cleanup }`; the page calls `cleanup()` from its own
  `onDestroy`.

## Gotchas

- **`transcript_segments` is the SINGLE representation of segment data — never reintroduce a
  second copy (#352).** `file.grouped_segments` is the backend's display grouping and carries
  only `segment_uuids`; `$lib/transcript/resolveGroupedSegments.ts` (shared by
  `TranscriptDisplay` and the search-result "view transcript" modal, issue #755) is the one
  place those references are resolved. Groups used to embed full segment copies, so the page
  held two objects per segment and every optimistic write patched only the flat one — renaming
  a speaker or editing a segment saved to the database and then rendered nothing until a full
  page reload. **Mutate segments only through `$lib/fileDetail/segmentSync`**
  (`renameSpeakersInFile`, `patchSegmentInFile`, `appendSegmentPage`), never with a bare loop
  over `transcript_segments`. `transcriptStore` (the raw store) is still fed for
  `AnalyticsSection`'s `SpeakerStats` — its `processedTranscriptSegments` derived store (the
  old `TranscriptModal`'s speaker-grouped reading view) was deleted with that component
  (#755).
- **Prop-drilling is the settled pattern — don't add a store for page state (#338).** The page
  used to also write a `reactiveFile` writable on every mutation; nothing ever subscribed to it,
  so all 13 `.set()` calls were inert while reading as if they refreshed the UI. It has been
  deleted. The update path is the page's own `file` assignment (`file = {...file}`, or a member
  assignment like `file.tags = …`) invalidating the variable, which re-renders the children
  through their props. `notificationHandler.setFile` exists for exactly that reason and must keep
  doing the real assignment.
- The file-detail E2E (`backend/tests/e2e/test_file_detail_transcript.py`) guards the page's
  transcript/export/speaker-editor surfaces — keep those working when adding children here.
- This is a coordinator route: it legitimately keeps a large `<script>` (data loading, WebSocket
  notifications, speaker bulk-save). Extract markup/CSS, not the orchestration.
