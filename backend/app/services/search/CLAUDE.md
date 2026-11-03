# app/services/search — transcript chunk indexing + hybrid/neural search

## ⚠️ Cosine scores: OpenSearch `cosinesimil` is NOT raw cosine

Every kNN field in this app maps `"space_type": "cosinesimil"` with `engine: lucene`
(`index_schema.py` for the chunks index; `opensearch_service/repair.py` +
`opensearch_service/indices.py` for the speaker indices). Lucene's `cosinesimil` returns
**`(1 + cosine) / 2`**, range 0–1.

```python
raw_cosine = 2.0 * hit["_score"] - 1.0
```

**Skipping this silently produces plausible-but-wrong similarity numbers** — thresholds drift
instead of failing loudly. Any new kNN read must convert. The 10 existing conversion sites (all
in the sibling speaker/voiceprint plane, none in this package):

- `../opensearch_service/matching.py` — `_extract_speaker_match` ·
  `batch_find_matching_speakers` · `msearch_speaker_similarities`
- `../opensearch_service/profiles.py` — `msearch_profile_knn_batch` · `find_matching_profiles`
- `../opensearch_service/clusters.py` — `find_matching_clusters`
- `../speaker_matching/` — `match_queries.match_speaker_to_known_speakers` · `match_storage.process_match_hit`
- `../profile_embedding_service.py` · `../similarity_service.py`

This package itself never reads a raw kNN score: transcript search ranks by **RRF**, whose
output is a rank-fusion score, not a similarity. Don't treat `relevance_score` as cosine.

### The conversion applies to WRITES too (issue #674)

A threshold sent *into* OpenSearch — `min_score`, or any filter expressed in score space —
lives in the same shifted space and must be converted the other way,
`opensearch_score = (1 + raw_cosine) / 2`. A read-site audit cannot find a bad write, because
nothing is being read: `min_score=0.75` looked like the auto-accept gate and actually admitted
everything at raw cosine ≥ 0.50, which `_propagate_profile_assignment` then wrote `verified=True`.
Both directions are named functions in `app/utils/cosine_space.py` — call them rather than
open-coding the arithmetic, so the space is in the name. There is exactly one `min_score` write
against a cosinesimil index today (`../similarity_service.py`, whose parameter is now
`min_raw_cosine`); every other kNN caller filters in Python *after* converting the score.

## Purpose

The transcript-chunk search plane: chunk → index → query. The **speaker/voiceprint** plane is
separate and lives in the `../opensearch_service/` package (alias `speakers` → `speakers_v3`
512d / `speakers_v4` 256d; see `core/embedding_models.py:get_speaker_index*`).

