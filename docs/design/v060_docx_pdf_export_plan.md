# #1278 + #564: DOCX, PDF and Markdown transcript export (plan)

**Status:** PLAN ONLY. Nothing here is implemented.
**Written:** 2026-10-25, against `master` @ `a236125a` (worktree `docs/v060-issue-plans`).
**Issues:** #1278 (DOCX + PDF, v0.6.0), #564 (full-transcript Markdown, currently v0.10.0),
context from #796 (GDPR Art. 15/20 archive, v0.9.0), #673/#821/#85 (server-side export policy),
#1277 (content-access audit).

This is written for an implementation agent with **no** conversation history. Paths are relative
to the repo root. Re-verify every `path:LINE` before editing; line numbers drift.

---

## 0. TL;DR (decisions)

1. **Deliver #564 in the same work.** It shares the turn-grouping/label seam, the endpoint, the
   gate test and, above all, the same 12 locale files. One locale wave instead of two. Format
   token is **`md`** (not `markdown`), so token == extension and the `.markdown` trap disappears.
2. **Libraries:** `python-docx` (MIT) for DOCX; **`fpdf2` (LGPL-3.0-only) + `uharfbuzz`
   (Apache-2.0)** for PDF. ReportLab (BSD) was the permissive first choice and is rejected for
   v1 only because its bidi extra (`rlbidi`) is not on PyPI, so Arabic/Hebrew PDF lines would
   render in the wrong order. fpdf2 is the same licence class as `ldap3`, already shipped
   (REVIEW, not BLOCKER, in `security-reports/license-compliance/SBOM-SUMMARY.md:359-366`).
   Owner confirmation is Q1.
3. **DOCX and PDF render asynchronously on the `download` queue**, reusing the bulk-export
   job/SSE/presigned-URL machinery. Markdown (and the five existing formats) stay synchronous.
   Measured reason: a 4 h Arabic PDF is about 1 minute of GIL-bound CPU, against axios's 60 s
   timeout, and it would starve the API process.
4. **The PDF is the citable artifact** (deterministic page:line). DOCX uses Word's native line
   numbering and page fields, so its pagination is decided by the renderer. That corrects the
   issue's "deterministic pagination" acceptance for DOCX (Q4).
5. **One export-policy decision for every surface.** The `export_locked` branch moves into one
   service function that the sync endpoint, the subtitle endpoint and the new task all call.
6. Fonts: a pinned Noto subset (OFL-1.1, ALLOW class) copied into the backend images, about
   22-25 MB including one CJK collection (Q3). Tests use vendored fixture fonts, so goldens never
   depend on host fonts.

---

## 1. Verified current state (file:line) and corrections

### 1.1 Facts

| Fact | Evidence |
|---|---|
| Sync formats are `("txt","json","csv","srt","vtt")` | `backend/app/services/transcript_export_service.py:36` |
| Dispatch: srt/vtt to `_build_cues` (`:154-163`), txt `:173`, json `:182`, and a **bare `return _build_csv(...)` fallthrough** `:184` | same file. #564's plan says the fallthrough is VTT; that is stale. A new token added to `VALID_FORMATS` without its own branch now silently returns **CSV** with HTTP 200 |
| Masking expunges before mutating, fails closed to `"[redacted — masking unavailable]"` | `transcript_export_service.py:53-104` |
| Turn grouping (consecutive same speaker) is inline in `_build_txt` | `:213-263`; `_merge_by_timestamp` `:195-210` |
| The service file is 374 lines, over the ~300 guideline | `wc -l` |
| Endpoint `GET /files/{uuid}/export`, `refuse_under_support_grant`, labels as query params | `backend/app/api/endpoints/files/transcript_export.py:94-120` |
| `export_locked` branch, copy 1 | `transcript_export.py:86-87` |
| `export_locked` branch, copy 2 | `backend/app/api/endpoints/files/subtitles.py:66` |
| Content types fall back to `text/plain` silently | `transcript_export.py:41-47,205` |
| **`Content-Disposition: attachment; filename="<raw name>"`**: a non-ASCII name raises `UnicodeEncodeError` (latin-1) in Starlette, so the export is a **500** | `transcript_export.py:211`, `summarization.py:367`, `subtitles.py:182`. Reproduced 2026-10-25 with `starlette.responses.Response(headers={"Content-Disposition": 'attachment; filename="اجتماع.txt"'})`. The RFC 6266 form already exists at `services/minio_service.py:441-446` |
| Gate test: `ALL_FORMATS` x 5 cases, asserts on `response.text`; uses `"pdf"` as its unsupported-format example | `backend/tests/api/test_transcript_export_redaction_gate.py:34,131-215,218-225` |
| Route-table plane test: any `export`/`download` route must reach `resolve_effective_config` | `backend/tests/unit/test_export_plane_resolves_a_policy.py:1-40` |
| Summary Markdown renderer (pure, takes an already-masked dict) | `backend/app/services/summary_export_service.py:40,359-390` |
| Async bulk export: HMAC job id bound to the user, Celery task on `download`, `bulk/{job}.zip`, presign, Redis result cache `bulk_export_result:{job}` (900 s), SSE stream | `subtitles.py:255-361`, `tasks/media_download.py:176-288`, `services/download_events.py:112-139`, route `core/celery.py:464` |
| `bulk/` objects expire after 1 day | `services/video_processing_service.py:388` |
| SPA download site: `requestTranscriptExport` then Blob, then `link.download = ${filename}.${format}` (extension recomputed client-side) | `frontend/src/routes/files/[id]/+page.svelte:1439-1471`, `frontend/src/lib/export/requestTranscriptExport.ts:57-78` |
| **Export failures are silent** (`console.error` only; 409/503 show nothing) | `+page.svelte:1468-1470` |
| Menu entries hardcoded | `frontend/src/components/transcript/TranscriptActionsBar.svelte:84-119` |
| Dead i18n family `export.*` (12 keys x 12 locales) still present, referenced by nothing | `frontend/src/lib/i18n/locales/en.json:2591-2602`; `rg "'export\.[a-z]" frontend/src` (excluding locales) returns nothing |
| Gallery bulk export consumes the SSE inline | `frontend/src/routes/+page.svelte:915-980` |
| Capability `exports` is UI-only vocabulary | `backend/app/core/capabilities.py:52,105,168` |
| `media_file.title`, `.filename`, `.language`, `.recorded_date` (+ `recorded_date_source`) | `backend/app/models/media.py:58,85,224,244` |
| Script ranges (CJK, Hangul, Thai and similar) already defined | `backend/app/services/captions/scripts.py:35-65` |
| Project licence AGPL-3.0; owner's commercial policy is permissive-only, LGPL is REVIEW | `LICENSE`, `scripts/generate-sbom-report.py:31-108` |
| `lxml` pinned in all three requirement sets; `pillow`, `fonttools`, `defusedxml`, `charset-normalizer` already in the full and lite images | `backend/requirements.txt:368`, `security-reports/lite-amd64-sbom.txt:37,59,78,440` |

