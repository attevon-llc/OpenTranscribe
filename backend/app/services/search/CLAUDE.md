# app/services/search — transcript chunk indexing + hybrid/neural search

## ⚠️ Cosine scores: OpenSearch `cosinesimil` is NOT raw cosine

Every kNN field in this app maps `"space_type": "cosinesimil"` with `engine: lucene`
(`indexing_service.py:122` for the chunks index; `opensearch_service/repair.py` +
`opensearch_service/indices.py` for the speaker indices). Lucene's `cosinesimil` returns
**`(1 + cosine) / 2`**, range 0–1.

```python
raw_cosine = 2.0 * hit["_score"] - 1.0
```

**Skipping this silently produces plausible-but-wrong similarity numbers** — thresholds drift
instead of failing loudly. Any new kNN read must convert. The 11 existing conversion sites (all
in the sibling speaker/voiceprint plane, none in this package):

- `../opensearch_service/matching.py` — `_extract_speaker_match` ·
  `batch_find_matching_speakers` · `msearch_speaker_similarities`
- `../opensearch_service/profiles.py` — `msearch_profile_knn_batch` · `find_matching_profiles`
- `../opensearch_service/clusters.py` — `find_matching_clusters`
- `../speaker_matching_service.py` — `find_unlabeled_speaker_matches` :333 · `_process_match_hit` :1317
- `../profile_embedding_service.py:276` · `../smart_speaker_suggestion_service.py:178` ·
  `../similarity_service.py:197`

This package itself never reads a raw kNN score: transcript search ranks by **RRF**, whose
output is a rank-fusion score, not a similarity. Don't treat `relevance_score` as cosine.

## Purpose

The transcript-chunk search plane: chunk → index → query. The **speaker/voiceprint** plane is
separate and lives in the `../opensearch_service/` package (alias `speakers` → `speakers_v3`
512d / `speakers_v4` 256d; see `core/constants.py:get_speaker_index*`).

## Key files

- `chunking_service.py` — `chunk_transcript_by_speaker_turns`: groups consecutive same-speaker
  segments into turns, splits over-long turns with sliding-window overlap
  (`SEARCH_CHUNK_TARGET_WORDS` 200 / `SEARCH_CHUNK_OVERLAP_WORDS` 40).
