# app/services/search — transcript chunk indexing + hybrid/neural search

## ⚠️ Cosine scores: OpenSearch `cosinesimil` is NOT raw cosine

Every kNN field in this app maps `"space_type": "cosinesimil"` with `engine: lucene`
(`indexing_service.py:122` for the chunks index; `opensearch_service.py:683/930/1122` for the
speaker indices). Lucene's `cosinesimil` returns **`(1 + cosine) / 2`**, range 0–1.

```python
raw_cosine = 2.0 * hit["_score"] - 1.0
```

**Skipping this silently produces plausible-but-wrong similarity numbers** — thresholds drift
instead of failing loudly. Any new kNN read must convert. The 11 existing conversion sites (all
in the sibling speaker/voiceprint plane, none in this package):

- `../opensearch_service.py` — `_extract_speaker_match` :1880 · `batch_find_matching_speakers`
  :2069 · `msearch_profile_knn_batch` :2661 · `find_matching_clusters` :2961 ·
  `msearch_speaker_similarities` :3056 · `find_matching_profiles` :3523
- `../speaker_matching_service.py` — `find_unlabeled_speaker_matches` :333 · `_process_match_hit` :1317
- `../profile_embedding_service.py:276` · `../smart_speaker_suggestion_service.py:178` ·
  `../similarity_service.py:197`

This package itself never reads a raw kNN score: transcript search ranks by **RRF**, whose
output is a rank-fusion score, not a similarity. Don't treat `relevance_score` as cosine.

## Purpose

The transcript-chunk search plane: chunk → index → query. The **speaker/voiceprint** plane is
separate and lives in `../opensearch_service.py` (alias `speakers` → `speakers_v3` 512d /
`speakers_v4` 256d; see `core/constants.py:get_speaker_index*`).

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
- `SEARCH_RRF_RANK_CONSTANT` defaults to **30** (`core/config.py:328`); the docstring in
  `_build_hybrid_search_pipeline` still says 40. A mismatch against a live pipeline triggers
  automatic pipeline recreation on startup.