### 1.2 Corrections to the issue bodies

- **#1278 "through the same export endpoint".** Policy must be the same, and it will be (§3.3).
  The **route** cannot be the same synchronous GET for DOCX/PDF (§2.2 measurements). DOCX/PDF use
  `POST /files/{uuid}/export/document` and the existing job stream. The policy rule itself lives
  in one function.
- **#1278 "pagination deterministic" for DOCX.** A DOCX has no fixed pagination: Word or
  LibreOffice lays it out with whatever fonts the reader has. The DOCX golden is its XML, not its
  rendered pages. Page:line citations come from the PDF.
- **#564 plan (2026-09-09).** The format token should be `md` (matches `summary_export_service`
  and removes the extension trap). The fallthrough trap is now CSV, not VTT. The failure-toast
  fix (its T5) is still needed. The dead `export.*` family (its JD11) is still present.
- **#796** states correctly (its §3.6) that DOCX/PDF are not Art. 20 deliverables. What it needs
  from this work is that the Markdown builder is a **pure function of already-masked data**, so a
  batch collector can reuse it. That is a design constraint here.
- **New finding (not in any issue): non-ASCII filenames 500 on every transcript, subtitle and
  summary export.** Fixed in WP0. File it as its own `bug` issue and link it from #1278.

---

## 2. Library and font decision

### 2.1 Candidates

| Option | Licence | Install cost | RTL/complex scripts | Verdict |
|---|---|---|---|---|
| `python-docx` 1.2.0 | MIT | 2.8 MB; deps lxml + typing_extensions already present | Word shapes at render time; we set `w:bidi`/`w:rtl` | **DOCX: use** |
| `fpdf2` 2.8.9 | LGPL-3.0-only | 3.0 MB; deps pillow/fonttools/defusedxml already present | built-in UAX #9 bidi (`fpdf/bidi.py`, Unicode 15.1), HarfBuzz shaping, fallback fonts, TTC + CFF fonts (`fonts.py` `collection_font_number`, `is_cff`) | **PDF: use (Q1)** |
| `uharfbuzz` 0.57.0 | Apache-2.0 | 5.0 MB; manylinux x86_64 + aarch64 wheels | shaping engine for fpdf2 | **use with fpdf2** |
| `reportlab` 5.1.0 | BSD | 9.0 MB | shaping via uharfbuzz works; bidi needs extra `rlbidi`, which is **404 on PyPI** (checked 2026-10-25). `python-bidi` is LGPL, so swapping it in loses the licence advantage | fallback if Q1 = no, with RTL PDF unsupported |
| `weasyprint` 70 | BSD | Pango/HarfBuzz/fontconfig OS libs plus 5 Python deps | full shaping | **reject**: CSS counters number elements, not wrapped line boxes, so line numbers still need pre-wrapping, and it adds the heaviest native footprint |
| LibreOffice headless (DOCX to PDF) | MPL-2.0 | >300 MB, subprocess | good | **reject**: size, nondeterministic layout |

fpdf2 is LGPL-3.0-only. It can be combined with AGPL-3.0 code (LGPLv3 is GPLv3 plus permissions;
GPLv3 §13 allows AGPLv3 combination). For closed derivative distribution, the LGPL obligation is
met by installing fpdf2 unmodified as its own Python package, which keeps it replaceable, and by
shipping its licence text. Never vendor or patch it into our source.

### 2.2 Measured spike (host, Python 3.12, one process, 2026-10-25)

Synthetic 3,600 segments x 10 words (about 4 h of speech), A4, 11 pt, 25 numbered lines per page,
wrapping with `multi_cell(dry_run=True, output=LINES)` and rendering with `cell()`:

