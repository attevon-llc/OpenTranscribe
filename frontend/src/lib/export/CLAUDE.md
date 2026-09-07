# frontend/src/lib/export

## Purpose

Client-side helpers around transcript export. As of issue #673, the export **content
itself** is serialized server-side (`GET /api/files/{uuid}/export`, txt/json/csv/srt/vtt) so
the admin `export_locked` policy floor is consulted for every format — the previous
client-side serializer (`transcriptExport.ts`) never asked the server anything, so
`export_locked` was enforced only for subtitle downloads. This directory now holds only the
one piece that stays legitimately client-side: persisted UI preferences for the TXT export
options dialog.

## Key files

- `txtExportPrefs.ts` — `loadTxtPrefs` / `saveTxtPrefs`: localStorage persistence for the TXT
  timestamps/speakers toggles (defaults both-on; partial blobs merge over defaults). Pure
  preference storage, not transcript content — nothing here handles redaction policy.

## How it connects

- `src/routes/files/[id]/+page.svelte` reads `txtExportPrefs` to prefill the TXT options
  modal, then calls `GET /files/{uuid}/export` (via `$lib/axios`, `responseType: 'blob'`) with
  the resolved i18n label strings as query params and triggers the browser download from the
  response blob. See `backend/app/services/transcript_export_service.py` and
  `backend/app/api/endpoints/files/transcript_export.py` for the serialization + redaction gate.

## Gotchas

- Do not reintroduce a client-side transcript serializer. If a new export format is needed,
  add it to `transcript_export_service.VALID_FORMATS` and the backend builder functions — a
  client-side one would silently opt that format out of `export_locked` again.
