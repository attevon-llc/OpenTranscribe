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
| `language.py` | **The English-only scope of RAG**, and the warning that makes it visible |
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

## ⚠️ RAG and chat are ENGLISH-ONLY. Transcription is not.

**Do not "fix" this by widening a constant.** WhisperX transcribes 100+ languages
and that must keep working; what is English-only is the *question-answering* path
on top of it. Four independent stages, none of which is a setting:

| Stage | Why it is English |
|---|---|
| BM25 | the `transcript_chunks` analyzer is `english_stop` + `english_snowball` (`services/search/indexing_service.py:55-70`) — a Spanish query stems as if it were English |
| Embeddings | default `all-MiniLM-L6-v2`, declared `"languages": ["en"]` in `core/constants.py:OPENSEARCH_EMBEDDING_MODELS` |
| Reranking | `CHAT_RERANKER_MODEL = cross-encoder/ms-marco-MiniLM-L-6-v2`, an English MS MARCO model |
| Prompting | `BASE_SYSTEM_RULES` and the query-rewriter prompt are written in English |

Chunking used to be listed here as "the exception — chunk boundaries are fine."
**That was wrong, and only true for Latin and Cyrillic scripts.**
`_PUNKT_LANG_MAP` covers 18 European languages, and everything else was handed to
the ENGLISH punkt model, which returned a whole Chinese transcript as one
sentence — while `str.split()` reported it as one *word*, so no size check fired
either. A 10,500-character Chinese recording became **one chunk**. Fixed in issue
#448; `tests/unit/test_chunking_scripts.py` covers zh/ja/ko/ar/hi/th with Latin
controls. Chunk boundaries are now correct for every script; what remains
English-only is everything that RANKS or READS them.

The failure this produced was **silent**: a non-English recording is not retrieved
for an English question, so the model answered confidently from whatever English
material remained and nothing said a recording had been effectively invisible.

**It warns, it does not refuse — deliberately.** A transcript library is normally
mixed, so refusing every question because one recording is Spanish would be worse
than useless. The turn answers from what it can and emits a `warning` frame
`{"code": "unsupported_language", "languages": [...], "files": N, "unknown_files": N,
"supported": ["en"]}` plus `msg_metadata.unsupported_language` — **the exact
mechanism `context_dropped` already uses**, so there is one render path and the
notice survives a reload. Do not invent a second one.

**Three buckets, because unknown is its own answer.** `MediaFile.language` is
nullable. An undetected language is *not* counted as English (that would hide a
real Spanish recording) and *not* as non-English (that would fire on every library
recorded before language detection existed). It is reported as
`context_languages.unknown_files` on every turn and warns on nothing by itself.

**What is judged, and what deliberately is not.** Languages come from the union of
the *resolved scope* and the files the retrieved excerpts came from. The scope half
is the important one: a scoped Spanish file that retrieval never surfaces would
otherwise never warn, because failing to retrieve it is the whole defect. But
`file_uuids is None` ("all accessible") is **never enumerated** — one foreign
recording anywhere in a library would put the warning on nearly every turn, and a
warning that is always on is one nobody reads.

Language is read from **Postgres, not the chunk document** — the index carries a
`language` keyword field, but for the same reason scope resolution avoids it (see
below). The read reapplies no permission filter because both inputs are already
authorized (`context_resolver` for the scope, `accessible_user_ids` for the hits).
The lookup fails **open**: a diagnostic must never break a chat turn, and a warning
invented from a failed read would be worse than none.

**Not admin-tunable, on purpose.** An operator can select a multilingual embedding
model, but that repairs one of the four stages; a `SystemSettings` row letting them
declare "Spanish is supported" would be dishonest about the other three.
`SUPPORTED_RAG_LANGUAGES` widens in code, alongside the pipeline that earns it.

Known gap: the *question's* language is not detected — asking in Spanish about an
English transcript is not flagged. That needs a language detector and is part of
the multilingual-RAG backlog, not this notice.

`tests/test_chat_language_scope.py` pins all of it, and every "it fires" test is
paired with an all-English **control** asserting silence.

**The client renders it** (`ChatMessage.svelte`, testid `chat-unsupported-language`),
via `stores/chat.ts` folding the frame into `msg_metadata` exactly as
`context_dropped` does. Three things had to move together and a fourth is a trap:

- `ChatWarningCode` in `lib/types/chat.ts` — a code missing from that union is
  **silently discarded**, so the server can emit a warning nobody ever sees.
- `stores/chat.ts`'s `warning` case, now a code→patch map rather than an
  `if` chain, so an unhandled code cannot fall through to nothing.
- `chatStream.ts`'s `known` list keys on the **event name** (`warning`), not the
  code, so it needed no change — but a genuinely new frame type would.
- The notice **names the languages** through `formatLanguageNames`
  (`Intl.DisplayNames`, reader's locale, raw code as fallback). A language
  silently dropped there would understate the warning — the user would be told
  fewer of their recordings were unsupported than actually were.

`chat.message.unsupportedLanguage` exists in all 8 locales (`npm run check:i18n`
enforces exact parity). Guards: `ChatMessage.unsupportedLanguage.test.ts`,
`chat.reducer.test.ts`, and `formatting.test.ts` — the naming assertion lives in
the last of those because vitest loads no locale bundle, so `$t` returns the raw
key and any assertion on the rendered sentence in a component test is vacuous.

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
