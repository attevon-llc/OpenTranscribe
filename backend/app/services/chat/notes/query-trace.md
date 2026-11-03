# chat — the query trace (GH #514)

Moved intact from `backend/app/services/chat/CLAUDE.md`. Read before touching `trace.py` or `trace_stream.py`.

## The query trace (GH #514)

A turn reports what it actually did, stage by stage, over the SSE stream. It
exists because this package keeps producing answers that *look* grounded but ran
on less evidence than the reader assumes.

**Four rules, in the order they matter:**

1. **It reports; it never participates.** Nothing here reads the trace back or
   branches on it, and a trace failure can never fail a turn — every public
   function in `trace.py` swallows its own errors.
2. **Free when off.** No recorder attached → `emit()` returns immediately. No
   frame, no queue, no allocation. `chat.trace_enabled` is a hard branch in
   `stream_reply`, not a fast path through shared code.
3. **It cannot leak.** `SAFE_DETAIL_KEYS` allows only NON-identifying values —
   counts, plane names, durations, configured limits. The trace never *holds* a
   file title or speaker name, so no rendering mistake downstream can expose
   one. ⚠️ Two call sites are one careless splat away from a leak:
   `PARSED_NAMES` (where `resolution.matched` and `as_meta()["ambiguous"]` are
   real roster names — pass `len(...)` only) and `REWRITTEN` (where the obvious
   thing to show is the rewritten query, which is the **user's own text** — pass
   `ms` only).
4. **`empty` is not `skipped`, and `failed` is not `empty`.** `retrieve_chunks`
   degrades to `[]` on ANY failure, so an OpenSearch outage and a genuine
   no-match are byte-identical downstream — the `diagnostics` out-param is the
   only thing separating them, which is exactly #438. Verified live by stopping
   OpenSearch on an isolated stack: `found main FAILED reason=search_failed`,
   with the two stages after it correctly `SKIPPED reason=no_candidates`.

⚠️ **`reason` is a CLOSED vocabulary of machine codes**, never free text.
`coverage["declined"]` holds English prose ("talk-time stats need a bounded set
of recordings"); it is mapped to a code, never forwarded.

**Emit from the chat layer, not from shared search code.** `retrieve_context`
already sees the cache branches, the rerank and `diversity_sample`, so the
recorder never has to reach into `chunk_retrieval.py` — which `/search` and the
eval harness also use.

**One node per plane that actually ran.** Retrieval issues a single chunk-plane
query rather than several legs, so there is no separate count to report and a
sibling node would misdescribe what ran.

**A cache hit marks search/rerank/sample `SKIPPED`, not absent** — an omitted
node reads as "not part of this pipeline". `_emit_search_skipped` vs
`_emit_narrowing_skipped` keeps that honest: a search that RAN and found nothing
keeps its `EMPTY` and is never relabelled.

**Live-only.** Nothing is persisted; measured, a typical trace is 1.5–2.5 KB
against a whole message row of ~1.5 KB, so storing it would roughly double every
conversation-load payload.

⚠️ **`trace_stream.drain_available` yields to the loop before reading the
queue.** `call_soon_threadsafe` SCHEDULES the enqueue rather than running it, so
draining without yielding inspects a queue the callbacks have not reached. The
live drain hides this (`asyncio.wait` yields anyway); the post-retrieval flush
does not, and without the yield `BUDGETED` and `PRESENTED` were emitted, queued,
and never delivered — silently.