| Run | Pages / lines | Wall | Peak RSS | Output |
|---|---|---|---|---|
| fpdf2, English, shaping off | 288 / 7,200 | **4.8 s** | 63 MB | 319 KB |
| fpdf2, English, shaping on for every line | 288 / 7,200 | **20.1 s** | 66 MB | 378 KB |
| fpdf2, Arabic, shaping on, 200 segments | 16 / 400 | 3.2 s (about **58 s** extrapolated to 4 h) | 63 MB | 41 KB |
| fpdf2, two renders with fixed `set_creation_date` | - | - | - | **byte-identical** (sha256 `3da0de2566f2` both times) |
| python-docx, 4 h | - | **2.0 s** | 37 MB | 81 KB |

Consequences: (a) memory is not the risk; CPU time is. (b) Shaping must be **per line, only when
the line contains a script that needs it** (§3.5); the measured 4x Latin penalty otherwise.
(c) Long RTL documents cannot be rendered in the request thread, hence §3.2.
Reproduce with WP3's `scripts/bench-transcript-document.py` (manual only; never a gate test with a
wall-clock budget, per `backend/tests/CLAUDE.md` "fixed wall-clock budget").

### 2.3 Fonts

- **PDF embeds subsets** (OFL-1.1 permits embedding; OFL is ALLOW in `generate-sbom-report.py:69`).
  Copy only the needed files from Debian `fonts-noto-core` / `fonts-noto-cjk` in a builder stage
  into `/usr/share/fonts/opentranscribe/`, plus `OFL.txt` (OFL requires the licence to travel with
  copies). Set, in fallback order: `NotoSans-{Regular,Bold}` (Latin/Greek/Cyrillic, about
  0.5 MB each), `NotoNaskhArabic-{Regular,Bold}` (about 0.2 MB), `NotoSansHebrew`,
  `NotoSansDevanagari`, `NotoSansBengali`, `NotoSansTamil`, `NotoSansThai`, `NotoSansGeorgian`,
  `NotoSansArmenian`, `NotoSansEthiopic` (each 0.03-0.3 MB), and `NotoSansCJK-Regular.ttc`
  (19.5 MB; collection index picked from `media_file.language`: zh to SC, ja to JP, ko to KR,
  zh-Hant to TC). No CJK bold: CJK speaker labels render in regular weight. Measure the real total
  in WP3 (estimate 22-25 MB per image). The installed size if we took the whole packages would be
  42.5 MB + 91 MB, so copy files instead.
- Applies to `backend/Dockerfile.prod`, `Dockerfile.lite` and `Dockerfile.blackwell`. Lite means
  "no GPU", not "small" (project memory), so lite gets the same fonts.
- **DOCX does not embed fonts.** It names `Noto Sans` (ascii/hAnsi), `Noto Naskh Arabic` (`w:cs`)
  and `Noto Sans CJK SC|JP|KR|TC` (`w:eastAsia`). Word substitutes when they are missing. No
  licence surface.
