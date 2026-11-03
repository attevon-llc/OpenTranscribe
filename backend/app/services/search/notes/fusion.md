# search — fusion is selectable per request (#363, #403 Stage 5)

Moved intact from `backend/app/services/search/CLAUDE.md`. Read before touching `search_pipeline`, `SEARCH_RRF_RANK_CONSTANT` or the fusion-id plumbing.

## Fusion is selectable per request (#363, #403 Stage 5 plumbing)

A search pipeline is **query-time** metadata: it is attached per request via the
`search_pipeline` parameter and it touches no document. That is why two fusion strategies can
be A/B'd against one live index with **no reindex and no global state swap** — the property
#363's A/B is built on. Adding a pipeline is safe; changing a *mapping* is not.

`fusion.py` owns what a strategy is; `pipelines.ensure_search_pipeline_exists(cfg)` owns
its lifecycle in the cluster; `search_infra.ensure_fusion_pipeline(cfg)` owns the
per-process cache and returns the id to attach.

| strategy | processor | knobs |
|---|---|---|
| `rrf` *(default)* | `score-ranker-processor` | `rank_constant` (`SEARCH_RRF_RANK_CONSTANT`, 30) |
| `normalization` | `normalization-processor` | `min_max`/`l2`/`z_score` × arithmetic/geometric/harmonic mean, optional per-leg weights |

- **The pipeline id is DERIVED from the parameters, never chosen** — `rrf-60`,
  `norm-min_max-arithmetic_mean`, `norm-l2-harmonic_mean-w70_30`. Two sweep arms that differ in
  any parameter therefore get two ids and can be in flight at once. Weights are encoded as
  integer percent and a weight needing more precision is **refused**, because two arms quietly
  aliased onto one id means one of them measured the other's pipeline.
- **`transcript-hybrid-search` is reserved for RRF at the configured rank constant.** No value
  of `SEARCH_FUSION_STRATEGY` can repoint that name at a normalization body — every deployment
  already holds that pipeline, and overwriting it in place is how a "measurement" silently
  becomes a migration. `SEARCH_FUSION_STRATEGY` selects the *default request-time strategy*,
  nothing about the name.
- **Four attach sites, all threaded** (#363's review comment named the first three; the fourth
  arrived with Stage 4): `two_phase_search._search_with_two_phase` (phase 1),
  `collapsed_search._search_with_collapse` (main hybrid), `chunk_retrieval.retrieve_chunks`,
  `chunk_retrieval.retrieve_digests`. The id is passed *down* rather than re-read from
  `settings` at each site, so an arm cannot lose its strategy on the way to the wire.
- **`_verified_pipelines` is a set, and it is why tests clear it.** A
  single "the pipeline is verified" bool let the first strategy a process saw certify every
  later one, so arm B would attach an id nobody created and OpenSearch would run the query
  **unfused** — a plausible number, not an error.
- **The search response cache keys on the resolved pipeline id.** Without it arm B replays arm
  A's page for `SEARCH_CACHE_TTL_SECONDS`. Same rule as `redaction/export_policy.py`: a cached
  artifact must name the policy it was rendered under.
- **The self-heal now compares the whole processor block**, not just `rank_constant`.
  OpenSearch echoes a pipeline body back verbatim (no defaults injected), so the comparison is
  exact. A hand-written test stand-in that omits a field therefore reads as drift.
- **A one-leg body still attaches nothing.** `semantic` and `keyword` modes have nothing to
  fuse; selecting a strategy must not change that.
- **Config is env-only and deliberately NOT in `SystemSettings`**, matching the sibling
  `SEARCH_RRF_RANK_CONSTANT`: `SEARCH_FUSION_STRATEGY`, `SEARCH_NORMALIZATION_TECHNIQUE`,
  `SEARCH_COMBINATION_TECHNIQUE`, `SEARCH_COMBINATION_WEIGHTS`. These are measurement knobs, and
  a per-request argument is the supported way to use them. Nothing is exposed on `/api/search`.
- ⚠️ **Measure the chat path and the search path separately.** They fuse over different
  candidate depths — search always over `SEARCH_RRF_WINDOW_SIZE` (500), chat over
  `dynamic_rrf_window(size) = max(100, min(size*4, 500))`. An A/B on one does not characterise
  the other. Evidence that the switch is honoured, not merely accepted, lives in
  `tests/integration/test_fusion_strategy_switch.py`: RRF scores are structurally bounded by
  `2/(k+1)` = 0.0645 at k=30 and the normalization arm measurably exceeds it (0.500 on the
  210,908-document eval index), with the same-pipeline-twice run as the control.

### MEASURED: RRF stays. Ten arms, zero both-corpus wins (#363 closed, #403 Stage 5)

**The default did not change, and the sweep is why — not inertia.** `scripts/benchmark_rag.py`
takes `--fusion` / `--rank-constant` / `--normalization-technique` / `--combination-technique` /
`--combination-weights`, and ten arms were measured over 1,651 queries against one unchanged
index (`indexing.total` 825,795 throughout). Headline nDCG@10 on the `all` row, control
`rrf-30-default` = 0.0983 (QMSum) / 0.2952 (synthetic):

| arm | QMSum | synthetic |
|---|---|---|
| `rrf-60` | −0.4% | **+2.1%** |
| `norm-z_score-arithmetic_mean` | **+1.5%** | −23.3% |
| `norm-min_max-arithmetic_mean` | **+0.8%** | −18.8% |
| `norm-min_max-arithmetic_mean` w70/30 | **+1.4%** | −49.1% |
| `norm-min_max-geometric_mean` | −15.5% | −67.2% |
| `norm-min_max-harmonic_mean` | −16.4% | −69.7% |

Four things to know before re-opening this:

- **OpenSearch's BEIR result (normalization +3.86% nDCG@10 over RRF) does not transfer.** Best
  case here is +1.5% on one corpus while losing 23% on the other. #363 was opened on the
  hypothesis that a BEIR average says nothing about transcript retrieval; it does not.
- **Geometric and harmonic mean are structurally wrong for this index, not merely worse.** Both
  are zero if either input is zero, and a normalised single-leg hit scores 0 on the leg that
  missed it — so they *annihilate* single-leg hits where RRF still gives them `1/(k+rank)`.
  Speaker-turn chunks average 17 words, so single-leg hits are the common case. Only worth
  retesting if chunk granularity changes.
- **The two corpora want opposite leg weights** — BM25-heavy is the best arm on QMSum lookup and
  the second-worst on synthetic lookup; vector-heavy is the exact mirror. Either would pass a
  single-corpus gate and regress the other half. This is the concrete case D5's both-tier rule
  exists for.
- **No arm is measurably faster or slower.** p50 ≈ 178 ms, p95 ≈ 260–274 ms for all of them. One
  run put `rrf-60` at +50% p95; it did **not** reproduce across two interleaved re-measurements
  and was machine noise. Do not quote a single latency run.

Full method, every arm's command line, the negative results with their margins, and the
reranker licence gate: `docs-site/docs/developer-guide/rag-evaluation.md` → "Stage 5 — the
retrieval tuning bake-off".
