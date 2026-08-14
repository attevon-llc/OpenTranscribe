---
sidebar_position: 6
---

# Documents (Developer Guide)

Architecture and internals of document ingestion (issue #362, Stage 6 of #403). For usage see
the [user guide](../user-guide/documents.md); for the product-level view see the
[feature page](../features/documents.md).

## Data model

Migration `v393_add_document_tables` adds `document` and `document_chunk` as their own tables —
deliberately not a `media_file` discriminator. `media_file` is ~70 columns loaded whole by every
gallery page, and most of that is A/V-specific state (duration, waveform, speakers, diarization)
meaningless for a PDF. `file_facts` (v390) was the precedent for a narrow sidecar; `document`
goes further and is first-class, since unlike `file_facts` it isn't a derived artifact — it has
its own upload/list/detail/delete lifecycle.

- **`document`** — one row per uploaded file: identity/storage (`uuid`, `filename`,
  `storage_path`, `file_size`, `content_type`, `file_hash`), lifecycle (`status`, reusing
  `FileStatus` rather than a parallel enum), parse result (`parser`, `parser_version`,
  `parse_version`, `page_count`, `language`, `has_embedded_text`, `ocr_applied`, `ocr_pages`,
  `parse_warnings`), and a redaction trio (`redaction_status` / `redaction_model_version` /
  `redaction_coverage`) that mirrors `media_file`'s — present in the schema, **not yet populated
  by any code path**; see "Known gaps" below.
- **`document_chunk`** — the durable-storage half of `services/documents/chunking.py`, one row
  per retrieval chunk: `chunk_index`, `text`, `char_start`/`char_end`, `page`, `section_path`,
  `block_types`. `ON DELETE CASCADE` from `document` — the schema's second deliberate CASCADE FK
  (`file_facts.media_file_id` is the first) — because a chunk has no meaning once its document is
  gone. This is **not** the OpenSearch document shape; keeping them separate means a reindex
  reads these rows instead of re-parsing the source file, the same relationship
  `transcript_segment` has to the `transcript_chunks` index.

`watch_source_file.document_id` (migration `v394`, `ON DELETE SET NULL`, mirroring
`media_file_id`) links an auto-imported document back to the watch source that found it.

Models: `backend/app/models/document.py`. Schemas: `backend/app/schemas/document.py`.

## Parsing (`services/documents/`)

Built in an earlier stage as a working library with nothing calling it; this vertical is what
wires it up. Three parser tiers, selected by `DOCUMENT_PARSER_BACKEND` (`auto` / `slim` /
`serve` / `tika`) via `registry.get_parser_for()`:

| Tier | Where | Handles |
|---|---|---|
| `docling.slim` | In-process, in the CPU/redaction workers — **torch-free**, enforced by a subprocess-isolated test | Text-layer PDF, OOXML, ODF, EPUB, md/csv/html/txt |
| `docling.serve` | Sidecar (`--with-documents`), CPU-only | OCR + layout/table structure for scanned pages and images |
| `tika` | Sidecar (`--with-documents`), JVM | Legacy `.doc`/`.ppt`/`.xls`/RTF only |

`auto` prefers `serve` > `slim` > `tika`, health-checked with a cache so a bulk import of many
small files doesn't pay a health-check round trip per file. The IR invariant
(`text[block.char_start:block.char_end] == block.text`, checked by `validate_ir()`) is what makes
every downstream consumer — chunker, citations, viewer highlighting, redaction spans — safe to
address document text by character offset.

### The #69 fix

docling-slim's stock HTML backend silently dropped up to 88% of tables in table-heavy documents
(measured: 140 tables → 3 on a real government-report fixture) — two independent upstream defects
(a `<table>` nested under `<p>` that a non-HTML5-compliant parser never detaches, and tables that
are non-`<li>` children of a `<ul>`/`<ol>` that Docling's list walk skips outright). Fixed with an
`lxml`-based tree repair before Docling sees the bytes (`backends/docling_slim.py`). Both defects
were silent — the parse still "succeeded" — which is why they're recorded here rather than just
in a changelog: a parser that fails quietly is worse than one that errors.

## Chunking (`services/documents/chunking.py`)

Chunks on the IR's block structure, not Docling's own chunkers (which return **zero** chunks for
a DOCX containing a table — a known upstream bug). Reuses the transcript chunker's target word
count, read at call time from the same setting, so document and transcript chunk-length
distributions stay comparable — mismatched lengths would otherwise distort RRF fusion, which is
exactly what ranks the two planes against each other in one query. A table is never split: half a
table is not a retrievable unit.

## Celery task (`tasks/document_tasks.py`)

CPU-queue task: detect format → pre-parse safety containment (`services/documents/safety.py` —
zip-bomb/traversal/XXE/encrypted-PDF/page-cap guards) → parse → chunk → persist
`document_chunk` rows → index. Idempotent and safe to retry — a re-run does not duplicate chunks.
Progress reporting uses `services/documents/progress.py`'s `ShardLedger` (models "one coherent
percentage across N OCR shards"), wired to the same websocket notification path transcription
tasks use.

:::warning Never hold a DB session across parsing
Parsing is slow. A session held open across it queues every `ALTER TABLE` behind it and can hang
an Alembic upgrade mid-release — the single highest-risk pattern in this task, enforced by
`scripts/audit-session-lifetime.py`.
:::

## Indexing

Documents index into the **existing** v6 `transcript_chunks` OpenSearch index — not a second
index. The index already carries a `doc_type` discriminator (`chunk` / `digest`) and
plane-scoping helpers (`chunk_plane_clause`, `digest_plane_clause`, `file_plane_query`); a
document chunk is `doc_type: chunk` with `char_start`/`char_end`/`page` provenance instead of a
transcript chunk's timestamp range. A second index would fork retrieval and break the router —
see `backend/app/services/search/CLAUDE.md`.

## API

`backend/app/api/endpoints/documents.py`, registered under `/documents`:

| Route | Purpose |
|---|---|
| `POST /` | Upload (reuses the presigned/multipart flow `services/multipart_upload.py` already provides for media — no second upload mechanism) |
| `GET /` | List |
| `GET /{uuid}` | Detail (metadata only — see "Known gaps") |
| `GET /{uuid}/chunks` | Chunk list, for the parsed-text viewer and citation-jump |
| `GET /{uuid}/download` | Presigned URL to the original file |
| `DELETE /{uuid}` | Delete — sweeps OpenSearch and object storage, not just the row |

A real bug was found and fixed during frontend integration: the upload handler used
`db.flush()` before a slow storage write, then `db.close()` on an unrelated path rolled back the
uncommitted insert — the row silently vanished and a later `UPDATE` raised `StaleDataError`.
Fixed with an explicit `db.commit()`, matching the precedent in `files/upload.py`.

## Frontend

`frontend/src/routes/documents/` (list + detail) and `components/documents/*`. Deliberately
**separate** from the main gallery (`/files`), matching the backend's own separate-table
decision — the existing gallery has zero file-kind filtering and assumes `MediaFile` everywhere;
retrofitting that felt more invasive than a small parallel view. Upload reuses the existing
presigned-upload plumbing and the floating `UploadManager`/`UploadProgress` components; the
transcription-specific wizard steps (speaker count, model selection, etc.) don't apply and
weren't reused. The detail page's Original tab uses `<iframe src={presignedUrl}>` for
browser-renderable formats (no PDF library needed) and a download-only fallback for DOCX/PPTX/
XLSX. Citation-jump (`?chunk=N` → scroll + highlight) mirrors the existing transcript
`?t=<seconds>` mechanism exactly, just chunk-indexed instead of time-indexed — built now, ready
for chat to link into once citations become document-aware.

## Watch-source integration

`services/watch_sources/processing.py:ingest_prepared_file` now branches on detected format after
fingerprinting: media routes through the existing (extracted, behaviorally unchanged)
`_finalize_media_ingest`; everything else routes through the new
`document_ingest.finalize_document_ingest`, which mirrors the manual-upload endpoint (size gate,
storage path, `documents.parse` dispatch, gated on `auto_transcribe` the same way media is).
Within-source and cross-source deduplication (on `WatchSourceFile.imohash`) work identically for
documents. Cross-pipeline dedup against `media_file.imohash` stays media-only — `Document` has no
`imohash` column yet, a known residual rather than a silently half-built feature.

## Known gaps for follow-up work

- **Redaction is not wired.** The `redaction_status`/`redaction_model_version`/
  `redaction_coverage` columns exist and mirror `media_file`'s, but nothing populates or reads
  them for documents yet. Document text also lands in the OpenSearch index **unredacted**, same
  as transcripts — but transcripts get masked before LLM egress via
  `services/chat/redactor.mask_chunks()`, and that function is written for time-range-addressed
  transcript chunks. It has no `doc_type` branch. A document-aware masker (addressing by
  `char_range` instead of time range) needs to exist before document content is safe to send to
  an LLM the way transcript content already is.
- **Chat citations aren't document-aware.** `services/chat/citations.py` builds every citation
  around a `start_time` field. Document chunks retrieved by chat currently either don't get
  proper citations or get citations that don't make sense for a non-timestamped source — this
  needs its own citation shape (page + section, matching the detail page's `?chunk=N` mechanism).
- **No cross-linking.** No collection-level mixing of documents and recordings, no item-level
  "this document is the report referenced in this recording" relationship. Planned as the next
  phase; see the roadmap in `docs/market-research/market-and-roadmap.md`.
- **No speaker attribution in document text.** Diarization is audio-only.

## Testing

Unit (`backend/tests/unit/`): `test_document_parser_backends.py` (all three tiers, including the
#69 HTML table-recovery regression suite), `test_document_detect.py`, `test_document_ir.py`,
`test_document_chunking.py`, `test_document_parser_registry.py`, `test_document_safety.py`,
`test_document_parse_task.py`, `test_document_shard_progress.py`, `test_document_tika_tier.py`,
`test_document_slim_tier_is_torch_free.py` (subprocess-isolated, since another test may have
already imported torch and made an in-process check unreliable), `test_watch_source_document_ingest.py`,
`test_v393_migration_consistency.py`, `test_v394_migration_consistency.py`. API:
`backend/tests/api/test_documents.py`. Integration (needs the live stack):
`backend/tests/integration/test_document_indexing_opensearch.py`.

## Infrastructure

`docker-compose.documents.yml`, `./opentr.sh start dev --with-documents` (docling-serve on
:5197, Tika on :5198, both bound to `127.0.0.1` only — no auth, so they're not exposed beyond the
host). See root `CLAUDE.md`'s "Document parsing sidecars" section for the full detail, including
why the slim tier must stay torch-free and why raw bytes go to Tika untyped (an internal MIME
type sent as `Content-Type` gets treated as a detection override and can silently produce an
empty parse).
