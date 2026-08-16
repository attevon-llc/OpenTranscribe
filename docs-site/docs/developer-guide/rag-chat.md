---
sidebar_position: 6
---

# RAG Chat (Internals)

This page documents the architecture of the AI Chat / RAG feature
([issue #52](https://github.com/attevon-llc/OpenTranscribe/issues/52)): the retrieval
pipeline, the two security-critical stages, what the audit log records, and how to develop
against it locally without a GPU or an LLM API key.

For the user-facing explanation, see [AI Chat (RAG)](../features/rag-chat.md).

## Pipeline overview

One chat turn runs through `backend/app/services/chat/`:

```
scope → retrieve → rerank → diversity-sample → mask → prompt → stream → persist
```

Each stage is its own module so the two security-critical ones (`redactor.py`,
`prompting.py`) can be read and tested independently of the streaming plumbing:

| File | Responsibility |
|---|---|
| `settings.py` | Admin knobs resolved in one `get_settings_map()` call |
| `context_resolver.py` | Scope (files/collections/tags) → file uuids, resolved in Postgres |
| `retrieval.py` | Cache → retrieve → rerank → diversity sample |
| `reranker.py` | Lazy CPU cross-encoder singleton |
| `query_rewriter.py` | Follow-up → standalone query |
| `retrieval_cache.py` | Redis exact-query cache |
| `redactor.py` | Re-masks retrieved chunks before the LLM |
| `prompting.py` | Layered system prompt + delimited excerpts, concatenation only |
| `citations.py` | Structured citations |
| `service.py` | SSE orchestration, persistence, audit, hooks |
| `limits.py` | Per-user hourly + concurrency caps |
| `hooks.py` | Cloud seam |

## Prompt-injection hardening

The transcript-chunk index that powers retrieval stores text **unredacted** — correct for
search, since you should be able to find your own words in your own recordings — which means
a retrieved chunk can, in principle, contain anything a speaker said, including something
that reads like an instruction ("ignore previous instructions and..."). `prompting.py` is
built specifically so that text can never steer the assistant:

- **Concatenation only.** Prompt assembly never uses `str.format`, `Template`, or an f-string
  over chunk content or user text — a transcript containing `{evil}` would raise or get
  interpolated instead of rendering as literal text. Excerpts are appended as plain strings.
- **Delimited excerpts with defused markers.** Excerpt text is wrapped in `<excerpt>` tags,
  and any `<excerpt` / `</excerpt` sequence *inside* the transcript text itself is defused so
  a recording cannot forge its own closing tag and inject content that looks like it
  originated outside the excerpt block.
- **An immutable base rule sits above every user-supplied layer**, stating explicitly that
  excerpt content is data, never instructions. The four prompt layers are ordered and
  additive — `base rules (code) → user default → project → conversation` — and **no layer
  can displace the base rules**, including the project and per-conversation layers a user or
  admin controls. Each layer is capped (`_MAX_SYSTEM_PROMPT_CHARS`) and the combined block
  again (`_MAX_COMBINED_PROMPT_CHARS`), so stacked layers cannot crowd out the excerpts
  themselves.
- **Redact-before-LLM still applies to chat.** Because chat can't wait mid-request for the
  shared redaction queue the way summarization does, it masks inline via
  `redactor.mask_chunks()` rather than gating on a cached `redaction_status`. Masking **fails
  closed**: if a chunk cannot be masked, its content becomes `""` — never the raw text.
  `tests/unit/test_chat_redactor.py` pins this; don't "fix" a failing test here by falling
  back to the original content.

A related trap lives in `context_resolver.py`: scope resolves **relationally in Postgres**,
never from the (denormalized, occasionally stale) OpenSearch document, so an unshared or
quarantined file can't reach a prompt through a stale index entry. An empty resolved scope
means **match nothing**; `None` means "all accessible." Inverting that check leaks the whole
library to a query that should have matched nothing — `retrieve_chunks` returns `[]` for
`file_uuids == []`. Note which is which: a conversation created with **no scope** is
`is_empty`, resolves to `None`, and searches everything the caller can access. "Match
nothing" is only ever the *result* of resolving a selection the caller may not read.

## An ungrounded answer must not look grounded

The stream emits a `warning` frame rather than letting the model's "I don't have enough
information" pass for a grounded negative. Two codes, mutually exclusive:

| Code | Meaning | `msg_metadata` |
|---|---|---|
| `context_dropped` | Excerpts were retrieved; the prompt budget fit none of them (#384) | `context_dropped: true` |
| `no_context` | Nothing reached the prompt at all (#438) | `no_context: true` |

```
event: warning
data: {"code": "no_context", "retrieved": 0, "files_searched": "all"}
```

`no_context` exists because **retrieval fails soft**: `retrieve_chunks` returns `[]` for a
missing OpenSearch client, a query exception, or a genuinely empty result, so a transient
backend failure and an empty library are the same value. The run that motivated it was a
`503 search_phase_execution_exception` raised while the chunk index was being rebuilt — the
answer that came back read exactly like a confident, grounded "I don't know". `retrieved`
narrows what happened: `0` is an empty or failed search, non-zero means masking failed closed
on every chunk (a redaction-configuration problem, not an empty index).

Warning **codes** are part of the frozen frame contract. A new one needs an entry in
`frontend/src/lib/types/chat.ts`'s `ChatWarningCode`, a branch in the store's fold, and a
rendering — otherwise the server reports a problem the user never sees.

## Audit logging: metadata only

Every chat turn fires a `CHAT_MESSAGE_SEND` audit event from `service.py`, after usage is
persisted. The rule is strict and has no exceptions: the audit event and every logger line
around it carry **ids and lengths only** — conversation id, message id, excerpt count, token
counts. **Message content and transcript excerpts are never written to the audit log or
application logs.** If you add a new log line or audit call anywhere in this pipeline, treat
"would this leak transcript text or a user's question into a log store" as the review
question, not just "does this help debugging."

`fire_before_message` runs in the **endpoint**, before `StreamingResponse` is constructed, so
a quota rejection surfaces as a clean HTTP 402 rather than a mid-stream error frame.
`fire_message_complete` runs in `service.py`; its idempotency scope is `message_uuid`.

## Local development without a GPU or API key

`./opentr.sh start dev --with-mock-llm` starts an OpenAI-compatible mock server
(`scripts/mock-llm-server.py`) on the app's internal Docker network at
`http://mock-llm:5199/v1` — no GPU, API key, or internet access required. Point a user or
system LLM config at it like any other OpenAI-compatible provider.

Only token generation is canned. Everything else in the pipeline above — retrieval, redaction
masking, citations, SSE streaming, and usage recording — takes its real code path, so this is
a legitimate way to exercise the chat feature end-to-end, not just a UI stub.

Scenario models select canned behavior so you can drive the app's real error handling:

| Model | Behavior |
|---|---|
| `mock-gpt` | Normal streamed response |
| `mock-echo` | Echoes back the prompt it was given — use this to assert what the app actually *sent* (e.g. that redaction masked a chunk, or that a project's instructions made it into the prompt) |
| `mock-empty` | Empty response |
| `mock-error` | Simulated provider error |
| `mock-slow` | Slow streaming, for testing cancel / stop-generating behavior |

:::warning Never start it as a bare host process
The mock server binds port 5199. Running it outside the container blocks that process instead
of serving requests. Always start it via `--with-mock-llm` so it runs on the app's Docker
network.
:::

Fixtures and the full scenario/model table live in `backend/tests/CLAUDE.md`.

## Concurrency slot handling

`limits.acquire_stream_slot` returns a slot id (or `None` when refused) into a Redis sorted
set. Release happens in `stream_reply`'s shielded `finally`, via the `on_teardown` hook —
**not** from the wrapping generator. Starlette tears the wrapper down on client disconnect
(the Stop button, a closed tab) and its `finally` does not reliably run there, so a release
placed in the wrong `finally` leaks a slot on every Stop.

## Testing

```bash
# From backend/, no live stack required (LLM/OpenSearch/Redis are mocked):
pytest tests/unit/test_llm_streaming.py tests/unit/test_chat_*.py -v
```

`tests/unit/test_v374_migration_consistency.py` and the chat endpoint tests need the live
stack (`./opentr.sh start dev`, optionally with `--with-mock-llm` so LLM calls resolve
without a real provider).
