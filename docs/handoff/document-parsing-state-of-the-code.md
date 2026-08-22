# Document parsing — state of the code, 2026-08-14

Companion to `document-ingestion-vertical.md`. That file is scope and process; **this one is
what already exists in the tree**, because the published plan predates it.

## Read these first, in this order

1. **Issue [#362](https://github.com/attevon-llc/OpenTranscribe/issues/362)** — the product
   plan. ~11k chars. Documents as first-class citizens of the same library, same index, same
   chat. Ingested from upload, mounted shares (SMB/S3/local) and URL capture.
2. **The design gist** —
   https://gist.github.com/attevon-admin/54463b926212ea5ceff59fcdf0da9cbb
3. **Issue #403** — the corpus-scale orchestrator this parser was built under (Stage 6a), and
   its comment thread for what has changed since.
4. Then this file, which reconciles the plan against the code.

**Where the plan is stale:** it was written before index **v6**, before the migration
renumbering, and before the redaction/session hardening of 2026-08-13. Those three things
change the integration surface, not the parsing design. Details below.

## What is already built (Stage 6a) — 9 modules, ~1,750 lines

`backend/app/services/documents/`. **The parsing half is complete and tested.** It is imported
by exactly two modules (`core/constants.py`, `search/chunking_service.py`), neither of which
ingests anything — so it is a working library with no product attached.

| Module | Lines | Role |
|---|---|---|
| `registry.py` | 199 | **The single branch point.** Parser selection by policy |
| `ir.py` | 332 | The intermediate representation — one canonical string, blocks indexing into it |
| `chunking.py` | 317 | IR → `document_chunk` rows. **Where 6a ends and 6b begins** |
| `detect.py` | 266 | Format detection — magic bytes, ZIP disambiguation, text heuristic |
| `progress.py` | 217 | One coherent percentage for a document that OCRs in N shards |
| `safety.py` | 166 | Pre-parse containment for hostile documents |
| `types.py` | 141 | Inputs, options, and the typed error hierarchy every backend raises |
| `protocol.py` | 48 | The `DocumentParser` contract |
| `backends/` | — | `docling_slim.py`, `docling_serve.py`, `tika.py` |

### The tiering, and the policy switch

`DOCUMENT_PARSER_BACKEND` selects it:

| Value | Behaviour |
|---|---|
| `auto` | sidecar when `DOCUMENT_PARSER_URL` health-checks, else slim, else tika |
| `slim` | in-worker only. A document needing OCR fails with a **typed, actionable** error |
| `serve` | sidecar only. Unreachable is a **retryable** failure, not a parse failure |
| `tika` | Tika only — the legacy-format escape hatch, for testing that tier |

Health results are cached (`_HEALTH_TTL_SECONDS`): without it, `auto` pays an HTTP round trip
per document and a bulk import of 50 small PDFs spends more time health-checking than parsing.

**The registry is the only place allowed to branch on backend identity.** If you write
`if parser.name == "docling.serve"` anywhere else, the branch belongs in the registry. This
mirrors `services/asr/factory.py` and `services/diarization/factory.py` — adding a provider is
a module plus one registry entry, never an edit to a call site.

### The IR invariant — the single most important thing in the subsystem

```
text[block.char_start:block.char_end] == block.text
```

`validate_ir()` asserts that, plus ordering and non-overlap, **at the producer**. A backend
that cannot satisfy it fails the parse rather than shipping offsets that point at the wrong
words.

That matters because **five** consumers address document text by character offset into one
string: the chunker, chat citations, the viewer's highlight anchors, redaction spans, and the
`char_range` provenance arm in `services/ingest_artifacts/provenance.py`. A citation into the
wrong passage is the silent-wrong-answer class this whole epic keeps finding — so the
invariant is checked where it can still fail loudly.

Blocks are built via `IRBuilder`, never by hand: hand-computing offsets in three backends is
three chances to be off by a separator's length, and it would only fail for documents nobody
tested.

`IR_VERSION = 1` is persisted on `document.parse_version` and stamped into the artifact key, so
a reparse sweep is a version comparison and old artifacts stay readable until collected.

`BLOCK_TYPES` is a **closed** vocabulary — the chunker breaks on `heading` level and the indexer
drops `page_header`/`page_footer` from embedded content (running heads are exactly the noise
olmOCR-bench's 753 `absent` assertions exist to catch). An unknown type would fall silently
through both.

### The chunker's four decisions

Each is deliberate and each has a non-obvious alternative that was rejected:

- **Chunk on the IR's block structure, not Docling's chunkers.** `docling#3335` has the DOCX
  chunkers returning **zero chunks** when the document contains a table. Beyond the bug: a
  chunker we do not control produces a length distribution we cannot hold equal to the
  transcript plane's, and **heterogeneous chunk lengths distort RRF** — which is precisely what
  fuses documents against transcripts in one query.
- **Reuse the transcript chunker's target size, read at call time.** Two settings that must
  agree are one that will not.
- **A table is never split.** Half a table is not a retrievable unit — the header row carrying
  the column names would land in a different chunk from the values.
- **Every chunk carries `char_start`/`char_end`.** The `char_range` arm of #403's **D3**
  provenance union, so a document digest joins the summary tier with no second addressing
  scheme.

`chunking.py` produces **durable storage only** — dicts destined for `document_chunk` rows. It
never touches OpenSearch, never embeds, never decides a mapping. **That is the seam you pick
up.**

### Security already handled — do not undo it

`defuse_xml()` runs at **package import**, not per-backend. Every OOXML part is XML and the
parsers reach for stdlib readers; an XXE in a crafted `document.xml` does not announce itself.
Package-level placement is the only one that covers a backend added later whose author didn't
think about it.

`safety.py` is pre-parse containment for hostile documents. `types.py` carries the typed error
hierarchy — `DocumentEncryptedError`, `DocumentTooLargeError`, `DocumentUnsafeError`,
`DocumentEmptyError`, `DocumentUnsupportedError`, `DocumentParserUnavailableError`. **Use them.**
The distinction between "could not parse" and "could not run the parser" is the same
unavailable-vs-failed split that produced six defects elsewhere in this epic: one is retryable,
the other is not, and collapsing them breaks either retries or user-facing accuracy.

## What changed since the plan was published

1. **Index v6 exists and is two-plane.** `transcript_chunks` carries a `doc_type` discriminator
   (`chunk` / `digest`) with helpers `chunk_plane_clause()`, `digest_plane_clause()`,
   `file_plane_query()`. **Index documents into it — do not create a third index.** A separate
   index forks retrieval and breaks the router. `backend/app/services/search/CLAUDE.md`.
2. **Migration numbering moved.** The plan reserved v377/v378; both are long gone. Current
   high-water is **v392** on the RAG/chat branch. **This lane is reserved v393–v399** — see the
   three-lane table in `backend/alembic/CLAUDE.md`. ⚠️ **Update (2026-08-19):** `master`
   published `v393_add_overlap_timing_columns` before this lane merged, forking the chain
   exactly the way the reservation warns about; the three document-plane revisions were
   renumbered to `v394`–`v396` (see "Renumbering note 3" in `backend/app/db/CLAUDE.md`), so the
   lane's remaining reservation is `v397`–`v399`.
3. **The redaction contract tightened, and it applies to documents.** The chunk index stores
   text **UNREDACTED** by design. Since 2026-08-13 there is now: per-file detector coverage
   (`media_file.redaction_coverage`, v392) that both LLM egress paths enforce; masking that
   fails **closed**; and export paths that thread policy through Celery. **Document text lands
   in the same index and inherits all of it.** A document chunk reaching an LLM unmasked is the
   same defect class, and `services/redaction/CLAUDE.md` documents the rules.
4. **Session-lifetime is now enforced by a gate.** Parsing is slow; holding a DB session across
   it queues every `ALTER TABLE` behind you and hangs Alembic. `scripts/audit-session-lifetime.py`
   will catch it, including `session_factory` / `_short_session` openers.
5. **`session_scope` commits on exit.** Mutating loaded ORM objects in a Celery task persists
   the mutation — this nearly destroyed transcripts in the export path (fixed `693a16c1` by
   expunging before mutating). Directly relevant if your parse task mutates anything it loaded.

## Decisions already taken — do not relitigate without a reason

- **Docling MIT, tiered: slim in-worker + sidecar.** OCR from day one.
- **Every parser/RAG-server rejection was a LICENCE decision, not a quality one.** If you
  reconsider a rejected component, check its licence first — that is why it lost.
- **No knowledge graph.** Explicitly out of scope.
- **Digest sizing derives from a *measured* 128-wordpiece embedding window**, not the plan's
  assumed 256. `services/ingest_artifacts/` and `scripts/measure_embedding_window.py`.

## The live defect to fix before measuring anything

**#69 — docling-slim's HTML backend silently drops ~85% of a table-heavy document
(140 tables → 3).** Silent, so it reads as success. Any document-retrieval quality number taken
before this is fixed describes a parser discarding most of its input.

## Open questions for your plan

These are genuinely undecided; the handoff has a recommendation only for the first.

1. **`document` table vs a `media_file` discriminator.** Recommendation: its own table
   (`media_file` is ~70 columns, loaded whole by every gallery page, and mostly A/V state).
   `file_facts` (v390) is the precedent for a narrow sidecar. Write down whichever you choose.
2. **Which Celery queue** the parse task runs on, and whether OCR shards fan out. `progress.py`
   already models "one coherent percentage across N shards", which implies fan-out was intended.
3. **Whether documents get a digest** (the summary tier) at ingest, as transcripts do via
   `services/ingest_artifacts/`. D3's `char_range` provenance was designed to allow it.
4. **Watch-source integration shape** — `services/watch_sources/` has a 3-layer imohash dedup
   that must not be bypassed; it has its own CLAUDE.md.
5. **URL capture** for documents (the plan mentions it) — likely reuses
   `media_download_service`'s shape, but that is yt-dlp-specific and may not transfer.
