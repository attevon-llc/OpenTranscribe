# RAG/chat lane — task state

**Branch:** `feat/rag-corpus-scale-403` · **HEAD when written:** `06a96f88`
**Written:** 2026-08-14. Snapshot of the in-session task list, committed so it survives a tool
disconnection. The task list itself is authoritative while it is reachable; this is the backup.

**Count at time of writing: 95 tracked, 93 completed, 1 running, and the open queue below.**

---

## Completed in this lane (each with the commit that closed it)

| # | What | Commit |
|---|---|---|
| 80 | #437 mixed vector index — 3 paths, 2 bypassing the switch endpoint | `2c798767` |
| 76 | Segment edit cached a detector outage as a clean scan | `8dbb8965` |
| 67 | `transcript_summaries` OpenSearch index retired | `39e96d23` |
| 81 | Subtitle export withholds (409) while redaction pending | `0eecd839` |
| 78 | v392 `redaction_coverage` — closed on `llm_guard` | `094907d6` |
| 79 | Toxicity reports its outage but maps to no categories | `094907d6` |
| 75 | Chat holds no DB session across OpenSearch/LLM | `e486f948` |
| 78b | Same coverage gate wired into chat's maskers | `73def041` |
| 50 | Baseline model attribution read from DOCUMENTS not settings | `8e52d887` |
| 18 | `chore/test-suite-perf-and-quality-overhaul` merged | `1f8e275f` |
| 82 | Dual Alembic head reconciled (v390–v392) | `1f8e275f` |
| 84 | Session-lifetime auditor sees factory-opened sessions | `b7ae0f89` |
| 74 | Presidio warmed off the request path + the missing build lock | `ce366efe` |
| 87 | Runner reporting modes; `EMPTY` ≠ `PASS` | `16cb686a`, `cd9148ce` |
| 85 | Bulk export + burned-in subtitles honour redaction policy | `693a16c1` |
| 86 | Search snippets mask PII | `f02b3640` |
| 88 | Derived cache key ignored `file_id` (cross-user) | `d484483e` |
| 83 | `mask_chunks` gathers → closes → masks | `2bcfb9ee` |
| 91 | Re-baselined with provenance; reproducibility proven | `cd9148ce` |
| 14 | Stage 5 bake-off — 24 arms, zero adoptable | `4babb9c7`, `68a56456` |
| 64 | Reasoning as a measured per-model capability | `06a96f88` |

**Delegated out of this lane** (tracked in the doc-ingestion worktree, NOT done here):
`#59` Stage 6b parse task/migration/tiers · `#69` docling-slim HTML table loss — already landed
there as `e5861700`.

## Running

- **#42 — QMSum ↔ synthetic agreement.** Do the two corpora agree about what counts as an
  improvement? Stage 5 measured them actively disagreeing (+1.4% vs −49.1% on one arm), so this
  decides whether single-corpus tuning is evidence at all.

## 🚦 Coming back to this after a break? Start at issue #461

**[#461](https://github.com/attevon-llc/OpenTranscribe/issues/461) opens with a "START HERE"
box carrying the phased execution order.** That is the entry point — it says what to do first
and why, and links everything else. Do not start from this file; it is an index.

The four tracking issues:

| | |
|---|---|
| **#461** | RAG retrieval quality — measured findings, dependency map, datasets, eval packages. **Has the execution order.** |
| **#463** | Answer quality — QMSum's human-written answers are on disk and unused. Gates the reranker decision. |
| **#464** | Tiered map output — use LLM summaries for the overview when an LLM is configured. |
| **#462** | Summary search — a separate product feature, parallel to all retrieval work. |

**The one-line version:** per-query nDCG first (cheap, gates credibility) → measure answer
quality (#463) → then the reranker A/B, which is the highest product impact and is **blocked**
until an answer-quality measure exists.

## Open queue, in dependency order

1. **#51 — generation quality is unmeasured.** No faithfulness or citation-correctness scoring
   exists. The largest remaining build, and the highest value: it is what would settle #92.
2. **#92 — ⚠️ the shipped cross-encoder COSTS 20.6% (QMSum) / 32.7% (synthetic) nDCG@10**, where
   #383 predicted a 0.3–3.1% gain, and worsens with pool size. **Do not remove it on this
   evidence** — nDCG cannot see whether the ANSWER improved. Needs #51 first.
3. **#16 — Stage 7, opt-in enrichment.** Its own gate is an nDCG comparison against the Stage 3
   control, so it consumes the measurement work rather than preceding it. Carries a net-new
   cost-estimate UI (G10c). Its `#362` tail belongs to the doc-ingestion lane.
4. **#17 — Stage 8 whitepaper.** Writes up the results of everything above. Must be last.
5. **#89 — summary keyword search** on the search page. Fully specified in
   `docs/specs/summary-search-and-display.md`. Postgres FTS, section-level hits, shared filter
   components, deliberately NOT in the chat RRF plane. Independent of the corpus.
6. **#90 — summaries are displayed completely unmasked.** Pre-existing, owner-deferred. Cheap
   now that `snippet_redaction.py` exists and Presidio is warm. **Close before #89 reaches
   users.** Detect per section, never batched.

## Carried findings that are not tasks but must not be lost

- **The reranker measurement is void before `68a56456`** — the harness defaulted to 20/3 while
  production ships 12/4, so every earlier `--stage rerank` number described a deployment nobody
  runs.
- **All committed baselines report `embedding_verdict: unattributed`** — all 210,908 documents
  carry the legacy `"neural"` bucket, so no baseline can name its embedding model. That is the
  auditable truth, not a gap to paper over.
- **The corpus figure that proves nothing disturbed it:** `indexing.total` **825,795**,
  docs 210,908, deleted 0, `_meta.version` 6.
- **`addopts` carries `-m 'not integration and not gpu'`** — integration tests are silently
  deselected unless run with `-m integration`.

## Where the real records live

Commit messages are the primary record; this file is an index. Beyond them:

- `docs-site/docs/developer-guide/rag-evaluation.md` — 1,537 lines. Stage 5 method, per-arm
  command lines, arms-as-data, corpus state, what was held constant, how to add an arm, and every
  negative result with its margin.
- `docs/specs/summary-search-and-display.md` — the #89/#90 design.
- `docs/handoff/document-ingestion-vertical.md` + `document-parsing-state-of-the-code.md` — the
  doc-ingestion lane.
- `backend/app/db/CLAUDE.md` — "Renumbering note 2", the dual-head account.
- `backend/alembic/CLAUDE.md` — the three-lane migration reservation.
