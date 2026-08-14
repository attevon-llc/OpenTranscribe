# app/services/ingest_artifacts — the deterministic, no-LLM summary tier

## Purpose

Turns a finished transcript into three artifacts and stores them in Postgres:

| Artifact | What it is | Who reads it |
|---|---|---|
| `facts` | exact statistics — duration, roster, talk-time split, turn count, longest monologue | Stage 4's aggregation path; the no-LLM collection overview |
| `digest` | a **sectioned extractive** summary; every sentence is verbatim source text with provenance | Stage 3 indexes it as `doc_type: digest`; Stage 4 cites it |
| `keyphrases` | top-20 stopword-bounded phrases, degree/frequency scored | facets, the no-LLM overview |

…and a fourth thing that is not an artifact but belongs here for the same reason:
**`recorded_date`** — when the recording actually happened — resolved from three
deterministic sources with **no LLM** (`recorded_date.py`, `date_sources.py`,
`recorded_date_service.py`). See "The recorded date" below.

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
- `index_mapping.py` — the OpenSearch mapping, id scheme and document builder for the digest
  plane. Defined here in Stage 2, **applied by Stage 3** (`_INDEX_VERSION = 6`); Stage 3
  imports every name in it rather than restating any of them.

Task: `app/tasks/ingest_artifacts_task.py` (`artifacts.generate_file_facts`, **nlp** queue),
dispatched fire-and-forget from `transcription/postprocess.enrich_and_dispatch`.
Model + migration: `app/models/file_facts.py`, `alembic/versions/v390_add_file_facts.py`.

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
- **Indexing (index v6)** calls `generate_file_artifacts` from
  `search/indexing_service._index_digest_plane`, which runs on the per-file index path —
  i.e. inside `reindex_transcript` as addendum **G1** requires, and also on first ingest,
  so a file is never chunked without its digest. `delete_transcript_chunks` is an
  unqualified per-file delete and every rebuild trigger routes through it, so anything not
  regenerated there is destroyed permanently. The `source_fingerprint` short-circuit is what
  makes calling it on every reindex cheap — an unchanged transcript costs a SHA-256, not a
  TextRank — and it opens its **own** session, because its callers are a Celery task holding
  one over a batch of files and an API path holding none.
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
- `index_mapping.py` still **writes nothing** — it builds dicts. Stage 3 applied it:
  `_INDEX_VERSION` is assigned *from* `TARGET_INDEX_VERSION`, and
  `tests/unit/test_digest_index_mapping.py` (retargeted, not deleted) asserts the live mapping
  contains every pinned field. The invariant it guards was never "Stage 2 has not shipped" but
  "the mapping and the version never disagree": a field without a bump reaches fresh installs
  only, a bump without the field costs every deployment a reindex.
- **A rename does not yet reach the INDEXED digest.** The fingerprint invalidates the
  `file_facts` row, so the next reindex regenerates it — but `rename_propagation_task` rewrites
  the chunk plane only, so until then a digest document's `speakers` array and its
  `embedding_text` participant header still name the old speaker. Its `content` is unaffected
  (digest sentences are verbatim transcript text, which does not contain the display name), it
  carries no single-valued `speaker` field, and it is excluded from facets and from retrieval,
  so nothing user-visible is wrong today. Stage 4 turns the digest leg on and must close this.

## The recorded date (#403 R7) — and why it is five columns, not one

Every date-scoped question used to filter `upload_time`, so a 2019 meeting imported today
counted as 2026. Measured on the eval corpus: `upload_time` had **one distinct value across
all 432 files** while the meetings spanned a year, and R7 ("how many meetings in January 2025
discussed X") scored 0/4 for that reason alone.

Three deterministic sources, in `date_sources.py`, plus a seam for a fourth:

| Source | Reads | Notes |
|---|---|---|
| `container` | `media_file.creation_date` | ffprobe/exiftool. For a row with no media (the eval injector) the corpus record plays this role |
| `filename` | `media_file.filename` | `2024-03-15_standup.mp4`, `Meeting Mar 15 2024.m4a`, … |
| `transcript` | the **opening** turns | The source unique to us — we have the words |
| `llm` | *nothing yet* | Enum value reserved so Stage 7 needs no migration |

- **Every extractor declines rather than guesses, and the refusals are the behaviour.**
  `03/04/2024` is 3 April or 4 March depending on a locale nobody told us, so it returns
  `None`. A spoken date needs a **deictic anchor** ("today is", "recorded on") *and* to be in
  the opening turns, or "the deadline is March the fifteenth" dates the meeting to a deadline.
  "It's Tuesday the fifteenth" is refused outright — completing it would mean assuming the
  recording happened near its upload, which is the assumption this whole change removes.
- **A date never travels without its source.** `ck_media_file_recorded_date_provenance` makes
  a bare date unrepresentable in the database, `Resolution.source` has no `None`, and the
  Pydantic validator on `schemas.media.MediaFile` assembles `recorded_date_provenance` on
  *every* serialisation path. That last one is not tidiness: there are **three** paths that
  serialise a MediaFile and hand-wiring covered two — `PUT /files/{uuid}` returns the ORM row
  straight through `response_model` and shipped an unattributed date until a test caught it.
- **Precedence is policy, confidence is a hint, and they are separate.** `PRECEDENCE`
  (`core/enums.py`) decides between sources; confidence only orders forms *within* one and is
  never blended with the conflict flag — a contested high-confidence value must stay
  distinguishable from an uncontested mediocre one.
- **Losing candidates are kept** in `recorded_date_candidates`. Sources legitimately disagree
  (a recording made on the 14th about the 15th's meeting is ordinary), so a conflict is
  surfaced to the user rather than settled by whichever branch ran first.
- **`recorded_date_locked` makes a user's correction permanent.** `resolve_for_file` returns
  early on a locked row; only an explicit `force=True` overrides it, and no automatic path
  passes it. Clearing the date unlocks and re-enables resolution — a mistaken edit has to be
  retractable, and a lock at NULL would disable the resolver for that file forever.
- **It is its own backfill.** Unlike `generate_file_artifacts` it has **no fingerprint
  short-circuit**, and `search/indexing_service._index_digest_plane` calls it on every
  per-file index. The filename and the transcript are already in Postgres for every existing
  row, so the first reindex after this ships dates the back-catalogue with no re-ingest.

⚠️ **`media_file.creation_date` used to lie, and this is why there is no backfill from it.**
It was the end of a silent chain — container metadata → filesystem mtime → `upload_time` —
that recorded no provenance, so on an existing row a real container date is indistinguishable
from a copy of the upload date. The chain is deleted (`tasks/transcription/metadata_extractor`)
and `creation_date` now means only "what the container said". Seeding `recorded_date` from it
would launder `upload_time` into a column claiming to know when the meeting happened. NULL
reads honestly as "not yet resolved"; a laundered value does not.

⚠️ **Nothing in the product may read `metadata_important['rag_eval']['date']`.** That block is
the evaluation harness's gold source. The injector writes `recorded_date` at injection time,
exactly as ingest would; a retrieval path reading the eval block instead would be scoring the
corpus against its own answer key.
