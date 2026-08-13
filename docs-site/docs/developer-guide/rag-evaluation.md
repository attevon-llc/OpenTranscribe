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

## Scoring an answer, not a rank

Three of the four query classes are scored by ranking. The fourth cannot be. An **aggregation**
query's ground truth is an integer ("how many meetings discussed X"), a file set ("which meetings
mention Y"), or a speaker with a session count — and no nDCG can say whether the answer was
*right*. Those queries carry `scored_on: "answer"` and go to a second engine
(`backend/tests/eval/harness/answers.py`) with its own measures, its own query set and its own
table. **No column name appears in both tables**, so an answer score can never be misread as a
retrieval score:

| measure | meaning |
|---|---|
| **EM** | exact match under the policy below — **this is Stage 4's gate** |
| `partial` | diagnostic partial credit; never a substitute for EM |
| `answered` | the share of the class the system attempted at all |

### The scoring rules, and why each is what it is

Every rule is a **parameter written into the results file**, the way the retrieval side made its
overlap thresholds parameters — a threshold nobody wrote down is a threshold nobody can challenge.

- **A count is exact.** `--answer-count-tolerance` defaults to **0**. Aggregation is computed by an
  exact mechanism (a terms aggregation, a `SUM`), so a tolerance would hide precisely the defects
  this class exists to catch — a double-counted overlapping chunk, an unrefreshed index, a filter
  that missed one file — and "off by one" is still a wrong answer to a user. A number gets no
  interpolated partial credit either: `partial` equals EM for a count.
- **A file set is exact-match for the gate, with F1 beside it.** A subset is *wrong*: 7 of 8 files
  is a wrong answer to "which meetings discuss X". But EM alone cannot separate "the aggregation is
  right and one file's phrase straddled a chunk boundary" from "the marker matched nothing", and
  those have different fixes — so F1 is reported as a **diagnostic**, never in place of EM.
  `--answer-set-credit exact` collapses it onto EM for anyone who considers partial credit
  unjustified.
- **A speaker answer is two fields** (name, session count) and needs both for EM. `partial` is the
  fraction of fields correct, so "right person, wrong count" is visibly different from "wrong
  person". Names are compared casefolded with whitespace collapsed — the only tolerance anywhere in
  the answer path.
- **An unanswered query scores zero and is counted.** `evaluate_answers` iterates the **gold** query
  set exactly as the retrieval side iterates the qrels (`trec_eval -c` semantics). This is not
  theoretical: a mean over what the system returned reads **1.000 where the truth is 0.500**, and
  the suite ships that comparison as a test so the substitution cannot quietly regress.
- **Every set is emitted sorted.** `PYTHONHASHSEED` is unpinned in this repo and set iteration order
  varies per process; an unsorted `list(set())` has already been a real bug here.

### Where the answer comes from — and why it is never a model

#403 requires aggregation to be answered from **OpenSearch aggregations or Postgres, never by an
LLM counting**, and D6 makes the no-LLM deployment first-class. An answer-scoring path that needed a
model would contradict the property it exists to measure, so `--answerer` selects between two
model-free sources:

| answerer | what it is |
|---|---|
| `none` | declines every query. The **honest product floor**: no aggregation route exists before Stage 4, so it scores 0.000 EM with `answered` 0.000 saying why |
| `reference` *(default)* | a rules intent parser over the five question frames, then a terms aggregation or SQL |

Its mechanisms, recorded per intent in every results file:

| rule | question | mechanism |
|---|---|---|
| R3 / R4 | how many / which meetings mention X | `match_phrase(content.exact)` + `terms(file_uuid)` aggregation |
| R5 | how many times in total did we defer X | Postgres `regexp_count` over `transcript_segment.text` |
| R6 | who attended the most \<kind\> sessions for \<team\> | title-scoped `terms(speakers)` × `terms(file_uuid)` |
| R7 | how many meetings **in \<month\>** discussed X | the phrase aggregation ∩ a Postgres meeting-date filter |

No aggregation is ever issued over a hybrid body (the OpenSearch 3.4 `score-ranker-processor`
crash), occurrence *counts* come from Postgres because chunk overlap double-counts a long turn's
tail, and a bucket list truncated at the size limit **raises** rather than reporting a count that
looks right.

