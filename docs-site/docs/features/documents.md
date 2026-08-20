---
sidebar_position: 10
title: Documents
description: Upload PDFs, Office files, and more — parsed, chunked, and searchable alongside your transcripts
---

# Documents

OpenTranscribe knows what was *said*. A meeting rarely stands alone: there is a contract, a
deck, a statement of work, a scanned invoice. Documents ([issue&nbsp;#362](https://github.com/attevon-llc/OpenTranscribe/issues/362),
Stage&nbsp;6 of [issue&nbsp;#403](https://github.com/attevon-llc/OpenTranscribe/issues/403))
bring that outside material into the same library, the same search index, and the same chat as
your recordings.

:::note What's here today vs. what's still coming
Upload, parsing, chunking, search indexing, and a dedicated viewer all work today. Two things
from the original design are **not built yet** — see "Known limitations" below before you rely
on either:
- **Documents don't yet share a library view with recordings.** They live at their own
  **Documents** section, not as a lens inside the main gallery. Collections and tags do not yet
  mix media and documents.
- **Content redaction has not been extended to document text yet.** Transcripts get PII/
  profanity/toxicity masking; documents currently do not. Don't upload documents containing
  sensitive information you'd rely on redaction to protect.
:::

## What it does

Upload a PDF, Word document, spreadsheet, presentation, web page, or plain-text file. It is
parsed into clean text, split into chunks the same way your transcripts are, and indexed into
the **same search index** transcripts use — so a hybrid search or a chat question can surface a
passage from a document exactly like it surfaces a moment from a recording.

## Supported formats

| Format | Notes |
|---|---|
| PDF | Text-layer PDFs parse directly; scanned/image-only pages route through OCR |
| DOCX, XLSX, PPTX | Microsoft Office (OOXML) |
| ODT, ODS | OpenDocument (LibreOffice) |
| EPUB | |
| HTML | |
| Markdown, plain text, CSV, TSV | |
| PNG, JPEG, GIF, BMP, TIFF, WEBP | Images — always go through OCR |
| Legacy `.doc` / `.xls` / `.ppt`, RTF | **Only** with the optional Apache Tika sidecar (`--with-documents`). Without it, these formats are rejected with a clear error rather than failing obscurely |

Format detection inspects file content, not the extension — the `.docx`/`.xlsx`/`.pptx`/ODF/EPUB
containers are all ZIP archives with identical magic bytes, so a mislabeled or renamed file is
still classified correctly.

## How parsing works

Parsing runs in one of three tiers, chosen automatically:

- **In-worker (always available)** — handles the text-layer formats above without any extra
  container. This is the fast path for the large majority of uploads.
- **OCR sidecar (`--with-documents`)** — a CPU-only [Docling](https://github.com/docling-project/docling)
  service that handles scanned pages and images. It only runs when a page's extractable text
  falls below a threshold, so a normal text-based PDF never pays the OCR cost.
- **Legacy sidecar (`--with-documents`)** — Apache Tika, for the pre-2007 Office formats and RTF
  that nothing else can read.

If a document needs OCR and the sidecar isn't running, you get a clear, actionable error rather
than a silently incomplete parse — a parser that quietly drops content is worse than one that
says so.

## Chunking

Parsed text is split into chunks using the same target size your transcript chunks use, so
document and transcript results rank comparably against each other in search and chat (mismatched
chunk lengths would otherwise skew ranking toward one or the other). Tables are never split
across chunks — a table's header row and its data always land together, because half a table is
not a useful search result.

## Viewing a document

Each document has its own page with two views:

- **Original** — PDF, HTML, Markdown, and plain-text files render directly in the browser.
  Office formats (DOCX/PPTX/XLSX) don't have a browser-native renderer, so you get a **Download**
  button instead of an inline preview for those.
- **Parsed Text** — the extracted text in reading order, with page markers where the source has
  pages. Linking directly to a specific chunk (`?chunk=N`) opens the document and scrolls to and
  highlights that passage — the same mechanism a chat citation would use, once chat citations
  become document-aware (see "Known limitations").

## Automatic import (watch sources)

If you use [Watch Sources](./watch-sources.md) to auto-import media, documents dropped into the
same watched folder, bucket, or share are picked up too — they no longer get skipped as an
"invalid type." The same duplicate-detection (content fingerprinting, within-source and
cross-source) applies.

## Known limitations

Recorded here deliberately, because a documentation page that only lists what works is
misleading by omission:

- **No redaction yet.** `redaction_status` exists on every document row, but nothing currently
  populates it — PII/profanity/toxicity detection has not been extended from transcripts to
  document text. Treat document uploads as unredacted.
- **Chat citations aren't document-aware yet.** Document text is indexed into the same search
  plane transcripts use, so it *can* surface in a chat answer's retrieved context — but the
  citation-building code is currently written for timestamp-addressed transcript excerpts. A
  citation pointing at a document passage (page and section, the way a transcript citation points
  at a timestamp) is planned but not yet built.
- **No cross-linking between documents and recordings.** A document that's clearly "the contract
  for this meeting" isn't yet something you can express in the app — no shared collections
  across both types, no "referenced by" relationship. This is the planned next phase.
- **No speaker attribution in document text.** Diarization is audio-only; a quote in a document
  ("Smith said...") isn't resolved to a `Speaker` record.

## Related

- [Watch Sources](./watch-sources.md) — auto-import now includes documents
- [AI Chat (RAG)](./rag-chat.md) — the search plane documents share with transcripts
- [Content Redaction](./content-redaction.md) — not yet extended to documents; see above
