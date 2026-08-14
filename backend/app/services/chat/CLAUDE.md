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
| `context_resolver.py` | Scope (files/collections/tags) → file uuids, **in Postgres** |
| `retrieval.py` | Cache → retrieve → rerank → diversity sample; returns `RetrievalResult` with diagnostics |
| `router.py` | Rules-only intent + tiers (#403 Stage 4). Loads nothing, calls nothing |
| `aggregation.py` | Counted answers, pure half: shapes, subject extraction, filters |
| `aggregation_service.py` | Counted answers, I/O half: OpenSearch aggs + Postgres |
| `mapreduce.py` | `tree_summarize` over the digest plane; two reducers, one no-LLM |
| `reranker.py` | Lazy CPU cross-encoder singleton; `None` when the model cache is absent |
| `query_rewriter.py` | Follow-up → standalone query; every failure returns the original |
| `retrieval_cache.py` | Redis exact-query cache, keyed by user+org+query+scope+settings-rev |
| `redactor.py` | **Re-masks retrieved chunks before the LLM** (the *egress* control) |
| `output_redactor.py` | **Re-masks what the MODEL WRITES, sentence by sentence** (the *display* control) |
| `prompting.py` | Layered system prompt + delimited excerpts, concatenation only |
| `citations.py` | Structured citations; `[n]` markers only SELECT, never construct |
| `service.py` | SSE orchestration, persistence, audit, hooks |
| `limits.py` | Per-user hourly + concurrency caps, cancel flags (fail open) |
| `hooks.py` | Cloud seam, mirrors `tasks/transcription/hooks.py` exactly |

## ⚠️ A turn holds NO database session while it talks to OpenSearch or an LLM

`service._prepare_context` **opens its own sessions and does not accept one.** The turn is
phased, and the phase boundaries are a correctness constraint rather than tidiness:

```
1  rewrite + route                     LLM round trip        NO session
2  counted tier (answer_aggregation)   Postgres ↔ OpenSearch short sessions, opened inside
3  retrieve_context                    OpenSearch/rerank     NO session
4  mask_chunks / mask_digests /        Postgres              ONE short session
   scope_digest_hits / build_file_summaries
5  build_overview + diagnostics        pure                  NO session
```

A session held across phases 1–3 is this repo's most repeated defect: a plain `SELECT` holds
`ACCESS SHARE` for the life of its transaction, so the hold queues every `ALTER TABLE` — it
hangs an Alembic upgrade mid-release, and dev runs migrations on backend startup. Measured on
the shape this replaced (real Postgres, 600 ms rewrite + 150 ms aggregation + 400 ms retrieval):
one session held **1,504 ms**, **564 ms** of it `idle in transaction`. After the split: two
short sessions, 260 ms total, longest transaction **23 ms**.

Two rules that keep it fixed:

- **Phase 4 returns PLAIN DATA — never an ORM instance.** `MaskedChunk`, `ChunkHit` and
  `FileSummary` are dataclasses. Returning a `MediaFile` or a `TranscriptSegment` would
  re-open a session on the first attribute read after the block, silently undoing the split;
  that is how a "fixed" instance of this regressed elsewhere.
- **`aggregation_service` takes a `session_factory`, not a `Session`** (`answer_aggregation`,
  `_short_session`). Its date filter and occurrence count each get their own transaction,
  released before the `size: 0` search. Pass `db.session_utils.session_scope`; `None` means
  "no Postgres" and those shapes decline, exactly as the old `db=None` did.

`tests/unit/test_chat_session_phases.py` drives the real `stream_reply` and asserts zero live
sessions at each slow stage — with a control asserting masking still gets one, because "zero
sessions everywhere" is also satisfied by deleting the session and failing closed on every
chunk. `test_chat_recorded_date_filter.py` pins the aggregation half.

## ⚠️ The chunk index stores transcript text UNREDACTED

That is correct for search — you should find your own words in your own
recordings — but it means `retrieve_chunks()` hands back raw text. **Every path
that sends chunk content to an LLM must go through `redactor.mask_chunks()`
first.** The gate *condition* is identical to summarization's
(`tasks/summarization.py`): apply when `cfg.enabled and cfg.redact_before_llm`,
with the admin force floor already folded in by `resolve_effective_config`.

The **subject** is not. Summarization resolves the *file owner's* config
(`redaction/llm_guard.py` reads `media_file.user_id`); chat resolves the
*requesting user's*, because one turn retrieves across a library of shared
recordings with no single owner. Anything layering summaries onto chat retrieval
(#383) must pick a subject deliberately rather than inherit whichever the
surrounding code used.

Masking **fails closed**. If the policy cannot be resolved, or a chunk cannot be
masked, the chunk's content becomes `""` and contributes nothing — never the raw
text. Tests in `tests/unit/test_chat_redactor.py` pin this; do not "fix" them by
falling back to the original content.

### ⚠️ Masking is to be conditional on WHERE the model runs — DECIDED, NOT YET BUILT

Owner decision, 2026-08-13. A **local** model — vLLM on our own GPU — is to receive excerpt text
unmasked: the text never leaves the machine, so masking costs recall and buys nothing. A **remote
or cloud provider** still receives masked text, because sending unredacted PII to a third party is
a data-egress event in a way a local inference call is not. Key that decision off the
**provider**, never off a global setting.

⚠️ **None of that is in the code yet.** `mask_chunks` / `mask_digests` still gate on
`cfg.enabled and cfg.redact_before_llm` with no provider check anywhere, so **input masking
applies to every provider today** and a local deployment is *not* currently less protected than
before the decision. Say so accurately: this was mis-stated once already.

**Order of operations is a safety constraint, not a preference.** Output redaction (below) had to
land *first*, and it has. Land the provider keying the other way round and the gap is real,
between two commits, on a deployment that believes it is protected.

When the keying does land, tighten the admin string with it. The floor
(`redaction.force_redact_before_llm`) already describes itself as **external**-only in all three
places a human reads it — the audit description ("Mandate masked text to external LLM providers")
and both i18n strings — so exempting a local model is the control finally matching its label, not
an override of it. But an admin on a local-only deployment may have set it believing it covered
their own vLLM, so the label should say that outright.

### Output redaction: masking what the model WRITES (`output_redactor.py`)

Offset-based redaction masks known spans in *stored* text. It cannot catch a model that reads
"John Smith, SSN 123-45-6789" and writes *"the number he gave was 123-45-6789"* in its own words:
there is no span to mask, at offsets that exist in no stored record, so **every cached-span masker
in this codebase renders it clean**. Measured on HEAD before the fix, through the real
`stream_reply`: the client received `'The number he gave was 123-45-6789. That is all.'`

**`redactor.py` is the egress control; `output_redactor.py` is the display control.** They are not
alternatives and neither substitutes for the other.

- **Sentence-buffered.** Deltas accumulate; only text up to the last completed sentence boundary is
  emitted, and the detector runs over that span first. The alternatives were rejected: masking at
  persist-and-reload is cheapest and wrong (the user already read it), and refusing to stream is
  honest and a large UX regression. A newline counts as a boundary (markdown lists carry no
  periods), honorifics and initials do not (splitting `Mr. Smith` hands the detector `Smith`
  alone), and an over-long boundary-free buffer force-flushes while holding a tail so an entity
  straddling the cut is not halved.
- **The gate is `cfg.enabled and cfg.enabled_categories` — NOT `redact_before_llm`.** Same
  `resolve_effective_config` call, same subject, same admin floor; a different field, deliberately.
  `redact_before_llm` is an *egress* control. A user with redaction on and `redact_before_llm` off
  has a masked transcript view and would still get an unmasked SSN rendered into the chat answer —
  precisely the hole this closes. The narrower gate is the plausible-looking wrong choice and has
  been proposed once already.
- **Fail closed, per sentence.** `detect_segment_spans` *swallows* a PII-detector failure and
  returns the spans it did get, so "found nothing" and "could not look" are the same value — the
  `failures` sink (issue #324) is the only thing that separates them. A failure of a detector
  feeding an **enabled category** replaces the sentence with `REDACTION_LLM_FAILSAFE_TEXT`; a
  failure outside the user's categories does not, or every CPU-only deployment without Presidio
  would lose its answers to a category it never asked for. An unresolvable *policy* activates
  masking with every category on, rather than passing text through.
- **The persisted answer is the masked one.** Storing the raw generation would create an unmasked
  PII store in `chat_message.content` that no read path masks, and reload would then show more than
  the stream did.
- **Reasoning gets its own buffer.** The thinking block is collapsed, not hidden — it is a display
  surface.
- **The watchdog measures the PROVIDER's first token, not the first emission.** Keying it off
  emission would read a buffered first sentence as a stalled model and kill it as a timeout.
- **Redaction off is a pure pass-through** — no buffering, no detector, byte-identical stream.

Measured cost (gemma-4-e4b on the local vLLM, 400 deltas / 1,677 chars / 23.9 tok/s, real
Presidio): **+549 ms to first visible token**, 12.7 ms of detector time per sentence, +318 ms
over the whole 16.7 s answer. The one-off cost that actually hurts is the **~10 s Presidio cold
load** on the first masked answer in a fresh API process — worth preloading, and not yet done.

Pinned by `tests/unit/test_chat_output_redaction.py`, whose `models`-marked test is the only one
that proves the *detector* — not the plumbing — catches a paraphrase.

### ⚠️ There are TWO maskers on the INPUT side and they are not interchangeable

(Three in the package overall — `OutputRedactor` above is the third, and it addresses text that
has no stored form at all. These two are the ones that can be confused for each other.)

| Function | For | Addresses text by | Fails closed |
|---|---|---|---|
| `mask_chunks()` | transcript chunks | **time range** — every segment overlapping the hit | per chunk, whole |
| `mask_digests()` | `doc_type: digest` documents | **provenance** — each sentence's own `segment_ids` | per **sentence** |
| `OutputRedactor` | model-generated text | **detection at emit time** — no stored spans exist | per **sentence** |

A digest sent through `mask_chunks()` **over-discloses**. A chunk *is* contiguous turns, so
rebuilding it from its time range reproduces it. A digest is a handful of non-contiguous *selected*
sentences spanning an entire recording, so the same rebuild returns the **whole recording
verbatim** — strictly more text than the digest held, from a function whose name asserts it masked
it. Nothing about a call to `mask_*` invites a second look in review, and under the current
redaction policy this is the path that egresses to a third-party provider.

`tests/unit/test_chat_digest_masking.py::test_the_chunk_path_over_discloses_a_digest` is a
**must-fire** guard: it asserts the wrong path still produces *more* text than the digest. If
someone folds `mask_digests` into `mask_chunks`, that test goes red while its twin goes green.

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

Two blocks can precede the excerpts, in this fixed order, and **both come off the top of the
excerpt budget** rather than out of what is left after it:

| Block | Built by | Why it outranks excerpts |
|---|---|---|
| `<counted>` | `aggregation_service.answer_aggregation` | It *is* the answer to "how many"; excerpts are examples beside it |
| `<overview>` | `mapreduce.build_overview` | It covers every recording in scope; excerpts cover a handful |

Base rules **10** and **11** exist because rule 3 ("answer from the excerpts") fights both of them:
rule 10 says report a `<counted>` number exactly and never recount from the excerpts; rule 11 says
cover every recording an `<overview>` lists rather than narrowing to whichever have excerpts.

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

`mapreduce.py` is `tree_summarize` over the digest plane. Level 1 of the map already ran at
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

## Scope resolves in Postgres, never OpenSearch

`context_resolver.py` expands collections and tags relationally, then passes an
explicit uuid list to retrieval. The index carries denormalized
`collection_ids` / `tags` / `accessible_user_ids`, but those can lag a share
change or a quarantine flag by a reindex. Resolving relationally means an
unshared or quarantined file cannot reach a prompt through a stale document.

An empty resolved scope means **match nothing**, not match everything —
`retrieve_chunks` returns `[]` for `file_uuids == []` and `None` means
"all accessible". Getting that backwards leaks the whole library.

**All three axes resolve against ACCESSIBLE files, not owned ones.** Explicit
files go through `get_file_by_uuid_with_permission`, collections through
`PermissionService.get_accessible_collection_ids`, and tags through
`get_accessible_file_ids_subquery` — the same sharing rule
`endpoints/tags.py:_visible_to` uses. Tags were owner-only until #385, so
scoping a chat by a tag spanning shared recordings silently dropped them and the
model answered confidently from the remainder. Don't write a fourth sharing rule
here; if an axis needs one, it is already wrong. Tag names are unique **per
owner**, so matching by name deliberately spans every user's tag row — that is
what lets a sharee's `atlas` scope reach the owner's atlas-tagged recording.
Unlike collections, tags have **no admin bypass**: a uuid is a deliberate pick, a
tag name is a wide net, and a tenant-wide one would resolve tags the admin's own
picker never shows them.

## Prompt assembly is concatenation-only

Never `str.format` / `Template` / f-string over chunk content or user text. A
transcript containing `{evil}` would raise or interpolate. `prompting.py`
concatenates, defuses `<excerpt` / `</excerpt` sequences in chunk text, and puts
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

`resolve_answer_tokens` (in `service.py`) does the same for the reply budget and
is resolved **before** `build_messages`, which reserves context for the answer —
raising `max_tokens` afterwards would let prompt + answer overrun the window.

## The excerpt budget is a hard ceiling, and citations follow it

Three rules that only hold together (issues #384, #386, #387):

1. **`format_excerpts` never exceeds `budget_chars`.** The loop used to be unable
   to break on iteration one, so the first excerpt was emitted whole however
   large — the overrun then surfaced provider-side (a 400, or silent truncation)
   rather than as a local guard, and `LLMConfig.max_tokens` is user-declared and
   never discovered, so nothing downstream catches it. A first excerpt that does
   not fit is trimmed at a sentence boundary and tagged `truncated="true"` (base
   rule 8 tells the model what that means); below
   `_MIN_TRUNCATED_EXCERPT_CHARS` it is skipped instead of shown as a fragment.
2. **`format_excerpts` returns the excerpt IDS it emitted, not a count**, and
   `service.py` builds the `sources` frame from those ids. The frame used to go
   out immediately after retrieval — before the budget existed — so the UI could
   render clickable citations for excerpts the model never saw: an answer that
   reads as sourced but is grounded in nothing. **Assemble the prompt first, then
   emit `sources`.** `tests/unit/test_chat_sources_frame.py` drives the real
   generator and pins `len(offered_citations) == chunks_used`.
3. **When retrieval found chunks and the budget fit none of them**, the turn sets
   `msg_metadata.context_dropped` and emits a `warning` frame
   (`{"code": "context_dropped", "retrieved": N}`). Answering anyway behind a
   normal-looking reply is the failure this exists to prevent. Adding a frame
   means adding it to `chatStream.ts`'s `known` list too, or the client drops it
   as an unknown future event.
4. **When NOTHING reached the prompt**, the turn sets `msg_metadata.no_context`
   and emits `{"code": "no_context", "retrieved": N, "files_searched": F}`
   (issue #438). The two codes are mutually exclusive branches of one `if`:
   `context_dropped` means excerpts existed and the budget rejected them,
   `no_context` means none survived to be budgeted.

   It exists because **`retrieve_chunks` degrades to `[]` on ANY failure** — the
   run that opened #438 was an OpenSearch `503 search_phase_execution_exception`
   raised while the v6 reindex was rebuilding `transcript_chunks`, swallowed by
   that fail-soft handler. The model then answered "I do not have enough
   information in the provided excerpts" over a 432-file corpus full of matching
   material, and nothing distinguished that from a grounded negative. `retrieved`
   separates the two remaining causes: `0` is an empty (or failed) search,
   non-zero is masking failing closed on every chunk.

   ⚠️ It cannot yet say *which*: the exception is caught inside
   `services/search/chunk_retrieval.retrieve_chunks`, which returns `[]` with no
   signal. Threading a `retrieval_failed` flag out of there would let the frame
   distinguish "your library has nothing about this" from "search was down" —
   worth doing, and it is a one-line change in a module this package does not own.

   A warning **code** is as much a contract as a frame name: it needs a
   `ChatWarningCode` entry in `frontend/src/lib/types/chat.ts`, a branch in the
   store's fold, and a rendering, or the server reports a problem nobody sees.

`history_max_turns` counts **turn pairs** on both sides — `_history_for_prompt`
fetches `max_turns * 2` rows and `build_messages` slices
`history[-(max_turns * 2):]`. They disagreed until #386, which halved the
advertised conversation depth and threw away half of every fetch.

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
- `fire_message_complete` — in `service.py` after usage is persisted; idempotency
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
