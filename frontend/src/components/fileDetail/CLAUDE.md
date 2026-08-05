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

- The file-detail E2E (`backend/tests/e2e/test_file_detail_transcript.py`) guards the page's
  transcript/export/speaker-editor surfaces — keep those working when adding children here.
- This is a coordinator route: it legitimately keeps a large `<script>` (data loading, WebSocket
  notifications, speaker bulk-save). Extract markup/CSS, not the orchestration.