:::warning The reference answerer is the instrument's control, not the product's answer
It does not touch the chat path — there is no aggregation route in the product until Stage 4 — and
`is_production_path: false` is recorded in every results file it writes. Its value is that Stage 4's
router arrives with a number to beat and a per-rule breakdown of where the difficulty is, instead of
a gate whose only prior reading is zero.
:::

### Measured: the synthetic tier at the 200-meeting budget

432 files indexed (232 QMSum + 200 synthetic), 208,333 chunks. **20 aggregation queries are
scoreable** — 21 have every gold file indexed, and one R7 query is dropped because 2 of its 4
*out-of-month* mentions are not, which would have turned a filtered count into an unfiltered one
that scores correct for the wrong reason.

| corpus | tier | class | rule | n | unans. | EM | partial | answered |
|---|---|---|---|---|---|---|---|---|
| synthetic | A | aggregation | all | 20 | 0 | **1.0000** | 1.0000 | 1.0000 |
| synthetic | A | aggregation | R3-agg-count-files | 4 | 0 | 1.0000 | 1.0000 | 1.0000 |
| synthetic | A | aggregation | R4-agg-list-files | 4 | 0 | 1.0000 | 1.0000 | 1.0000 |
| synthetic | A | aggregation | R5-agg-count-events | 4 | 0 | 1.0000 | 1.0000 | 1.0000 |
| synthetic | A | aggregation | R6-agg-speaker-top | 4 | 0 | 1.0000 | 1.0000 | 1.0000 |
| synthetic | A | aggregation | R7-agg-temporal-count | 4 | 0 | 1.0000 | 1.0000 | 1.0000 |

The `none` control on the same 20 queries — **0.0000 EM, 20 unanswered, `answered` 0.0000** — is
what makes the row above a measurement rather than a tautology: the same scoring path over the same
gold set moves from 0 to 1 purely on who answered. Both tables are committed under
`backend/tests/eval/baselines/stage1-synthetic-answers/` (`answers.md` and
`answers-null-control.md`), and two consecutive runs produce **byte-identical** `metrics.json`,
`metrics.md` and `answers.md` (verified by sha256; elapsed time lives in the gitignored
`runinfo.json`, outside the claim).

### What a 1.000 does *not* mean

- **It is an upper bound on an easy surface, not a claim about real questions.** Aggregation markers
  are exact multi-word phrases ("the Cedar Lantern compliance audit"), which is defensible only
  because this class is answered by exact matching rather than by ranking — and it means the number
  measures the *mechanism*, not robustness to paraphrase. A user asking "how many meetings covered
  the Cedar Lantern audit" is not covered by any measurement here.
- **It is 20 queries, four per rule.** The corpus holds 166; the rest need a larger injection budget.
- **The index has to be complete for it to hold.** During indexing, the same R3 query that now scores
  exact returned 3 of 12 files: the mechanism reads the index, so an incomplete index produces a
  confidently wrong count. That is the failure mode the `answered`/EM split is meant to surface.
- **R7's month filter reads the date the injector stamped into
  `media_file.metadata_important`**, because no recorded-date column is populated for injected
  files. A production answerer would read a real one; this is recorded as a limitation of the
  measurement, not presented as the production mechanism.
- **The intent parser is matched to the generator's five question frames.** It recovers the subject
  phrase from a natural-language question — the phrase is never the answer — but a differently
  worded question is *declined*, and a declined query scores 0.

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
| `metrics.md` | the retrieval metric table | **yes** |
| `answers.md` | the answer table (EM / partial / answered), per query class and per rule | **yes** |
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

## Three ways this harness measured the wrong index

The reproducibility section above fixed the *chunking*. Establishing the pre-v6 control then
found three more ways a number can be recorded against an index that is not what it looks like.
All three are now closed in code, and each one is worth knowing about because none of them
produces an error — they produce a plausible number.

### 1. The workers that index had no punkt (issue #436)

`chunk_transcript_by_speaker_turns` splits sentences with NLTK punkt when a punkt model is
resolvable and with a regex otherwise, and the two cut in different places — **49 files / 226
chunks apart** over this corpus. The `nltk_data` mount existed on `backend`, `celery-worker` and
the GPU workers, and on **neither of the two workers that actually index**:
`index_transcript_search` runs on the `embedding` queue and `reindex_transcripts` on `cpu`.

