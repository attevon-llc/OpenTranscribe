# RAG chat services (issue #52)

The pipeline for one chat turn:

```
scope → rewrite → ROUTE ─┬─ aggregate ──▶ <counted> block
                         ├─ digest map ──▶ <overview> block
                         └─ chunk plane ─▶ excerpts
                              → rerank → diversity-sample → mask → prompt → stream → persist
```

**ROUTE, DON'T FUSE.** The tiers are separate queries whose results the prompt layer places in a
fixed order; they are never merged into one RRF ranking. Fusing documents of very different length
— a 55-word digest section against a 200-word speaker turn — ranks by an artefact of that
difference rather than by relevance. Every route keeps the chunk tier, so a misroute costs a
reduced excerpt budget and never an unanswerable turn.

Each stage is its own module so the two security-critical ones (`redactor.py`,
`prompting.py`) can be read and tested without wading through streaming plumbing.

| File | Responsibility |
|---|---|
| `settings.py` | Admin knobs resolved in ONE `get_settings_map()` call; `.revision` digest keys the retrieval cache so a retune invalidates it |
| `language.py` | **The English-only scope of RAG**, and the warning that makes it visible |
| `context_resolver.py` | Scope (files/collections/tags) → file uuids, **in Postgres**; speaker mentions (no LLM/model, all held to that by `test_laptop_deployment_invariant`): `speaker_resolver.resolve_speaker_mentions`, `speaker_roster.build_candidate_roster` (permission-gated, accessible + non-quarantined files only), `speaker_mention_extract`, `speaker_name_match.match_candidate`, `speaker_name_text` (shared normalization) |
| `retrieval.py` | Cache → retrieve → rerank → diversity sample; returns `RetrievalResult` with diagnostics |
| `router.py` | Rules-only intent + tiers (#403 Stage 4). Loads nothing, calls nothing |
| `aggregation.py` | Counted answers, pure half: shapes, subject extraction, filters |
| `aggregation_service.py` + siblings | Counted answers, I/O half. `aggregation_service` = `answer_aggregation` (dispatch, date scope); `aggregation_queries` = scoped I/O primitives and the **tenant/quarantine chokepoints** `_accessible_scoped_files` (Postgres) / `_quarantined_among` (OpenSearch shapes); `aggregation_speakers` = facet + talk-time stats; `recurrence_service` = recurrence I/O twin of pure `recurrence.py` (masks per file, fails closed) |
| `mapreduce/` | `tree_summarize` over the digest plane; two reducers, one no-LLM. A **package**, not a single module (split from a 1242-line `mapreduce.py` — see its own `__init__.py` docstring for the seam table); every pre-split name is still re-exported from `app.services.chat.mapreduce` |
| `reranker.py` | Lazy CPU cross-encoder singleton; `None` when the model cache is absent |
| `reranker_warmup.py` | Loads that singleton on a daemon thread at API startup (#1269) — see below |
| `reranker_threads.py` | Runs `predict` on a small pool whose threads latch the deployment's torch thread cap (#1270) — see below |
| `query_rewriter.py` | Follow-up → standalone query; every failure returns the original |
| `retrieval_cache.py` | Redis exact-query cache, keyed by user+org+query+scope+settings-rev |
| `redactor.py` + siblings | **Re-masks retrieved chunks before the LLM** (the *egress* control). `redactor` = the masking half (`mask_chunks`, `mask_digests`, `_gather`, `_mask_inline`, `_egress_style`); `redactor_chunk_plans` / `redactor_digest_plans` = phase A, the Postgres gather that returns plain-data plans (the strictest-wins `_effective_cfg_for_owner` lives in the former) |
| `output_redactor.py` | **Re-masks what the MODEL WRITES, sentence by sentence** (the *display* control) |
| `prompting.py` + siblings | Layered system prompt, concatenation only; `prompt_blocks` = delimited excerpts and counted/recurrence/synthesis blocks; `prompt_sanitize` = the injection boundary (`_sanitize_body_text`/`_sanitize_attribute`) |
| `citations.py` | Structured citations; `[n]` markers only SELECT, never construct |
| `service.py` | The SSE generator: keepalive/trace, the answer stream and teardown |
| `context_prep.py` | `_prepare_context`'s phase wiring (rewrite, route, count, retrieve, mask) and the ONE masking call site, `_mask_evidence`; the phases live in the siblings its docstring maps: `turn.py`, `prompt_budget.py`, `planning_steps.py`, `fanout.py`, **`masking.py` (whether excerpts are masked before the LLM)**, `evidence.py` (quarantine drop, fail-closed filter), `summary_tier.py`, `streaming.py`, `persistence.py` |
| `frames.py` | The SSE encoder (`sse`), the failure texts and the frame builders (context warnings, the `sources` frame via `_grounding_frames`, delta/reasoning, error, usage/done) |
| `limits.py` | Per-user hourly + concurrency caps, cancel flags (fail open) |
| `hooks.py` | Cloud seam, mirrors `tasks/transcription/hooks.py` exactly |
| `trace.py` | **The query-trace vocabulary** (GH #514): 16 stages, 6 outcomes, a scrubbed detail allowlist, and a no-op-by-default `emit()` |
| `trace_stream.py` | Bridges trace events from worker threads onto the turn's event loop |

## The reranker: warmed at startup (#1269), scored on a capped thread pool (#1270)

The first chat in a fresh API process used to pay a 9.9-12.3 s cold load inside retrieval; `reranker_warmup.py` loads it on a daemon thread at startup, and `reranker_threads.py` pins torch's thread cap on the pool that runs `predict`. Measurements, failure modes and what not to change: `backend/app/services/chat/notes/reranker.md`.
## The query trace (GH #514)

`trace.py` is the trace vocabulary (16 stages, 6 outcomes, a scrubbed detail allowlist, no-op-by-default `emit()`); `trace_stream.py` bridges worker-thread events onto the turn's event loop. Contract and the scrubbing rules: `backend/app/services/chat/notes/query-trace.md`.
## ⚠️ A turn holds NO database session while it talks to OpenSearch or an LLM

`context_prep._prepare_context` **opens its own sessions and does not accept one.** The turn is
phased, and the phase boundaries are a correctness constraint rather than tidiness:

```
1  rewrite + route                     LLM round trip        NO session
2  counted tier (answer_aggregation)   Postgres ↔ OpenSearch short sessions, opened inside
3  retrieve_context                    OpenSearch/rerank     NO session
4  mask_chunks / mask_digests          Postgres + Presidio   short sessions, opened inside
   scope_digest_hits / build_file_summaries   Postgres       one short session each
5  build_overview + diagnostics        pure                  NO session
   describe_context_languages          Postgres              ONE short session
```

A session held across phases 1–3 is this repo's most repeated defect: a plain `SELECT` holds
`ACCESS SHARE` for the life of its transaction, so the hold queues every `ALTER TABLE` — it
hangs an Alembic upgrade mid-release, and dev runs migrations on backend startup. Measured on
the shape this replaced (real Postgres, 600 ms rewrite + 150 ms aggregation + 400 ms retrieval):
one session held **1,504 ms**, **564 ms** of it `idle in transaction`. After the split: two
short sessions, 260 ms total, longest transaction **23 ms**.

Three rules that keep it fixed:

- **Phase 4 returns PLAIN DATA — never an ORM instance.** `MaskedChunk`, `ChunkHit` and
  `FileSummary` are dataclasses. Returning a `MediaFile` or a `TranscriptSegment` would
  re-open a session on the first attribute read after the block, silently undoing the split;
  that is how a "fixed" instance of this regressed elsewhere.
- **`aggregation_service` takes a `session_factory`, not a `Session`** (`answer_aggregation`,
  `_short_session`). Its date filter and occurrence count each get their own transaction,
  released before the `size: 0` search. Pass `db.session_utils.session_scope`; `None` means
  "no Postgres" and those shapes decline, exactly as the old `db=None` did.
- **`mask_chunks` / `mask_digests` take the factory too (issue #83), because MASKING IS NOT
  ALWAYS FAST.** The cached-span path is sub-millisecond, but a file whose scan cannot be
  trusted falls through to `_mask_inline`, which runs Presidio *here and now* — and a cold
  `AnalyzerEngine` build is ~10 s. Measured through the real masker against real Postgres, one
  chunk, cold process: the session was open **14,262 ms** with **13,898 ms** of it
  `idle in transaction`; after the split, **266 ms** open and **0 ms** idle in transaction
  (0 of 540 samples), with byte-identical masked output. Warm, the same call went 15.2 ms open
  / 7 ms idle → 10.4 ms open / 0 ms idle. So both maskers are two-phase: **gather → close →
  mask**, and the gather returns `_SegmentSpans` (plain data), never a `TranscriptSegment`.
  `redaction/warmup.py` makes the cold build rare, not impossible — its gate is evaluated
  **once** at API startup, so a deployment that enables redaction afterwards still pays it, and
  a request arriving mid-warm-up waits for the remainder. The fallback is also reached *more*
  often since `v392`: a scan that skipped the `pii` detector now takes it too.

Related, and found while splitting phase 4: the language read was handed the phase-4 `db`
**after** its `with` block had closed it. SQLAlchemy answers a query on a closed session by
beginning a new transaction on a newly checked-out connection — measured with
`pg_stat_activity`: `idle in transaction` inside the block, `idle` after it, and
`idle in transaction` again after one query on the closed session — which nothing then returns.
Every turn leaked one such backend until the GC reached the connection. It has its own short
session now.

`tests/unit/test_chat_session_phases.py` drives the real `stream_reply` and asserts zero live
sessions at each slow stage — with a control asserting masking still OPENS one (from the factory
it is handed), because "zero sessions everywhere" is also satisfied by deleting the session and
failing closed on every chunk. The same pairing is in `test_chat_redactor.py`
(`test_the_inline_detector_runs_with_no_db_session_open` + the cached-span control +
"one session per turn, not per chunk") and in `test_chat_digest_masking.py`.
`test_chat_recorded_date_filter.py` pins the aggregation half.

## ⚠️ The chunk index stores transcript text UNREDACTED

So masking is the egress control and it is **conditional on where the model runs**: a local model receives excerpts unmasked, a remote provider gets masked text, keyed off the provider (`redaction.llm_guard.is_local_provider`, fails closed) and never a global setting; an admin `force_redact_before_llm` lock always wins. Output redaction (what the model WRITES) is a separate, display-side layer. ⚠️ **There are TWO maskers on the input side and they are not interchangeable**: `mask_chunks` addresses text by time range, `mask_digests` by provenance (`segment_ids`); a digest sent through the chunk path comes back as the whole recording verbatim. Full rules, history and tests: `backend/app/services/chat/notes/redaction-masking.md`. Read it before changing any masking call site.
## The router, and the two structured blocks it can add (#403 Stage 4)

`router.py` classifies a turn into `lookup` / `summarize` / `aggregate` / `temporal` from a
lexicon plus structural signals. It loads no model and makes no call; the only LLM signal is an
optional `INTENT:` line on a rewrite that was **already being paid for**, so turn 1 still makes
zero calls before retrieval. Two invariants bound the cost of a misroute:

1. **The chunk tier is never removed** — a misrouted query retrieves the same evidence it would
   have, and `[n]` markers still resolve.
2. **Structure only ever REMOVES a non-chunk tier; it never changes the label.** A signal that
   could *promote* would let corpus size alone reroute an ordinary lookup, which is the one
   regression D5 forbids. Measured lookup leakage: **0.104%** (2 of 1,923 labelled queries).

Several blocks can precede the excerpts, in a fixed order, and **all of them come off the top of
the excerpt budget** rather than out of what is left after it:

| Block | Built by | Why it outranks excerpts |
|---|---|---|
| `<counted>` | `aggregation_service.answer_aggregation` | It *is* the answer to "how many"; excerpts are examples beside it |
| `<overview>` | `mapreduce.build_overview` | It covers every recording in scope; excerpts cover a handful |
| `<recurrence>` | `recurrence.detect_recurring_items` | Items that recurred across TWO OR MORE recordings; excerpts show only one at a time |
| `<synthesis>` | `fanout._maybe_run_enrichment` → `prompt_blocks.format_synthesis_block` | A machine-drafted reconciliation of evidence, not itself evidence |

Base rules exist for each, because rule 3 ("answer from the excerpts") fights all of them: rule
**10** says report a `<counted>` number exactly and never recount from the excerpts; rule **12**
says cover every recording an `<overview>` lists rather than narrowing to whichever have
excerpts (rule **14** is the same block's focus-speaker variant); rule **13** says a
`<recurrence>` item is "recurred", never "open"/"done" (this data carries no completion status);
rule **15** says treat a `<synthesis>` block as an unverified draft, not a source. Rule **11** is
unrelated to any block — it is the same-language-as-the-question rule. `prompting.py`'s
`_RULE_*` module constants are the index; don't infer a rule's subject from its number without
checking there.

### The counted tier never lets a model count

"How many meetings discussed X" is not a retrieval question. Ranking twelve chunks and asking a
model to count them yields a confident number that is wrong whenever the answer exceeds the
excerpt budget — on a corpus, usually. So the tier counts with OpenSearch aggregations and
Postgres and hands over a table. Three constraints are not negotiable:

- **No `search_pipeline` and no `hybrid` clause on an aggregation body.** OpenSearch 3.4 throws
  `ArrayIndexOutOfBoundsException` inside `score-ranker-processor` when an aggregation meets
  hybrid + collapse + RRF. This is a crash, not a style rule.
- **A truncated bucket list is refused, never reported.** An aggregation that dropped a shard's
  tail is a wrong answer that looks like a right one.
- **The DATE filter is resolved in Postgres, not on the index** (#403 R7). `_temporal_bounds`
  is the pure half; `_files_in_period` intersects `media_file.recorded_date` into the scope's
  uuid list and the OpenSearch body never sees a date at all. Two reasons, both correctness:
  the index lags a user's correction by a whole reindex — and a user-correctable date is the
  point of `recorded_date_locked`, so an index-side filter would make the correction silently
  not apply — and scope resolves relationally everywhere else in this package for exactly the
  same reason. Past `CHAT_MAX_SCOPE_FILES` it **declines** rather than truncating.
  ⚠️ It filters `recorded_date`, which means it **excludes every file that has none**, so
  `coverage["undated_files_excluded"]` is not optional decoration: on a library the resolver
  has not swept, the count is a floor and the user has to be told. And `coverage["date_sources"]`
  reports WHICH source dated each counted file — "3 meetings in March, dates from filenames"
  is checkable; a bare 3 is not.
  This replaced a `range` on the index field named `upload_time`, which was fed
  `(creation_date or upload_time)` — so the field lied about what it held, and on the eval
  corpus it held one distinct value across all 432 files. `_occurrence_count` never received
  the clause at all while `coverage` reported the filter applied; because base rule 10 tells
  the model to report a counted block **exactly**, that over-claim became a confident wrong
  sentence in the answer rather than a stray dict key.
- **Occurrences are counted over segments, not chunks.** Chunking overlaps a long turn's tail into
  the next chunk, so counting occurrences over chunk documents double-counts every overlap. File
  *coverage* is fine over chunks — a file is counted once either way.

Every shape **declines** rather than guessing: a number from the wrong mechanism is
indistinguishable from a number from the right one, so falling back to ranked excerpts is strictly
better than a confident wrong count.

### The overview: ranking is not mapping

`mapreduce/` (a package, not a single module — see its `__init__.py`) is `tree_summarize` over
the digest plane. Level 1 of the map already ran at
ingest — the extractive digest **is** the per-file map output — so a summary over 1,000 recordings
costs zero map-time work.

⚠️ **For a bounded scope the map reads `file_facts` for every file in it (`scope_digest_hits`) and
ignores relevance.** It does *not* use the ranked digest leg, and that will look like a redundant
second path until you know why: asked for 50 sections over a 25-file scope, `retrieve_digests`
returned 50 sections drawn from **8 files**, because sections cluster by relevance — that is what a
ranker is for. The composed block was headed `recordings: 8` and the model answered *"8 vendor
review board sessions"* over a scope of 25. **Ranking picks the best passages; mapping covers every
document.** Raising `size` does not fix it: ranking gives no coverage guarantee at any K.

The ranked leg survives only for the unbounded "all accessible" scope, where mapping over
everything is impossible — and there the header reports what it covered (`8 of 25 in scope`)
rather than presenting the covered count as the total.
## ⚠️ RAG and chat are ENGLISH-ONLY. Transcription is not.

Multilingual is the goal and this tracks the distance to it. Language support is DERIVED from the embedding model that actually produces the vectors (`supported_rag_languages(db)`); `SUPPORTED_RAG_LANGUAGES` is only the fallback, so **do not widen the constant by hand**. The per-stage state table, the warning's shape and what was measured (#453): `backend/app/services/chat/notes/english-only.md`.
## Scope resolves in Postgres, never OpenSearch

`context_resolver.py` expands collections and tags relationally, then passes an
explicit uuid list to retrieval. The index carries denormalized
`collection_ids` / `tags` / `accessible_user_ids`, but those can lag a share
change or a quarantine flag by a reindex. Resolving relationally means an
unshared or quarantined file cannot reach a prompt through a stale document.

⚠️ **That still leaves the exact-query retrieval cache** (`retrieval_cache.py`)
as a second surface a revoked share can leak through: its key is scope-aware
but deliberately ACL-blind (see that module's docstring), so a chat query
cached while a file was shared would otherwise keep serving it after the share
was revoked, for up to the cache TTL. `tasks.search_indexing_task
.update_file_access_index` — the one place that rewrites
`accessible_user_ids` for a collection-share grant/revocation or group-
membership change — now calls `bump_corpus_version()` unconditionally after
its `update_by_query` loop completes, which the cache key already folds in.
An endpoint-side synchronous bump was considered and rejected: the endpoints
dispatch that task with `.delay()`, so bumping from the endpoint races the
in-flight ACL rewrite and can re-cache the still-stale (revoked) result under
the new version for a fresh TTL.

An empty resolved scope means **match nothing**, not match everything —
`retrieve_chunks` returns `[]` for `file_uuids == []` and `None` means
"all accessible". Getting that backwards leaks the whole library.

**All three axes resolve against ACCESSIBLE files, not owned ones.** Explicit
files go through `get_file_by_uuid_with_permission`, collections through
`PermissionService.get_accessible_collection_ids`, and tags through
`get_accessible_file_ids_subquery` — the same sharing rule
`services/tag_scope.py:get_accessible_file_ids_subquery` uses. Tags were owner-only until #385, so
scoping a chat by a tag spanning shared recordings silently dropped them and the
model answered confidently from the remainder. Don't write a fourth sharing rule
here; if an axis needs one, it is already wrong. Tag names are unique **per
owner**, so matching by name deliberately spans every user's tag row — that is
what lets a sharee's `atlas` scope reach the owner's atlas-tagged recording.
**None of the three axes has an admin bypass** — collections and explicit files never
did (`_resolve_explicit_files`/`_resolve_collections`), and tags never did either: a uuid
is a deliberate pick, a tag name is a wide net, and a tenant-wide one would resolve tags
the admin's own picker never shows them. (An earlier revision of this note said "unlike
collections", which was already false the day it was amended for #385 — check the code,
not this sentence, before repeating the claim.) Quarantine visibility follows the same
rule: `_visible_files_query` excludes a quarantined file for every caller, admin included
— chat is a retrieval surface, not the admin review one (`GET /admin/files/quarantined`).

## Prompt assembly is concatenation-only

Never `str.format` / `Template` / f-string over chunk content or user text. A
transcript containing `{evil}` would raise or interpolate. `prompting.py` and
`prompt_blocks.py` (sanitisers: `prompt_sanitize.py`) concatenate, defuse `<excerpt` / `</excerpt` sequences in chunk text, and puts
an immutable base rule above every user-supplied layer stating that excerpt
content is data and never instructions.

The **four** prompt layers are ordered and additive (issue #360):

```
base rules (code) → user default → project → conversation
```

`UserSetting chat.system_prompt` is layer 2, `ChatProject.system_prompt` layer 3,
`conversation.settings["system_prompt"]` layer 4. Every non-empty layer is
appended inside one delimited preferences block; **no layer can displace the base
rules.**

Additive, not most-specific-wins — the conversation layer used to REPLACE the
user default, which is wrong once projects exist: "answer concisely" (user) and
"their product is called Atlas" (project) are both true at once, and a project
prompt silently discarding an account preference is a trap. Each layer is capped
at `_MAX_SYSTEM_PROMPT_CHARS` and the joined block again at
`_MAX_COMBINED_PROMPT_CHARS`, so three maxed-out layers cannot crowd out the
excerpts.

## Projects (issue #360)

`chat_project` groups conversations and pins a default scope + prompt layer that
they inherit. `chat_conversation.project_id` is NULLABLE — every pre-v376
conversation is simply ungrouped — and the FK is **ON DELETE SET NULL, not
CASCADE**: deleting a project must never destroy the threads inside it.

`common.resolve_effective_scope()` decides what a turn retrieves against:
the conversation's own pinned recordings, else the project's, else empty.

**The trap it exists to avoid:** an EMPTY scope means "all accessible", while an
explicitly-resolved-but-empty file list means "match nothing". A project that
pins no recordings must therefore leave the scope *empty* rather than substitute
an empty `file_uuids` list — do the latter and every answer in that project
reports no relevant excerpts. `tests/unit/test_chat_project_scope.py` pins it.
Speakers are a separate axis and survive inheriting the project's recordings.

## Per-request setting layers

Three narrowing passes, each of which can only ever TIGHTEN:

```
admin (get_chat_settings) → tenant (apply_tenant_limits) → user (apply_user_preferences)
```

`apply_user_preferences` runs LAST so a user preference narrows an
already-narrowed value and cannot widen it back out. Reranking is one-way: a
user may turn it off, but `True` cannot enable it when the admin has it off,
because the cross-encoder may not be installed on that deployment.

`resolve_answer_tokens` (in `prompt_budget.py`) does the same for the reply budget and
is resolved **before** `build_messages`, which reserves context for the answer —
raising `max_tokens` afterwards would let prompt + answer overrun the window.
`GET /api/chat/answer-budget` exposes the same function's result (default and ceiling) for
the chat settings panel, so the UI never shows a budget the turn does not use.
`llm.user_context_window` is already the measured-narrowed value by the time chat sees it
(issue #833, `app/services/CLAUDE.md`), so nothing on the chat path should re-consult the
measurement separately.

## The excerpt budget is a hard ceiling, and citations follow it

`format_excerpts` returns the excerpt IDS it emitted, and `frames._grounding_frames` builds the `sources` frame from them **after** the prompt is assembled (assemble first, then emit `sources`). A citation snippet is a PREFIX SAMPLE, not a quote-aware window (#832), and the chunk-plane cap was raised by #913 (the "REMOTE provider" framing that gated it was wrong). The budget rules, the `context_dropped` / `no_context` / `retrieval_failed` branches and the cap derivations: `backend/app/services/chat/notes/excerpt-budget-and-citations.md`.
## Concurrency slots leak if you release them in the wrong place

`limits.acquire_stream_slot` returns a **slot id** (or `None` when refused), and
slots live in a Redis sorted set pruned by age. It used to be a bare counter
whose TTL was refreshed on every acquire, so a slot leaked by a died-mid-stream
request never aged out for an active user: their concurrency degraded 2 → 1 → 0
with no recovery. An upgraded deployment still holds the old string key, and
every sorted-set command against it raises WRONGTYPE — swallowed by the
fail-open handler, silently disabling the cap — so `_drop_legacy_counter`
retires it on first contact.

**Release from `stream_reply`'s shielded `finally`, via the `on_teardown` hook —
not from the wrapping generator.** Starlette tears the wrapper down on client
disconnect (Stop, a closed tab) and its `finally` does not reliably run, so a
release placed there leaks on every Stop. The hook needs its own `finally` too:
finalisation can raise, and a release after it would never run.

## Streaming and threads

Everything except the SSE frames is synchronous (OpenSearch, SQLAlchemy,
`requests`), so `service.py` runs blocking stages via `run_in_threadpool` and
bridges the provider's sync generator with `iterate_in_threadpool`.

The generator opens **its own** DB session via `session_scope()` rather than
borrowing the request's: it outlives the handler's dependency scope, and a stream
still writing when that session closes would fail exactly when it needs to
persist the partial answer.

## Where things fire

- `fire_before_message` — in the **endpoint**, before `StreamingResponse` is
  constructed, so a quota rejection is a clean HTTP 402 rather than a mid-stream
  error frame.
- `fire_message_complete` — in `persistence._finalize_turn` after usage is persisted; idempotency
  scope is `message_uuid`.
- Audit (`CHAT_MESSAGE_SEND`) — metadata only. **Never message content**, and
  logger lines carry ids and lengths only.

## Testing

```bash
# From backend/, no stack required (LLM/OpenSearch/Redis are mocked):
pytest tests/unit/test_llm_streaming.py tests/unit/test_chat_*.py -v
```

`tests/unit/test_v374_migration_consistency.py` and the endpoint tests need the
live stack (`./opentr.sh start dev`).
