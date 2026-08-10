# RAG chat services (issue #52)

The pipeline for one chat turn:

```
scope → retrieve → rerank → diversity-sample → mask → prompt → stream → persist
```

Each stage is its own module so the two security-critical ones (`redactor.py`,
`prompting.py`) can be read and tested without wading through streaming plumbing.

| File | Responsibility |
|---|---|
| `settings.py` | Admin knobs resolved in ONE `get_settings_map()` call; `.revision` digest keys the retrieval cache so a retune invalidates it |
| `context_resolver.py` | Scope (files/collections/tags) → file uuids, **in Postgres** |
| `retrieval.py` | Cache → retrieve → rerank → diversity sample; returns `RetrievalResult` with diagnostics |
| `reranker.py` | Lazy CPU cross-encoder singleton; `None` when the model cache is absent |
| `query_rewriter.py` | Follow-up → standalone query; every failure returns the original |
| `retrieval_cache.py` | Redis exact-query cache, keyed by user+org+query+scope+settings-rev |
| `redactor.py` | **Re-masks retrieved chunks before the LLM** |
| `prompting.py` | Layered system prompt + delimited excerpts, concatenation only |
| `citations.py` | Structured citations; `[n]` markers only SELECT, never construct |
| `service.py` | SSE orchestration, persistence, audit, hooks |
| `limits.py` | Per-user hourly + concurrency caps, cancel flags (fail open) |
| `hooks.py` | Cloud seam, mirrors `tasks/transcription/hooks.py` exactly |

## ⚠️ The chunk index stores transcript text UNREDACTED

That is correct for search — you should find your own words in your own
recordings — but it means `retrieve_chunks()` hands back raw text. **Every path
that sends chunk content to an LLM must go through `redactor.mask_chunks()`
first.** The gate is identical to summarization's (`tasks/summarization.py`):
apply when `cfg.enabled and cfg.redact_before_llm`, with the admin force floor
already folded in by `resolve_effective_config`.

Masking **fails closed**. If the policy cannot be resolved, or a chunk cannot be
masked, the chunk's content becomes `""` and contributes nothing — never the raw
text. Tests in `tests/unit/test_chat_redactor.py` pin this; do not "fix" them by
falling back to the original content.

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
