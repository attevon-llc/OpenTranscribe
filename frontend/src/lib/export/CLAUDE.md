# frontend/src/lib/export

## Purpose

Client-side helpers around transcript export. As of issue #673, the export **content
itself** is serialized server-side (`GET /api/files/{uuid}/export`, txt/json/csv/srt/vtt) so
the admin `export_locked` policy floor is consulted for every format — the previous
client-side serializer (`transcriptExport.ts`) never asked the server anything, so
`export_locked` was enforced only for subtitle downloads. Issue #821 finished the job: the
transcript modal's **clipboard copy** was the residual #673 did not reach — it built the
whole consolidated transcript from `processedTranscriptSegments` and wrote it straight to
the clipboard, unredacted whenever the reader had "Show original" on, **and** only the
segments paginated in so far, so a long recording silently copied a PREFIX.

**The clipboard is an export destination.** It does not look like one, which is exactly
why it was missed; `clipboardSurfaces.test.ts` now enumerates every surface in the SPA that
writes to it, each with a written reason, so the next one has to answer the question.

## Key files

- `requestTranscriptExport.ts` — the ONE call onto `GET /api/files/{uuid}/export`, shared by
  the download flow and the clipboard copy. ⚠️ Never give it a local fallback: a `catch` that
  serialized in the browser would reinstate the bypass for precisely the cases the server
  refuses (503 = policy unresolvable, 409 = redaction scan unfinished).
- `clipboardSurfaces.test.ts` — the allowlist of clipboard-writing surfaces. It proves they
  are _enumerated_, not that each is correct; the reason text is the claim a human checks.
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
- **A "copy" button is an export.** So is anything that hands transcript text to another
  application. Route it through `requestTranscriptExport` and add the surface to
  `clipboardSurfaces.test.ts`'s allowlist with a reason.
- The copied text is now the server's **TXT** rendering (`[hh:mm:ss --> hh:mm:ss]` on its own
  line, then `Speaker:`, then the text), not the modal's old
  `Speaker [m:ss-m:ss]: text`. That is deliberate: one renderer for the download and the
  copy, so they cannot disagree about what the transcript says.
