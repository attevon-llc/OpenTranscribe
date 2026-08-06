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

- Parent: `routes/files/[id]/+page.svelte`. Export pipeline: `$lib/export/transcriptExport`.
- The transcript itself is rendered by `TranscriptDisplay` + `components/transcript/*`.

## Gotchas

- **`transcript_segments` is the SINGLE representation of segment data — never reintroduce a
  second copy (#352).** `file.grouped_segments` is the backend's display grouping and carries
  only `segment_uuids`; `TranscriptDisplay.mapBackendGroup` is the one place those references
  are resolved. Groups used to embed full segment copies, so the page held two objects per
  segment and every optimistic write patched only the flat one — renaming a speaker or editing
  a segment saved to the database and then rendered nothing until a full page reload. **Mutate
  segments only through `$lib/fileDetail/segmentSync`** (`renameSpeakersInFile`,
  `patchSegmentInFile`, `appendSegmentPage`), never with a bare loop over `transcript_segments`.
  Note `transcriptStore` is a _separate_ flat store feeding `TranscriptModal` — keep calling
  `transcriptStore.updateSpeakerName` alongside the helper.
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
