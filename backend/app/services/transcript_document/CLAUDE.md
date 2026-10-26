# app/services/transcript_document — Word and PDF transcript documents (issue #1278)

## Purpose

Turns one transcript into a `.docx` or `.pdf` with numbered lines, so a place can be cited as
"page 3, line 12". Reached only through the async route `POST /files/{uuid}/export/document`
(`api/endpoints/files/transcript_document_export.py`) and the Celery task
`download.prepare_transcript_document` (`tasks/transcript_document.py`). The sync
`GET /files/{uuid}/export` refuses `docx`/`pdf` with a 400. User docs:
`docs-site/docs/features/document-export.md`.

## Pipeline and files

`build.prepare_document` (read phase, inside the worker's short DB session) -> `render.render_document`
(no session held; a long PDF takes about a minute) -> bytes uploaded under `bulk/`.

| File | Role |
|---|---|
| `model.py` | Frozen data only: `PageSpec` (`a4`/`letter`), `DocumentOptions`, `DocumentLabels`, `DocumentMeta`, `TurnBlock`/`CommentBlock`, `LayoutLine`, `DocumentLayout`, `DocumentTooLongError`, `FontsUnavailableError` |
| `build.py` | `prepare_document`: quarantine + scan-state re-check, policy resolution, masking, grouping. Raises `DocumentExportRefusedError(reason)` |
| `blocks.py` | `build_blocks`: turns + comments -> blocks (headers, timestamps, speaker names) |
| `scripts.py` | Content-based direction (first strong char), `needs_shaping`, `breaks_between_characters`, `sanitize_for_xml`, `cjk_region`/`cjk_font_family` |
| `layout.py` | `paginate` over a `Measurer` protocol: the fixed grid, keep-with-next, numbering modes |
| `docx_renderer.py` | python-docx writer |
| `pdf_renderer.py` + `fonts.py` | fpdf2 writer; per-document font plan, glyph checks |
| `render.py` | Dispatch + `CONTENT_TYPES` |

## Rules that are not obvious

- **The policy is resolved HERE, at task time, from a `user_id`** (`resolve_export_reveal`:
  `export_locked` beats any reveal; `admin_reveal_allowed=False`, because that bypass is scoped to
  a request and a worker has none). Never carry a config through the Celery signature. No
  `user_id` or an unreadable policy fails closed (`unavailable`); a scan still pending under a
  masking policy is `redaction_pending`. Masked text is never written back
  (`transcript_export_turns.masked_segments` expunges each segment first).
- **One layout model, two renderers.** A PDF page is a fixed grid of `DOCUMENT_LINES_PER_PAGE` (25)
  numbered slots, A4 and Letter alike; a turn header is kept with its first body line by padding
  with blank numbered slots. DOCX uses Word's native line numbering (`w:lnNumType`), so its
  pagination can differ. Layout tests run against `tests/unit/_monospace_measurer.py`, never
  host fonts; production measures with `FpdfMeasurer`.
- **Direction is a property of the CONTENT**, per turn, not of the UI locale. Shaping runs per line
  and only for scripts that join or reorder (`needs_shaping`): shaping every line of a Latin
  four-hour transcript cost four times the render for no visible difference.
- **CJK and Thai wrap between any two characters** (`CharWrap`); no kinsoku (the captions segmenter
  has it; a transcript page does not). The CJK face comes from the language region (SC/TC/JP/KR),
  collection index chosen per region.
- **Fonts are a deployment fact.** `DOCUMENT_FONT_DIR` (`/usr/share/fonts/opentranscribe`) is filled by
  the `fonts` stage of `Dockerfile.prod`/`.lite`/`.blackwell`, pinned by SHA-256
  (`backend/third_party/fonts/SHA256SUMS`). A missing face raises `FontsUnavailableError` ->
  `fonts_unavailable`, never a fontless PDF. The count of missing glyphs is logged, never the
  characters (the fpdf logger is silenced: it prints them). A DOCX names the fonts and never
  embeds them.
- **Deterministic bytes.** The PDF creation date is the export time, timezone-aware UTC, so the same
  input renders the same bytes. DOCX core properties are empty (no author leak). Everything written
  to DOCX goes through `sanitize_for_xml` (python-docx raises `ValueError` on XML-1.0-illegal
  characters). `pPr` children must follow the schema order.
- **Line cap:** `DOCUMENT_EXPORT_MAX_LINES` (60,000) -> `DocumentTooLongError` -> `too_long`.
  Labels are caller-supplied and substituted with `str.replace`, never `str.format`.
- **SSE contract:** the stream's `error` frame keeps `code` a literal slug (frontend `ChatErrorCode`
  subset, guarded by `test_document_export_stream_frames.py`); the reason is a separate field.

## Licences (verified 2026-10-25; full text in the repo `NOTICE`)

python-docx 1.2.0 MIT; **fpdf2 2.8.9 LGPL-3.0-only** (kept an unmodified, separately installed
package: never vendor or patch it); uharfbuzz 0.57.0 Apache-2.0 (bundles HarfBuzz, Old MIT);
fontTools MIT; pypdf BSD-3 (tests only); Noto fonts SIL OFL 1.1 (CJK is Adobe's Source Han Sans).
Only open-source, commercially usable licences are allowed here. Adding a dependency or a font
means recording its licence in `NOTICE` in the same commit.

## Tests

`tests/unit/test_transcript_document_{layout,blocks_scripts,docx,pdf}.py`,
`tests/api/test_document_export_{redaction_gate,access}.py`, `test_export_reveal_policy.py`; harness
in `tests/fixtures/document_export.py` (see `backend/tests/CLAUDE.md`). Timing:
`scripts/bench-transcript-document.py` (manual).