So every chunk in the index was cut by the regex fallback while every test process and the host
venv resolved punkt, and `--dispatch eager` (host) built a different index from `--dispatch celery`
(worker) *from the same corpus*. The mount is now on both workers, with a test that derives
"which queues chunk" from the call graph and "which service serves that queue" from the compose
file, so a task moved to another queue keeps the assertion honest.

Because it changes chunk boundaries for every subsequently indexed file, it deliberately shipped
inside the single index-v6 reindex rather than on its own.

### 2. Settling was inferred from a plateau

The harness recorded whatever the index held when it was asked. Polling the total chunk count
alone reported phantom deltas of **223 / 357 / 591 chunks** between runs over a corpus nobody had
changed: the count was read while a reindex was still walking the file list, and a plateau in a
rising count is indistinguishable from the end of one.

`bench rag` now refuses to measure until three conditions hold together
(`tests/eval/harness/index_reader.await_settled`):

1. **every expected file carries chunks** — a reindex deletes a file's documents before writing
   the new ones, so a mid-run poll sees a corpus that is merely *smaller*;
2. **(files, chunks) is identical on two consecutive polls**;
3. **nothing predates the run**, when a dispatch timestamp is supplied. Conditions 1 and 2 are
   both satisfied by a reindex that has been *dispatched and has not started* — Celery queue
   latency easily outlasts two poll intervals — and the check would then certify the old index as
   the new one, producing an "after" measurement byte-identical to the "before" for the best
   possible reason.

`--expect-files N` overrides the manifests' own count; `--expect-files -1` skips the check.

### 3. A reindex could delete most of the index

Establishing the control, a full reindex reduced the corpus from **432 files / 208,333 chunks** to
**252 files / 111,097 chunks**. Not a measurement artefact — a real, pre-existing product bug:

`app/main.py`'s startup sweep cleared `reindex_lock:*` / `reindex_state:*` / `reindex_uuids:*`,
from the **API** process, which restarts independently of the Celery workers that own those keys.
With the lock gone mid-reindex, `search_index_maintenance` dispatched a second coordinator; that
coordinator rewrote the shared state with its own `worker_count`; the in-flight batch workers
incremented into it; completion fired holding 22 of 432 uuids; and the post-reindex orphan sweep —
which deletes every file *not* in that set — targeted 195,930 documents.

The API no longer clears those keys (they carry TTLs, so a genuinely dead coordinator still
unblocks itself), and the sweep now fails closed: it declines unless indexed + failed accounts for
every file the coordinator snapshotted. Skipping the sweep leaves stale documents the next full
reindex removes; running it on an incomplete tally deletes an index nothing recovers.

:::tip Measuring on a stack with Celery Beat running
Even with the destructive path closed, a maintenance-dispatched partial reindex overlapping a
measurement changes what is being measured. Stop `celery-beat` for the duration of a control run.
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

## Stage 3 — index v6, measured against a control taken on the same day

`stage1-baseline` is not the control for this. It was measured before three determinism
fixes and on a 232-file corpus that is now 432, so a delta against it would be mostly
composition and chunking. `stage3-control-pre-v6` replaces it: same code, same corpus, same
day, measured after the corpus was proven stable.

**Determinism, measured not assumed.** Two consecutive full re-indexes of the unchanged
corpus (`scripts/reindex_eval_corpus.py`, which drives the real `reindex_transcripts` task
and waits for the settle) produced **432 files / 208,332 chunks both times**, and the
control measurement was byte-identical across two runs. After the v6 reindex the chunk plane
is *still* exactly 208,332 documents, with 2,576 digest documents beside it — so the digest
tier is purely additive and the two tables below differ only by what Stage 3 changed.

| | pre-v6 control | index v6 |
|---|---|---|
| files | 432 | 432 |
| chunk documents | 208,332 | 208,332 |
| digest documents | — | 2,576 (~6.0 sections/file) |
| embedded field | `content` | `embedding_text` (title + date + roster + body) |

### Per class, corpus-wide scope (what chat actually does)

