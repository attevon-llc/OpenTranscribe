---
sidebar_position: 4
---

# Documents

This guide walks through uploading and viewing documents. For a conceptual overview see
[Documents (feature)](../features/documents.md).

## Uploading a document

Open **Documents** in the main navigation and use the upload panel — pick a file or drag one in.
Supported formats: PDF, DOCX, XLSX, PPTX, ODT, ODS, EPUB, HTML, Markdown, plain text, CSV, TSV,
and common image formats (scanned pages route through OCR automatically). Legacy `.doc`/`.xls`/
`.ppt` and RTF need the optional document sidecars to be running — ask your administrator if an
upload of one of those is rejected.

Upload progress shows in the same floating progress panel used for media uploads. Once uploaded,
the document moves through **pending → processing → completed** while it's parsed and indexed;
you'll see its status update live.

## Browsing your documents

The **Documents** list shows every document you've uploaded, with its filename, status, and page/
chunk counts. Click a document to open it.

## Viewing a document

Each document has two tabs:

- **Original** — the source file as uploaded. PDF, HTML, Markdown, and plain-text files render
  directly in the page. For DOCX, PPTX, and XLSX (formats no browser can render natively), use
  the **Download** button instead.
- **Parsed Text** — the text OpenTranscribe extracted, in reading order, with page markers where
  the source document has pages. This is what search and chat actually see.

## Search and chat

Document content is indexed into the same search plane your transcripts use, so a hybrid search
or a chat question can return a passage from a document alongside a moment from a recording. See
[AI Chat](./chatting-with-transcripts.md) for how to ask questions across your library.

:::note Not yet built
Chat's citation cards currently point at a timestamp in a recording — document-specific citations
(page and section) are planned but not live yet. See [Documents (feature)](../features/documents.md#known-limitations)
for the full list of what's still in progress.
:::

## Automatic import from a watch source

If you have a [Watch Source](./watch-sources.md) configured, documents dropped into the watched
location are imported automatically, the same way media files are — no separate setup needed.

## Deleting a document

Open the document and use the delete action. This removes the file, its parsed text, and its
search-index entries.