- Fallback for an uncovered glyph: fpdf2 draws `.notdef`. We count missing glyphs and log the
  **count only**. fpdf2's own logger message includes the missing characters, which are transcript
  content (`fpdf/output.py:1352`), so set `logging.getLogger("fpdf")` to ERROR inside the renderer
  (#1022 log hygiene).

---

## 3. Design

### 3.1 The shared seam (what #564 and #1278 share)

```
build_export_content (sync: txt json csv srt vtt md)        prepare_transcript_document task (docx pdf)
        |                                                             |
        +----------- masked_segments() + group_turns() + merge_comments() -----------+
                                  (one masking path, one grouping rule)
                 txt  json  csv  md                 docx renderer      pdf renderer (layout + fpdf2)
```

- `masked_segments` is today's `_redact_segments` (unchanged semantics, expunge preserved).
- `group_turns(masked, speaker_name) -> list[Turn]` is lifted from `_build_txt` (`:221-238`), and
  `_build_txt` is rewritten on top of it. Its golden bytes must not change; the existing TXT tests
  pin that.
- Every builder is a **pure function of already-masked data plus caller labels**. No DB, no
  session (#796 constraint).

### 3.2 Sync vs async

- `md` joins the synchronous GET (python-level string building, comparable to TXT).
- `docx`, `pdf` use `POST /files/{uuid}/export/document`, which returns 202 + `job_id`. Celery task
  `download.prepare_transcript_document` renders, uploads `bulk/{job_id}.{ext}` (the existing 1-day
  lifecycle, no new rule), presigns with the RFC 6266 filename, caches the result and publishes on
  the existing `bulk_export_channel`. The browser reuses `GET /files/bulk-export-stream?job=`.
  That stream is a generic signed-job stream; its name is historical, and a rename is out of scope.
- **Rejected:** sync for all sizes (GIL starvation, 60 s axios timeout, a 1-minute Arabic case);
  a hybrid with sync below N lines and async above (two paths for one job, forbidden by root
  `CLAUDE.md`); a 202 from the GET endpoint (a GET with a side effect and a mixed response type).
- DOCX is fast enough for sync, but it goes async too: one document path, one gate, one UI flow.

### 3.3 Policy: one decision, three callers

New in `backend/app/services/redaction/export_policy.py`:

```python
def resolve_export_reveal(cfg, *, reveal_requested: bool, is_owner: bool,
                          admin_reveal_allowed: bool) -> set[str]:
    """export_locked always wins; otherwise cfg.reveal_categories(...)."""
```

- `transcript_export._resolve_export_redaction` and `subtitles._resolve_subtitle_redaction` call it.
  After WP2, `rg "export_locked" backend/app/api` returns zero branch sites; the one branch is in
  the service.
- Move `audit_unredacted_reveal` from `api/endpoints/files/crud.py:614` to
  `services/redaction/reveal_audit.py` and re-export it from `crud.py`. Still one implementation;
  the worker cannot import an API module.
- **Task-time resolution:** the task receives `user_id`, `organization_id`, `reveal_requested`,
  never a config (`export_policy.py` rule). It recomputes `is_owner = media_file.user_id ==
  user_id`. `admin_reveal_allowed=False` in the task: a non-owner admin's reveal is **not**
  offered for document formats in v1. This is a deliberate narrowing, because the platform bypass
  is request-scoped. Audit surface name: `transcript_document_export`.
- Pending scan: 409 at dispatch (fast path through `_redaction_pending`), and a re-check in the
  task with `export_masking_is_pending` (an error event with code `redaction_pending`).
- Quarantine: re-checked in the task (`exclude_quarantined`, or `is_review_admin` like
  `media_download.py:244`), matching #818.

### 3.4 Pagination and line numbering (PDF)

- `PageSpec`: A4 (595x842 pt) or Letter (612x792 pt); margins 72 pt top and bottom, a 54 pt
  left gutter for numbers; 11 pt text; **25 numbered lines per page** (`DOCUMENT_LINES_PER_PAGE`).
  Line height derives from the available height, so both page sizes keep the 25-line grid.
- Unnumbered header: display name (`title or filename`, ellipsised to width), `exported_label`
  with ISO `YYYY-MM-DD` (UTC, injectable clock), and `redacted_label` when masking applied.
  Footer: `page_label` with `{page}`/`{pages}` substituted by `str.replace`, never `str.format`.
- Numbered lines: turn header (`[hh:mm:ss] SPEAKER:` bold; parts omitted per options), the wrapped
  text lines, and comment lines (italic, `{user_comment_label}: {user} [ts]: text`, merged by
  timestamp with `merge_comments`).
- `line_numbering`: `per_page` (1-25 restart, the default), `continuous` (1..N), `none`.
- Keep-with-next: a turn header never sits on the last line of a page. The slot stays blank and
  numbered, so the grid stays fixed.
- Layout is a **pure two-phase model**: `layout.paginate(blocks, measurer, spec) -> DocumentLayout`
  (pages, then lines of `(number, kind, text, direction)`), then `pdf.render(layout, fonts)`.
  Production `measurer` = fpdf2 `multi_cell(dry_run=True, output=LINES)` with the same font and
  shaping state as rendering, so measurement and drawing agree. Tests use a `MonospaceMeasurer`.
- Hard cap `DOCUMENT_EXPORT_MAX_LINES = 60_000` (about 2,400 pages; 8x the measured 4 h case).
  Exceeding it produces the error event `too_long`. Task `soft_time_limit =
  DOCUMENT_EXPORT_SOFT_TIME_LIMIT_S = 300`, `max_retries=0`. All are constants in
  `core/constants.py`, not env vars and not SystemSettings (they are safety bounds, not tuning).

### 3.5 RTL and non-Latin

- Direction is a property of the **content**, not the UI locale. Per turn, the base direction is
  the first strong character (UAX #9 P2/P3, `unicodedata.bidirectional`). That mirrors the SPA's
  `dir="auto"`.
- PDF: an RTL turn's lines are right-aligned and go through fpdf2 bidi; the line-number gutter
  stays left in every document (one citation column).
- Shaping is toggled **per line**, only when the line contains Arabic, Hebrew, Syriac, Thaana,
  N'Ko, Indic, Thai, Lao, Khmer, Myanmar or Tibetan. Put these ranges in
  `transcript_document/scripts.py`, reusing the range constants from `captions/scripts.py:35-65`
  instead of a second table. WP3 must measure that Latin returns to about 5 s.
- CJK/Thai line breaking: fpdf2 `WrapMode.CHAR` for lines containing unspaced scripts. Kinsoku is
  out of scope for v1 (note it in the docs). Goldens cover zh, ja and th.
- DOCX: `w:bidi` on RTL paragraphs, `w:rtl` on their runs, cs/eastAsia fonts as in §2.3.

### 3.6 DOCX specifics

Page size and margins from `PageSpec`. Section `w:lnNumType w:countBy="1"` with
`w:restart="newPage"` (per_page) or `"continuous"`; omitted for `none`. Header paragraph(s) as in
§3.4. Footer `page_label` split around `{page}`/`{pages}` into `PAGE`/`NUMPAGES` `fldSimple`
fields. Turn header paragraph bold with `keep_with_next`. Comments are **inline italic paragraphs**,
not Word comments, so print matches the PDF. Core properties: `title` = display name, author and
last_modified_by empty, created/modified = injectable clock. **Strip XML-1.0-illegal characters**
(C0 except tab/LF/CR, U+FFFE/FFFF) from every string first: python-docx raises `ValueError` on them,
and transcripts can contain them.

### 3.7 Markdown (#564, transcript only in v0.6.0)

```markdown
---
title: "Q4 Budget Review"
date: "2024-03-15"          # media_file.recorded_date; key omitted with date_source when null
date_source: "filename"     # recorded_date_source
duration: "01:12:30"
participants: ["Sarah Chen", "SPEAKER_01"]
language: "en"
opentranscribe_uuid: "0192f3a1-..."
exported_at: "2026-10-25T14:02:11Z"
redacted: true
---

# Q4 Budget Review

**Sarah Chen** [00:00:15]
I've reviewed the numbers...

> **USER COMMENT** — Ana [00:01:02]: check slide 4
```

- Every YAML scalar is written with `json.dumps(..., ensure_ascii=False)`. JSON is valid YAML, so
  `:`, `"`, `#`, a leading `-` or a newline cannot break a vault. Never write `upload_time` as the date.
- Transcript text is Markdown-escaped (`\ * _ ` [ ] < >`, and a leading `# > - + |` or `1.`).
- `include_timestamps` / `include_speakers` apply as for TXT. Labels reuse existing query params,
  plus `md_comment_label` if needed (do not invent more).
- **Summary inclusion (#564 JD9) is WP6, optional.** It needs `mask_summary`, live detection and a
  503 failure mode. It can stay on v0.10.0 without blocking anything here.

---

## 4. API contracts

**Sync (changed):** `GET /api/files/{uuid}/export?format=md` adds `md`, content type
`text/markdown; charset=utf-8`. **All sync exports** now send
`Content-Disposition: attachment; filename="<ascii fallback>"; filename*=UTF-8''<pct-encoded>`
through a new `app/utils/content_disposition.py::attachment_header(name)`, extracted from
`minio_service.py:441-446` (which then calls it). `docx`/`pdf` on this route return 400 with
detail `"Use POST /files/{uuid}/export/document for docx and pdf"`.

**New:** `POST /api/files/{file_uuid}/export/document`. Dependencies:
`refuse_under_support_grant`, `require_capability("exports.documents")`,
`get_current_active_user`, `get_current_context`. Schema in
`backend/app/schemas/transcript_export.py`:

```python
class DocumentExportLabels(BaseModel):          # every field: str, max_length=200
    speaker_default_label: str = "Speaker"
    user_comment_label: str = "USER COMMENT"
    page_label: str = "Page {page} of {pages}"
    exported_label: str = "Exported {date}"
    redacted_label: str = "Redacted"

class TranscriptDocumentExportRequest(BaseModel):
    format: Literal["docx", "pdf"]
    include_comments: bool = False
    include_timestamps: bool = True
    include_speakers: bool = True
    line_numbering: Literal["per_page", "continuous", "none"] = "per_page"
    page_size: Literal["a4", "letter"] = "a4"
    reveal_original: bool = False                # a REQUEST; refused under export_locked
    labels: DocumentExportLabels = DocumentExportLabels()

class DocumentExportAccepted(BaseModel):
    status: Literal["processing"]
    job_id: str                                  # 64 hex, HMAC-bound (new_bulk_job_id)
```

Responses: 202 `DocumentExportAccepted`; 400 bad option or file not completed; 404 file not
visible, no transcript, or capability off; 409 redaction pending; 503 policy unresolvable;
403 under a support grant; 422 schema. **Dedupe:** Redis
`SET transcript_doc_guard:{user_id}:{file_id}:{options_sha} <job_id> NX EX 300`. A repeat returns
the **same** `job_id` (a double click never renders twice). The guard is released on completion or
error.

**Stream events** (existing channel). `publish_bulk_event` gains an optional `code`:
`{"status":"processing"}` then either
`{"status":"completed","url","filename","exported":1,"skipped":0}` or
`{"status":"error","code":"redaction_pending"|"too_long"|"unavailable"|"fonts_unavailable"|"failed","message":<fixed sentence>}`.
The reconnect cache `bulk_export_result:{job}` holds the completed payload.

**Capability:** new key `exports.documents` (default True, `AUDIENCE_USER`), server-enforced on the
POST only. The task does **not** check it (no request at task time; see the
`core/capabilities.py:44-46` warning). Add it to the docstring's server-enforced list and to the
frontend `stores/capabilities.ts` community defaults.

**No migration. No new SystemSettings. No new env vars.**

---

## 5. File-by-file change list

Backend:
- `app/utils/content_disposition.py` (new): `attachment_header`. Callers: `transcript_export.py`,
  `summarization.py:367`, `subtitles.py:182`, `minio_service.py:441-446`.
- `app/services/transcript_export_service.py`: keep `VALID_FORMATS` (add `"md"`) and
  `build_export_content`; add an explicit `md` branch **and** turn the final CSV fallthrough into
  `if export_format == "csv"` + `raise AssertionError` (closes the silent-fallthrough trap for
  good). Move the builders out to keep the file under 300 lines:
  `transcript_export_turns.py` (`masked_segments`, `group_turns`, `merge_comments`,
  `format_duration`), `transcript_export_text.py` (txt/json/csv), `transcript_export_markdown.py`.
- `app/api/endpoints/files/transcript_export.py`: `_CONTENT_TYPES["md"]`, the 400 for docx/pdf,
  `attachment_header`, call `resolve_export_reveal`. New sibling module
  `files/transcript_document_export.py` with the POST route, registered in `files/__init__.py`
  beside `transcript_export`.
- `app/services/redaction/export_policy.py`: `resolve_export_reveal`.
  `app/services/redaction/reveal_audit.py`: moved `audit_unredacted_reveal`.
  `subtitles.py:66` uses the helper.
- `app/services/transcript_document/` (new package + `CLAUDE.md` recording the library/licence
  decision): `model.py` (blocks, `Turn`, `PageSpec`, options), `scripts.py`, `layout.py`,
  `docx_renderer.py`, `pdf_renderer.py`, `fonts.py` (font registry from
  `DOCUMENT_FONT_DIR`; `FontsUnavailableError`).
- `app/tasks/transcript_document.py` (new): `download.prepare_transcript_document`; route added in
  `core/celery.py` beside `:464`. `services/download_events.py`: optional `code`.
- `app/core/constants.py`: `DOCUMENT_LINES_PER_PAGE`, `DOCUMENT_EXPORT_MAX_LINES`,
  `DOCUMENT_EXPORT_SOFT_TIME_LIMIT_S`, `DOCUMENT_FONT_DIR = "/usr/share/fonts/opentranscribe"`,
  `DOCUMENT_EXPORT_GUARD_TTL_S = 300`. `core/capabilities.py`: `exports.documents`.
- `app/schemas/transcript_export.py` (new).
- `requirements.txt`, `requirements-lite.txt`, `requirements-ci.txt`: `python-docx==1.2.0`,
  `fpdf2==2.8.9`, `uharfbuzz==0.57.0` (re-check for newer patch releases at implementation time;
  versions move forward only). `requirements-test.txt`: `pypdf==6.21.2` (BSD-3). Pin `PyYAML`
  there too if `./scripts/check-dependency-parity.sh` shows it absent from CI (venv has 6.0.3
  transitively).
- `Dockerfile.prod`, `Dockerfile.lite`, `Dockerfile.blackwell`: a `fonts` builder stage
  (`apt-get install fonts-noto-core fonts-noto-cjk`), then `COPY --from=fonts` of the listed
  files + `OFL.txt` into `DOCUMENT_FONT_DIR`. Verify the trixie paths
  (`/usr/share/fonts/truetype/noto/`, `/usr/share/fonts/opentype/noto/`) when building.
- `backend/tests/fixtures/fonts/` (new): `NotoSans-Regular.ttf`, `NotoSans-Bold.ttf`,
  `NotoNaskhArabic-Regular.ttf` + `OFL.txt` (about 1.2 MB). CJK goldens use the
  `MonospaceMeasurer` layout only; the CJK PDF check runs in the image-level smoke (§9).
- `scripts/bench-transcript-document.py` (new, manual).

Frontend:
- `src/lib/ui/menuIcons.ts`: `format-md`, `format-docx`, `format-pdf`.
- `TranscriptActionsBar.svelte`: three entries; docx/pdf only when `exports.documents`.
- `src/lib/export/requestTranscriptExport.ts`: return `filename` parsed from
  `content-disposition` (RFC 6266 `filename*` first). `+page.svelte` uses it instead of
  recomputing.
- `src/lib/export/exportJobStream.ts` (new): EventSource consumer for a signed job (resolve on
  `completed`, reject with `code` on `error`, close on settle). Migrate the gallery
  `routes/+page.svelte:915-980` to it in the same PR (one consumer, not two).
- `src/lib/export/requestDocumentExport.ts` (new): POST, then `exportJobStream`, then an anchor
  click on the presigned URL. **No local fallback.**
- `src/lib/export/documentExportPrefs.ts` (new): localStorage like `txtExportPrefs.ts`.
- `src/components/fileDetail/DocumentExportOptionsModal.svelte` (new, **dynamic `import()`** from
  the page): timestamps/speakers/comments (reuse the `exportOptions.*` keys), line numbering,
  page size, the redaction note (copy the TXT modal's block).
- `+page.svelte`: route md through the existing sync flow, docx/pdf through the modal; **toast on
  every export/copy failure** (409, 503, error event codes) via `toastStore.error`.
- `src/lib/export/CLAUDE.md`, `frontend/src/components/fileDetail/CLAUDE.md`: document both flows.

i18n (all 12 locales, translated, not copied English): `transcript.exportMarkdown(+Title)`,
`transcript.exportDocx(+Title)`, `transcript.exportPdf(+Title)`, updated `transcript.exportTitle`
(it lists the formats); `documentExport.{title,description,lineNumbering,lineNumberingPerPage,
lineNumberingContinuous,lineNumberingNone,pageSize,pageSizeA4,pageSizeLetter,preparing,
pageLabel,exportedLabel,redactedLabel,errorRedactionPending,errorTooLong,errorUnavailable,
errorFailed}`; `transcript.exportFailed`, `transcript.exportRedactionPending`,
`transcript.exportPolicyUnavailable`. **Delete** the dead `export.*` family (12 x 12).
Server-template labels are produced as `$t('documentExport.pageLabel', { page: '{page}',
pages: '{pages}' })`, so i18next's `{{page}}` yields the literal `{page}` the server substitutes.

Docs: `docs-site/docs/user-guide/uploading-files.md` "Export Options" (`:195-251`): Markdown, Word,
PDF sections, page:line citation note, DOCX-vs-PDF guidance, CJK/Thai line-break limitation;
`docs-site/docs/features/content-redaction.md`: every format honours the export floor;
`backend/app/services/CLAUDE.md` export section; `CHANGELOG.md`; regenerate
`security-reports/license-compliance/` (fpdf2 appears as REVIEW).

---

## 6. Security and tenant isolation

| Surface | Could leak | Control | Negative test |
|---|---|---|---|
| POST dispatch | another tenant's or user's file | `get_file_by_uuid_with_permission(bypass=ctx.bypass, organization_id=ctx.org_id)` | other user's file is 404; other org's file is 404 |
| Support session | content export under a grant | `refuse_under_support_grant` | 403 with a grant header |
| Capability off | feature still reachable | `require_capability` | 404 |
| Job stream | someone else's presigned URL | HMAC job id (`bulk_job_owned_by`) | user B with A's job id gets 404 |
| Task subject | raw export with no policy | `user_id is None` gives an error event (fail closed) | call task with `user_id=None` |
| `export_locked` | reveal through `reveal_original` | `resolve_export_reveal` | locked + reveal: PROFANITY absent (docx, pdf) |
| Owner reveal | non-owner reveal | `is_owner` recomputed in task | shared viewer + reveal: masked; owner + unlocked: original |
| Takedown in the gap | quarantined content | task re-check | quarantine after dispatch gives `error`, no object written |
| Unfinished scan | raw text | 409 at dispatch + task re-check | `redaction_status="processing"` |
| ORM write-back | masked text committed over the original | expunge in `masked_segments`; `session_scope` commits | text unchanged after the task |
| Logs | glyphs or text in logs | `fpdf` logger at ERROR; failures log type + counts | `caplog` contains none of the fixture words |
| Labels | oversized or odd input | `max_length=200`, `str.replace` templates, XML sanitizer | 422 on 201 chars; `{__class__}` rendered literally |
| PDF metadata | user identity | author empty, title = display name only | metadata has no email |
| Presigned object | lives past a takedown | `bulk/` 1-day lifecycle and the presign clamp, same as the bulk ZIP today | (documented residual) |
| Audit | silent reveal | `audit_unredacted_reveal(surface="transcript_document_export")` | event written on owner reveal |

**Vacuity guard:** every "PROFANITY absent" assertion on extracted DOCX/PDF text is paired with
`CLEAN_TEXT present` in the same output and with the unlocked case showing PROFANITY. Text
extraction that returns nothing must fail, not pass. #1277 (content-access audit) needs no extra
wiring if it hooks the export routes; both routes live under `files/`.

---

## 7. Frontend notes

- Light/dark: tokens only; any dark override uses `:global([data-theme='dark'])`
  (`styles/theme-parity.test.ts`). RTL: logical properties only. Check `ar` in the modal and menu.
- Bundle: no new npm dependency. The modal is lazy-loaded; the file route is not in the root
  closure. The root node (about 16.7 of 22.5 KB) and closure (about 128.8 of 139.3 KB) must show
  **no delta** in the `postbuild` budget output.
- Progress: the menu button shows a busy state with `documentExport.preparing` while the job runs.
  `completed` triggers an anchor click on the presigned URL, which already carries
  `Content-Disposition: attachment`.

---

## 8. Test plan (must fail first)

Red is proven on a `git archive HEAD` tree (root `CLAUDE.md`): `RED="$(mktemp -d -t
ot-redcheck.XXXXXX)" && git archive HEAD | tar -x -C "$RED"`, copy only the new tests in, run, and
expect red. Where HEAD lacks the module entirely (an ImportError is not evidence), prove red by
**mutating the fix out** in the redcheck tree after copying the new source in.

| # | Test | Red on HEAD / mutation |
|---|---|---|
| B1 | `api/test_transcript_export_redaction_gate.py`: `ALL_FORMATS += ("md",)`; unsupported example becomes `"exe"`; new `test_document_formats_rejected_on_sync_route` | HEAD: `md` gives 400, so all five md cases fail |
| B2 | `api/test_export_content_disposition.py`: Arabic + CJK filename on txt, md, summary export, subtitles; asserts 200 and `filename*=UTF-8''` | HEAD: **500** |
| B3 | `unit/test_transcript_export_markdown.py`: hand-written golden bytes (written **before** the code); hostile title round-trips through `yaml.safe_load`; escaping; null `recorded_date` omits both keys | mutation: write `title` unquoted gives a YAML error |
| B4 | `unit/test_transcript_export_fallthrough.py`: an unknown token injected into `VALID_FORMATS` raises, never returns CSV | HEAD: returns CSV |
| B5 | `unit/test_export_reveal_policy.py`: locked wins over reveal/owner/admin; unlocked owner reveals; non-owner does not | mutation: drop the locked branch |
| B6 | `api/test_document_export_redaction_gate.py` (docx, pdf x the five gate cases; task run eagerly by calling the task function with a fresh job id; DOCX text from `word/document.xml`, PDF via `pypdf`) | HEAD: route 404 |
| B7 | `api/test_document_export_access.py`: every row of §6 | HEAD: route 404 |
| B8 | `unit/test_transcript_document_layout.py`: JSON goldens (`MonospaceMeasurer`): per_page restart, continuous, none, keep-with-next blank slot, comment interleave, an over-long unspaced word, `too_long` cap | mutation: off-by-one in restart |
| B9 | `unit/test_transcript_document_pdf.py`: two renders byte-identical (fixed clock); `pypdf` page count and the first gutter number per page; Arabic fixture extracts its words and the layout marks the turn `rtl`; missing-glyph count logged without glyphs | mutation: drop `set_creation_date` makes bytes differ |
| B10 | `unit/test_transcript_document_docx.py`: normalized `document.xml` golden; `lnNumType` restart per mode; `w:bidi`/`w:rtl` on Arabic only; a control character in the text renders | mutation: remove the sanitizer gives `ValueError` |
| B11 | `redaction/test_export_redaction_paths.py`: the document task never writes masked text back | mutation: remove the expunge |
| B12 | `unit/test_export_plane_resolves_a_policy.py` must stay green with the new route (it reaches `resolve_effective_config` through the dispatch-time resolver; if the walker does not follow helpers, extend its resolver set, never `EXEMPT`) | n/a |
| F1 | `requestTranscriptExport.test.ts`: filename from `filename*` | HEAD: no field |
| F2 | `requestDocumentExport.test.ts` / `exportJobStream.test.ts`: completed gives URL; each `code` maps to its key; on error **no Blob is built** | new module |
| F3 | file page test: a 409 export shows `transcript.exportRedactionPending` toast | HEAD: `console.error` only |
| F4 | `menuIcons.convention.test.ts`, `npm run check:i18n`, `npm run test:audit` | parity fails if one locale is missing |

Gate hygiene: `cd backend && python3 ../scripts/audit-tests.py tests` at 0 open findings;
`npm run test:audit`; no wall-clock assertions anywhere.

---

## 9. Work packages (each independently mergeable)

| WP | Size | Content | Depends | Parallel? |
|---|---|---|---|---|
| **WP0** | S | `content_disposition.py` + 3 endpoint callers + minio refactor; SPA reads the server filename (F1). B2. No i18n | - | yes, with WP3a |
| **WP1** | M | Service split (turns/text/markdown), explicit csv branch (B4), `md` format, content type, gate `ALL_FORMATS` (B1, B3). Backend only | WP0 (same endpoint file) | yes, with WP3a |
| **WP2** | M | `resolve_export_reveal` + `reveal_audit` move (B5); schema, POST route, guard, capability, task, `code` on events; **DOCX renderer** (B6/B7/B10/B11 for docx) | WP1 | WP3a runs alongside on disjoint files |
| **WP3a** | M | `transcript_document/{model,scripts,layout}.py` + B8 (pure, no deps) | - | yes |
| **WP3b** | L | `pdf_renderer.py`, `fonts.py`, requirements x3, Dockerfiles x3 + fixture fonts, bench script, B9 + pdf rows of B6/B7; measure Latin with per-line shaping (target about 5 s) and the real font MB; SBOM regen | WP2, WP3a | no |
| **WP4** | M | Frontend: icons, menu, modal (lazy), prefs, `exportJobStream` + gallery migration, toasts, all i18n incl. dead-family deletion (F2-F4); browser check in light/dark, `en` + `ar` | WP2 contract (can start against it) | yes, with WP3b (disjoint tree) |
| **WP5** | S | Docs-site, CLAUDE.md files, CHANGELOG | WP3b, WP4 | - |
| **WP6** | S, optional | Markdown `include_summary` via `mask_summary` + `summary_export_service` (503 on masking failure, never a silent omission) | WP1 | may slip to v0.10.0 |

One writer per worktree; separate worktrees for WP3a and WP4 (root `CLAUDE.md` concurrency rule).
Locale files are touched **only** in WP4.

---

## 10. Rollout and compatibility (derived deployments)

- **Capability resolver.** A deployment that replaces the resolver must answer `exports.documents`.
  If it answers False, the POST is a 404 and the SPA hides the two entries. The worker never asks.
- **Frontend overlay.** No `$lib/cloud` seam change. An overlay capability store must pass the new
  key through (`seamContract.test.ts` unaffected). No root-closure growth, so no new bundle
  allowance.
- **Hooks / extra export surfaces.** Any added export route must call `resolve_export_reveal` and
  the shared builders. `test_export_plane_resolves_a_policy.py` fails a route that does not.
- **Images.** About +10 MB of Python packages and +22-25 MB of fonts per backend image (prod, lite,
  blackwell). An image built `FROM` ours inherits both. One built from its own Dockerfile must copy
  the `fonts` stage, or PDF jobs end with `fonts_unavailable` (fail loud, never a fontless PDF).
- **Licence.** fpdf2 is LGPL-3.0-only: keep it an unmodified, separately installed package and ship
  its licence; the Noto fonts need `OFL.txt` beside them.
- **Old clients.** Sync route behaviour is unchanged for the five formats except the header. The
  `filename*` parameter is additive.

---

## 11. Verification and exit criteria

1. `./scripts/run-dev-tests.sh --full` and `./scripts/run-integration-tests.sh` green, with output
   confirmed to contain the new test names (attribute the run).
2. `scripts/safe-precommit.sh run --all-files` and `... --hook-stage pre-push`.
3. On `./opentr.sh start dev --fresh docexport`: export a seeded English, Arabic, Japanese and Thai
   transcript in all 8 formats; open each PDF and DOCX (headed browser on `DISPLAY=:11`, and
   LibreOffice for DOCX); Arabic reads right to left with joined letters; page:line grid is 25 per
   page; per_page/continuous/none behave; Arabic filename downloads intact.
4. With `force_export_redacted` on: every format masked, reveal refused. With a pending scan: 409
   toast. With the file quarantined mid-job: error toast.
5. `scripts/bench-transcript-document.py` on the image: record 4 h English and Arabic timings in the
   PR (expected about 5 s and under 90 s).
6. Bundle gate output shows no root delta; `npm run check:i18n` green; `ar` checked visually in
   light and dark.
7. SBOM regenerated; the PR states the measured image-size delta.

Exit: #1278's three acceptance bullets met (DOCX determinism scoped as in §1.2); #564's transcript
scope met; the non-ASCII filename bug closed.

---

## 12. Open questions (owner)

| # | Question | Recommended default |
|---|---|---|
| Q1 | Accept fpdf2 (LGPL-3.0-only, REVIEW class like `ldap3`) for PDF? | **Yes.** The alternative (ReportLab, BSD) ships without Arabic/Hebrew PDF until a permissive bidi exists on PyPI |
| Q2 | Move #564 (transcript Markdown) to v0.6.0, leaving summary inclusion (WP6) on v0.10.0? | **Yes** |
| Q3 | Ship the 19.5 MB CJK font collection in every backend image, lite included? | **Yes.** zh/ja/ko are UI locales; without it CJK PDFs print boxes |
| Q4 | DOCX: native Word line numbering (editable, renderer-paginated), or a fixed-layout DOCX that mirrors PDF page:line? | **Native**; the PDF is the citable artifact |
