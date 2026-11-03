# chat — the excerpt budget, citations, and snippet caps

Moved intact from `backend/app/services/chat/CLAUDE.md`. Read before touching `prompt_budget.py`, `citations.py`, `prompt_blocks.py` or the chunk/digest snippet caps.

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
   `frames._grounding_frames` builds the `sources` frame from those ids. The frame used to go
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
   (search genuinely found nothing, or masking failed closed on every chunk) or
   `msg_metadata.retrieval_failed` (the search itself broke) and emits the
   matching `{"code": ..., "retrieved": N, "files_searched": F}` (issue #438).
   All **three** — `context_dropped`, `no_context`, `retrieval_failed` — are
   mutually exclusive branches of one `if`/`elif` in `frames._grounding_frames`:
   `context_dropped` means excerpts existed and the budget rejected them;
   between the other two, `retrieval_failed` means the chunk-plane search itself
   raised or had no client, `no_context` means it ran and genuinely found
   nothing (or masking dropped everything it found).

   It exists because **`retrieve_chunks` degrades to `[]` on ANY failure** — the
   run that opened #438 was an OpenSearch `503 search_phase_execution_exception`
   raised while the v6 reindex was rebuilding `transcript_chunks`, swallowed by
   that fail-soft handler. The model then answered "I do not have enough
   information in the provided excerpts" over a 432-file corpus full of matching
   material, and nothing distinguished that from a grounded negative. `retrieved`
   still separates the *other* two causes inside `no_context` (`0` is an empty
   search, non-zero is masking having failed closed on every chunk).

   **Now says which** (closed the gap this note used to flag): `retrieve_chunks`
   takes an optional `diagnostics: dict | None` out-param and sets
   `diagnostics["retrieval_failed"] = True` on the no-client and exception
   branches only — never on a legitimately empty result, so its *absence* is the
   "ordinary empty search" signal. `chat/retrieval.retrieve_context` threads it
   onto `RetrievalResult.retrieval_failed`, and `_prepare_context` folds it into
   `msg_metadata` only when true (most turns never carry the key at all).

   A warning **code** is as much a contract as a frame name: it needs a
   `ChatWarningCode` entry in `frontend/src/lib/types/chat.ts`, a branch in the
   store's fold, and a rendering, or the server reports a problem nobody sees.

`history_max_turns` counts **turn pairs** on both sides — `_history_for_prompt`
fetches `max_turns * 2` rows and `build_messages` slices
`history[-(max_turns * 2):]`. They disagreed until #386, which halved the
advertised conversation depth and threw away half of every fetch.

## A citation snippet is a PREFIX SAMPLE, not a quote-aware window (issue #832)

`citations.py`'s `snippet` field is `clean[:limit]`, cut on a word boundary, plus an
ellipsis if it cut anything. It is **positional**, not chosen for what the model
actually quoted — `_snippet` has no idea which part of the excerpt a `[n]` marker's
quote came from, and never will: the module's own docstring states citations are
built from structured data, never parsed out of model prose beyond the `[n]`
marker itself, so "re-window around the quote" is out of scope by that same rule
(see "explicitly out of scope" below).

That mattered more than it looked like it should. A measured probe run found
`quote_fidelity` (does a citation's displayed snippet actually contain the quote
the model claims to cite) at **0.527 pooled** — and the issue that reported it
guessed the cause was "the snippet is too short and gets cut mid-quote". That
guess was **wrong**: 0 of 69 failing quotes were cut by the boundary; the longest
quote in the whole corpus was 138 chars. The real defect was the fixed-prefix
window having no relationship to where the quote lived in a LONGER excerpt, and
it hit one plane far harder than the other:

| Kind | Cap | Why |
|---|---|---|
| ordinary chunk | `SNIPPET_CHARS` = 10 × `SEARCH_CHUNK_TARGET_WORDS` (2000 today) | Left at a flat 240 by #832 — **raised by #913** (see below); derived from the same indexing target `chunking_service.py` builds a chunk against |
| digest | `DIGEST_SNIPPET_CHARS` = 10 × `ingest_artifacts.sizing.DIGEST_SECTION_MAX_WORDS` (700 today) | A digest section is bounded at ingest to that many words, always > 240 chars — so the plain 240 cap truncated **100% (113/113)** of digest citations in the measured probe, vs 31% of chunk citations |
| overview (`build_overview_citations`, #532 arm (a)) | `OVERVIEW_SNIPPET_CHARS` = 3 × `DIGEST_SNIPPET_CHARS` | A `FileSummary.digest` there is several digest sections JOINED, up to `mapreduce.overview.sections_budget()`'s ceiling of 3 per file |

⚠️ **`SNIPPET_CHARS` (2000) is now WIDER than `DIGEST_SNIPPET_CHARS` (700)** — the ordering
`test_an_ordinary_chunk_citations_snippet_is_unchanged` used to assert (chunk narrower than
digest) is inverted by design after #913, and that test was rewritten, not deleted, to assert
the rule that actually holds instead: each kind is capped against its OWN derived constant,
never a literal and never another kind's cap. Full ordering today:
`DIGEST_SNIPPET_CHARS` (700) < `SNIPPET_CHARS` (2000) < `OVERVIEW_SNIPPET_CHARS` (2100) <
`EXPANDED_SNIPPET_CHARS` (2500).

**`build_citation` and `build_overview_citations` are TWO separate call sites and
must be kept in sync** — each computes its own `snippet_limit` and its own
`content_chars`. There is no shared helper that derives both from one place; if a
future kind is added, give it its own cap here rather than reusing the wrong one.

**`content_chars`** (on both citation payloads, and on `schemas.chat.Citation`) is
the whitespace-normalized length of the excerpt BEFORE truncation — a plain
integer count, never text. It exists so a future measurement of "what cap
actually covers what this deployment produces" is a real number instead of a
guess, and so `tests/eval/harness/traceability.py` can report a truncation rate
without ever touching source text. Like `kind`/`digest_section` before it (see
`schemas/chat.py`'s own docstring), a citation field that is not declared on
`Citation` is **silently stripped** by Pydantic on every reload — declare it
there or it works mid-stream and vanishes after a refresh.

**Which surfaces mask a citation snippet, and which do not — pre-existing,
provider-keyed policy, unchanged by this fix.** Export masks it
(`export_policy.py`). The live SSE `sources` frame, the persisted
`chat_message.citations` JSONB, and an in-app conversation reload do **not** — a
citation's `snippet` already went through `redactor.mask_chunks`/`mask_digests`
at retrieval time (masked-if-the-policy-required-it, unmasked for a genuinely
local provider — see "Masking is conditional on WHERE the model runs" above), so
what a citation card shows is exactly what the model was given, never a second,
independently-decided masking pass.

**Explicitly out of scope for #832, and why:**

- ~~Raising `SNIPPET_CHARS` for the chunk plane.~~ **Done — issue #913, see below.**
- **Quote-aware re-windowing** (choosing the snippet based on what the model
  actually quoted). Would contradict the module's own invariant that a citation
  is never built by parsing model prose beyond the `[n]` marker.
- **`EXPANDED_SNIPPET_CHARS`/the `expanded` context-expansion path.** Unrelated
  to this fix and untouched — it is chunk-only by construction
  (`needs_expansion` excludes digests). `chat.rag.context_expansion_enabled` is
  default-ON as of issue #523's closure (2026-09-21), so this path is reachable
  on shipped defaults — see `constants.py`'s `DEFAULT_CHAT_CONTEXT_EXPANSION_ENABLED`.

## The chunk-plane cap was raised, and the "REMOTE provider" framing that gated it was wrong (issue #913)

#832's follow-up issue framed raising `SNIPPET_CHARS` as blocked on a redaction-policy question:
"does a wider excerpt shown to a REMOTE provider's reader change the egress calculus?" That
framing is **false on two counts**, and #913 corrected it rather than answering it:

1. **The snippet never reaches any provider.** `chat/prompting.py` never reads
   `citation["snippet"]` — its only consumers are the SSE `sources` frame, the persisted
   `citations` JSONB column, `chat/export.py`, `chat/export_redaction.py`, and
   `citation_takedown.py`. There is no "what a REMOTE provider's reader gets" to change.
2. **On a remote provider the snippet is masked anyway.** `mask_chunks`/`mask_digests` mask it
   before it is ever attached to the citation, so a wider cap there shows more MASKED text —
   zero new PII reaches that reader.

The real precedent — and the one that should have been cited the first time — is #832's own
`DIGEST_SNIPPET_CHARS` (700, a 2.9× widening over the flat 240) and `OVERVIEW_SNIPPET_CHARS`
(2100, 8.75×), both of which ship **default-on** through the identical `_snippet(chunk.content,
…)` call. Raising `SNIPPET_CHARS` to `10 × SEARCH_CHUNK_TARGET_WORDS` (2000 today) is that same
method's **third** ratified application, not a new decision — see `citations.py`'s module-level
comment for the full derivation and the widened-UI-change rationale.

The genuine exposure this widens is narrower than the issue's framing: on a **local** provider
with the admin masking floor off, a citation snippet is raw transcript text (unchanged rule,
`models/chat.py`'s `ChatMessage` docstring and `citation_takedown.py`'s header) — widening the
cap increases the volume of that per-citation exposure, at the same order of magnitude #832
already shipped on the digest/overview planes.

**The UI half of the #832 blocker was real** and is also closed by #913: `ChatSources.svelte`'s
citation card now has a per-card "Show more"/"Show less" toggle (collapsed clamp stays at 2
lines below 240 chars — the exact size the card showed in full before this issue, so no
existing citation gains a toggle that reveals nothing), so a wider snippet is actually
reachable by a reader rather than silently clipped by CSS.

No RAG-quality measurement was run for this change. `quote_fidelity` needs real model prose
(the mock LLM would make the score meaningless), `--question-set` is supplied at runtime with
no set committed, and the only committed traceability artifact predates #832 and carries no
`content_chars`. The derivation above (cover the same indexing target the other two caps
already cover) is the justification on record; a future evidence-based re-tune should filter
offered citations to `kind == "chunk"`, use the `content_chars` distribution, and compare
against `quote_fidelity_at_240` per the decision rule in `citations.py`'s docstring.
