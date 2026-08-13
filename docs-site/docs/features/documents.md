---
sidebar_position: 10
title: Documents (Planned)
description: The planned document knowledge base — formats, OCR, and mixed-collection chat
---

# Documents

:::danger Planned — none of this has shipped
Nothing on this page exists in OpenTranscribe today. You cannot currently upload a PDF, and the
gallery has no document lens. This page describes the **intended** shape of the document
knowledge base ([issue&nbsp;#362](https://github.com/attevon-llc/OpenTranscribe/issues/362),
sequenced as Stage&nbsp;6 of
[issue&nbsp;#403](https://github.com/attevon-llc/OpenTranscribe/issues/403)) so the design is
reviewable before it is built.

Numbers, formats and limits below are **plan values**. They are recorded here because they were
decided deliberately, not because they have been measured in a running system. Expect some to
change during implementation.
:::

## What it is for

OpenTranscribe knows what was *said*. A meeting rarely stands alone: there is a contract, a deck,
a statement of work, a scanned invoice. The motivating question for the whole feature is one
sentence long:

> *"Does what we agreed in the call match what the contract says?"*

Answering it means one library, one search index and one conversation covering both recordings
and documents — not a second product bolted alongside the first.

## Planned formats

| Format | Status in the plan |
|---|---|
| PDF (text layer) | v1 |
| PDF (scanned) | v1, via OCR |
| DOCX, PPTX, XLSX | v1 |
| CSV, Markdown, HTML, TXT | v1 |
| Images (PNG/JPEG and similar) | v1, via OCR |
| Legacy `.doc` / `.xls` / `.ppt`, RTF | **Only** with the optional Apache Tika fallback container. Without it the API returns a clear "convert to .docx or .pdf first" error rather than failing obscurely |
| Email (`.eml` / `.msg`) | A later phase, not v1 |

The parser is [Docling](https://github.com/docling-project/docling) (MIT). Format selection is
not guesswork: the `.docx`, `.xlsx`, `.pptx`, ODF and EPUB containers are all ZIP archives with
identical magic bytes, so the planned detector inspects the archive rather than trusting the
extension.

## OCR

Scanned pages are in scope **on day one**, not deferred — a document knowledge base that quietly
ignores every scanned contract is not one.

- **Engine:** RapidOCR (Apache-2.0), running PaddleOCR models on ONNX Runtime. Around 50–80&nbsp;MB,
  usable on CPU, faster on GPU.
- **When it runs:** never inline. If a page's extractable text layer falls below a threshold, the
  document is indexed from what text there is and the pages are queued for OCR, then re-indexed
  when they come back.
- **Why it is sharded:** a 500-page scan is split into 20-page units, so it becomes roughly 25
  one-minute jobs rather than a single 25-minute job that starves the queue behind it.
- **Per document control:** `auto` (OCR only when the text layer is thin), `force`, or `never`,
  with a page ceiling and a global on/off. These are planned as database-backed settings editable
  in the admin UI — following the existing convention, not new `.env` variables.
- **GPU manners:** transcription always wins. Document OCR is designed as the lowest-priority GPU
  consumer — it checks for free VRAM per shard, yields to everything else, and falls back to CPU
  rather than queueing behind a transcription job.

If the OCR service is unavailable, the plan requires the document to be marked as text-layer-only
rather than silently indexed as if it were complete. **Silent degradation is the failure mode the
design is most concerned with.**

## Planned parse limits

These exist because a document parser is an attack surface, not merely a feature.

| Limit | Planned value |
|---|---|
| Pages per document | 2,000 |
| Upload size | 256&nbsp;MB (deliberately far below the 15&nbsp;GB media limit — "a 15&nbsp;GB document is an attack, not a use case") |
| ZIP total uncompressed size | 512&nbsp;MB |
| ZIP compression ratio | 200:1 |
| ZIP member count | 5,000 |
| ZIP nesting depth | 1 |
| Password-protected PDFs | Rejected outright, not prompted for a password |
| OCR shard size | 20 pages |

XML entity expansion attacks (XXE, "billion laughs") are blocked at import time for every parser
path.

There is deliberately **no stated maximum chunk count per document**; the page cap is the bound
that matters.

## Mixed collections

Documents and recordings are planned to share **one** library, not two.

- The gallery gains **All / Media / Documents** lenses over the same grid, and the filter rail —
  collections, tags, dates — stays cross-type. Filter by a collection and you see both kinds,
  grouped with counts ("8 recordings · 23 documents").
- Collections, tags and sharing work identically for both.
- The chat context picker gains a **Documents** tab writing into the same selection as
  recordings, so one conversation can be scoped to a collection containing both.

The obligation that comes with mixing them: an answer must not quietly cover only half the
collection. *"Summarise the Acme collection"* over 8 recordings and 23 documents has to say so,
and aggregate answers are expected to report coverage per type — *"mentions X in 9 recordings and
4 documents"*.

## Citations, per type

Citations are the feature's trust surface, and a document has no timestamp to jump to.

| Source | Citation points at |
|---|---|
| Recording | The recording, opened **at that second** in the player |
| Document | The document, opened at **that page and passage**, with the page and section shown on the card |

A document citation that deep-links to `0:00` is treated as a **failure of the feature**, even if
the prose answer is correct — it is exactly the kind of plausible-looking wrong link that teaches
people to stop clicking citations. For the same reason, document excerpts are planned to omit
speaker and timestamp fields entirely rather than carry `"Unknown"` and `0.0`, so *"what did Dana
say about pricing?"* cannot be answered from a PDF.

The assistant also gets document-specific instructions, because two of the transcript rules are
actively wrong for a contract: "speech is messy, do not smooth over hesitation" and "attribute
every statement to a speaker". For documents it is told to cite page and section, quote clause
text verbatim, and — when a document and a recording disagree — **report both and say which is
which**.

## Deliberately not planned

Recorded here because "we considered it and said no" is more useful than silence:

- **No knowledge graph / GraphRAG.** The decision is evidence-based rather than aesthetic: on a
  39,190-artifact enterprise benchmark, retrieval over structured metadata scored 32.96 and plain
  hybrid search 20.61, while a GraphRAG variant scored 10.31 — at an indexing cost two to three
  orders of magnitude higher.
- **No chunk-level metadata facets.** Document-level metadata measures as a clear win; the same
  study measured chunk-level metadata as a small *negative*.
- **No LLM-generated per-chunk context in v1.**
- **No second search index.** Documents join the existing one through an additive mapping change.
- **No speaker features for documents** — no diarization, no voiceprints, no speaker suggestions.
  Timestamps, waveform and subtitle export are replaced by page, section and passage anchors.
- **No off-the-shelf RAG server.** Every candidate would duplicate four subsystems OpenTranscribe
  already owns to supply the one it lacks, and the three that could talk to our OpenSearch are
  licence-blocked for an AGPL project (branding riders, multi-tenant prohibitions, or enterprise
  code shipped inside the official images).
- **PDF page → chunk scroll sync.** Not feasible with an embedded browser viewer; chunk → page is.

## Where this sits in the plan

Documents are Stage 6 of the corpus-scale RAG programme, deliberately after the retrieval work
they depend on: the evaluation harness, the summary/digest tier, and the citation machinery all
have to exist first, or documents would be built on rails that are still moving. See
[How Corpus-Scale RAG Is Designed and Validated](../developer-guide/rag-design-and-validation.md)
for that sequence and the measurement discipline behind it.

## Related

- [AI Chat (RAG)](./rag-chat.md) — the feature documents will join
- [RAG Evaluation Methodology](../developer-guide/rag-evaluation.md)
- [Content Redaction](./content-redaction.md) — planned to apply to documents on the same terms