| corpus | class | n | nDCG@10 pre-v6 | nDCG@10 v6 | Δ | Δ nDCG@5 | Δ MRR | Δ R@10 |
|---|---|---|---|---|---|---|---|---|
| qmsum | lookup | 1172 | 0.0885 | **0.1038** | +0.0153 | +0.0154 | +0.0326 | +0.0130 |
| qmsum | summarize | 404 | 0.0561 | **0.0821** | +0.0259 | +0.0279 | +0.0572 | +0.0095 |
| qmsum | all | 1576 | 0.0802 | **0.0983** | +0.0181 | +0.0186 | +0.0389 | +0.0121 |
| synthetic | lookup | 50 | 0.3134 | **0.3363** | +0.0228 | +0.0342 | +0.0370 | +0.0100 |
| synthetic | multi_file | 25 | 0.1957 | **0.2132** | +0.0175 | +0.0466 | +0.0860 | −0.0280 |
| synthetic | all | 75 | 0.2742 | **0.2952** | +0.0210 | +0.0383 | +0.0533 | −0.0027 |

**The gate is met**: nDCG@10 is up on the multi-file class, and the lookup class did not
regress — it rose in both corpora, which is more than the gate asked for.

### Where the gain comes from, and what it costs

Digest documents are **not retrieved** in Stage 3 — `_build_filters` carries the chunk-plane
clause, so neither search nor chat can return one. The movement is entirely the
`embedding_text` repoint: every chunk is now embedded as
`"{title} | {date} | participants: {roster}\n\n{chunk text}"` instead of the bare chunk. The
synthetic queries ask things like *"Across the sprint retrospective sessions for the
logistics team…"*, where the discriminating words live in the **title** and in nothing
anybody said; BM25 already scored `title`, the vector leg did not.

The cost is visible and expected. The measured embedding window is **128 wordpieces**, so a
~30-piece header displaces roughly the last 20 words of a 200-word chunk from that chunk's
own vector — which is why the top-heavy measures (nDCG@5, MRR) gain most while
`multi_file` R@10 slips 0.028 and R@20 slips 0.021: the right files are pulled up, and a
little of the tail is pushed out. On this corpus that trade is clearly positive; it is also
the first knob Stage 5's bake-off should sweep (header on chunks vs digests only).

### G9 — BM25 IDF cross-talk between doc types

Digest documents carry a `content` field, so they contribute to the document frequencies
that score chunk queries. Measured: **2,576 digests against 208,332 chunks, 1.2% of the
index**. The addendum's guard for this is "lookup must stay within noise"; lookup *rose* in
both corpora, so if the effect is present it is smaller than the `embedding_text` gain. The
mechanism is named here so a future regression is not mysterious, but nothing in this
measurement attributes anything to it.

## What we cannot currently claim

Stated plainly, because a benchmark's limits are part of its result:

- **Publishable retrieval quality rests on QMSum.** The other Tier A corpora supply realism,
  multilingual coverage or long-context, not additional English meeting-retrieval judgements.
- **`multi_file` and `aggregation` have no real-data ground truth anywhere**, so both rest entirely
  on the synthetic tier. That tier is now injectable — a native adapter reads the generator's own
  format and selects meetings by **gold closure**, because aggregation markers are planted across
  the whole 2,000-meeting corpus and a query whose gold set is only partly present is correctly
  dropped. At the default budget (200 meetings, ~1.1× QMSum) that closes 25 `multi_file` and 21
  `aggregation` queries; a first-N-by-key subset would have closed 4.
- **`aggregation` is now scored on its answer** (exact match, see [Scoring an answer, not a
  rank](#scoring-an-answer-not-a-rank)), but by the harness's own aggs+SQL reference answerer —
  **the product has no aggregation route until Stage 4**, and measured through the `none` answerer
  it scores 0.000. The published 1.000 characterises the corpus and the mechanism, not the shipped
  system.
- **Injecting synthetic data moves the QMSum numbers.** Retrieval runs corpus-wide, so the
  candidate pool roughly doubles and document frequencies shift. Run the QMSum-only control before
  and after and record the delta; **never compare a measurement taken across the injection.**
- **Injecting the synthetic tier will move the QMSum numbers**, because both corpora share one
  index and its document frequencies. Any mixed-corpus baseline is a new control, not a comparison
  against this one.
- **No *generation* quality number exists.** Aggregation exactness is scored; faithfulness,
  citation correctness and answer prose are not, and the synthetic tier is explicitly not a source
  of generation ground truth. Nothing in this harness evaluates what a model wrote.
- **Scale is split**: the largest real corpus (MeetingBank, 31.7 M words) is internal-only.
- **Multilingual coverage is 20 languages scored, not 100.** The unscored remainder is enumerated
  with a specific reason each — no public benchmark with relevance judgements, transcripts but no
  queries, or non-commercial licensing — rather than being implied by the product's language list.
