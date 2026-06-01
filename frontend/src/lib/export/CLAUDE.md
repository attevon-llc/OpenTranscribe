# frontend/src/lib/export

## Purpose

Client-side transcript export serialization — turns already-downloaded transcript data into
TXT / JSON / CSV / SRT / VTT strings. The **one approved exception** to the thin-frontend rule:
a purely presentational transform over data already on the client.

## Key files

- `transcriptExport.ts` — `buildExportContent(...)` plus `mergeCommentsWithTranscript`,
  `mergeSortedArrays`; serializes all five formats. Time formatting is delegated to the single
  shared home in `$lib/utils/formatting`.
- `txtExportPrefs.ts` — `loadTxtPrefs` / `saveTxtPrefs`: localStorage persistence for the TXT
  timestamps/speakers toggles (defaults both-on; partial blobs merge over defaults).
- `transcriptExport.test.ts` — golden tests (Vitest) locking byte-output of every format.

## Conventions / patterns

- **Svelte-free**: imports no stores and resolves no i18n. The caller resolves every user-visible
  string via `$t(...)` and passes them in through `opts.translations` (`ExportStrings`).
- Output is byte-locked by the golden tests — `jsonMeta` carries the original (with-extension)
  filename/duration so JSON stays byte-identical.

## How it connects

- Called from `src/routes/files/[id]/+page.svelte` (and the transcript actions bar) when the user
  exports. The page resolves i18n and triggers the browser download.

## Gotchas

- Keep this module store-free and i18n-free — that's why it's testable. Don't import `$stores/...`.
- Changing serialization output requires updating the golden fixtures in `transcriptExport.test.ts`.