- `indexing_service.py` — index body + `_INDEX_VERSION` (bump ⇒ startup logs a "run a full
  reindex" warning, it does **not** migrate), RRF + neural ingest pipelines,
  `TranscriptIndexingService`.
- `hybrid_search_service.py` — `HybridSearchService.search`, the only query path.
- `ml_model_service.py` — ML Commons register/deploy. `model_downloader.py`,
  `settings_service.py` (DB-backed model + dimension), `tenant_scope.py`.

## Conventions / patterns

- Index `transcript_chunks` + alias `transcript_search`. Alias creation/teardown is duplicated
  in `ensure_chunks_index_exists` and `recreate_index_for_dimension` — keep both in sync.
- Neural search on by default (`OPENSEARCH_NEURAL_SEARCH_ENABLED=true`); default model
  `all-MiniLM-L6-v2` (384d, 80 MB). Tiers in `core/constants.py:OPENSEARCH_EMBEDDING_MODELS`
  (fast 384d / balanced 768d / best, English + multilingual). Offline pre-fetch:
  `DOWNLOAD_ALL_OPENSEARCH_MODELS=true bash scripts/download-models.sh models`.
- Startup (`app/main.py`, ~line 420) sleeps 15 s, configures ML Commons cluster settings, scans
  the read-only `/ml-models/` mount, downloads the default model if internet is reachable, then
  `ensure_model_deployed` registers via `file://` and **falls back to remote HF** if that fails.
  Active model id persists as SystemSettings `search.opensearch_model_id`.
- Every query must AND in `tenant_scope.org_filter_clauses(organization_id)` — personal scope is
  `must_not exists organization_id`, so org docs can never leak into personal results.
- Embeddings are generated **server-side** by the ingest pipeline; `_generate_query_embedding`
  returns `None` for the vector by design. There is no client-side encoder here.

## How it connects

Written by `app/tasks/search_indexing_task.py` and `app/tasks/reindex_task.py`; chunks deleted
by `file_cleanup_service.delete_transcript_chunks`. Queried from `app/api/endpoints/search.py`.
Snippets are masked at read time via `services/redaction` (`_redact_snippets`, profanity +
custom only, label style).

## Gotchas

- **Do NOT add `aggs` to the collapsed hybrid body** (`hybrid_search_service.py:1464`):
  OpenSearch 3.4 throws `ArrayIndexOutOfBoundsException` in `score-ranker-processor` when
  cardinality aggs meet hybrid + collapse + RRF. `total_files` is derived from collapsed results.
- RRF + collapse **strips inner-hit highlights** — hence `_detect_keyword_match_fallback` and
  `_generate_synthetic_snippet`. Don't "simplify" those away.
- Relevance sorts can't mix `_score` with other sort criteria under the pipeline; non-relevance
  sorts therefore take the `_search_with_two_phase` path (hybrid aggs → BM25 collapse per page).
- `recreate_index_for_dimension` **deletes the index**. Switching embedding model = full reindex.
- Availability, index, and pipeline checks are cached in module globals behind `_state_lock`.
  After recreating an index call `reset_infrastructure_state()` / `reset_neural_search_state()`,
  or the worker keeps trusting stale state (neural-failure TTL is only 30 s, success 120 s).
- `SEARCH_RRF_RANK_CONSTANT` defaults to **30** — grep the symbol in `core/config.py`; a line
  number cited here has already rotted once. A mismatch against the live **search** pipeline
  triggers automatic recreation on startup (`ensure_search_pipeline_exists`). Since #401 the
  **ingest** pipeline self-heals the same way: `_check_existing_pipeline_config` compares
  `model_id`, `field_map` and `batch_size` against `_build_neural_ingest_pipeline`, so
  repointing what gets embedded reaches upgraded deployments and not only fresh installs.
  `batch_size` is compared only when the live pipeline has one (the creation path drops it on
  OpenSearch versions that reject it, and treating that as drift is a boot loop), and a
  `field_map` change still needs a **reindex** before existing documents embed the new field.
- **Every delete AND every targeted rewrite of the chunks index goes through
  `chunk_plane_query`** (issues #400, #405).
  Re-indexing overwrites `{file_uuid}_{chunk_index}` in place, so a shorter re-chunk used to
  orphan the tail — stale text, stale speakers, stale timestamps, still returned by search and
  by RAG chat. `index_transcript_chunks` now prunes `chunk_index >= len(chunks)` after the bulk
  load, gated by a `count` so the first index after transcription pays no `delete_by_query`.
  When #383 Phase 3 adds digest documents to this index, the `doc_type` predicate goes in
  `chunk_plane_query` and nowhere else. `extra_filters=` lets a caller narrow *within* the
  chunk plane without forking the predicate — `tasks/rename_propagation_task.py` uses it.
- **The v6 target mapping already exists, in `services/ingest_artifacts/index_mapping.py`**
  (#403 Stage 2). It is *defined and applied nowhere*: `_INDEX_VERSION` is still 5, and
  `tests/unit/test_digest_index_mapping.py` asserts that, so the mapping and the bump land
  together in Stage 3. It owns `doc_type` (keyword, #403 **D1** — not `source_type`), the
  compat-armed `chunk_plane_clause()` every reader must import, `embedding_text`, the
  `{uuid}_digest_{n}` id scheme, the negative `chunk_index` sentinel, and a digest-document
  builder carrying **both** `file_id` and `file_uuid`. The compat arm is mandatory for a
  reason worth stating precisely: a bare `term` on `doc_type` is not broken by the dynamic
  mapping (the four values are single lowercase tokens OpenSearch 3.4 matches fine) — it is
  broken by **every document already indexed carrying no `doc_type` at all**, which makes the
  #400 prune count return 0 for the whole installed corpus.
- **Chunk docs snapshot `speaker` / `speakers` / `title`; renames must be propagated**
  (issue #405). Chat's speaker axis resolves the display name from **Postgres** and filters the
  index with an exact `terms` match on `speaker` (`hybrid_search_service:1107`), so an
  un-propagated rename makes every pre-rename chunk unreachable under the only name the user can
  ask about — and the model answers from the remainder. `app/tasks/rename_propagation_task.py`
  owns both rewrites (`propagate_speaker_rename` / `propagate_title_rename`, cpu queue) and
  every rename path dispatches through its `dispatch_speaker_rename`, which coalesces per file.
  Both bump the chat corpus version, or chat keeps serving the pre-rename retrieval for the
  cache TTL. Once #383's digest tier lands, rename joins the digest-regeneration triggers
  (addendum G1) — the seam is that module's `_finish`.