**Every write to that speaker plane passes the privacy write backstop (#1280).** `speaker_write.add_speaker_embedding_v4` /
`bulk_add_speaker_embeddings_v4` / `add_speaker_embedding`, `profiles.store_profile_embedding_v4` / `store_profile_embedding` and
the cluster-centroid writes in `clusters.py` call `services/privacy/policy.assert_voiceprint_write_allowed(user_id, organization_id)`; a speaker document passes its `media_file_id` too and the recording's per-file consent (#1356) is then re-read fresh on every write
first and return their existing "not written" value when it is False (the scope comes from the document's own owner and
organization, never request state; the cluster update reads it from the stored document and carries its `organization_id`
over, because the rewrite replaces the whole document and a document without the stamp reads as personal). The answer is cached 10 s per
process (`PRIVACY_WRITE_BACKSTOP_TTL_SECONDS`), a resolver or storage error denies and is not cached. A NEW function that writes a
speaker, profile or cluster embedding document must call it too; do not rely on the caller having checked, because the callers
are Celery tasks that may have been planned before a switch flipped. Deleting these documents is the purge
(`services/privacy/purge.py`, via `gdpr_voiceprint_erasure._erase_speaker_voiceprints`), never a side effect of switching a layer off.
The chunk plane in this package carries no voiceprint and is not gated.

## Key files

- `chunking_service.py` — `chunk_transcript_by_speaker_turns`: groups consecutive same-speaker
  segments into turns, splits over-long turns with sliding-window overlap
  (`SEARCH_CHUNK_TARGET_WORDS` 200 / `SEARCH_CHUNK_OVERLAP_WORDS` 40).
  **Two invariants here are load-bearing and both were broken (#448, #449):**
  - **Size is measured with `count_words`, never `str.split()`.** CJK and Thai are written
    without spaces, so `split()` reports a 10,000-character Chinese transcript as **one
    word** — every size check passed and the whole recording became a single chunk.
    `count_words` counts scriptio-continua characters individually and is identical to
    `split()` for Latin, so no existing boundary moves. Chunk text is sliced from the
    ORIGINAL string via `_word_spans`; `" ".join(...)` would put a space between every
    Chinese character.
  - **The sentence-splitter choice is latched per process** (`_nltk_load_failed`, in `sentence_splitter.py` with `count_words`). It used
    to be a 5-minute retry cooldown, which let a single re-index chunk its early files with
    the regex and its later files with punkt — the two disagree on abbreviations, so that
    is one corpus chunked two ways in one pass. Never reintroduce a retry here. Unmapped
    languages also must NOT fall back to English punkt: it loads fine, runs fine, and
    returns a Chinese transcript as one sentence, so the regex fallback was never reached.
    `reset_sentence_splitter_state()` exists for tests only.
- `index_schema.py` (body, `_INDEX_VERSION`: bump logs a "full reindex" warning, never migrates), `index_admin.py`
  (create/recreate/additive steps), `pipelines.py`, `plane_queries.py`; `indexing_service.py` +
  `indexing/` = `TranscriptIndexingService` (per-plane mixins) — see `indexing/CLAUDE.md`.
- `fusion.py` — **what a fusion strategy IS**: `FusionConfig`, the derived pipeline id, the
  pipeline body, and drift detection. See "Fusion is selectable per request" below.
- `hybrid_search_service.py` — `HybridSearchService.search`, the only query path; the planes and
  request pieces it binds are sibling modules (its module docstring lists them).
- `snippet_redaction.py` — read-time masking of the snippets on a search page. See
  "Snippets are masked, and the tags make that non-obvious" below.
- `ml_model_service.py` — `OpenSearchMLModelService` (client, settings, status/listing) composed from three mixins over `ml_model_host.py`: `ml_model_registration.py` (register, local artifacts), `ml_model_deployment.py` (deploy, verify, ensure-deployed), `ml_ingest_pipelines.py` (ingest pipeline, active model). The client is owned only by `ml_model_service` (`__init__`/`_ensure_client`): mixins must never import `get_opensearch_client`, or tests patching it stop taking effect. `model_downloader.py`,
  `settings_service.py` (DB-backed model + dimension), `tenant_scope.py`.
- `embedding_provenance.py` — which model produced the vectors, and the one-query
  mixed-index survey. `model_switch.py` — the whole model switch, shared by both
  endpoints, plus the ONE reindex fan-out loop. See "Switching the embedding model"
  below. `reindex_scope.py` — which owners and which of their files a reindex must
  cover; `reindex_cancel.py` — the cancel flag's key shape, the fan-out record, and
  why the flag names a run. See "A reindex is corpus-wide" below.

## Conventions / patterns

- Index `transcript_chunks` + alias `transcript_search`. Alias creation/teardown is duplicated
  in `ensure_chunks_index_exists` and `recreate_index_for_dimension` — keep both in sync.
- Neural search on by default (`OPENSEARCH_NEURAL_SEARCH_ENABLED=true`); default model
  `all-MiniLM-L6-v2` (384d, 80 MB). Tiers in `core/embedding_models.py:OPENSEARCH_EMBEDDING_MODELS`
  (fast 384d / balanced 768d / best, English + multilingual). Offline pre-fetch:
  `DOWNLOAD_ALL_OPENSEARCH_MODELS=true bash scripts/download-models.sh models`.
- Startup (`app/startup/warmups.py`, `_initialize_neural_search`) sleeps 15 s, configures ML Commons cluster settings, scans
  the read-only `/ml-models/` mount, downloads the default model if internet is reachable, then
  `ensure_model_deployed` registers via `file://` and **falls back to remote HF** if that fails.
  Active model id persists as SystemSettings `search.opensearch_model_id`.
- Every query must AND in `tenant_scope.org_filter_clauses(organization_id)` — personal scope is
  `must_not exists organization_id`, so org docs can never leak into personal results.
  That includes the legacy whole-document `transcripts` index (#1027): `index_transcript` takes
  a required `organization_id` and stamps org files only, and its readers (the gallery's
  `apply_transcript_search_filter`, `find_speaker_across_media`) gate on it instead of relying
  only on the SQL query they are intersected with. Docs written before that carry no stamp —
  `tasks/tenant_backfill_task.py` stamps them (its runbook covers both indices). An index
  created before the field existed gets the `organization_id: integer` mapping on bootstrap
  (`opensearch_service/indices.py:ensure_transcript_tenant_mapping`, #1115) — additive
  `put_mapping`, no reindex; run the backfill after that deploy.
- Embeddings are generated **server-side** by the ingest pipeline; `_generate_query_embedding`
  returns `None` for the vector by design. There is no client-side encoder here.

## How it connects

Written by `app/tasks/search_indexing_task.py` and `app/tasks/reindex_task.py`; chunks deleted
by `opensearch_erasure._erase_transcript_chunks`. Queried from `app/api/endpoints/search.py`.
Snippets are masked at read time by `snippet_redaction.mask_snippets`, called from
`_redact_snippets` — see below.

## Snippets are masked, and the tags make that non-obvious (#86)

The index holds transcript text UNREDACTED, so a search snippet must be masked per reader before it leaves the service (`_redact_snippets`; a policy that masks only `pii` once got every snippet verbatim), and the highlight tags are what make that non-obvious. The rules and the tests: `backend/app/services/search/notes/snippet-masking.md`.
## Who may read a chunk: `access_filter_clause` (#1275)

Every transcript-plane reader builds its ACL term through `search/access_filter.access_filter_clause`
(`_build_filters`, the autocomplete and facet readers, chat aggregation). The clause is
`accessible_user_ids ∋ me` **or**, inside an organization, `collection_ids` ∈ the organization
collections the caller is a member of and may see (`collection_visibility.org_readable_collection_ids`,
one Postgres read per request, capped at `MAX_VISIBLE_COLLECTION_TERMS` = 10 000, above which it
falls back to the per-file term and logs a WARNING). It is a query-time arm on purpose: joining or
leaving an organization or switching a collection to restricted needs no reindex. What it does need
is a fresh `collection_ids` on the chunks, so `update_file_access_index` rewrites it together with
`accessible_user_ids`, and every add to / removal from an organization collection (and auto-label)
dispatches it. A bare `{"terms": {"accessible_user_ids": ...}}` in a new reader hides files the rest
of the app lets the caller open.

## Gotchas

- **Do NOT add `aggs` to the collapsed hybrid body** (grep "Do NOT add" in `collapse_bodies.py`):
  OpenSearch 3.4 throws `ArrayIndexOutOfBoundsException` in `score-ranker-processor` when
  cardinality aggs meet hybrid + collapse + RRF. `total_files` is derived from collapsed results.
- RRF + collapse **strips inner-hit highlights** — hence `_detect_keyword_match_fallback` and
  `_generate_synthetic_snippet`. Don't "simplify" those away.
- **Keyword (BM25-only) search does NOT send `collapse.inner_hits` (#1064).** OpenSearch runs
  inner hits as one sub-search per collapsed group, and each one rewrites the fuzzy clauses and
  sets up the highlighter again. That fixed cost multiplied by the 200-group over-fetch made
  keyword search 0.4-2.0 s on a 2,000-chunk index, where a minimal `match` took 2-6 ms and
  neural took ~100 ms. `profile: true` doesn't show it: the shard query and fetch come to ~60 ms,
  and the rest of `took` is the unprofiled expand phase. `_execute_split_bm25_collapse` instead
  sends the collapse without inner hits, then one `terms` + `top_hits` agg for the segments, and
  `_hydrate_page_highlights` highlights **only the displayed page**. The page is byte-identical
  to the single-request body (`tests/integration/test_keyword_search_latency_opensearch.py`
  proves this and gates the latency). Two consequences: every hit of that body counts as a
  keyword match without needing a highlight (`assume_keyword_match`), and an empty query (a
  `match_all` browse) still takes the single-request path. The hybrid backfill runs split too.
  The hybrid, neural, fallback and two-phase bodies still use inner hits.
- **Every matching file must be reachable by paging (#1078, #1079).** Three rules, each learned
  from a bug. `tests/integration/test_search_completeness_opensearch.py` pages through every
  result set and fails on the first file that is missing or shown twice.
  - Pagination is cut **client-side only**. A BM25 non-relevance sort used `from`/`size=page_size`
    as well, so the one-page response got paged a second time and page 2+ came back empty.
  - Hybrid **always** backfills keyword files the fused window missed, below the hybrid-ranked
    head. The window is ~200 *chunks*, not files. It used to backfill only when the window held
    less than one page, and then 43 of 165 keyword-matching files were reachable.
  - The fused window is sized as for **page 1 on every page**. If it grew with the page number,
    deep pages pulled tail files into the head and moved every page boundary, so some files
    appeared twice and others never. The backfill's BM25 window may grow, because its order is
    stable as it grows.
- Relevance sorts can't mix `_score` with other sort criteria under the pipeline; non-relevance
  sorts therefore take the `_search_with_two_phase` path (hybrid aggs → BM25 collapse per page).
- `recreate_index_for_dimension` **deletes the index**. Switching embedding model = full reindex —
  and "full" means *every user's* transcripts; see the section below for why that is not what it
  used to mean.
- Availability (`hybrid_search_service`) and index/pipeline (`search_infra`) checks are cached in
  module globals, each behind its module's `_state_lock`. The infra cache needs **no** reset after
  an index recreation (the index stays present and pipelines are cluster-level). Tests that
  repoint index names or stub the existence checks clear it with
  `tests.helpers.reset_search_infrastructure_state()`; `hybrid_search_service.reset_neural_search_state()`
  still applies to the availability cache (neural-failure TTL is only 30 s, success 120 s).
  **The infra cache holds a SET of verified pipeline ids**, not a flag — see below.
- `SEARCH_RRF_RANK_CONSTANT` defaults to **30** — grep the symbol in `core/config.py`; a line
  number cited here has already rotted once. A mismatch against the live **search** pipeline
  triggers automatic recreation on startup (`ensure_search_pipeline_exists`). Since #401 the
  **ingest** pipeline self-heals the same way: `_check_existing_pipeline_config` compares
  `model_id`, `field_map` and `batch_size` against `_build_neural_ingest_pipeline`, so
  repointing what gets embedded reaches upgraded deployments and not only fresh installs.
  It also compares the processor list's **shape** — exactly one processor, and it a
  `text_embedding`. It used to iterate to the first `text_embedding` and return on it, so
  an **extra** processor (a stray `set`, a second embedding, anything left by a manual PUT
  or an older release) and a **reordering** were both invisible and survived every boot.
  A processor ahead of the embedding can rewrite the very field being embedded, and the
  check reported a perfect match.
  `batch_size` is compared only when the live pipeline has one (the creation path drops it on
  OpenSearch versions that reject it, and treating that as drift is a boot loop), and a
  `field_map` change still needs a **reindex** before existing documents embed the new field.
- **Every delete AND every targeted rewrite of the chunks index goes through
  `chunk_plane_query`** (issues #400, #405).
  Re-indexing overwrites `{file_uuid}_{chunk_index}` in place, so a shorter re-chunk used to
  orphan the tail — stale text, stale speakers, stale timestamps, still returned by search and
  by RAG chat. `index_transcript_chunks` prunes `chunk_index >= len(chunks)` after the bulk
  load, so the first index after transcription still pays no `delete_by_query`.
  Since v6 the index also holds digest documents, so the predicate is one of **three** —
  see "Index v6" below; picking the wrong one either strands a digest or destroys it.
  `extra_filters=` lets a caller narrow *within* the chunk plane without forking the
  predicate — `tasks/rename_propagation_task.py` uses it.
- **The prune's gate is an `mget`, and it must never go back to being a `count`** (issue
  #435). A `count` is a **search**, and a search sees only what the last refresh made
  visible; the bulk load above uses `refresh=False`. So a second index of one file inside
  the refresh window used to find an empty tail, skip the delete, and orphan the chunks
  **permanently** — nothing later re-examined them. Measured before the fix: a back-to-back
  pair of `index_transcript_chunks` calls completes in 125–224 ms and **the tail survived 11
  of 12 pairs**, and *nothing serialises the callers* — five dispatch sites reach the
  single-file task, including `api/endpoints/files/reprocess.py` (user-triggered, no
  in-flight check) and the recovery sweep, which branches on the index record being
  **missing** rather than on the original task having stopped. `mget` addresses documents by
  id, reads the translog and is realtime, so it sees the tail the instant it is written; it
  also works on a pre-v6 corpus by construction. Both `_prune_stale_chunks` and
  `_prune_stale_digests` use it.
  - The probe is a **window** of `_ORPHAN_PROBE_WINDOW` (64) ids, not one id: a partially
    failed bulk load can leave a hole, and `test_a_shorter_resection_leaves_no_orphan_digest`
    plants its orphan at `sections + 3`.
  - **A `_seq_no`/`_version` predicate cannot replace it.** `delete_by_query` runs as a
    scroll **search**, so it has the same visibility dependency — measured, a
    `delete_by_query` over 4 unrefreshed documents deletes **0**. That is also why the prune
    `indices.refresh()` *after* the gate fires and before the delete.
  - **Refreshing before the gate instead would cost 23–44% of every full reindex**: measured
    at ~95 ms median per file (near-flat in index size — it is dominated by building the HNSW
    graph for the vectors just written), i.e. +41–79 s on a 432-file run that takes 182 s.
    The `mget` costs 4.0–4.8 ms against the 3.7–4.1 ms count it replaced.
  - Still **not** guaranteed: two genuinely overlapping index calls for one file are not
    serialised by anything. A probe cannot find a tail a concurrent writer has not written
    yet. That is a coordination problem, not a visibility one.
- **Chunk docs snapshot `speaker` / `speakers` / `title`; renames must be propagated**
  (issue #405). Chat's speaker axis resolves the display name from **Postgres** and filters the
  index with an exact `terms` match on `speaker` (`filters._build_filters`), so an
  un-propagated rename makes every pre-rename chunk unreachable under the only name the user can
  ask about — and the model answers from the remainder. `app/tasks/rename_propagation_task.py`
  owns both rewrites (`propagate_speaker_rename` / `propagate_title_rename`, cpu queue) and
  every rename path dispatches through its `dispatch_speaker_rename`, which coalesces per file.
  Both bump the chat corpus version, or chat keeps serving the pre-rename retrieval for the
  cache TTL. Once #383's digest tier lands, rename joins the digest-regeneration triggers
  (addendum G1) — the seam is that module's `_finish`.

## Fusion is selectable per request (#363, #403 Stage 5 plumbing)

The fusion strategy id is threaded down to four attach sites and never re-read from settings; `_verified_pipelines` is a set so one strategy cannot certify another; the response cache keys on the resolved pipeline id. **MEASURED: RRF stays** (ten arms, zero both-corpus wins; #363 closed). Detail: `backend/app/services/search/notes/fusion.md`.
## Which models are offered, and why those (#504)

The offered-model list, its selection criteria and the measured multilingual result (#453): `backend/app/services/search/notes/embedding-models.md`.
## Switching the embedding model, reindexing, and self-healing (#437, #627, #691, #625, #495)

Switching the embedding model is one implementation that fans out (#437); `embedding_model` is the model and `"neural"` means UNKNOWN; a reindex is corpus-wide in every mode (#627); cancelling a fan-out is by a flag that NAMES the run (#691); the bootstrap self-heals and is a beat task (#625); and **a failed index must not report success** (#495). Each rule with its history: `backend/app/services/search/notes/reindex-and-model-switch.md`. Read it before touching any code that creates, deletes or repopulates the chunk index.
## The speaker label in the index follows the voiceprint switch (#1280)

A voiceprint switch change rebuilds the affected labels through a durable, audited resync: `backend/app/services/search/notes/speaker-label-in-index.md`.
## Index v6: three planes in one index (#403 Stage 3, #963)

`_INDEX_VERSION` is **6**, and it is assigned *from*
`services/ingest_artifacts/index_mapping.TARGET_INDEX_VERSION` so the number and the mapping
cannot drift. `transcript_chunks` now holds three kinds of document:

| plane | `doc_type` | `_id` | `chunk_index` |
|---|---|---|---|
| transcript chunk | `chunk` (absent on anything written before v6) | `{uuid}_{n}` | `n` |
| digest section | `digest` | `{uuid}_digest_{n}` | `-1-n` (negative sentinel; `index.sort.field` includes `chunk_index`, and 0 is a real chunk) |
| summary leaf | `summary` (issue #963) | `{uuid}_summary_{n}` | `SUMMARY_CHUNK_INDEX_BASE - n` (a disjoint, more-negative sentinel band; `test_the_sentinel_bands_never_collide`) |

Everything about the shape — the mapping additions, the id scheme, the sentinels, the
`chunk_plane_clause()` / `digest_plane_clause()` / `summary_plane_clause()` helpers,
`build_embedding_text`, `build_digest_documents` and `build_summary_documents` — lives in
`services/ingest_artifacts/index_mapping.py` and is **imported, never restated**. The summary
plane's mapping additions (`summary_key_path` keyword, `summary_leaf_index` integer) landed as
`ADDITIVE_MAPPING_STEPS` step 3 — additive, no `_INDEX_VERSION` bump, no destructive reindex.

- **The summary plane's SOURCE OF TRUTH is `media_file.summary_data` in Postgres, never
  OpenSearch.** `TranscriptIndexingService._index_summary_plane` has no `summary_data`
  parameter — it reads the column itself, on its own session — which is the structural
  anti-regression for #67 (the retired implementation wrote the SAME dict to the column and to
  OpenSearch in one task). The plane is a derived, fully-rebuildable cache, addressed by
  deterministic leaf-ordinal id, never a second store of record.
- **Four predicate builders, and picking the wrong one is silent.** `chunk_plane_query`
  (compat-armed chunk plane — the #400 tail prune, the #405 rename rewrite),
  `digest_plane_query` (digest sections of one file — the digest-orphan prune),
  `summary_plane_query` (summary leaves of one file — the summary-orphan prune, #963), and
  `file_plane_query` (**every** plane — file deletion and the full rebuild in
  `reindex_transcript`). A rebuild that used the chunk-plane predicate would leave the
  digests/summaries of a shorter re-sectioning/re-summarization behind; a delete that used it
  would leave a readable summary of a deleted recording.
- **The summary plane rides the SAME per-file index path as the digest plane, for the identical
  reason (addendum G1).** `index_transcript_chunks` calls `_index_summary_plane` right after
  `_index_digest_plane`, sharing one `base_metadata` dict built once. `delete_transcript_chunks`
  is unqualified (`file_plane_query`), and every rebuild trigger — version bump, model switch,
  maintenance repair, manual reindex — routes through `index_transcript_chunks`; a rebuild that
  regenerated chunks and digests but not summaries would destroy the summary plane permanently.
  A summary is also generated/cleared LATER and independently of the transcript, so it has its
  own light trigger too: the `index_file_summary` Celery task (CPU queue), dispatched from every
  site that mutates `media_file.summary_data` — **always after that write's session has
  committed**, never from inside it.
- **The summary plane is query-time isolated, never chat-visible.** `HybridSearchService._build_filters`
  takes an optional `plane_clause` kwarg; every existing caller keeps the default
  (`chunk_plane_clause()`), and `HybridSearchService.search_summary_plane` is the one caller
  that passes `summary_plane_clause()`. `VERBATIM_DOC_TYPES` stays `(DOC_TYPE_CHUNK,)` — a
  summary is derived, interpretive text and must never surface as if a speaker had said it.
  Chat retrieval (`chunk_retrieval.retrieve_chunks`/`retrieve_digests`) is untouched: neither
  clause it uses matches `doc_type: "summary"`.
- **Summary search stores RAW text; masking is query-time, per leaf, under the REQUESTING
  user's policy** — the same rule the chunk plane follows (`summary_search.py`,
  `redaction/summary_redaction.mask_summary_leaf`). Index-time masking was rejected: one index
  copy cannot carry N users' policies, and it would make a user's own masked content unfindable
  by them. `unmask_for_local`/`is_local_provider` have no bearing here — this is a display
  surface, not an egress event.
- **The compat arm is mandatory, and for a precise reason.** A bare `term` on `doc_type` is
  not broken by the dynamic mapping (the four values are single lowercase tokens OpenSearch
  3.4 matches fine) — it is broken by **every document already indexed carrying no `doc_type`
  at all**, which makes the #400 prune count return 0 for the whole installed corpus. An
  explicit keyword mapping does nothing for documents written before it existed.
  `tests/unit/test_chunk_plane_compat_arm.py` sweeps every function in `app/` that queries
  this index and requires each one to have *decided*; the deliberate exceptions
  (`update_file_access_index`, the tenant backfill, the whole-file orphan sweeps) are an
  allowlist with written reasons, and a stale entry fails.
- **The ACL and tenant rewrites must REACH digests** (addendum G5) — `update_file_access_index`
  keys on `file_id`, the tenant backfill on `file_uuid`, and `build_digest_documents` puts
  **both** on every digest. Excluding them there is a permission leak, not a relevance bug.
  `update_file_access_index` also now calls `chat.retrieval_cache.bump_corpus_version()`
  unconditionally after its `update_by_query` loop — the ACL rewrite reaching the index is not
  enough by itself, because chat's exact-query retrieval cache is ACL-blind and would otherwise
  keep serving an already-cached, now-revoked result for the rest of its TTL. See that module's
  CLAUDE.md for the endpoint-side-bump alternative that was rejected and why.
- **The digest plane is written by `index_transcript_chunks`, not by the coordinator**
  (addendum G1). `delete_transcript_chunks` is unqualified and every rebuild trigger routes
  through it, so a rebuild that regenerated only chunks would destroy the digest tier
  permanently. `_index_digest_plane` calls `ingest_artifacts.generate_file_artifacts` on its
  **own** session; the `source_fingerprint` short-circuit makes an unchanged transcript cost a
  SHA-256, and because the fingerprint covers the *resolved* speaker display name, a rename
  invalidates the row by itself.
- **The neural ingest pipeline embeds `embedding_text`, not `content`.** Every document
  carries it: for a chunk it is `"{title} | {date} | participants: {roster}\n\n{chunk text}"`,
  for a digest section the same header plus the section. That header is the zero-LLM
  contextualization — it is what makes "the logistics team's retro" retrievable when the
  phrase is in the title and in nothing anybody said. BM25 still scores `content`.
  ⚠️ The measured embedding window is **128 wordpieces**, not the 256 the plan assumed
  (`services/ingest_artifacts/sizing.py`), so the header is a real cost: it displaces roughly
  the last 20 words of a 200-word chunk from that chunk's own vector.
- **Search does not return digests; chat reaches them only through its own leg.** `_build_filters`
  carries `chunk_plane_clause()`, so both the search UI and `retrieve_chunks` see the chunk plane
  only. That is deliberate: a digest is derived text, it would render as if a speaker had said
  it, and it would receive **neither** read-time masking treatment (addendum G6). The digest
  leg (`chunk_retrieval.retrieve_digests`, Stage 4) has its own citation shape and masker
  (`chat/redactor.mask_digests`).
