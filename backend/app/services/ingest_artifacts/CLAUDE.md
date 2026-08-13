# app/services/ingest_artifacts — the deterministic, no-LLM summary tier

## Purpose

Turns a finished transcript into three artifacts and stores them in Postgres:

| Artifact | What it is | Who reads it |
|---|---|---|
| `facts` | exact statistics — duration, roster, talk-time split, turn count, longest monologue | Stage 4's aggregation path; the no-LLM collection overview |
| `digest` | a **sectioned extractive** summary; every sentence is verbatim source text with provenance | Stage 3 indexes it as `doc_type: digest`; Stage 4 cites it |
| `keyphrases` | top-20 stopword-bounded phrases, degree/frequency scored | facets, the no-LLM overview |

**Nothing here calls an LLM, loads a model, or touches OpenSearch.** That is the point:
#403 **D6** makes the `LLM_PROVIDER`-empty deployment first class, and this package is what
gives it a summary tier at all. If you find yourself reaching for a provider in this
directory, you have misread the stage.

## Key files

- `service.py` — the orchestrator. `generate_file_artifacts(db, file_id, force=False)`
  reads segments, builds all three, upserts `file_facts`. **Flushes, never commits** — the
  caller owns the transaction so a reindex batch can do many files in one.
- `digest.py` — sentence extraction with provenance, contiguous sectioning, selection.
- `textrank.py` — TF-IDF + PageRank, numpy only. Also owns `tokenize` / `stopwords_for`.
- `keyphrases.py` — RAKE-shaped scoring. Reads the same stopword set.
- `facts.py` — the statistics payload. Turn-level stats are computed here; per-speaker
  talk time comes from `utils/transcript_builders.compute_speaker_stats`.
- `provenance.py` — the D3 tagged union and its validator.
- `sizing.py` — **the measured embedding window** and everything derived from it.
- `index_mapping.py` — the target OpenSearch mapping for Stage 3. **Defined, not applied.**

Task: `app/tasks/ingest_artifacts_task.py` (`artifacts.generate_file_facts`, **nlp** queue),
dispatched fire-and-forget from `transcription/postprocess.enrich_and_dispatch`.
Model + migration: `app/models/file_facts.py`, `alembic/versions/v389_add_file_facts.py`.

## The measured number, and why it is not the one in the plan

`all-MiniLM-L6-v2` **as OpenSearch ML Commons deploys it truncates at 128 wordpieces**, not
the ~256 the #383 review addendum (G8) assumed. Measured three ways against the isolated
stack; reproduce with `scripts/measure_embedding_window.py`:

| Probe | Appending stops changing the vector at |
|---|---|
| single-wordpiece filler | 126 words |
| rare multi-wordpiece words | 35 words (≈126 pieces) |
| real QMSum transcript text | 92 words → **1.37 wordpieces/word** |

Nothing in the model's ML Commons record states this (`all_config` advertises
`max_position_embeddings: 512`), so it is only knowable by asking the model. The
consequence: a 150–200 word digest would have had **more than half its text absent from its
own vector**, silently — a truncated embedding is a perfectly valid embedding of the wrong
text. Hence sectioned digests, ~55 target / 70 hard-cap words each, derived in `sizing.py`
rather than chosen. `tests/integration/test_embedding_window_truncation.py` re-measures
against the live model, with a negative control that fails if the window ever grows.

## Conventions / patterns

- **Determinism is a correctness property.** This output feeds Stage 3's index, so a digest
  that varies run to run makes every phase-over-phase retrieval delta in the epic partly
  noise. Concretely: vocabularies are `sorted()` lists, never set iteration order
  (`PYTHONHASHSEED` is unpinned in the workers); power iteration has a fixed cap and a
  uniform start; score ties break on source position; **every `set` → `list` is sorted.**
- **Segments arrive in a total order** — `(start_time, end_time, id)`. `start_time` alone is
  not one (3,072 tie groups over 6,152 segments on the eval corpus), and every artifact here
  is a function of adjacency. `load_ordered_segments` is the only reader; the pure functions
  take plain dicts and deliberately **do not sort**, so a caller reading in a partial order
  fails visibly instead of silently producing a different digest (#433).
- The pure builders (`build_facts`, `build_digest`, `extract_keyphrases`) touch no ORM and no
  I/O — they are testable without a database, and `build_artifacts` composes them.
- Provenance is validated at the producer. A malformed provenance surfaces downstream as a
  citation that points nowhere, which is the silent-wrong-answer class this epic keeps hitting.

## How it connects

- **Transcription** → `postprocess.enrich_and_dispatch` → `dispatch_file_facts`. Not gated on
  an LLM provider, unlike every other enrichment task on the nlp queue.
- **Stage 3 (reindex)** must call `generate_file_artifacts` **inside** `reindex_transcript`
  (addendum **G1**): `delete_transcript_chunks` is an unqualified delete and every rebuild
  trigger would otherwise destroy digests permanently. The `source_fingerprint`
  short-circuit is what makes calling it on every reindex cheap — an unchanged transcript
  costs a SHA-256, not a TextRank.
- **Speaker rename (#405)** needs no separate trigger: the fingerprint covers the *resolved*
  display name, so a rename invalidates the row by itself.
- **Redaction**: digest sentences are verbatim segment text with segment ids attached, so
  `redactor._mask_from_segments` can re-mask them from the cached spans. Any path that sends
  digest text to an LLM still has to mask it — the artifacts are stored **unredacted**, the
  same as `transcript_segment.text` and the chunk index.

## Gotchas

- **`file_facts` is a sidecar table, not columns on `media_file`.** The #383 plan text says
  `MediaFile.file_facts` / `MediaFile.extractive_digest`. Deviating was deliberate:
  `media_file` is ~70 columns and is loaded whole by every gallery page and permission
  subquery; Stage 3 needs a narrow "which digests are stale" scan; and #362 puts documents
  in `media_file` with `kind='document'`, so the sidecar is already the document analog
  (#403 comment 1, Nuance 3) with `char_range` provenance instead of `segment_ids`.
- **Keyphrases deviate from the plan too.** The plan specifies corpus-relative TF-IDF against
  a document-frequency table from OpenSearch. That would couple artifact generation to index
  availability — breaking the 100% gate whenever OpenSearch is down, for the same reason the
  plan itself rejects reading chunk vectors back out — and make the artifact a function of
  *when* it was generated, so two identical files ingested a month apart would differ. A
  corpus-relative **re-ranking** pass is still open to Stage 3.
- **An empty digest is a valid outcome**, not a failure: a ten-second clip, or a transcript
  that is entirely backchannels, has no sentence of `MIN_SENTENCE_WORDS`. Callers must not
  treat `sections == []` or a `None` return (no segments at all) as an error.
- `generator_version` is `"{facts}.{digest}.{keyphrases}"` schema versions. Bump the relevant
  one when an algorithm changes and the next reindex regenerates; forget to, and a mixed-
  vintage corpus is being measured as if it were one thing.
- `index_mapping.py` **applies nothing**. `tests/unit/test_digest_index_mapping.py` asserts
  `_INDEX_VERSION` is still 5, so the mapping and the bump cannot drift apart before Stage 3
  lands them together.
