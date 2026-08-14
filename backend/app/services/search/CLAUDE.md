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
  **Two invariants here are load-bearing and both were broken (#448, #449):**
  - **Size is measured with `count_words`, never `str.split()`.** CJK and Thai are written
    without spaces, so `split()` reports a 10,000-character Chinese transcript as **one
    word** — every size check passed and the whole recording became a single chunk.
    `count_words` counts scriptio-continua characters individually and is identical to
    `split()` for Latin, so no existing boundary moves. Chunk text is sliced from the
    ORIGINAL string via `_word_spans`; `" ".join(...)` would put a space between every
    Chinese character.
  - **The sentence-splitter choice is latched per process** (`_nltk_load_failed`). It used
    to be a 5-minute retry cooldown, which let a single re-index chunk its early files with
    the regex and its later files with punkt — the two disagree on abbreviations, so that
    is one corpus chunked two ways in one pass. Never reintroduce a retry here. Unmapped
    languages also must NOT fall back to English punkt: it loads fine, runs fine, and
    returns a Chinese transcript as one sentence, so the regex fallback was never reached.
    `reset_sentence_splitter_state()` exists for tests only.
- `indexing_service.py` — index body + `_INDEX_VERSION` (bump ⇒ startup logs a "run a full
  reindex" warning, it does **not** migrate), RRF + neural ingest pipelines,
  `TranscriptIndexingService`.
- `hybrid_search_service.py` — `HybridSearchService.search`, the only query path.
- `snippet_redaction.py` — read-time masking of the snippets on a search page. See
  "Snippets are masked, and the tags make that non-obvious" below.
- `ml_model_service.py` — ML Commons register/deploy. `model_downloader.py`,
  `settings_service.py` (DB-backed model + dimension), `tenant_scope.py`.
- `embedding_provenance.py` — which model produced the vectors, and the one-query
  mixed-index survey. `model_switch.py` — the whole model switch, shared by both
  endpoints. See "Switching the embedding model" below.

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
Snippets are masked at read time by `snippet_redaction.mask_snippets`, called from
`_redact_snippets` — see below.

## Snippets are masked, and the tags make that non-obvious (#86)

**A snippet is unredacted transcript text.** It comes out of `transcript_chunks`, which stores
transcript text UNREDACTED by design, so it carries whatever the recording carried. Search
spans collection and group shares, so an unmasked preview reaches readers whose transcript
view would have masked it.

`_redact_snippets` masked `profanity` and `custom` only, on a docstring's claim that "this path
never carries PII spans". It does. A user whose policy masks **only** `pii` intersected to the
empty set and got **every snippet on every page verbatim**. Four things about the fix:

- **`MASKABLE_CATEGORIES` is `{pii, profanity, custom}` — `toxicity` is absent on purpose.**
  That detector emits a per-segment SCORE and never a span (`redaction/config`'s
  `_DETECTOR_CATEGORIES` maps it to the empty set); the `toxicity` *category*'s maskable spans
  come from the `llm` detector, a provider round-trip with no business on a search request. A
  toxicity-only policy therefore masks nothing here, correctly.
- **`<mark>` tags are split out before detection and restored after.** The wordlist path was
  safe by accident — its regex only matches word-boundary alphanumeric runs, so it can never
  touch a `<`. Presidio has no such property, and OpenSearch does **not** wrap whole words:
  real output includes `<mark>budget ? Not the </mark>original`. A span really can begin inside
  a highlight and end outside it; masking the raw string would emit an orphan `</mark>` and the
  frontend sanitizer then drops the fragment. A span crossing a tag masks each side separately
  (two adjacent labels), because the alternative swallows the tag.
- **⚠️ Detection is ONE CALL PER SNIPPET, and batching the page is forbidden.** Packing
  snippets into shared `analyze()` calls is the obvious optimisation, it is **2.2-3.0x
  faster**, and it **loses PII**: `en_core_web_sm`'s NER reports each distinct `PERSON`
  **once per document**. Measured directly — `Talia Yarrow` in two joined snippets yields
  one span, in three yields one, while three snippets naming three *different* people yield
  three. Through the live search API on a real page, the batched version left the name in
  clear in **31 of the 32 snippets containing it**, with `[NAME]` labels elsewhere on the
  same page so the result looked masked. A search page is by construction a set of fragments
  about the same subject, which is exactly the input that property destroys. The batched
  implementation was written, measured, and deleted; `_detect` carries the warning.
  - The same spaCy property means a name repeated **inside one snippet** is masked only at
    its first mention. That residual is app-wide (cached segment detection has it too), not
    a property of this path, and is not fixable here.
- **Cost, and why it is not the fail-closed branch's problem.** Warm Presidio, steady state,
  real pages from the live stack (94-200 snippets, 9-39k chars): **0.8-2.1 s** per page for a
  `pii`-masking user, against a 305-1372 ms baseline search. A `profanity`-only user pays
  **2.6-13.5 ms** (the detector gate is on the *categories*, not on `enabled`) and a
  redaction-disabled user pays nothing — and PII masking is opt-in twice over, since redaction
  is opt-out and `pii` is not in `DEFAULT_REDACTION_CATEGORIES`. Masking is applied before the
  per-user response cache (`SEARCH_CACHE_TTL_SECONDS`, 300 s), so a repeat query pays zero.
  Withholding the previews instead (as the fail-closed branch does) was rejected: that branch
  exists for when we *cannot* mask, not when masking is merely slow, and a search UI with no
  previews is the feature removed rather than a trade.
  - ⚠️ **The response cache does not key on the redaction policy**, so for up to
    `SEARCH_CACHE_TTL_SECONDS` after a user enables masking, queries they had already run
    still return the previously cached unmasked snippets. Pre-existing (it applied to
    profanity identically) and bounded, but `redaction/export_policy.py` argues the general
    case — a cached artifact must name the policy it was rendered under — and this cache
    does not. Fixing it means resolving the config *before* the cache lookup and folding a
    policy fingerprint into `_make_cache_key`.
  - The first masked search in a fresh API process pays the **~7-10 s Presidio build** unless
    `redaction/warmup.py` already ran, and that warm-up's gate is evaluated at startup — so a
    user who enables redaction on a process that started with it off pays it once. Measured
    end to end through the API: 14.7 s on the first such query, 1.2 s on the next.

Two rules the implementation depends on, both easy to undo by accident:

- **The config session closes BEFORE any detector runs.** A ~1 s Presidio pass inside an open
  transaction holds `ACCESS SHARE` for its duration and queues every `ALTER TABLE` behind it —
  exactly what `scripts/audit-session-lifetime.py` exists to catch, and what hangs an Alembic
  upgrade mid-release.
- **Fail-closed uses `redaction.config.blocking_detector_failures`, not a second rule.** A
  detector failure withholds the page's snippet text only when it feeds a category *this* user
  masks; a box with no Presidio must not cost their previews to a profanity-only user.

Not masked, deliberately: `title_highlighted` and `speaker_highlighted`. Both are
user-assigned labels rather than transcript text, and masking a speaker name is what the
speaker plane exists to let people *set*.

## Gotchas

- **Do NOT add `aggs` to the collapsed hybrid body** (`hybrid_search_service.py:1464`):
  OpenSearch 3.4 throws `ArrayIndexOutOfBoundsException` in `score-ranker-processor` when
  cardinality aggs meet hybrid + collapse + RRF. `total_files` is derived from collapsed results.
- RRF + collapse **strips inner-hit highlights** — hence `_detect_keyword_match_fallback` and
  `_generate_synthetic_snippet`. Don't "simplify" those away.
- Relevance sorts can't mix `_score` with other sort criteria under the pipeline; non-relevance
  sorts therefore take the `_search_with_two_phase` path (hybrid aggs → BM25 collapse per page).
- `recreate_index_for_dimension` **deletes the index**. Switching embedding model = full reindex —
  and "full" means *every user's* transcripts; see the section below for why that is not what it
  used to mean.
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
  index with an exact `terms` match on `speaker` (`hybrid_search_service:1107`), so an
  un-propagated rename makes every pre-rename chunk unreachable under the only name the user can
  ask about — and the model answers from the remainder. `app/tasks/rename_propagation_task.py`
  owns both rewrites (`propagate_speaker_rename` / `propagate_title_rename`, cpu queue) and
  every rename path dispatches through its `dispatch_speaker_rename`, which coalesces per file.
  Both bump the chat corpus version, or chat keeps serving the pre-rename retrieval for the
  cache TTL. Once #383's digest tier lands, rename joins the digest-regeneration triggers
  (addendum G1) — the seam is that module's `_finish`.

## Switching the embedding model (#437) — one implementation, and it fans out

**Clearing a cache re-embeds nothing.** Two vectors from two different models occupy the same
kNN space and `cosinesimil` returns a number for them, so hybrid search ranks the two
populations against each other and nothing looks wrong. There is no error, no log and no metric
in the ordinary run — which is why the provenance below exists.

- **`model_switch.apply_embedding_model_switch` is the ONE switch.** `POST
  /search/models` (the settings UI) and `PUT /search/models/neural/active` (ops-only) both call
  it. They used to be two halves of the job and neither half was a switch: `POST` wrote the
  `search.embedding_model` / `search.embedding_dimension` settings and reindexed but never
  touched the ML active model or the ingest pipeline, so **no vector changed and the setting
  lied**; `PUT` repointed the pipeline and recreated the index but never wrote the settings, so
  the reindex coordinator then read the OLD dimension and recreated the index a **second** time
  at the wrong size — a dimension-changing switch was broken outright.
- **There are two SystemSettings keys and nothing reconciled them.**
  `search.embedding_model` is the HuggingFace name and drives the index's `knn_vector`
  dimension; `search.opensearch_model_id` is the ML Commons id and drives the pipeline. Never
  read the first as "what embedded this document" — it is demonstrably able to disagree.
- **Order is load-bearing.** Settings → pipeline → caches → index → reindex. Every coordinator
  reconciles the index against `get_search_embedding_dimension()`, and
  `recreate_index_for_dimension` *deletes the index* when it disagrees, so writing the setting
  first is what makes the fan-out safe: each coordinator sees a match and no-ops.
- **The reindex covers every owner, not the caller.** `reindex_transcripts_task` filters
  `MediaFile.user_id == user_id`, so the "full reindex" the endpoints advertised covered only
  the admin who pressed the button — on a multi-user deployment that left every other user's
  chunks in the previous model's vector space, permanently, via the documented happy path.
  `dispatch_reindex_for_every_owner` dispatches one coordinator per owner of a COMPLETED file.
  N concurrent coordinators is not a new shape: `tasks/search_maintenance_task._dispatch_reindex_tasks`
  already loops one per user.
- **An undeployed model is refused (409), not recorded.** Saving a selection for a model that
  cannot embed anything is what made the legacy path *destructive*: the coordinator honours the
  new dimension, deletes the whole chunks index, and then fails every write because the
  untouched pipeline still emits the old width. ⚠️ The settings UI has **no** register/deploy
  control, so a non-default model currently has to be registered and deployed by API first.

### `embedding_model` is the model, and `"neural"` means UNKNOWN

The chunk mapping has always declared `embedding_model` as a `keyword` and the write site
always filled it with the string `"neural"` — the embedding *mode*. Measured before the fix:
**210,908 documents, cardinality 1**.

- `active_embedding_model()` returns what `ensure_neural_ingest_pipeline` resolved **from the
  pipeline it wrote**, cached beside `_neural_pipeline_available` and cleared by
  `reset_neural_pipeline_state`. A stale label is worse than none — it is a specific claim that
  gets believed.
- The label is the model **name**, not the ML id: re-registering one model yields two ids and
  identical vectors, so an id-keyed label would report a mix where there is none.
- **`"neural"` is kept as the unknown bucket.** Not replaced with a new sentinel (that splits
  the unattributed population in two) and never backfilled with the current model (that asserts
  something about 210,908 documents nobody can know). Unresolvable new writes land there too.
- **No mapping change, so no `_INDEX_VERSION` bump.** The index `_meta` deliberately does *not*
  record the model either: that is a second copy of a fact that can drift from the documents,
  and the `terms` agg reads the documents themselves. Derive, don't record.

### Detecting a mixed index

`survey_embedding_models()` — one bounded `terms` agg (`size: 50`, `missing` bucket for
documents with no field at all), **2.5–6.7 ms** measured against the 210,908-document index.

| verdict | meaning |
|---|---|
| `empty` / `unavailable` | no documents / could not ask — `unavailable` never reads as agreement |
| `unattributed` | only `"neural"`. Every deployment's state before its first post-fix index |
| `uniform` | exactly one named model, no unknowns. The only `comparable` verdict besides `empty` |
| `partially_unattributed` | one named model **beside** the unknown bucket |
| `mixed` | **two or more NAMED models.** Proven, not suspected |

**`partially_unattributed` is deliberately not `mixed`.** Those unknown documents might be from
the same model, and it is the state every existing deployment enters the moment it indexes
anything after this lands — an alarm that fires wrongly for the universal case is one people
learn to ignore in the dangerous one.

Read at `GET /search/models/neural/status` (the endpoint an operator consults before deciding
to reindex), in both switch responses, and once per `search_index_maintenance` tick — the only
**automatic** detector, because `_get_indexed_uuids` only finds files with *no* chunks, so a
file holding old-model chunks looks indexed forever. It logs and does **not** act: dispatching
a full re-embed of every corpus from a beat tick on one aggregation is how a health check
becomes an outage. It is deliberately **not** in `POST /search/repair-indices`, whose machinery
is close/reopen/force-merge and cannot repair a vector space — reporting it there would imply
the repair had addressed it.

`ml_model_service.get_active_model_id()` **refuses to guess.** It used to return
`list_models(deployed_only=True)[0]` — the first hit of an unsorted `match_all` — whenever the
stored model was not deployed, and `ensure_neural_ingest_pipeline` writes whatever it is handed
straight into the pipeline. That silently repointed embedding **with no user action**. Exactly
one deployed model is not a choice and is adopted with a warning (the recovery the fallback
exists for); more than one returns `None` and leaves search on BM25 — loud, obvious and
reversible, where a wrong guess is silent and costs a full re-embed.

## Index v6: two planes in one index (#403 Stage 3)

`_INDEX_VERSION` is **6**, and it is assigned *from*
`services/ingest_artifacts/index_mapping.TARGET_INDEX_VERSION` so the number and the mapping
cannot drift. `transcript_chunks` now holds two kinds of document:

| plane | `doc_type` | `_id` | `chunk_index` |
|---|---|---|---|
| transcript chunk | `chunk` (absent on anything written before v6) | `{uuid}_{n}` | `n` |
| digest section | `digest` | `{uuid}_digest_{n}` | `-1-n` (negative sentinel; `index.sort.field` includes `chunk_index`, and 0 is a real chunk) |

Everything about the shape — the mapping additions, the id scheme, the sentinel, the
`chunk_plane_clause()` / `digest_plane_clause()` helpers, `build_embedding_text` and
`build_digest_documents` — lives in `services/ingest_artifacts/index_mapping.py` and is
**imported, never restated**. It was pinned there a stage early precisely so Stage 3 got one
bump and one reindex.

- **Three predicate builders, and picking the wrong one is silent.** `chunk_plane_query`
  (compat-armed chunk plane — the #400 tail prune, the #405 rename rewrite),
  `digest_plane_query` (digest sections of one file — the digest-orphan prune), and
  `file_plane_query` (**every** plane — file deletion and the full rebuild in
  `reindex_transcript`). A rebuild that used the chunk-plane predicate would leave the
  digests of a shorter re-sectioning behind; a delete that used it would leave a readable
  summary of a deleted recording.
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
- **Search does not return digests; chat does not either, yet.** `_build_filters` carries
  `chunk_plane_clause()`, so both the search UI and `retrieve_chunks` see the chunk plane
  only. That is deliberate: a digest is derived text, it would render as if a speaker had said
  it, and it would receive **neither** read-time masking treatment (addendum G6). The digest
  leg, with its own citation shape, is Stage 4's.
