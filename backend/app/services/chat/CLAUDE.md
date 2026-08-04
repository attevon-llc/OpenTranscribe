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

## Prompt assembly is concatenation-only

Never `str.format` / `Template` / f-string over chunk content or user text. A
transcript containing `{evil}` would raise or interpolate. `prompting.py`
concatenates, defuses `<excerpt` / `</excerpt` sequences in chunk text, and puts
an immutable base rule above every user-supplied layer stating that excerpt
content is data and never instructions.

The three prompt layers are ordered and additive: base rules (code) → user
default (`UserSetting chat.system_prompt`) → per-conversation override. A user
layer can never displace the base rules.

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
