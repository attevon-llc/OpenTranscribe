---
sidebar_position: 8
---

# RAG Evaluation Methodology

How OpenTranscribe's retrieval and chat quality is measured, what the numbers mean, and how to
reproduce them.

:::info Living document
This page is updated at every stage of the corpus-scale RAG work
([issue #403](https://github.com/attevon-llc/OpenTranscribe/issues/403)), not written at the end.
Sections marked **planned** are not yet implemented. The published research paper is written *from*
this page rather than duplicating it.
:::

[`rag-chat.md`](./rag-chat.md) documents how retrieval *works*. This page documents how we know
whether it works **well** — and, just as importantly, the ways a retrieval benchmark can quietly
lie to you.

## Why this page exists

A retrieval number is easy to produce and easy to get wrong. Every measurement below is reported
with the corpus it ran against, the metric implementation that computed it, and the control it is
compared to. Where a result is not defensible, it is labelled as such rather than rounded up.

Three principles govern everything here:

1. **A metric with no named implementation is not a result.** Different libraries disagree on nDCG
   for identical input — see [Metric implementation](#metric-implementation-matters).
2. **Every retrieval-affecting change reports its delta against the previous stage as control**, per
   query class and per model tier. A win in one tier and a loss in another is not a win, and the
   lookup class must never regress.
3. **Negative results are recorded**, including configurations we tried and rejected.

## What gets measured

Four query classes, because they fail differently:

| Class | Example | What it stresses |
|---|---|---|
| **lookup** | "what did Dana say about pricing?" | precision on a single passage |
| **multi-file** | "what did we decide about pricing across all my recordings?" | evidence assembly across N files |
| **summarize** | "summarise the Q3 planning meetings" | coverage — did every relevant file contribute? |
| **aggregation** | "how many meetings mentioned the vendor contract?" | exactness; answered via search aggregations or SQL, never by an LLM counting |

## Evaluation corpora

Three tiers. Which tier a number comes from determines what may be claimed about it.

### Licence tiering

OpenTranscribe is an open-source research product, so non-commercially-licensed corpora are usable
for **development, tuning and internal validation**. The restriction is on **publication**.

| Tier | Licences | Use |
|---|---|---|
| **A — publishable** | MIT, Apache-2.0, CC-BY, CC0, public domain | may appear in published results |
| **B — internal only** | NC, CC-BY-NC(-SA), research-use-only, restrictive EULA | full internal use; never a published number |
| **C — unobtainable** | paywalled, requires a signed agreement | recorded and skipped |

The tier travels with the data all the way into the results files, so publishable and internal-only
tables are separated mechanically rather than by memory at writing time.

:::warning Platform metadata is not a licence
Repository and dataset-hub metadata has misrepresented the real terms **four times** in this
project:

- OpenSLR's AMI mirror serves an older release under **CC BY-NC-SA**, while the Edinburgh original
  v1.6.2 is **CC BY 4.0**.
- MeetingBank's Zenodo metadata field says `cc-by-4.0`; the `LICENSE.txt` shipped *inside* the
  archive — and the authors' own site — say **CC BY-NC-ND 4.0**.
- Every `BeIR/*` dataset repo is tagged `cc-by-sa-4.0`, including `BeIR/msmarco`, whose underlying
  MS MARCO terms are **non-commercial research only**.
- OmniDocBench's dataset-hub `license` field is **empty** — so no automated check flags anything —
  while its prose "Copyright Statement" says **research purposes only, not for commercial use**.
  An absent metadata field is not evidence of a permissive licence.

Always trace the licence to the original corpus's own terms. Each of these would have put an
unpublishable number in a paper.
:::

### Tier 1 — committed fixtures

Small, fast, deterministic; run on every change.

### Tier 2 — public benchmark corpora

Acquired reproducibly by `scripts/fetch-rag-eval-data.sh`, which records source URL, licence, tier
and SHA256 per artefact, and supports offline `--verify`. Non-commercial corpora require an explicit
`--accept-noncommercial` flag.

| Corpus | Tier | Relevance judgements | Role |
|---|---|---|---|
| QMSum | A (MIT) | **1,576 human queries with gold spans** | the backbone of published retrieval numbers |
| AMI v1.6.2 | A (CC BY 4.0) | none | real word-level timings, speaker channels |
| ICSI | A (CC BY 4.0) | none | real timings for QMSum's Academic split |
| Earnings-21 | A (CC BY-SA 4.0) | none | domain realism: real names, sectors, RTTMs |
| LoCoV1 | A (Apache-2.0) | yes | long-context retrieval |
| MIRACL | A (Apache-2.0) | **pooled human, with negatives** | multilingual, 18 languages |
| CIRAL | A (Apache-2.0) | pooled human | 4 African languages (CLIR) |
| Mr. TyDi | A (Apache-2.0) | human, positives-only | multilingual, 11 languages |
| MeetingBank | **B** (CC BY-NC-ND) | none | 31.7 M words — internal scale testing only |
| ELITR | **B** (CC BY-NC-SA) | manual span alignments | internal |

Corpora with **no relevance judgements cannot score retrieval.** They contribute ingest realism —
real timings, real speaker structure — and nothing is claimed from them beyond that.

### Tier 3 — synthetic

Generator: `backend/tests/eval/synthetic/`. Deterministic from a seed, no LLM anywhere, gold sets
known by construction and re-derived from the written text by a ten-check validator before the
corpus is usable. Its gold spans use **QMSum's inclusive turn-range convention on purpose**, so one
adapter serves both corpora.


Two of the four query classes — **multi-file** and **aggregation** — have no public corpus with
ground truth, and the only corpus large enough to test thousands-of-files scale is non-commercial.
Synthetic data fills exactly those gaps.

For these classes synthetic ground truth is arguably *stronger* than human annotation: "this
question requires files 3, 9 and 14 and only those" is true by construction, whereas a human
annotator over a real corpus is sampling — with the false-negative problem measured below. The risk
with synthetic data is **realism, not correctness**, so the generator reports the BM25 R@1 and
near-duplicate rate of its own output.

## Corpus composition is a result, not a detail

The single most consequential finding so far is that **how a corpus is assembled affects measured
retrieval quality more than how large it is.**

QMSum's 232 meetings span three domains. The 137 "Product" meetings are all the *same fictional
remote-control design scenario*, with the same four role names. Measuring BM25 over all 1,576
queries with an identical qrels convention, varying only the domain:

| Domain | R@1 | Median rank of gold | Other meetings scoring ≥90% of gold |
|---|---|---|---|
| Product (AMI, one scenario) | **0.124** | 22 | **49.2** of 136 |
| Academic (ICSI) | 0.234 | 4 | 22.1 |
| Committee | **0.664** | 1 | 2.2 |

A query like *"what did the Project Manager say about the buttons?"* legitimately matches dozens of
Product meetings, but only one is marked relevant. The retriever is penalised for being right. This
is **qrels false-negativity**, not a retrieval defect.

Deduplicating to one session per AMI series — Academic + Committee + 130 documents total — lifts
R@1 from **0.289 to 0.422**.

The counterintuitive part: a 5.4× larger index costs 6 points of R@1, while adding back 107
near-duplicate AMI sessions costs 13 points *while shrinking the index*. **Near-duplicate structure
costs more than scale.** Any benchmark pooling all 232 meetings is measuring its own corpus
construction.

## Metric implementation matters

We use [`pytrec_eval_terrier`](https://github.com/terrierteam/pytrec_eval) 0.5.10 — the maintained
fork, which publishes wheels — wrapping NIST's `trec_eval` C implementation. This is not a
convenience choice; different libraries produce materially different numbers for identical input:

| Case | pytrec_eval | ranx | sklearn |
|---|---|---|---|
| graded 3/2/1, nDCG@10 | 0.922495 | 0.922495 | 0.922495 |
| tie, gold sorts first | **0.630930** | **1.000000** | 0.5 |
| tie, gold sorts last | **1.000000** | **0.630930** | 0.5 |

**0.369 absolute nDCG@10 divergence on the same input.** `trec_eval` breaks ties by document id
descending; `ranx` breaks them by dictionary insertion order, which is not reproducible.

Three further behaviours the harness must handle explicitly:

- `ndcg_cut_10` truncates the ideal DCG at *k*; bare `ndcg` does not (1.0 vs 0.645 on the same run).
- Unjudged documents score zero **but still consume a rank slot**.
- **A query present in the qrels that the run did not answer is silently omitted from the results.**
  So `mean(results.values())` *flatters* exactly the regressions worth catching — a run that returns
  nothing scores nothing rather than zero.

:::danger Tie-breaking can manufacture a result
Chunk documents are identified `{file_uuid}_{chunk_index}`; summary digests are `{file_uuid}_digest`.
In ASCII `d` sorts above every digit, and `trec_eval` breaks ties by document id **descending** — so
at *identical relevance scores* a digest was measured ranking **1st of 13** against `_0`…`_11`.

Reciprocal-rank-fusion ties are structural, not incidental: they are sums of `1/(k + rank)` over
integer ranks. A stage whose gate is "nDCG@10 improves on the multi-file class", and which
introduces digest documents in that same stage, could pass its gate on document naming alone with
no retrieval improvement whatsoever.

The harness therefore re-sorts by `(-score, doc_type, file_uuid, chunk_index)` before scoring, and
ships a test that swaps document id conventions and asserts the metric is unchanged.
:::

`trec_eval` is an **evaluation-only dependency**, isolated in `requirements-eval.txt` and never
built into published images: its `LICENSE.md` is permissive, but several of its source files carry
"permission is granted for use and modification of this file for research, non-commercial purposes."

## From gold turn ranges to chunk judgements

Both scoreable corpora publish gold as **inclusive turn ranges** — QMSum's `relevant_text_span` as
decimal strings, the synthetic tier's `gold_turns` as ints, deliberately the same convention. The
app retrieves *chunks*. Turning one into the other is the substance of the harness, and the only
part of it a reviewer can reasonably challenge, so the rule is stated rather than implied
(`backend/tests/eval/harness/qrels.py`).

**Turn → chunk.** The indexer chunks by speaker turn and the chunk document records `speaker`,
`start_time` and `end_time` but **no segment ids**. Chunks are therefore matched to source turns by
time overlap **restricted to the chunk's own speaker**. The restriction is what makes this exact
rather than approximate: a chunk contains only its own speaker's segments, but overlapping speech
means another speaker's turn routinely shares its time window. Dropping the restriction attributes a
neighbour's words to the chunk.

**Coverage.** Each covered turn contributes `word_count × (seconds inside the chunk ÷ turn
duration)`. The scaling matters because a long monologue is split into sub-chunks mid-turn.
Coverage is the gold share of that total.

**Coverage → graded relevance.** A parameter, not a magic constant:

| coverage | grade | reasoning |
|---|---|---|
| ≥ `high` (default **0.5**) | 2 | a chunk at least half made of gold material *is* the answer passage |
| > `low` (default **0.0**) | 1 | a chunk clipping the edge of a span is marginal, but a retriever ranking it above unrelated material is behaving correctly |
| otherwise | 0 | — |

Under linear gain a 2 is worth exactly twice a 1. `--binary-relevance` collapses the ladder for
anyone who considers grading unjustified, `--relevance-high` / `--relevance-low` move it, and the
values in force are written into every results file. Spans in the same file are unioned before
grading, so two adjacent ranges cannot each fall below the threshold that their union clears.

A query whose gold spans map to **no** chunk is dropped and counted
(`queries_dropped_unjudgeable`) rather than scored: keeping it would score every system zero and
drag every mean by the same amount, which looks like a result and is not.

## What the harness drives, and why

Addendum §4 of the #383 review requires benchmarking the **chat** path, not `/api/search`: chat
fuses over `dynamic_rrf_window(size) = max(100, min(size×4, 500))` while the search UI always fuses
over 500, so numbers from one do not characterise the other. §4 names `retrieve_chunks`; on this
branch that function lives in `app/services/search/chunk_retrieval.py` and is imported by
`services/chat/retrieval.py` and by nothing else — so driving it *is* driving the chat path, and §4
and the code agree.

Two stages and two scopes are measurable, because they fail differently:

| axis | value | meaning |
|---|---|---|
| stage | `retrieve` *(default, the control)* | `retrieve_chunks` alone — the candidate pool every fusion or indexing change moves |
| stage | `rerank` | + production cross-encoder + `diversity_sample`: what actually reaches the prompt. Raises rather than silently no-op'ing when the weights are absent |
| scope | `corpus` *(default)* | `file_uuids=None` — what chat actually does |
| scope | `gold-files` | **oracle**: restrict to the query's own gold files. An upper bound, not a system result |

The two scopes separate "the right meeting never surfaced" from "the right meeting surfaced and the
wrong passage in it won". A single corpus-wide number cannot tell those apart, and they have
different fixes.

**No LLM is involved at any point.** D6 requires the `LLM_PROVIDER`-empty deployment to stay
first-class, and a retrieval benchmark that needed a model would make exactly that deployment
unmeasurable.

Before measuring, the harness **refreshes → force-merges → refreshes** the single-shard chunk index.
Deleted documents leave tombstones whose term statistics still count toward IDF, so without it two
runs over the same corpus can disagree on how much re-indexing happened in between. No aggregation
is ever issued against a hybrid body (the OpenSearch 3.4 `score-ranker-processor` crash applies to
measurement code too); per-file coverage is derived from hits.

## Reproducing the numbers

```bash
# 1. an isolated stack — never the shared dev one
./opentr.sh start dev --fresh rag403 --port-offset 100

# 2. the corpora (once), then inject + index through the PRODUCTION indexer
./scripts/fetch-rag-eval-data.sh --accept-licenses
./scripts/inject-eval-corpus.sh --fresh rag403 --corpus qmsum

# 3. measure
./opentr.sh bench rag --fresh rag403
```

`bench rag` is a peer of the GPU bench arms, not a mode of them: it needs no GPU, no ASR and no LLM.
It resolves the deployment's recorded port offset and verifies **that deployment's own**
`otfresh-<name>-opensearch` container is running before exporting anything — the same lesson as
issue #399, where a bench gate validated the dev stack's container names while the bench overlay had
renamed everything.

Results land in `backend/tests/eval/baselines/<control-name>/`:

| file | contents | deterministic? |
|---|---|---|
| `metrics.json` | the full result: rows, corpus composition, licence tier, metric-engine provenance, relevance policy, retrieval config | **yes — byte-identical across runs** |
| `metrics.md` | the metric table | **yes** |
| `runinfo.json` | elapsed seconds and the resolved target | no, and deliberately outside the claim |

Compare a later stage against the committed control with
`--compare backend/tests/eval/baselines/stage1-baseline/metrics.json`, which prints the per-class
delta table D5 requires in the PR description.

Reproduction requires four things to be pinned, and all four are recorded with every result:

1. **Corpus** — dataset versions and checksums from `scripts/fetch-rag-eval-data.sh --verify`, plus
   the exact composition (which meetings, after which deduplication).
2. **Corpus state** — the injection manifest mapping each indexed file to its source meeting, turn
   and word counts, and whether its timings are real or synthetic.
3. **Seeds** — synthetic corpora regenerate byte-identically from their recorded seed.
4. **Metric implementation and version** — per the divergence above.

## Reproducibility: the index has to be stable, not just the measurement

A benchmark can be deterministic in the wrong place. This one was, and the gap took a stack rebuild
to expose.

**The measurement is deterministic.** Two consecutive runs against an unchanged index produce
byte-identical `metrics.json` and `metrics.md`. That was verified and is still true.

**The index was not.** Re-indexing one *unchanged* corpus three times produced three different
chunk counts and three different scores:

| run | chunks | nDCG@10 (all) |
|---|---|---|
| initial | 119,950 | 0.1052 |
| after a stack rebuild | 119,949 | 0.1023 |
| after a forced re-index | 120,540 | 0.1029 |

Identical inputs each time — 232 files, 129,062 segments, 2,145 speakers — and the index was
internally coherent on every run (no orphans, no stale tails, `doc_count == max(chunk_index)+1` for
all 232 files). The chunking genuinely differed.

**Cause: `ORDER BY start_time` is not a total order.** Overlapping speech and interpolated
backchannels routinely share an onset — **3,072 tie groups covering 6,152 segments** in this corpus.
Postgres returns tied rows in physical storage order, which a delete-then-bulk-insert reshuffles.
Tied segments swap, speaker-turn grouping changes, chunk boundaries move:

```
471.983  471.993  "Uh - huh ."
471.983  473.233  "I mean , if you did it at th..."
```

Whether that 10 ms backchannel sorts before or after the 1.25 s utterance it overlaps decides
whether the turn is split. Every chronological segment read now orders by
`(start_time, end_time, id)`, with an AST test failing any that does not end in the primary key.

:::danger Why this was worth chasing before building on the control
Every stage reports its delta against the previous stage as control, and the index-v6 stage
**mandates a full reindex** — so its control and treatment necessarily sit on different indexes.
Its gate is "nDCG@10 up on the multi-file class", and the drift from reshuffling (~2.8%) is the same
size as a plausible real improvement. An unstable index would have let that stage pass its own gate
on document reordering alone, and the result would have looked exactly like a win.

The general rule: **a control with an unmeasured reproducibility band is not a control.** Establish
the band before trusting any delta against it.
:::

## The Stage 1 baseline — the named control

Committed at `backend/tests/eval/baselines/`. Every later stage reports its delta against these,
per query class, per D5.

**Composition:** all 232 QMSum meetings (Tier A, MIT), injected through the production indexer,
**119,950 chunks**, 1,576 human queries, 0 dropped as unjudgeable, 0 unanswered. Chunk documents
average 17 words because speaker-turn chunking over conversational transcripts produces one chunk
per turn — that is production behaviour, not a harness artefact, and it is a large part of what
these numbers measure.

`stage1-baseline` — corpus-wide scope, what chat actually does:

| corpus | tier | class | n | nDCG@5 | nDCG@10 | nDCG@20 | R@5 | R@10 | R@20 | MRR |
|---|---|---|---|---|---|---|---|---|---|---|
| qmsum | A | lookup | 1172 | 0.1201 | 0.1107 | 0.1099 | 0.0641 | 0.0830 | 0.1017 | 0.2490 |
| qmsum | A | summarize | 404 | 0.1020 | 0.0889 | 0.0760 | 0.0188 | 0.0300 | 0.0439 | 0.2093 |
| qmsum | A | all | 1576 | 0.1154 | 0.1052 | 0.1012 | 0.0525 | 0.0694 | 0.0869 | 0.2389 |

`stage1-baseline-goldscope` — the **oracle** scope (perfect file selection); an upper bound, never a
system result:

| corpus | tier | class | n | nDCG@5 | nDCG@10 | nDCG@20 | R@5 | R@10 | R@20 | MRR |
|---|---|---|---|---|---|---|---|---|---|---|
| qmsum | A | lookup | 1172 | 0.3184 | 0.3006 | 0.2928 | 0.1238 | 0.1761 | 0.2357 | 0.5430 |
| qmsum | A | summarize | 404 | 0.4451 | 0.4138 | 0.3753 | 0.0561 | 0.0907 | 0.1412 | 0.6385 |
| qmsum | A | all | 1576 | 0.3509 | 0.3296 | 0.3140 | 0.1065 | 0.1542 | 0.2115 | 0.5675 |

**Reading these numbers honestly:**

- **Most of the loss is file selection, not passage ranking.** Handing the retriever the right
  meeting triples nDCG@10 (0.105 → 0.330). That is the single most useful thing Stage 1 establishes,
  and it is a direct argument for the summary/digest tier and the router: they attack the larger
  term.
- **Recall@k is bounded by the gold-set size.** A query's gold set averages **49.3** judged chunks,
  so R@20 cannot exceed ≈0.41 even for a perfect system. Compare recall across stages, never against
  1.0.
- **Corpus composition is doing work here.** All 232 meetings are pooled, including the 137 AMI
  `Product` meetings that are one fictional scenario — the composition measured above to cost 13
  points of R@1 on its own. The baseline is deliberately the *naive* pooling so later compositions
  can be compared against it; it is not the best number this corpus can produce.
- **These are `stage=retrieve` numbers** — the candidate pool, before the cross-encoder. The
  `rerank` stage measures what reaches the prompt and is not part of the committed control, because
  it depends on model weights being present in the cache.

:::note Synthetic timestamps
QMSum has no timestamps, but OpenTranscribe chunks and cites by time. Meetings without a timed
counterpart in AMI or ICSI receive **synthetic** timings, flagged as such in the injection manifest
so that no timing-derived metric can be computed from them. **Measured on the real corpus: 188 of
232 meetings (81%) get real timings** — all 59 Academic/ICSI and 129 of 137 Product/AMI. The 8 AMI
misses are meetings whose QMSum text diverges enough that fewer than 80% of turns align; provenance
is per file and all-or-nothing, because a file that is 60% measured and 40% invented is neither.
:::

## What we cannot currently claim

Stated plainly, because a benchmark's limits are part of its result:

- **Publishable retrieval quality rests on QMSum.** The other Tier A corpora supply realism,
  multilingual coverage or long-context, not additional English meeting-retrieval judgements.
- **Two of the four query classes are unmeasured as of Stage 1.** `multi_file` and `aggregation`
  have no real-data ground truth anywhere, and the synthetic corpus that supplies them is generated
  but **not yet injected**: its on-disk shape (`meetings/part-*.jsonl`, `turns[].content`,
  `meeting_key`) does not match what the injector's generic JSON adapter reads
  (`meetings.jsonl`, `turns[].text`, `meeting_id`), so a converter is still owed. The harness's
  loader, gold-span handling and per-class reporting for those classes are implemented and unit
  tested — what is missing is the data on the stack, not the code.
- **Injecting the synthetic tier will move the QMSum numbers**, because both corpora share one
  index and its document frequencies. Any mixed-corpus baseline is a new control, not a comparison
  against this one.
- **No answer-quality number exists.** Aggregation queries carry `scored_on: "answer"` and are
  scored on exactness, not ranking; nothing in Stage 1 scores an answer.
- **Scale is split**: the largest real corpus (MeetingBank, 31.7 M words) is internal-only.
- **Multilingual coverage is 20 languages scored, not 100.** The unscored remainder is enumerated
  with a specific reason each — no public benchmark with relevance judgements, transcripts but no
  queries, or non-commercial licensing — rather than being implied by the product's language list.
