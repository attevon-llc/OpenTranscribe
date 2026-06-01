# src/components/fileDetail

## Purpose

Presentational children extracted from the file-detail route `src/routes/files/[id]/+page.svelte`
(which stays the coordinator). Import via `$components/fileDetail/...`.

## Key files

- `TxtExportOptionsModal.svelte` — the TXT-export options dialog (include timestamps / speakers /
  comments + redaction note). Markup + scoped CSS only.

## Conventions / patterns

- The route page owns the state and orchestration; these children take props and dispatch events.
- `TxtExportOptionsModal`: `bind:show` + the `include*` toggles; emits `confirm`/`close`.
  **Scroll-lock stays in the page** (`_prevTxtExport` + a `$:` lock/unlock block) — the child only
  sets `show=false` + dispatches so the page's reactive block sequences lock/unlock exactly as before.
- The modal-chrome CSS (`.modal-overlay`/`.modal-dialog`/`.modal-*`/`.btn*`) is duplicated here and
  in the page on purpose: the page still hosts the Speaker-Profile confirmation modal that uses the
  same classes. Don't "dedupe" by deleting it from the page.

## How it connects

- Parent: `routes/files/[id]/+page.svelte`. Export pipeline: `$lib/export/transcriptExport`.
- The transcript itself is rendered by `TranscriptDisplay` + `components/transcript/*`.

## Gotchas

- The file-detail E2E (`backend/tests/e2e/test_file_detail_transcript.py`) guards the page's
  transcript/export/speaker-editor surfaces — keep those working when adding children here.
- This is a coordinator route: it legitimately keeps a large `<script>` (data loading, WebSocket
  notifications, speaker bulk-save). Extract markup/CSS, not the orchestration.
