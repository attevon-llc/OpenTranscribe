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
- **CJK and Thai wrap between any two characters** (`breaks_between_characters` -> fpdf2 `WrapMode.CHAR`); no kinsoku (the captions segmenter
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

## Exports and redaction — three rules that are not obvious (issue #85)

`subtitle_service.build_subtitle_archive` (the bulk ZIP) and the burned-in-subtitle render
in `video_processing_service` used to call the subtitle generators with **no redaction
config**, exporting the raw transcript for everyone — including under the admin
`redaction.force_export_redacted` floor, which the UI presents as covering exports.

- **A "disabled" policy and an absent one must not be the same value.** `cfg=None` read as
  "redaction is off" to `_redact_segments_inplace`, and that is exactly what "the caller
  forgot" looks like, so no gate could ever fire on it. The parameter is now required and
  non-`Optional` on both paths; a disabled policy is an `EffectiveRedactionConfig` whose
  `enabled` is False. There is no argument value meaning "unspecified".
- **⚠️ The subtitle masker mutates the loaded ORM objects, and both export workers COMMIT.**
  `session_scope` commits on exit, so wiring masking into those tasks would have flushed the
  masked text into `transcript_segment.text` and destroyed the original transcript — the one
  thing read-time masking exists to preserve. The API path never noticed because `get_db`
  closes without committing. `_redact_segments_inplace` now **expunges each segment before
  mutating it**, so the guarantee is structural rather than a property of whichever caller
  holds the session. (`formatting_service._apply_redaction` was never exposed: it masks a
  dict copy.) Pinned by `test_export_redaction_paths.py`'s
  `test_the_export_never_writes_the_masked_text_back`, which is red without the expunge.
- **A cached artifact must name the policy it was rendered under.** Burned-in text cannot be
  masked afterwards, so `generate_cache_key` takes a required keyword-only
  `redaction_fingerprint`; without it the first reader's render is served to everyone, and an
  admin enabling the floor would keep serving an already-cached unmasked video. Consequence:
  `derived_cache_keys` can no longer enumerate a file's objects, so `clear_derived_cache`
  **lists** the masked variants — a delete that only reads that list leaves a video of the
  transcript in storage.

Whose policy applies (the **requesting user**, not the file owner) and when it is resolved
(**run time inside the task**, from a `user_id`, never a serialized config) are argued in
`redaction/export_policy.py`.

**And one rule that is not about redaction at all (issue #818): the export workers re-check
QUARANTINE at run time.** Both entry points are permission-filtered at dispatch, but a
takedown can land in the gap — `build_subtitle_archive` takes `include_quarantined` and
*skips* a taken-down file like any other unusable entry (one file must not fail the other
99), while `prepare_media_download_task` wraps its own file read in `exclude_unavailable` so
a taken-down file is refused exactly as a deleted one is. Sharing/tenant scope is still
decided once, at the endpoint; only the abuse gate, which is time-varying by design, is
re-applied.
