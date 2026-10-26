---
sidebar_position: 9
title: Document export (Word and PDF)
description: How Word and PDF transcript export works, the fonts and licences it ships, its limits, and how to troubleshoot it
---

# Document export (Word and PDF)

**Word Document (.docx)** and **PDF Document (.pdf)** turn a transcript into a printable document
with numbered lines, so a place in a recording can be cited as "page 3, line 12". Markdown
(.md) is the sibling format for note-taking tools. This page is for administrators and
integrators; the user-facing options are in
[Uploading Files: Export Options](../user-guide/uploading-files.md#export-options).

## How it works

Document export is **server-side and asynchronous**. The browser never builds a document, so the
redaction policy and the administrator export floor cannot be bypassed from the client.

1. **Choosing Word or PDF** in the transcript's **Export** menu opens **Document Export Options**
   (timestamps, speaker names, comments, line numbers, page size) and posts them to
   `POST /api/files/{file_uuid}/export/document`. The request carries the labels the document
   prints (page footer, comment label, and so on) already translated into the user's language,
   because the backend holds no translations. It answers `202` with a signed job id.
2. **A download-queue worker** (`download.prepare_transcript_document`) reads the transcript,
   re-checks quarantine and the redaction scan state, resolves the redaction policy for the
   requesting user, masks the text, lays out the pages, and renders the file. Masked text is never
   written back to the stored transcript.
3. **Progress and the result** arrive on the existing bulk-export stream
   (`GET /api/files/bulk-export-stream?job=...`, server-sent events). The worker uploads the file to
   the `bulk/` prefix of the processed-videos bucket, which expires after one day, and publishes a
   short-lived presigned link with a proper download filename. The result is also cached for a
   few minutes, so a browser that reconnects late still receives it, or the error.
4. **A second click while a job runs** for the same file and options returns the same job
   instead of queueing another.

The synchronous export route (`GET /api/files/{file_uuid}/export`) serves `txt`, `json`, `csv`,
`srt`, `vtt` and `md`. Asking it for `docx` or `pdf` returns `400` pointing at the POST route.

### Redaction and access

- The policy is resolved **when the job runs**, from the requesting user's id, never from a value
  carried in the request. An administrator's export floor (`force_export_redacted`) wins over any
  reveal a user asks for.
- If the user's policy masks text and the file's redaction scan is unfinished, the job fails with
  `redaction_pending` instead of exporting the original. If the policy cannot be read, it fails with
  `unavailable`.
- A non-owner administrator's reveal is not offered for documents.
- A file that was quarantined after the request was accepted is refused like a deleted one.
- Document export is refused during a support-access session, like every other export.
- Unredacted reveals are audited (surface `transcript_document_export`).
- A finished document is one `content.export` audit event (surface `transcript`, format `docx` or `pdf`), recorded by the worker once the file exists and only while content access auditing is on ([content access auditing](../user-guide/admin-panel.md#content-access-auditing)). It carries ids and the format, never a title or text, and has no client address because the worker has no request.

### Capability

`exports.documents` (default on) controls the feature. It is enforced on the server: with it off,
the POST route returns `404`, and the SPA hides the Word and PDF entries. The setting is part of
the capability map in `backend/app/core/capabilities.py`; no environment variable is involved.

## Page layout

A PDF page is a fixed grid of **25 numbered lines** (A4 and US Letter alike), so page and line
numbers mean the same thing on every reader's copy. A speaker's header line is never left alone at
the bottom of a page. Line numbers can **restart on each page**, **continue through the document**,
or be omitted. A Word document uses Word's own line numbering and pagination, so its numbers can
differ from the PDF. The footer prints **Page N of M**.

A transcript that lays out to more than **60,000 lines** is refused (`too_long`). The render has a
300 second soft time limit.

Measured on the development host for four hours of speech (3,600 segments), PDF only, no other
load: Latin about 5 seconds, Chinese 24, Japanese 35, Arabic 64 and Thai 98. Shaping scripts are
slower because each line goes through the text-shaping engine; Latin lines skip it.
`scripts/bench-transcript-document.py` reproduces the figures.

## Fonts and licences

PDF output needs fonts that cover the transcript's scripts. The backend images (`Dockerfile.prod`,
`Dockerfile.lite`, `Dockerfile.blackwell`) copy a pinned set of **Noto** fonts from the Debian
packages `fonts-noto-core` and `fonts-noto-cjk` into `/usr/share/fonts/opentranscribe/`, checked
against SHA-256 sums (`backend/third_party/fonts/SHA256SUMS`) at build time. A PDF loads only the
faces its text needs and embeds only the glyphs it uses. A missing font is a loud
`fonts_unavailable` error, never a PDF with blank boxes.

| Coverage | Fonts |
|---|---|
| Latin, Greek, Cyrillic | Noto Sans (regular, bold, italic) |
| Arabic | Noto Naskh Arabic |
| Hebrew, Armenian, Georgian, Ethiopic | Noto Sans Hebrew, Armenian, Georgian, Ethiopic |
| Indic scripts | Devanagari, Bengali, Gurmukhi, Gujarati, Tamil, Telugu, Kannada, Malayalam, Sinhala |
| Southeast Asian | Thai, Lao, Myanmar, Khmer |
| Chinese, Japanese, Korean | Noto Sans CJK (one collection; the Simplified, Traditional, Japanese or Korean face follows the file's language) |

A Word document **names** these fonts (it never embeds them). On a machine without Noto fonts, Word
substitutes another face; install Noto Sans, Noto Naskh Arabic and Noto Sans CJK there for an
identical look.

Chinese, Japanese, Korean and Thai text wraps between any two characters, because there is no
word dictionary and the rule against starting a line with a closing mark is not applied.

### Licences of what this feature adds

Only open-source licences that allow commercial use are shipped. The full text and attributions are
in the `NOTICE` file at the repository root.

| Component | Licence | Role |
|---|---|---|
| python-docx 1.2.0 | MIT | Writes `.docx` |
| fpdf2 2.8.9 | LGPL-3.0-only | Writes `.pdf` |
| uharfbuzz 0.57.0 (bundles HarfBuzz) | Apache-2.0 (HarfBuzz: Old MIT) | Shapes Arabic, Indic and Thai text |
| fontTools | MIT | Reads font tables |
| Noto fonts | SIL Open Font License 1.1 (Noto Sans CJK is copyright Adobe) | Glyphs |
| pypdf 6.21.2 | BSD-3-Clause | Tests only: reads text back from generated PDFs |

**fpdf2 is LGPL.** It is installed as an ordinary, unmodified Python package and never vendored or
patched, so a recipient can replace it with another version of the library, which is what the LGPL
asks of a combined work. Its licence text ships in the installed package. The SIL Open Font
License allows bundling the fonts with software and embedding subsets in documents; it forbids
selling the fonts by themselves, and the licence text (`OFL.txt`) is copied beside the fonts in
every image.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "The fonts needed to build this document are not installed on this server" | The backend image predates this feature, or a custom image dropped `/usr/share/fonts/opentranscribe/`. Rebuild or pull a current image. |
| "Content redaction has not finished for this file" | The user's policy masks text and the scan is still running. It clears when detection completes. |
| "The document export is temporarily unavailable" | The redaction policy could not be read. Check the database and the backend log. |
| The Export button stays on "Preparing document..." | The download-queue worker is not running or is busy. Check `./opentr.sh logs celery-download-worker`. |
| Word shows different page or line numbers than the PDF | Expected. Word paginates itself; cite from the PDF. |
