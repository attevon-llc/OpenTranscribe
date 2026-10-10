# Issue #963 — Rebuild OpenSearch summary indexing as a proper leg — Implementation Plan

Status: PLAN ONLY, not started. Written 2026-09-20 for issue
[#963](https://github.com/attevon-llc/OpenTranscribe/issues/963) (`feat(search): rebuild
OpenSearch summary indexing as a proper leg (single source of truth, no duplicate writes)`,
milestone v0.6.0, labels `enhancement` `backend` `performance` `rag-chat` `search`
`epic:rag-quality`). A fresh agent with no conversation memory should be able to execute this
end-to-end from this file alone.

**Verified against base commit `aeb4dc414557544be186a2a5104a9fe5fab1f188`**
(`aeb4dc41 Merge pull request #955 from attevon-llc/chore/v0.5.1-followup-dependency-batch`) —
the head of the `feat/v0.6.0-frontend-ux` worktree — 2026-09-20. Every line number below was
read from `master` at `93bad998`, and
`git diff --stat aeb4dc41 93bad998 -- backend/app/services/search/ backend/app/api/endpoints/search.py backend/app/services/ingest_artifacts/ backend/app/tasks/ backend/app/models/media.py backend/tests/unit/test_chunk_plane_compat_arm.py backend/tests/unit/test_transcript_summaries_index_retired.py docs/specs/`
returns **empty** — the two trees are byte-identical across every file this plan touches, so
the line numbers hold in the worktree. Re-derive with `git rev-parse HEAD` before trusting
them; `hybrid_search_service.py` and `indexing_service.py` are edited often.

Branch for this work: `feat/v0.6.0-frontend-ux` (worktree `.claude/worktrees/v0.6.0-frontend-ux`),
shared with other v0.6.0 lanes. Per the root `CLAUDE.md` concurrency rule, **only one writer in
that checkout commits** — if another lane is active, hand over exact paths plus a commit message
instead of running `git commit` yourself. This lane is backend-heavy and touches
`backend/app/services/search/*`, which no frontend lane should be in.

---

## ⚠️ Premise corrections — read before doing anything

Roughly half the issue bodies in this repo carry a wrong premise. Five were found here.
**Correcting them is part of the deliverable**, not commentary.

### PC1 — the "6 dead methods" are ALREADY GONE. There is nothing to delete.

The issue's scope item 6 reads as though `get_summary`, `get_summary_analytics`,
`update_summary` and the three `_process_*_aggregation` helpers are still sitting in the tree.
They are not. Commit `39e96d239` deleted
`backend/app/services/opensearch_summary_service.py` whole (645 lines) along with all six.
Verified: **zero definitions and zero call sites anywhere under `backend/app/`.**

The only surviving textual traces are prose:
`backend/app/api/endpoints/summarization.py:429` (a comment naming
`OpenSearchSummaryService.get_summary_analytics` as removed), and
`backend/tests/unit/test_transcript_summaries_index_retired.py:53-55,119` (the tests that
assert it is gone). Name collisions with *unrelated* features exist and must not be touched:
`app/utils/hardware_detection.py:570` (`HardwareConfig.get_summary`),
`app/api/endpoints/files/summary_status.py:27` (`get_summary_status`),
`app/utils/summary_settings.py:61` (`get_summary_disable_reason`).

**Action: none.** Do not "clean up" the tombstone comments — the AST detector at
`test_transcript_summaries_index_retired.py:60-79` was deliberately built so that prose
explaining the retirement does **not** trip it, precisely so nobody feels pressure to delete
the explanation.

### PC2 — `POST /files/search` is ALREADY UNMOUNTED, and must stay unmounted.

The summarization router is mounted at prefix `/files` (`backend/app/api/router.py:192`). Its
remaining routes are `POST /{file_uuid}/summarize` (`summarization.py:58`),
`GET /{file_uuid}/summary` (`:197`), `GET /{file_uuid}/summary/export` (`:238`),
`POST /{file_uuid}/identify-speakers` (`:434`), `DELETE /{file_uuid}/summary` (`:518`). There
is no `/search` POST route. Two tombstone comment blocks sit where it was
(`summarization.py:410-421` for the route, `:424-431` for the never-reachable
`GET /analytics`), and `test_transcript_summaries_index_retired.py:122-146` pins both the
unmounted path set **and** the wire-level `405` (the literal segment is absorbed by
`/api/files/{file_uuid}`).

**Action: do NOT resurrect it.** The new capability lands on `/api/search` — one search
surface. The separate route is exactly what rotted. Leave those tests green as written.

### PC3 — the #462 spec explicitly REJECTED `doc_type: "summary"`, and that rejection is on
file. This plan overrides it, and has to say why.

`docs/specs/summary-search-and-display.md:10` — *"Supersedes: the 'add `doc_type: summary` to
the v6 index' idea, rejected below"* — and `:33-44` lists four reasons. The 2026-09-09
implementation gist for #462/#760 (`gh gist view fda835e64ed7c1fc76279becc9f9178d`, §6)
restates them as intact.

**You cannot execute this issue without contradicting that spec.** §2 of this plan answers all
four reasons point by point. The spec must be **amended, not silently bypassed** — see Edit 13.
If §2's argument does not convince you when you read it against the code, stop and escalate
rather than building a design the spec forbids.

### PC4 — `media_file.summary_opensearch_id` is not merely "draining", it is now pointless.

`backend/app/models/media.py:104` keeps the column with a comment at `:98-103`. Ten sites
still touch it: eight clear-to-`None` drains
(`tasks/summarization.py:398`, `tasks/summary_retry.py:53,123,140`,
`utils/task_utils.py:856`, `api/endpoints/files/reprocess.py:73,253`,
`api/endpoints/summarization.py:550-551`) and two truthiness reads that feed `has_summary`
(`api/endpoints/files/crud.py:795`, `api/endpoints/files/summary_status.py:81`). Nothing has
ever written a value since `39e96d239`. Four frontend sites read/clear it too
(`frontend/src/lib/types/media.ts:147`, `routes/files/[id]/+page.svelte:1709,1759,2424,2447`,
`components/fileDetail/FileActionButtons.svelte:42`, `components/MetadataDisplay.svelte:135`).

The new plane addresses its documents by **deterministic id** (`{uuid}_summary_{n}`), so there
is no pointer to store, ever. The column's remaining job — "a summary might exist in the old
index even though `summary_data` is null" — was never true: `39e96d239`'s own commit message
says `_persist_summary` wrote *the same dict* to both. Phase 6 drops it, behind a hard SQL
precheck.

### PC5 — the issue says summary search is "slow"; the *measured* cost is the redaction pass
and the whole-tree leaf walk, not the Postgres query.

`search_summaries` (`backend/app/services/search/summary_search.py:284-318`) walks **every
leaf of every file on the page** with `_walk_leaves` (`:108-131`) before it knows which leaves
matched, then issues one batched `unnest` query (`:134-172`) to find the matching ones, then
runs `mask_summary_leaf` (`:352`) per returned leaf. The `to_tsvector('simple', ...)` over the
serialised JSONB is genuinely unindexed — and the #462 spec (`:57-58`) says a GIN index was
deliberately deferred as unmeasured — but on a ~2.5k-file corpus that is not where the
seconds are. The measured Presidio numbers are in `backend/app/services/search/CLAUDE.md`
("Cost, and why it is not the fail-closed branch's problem"): **0.8–2.1 s** per page for a
`pii`-masking user against a 305–1372 ms baseline search, and **2.6–13.5 ms** for a
profanity-only user.

This matters because it tells you what "make it fast" means: §5 removes the whole-tree walk
(OpenSearch hands back exactly the matching leaves) and gives the leg the policy-keyed
response cache it currently has none of. It does **not** mean masking moves to index time —
see §5, which rejects that and explains why the arithmetic does not even favour it.

---

## 1. What exists today — the map

### 1.1 The summary leg (the thing being replaced)

| Piece | Path | Notes |
|---|---|---|
| Postgres FTS search | `backend/app/services/search/summary_search.py` (368 lines) | `search_summaries()` at `:181`. `websearch_to_tsquery('simple', …)` over `cast(MediaFile.summary_data, String)` at `:246-248`; `ts_rank` order at `:282`, `id` tie-break at `:294`. |
| Leaf walker | same, `:108-131` | `_walk_leaves(node, path)` → `[(key_path, text)]`. Skips `_UNMASKED_TOP_LEVEL_KEYS` (`{"metadata"}`) at the top level only. **Reuse this verbatim — do not restate it.** |
| Leaf matcher | same, `:134-172` | One `unnest(int[], int[], text[])` query per page. Array-parallelism invariant documented at `:144-147`. Dies with the Postgres leg. |
| Result dataclasses | same, `:76-105` | `SummarySectionMatch(key_path, snippet)`, `SummaryHit(file_uuid, file_id, title, matches)`, `SummarySearchResult(results, total)`. **Wire contract — keep.** |
| Metadata filters | `backend/app/services/search/summary_filters.py` (287 lines) | `SummarySearchFilters` (`:84-95`), `parse_date_bound` (`:99`), `summary_filter_predicates` (`:275`). A second, SQL implementation of the same filter set the transcript leg expresses in OpenSearch. **Dies in Phase 4.** |
| Endpoint wiring | `backend/app/api/endpoints/search.py` | `result_type` param `:157`; `want_transcripts`/`want_summaries` `:227-228`; filter construction `:282-311`; `_summary_search_payload` `:463`, body `:540-572`; `total_pages` reconciliation `:313-345`. |
| Wire schema | `backend/app/schemas/search.py` | `SEARCH_RESULT_TYPES = ("transcripts","summaries","all")` `:110`; `SummarySectionMatchSchema` `:113`; `SummaryHitSchema` `:129`. |
| Frontend contract | `frontend/src/stores/search.ts`, `frontend/src/components/search/SummaryResultCard.svelte`, `frontend/src/lib/utils/summaryKeyPath.ts` | `key_path` is resolved against the downloaded `summary_data` to scroll the modal to the section. **The exact `key_path` string format is a wire contract.** |
| Masking | `backend/app/services/redaction/summary_redaction.py` | `mask_summary_leaf` `:174`; shared policy resolver `resolve_summary_leaf_policy` `:134`; `MASKABLE_CATEGORIES = {pii, profanity, custom}` `:61`; `SummaryMaskingUnavailableError` `:71`. |

### 1.2 The chunks index — what the summary plane will join

`transcript_chunks` (`backend/app/core/config.py:598`), read alias `transcript_search`
(`indexing_service.py:48`). Two planes today (`backend/app/services/search/CLAUDE.md`,
"Index v6"):

| plane | `doc_type` | `_id` | `chunk_index` |
|---|---|---|---|
| transcript chunk | `chunk` (absent pre-v6) | `{uuid}_{n}` | `n` |
| digest section | `digest` | `{uuid}_digest_{n}` | `-1-n` |

- Shape home: `backend/app/services/ingest_artifacts/index_mapping.py`. `TARGET_INDEX_VERSION = 6`
  `:57`; `DOC_TYPE_FIELD` `:62`; `DOC_TYPE_CHUNK` `:66`; `DOC_TYPE_DIGEST` `:69`; `DOC_TYPES`
  `:71-74`; `VERBATIM_DOC_TYPES` `:79`; `TARGET_MAPPING_ADDITIONS` `:89-93`;
  `chunk_plane_clause()` `:96-117`; `digest_plane_clause()` `:120-122`; `digest_document_id()`
  `:125-132`; `digest_chunk_index()` `:135-142`; `build_embedding_text()` `:145-186`;
  `build_digest_documents()` `:198-259`; `digest_document_ids()` `:264-266`.
- Index body: `indexing_service.py:201-304`. `_meta` at `:235-242` carries `version` +
  `additive_version`. kNN at `:275-288` (`cosinesimil`, `lucene`, hnsw 256/16).
- **Two version tiers.** MAJOR (`_INDEX_VERSION`, `indexing_service.py:41`) is log-only at
  `_check_index_version` `:890-913` and only lands via `reindex_task._check_and_recreate_stale_index`
  `:362-470`, which **deletes the index**. MINOR is `AdditiveMappingStep` `:51-80` +
  `ADDITIVE_MAPPING_STEPS` `:86-120` + `_ADDITIVE_VERSION` `:124`, applied in place with
  `indices.put_mapping` by `_apply_pending_additive_steps` `:916-987`. ⚠️ Step 2 is
  **retired but retained**; a step version is never reused (`:104-118` says why).
- Plane predicates: `chunk_plane_query` `:323`, `digest_plane_query` `:371`, `file_plane_query`
  `:399`. Deletion is `delete_transcript_chunks` (`:1368`, uses `file_plane_query`).
- Write path: `index_transcript_chunks` `:1127`; digest plane regenerated inside it at
  `:1311-1329` by `_index_digest_plane` `:1696`; orphan prune `_prune_stale_digests` `:1807`
  using `_orphaned_document_ids` `:1463` with `counter_field="digest_section"`; bulk writer
  `_bulk_index_documents` `:1853`.
- Embeddings: server-side ingest pipeline, `field_map {"embedding_text": "embedding"}`
  (`indexing_service.py:652`). **Attached per bulk action, never as the index's
  `default_pipeline`** — `:1869-1870` and `:1983-1984`. Consequence: `update_by_query` can
  never re-embed.
- Read path: `HybridSearchService._build_filters` `:1565`, which appends `chunk_plane_clause()`
  unconditionally at `:1610`. Two callers: `:995` and `:1695`. Text query builder
  `_build_text_query` `:1773`; field boosts `_get_search_fields` `:1928-1955`
  (`content^3` / `content.exact^2` / `title^2` / `speaker^3`).
- ACL / tenancy rewrites that MUST reach every plane (addendum G5):
  `tasks/search_indexing_task.py::update_file_access_index` (`:366-371`, keys on `file_id`),
  `update_file_tags_index`, `tasks/tenant_backfill_task.py::_backfill_transcript_chunks`
  (`:206`, keys on `file_uuid`).
- **The structural gate**: `backend/tests/unit/test_chunk_plane_compat_arm.py` (210 lines)
  sweeps every function in `app/` that calls `search`/`msearch`/`count`/`delete_by_query`/
  `update_by_query` and fails any that does not name one of `_DECIDED` (`:40-46`:
  `chunk_plane_clause`, `chunk_plane_query`, `digest_plane_query`, `file_plane_query`).
  Deliberate exceptions are an allowlist with written reasons (`:49-…`), and
  `test_the_allowlist_cannot_outlive_its_subjects` (`:154`) fails a stale entry.

### 1.3 The #67 remnants that must be LEFT ALONE

These purge a **different index** (`transcript_summaries`) that still exists on upgraded
deployments. Deleting them is the GDPR gap `39e96d239` named.

- `backend/app/core/config.py:595` — `OPENSEARCH_SUMMARY_INDEX: str = "transcript_summaries"`.
- `backend/app/services/file_cleanup_service.py:588` `_erase_summary_docs`, called at `:472`.
  `indices.exists`-guarded at `:597-598`, so it no-ops where the operator already dropped it.
- `backend/app/tasks/opensearch_integrity_task.py:328` (sweep config tuple), `:554-568`
  (census block).
- The AST guard: `backend/tests/unit/test_transcript_summaries_index_retired.py`, whose
  `_LEGACY_PURGE_ALLOWLIST` (`:45-50`) is exactly those three modules.

**The new plane lives in `transcript_chunks` and is therefore covered by the existing
`file_plane_query` deletion and `opensearch_integrity_task._cleanup_index_by_field` sweep with
no new erasure code at all.** That is one of the reasons §2 picks a plane over an index.

---

## 2. DECISION 1 — a third plane in `transcript_chunks`, not a new index

**Decision: `doc_type: "summary"` documents inside the existing `transcript_chunks` index,
one document per summary leaf, reachable only through an explicit `summary_plane_clause()`.**

### 2.1 Why not a dedicated index — the #67 lesson, applied properly

`39e96d239`'s defect was not "summaries were in OpenSearch". It was **two stores treated as
parallel primaries** plus **a second index with its own six lifecycle paths, none of which
anybody maintained**. A dedicated `transcript_summaries_v2` would have to grow, from scratch
and forever:

| lifecycle concern | a new index needs | a third plane gets for free |
|---|---|---|
| creation + mapping versioning | its own `_INDEX_VERSION`/additive machinery | `ensure_chunks_index_exists` `:442`, `_apply_pending_additive_steps` `:916` |
| kNN dimension reconciliation on a model switch | its own `recreate_index_for_dimension` + a fourth coordinator | `model_switch.apply_embedding_model_switch` already recreates and re-embeds it |
| ACL rewrite on a share change | a second `update_file_access_index` | `search_indexing_task.py:371` (keys `file_id`), already allowlisted to reach every plane |
| tenant stamping | a second backfill | `tenant_backfill_task.py:206` (keys `file_uuid`) |
| file deletion / GDPR erasure | a fourth `_erase_*_docs` | `file_plane_query` `:399` already deletes every plane |
| orphan sweep + admin census | a new `index_configs` entry + overview block | `opensearch_integrity_task._cleanup_index_by_field`, already allowlisted as whole-file |
| embedding | its own ingest pipeline or no embeddings at all | the same `embedding_text` pipeline; **the retired index had NO embeddings** (`docs/specs/summary-search-and-display.md:21`) |

Seven paths, each of which is exactly the kind of thing that was uncalled and unnoticed last
time. **A plane adds one field and one clause; an index adds seven maintenance obligations.**

### 2.2 Answering the #462 spec's four rejections head-on

`docs/specs/summary-search-and-display.md:33-44`. Each reason is about **chat retrieval
grounding**, and each is neutralised by plane isolation rather than by a separate index.

1. **"Grounding — a citation must point at words someone actually said."** Correct, and
   unaffected. `HybridSearchService._build_filters:1610` appends `chunk_plane_clause()`, which
   matches `doc_type == "chunk"` **or the field being absent**. A summary document carries
   `doc_type: "summary"`, so it is excluded from the transcript results page by construction.
   `chat_retrieval.retrieve_chunks` (`chunk_retrieval.py:283`, clause at `:503-505`) and
   `retrieve_digests` (`:432`, clause at `:478,:507`) likewise cannot see it. `VERBATIM_DOC_TYPES`
   (`index_mapping.py:79`) stays `(DOC_TYPE_CHUNK,)` — **a test pins that summaries are never
   added to it** (§8, T-G4). Chat grounding is untouched by this issue; no chat code changes.
2. **"Instability — summaries change shape on regeneration, so an entry is unstable to rank
   against."** This is a real property and it is handled the way the digest plane handles the
   identical property: the plane is **fully rebuilt** on every write, ids embed the leaf
   ordinal, and a shorter re-summarisation is pruned by `_prune_stale_summary_leaves`
   (§4.4, modelled on `_prune_stale_digests:1807`). Ranking instability across regenerations is
   a property of the *content*, not of the index, and it applies to the Postgres leg today
   exactly as much — `ts_rank` over a rewritten blob is no more stable.
3. **"D6 — the no-LLM deployment has no summaries at all."** With `LLM_PROVIDER` empty,
   `summary_data` is `NULL` on every row, `build_summary_documents` returns `[]`, the plane is
   empty, and the summary leg returns `{"summary_results": [], "summary_total": 0}` — which is
   what it returns today from Postgres. Nothing silently vanishes because nothing silently
   appears. Pinned by T-B7.
4. **"RRF competition — two representations of one recording in one fused ranking."** This is
   the strongest reason and it is the one that decides the *query* design, not the *storage*
   design. The summary plane gets **its own fused query** (its own BM25 leg + its own kNN leg,
   fused by the same RRF pipeline), returned as its own result list with its own total. It is
   **never** merged into the transcript leg's ranked sequence. That preserves the deliberate
   decision already recorded at `api/endpoints/search.py:333-341`: *"Nothing is interleaved
   into a single ranked sequence either: an RRF fusion score and a `ts_rank` are not on a
   common scale."* After this change both legs are RRF scores — and they are **still not
   merged**, for reason 4's sake. See §6.

### 2.3 The objection `summary_search.py` itself raises, and the answer

`summary_search.py:6-10` argues against OpenSearch: *"the access-control authority
(`PermissionService.get_accessible_file_ids_subquery`) is a SQL predicate already, so a second
round trip through OpenSearch would buy nothing but a second place for that rule to drift."*

That is a good argument against a **new index**, and it is wrong about a **plane**. The
denormalised `accessible_user_ids` copy of the sharing rule **already exists** and is already
maintained — the transcript leg has depended on it since v1, and `update_file_access_index`
rewrites it on every share change. Putting summaries in the same index means they inherit
that one copy. Putting them in a *second* index would create the second place to drift, which
is the thing the docstring is right to fear.

⚠️ **Non-negotiable consequence (addendum G5):** every summary document must carry **both**
`file_id` (what the ACL rewrite keys on) and `file_uuid` (what the tenant backfill keys on).
A summary missing either becomes unreachable by one of them, which is a **permission leak**,
not a relevance bug. Pinned by T-B3 and T-I5.

---

## 3. DECISION 2 — exact mapping changes (no `_INDEX_VERSION` bump, no destructive reindex)

Every field a summary document needs is **already mapped** except two. So this is an
**additive** step, not a MAJOR bump.

### Edit 1 — `backend/app/services/ingest_artifacts/index_mapping.py`

Add, next to the digest constants:

```python
#: An LLM-authored summary leaf (issue #963). Derived, interpretive text: it is NOT in
#: :data:`VERBATIM_DOC_TYPES` and must never reach the transcript results page or chat
#: retrieval. Reachable only through :func:`summary_plane_clause`.
DOC_TYPE_SUMMARY = "summary"

DOC_TYPES: tuple[str, ...] = (DOC_TYPE_CHUNK, DOC_TYPE_DIGEST, DOC_TYPE_SUMMARY)

#: Base of the `chunk_index` sentinel band reserved for summary leaves. Digests occupy
#: -1, -2, … (:func:`digest_chunk_index`); a shared band would let leaf 0 of a summary and
#: section 0 of a digest sort into the same `index.sort.field` slot. A million sections is
#: not reachable — `ingest_artifacts/sizing.py` bounds a digest far below that — so the two
#: bands are provably disjoint (pinned by `test_the_sentinel_bands_never_collide`).
SUMMARY_CHUNK_INDEX_BASE = -1_000_000


def summary_plane_clause() -> dict[str, Any]:
    """Match only summary-leaf documents. No compat arm — the plane is all new."""
    return {"term": {DOC_TYPE_FIELD: DOC_TYPE_SUMMARY}}


def summary_document_id(file_uuid: str, leaf_index: int) -> str:
    """``{uuid}_summary_{n}`` — disjoint from ``{uuid}_{n}`` and ``{uuid}_digest_{n}``."""
    return f"{file_uuid}_summary_{leaf_index}"


def summary_chunk_index(leaf_index: int) -> int:
    return SUMMARY_CHUNK_INDEX_BASE - int(leaf_index)
```

Extend `TARGET_MAPPING_ADDITIONS`? **No.** That dict is what v6 shipped; editing a landed
mapping constant changes what version 6 meant on every deployment that already has it. The
two new fields go in an **additive step** instead (Edit 2).

Add `build_summary_documents` + `summary_document_ids` to the same module, mirroring
`build_digest_documents:198-259`:

```python
def build_summary_documents(
    *,
    file_uuid: str,
    file_id: int,
    summary_data: dict[str, Any],
    facts: dict[str, Any],
    base_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """One document per string leaf of ``media_file.summary_data``.

    Pure: builds dicts, indexes nothing — the same contract as
    :func:`build_digest_documents`. The leaf walk is imported from
    ``services/search/summary_search.walk_summary_leaves``, NOT restated: the
    ``key_path`` string it produces is a frontend wire contract
    (``frontend/src/lib/utils/summaryKeyPath.ts`` resolves it against the downloaded
    ``summary_data``), and two walkers would be two chances to disagree about it.
    """
```

Per-document fields:

| field | value | why |
|---|---|---|
| `doc_type` | `"summary"` | the discriminator |
| `file_id`, `file_uuid` | both | G5 — ACL rewrite keys on one, tenant backfill on the other |
| `chunk_index` | `summary_chunk_index(n)` | `index.sort.field` needs one |
| `summary_leaf_index` | `n` | the prune counter (`_orphaned_document_ids(counter_field=…)`) |
| `summary_key_path` | e.g. `major_topics[0].key_points[2]` | the wire contract |
| `content` | the raw leaf text | what BM25 scores |
| `embedding_text` | `build_embedding_text(title=…, recorded_at=…, roster=…, body=leaf)` | what the ingest pipeline embeds; **reuse, do not invent a second header format** |
| `speakers` | `facts["roster"]` | so the `speakers` filter works on this plane |
| `title`, `tags`, `collection_ids`, `accessible_user_ids`, `organization_id`, `upload_time`, `language`, `content_type`, `duration`, `file_size`, `indexed_at`, `embedding_model` | from `base_metadata` | identical to the digest plane |
| `speaker` (singular) | **`document.pop("speaker", None)`** | a summary is not attributable to one speaker; leaving it set puts derived text in the speaker facet and in chat's exact `terms` filter (`build_digest_documents:253-256` does the same, for the same reason) |
| `start_time`, `end_time` | **absent** | a summary leaf has no timespan. Do not fabricate one. |

### Edit 2 — `backend/app/services/search/indexing_service.py`, `ADDITIVE_MAPPING_STEPS`

Append (never edit steps 1 or 2, never reuse a version number — `:104-118`):

```python
    AdditiveMappingStep(
        version=3,
        description="summary_key_path / summary_leaf_index for the summary plane (#963)",
        properties={
            # keyword, not text: this is a JSON path the frontend resolves verbatim
            # (frontend/src/lib/utils/summaryKeyPath.ts). Analyzing it would make it
            # unusable as an exact term and buy nothing — it is never searched.
            "summary_key_path": {"type": "keyword"},
            # The prune counter, sibling of `digest_section`. NOT `chunk_index`: a
            # summary's is a negative sentinel that counts the wrong way.
            "summary_leaf_index": {"type": "integer"},
        },
    ),
```

`_ADDITIVE_VERSION` (`:124`) becomes 3 automatically. `_ADDITIVE_MAPPING_ADDITIONS` (`:129`)
folds the fields into a fresh index automatically. An **existing** index gets them by
`indices.put_mapping` on the next `ensure_chunks_index_exists()` call, with no downtime and no
data loss.

**Do NOT bump `_INDEX_VERSION` / `TARGET_INDEX_VERSION`.** A bump means
`_check_and_recreate_stale_index` **deletes** `transcript_chunks` and every owner re-embeds
(`reindex_task.py:362-470`, `model_switch.dispatch_reindex_for_every_owner`). Nothing here
changes a type, an analyzer, or the vector dimension, so there is nothing a recreation would
fix. The #462/#760 gist's §6 warning — that a half-finished MAJOR migration is *undetectable*
because `_meta` carries no chunking provenance and `_get_indexed_uuids` only finds files with
*no* chunks — is the reason this is worth being careful about. The additive path sidesteps all
of it.

### Edit 3 — `backend/app/services/search/indexing_service.py`, plane predicate

Add beside `digest_plane_query:371`:

```python
def summary_plane_query(
    file_uuid: str,
    *,
    from_leaf: int | None = None,
) -> dict[str, Any]:
    """The summary-plane sibling of :func:`chunk_plane_query`. No compat arm."""
    filters: list[dict[str, Any]] = [
        {"term": {"file_uuid": file_uuid}},
        digest_mapping.summary_plane_clause(),
    ]
    if from_leaf is not None:
        filters.append({"range": {"summary_leaf_index": {"gte": from_leaf}}})
    return {"bool": {"filter": filters}}
```

`file_plane_query:399` needs **no change** — it deliberately has no `doc_type` predicate, so
deletion and `count_file_documents` already cover the new plane. Verify with T-I6.

---

## 4. DECISION 3 — the write path: single source of truth, structurally

### 4.1 The rule, and how it is made unbreakable

**Postgres `media_file.summary_data` is canonical. OpenSearch is derived and rebuildable.
Nothing ever writes the same dict to both.**

The enforcement is not a comment. `_index_summary_plane` **reads `summary_data` from the
database in its own session** and **has no parameter through which a summary payload could be
passed in**. There is no argument shape that lets a caller hand it a second copy, so the #67
defect (*"`_persist_summary` wrote the SAME dict to the column and to OpenSearch in one
task"*) is not merely discouraged — it is unexpressible. T-G2 asserts the signature.

### 4.2 Edit 4 — `TranscriptIndexingService._index_summary_plane`

New method in `backend/app/services/search/indexing_service.py`, placed immediately after
`_index_digest_plane` (`:1696-1805`) and modelled on it line for line.

```python
    def _index_summary_plane(
        self,
        *,
        file_id: int,
        file_uuid: str,
        base_metadata: dict[str, Any],
        use_neural: bool,
    ) -> int:
        """Rebuild this file's summary-leaf documents from Postgres (#963).

        ⚠️ There is deliberately **no `summary_data` parameter**. The summary is read
        here, from ``media_file``, on this method's own session. That is the whole
        anti-regression for #67: the retired implementation took the dict the
        summarization task had just produced and wrote it to the column and to
        OpenSearch in the same task, making two stores into two primaries. With no
        argument to pass, a caller cannot do that even by accident.
        ...
        """
```

Body, in order:
1. `with session_scope() as db:` — its **own** session (same reason `_index_digest_plane`
   gives at `:1727-1730`: the callers are a Celery task holding one across a batch and an API
   path holding none). Load `MediaFile.summary_data`. **Note:** `reindex_task._load_reindex_page`
   `defer(MediaFile.summary_data)` at `:892`, so it is genuinely not on the object the reindex
   path holds — reading it here is required, not redundant.
2. Also load `file_facts.facts` via `generate_file_artifacts(db, file_id)` for `roster` +
   `recorded_at` (the `embedding_text` header). The `source_fingerprint` short-circuit makes an
   unchanged transcript cost a SHA-256 (`_index_digest_plane:1732-1735`); `_index_digest_plane`
   has usually just run it on the same file, so this is warm.
3. Close the session **before** any OpenSearch call (the `audit-session-lifetime.py` rule).
4. `documents = build_summary_documents(...)`; `ids = summary_document_ids(...)`.
5. `written = self._bulk_index_documents(list(zip(ids, documents, strict=True)), use_neural)`
   — the existing writer (`:1853`), which attaches the ingest pipeline at `:1869-1870`. **No
   new bulk writer. No new pipeline.**
6. `self._prune_stale_summary_leaves(file_uuid, keep_count=len(documents))`.
7. Report what **landed**, not what was built (issue #495): `if written < len(documents):
   logger.error(...)`.
8. `except Exception: logger.error(...); return 0` — **does not raise**, matching
   `_index_digest_plane:1802-1804`. The asymmetry is deliberate and must be preserved: the
   chunks are the transcript itself and a partial chunk index raises (`:1279-1286`); a summary
   is derived enrichment and must not fail an otherwise-good index.

`summary_data` of `None`, `{}`, or a non-dict → return 0 after pruning to 0. A file that
**lost** its summary must lose its documents; returning early without the prune leaves a
readable summary of a summary that no longer exists.

### 4.3 Edit 5 — call it from `index_transcript_chunks`

`indexing_service.py`, immediately after the `_index_digest_plane(...)` call at `:1311-1329`:

```python
            summary_count = self._index_summary_plane(
                file_id=file_id,
                file_uuid=file_uuid,
                base_metadata=base_metadata,   # hoist the dict built at :1314-1328
                use_neural=use_neural,
            )
```

and add `"summary_leaves": summary_count` to the returned stats dict (`:1346-1355`).

**This call is mandatory and is the single most important line in the change.** Addendum G1's
reasoning applies verbatim: `delete_transcript_chunks` is unqualified (`file_plane_query`), and
**every** rebuild trigger — version bump, model switch, maintenance repair, manual reindex,
`reindex_dispatch` debounce — routes through `index_transcript_chunks`. A rebuild that
regenerated chunks and digests but not summaries would destroy the summary plane
**permanently**, with no error and nothing that later re-examines it
(`_get_indexed_uuids` only finds files with *no* chunks). Pinned by T-I2.

Refactor note: lift the `base_metadata` literal currently inline at `:1314-1328` into a local
`base_metadata = {...}` above both calls, so the two planes provably share one dict rather
than two copies that can drift.

### 4.4 Edit 6 — `_prune_stale_summary_leaves`

New method after `_prune_stale_digests` (`:1807-1851`), a near-copy:

```python
            stale_ids = self._orphaned_document_ids(
                index_name=index_name,
                document_id=lambda n: digest_mapping.summary_document_id(file_uuid, n),
                first_orphan=keep_count,
                plane_query=summary_plane_query(file_uuid),
                # NOT `chunk_index`: a summary's is a negative sentinel that counts the
                # wrong way. `summary_leaf_index` is what its document id counts up.
                counter_field="summary_leaf_index",
            )
```

then `indices.refresh()` → `delete_by_query(summary_plane_query(file_uuid, from_leaf=keep_count),
refresh=True, conflicts="proceed")`.

⚠️ **The gate must stay an `mget`, never a `count`** (issue #435). `_orphaned_document_ids`
already is one. The reason is not theoretical: the bulk load above uses `refresh=False`, a
`count` is a search and sees only the last refresh, and the measured result was *the tail
survived 11 of 12 back-to-back index pairs*. The probe is a **window** of `_ORPHAN_PROBE_WINDOW`
(64, `:180`) because a partially failed bulk load leaves holes. T-I4 plants an orphan at
`leaves + 3` for exactly that reason, mirroring `test_a_shorter_resection_leaves_no_orphan_digest`.

### 4.5 Edit 7 — the fresh-summary trigger

`index_transcript_chunks` only runs on transcription/reindex. A summary is generated *later*
and can be regenerated any time, so it needs its own light trigger.

**New task**, `backend/app/tasks/search_indexing_task.py`:

```python
@celery_app.task(name="index_file_summary", priority=CPUPriority.MAINTENANCE)
def index_file_summary(file_id: int) -> dict[str, Any]:
    """Rebuild ONE file's summary plane from `media_file.summary_data` (#963).

    Dispatched AFTER the summarization task's Postgres transaction commits — never
    from inside it. A dispatch inside the transaction would either read uncommitted
    state or, worse, tempt someone to pass the dict along and recreate #67.
    """
```

It resolves `file_uuid` + the same `base_metadata` the chunk path builds (reuse
`reindex_task._extract_file_metadata:116` rather than assembling a third copy — it already
flattens tags/collections/`accessible_user_ids`/`organization_id`), then calls
`TranscriptIndexingService()._index_summary_plane(...)`.

Register the name in `backend/app/core/celery.py`'s `task_routes` → `CeleryQueues.CPU`.
`task_create_missing_queues=False`, so a typo raises at dispatch rather than silently dropping.

**Dispatch sites** (all *after* commit):

| site | path | action |
|---|---|---|
| summary generated | `backend/app/tasks/summarization.py`, after `_persist_summary(...)` at `:697` | `index_file_summary.delay(file_id)` — on the line **after** `_persist_summary` returns, i.e. after `session_scope` committed |
| summary deleted | `backend/app/api/endpoints/summarization.py:518` `delete_summary`, after `db.commit()` at `:560` | same task — `summary_data` is now `NULL`, so the plane prunes to 0 |
| summary force-cleared | `backend/app/tasks/summarization.py:395`, `backend/app/tasks/summary_retry.py:52`, `backend/app/utils/task_utils.py:855`, `backend/app/api/endpoints/files/reprocess.py:73,253` | same task, after each commit |

⚠️ **Do not put the dispatch inside `_persist_summary`.** That function is the #67 crime scene
(`summarization.py:554-589`); its docstring says *"Phase 3 — write (short session, Postgres
only)"*. Keep it Postgres-only and dispatch from its caller. T-G1's AST detector enforces this.

### 4.6 Edit 8 — the ACL/tenant rewrites already reach the plane; prove it, don't change it

`update_file_access_index` (`search_indexing_task.py:371`) is an `update_by_query` keyed on
`file_id` with **no** plane predicate — it is on the compat-arm allowlist for exactly this
reason (G5). It therefore reaches summary documents automatically **provided they carry
`file_id`**, which Edit 1 guarantees. Same for `update_file_tags_index` and
`tenant_backfill_task._backfill_transcript_chunks` (keyed on `file_uuid`).

**No code change here. Two tests instead** (T-I5, T-I7) — because "it works by construction" is
exactly the claim that turns into a permission leak when someone later adds a plane filter to
one of those rewrites for tidiness.

---

## 5. DECISION 4 — redaction: query-time, on raw indexed text. Index-time masking is REJECTED.

### 5.1 The decision

**The summary plane stores RAW (unredacted) leaf text, exactly as `transcript_chunks` stores
raw transcript text. Masking happens at read time, per returned leaf, under the *requesting
user's* effective policy, via the existing `mask_summary_leaf`. Nothing about this changes.**

### 5.2 Why index-time masking is wrong here — five reasons, each concrete

1. **The policy is per-reader, and one index copy cannot carry N policies.**
   `api/endpoints/search.py:544` resolves `resolve_effective_config(db, ctx.user.id)` — the
   *requesting* user, not the file owner (the #85 read-surface rule). Masking at index time
   forces a choice of whose policy to bake in. Choosing the owner's is precisely the
   display/export divergence that made bulk subtitle export a separate defect from transcript
   display (`services/CLAUDE.md`, "Exports and redaction").
2. **It breaks match-on-raw, which is an app-wide invariant.** `summary_search.py:12-21` and
   `services/search/CLAUDE.md` both state it: *"A user's own masked-content search must still
   be able to find the section their policy would mask; only the returned snippets are
   masked."* Index-time masking makes that section unfindable by the person it belongs to.
3. **A false positive becomes permanent.** Presidio mislabels; an allowlist edit fixes a live
   masking pass instantly but would need a corpus-wide re-embed to fix a baked index — and
   `update_by_query` **cannot re-embed** (no `default_pipeline`;
   `reindex_dispatch.py`'s docstring), so it is a full reindex, every owner.
4. **An admin policy change silently makes the index lie.** Toggling
   `redaction.force_export_redacted`, or adding a category, or editing `custom_words`, changes
   what should be masked. A baked index keeps serving yesterday's policy — the same class as
   the fixed cache bug in `services/search/CLAUDE.md` ("the response cache keys on the
   redaction policy").
5. **It does not even save the cost.** The leaves actually returned still have to be rendered
   under the requester's policy. Index-time masking would add a Presidio pass to *indexing*
   (on the CPU worker, per leaf, per reindex) without removing the read-time one.

### 5.3 How the cost actually goes away — and it is not masking

Today's per-request cost has two components, and this change removes both:

1. **The whole-tree leaf walk.** `search_summaries:304` runs `_walk_leaves` over the **entire**
   `summary_data` of **every** file on the page, before anything knows which leaves matched.
   With one document per leaf, OpenSearch returns *exactly the matching leaves* as
   `inner_hits` — typically 1–3 per file. The number of `mask_summary_leaf` calls per page
   therefore drops from "every returned leaf, selected from a full tree walk" to "the handful
   of leaves the query actually hit". Issue #822 already narrowed masking to returned leaves
   (`summary_search.py:348-358`); this narrows the *walk* too.
2. **No response cache at all.** The transcript leg's page is cached for
   `SEARCH_CACHE_TTL_SECONDS` (300 s) keyed on `_redaction_policy_fingerprint(cfg)` plus the
   resolved fusion pipeline id. The summary leg is uncached — every repeat query pays Presidio
   again (the gist's §R3 residual). Routing the summary leg through `HybridSearchService` puts
   it inside that cache with the same policy fingerprint, so a repeat query pays **zero**.

**Measure this, do not assert it.** §9's V3 is the before/after.

### 5.4 Rules that must survive the rewrite

- ⚠️ **Detection is ONE CALL PER LEAF. Batching the page is forbidden.** `en_core_web_sm`
  reports each distinct `PERSON` **once per document**, so packing a page's leaves into one
  `analyze()` call leaks the name from every leaf after the first *while the page still renders
  `[NAME]` elsewhere and looks masked*. Measured on the sibling snippet path: **31 of 32
  snippets** leaked. A results page is by construction a set of fragments about one subject —
  the exact input that property destroys. `snippet_redaction._detect` carries the warning; the
  batched implementation was written, measured, and deleted. T-R3 pins it.
- ⚠️ **`unmask_for_local` / `is_local_provider` have NO bearing here, and wiring them in would
  be a security regression.** Those key the **egress** question ("may this text be *sent to a
  model*?") off where the model runs. Summary search is a **display** surface: no model is
  involved, so there is no egress event to exempt. Put that sentence in
  `summary_search.py`'s module docstring — the gist's §5 names "a future change aligning search
  with chat's local-provider logic" as the specific foreseeable mistake.
- **Fail closed, narrowly.** `mask_summary_leaf` raises `SummaryMaskingUnavailableError` when a
  detector feeding an *enabled* category could not run; `_summary_search_payload:558-560` turns
  it into a **503**. Keep both. A detector outage on a leaf that was never going to be returned
  must **not** raise (#822) — with per-leaf documents that is automatic, since unreturned
  leaves are never fetched.
- **The config session closes before any detector runs.** A ~1 s Presidio pass inside an open
  transaction holds `ACCESS SHARE` and queues every `ALTER TABLE` behind it —
  `scripts/audit-session-lifetime.py` exists to catch exactly that.

### 5.5 The optimisation that is NOT in scope until measured (Phase 5)

If V3 shows a `pii`-enabled page still costing > ~300 ms in masking, the next move is to cache
the **Presidio-derived spans** per leaf in Postgres (a `summary_redactions` JSONB column beside
`summary_data`, computed once on the `celery-redaction` worker) and apply them at read time
filtered by the requester's enabled categories, computing only the cheap wordlist/`custom`
spans live. The split is principled: the measured cost is **0.8–2.1 s** for `pii` and
**2.6–13.5 ms** for the wordlist, and `custom_words`/`allowlist` are per-policy and cannot be
cached across users anyway.

**Do not build this speculatively.** `docs/specs/summary-search-and-display.md:57-58` already
applied the same discipline to the GIN index — *"Measure before adding … an unused index is a
write cost for nothing."* Phase 5 is gated on V3's number.

---

## 6. DECISION 5 — query and fusion integration

### 6.1 Shape

The summary leg becomes a **second hybrid query against the same index**, with the same RRF
search pipeline, differing only in its plane clause and its field boosts.

```
GET /api/search?q=…&result_type=all
  ├─ transcript leg  filters=[acl, org, chunk_plane_clause(), …]  → RRF(BM25, kNN) → results / total_files
  └─ summary leg     filters=[acl, org, summary_plane_clause(), …] → RRF(BM25, kNN) → summary_results / summary_total
```

**The two legs are NOT merged into one ranked sequence.** That is #462 rejection 4 and the
existing decision at `api/endpoints/search.py:333-341`. Two lists, two totals, one
`total_pages` that reaches the end of both. What changes is that the summary leg's ranking is
now RRF over a real BM25 + kNN fusion instead of `ts_rank` over a serialised JSONB blob — i.e.
it becomes **semantically** searchable, which is the capability Postgres FTS can never have and
which the *retired* index never had either (`docs/specs/summary-search-and-display.md:21`:
`summary_content` was mapped `"enabled": False`).

### 6.2 Edit 9 — parameterise `_build_filters`

`backend/app/services/search/hybrid_search_service.py:1565`. Add a keyword-only parameter:

```python
        plane_clause: dict[str, Any] | None = None,
```

and replace the unconditional append at `:1610`:

```python
        filters.append(plane_clause if plane_clause is not None else chunk_plane_clause())
```

Both existing callers (`:995`, `:1695`) are unchanged and keep the chunk plane. The function
still names `chunk_plane_clause`, so `test_chunk_plane_compat_arm.py::_DECIDED` still matches
it. **Do not** give it a `doc_type: str` parameter instead — a clause is what the callers need
to keep passing around, and a bare `{"term": {"doc_type": ...}}` reintroduces the pre-v6
compat hazard for the chunk case.

### Edit 10 — `HybridSearchService.search_summary_plane`

New public method. It reuses, and does **not** reimplement:

- `_build_filters(..., plane_clause=summary_plane_clause())`
- `_build_text_query(query, search_fields)` (`:1773`)
- the RRF pipeline via `ensure_fusion_pipeline(cfg)` and `resolve_fusion`
- `_search_with_collapse`'s collapse-on-`file_uuid` + `inner_hits` shape

Field boosts — a new small sibling of `_get_search_fields` (`:1928`), because the summary plane
has no `speaker` (singular) field by construction:

```python
_SUMMARY_SEARCH_FIELDS = ["content^3", "content.exact^2", "title^2", "speakers^1"]
```

Collapse gives one bucket per file with `inner_hits` carrying the matching leaves; each inner
hit yields `summary_key_path` + `content` → one `SummarySectionMatch`.

⚠️ **Three landmines, all documented in `services/search/CLAUDE.md`:**

1. **Do NOT add `aggs` to the collapsed hybrid body.** OpenSearch 3.4 throws
   `ArrayIndexOutOfBoundsException` in `score-ranker-processor` when a cardinality agg meets
   hybrid + collapse + RRF — the in-code warning is at `hybrid_search_service.py:2135-2139`,
   with a sibling note at `:2957`. So `summary_total` **cannot** be a `cardinality` agg.
   ⚠️ **Incidental finding, fix it in this PR (Edit 15):** `backend/app/services/search/CLAUDE.md`
   cites this gotcha as `hybrid_search_service.py:1464`. That line number is **stale** — `:1462-1466`
   is an unrelated `Args:` docstring block. Correct the citation while you are in that file.
2. **RRF + collapse strips inner-hit *highlights*.** Inner hits themselves survive; the
   `<mark>` tags do not. The summary leg does not use highlights today (it returns the leaf
   text truncated at `_MAX_SNIPPET_CHARS = 400`, `summary_search.py:73,175-178`), so keep that
   and do not add highlighting. A bonus: no `<mark>`-splitting problem for the masker, which is
   the fiddliest part of `snippet_redaction`.
3. **A one-leg body attaches no pipeline.** `semantic`/`keyword` modes have nothing to fuse.
   Inherit that behaviour; do not special-case it.

### 6.3 `summary_total` changes meaning — state it, do not hide it

Today `summary_total` is an **exact** `COUNT(*)` (`summary_search.py:265`). The transcript leg's
`total_files` is `len(grouped)` (`:1293`), i.e. **distinct files within the fused
`SEARCH_RRF_WINDOW_SIZE` (500) window**. After this change `summary_total` becomes the same
kind of number.

That is a wire-semantics change and it must be recorded in the endpoint docstring and in
`schemas/search.py`. It **preserves** #831's actual invariant — *"each leg's own total matches
what that leg's pages actually walk"* — because the pages now walk the same window the total
counts. A file library with more than 500 summary-matching files will report 500. The
alternative (a separate BM25 `count` for the total) is worse: it would disagree with the pages
whenever the kNN leg contributes a file BM25 missed.

### 6.4 Quarantine — the pre-filter must survive, and OpenSearch has no quarantine field

Today `search_summaries` applies `takedown_service.exclude_quarantined` as a **PRE**-filter
(`:263`) *inside* the query that both counts and pages. `summary_search.py:27-31` explains why:
a post-filter cannot correct `total`/`offset`, and a count that includes a taken-down file is a
**content oracle** (#818, same class as #876's `/search/count`).

The chunks index has no `is_quarantined` field, and the transcript leg papers over this with a
page-sized DB post-filter (`_drop_quarantined_search_hits`, `search.py:416`). **Do not copy
that.** It is a known weaker shape and copying it onto a surface that currently pre-filters is
a regression.

**Approach:** resolve the quarantined UUID set from Postgres *before* the OpenSearch query and
add `{"must_not": {"terms": {"file_uuid": [...]}}}`.

- Non-admin only; admins keep `include_quarantined` visibility, matching
  `search.py:555` (`include_quarantined=ctx.user.is_admin`).
- One cheap query: `SELECT uuid FROM media_file WHERE is_quarantined IS TRUE` — the
  quarantined set is an admin takedown list, expected to be tiny. Reuse
  `takedown_service`; do not open-code the predicate (that module's docstring says it is *the
  one place quarantine lives*).
- **Fail closed on size.** Define `_MAX_QUARANTINE_TERMS = 4096` (OpenSearch's
  `index.max_terms_count` defaults to 65536, but a terms clause that large is a different
  problem). Above it, raise → **503**, never "skip the exclusion". Silently dropping the
  clause is a takedown bypass. T-Q2 pins this.
- **Adding an `is_quarantined` field to the index was considered and rejected**: it needs a
  rewrite on every quarantine/release, i.e. a *new* denormalised field that can drift, on a
  security-relevant boolean. The pre-resolve keeps the authority in Postgres, where
  `takedown_service` already is.

### 6.5 Edit 11 — rewrite `search_summaries`, keep its signature and dataclasses

`backend/app/services/search/summary_search.py`. **Keep** the module, `SummarySectionMatch`,
`SummaryHit`, `SummarySearchResult`, `_snippet`, `_MAX_SNIPPET_CHARS`. **Export** the leaf
walker as a public `walk_summary_leaves(node, path="")` (rename of `_walk_leaves:108`) so
`build_summary_documents` imports it. **Delete** `_matching_leaf_indices:134` and the
`ts_document`/`ts_query`/`rank` block `:246-298`.

New body: pre-resolve quarantine → call `HybridSearchService.search_summary_plane(...)` →
map hits to `SummaryHit`/`SummarySectionMatch` → `mask_summary_leaf` per returned leaf (per
leaf, never batched) → `_snippet(...)`.

Signature change: drop `filters: SummarySearchFilters | None` and take the same explicit
filter kwargs the transcript leg takes (`speakers`, `tags`, `date_from`, `date_to`,
`file_type`, `collection_id`, `min_duration`, `max_duration`, `min_file_size`,
`max_file_size`, `language`, `title_filter`, `file_uuid`) so they can be forwarded straight
into `_build_filters`. `db: Session` **stays** (quarantine resolution).

Add a per-match `score` (the RRF score) to `SummarySectionMatch` and
`SummarySectionMatchSchema` — #831 item 2 wanted relevance on this leg, and it is also the
cheapest honest provenance signal that the OpenSearch path ran (T-B2).

### 6.6 Edit 12 — endpoint

`backend/app/api/endpoints/search.py`:

- `:282-311` — delete the `SummarySearchFilters` construction block and the
  `parse_date_bound` try/except. Dates now go to OpenSearch as raw strings, exactly as the
  transcript leg does at `:240-241`; OpenSearch parses them. **The 400-on-bad-date arm
  (`:303-311`) disappears** — note this in the endpoint docstring, it is a behaviour change.
- `_summary_search_payload:463` — forward the same filter kwargs the transcript leg received.
  Keep the `SummaryMaskingUnavailableError` → 503 arm at `:558-560` unchanged.
- `:313-345` — the `total_pages` reconciliation stays exactly as written. Both legs now count
  the same way, which makes `max(...)` more meaningful, not less.
- Update the docstrings at `:283-291` and `:463-539` — several of their sentences ("Postgres",
  "one JSONB blob per file", "`ts_rank`") become false.

### 6.7 What #760 needs once this lands

#760 ships with `summary` as an **opt-in** source specifically because of this leg's cost.
After this:

- **`summary` can be default-on.** It is one OpenSearch round trip against the same index,
  through the same filter builder, inside the same policy-keyed response cache. Flip
  `frontend/src/stores/search.ts`'s default source set once V3's number is recorded.
- **`summary_filters.py` is deleted, so #831's whole defect class disappears structurally.**
  #760 item 1's hardest sub-problem — "one request's two legs disagree about which files the
  caller asked for" — stops being a thing to keep in sync: there is one `_build_filters`.
- **`parse_date_bound` goes with it.** Check for other importers first
  (`rg -n "parse_date_bound" backend/`) — at time of writing its only non-test importer is
  `api/endpoints/search.py:41`.
- **Per-source counts (#760 item 2)** for `title` and `speaker` come from `match_sources` on
  the transcript leg, unchanged by this work. The `summary` count is `summary_total`.
- **No mapping change for #760**, still. #760 changes which fields a query targets — query-time
  metadata, like the fusion pipeline. It rides on top of this work; it does not need any of it
  to be redone.

---

## 7. DECISION 6 — delete vs. repurpose, item by item

| Thing | Current state (verified) | Disposition |
|---|---|---|
| `get_summary`, `get_summary_analytics`, `update_summary`, 3× `_process_*_aggregation` | **Already deleted** by `39e96d239`. Zero definitions, zero callers. | **Nothing to do.** PC1. Do not touch the tombstone comments. |
| `POST /files/search` | **Already unmounted.** Tombstone `summarization.py:410-421`; 405 pinned by `test_transcript_summaries_index_retired.py:131-146`. | **Stays unmounted.** New capability is on `/api/search`. |
| `GET /files/analytics` | Already unmounted; tombstone `:424-431`. | Stays unmounted. |
| `transcript_summaries` index, `OPENSEARCH_SUMMARY_INDEX` (`config.py:595`) | Legacy index; may still hold documents on upgraded deployments. | **KEEP, unchanged.** |
| `file_cleanup_service._erase_summary_docs` (`:588`, called `:472`) | Purges legacy docs; `indices.exists`-guarded. | **KEEP, unchanged.** Deleting it is the GDPR gap #67 named. |
| `opensearch_integrity_task` summary arms (`:328`, `:554-568`) | Sweeps + counts the legacy index. | **KEEP, unchanged.** |
| `test_transcript_summaries_index_retired.py` | The AST guard + allowlist for the above. | **KEEP, unchanged.** It guards the legacy index; the new plane is in a different index and does not trip it. Verify that claim (T-G5) — `summary_plane_clause` contains the literal `"summary"`, not `"transcript_summaries"`, so it must not fire. |
| `summary_filters.py` (287 lines) | The SQL twin of `_build_filters`. | **DELETE** in Phase 4, with `SummarySearchFilters` and `parse_date_bound`. Its whole reason to exist was that the summary leg queried a different store. |
| `summary_search._matching_leaf_indices`, the `ts_rank`/`to_tsvector` block | The Postgres query. | **DELETE** in Phase 4. |
| `summary_search._walk_leaves` | The leaf walker + `key_path` format. | **KEEP and PROMOTE** to public `walk_summary_leaves`. One walker, imported by the document builder. |
| `media_file.summary_opensearch_id` (`models/media.py:104`) + 10 backend sites + 6 frontend sites | Dead pointer, never written since `39e96d239`, drains only. | **DROP** in Phase 6, gated on the SQL precheck in §10. PC4. |

**The rule for this issue:** nothing half-connected. Every path listed above is either
(a) actively used by the new plane, (b) actively purging the *legacy* index and therefore
justified, or (c) deleted. There is no third generation of "kept in case".

---

## 8. Test plan

Run `cd backend && python3 ../scripts/audit-tests.py tests` to zero findings before reporting
any unit ready — it is a **whole-tree** gate and one open finding blocks every commit in the
worktree.

### 8.1 The red-first test — watch this fail before writing the read path

**T-R1 · `backend/tests/integration/test_summary_plane_semantic_retrieval.py::test_a_semantically_near_query_finds_a_summary_with_no_lexical_overlap`**

This is the one test that is **red at HEAD and can only go green through the new index**, and
it proves the specific capability the Postgres leg structurally cannot have.

1. Fixture: a file whose `summary_data` contains a leaf reading (e.g.)
   `"Cut business-trip costs by 20% before the next quarter."` and whose transcript segments
   contain **none** of the query's terms.
2. Index it through the **real** `TranscriptIndexingService`, against the live dev stack.
3. `GET /api/search?q=reduce travel spend&result_type=summaries`.
4. Assert exactly one hit, `key_path` naming that leaf.

At HEAD, `websearch_to_tsquery('simple', 'reduce travel spend')` shares no lexeme with the leaf
→ **zero results**. After the change, the kNN leg retrieves it.

⚠️ **Guard against the two ways this test lies.**
- It depends on the neural pipeline. Assert `is_neural_pipeline_available()` and **fail with an
  explicit message** if it is not — do not `skip`. A silently-skipped test is the exact failure
  mode `scripts/audit-tests.py` exists for. Document the dependency in the module docstring, per
  the repo's "don't silently skip" rule.
- Run it red in a `git archive HEAD` tree, never by reverting files in the shared checkout:
  ```bash
  git archive HEAD | (mkdir -p /tmp/redcheck && tar -x -C /tmp/redcheck)
  cp backend/tests/integration/test_summary_plane_semantic_retrieval.py /tmp/redcheck/backend/tests/integration/
  cd /tmp/redcheck/backend && python3 -m pytest tests/integration/test_summary_plane_semantic_retrieval.py -v   # expect RED
  ```
  And check the output names **your** test — a pass you cannot attribute is not a measurement.

**T-B2 · the provenance assertion** — `summary_results[*].matches[*].score` is present and
strictly positive. The per-leaf `ts_rank` the Postgres path would need to produce this does not
exist there; it is impossible to satisfy without the OpenSearch path.

**T-B1 · the structural one** — `api/endpoints/search.py` no longer imports
`SummarySearchFilters` / `parse_date_bound`, and `summary_search.py` no longer imports
`websearch_to_tsquery`/`ts_rank`. Red before Phase 4, green after.

### 8.2 Guard-the-guard — the AST detector, modelled on `39e96d239`'s

**New file: `backend/tests/unit/test_summary_plane_single_source_of_truth.py`.** Model it
*exactly* on `test_transcript_summaries_index_retired.py:60-102` — detector, then a test of the
detector, then the repo-wide sweep with a named allowlist.

- **T-G1 · the dual-write detector.**
  `_writes_a_second_summary_copy(source) -> set[str]`: `ast.parse` + `ast.walk` over
  `app/tasks/summarization.py`, `app/tasks/summary_retry.py`,
  `app/api/endpoints/summarization.py`. For each `FunctionDef`/`AsyncFunctionDef`, return its
  name if the body contains **both** (a) an `ast.Assign`/`ast.AnnAssign` whose target is an
  `ast.Attribute` with `.attr == "summary_data"`, **and** (b) a `Call` whose function name is
  in `{"index", "bulk", "_bulk_index_documents", "_bulk_index_chunks", "_index_summary_plane",
  "build_summary_documents"}`. That is #67's exact shape — *"`_persist_summary` wrote the SAME
  dict to the column and to OpenSearch in one task"* — expressed structurally. Assert the set
  is empty.
- **T-G2 · the signature invariant.** `inspect.signature(TranscriptIndexingService._index_summary_plane)`
  has **no** parameter named `summary_data`, `summary`, `summary_payload`, or `summary_json`.
  This is the mechanical half of §4.1: there is no argument through which a second copy can
  arrive.
- **T-G3 · THE GUARD-THE-GUARD TEST — `test_the_dual_write_detector_discriminates`.**
  Four assertions, two must-fire and two must-stay-clean, over literal source strings:
  ```python
  assert _writes_a_second_summary_copy(
      "def f(mf, svc):\n    mf.summary_data = d\n    svc._bulk_index_documents(docs, True)\n"
  ) == {"f"}
  assert _writes_a_second_summary_copy(
      "def f(mf):\n    mf.summary_data = d\n"
  ) == set()
  assert _writes_a_second_summary_copy(
      '"""We must never write summary_data and bulk index in one function."""\n'
  ) == set()
  assert _writes_a_second_summary_copy(
      "x = 1  # mf.summary_data = d then _bulk_index_documents(...)\n"
  ) == set()
  ```
  **This test is not optional ceremony.** `--selftest` caught two detectors in each repo
  auditor that matched *nothing* — silently reporting 0 findings, indistinguishable from a
  clean tree. The must-stay-clean cases also encode `39e96d239`'s own lesson: a detector that
  cannot tell prose from code *"pressures the next author to delete the explanation."*
- **T-G4 · `test_a_summary_is_never_verbatim`.** `DOC_TYPE_SUMMARY not in VERBATIM_DOC_TYPES`,
  and `DOC_TYPE_SUMMARY in DOC_TYPES`. One line, and it is the structural statement that a
  summary can never surface as if someone had said it (#462 rejection 1).
- **T-G5 · `test_the_new_plane_does_not_trip_the_legacy_retirement_guard`.** Run
  `test_transcript_summaries_index_retired.py`'s detector over every file this plan edits and
  assert it stays silent. If it fires, the new code is naming the *legacy* index somewhere.
- **T-G6 · extend `backend/tests/unit/test_chunk_plane_compat_arm.py`.** Add
  `"summary_plane_query"` to `_DECIDED` (`:40-46`). Then add
  `test_the_sweep_would_catch_a_summary_plane_reader_that_forgot`: a synthetic source naming
  `client.search` with no `_DECIDED` marker must be reported. The existing
  `test_the_allowlist_cannot_outlive_its_subjects` (`:154`) already stops the allowlist growing
  silently.
- **T-G7 · `test_the_sentinel_bands_never_collide`.** For `n in range(0, 100_000)`:
  `digest_chunk_index(n) != summary_chunk_index(n)`, `summary_chunk_index(n) < SUMMARY_CHUNK_INDEX_BASE + 1`,
  and `summary_document_id(u, n)` differs from both `digest_document_id(u, n)` and `f"{u}_{n}"`.

### 8.3 Builder / document-shape units (`backend/tests/unit/test_summary_index_mapping.py`)

Model on the existing `backend/tests/unit/test_digest_index_mapping.py`.

- **T-B3** — every document carries **both** `file_id` and `file_uuid` (G5). Red if either is
  dropped.
- **T-B4** — `speaker` (singular) is **absent**; `speakers` is present.
- **T-B5** — `key_path` values are byte-identical to `walk_summary_leaves`'s output for the same
  input, including the `a[0].b` array syntax. Uses a nested fixture with a list of dicts.
- **T-B6** — the top-level `metadata` key is skipped (`_UNMASKED_TOP_LEVEL_KEYS`), and it is
  skipped **only at the top level** (a nested `metadata` key is indexed).
- **T-B7** — `summary_data` of `None` / `{}` / a string / a list produces `[]` and never raises.
- **T-B8** — `embedding_text` is `build_embedding_text(...)`'s output, not a second format.
- **T-B9** — `start_time`/`end_time` are absent.
- **T-B10** — `backend/tests/unit/test_index_additive_version.py` still passes and
  `_ADDITIVE_VERSION == 3`; steps 1 and 2 are unchanged byte-for-byte.

### 8.4 Indexing / lifecycle integration (`backend/tests/integration/test_summary_plane_indexing.py`)

- **T-I1** — index a file with a summary; the plane holds one document per leaf with the
  expected ids.
- **T-I2 · the G1 regression** — after `reindex_transcript(file_uuid)` (which deletes via
  `file_plane_query` and rebuilds), the summary documents are **back**. Red if Edit 5 is
  omitted. This is the test that catches the permanent-destruction failure mode.
- **T-I3** — re-summarising with the same content is idempotent: same ids, same count, no
  duplicates.
- **T-I4 · the #435 orphan test** — `test_a_shorter_resummarization_leaves_no_orphan_leaf`.
  Index N leaves, plant an extra document at `N + 3` (beyond the contiguous assumption, exactly
  as `test_a_shorter_resection_leaves_no_orphan_digest` does), re-index with fewer leaves,
  assert zero orphans. Also assert the gate used an `mget` and not a `count` — run the two
  index calls back to back inside the refresh window, which is where a `count` gate measurably
  failed 11 of 12 times.
- **T-I5 · the G5 ACL test** — share the file, run `update_file_access_index([file_id])`,
  assert the summary documents' `accessible_user_ids` changed. Then revoke and assert the
  summary is no longer returned. Red if summary docs lack `file_id`.
- **T-I6** — `count_file_documents(file_uuid)` includes the summary documents, and
  `delete_transcript_chunks(file_uuid)` leaves **zero** (`file_plane_query` covers the plane).
- **T-I7 · the tenant test** — the tenant backfill stamps `organization_id` onto summary
  documents (keyed on `file_uuid`).
- **T-I8** — deleting the summary (`DELETE /files/{uuid}/summary`) prunes the plane to zero.
- **T-I9** — a file with **no** summary contributes no documents and the leg returns an empty
  list with `summary_total == 0` (the D6 / no-LLM deployment, #462 rejection 3).

### 8.5 Read-path and redaction (`backend/tests/redaction/test_summary_plane_redaction.py`)

- **T-R1 / T-R2** — §8.1's semantic hit, plus a lexical hit for the control.
- **T-R3 · `test_the_index_stores_raw_text`** — fetch the summary document straight out of
  OpenSearch by id and assert `_source.content` contains the PII literal, while the same leaf
  rendered through `/api/search` under a `pii` policy contains `[NAME]`. **This is the test
  that would catch index-time masking being smuggled in later.**
- **T-R4** — masking uses the **requesting** user's policy, not the owner's: two users with
  different policies get different renderings of the same leaf on the same query.
- **T-R5** — a detector outage on a **returned** leaf still produces a **503** (fail closed).
- **T-R6** — a detector outage on a leaf that is *not* returned does **not** 503 (#822's
  narrowing).
- **T-R7 · the batching leak** — two leaves from the same file, both naming the same person;
  **both** come back masked. This is the 31-of-32 leak, scaled down. Red if anyone pools the
  page into one `analyze()` call.
- **T-R8** — a `profanity`-only user never constructs Presidio
  (`resolve_summary_leaf_policy:156-162`); assert via the same seam the existing redaction tests
  use.
- **T-R9 · `test_the_summary_leg_never_consults_the_llm_provider`** — grep/AST: neither
  `summary_search.py` nor `hybrid_search_service.search_summary_plane` names
  `is_local_provider` or `unmask_for_local`. §5.4's category error, pinned.

### 8.6 Endpoint / contract

- **T-Q1** — a quarantined file is absent from `summary_results` **and** from `summary_total`
  (#818's pre-filter, not a post-filter).
- **T-Q2** — with the quarantined set over `_MAX_QUARANTINE_TERMS`, the endpoint returns
  **503**, not a page with the exclusion silently dropped.
- **T-Q3** — an admin still sees quarantined files (`include_quarantined=is_admin`).
- **T-C1** — `key_path` values still resolve through
  `frontend/src/lib/utils/summaryKeyPath.ts`. Add a vitest case using a `key_path` captured
  from a real backend response fixture.
- **T-C2** — `result_type=all` still reconciles `total_pages` across both legs
  (`search.py:342-344`), and a summary-only multi-page result is fully walkable.
- **T-C3** — existing `backend/tests/api/test_search_summary*.py` (whatever the current names —
  `rg -l "summary_results" backend/tests`) pass unchanged except for the deliberate
  `summary_total` semantics change and the removed 400-on-bad-date arm. **Update those two
  assertions; do not delete the tests.**

---

## 9. Verification — how we prove it, with numbers

Run each against the live dev stack (`./opentr.sh start dev`), with **zero other writers in the
checkout** and a clean `git status` — a contended run's timings are unusable.

- **V1 · the plane exists and is populated.**
  ```bash
  curl -s localhost:5180/transcript_chunks/_search -H 'Content-Type: application/json' -d \
    '{"size":0,"aggs":{"planes":{"terms":{"field":"doc_type","missing":"__absent__"}}}}' | jq
  ```
  Expect a `summary` bucket. `__absent__` is the pre-v6 population and must still be there.
- **V2 · the additive step landed without a reindex.**
  ```bash
  curl -s localhost:5180/transcript_chunks/_mapping | jq '.transcript_chunks.mappings._meta'
  ```
  Expect `{"version": 6, "additive_version": 3}` — **`version` must still be 6.** A 7 means
  someone bumped `_INDEX_VERSION` and a destructive reindex is pending.
- **V3 · the cost, before and after.** Same query, same user, same policy (`pii` enabled),
  warm Presidio, three runs each, report p50. Capture with `-o junit.xml` / `tee`, once —
  never re-run a slow suite for numbers.
  - Before: `result_type=summaries` at HEAD.
  - After: the same, first request (cold cache) and second request (warm cache).
  - Record all three in this file's §12 and in the PR body. The warm number is what makes
    #760's default-on flip defensible.
- **V4 · no regression on the transcript leg.** Run the same transcript query before and
  after; `total_results`, `total_files` and the top-10 hit ids must be **identical**. Edit 9
  touches `_build_filters`, which the transcript leg uses.
- **V5 · chat is untouched.** `./scripts/run-dev-tests.sh --backend-only` plus a manual chat
  turn; assert no citation has `kind: "summary"`.
- **V6 · the gates.**
  ```bash
  cd backend && python3 ../scripts/audit-tests.py tests        # 0 findings
  scripts/safe-precommit.sh run --all-files
  scripts/safe-precommit.sh run --all-files --hook-stage pre-push
  ./scripts/run-dev-tests.sh --full
  ```
- **V7 · the upgrade path.** On a `--fresh` stack seeded from a pre-change backup:
  `./opentr.sh start dev --fresh s963` → confirm `additive_version` walked 2 → 3 in place,
  the index was **not** deleted, and pre-existing chunk documents still return.

---

## 10. Phasing

Each phase is independently committable and leaves the tree green.

| Phase | Content | Gate to proceed |
|---|---|---|
| **P0** | Edits 1–3 (shape, additive step, plane query) + T-B*, T-G4, T-G7, T-B10. **No behaviour change.** | V2 shows `additive_version: 3`, `version: 6`. |
| **P1** | Edits 4–7 (write path, prune, triggers) + T-I*, T-G1–G3, T-G5–G6. Read path still Postgres. | T-I2 green (the G1 regression). |
| **P2** | Backfill — see §11. | Backfill completes on the dev corpus; V1 bucket count ≈ number of files with `summary_data`. |
| **P3** | Edits 9–12 (read path) + T-R*, T-Q*, T-C*. **T-R1 must be watched RED first.** | V3, V4 recorded. |
| **P4** | Delete `summary_filters.py`, `SummarySearchFilters`, `parse_date_bound`, the Postgres query block. T-B1 goes green. | `rg -n "SummarySearchFilters\|parse_date_bound" backend/` returns only deleted-file history. |
| **P5** *(gated)* | Span cache (§5.5). **Only if V3's masked p50 is still > ~300 ms.** | A written measurement, not a guess. |
| **P6** *(gated)* | Drop `media_file.summary_opensearch_id` (PC4). | **Hard precheck** (below) returns 0. |

**P6's precheck — run against the live DB, and do not proceed on a non-zero result:**
```sql
SELECT count(*) FROM media_file
 WHERE summary_opensearch_id IS NOT NULL AND summary_data IS NULL;
```
Must be `0`. It should be, because `39e96d239` records that `_persist_summary` wrote the same
dict to both — but `crud.py:795` and `summary_status.py:81` read the column into `has_summary`,
so a non-zero result means dropping it would flip `has_summary` to false for real files.

P6 mechanics: an Alembic revision with `op.drop_column("media_file", "summary_opensearch_id")`
and **no data work** (the repo's convention — `v390_add_file_facts.py`, `v392:49-53`,
`v391:34` each state it explicitly); remove the 10 backend sites listed in PC4; remove the
frontend type + 4 UI conditionals + `FileActionButtons.svelte:42` + `MetadataDisplay.svelte:135`;
update `backend/tests/unit/test_summary_retry.py:64,75,140,147`; update
`docs/database-schema.md:80,534,683`; regenerate `backend/openapi.json`.

---

## 11. The backfill (Phase 2) — reuse, do not invent

### 11.1 The free half

**A corpus-wide reindex is already a summary backfill**, because Edit 5 puts
`_index_summary_plane` inside `index_transcript_chunks`, and `POST /search/reindex` has been
corpus-wide across every owner since #627 (`api/endpoints/search.py:678`, →
`model_switch.dispatch_reindex_for_every_owner:67-197`). That path is already idempotent,
resumable, cancellable (`reindex_cancel:{user_id}` naming the run, #691), partitioned across
`REINDEX_PARALLEL_WORKERS` (4), progress-reported over WebSocket, and session-lifetime-correct.

**But it re-embeds every chunk of every file**, which is minutes-to-hours on a real library —
far too much to demand for a derived plane that costs a handful of documents per file.

### 11.2 Edit 14 — a dedicated, cheap backfill

**New file: `backend/app/tasks/summary_plane_backfill.py`**, modelled on
`backend/app/tasks/imohash_recompute.py` (the canonical keyset-cursor, self-rescheduling
walker) with `search_reembed_task.py` as the shape of the operator entry point.

```python
@celery_app.task(name="summary_plane_backfill", bind=True, priority=CPUPriority.MAINTENANCE)
@with_task_lock(SUMMARY_BACKFILL_LOCK_KEY, timeout=300)
def backfill_summary_plane(self, batch_size: int = 100, after_id: int = 0) -> dict[str, Any]:
```

Mechanics, copied from `imohash_recompute.py`:
- `after_id` is a **stable id cursor**; callers leave it at 0 (`:129-130`).
- `_load_batch(after_id, batch_size)` → `.filter(MediaFile.summary_data.isnot(None), MediaFile.id > after_id)`
  `.order_by(MediaFile.id.asc()).limit(batch_size + 1)` — **one extra row to detect `has_more`**
  (`:87-92`). Returns plain dicts and **closes the session** (`:132-138` spells out the
  ACCESS SHARE / blocked-`ALTER TABLE` reasoning).
- Per row: `summary["last_id"] = row["id"]` **before** the work (`:168`), then
  `_index_summary_plane(...)` inside its own try so one bad file cannot abort the batch
  (`:178`).
- **Self-reschedule** with the new cursor (`:195-198`), queue `CeleryQueues.CPU`.
- Terminal: a one-time `SystemSettings` completion flag so a later boot skips it
  (`_finalize_recompute:209-217`). Key: `search.summary_plane_backfilled`.
- **Idempotent by construction** — deterministic ids overwrite, and the prune removes the
  tail. Re-running from `after_id=0` is harmless, which is what makes "resumable" true even if
  the cursor is lost.

**Operator entry**, mirroring the `degraded-embeddings` / `reembed-degraded` pair
(`api/endpoints/search.py:875` + `:918`, which is the repo's stated model for this):
- `GET /api/search/summary-plane-status` — read-only survey: files with `summary_data`, vs.
  distinct `file_uuid` in the summary plane (use a **`composite` agg paginated by `after_key`**,
  not `terms` with `size: 50000` — `search_indexing_task.py:653-696` explains the ceiling).
- `POST /api/search/summary-plane-backfill` — `get_current_admin_user`, pre-checks
  `SUMMARY_BACKFILL_LOCK_KEY` (never dispatch into a held lock, `:929`), then
  `backfill_summary_plane.apply_async(...)`.

**No beat entry.** The backfill is one-time and operator-triggered. `search_index_maintenance`
already has two arms and a third that fires unprompted on every deployment forever is how a
health check becomes an outage (`services/search/CLAUDE.md` says this twice, about the
mixed-index detector and about the text-only tail).

**No Alembic data work.** `v390_add_file_facts.py` creates a derived-artifact table with no
backfill on purpose, and `v392_add_redaction_coverage.py:49-53` states the rule outright. This
change needs no DDL at all until P6.

---

## 12. File-by-file change list

### Backend — shape and indexing
| # | File | Change |
|---|---|---|
| 1 | `backend/app/services/ingest_artifacts/index_mapping.py` | `DOC_TYPE_SUMMARY`, `DOC_TYPES`, `SUMMARY_CHUNK_INDEX_BASE`, `summary_plane_clause`, `summary_document_id`, `summary_chunk_index`, `build_summary_documents`, `summary_document_ids`. `VERBATIM_DOC_TYPES` **unchanged**. |
| 2 | `backend/app/services/search/indexing_service.py` | `ADDITIVE_MAPPING_STEPS` step 3 (`:86-120`); `summary_plane_query` (after `:398`); `_index_summary_plane` (after `:1805`); `_prune_stale_summary_leaves` (after `:1851`); call + `base_metadata` hoist in `index_transcript_chunks` (`:1311-1355`). |
| 3 | `backend/app/tasks/search_indexing_task.py` | New `index_file_summary` task. |
| 4 | `backend/app/core/celery.py` | Route `"index_file_summary"` and `"summary_plane_backfill"` → `CeleryQueues.CPU` (`task_routes`, ~`:325`). |
| 5 | `backend/app/tasks/summary_plane_backfill.py` | **New.** §11.2. |
| 6 | `backend/app/tasks/summarization.py` | Dispatch after `_persist_summary` (`:697`) and after the force-clear (`:395`). `_persist_summary` itself **unchanged**. |
| 7 | `backend/app/tasks/summary_retry.py`, `backend/app/utils/task_utils.py`, `backend/app/api/endpoints/files/reprocess.py` | Dispatch after each summary clear. |
| 8 | `backend/app/api/endpoints/summarization.py` | Dispatch after `delete_summary`'s commit (`:518-...`). |

### Backend — read path
| # | File | Change |
|---|---|---|
| 9 | `backend/app/services/search/hybrid_search_service.py` | `plane_clause` kwarg on `_build_filters` (`:1565`, `:1610`); `_SUMMARY_SEARCH_FIELDS`; `search_summary_plane`. |
| 10 | `backend/app/services/search/summary_search.py` | Rewrite body; promote `_walk_leaves` → `walk_summary_leaves`; delete `_matching_leaf_indices` and the FTS block; add `score` to `SummarySectionMatch`; docstring rewrite incl. the `unmask_for_local` warning. |
| 11 | `backend/app/services/search/summary_filters.py` | **DELETE** (Phase 4). |
| 12 | `backend/app/api/endpoints/search.py` | Remove the filter-construction block + `parse_date_bound` arm (`:282-311`); forward kwargs from `_summary_search_payload` (`:463`, `:540-572`); docstring updates. |
| 13 | `backend/app/schemas/search.py` | `score` on `SummarySectionMatchSchema` (`:113`); `summary_total` semantics documented (`:129`-ish). |

### Docs
| # | File | Change |
|---|---|---|
| 14 | `docs/specs/summary-search-and-display.md` | **Amend, do not rewrite.** Add a dated "2026-… — superseded in part by #963" block after `:44`: the four rejections stand *for the chat retrieval plane*; a search-only plane isolated behind `summary_plane_clause()` answers all four; link to this file. PC3 requires this. |
| 15 | `backend/app/services/search/CLAUDE.md` | Add `summary` to the "Index v6: two planes in one index" table (it becomes three) and to the plane-predicate list; note that `_build_filters` now takes a `plane_clause`. |
| 16 | `backend/app/services/CLAUDE.md:26` | The parenthetical says a summary "lives in `media_file.summary_data` **and nowhere else**". Still true as the *source of truth*; add "…and is indexed as a derived plane in `transcript_chunks` (#963)". |
| 17 | `backend/app/services/ingest_artifacts/CLAUDE.md:35` | `index_mapping.py` now owns three planes' shapes, not just the digest's. Note that `build_summary_documents` builds dicts and calls no LLM, so the package's "nothing here calls an LLM" invariant is intact. |

### Tests
`test_summary_index_mapping.py`, `test_summary_plane_single_source_of_truth.py` (new, unit);
`test_summary_plane_indexing.py`, `test_summary_plane_semantic_retrieval.py` (new, integration);
`test_summary_plane_redaction.py` (new, redaction); edits to
`test_chunk_plane_compat_arm.py:40-46` and the existing summary-search API tests.

---

## 13. Trap register — things that will bite

1. **Omitting Edit 5** (the `index_transcript_chunks` call) is the single worst outcome: the
   plane works until the first reindex, then vanishes permanently and silently. T-I2 exists
   for this.
2. **Bumping `_INDEX_VERSION`** instead of adding an additive step triggers a destructive
   corpus-wide reindex on every deployment, and a half-finished one is undetectable. V2 checks
   `version` is still 6.
3. **Reusing additive step version 2** (retired-but-retained) makes a deployed index's stamp
   disagree with a fresh one, permanently and silently (`indexing_service.py:104-118`).
4. **A `count` gate instead of `mget`** in the prune. Measured: the tail survived 11 of 12
   back-to-back index pairs. `delete_by_query` has the same visibility dependency — it deleted
   **0** of 4 unrefreshed documents.
5. **Restating the leaf walker** instead of importing it. Two walkers = two `key_path` formats
   = the frontend's `summaryKeyPath.ts` silently failing to scroll.
6. **Dropping `file_id` or `file_uuid`** from a summary document. It is a permission leak (G5),
   not a relevance bug, and nothing in the happy path reveals it.
7. **Adding `aggs` to the collapsed hybrid body** for `summary_total`. OpenSearch 3.4 throws
   `ArrayIndexOutOfBoundsException`. Derive the total from collapsed results.
8. **Batching the page into one Presidio call.** 2.2–3.0× faster and it loses PII in 31 of 32
   snippets while the page still looks masked.
9. **Threading `unmask_for_local` into the summary leg.** Category error; there is no egress
   event on a display surface.
10. **Post-filtering quarantine** instead of pre-filtering. A count that includes a taken-down
    file is a content oracle (#818/#876).
11. **Dispatching `index_file_summary` inside `_persist_summary`'s session.** Reads uncommitted
    state and reopens the #67 shape. T-G1.
12. **Deleting `_erase_summary_docs` or the integrity-task arms** because "the index is gone
    now". It is not gone on upgraded deployments; that is a GDPR gap.
13. **Resurrecting `POST /files/search`.** A second search route is what rotted. `/api/search`
    is the one surface.
14. **Editing `TARGET_MAPPING_ADDITIONS`** to add the two new fields. That changes what v6
    meant on every deployment that already has it.
15. **A silently-skipped T-R1.** If the neural pipeline is down, fail loudly. A skip is
    indistinguishable from a pass and is exactly what `scripts/audit-tests.py` exists to catch.
16. **Running the gate while another writer is in the checkout.** pre-commit stashes the whole
    tree; a green run can be a run against the *old* code. Check the test names in the output
    match the ones you just wrote.
17. **`.py` edits under `backend/app/` restart the hot-reloading dev backend**, and startup
    dispatches `search_index_maintenance`. Batch the edits; announce a window before V3.
18. **`reindex_task._load_reindex_page` defers `summary_data`** (`:892`). `_index_summary_plane`
    must load it itself — a `getattr` on the deferred object would lazy-load it inside a
    closed-session context and raise, or worse, silently re-open one.

---

## 14. Open questions for the owner (do not guess)

- **Q1 — `summary_total` window semantics (§6.3).** Exact count → "within the top-500 fused
  window". Acceptable, or is an exact count worth a second round trip that can disagree with
  the page? *Recommendation: accept the window; it matches the transcript leg and preserves
  #831's real invariant.*
- **Q2 — the privacy decision the gist's J8 flagged and nobody has written down.** Matching
  runs against RAW summary text app-wide (transcript plane too), so redaction is a **display**
  control, not a **search** control. This plan extends that to a fourth surface and #760 may
  make it default-on. It is an inherited property, not a decided one. **This needs a written
  decision before #760 flips the default**, not a code change discovered during implementation.
- **Q3 — P6 (dropping `summary_opensearch_id`).** In this PR, or its own? *Recommendation: its
  own, after P4 is merged and stable — it touches 16 sites across both halves of the stack and
  needs a live-DB precheck.*
- **Q4 — should the summary plane be excluded from `POST /search/repair-indices`' force-merge
  path?** It is a small plane; probably not, but confirm nobody assumes "documents in
  `transcript_chunks`" means "transcript chunks" anywhere in that machinery.
