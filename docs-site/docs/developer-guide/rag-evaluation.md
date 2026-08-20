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
| AMI v1.6.2 | A (CC BY 4.0) | none | real word-level timings, speaker channels; 34 of its 171 meetings (not redistributed by QMSum's `Product` domain) are also injectable as a distractor-only haystack — see "AMI distractor haystack" below |
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
- **R7's month filter, in THIS reference-answerer table, reads the date the injector stamped
  into `media_file.metadata_important`.** That is the harness's gold source and no product code
  may read it. Since v391 the **product** answers R7 from `media_file.recorded_date` instead —
  a real column, written at injection time the way ingest writes it — so the product path is no
  longer scoring against the answer key. See "The product's aggregation path" below for what
  that number does and does not cover.
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
| `metrics.json` | the full result: rows, corpus composition, licence tier, metric-engine provenance, **embedding provenance**, relevance policy, retrieval config | **yes — byte-identical across runs** |
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
5. **Embedding model** — per the section immediately below. It is pinned in `metrics.json`'s
   `index` block, not in `runinfo.json`, because it is part of the claim rather than part of the
   run.

## A retrieval number that does not name its embedding model is not comparable

Swap the embedding model and the same code over the same corpus produces a different number. So the
model belongs in the **claim**, beside the corpus and the metric engine — and the first nine
committed baselines did not carry it.

`metrics.json`'s `index` block now records five fields:

| field | meaning |
|---|---|
| `embedding_models` | the models the indexed **documents themselves** report |
| `embedding_verdict` | `empty` / `unattributed` / `uniform` / `partially_unattributed` / `mixed` |
| `embedding_unattributed` | documents in the `"neural"` UNKNOWN bucket |
| `embedding_dimension` | the index's `knn_vector` dimension |
| `configured_embedding_model` | what the **settings** say |

:::danger The settings are not authoritative about the vectors
The label is surveyed from the documents, never from `get_search_embedding_settings()`. Issue #437
established that two `SystemSettings` keys — `search.embedding_model`, which drives the index
dimension, and `search.opensearch_model_id`, which drives the ingest pipeline — are written by
**different endpoints with nothing reconciling them**. A settings-derived label can therefore name
a model that never touched a single vector in the corpus being measured, which is worse than no
label at all.

`configured_embedding_model` is kept as a separate, differently-named field precisely so that drift
between what is configured and what is indexed shows up in the committed baseline instead of being
collapsed into one number that looks authoritative.

The harness **refuses to write a baseline (exit 3) over a proven-mixed vector space** — two
*named* models. Cosine similarity between two models is not a similarity, so a ranking scored over
such an index fused two incomparable populations, and no later reading of that number could be
correct.
:::

### The model behind the existing numbers is unknowable, and saying so is the result

Every document in the epic's index — all 210,908 — carries `embedding_model: "neural"`. That is
#437's single UNKNOWN bucket, kept deliberately as *one* unknown rather than backfilled with the
current model, which would assert something nobody can know. So the verdict on every baseline here,
re-derived ones included, is **`unattributed`**, and the only evidence for the model is
circumstantial: a 384-dimension index and a configured
`huggingface/sentence-transformers/all-MiniLM-L6-v2`.

That is a weaker claim than "measured on all-MiniLM-L6-v2", and it is the honest one. What the
re-derived baselines now record is not *which* model, but the auditable fact that **nobody can
tell** — which a later comparison can check, where silence could not.

### Which baselines were re-derived, and which are historical

Full table and reasoning: `backend/tests/eval/baselines/README.md`.

| baseline | index when measured | status |
|---|---|---|
| `stage3-index-v6`, `stage4-control`, `stage4-routed`, `stage4-aggregation` | 210,908 | **re-derived** with provenance |
| `stage1-baseline`, `stage1-baseline-goldscope` | 119,950 | **historical** — pre-v6, pre-determinism-fix |
| `stage1-synthetic-answers` | 208,333 | **historical** — pre-v6, pre-determinism-fix |
| `stage3-control-pre-v6` | 208,332 | **historical by definition** — the *before* arm of the v6 A/B |
| `stage4-router` | — | **not applicable** — a classifier over query strings, touches no index |

The historical four measured an index that **no longer exists**. Re-running their commands today
would not re-derive them; it would replace a measurement of one index with a measurement of a
different one, under the old name. Nothing was deleted — `stage1-baseline` remains
`--control-name`'s default and the documented `--compare` target, and
`stage1-synthetic-answers/answers-null-control.md` holds the 0.0000-EM floor that exists nowhere
else.

**Re-deriving the four moved nothing.** Every metric row, every answer row and the whole
`digest_leg` block came back identical to what was committed; `metrics.md` and `answers.md` were
byte-identical files. The entire diff was the `index` block. That is the outcome a re-baseline
wants: the numbers were already right, and they now say what they were measured with.

## Reproducibility: the index has to be stable, not just the measurement

A benchmark can be deterministic in the wrong place. This one was, and the gap took a stack rebuild
to expose.

**The measurement is deterministic.** Two consecutive runs against an unchanged index produce
byte-identical `metrics.json` and `metrics.md`. That was verified and is still true — re-checked at
the provenance re-baseline, where `stage4-control` (1,651 queries) and `stage4-aggregation` were
each run twice and matched on `metrics.json`, `metrics.md` **and** `answers.md` by sha256. Across
that whole measurement window the index's `indexing.total` held at **825,795** — not one document
was written — which is the cheap, non-invasive way to prove a control run measured the index it
claimed to.

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

:::warning These two tables are HISTORICAL — do not use them as a control for current work
Both were measured on a **119,950-chunk, 232-file, qmsum-only** index, before the v6 reindex and
before the three determinism fixes below. That index no longer exists, so neither table is
re-derivable and neither carries embedding provenance. They are kept because they are the evidence
for the file-selection result quoted throughout this page — and because re-running their commands
today would quietly replace them with a measurement of a *different* index under the same name. The
control for current work is `stage3-index-v6` / `stage4-control`. See
[which baselines were re-derived](#which-baselines-were-re-derived-and-which-are-historical).
:::

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

## Stage 4 — the router, the counted tier and the digest leg

Three baselines, taken in a single measurement window against index v6, all committed under
`backend/tests/eval/baselines/`. Each is a control **for** something specific, and reading one for
a question it does not answer is the failure this section exists to prevent.

| Baseline | Command | It is a control for |
|---|---|---|
| `stage4-control` | `--corpus qmsum --corpus synthetic` | **D5**: that Stage 4 did not regress the chunk plane |
| `stage4-routed` | `--stage route` (adds `digest_leg`) | what the **digest tier** contributes, as file selection |
| `stage4-aggregation` | `--answerer product` | what the **product's** aggregation path scores |
| `stage4-router` | `scripts/benchmark_router.py` | the router as a **classifier**, no stack required |

### The control: zero, to four decimal places

Against `stage3-index-v6`, every class and every measure:

| corpus | class | Δ nDCG@10 | Δ R@10 | Δ MRR |
|---|---|---|---|---|
| qmsum | lookup | +0.0000 | +0.0000 | +0.0000 |
| qmsum | summarize | +0.0000 | +0.0000 | +0.0000 |
| synthetic | lookup | +0.0000 | +0.0000 | +0.0000 |
| synthetic | multi_file | +0.0000 | +0.0000 | +0.0000 |

D5's "lookup must never regress" is satisfied **by identity, not by a margin**: the router, the
counted tier, the digest plane and `mask_digests` are all live, and the chunk plane is byte-for-byte
what it was. G9's predicted BM25 IDF cross-talk did not materialise.

### Why the routed run's ranked list is identical, on purpose

`--stage route` puts the production router in the loop, and its metric table is **the same table**.
That is the design, and it is test-asserted.

Digest hits are recorded *beside* the ranked list, never merged into it. The qrels judge **chunks** —
they map gold turn spans onto whatever chunks the indexer produced — so a digest document is
unjudged: it would score 0 and push relevant chunks down. A merged run would therefore score *worse*
than the control **by construction**, and every reading of that number would be wrong — it would look
like the digest tier hurt retrieval when it was an artefact of the judgement space. Judging digests
instead would mean inventing a second relevance rule mid-epic, which is how a qrels file stops
meaning anything.

So the digest tier is measured for what it is actually claimed to do. Stage 1 established the shape
of the problem: corpus-wide nDCG@10 **0.1052** against **0.3296** with an oracle gold-file scope —
**roughly two thirds of the loss is picking the wrong recording.**

### The digest leg, measured as file selection

```
queries scored                                     1651
routed to the digest tier                           401   (of a 404-query summarize class)
...and the tier returned something                  401   (100%)
digest leg found a gold file                         93
chunk leg found a gold file in its top 10 files     193
RESCUED (digest found it, chunk top-10 did NOT)      16   = 3.99% of routed
```

**Read 3.99% as the modest result it is.** Of the 93 gold files the digest leg found, **77 were
files the chunk leg already had** — re-finding those is worth nothing and is excluded by the strict
definition. Across all 1,651 scored queries the rescue rate is **0.97%**. It is a real effect on the
~4% of summarize-class queries where the digest tier is the only reason the right recording is
reachable at all; it is **not** the answer to file selection, and it recovers only a small corner of
the 0.1052 → 0.3296 gap.

Routing on live traffic reproduced the offline confusion matrix without adjustment: 404/404
summarize, 1220/1222 lookup, 25/25 `multi_file` → lookup, and the same two borderline QMSum leaks
behind the committed **0.104%** leakage figure.

The arithmetic between 404 classified `summarize` and 401 that reached the digest tier is worth
following, because neither number is the other's superset:

```
404 summarize-class queries
 −5  carry a QUOTED PHRASE, which removes the digest tier
 ───
399
 +2  lookup-class queries that leaked to summarize (the 0.104%) and got the tier
 ───
401  routed to the digest tier
```

Both adjustments are the design working rather than failing. A **quoted phrase removes the digest
tier** because a digest is *selected* sentences: a literal phrase can be absent from the digest and
present in the transcript, and answering "not mentioned" from a digest is the silent-wrong-answer
shape this epic keeps hitting. And the two leaked lookups keep their chunk tier throughout, so they
cost a reduced excerpt budget and nothing else.

`tiers` on the record is what distinguishes "the tier was not asked for" from "the tier was asked
and returned nothing" — without it, `routed_to_digest_tier` would be unreadable.

### The product's aggregation path: 0.800 -> 1.000, once it knew when meetings happened {/* #the-products-aggregation-path */}

| answerer | EM | R3 count | R4 list | R5 events | R6 speaker | R7 temporal |
|---|---|---|---|---|---|---|
| `none` (pre-Stage-4 floor) | 0.000 | 0/4 | 0/4 | 0/4 | 0/4 | 0/4 |
| `product` (before v391) | 0.800 | 4/4 | 4/4 | 4/4 | 4/4 | **0/4** |
| **`product`** (v391) | **1.000** | 4/4 | 4/4 | 4/4 | 4/4 | **4/4** |
| `reference` (harness ceiling) | 1.000 | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 |

The comparison means something only because the two share **no intent parsing**: the reference's
regexes are matched to the generator's exact question frames, while the product strips a *generic*
interrogative frame.

R7 used to fail for a reason that was a **product gap, not a harness artefact**: `media_file`
recorded `upload_time` and nothing else, so "meetings in March 2025" filtered on the date a file
was *ingested*. All 432 corpus files shared one upload date. Every user with a back-catalogue had
that problem. `v391_add_recorded_date_provenance` added `recorded_date` **and its source**, and
`services/chat/aggregation_service._files_in_period` now resolves the period against it in
Postgres.

#### What the 4/4 proves, and what it does not

**It proves the product filters correctly on a recorded date. It does not prove we can derive
one.** The distinction is the whole reason this subsection exists:

- On this corpus the date reaches `recorded_date` from the **corpus record**, written by the
  injector — the analogue of a container `creation_time` for a row that has no media. That is the
  `container` source, and it is the only one exercised here.
- The **filename** and **transcript** sources are **not measured by this number at all**: these
  meetings are titled `"{Team} — {kind} #{n}"`, their filenames carry no date, and the generated
  dialogue never states one. Their only evidence is their unit tests
  (`tests/unit/test_recorded_date_sources.py`) and the mutants those kill.
- A file no source can date is **excluded** from the filter and reported in
  `coverage["undated_files_excluded"]`, so a count is a **floor** over any corpus that is not
  fully dated. Here 200 of 432 files are dated (QMSum's meetings carry no date and are left
  undated rather than given an invented one), and the R7 gold sets live entirely in the synthetic
  half.

#### The re-injection did not move the retrieval baselines, and that was checked

Writing the dates required re-running the injector, which is exactly the operation that can
invalidate every retrieval number measured against the previous injection. It did not, and the
evidence is three-fold rather than assumed:

- the injector's **skip path** refreshes the date and nothing else, so no segment was rewritten
  (`--force` would have deleted and reinserted all 101,620 of them, re-chunking every file);
- the OpenSearch index was **byte-identical** before and after — `docs.count`, `docs.deleted`,
  `store.size` and, decisively, `indexing.total` (825,795) all unchanged, i.e. **not one document
  was written**;
- `stage4-control` re-run over **both** corpora came back **bit-identical on all six rows**
  (every nDCG/R/MRR to within 1e-9), and two consecutive `stage4-aggregation` runs produced
  byte-identical `metrics.json`.

### ⚠️ A metric we replaced, and why — do not quietly drop metrics {/* #a-metric-we-replaced */}

The #383 plan specified, for map-reduce: *"on the summarize class with N files in scope, distinct
`file_uuid`s represented in the answer goes from ~N/4 (today's `max_chunks_per_file` ceiling) to
N."*

**Measured, over a 25-recording scope against a real model, it went 1/25 → 0/25.** It did not move,
and the honest reading is that **the metric measured the wrong thing**. Operationalised as
"recordings the answer names", it rewards enumeration — but a model asked to *"summarise what these
sessions covered across all of them"* answers thematically, and that is the better answer, not a
worse one. The metric would have scored a good summary zero and a list of twenty-five titles full
marks.

What actually improved is checkable, and was checked against Postgres rather than read approvingly:

| | control (chunk leg only) | with the overview block |
|---|---|---|
| evidence covers | 12 of 25 files | **25 of 25** |
| recordings claimed | — | **25** ✓ (truth: 25) |
| total duration claimed | — | **44h 30m** ✓ (truth: 160,250 s) |
| distinct speakers claimed | — | **102** ✓ (truth: 102) |
| what it said | *"the only excerpt that details substantive topics is [12]… specify which recording you would like me to search"* | a corpus-level summary across all 25 |

**The replacement metric: corpus-level claims in the answer are verifiable against the full scope.**
The control could make none of them — it had a quarter of the collection and asked the user to pick
a recording.

Recorded at this length because a metric that is silently swapped is how a baseline stops meaning
anything. The old one is written down, the measurement that retired it is written down, and the
replacement says what it measures.

:::warning The first run of that measurement produced a confidently wrong answer
Before the fix, the overview was composed from the *ranked* digest leg. Asked for 50 sections over
a 25-file scope it returned 50 sections drawn from **8 files**, the block was headed
`recordings: 8`, and the model faithfully reported *"8 vendor review board sessions"* over a scope
of twenty-five. **Ranking picks the best passages; mapping covers every document** — see
[the prior-art page](rag-prior-art-and-packages.md#ranking-picks-the-best-passages-mapping-covers-every-document).
No unit test would have caught it: every unit test hands the composer the summaries it was supposed
to have.
:::

## Stage 5 — the retrieval tuning bake-off

Twenty-four arms — ten fusion, nine budget, five candidate-pool — plus a reranker licence gate,
all against one unchanged index (`indexing.total` **825,795** start to finish). **Nothing was
adopted.** That is the result, not a failure to produce one, and the numbers that justify it are
below in full, losers included and with their margins.

Two things came out of it that matter more than the table: **a defect in the instrument** that
made every `--stage rerank` number describe a pipeline that does not exist, and the measurement
that **the shipped cross-encoder costs 20.6% / 32.7% nDCG@10** on this corpus.

### "Both tiers" is the gate, and it had to be read before it could be applied

#403's Stage 5 gate is *"adopted config wins in both tiers"*, and D5 spells the rule out: *"per
query class, per model tier. A win in one tier and a loss in the other is not a win."* The word
**tier** carries three unrelated meanings on this page — licence tier (A/B/C), evaluation-corpus
tier (fixtures / public / synthetic), and model tier — so the gate is ambiguous until you trace
it. The plan does define it: *"per query class **and per model tier** (local vs. API)"*
(#383 body), i.e. **the LLM tier**, and Phase 7 attaches it specifically to the reranker A/B —
*"Run per model tier (reranking matters more for small models)."*

That has a consequence which has to be stated rather than quietly worked around:

:::danger The model-tier axis is unmeasurable by this harness, by design
Every number here is produced with **no LLM anywhere** (D6). A fusion strategy changes the order
OpenSearch returns documents in; that order is identical whichever model later reads them. So
for the fusion and budget arms the model-tier axis is not merely unmeasured — it is **invariant
by construction**, and reporting "it won in both model tiers" would be reporting the same number
twice.

Where the axis is *not* vacuous is the **reranker**, exactly as the plan says. Judging a reranker
per model tier requires an answer-quality measurement, and [this harness has
none](#what-we-cannot-currently-claim). That is why no reranker is adopted below, and it is a
structural gap, not an oversight.

**The rule actually applied to the fusion and budget arms is therefore: a win must hold on BOTH
CORPORA — QMSum and the synthetic tier — with the lookup class never regressing on either.**
That is the discipline every Stage 3 and Stage 4 table already used ("it rose in both corpora"),
it is what D5's *"a win in one tier and a loss in the other is not a win"* was enforcing in
practice, and — as [the weighted arms below](#the-two-corpora-want-opposite-leg-weights) show —
it is not a formality: two arms won on one corpus and lost heavily on the other.

That rule has since been **measured rather than asserted**: over these 24 arms the two corpora
rank changes at Kendall tau-b **+0.301** overall and **−0.005 on the lookup class**, and on the
fusion arms that are genuine candidates they are significantly **anti**-correlated. See [Do the
two corpora agree?](#corpus-agreement) — the analysis tightens this gate rather than relaxing it.
:::

### Method — how to re-run this, or add an eleventh arm

An arm is one `benchmark_rag.py` invocation. Everything that distinguishes it is on the command
line, and the resolved arm is written into that run's `metrics.json` under `retrieval.fusion`,
so no results file can fail to name the pipeline it measured.

```bash
export OT_EVAL_PYTHON=/path/to/backend/venv/bin/python   # a worktree has no venv of its own

# the control — no fusion flag at all, i.e. whatever the deployment is configured for
./opentr.sh bench rag --fresh rag403 --corpus qmsum --corpus synthetic \
    --control-name rrf-30-default --out /tmp/sweep403/rrf-30-default

# an explicit arm
./opentr.sh bench rag --fresh rag403 --corpus qmsum --corpus synthetic \
    --fusion normalization --normalization-technique z_score \
    --combination-technique arithmetic_mean \
    --control-name norm-zscore-arith --out /tmp/sweep403/norm-zscore-arith
```

`bench rag` forwards every unrecognised flag to the harness and derives the stack's ports from
`.fresh/rag403.offset`; running the script directly needs
`POSTGRES_PORT`/`OPENSEARCH_PORT`/`REDIS_PORT`/`MINIO_PORT` exported for the deployment plus
`DATA_DIR`/`TEMP_DIR` pointed somewhere writable (`Settings.__init__` otherwise tries to mkdir
`/app`). The measurements below used the `otfresh-rag403` deployment: **Postgres 5276,
OpenSearch 5280** (offset +100).

**Adding an eleventh arm is one flag combination and nothing else.** There is no pipeline to
pre-create, no cache to clear, and no restart: `ensure_fusion_pipeline` creates the arm's
pipeline on first use, `_verified_pipelines` is a **set** so one arm's verification cannot
certify another's, and the response cache keys on the resolved pipeline id so arm B cannot
replay arm A's page. The one real gotcha: **weights are encoded into the pipeline id as integer
percent, and anything needing more precision is refused rather than rounded** — `0.705,0.295`
exits with `Unusable --fusion configuration`, because two arms aliased onto one id means one of
them silently measured the other's pipeline.

#### The arms as data, not names

Pipeline ids are **derived from the parameters, never chosen**, which is what makes the set
regenerable from this table rather than decodable from the names. `transcript-hybrid-search` is
reserved for RRF at the configured `SEARCH_RRF_RANK_CONSTANT`, so the arm that is the shipped
default keeps the cluster's existing pipeline.

| arm | `--fusion` | `--rank-constant` | `--normalization-technique` | `--combination-technique` | `--combination-weights` | resolved pipeline id |
|---|---|---|---|---|---|---|
| `rrf-30-default` | *(no flag)* | — | — | — | — | `transcript-hybrid-search` |
| `rrf-30-explicit` | `rrf` | `30` | — | — | — | `transcript-hybrid-search` |
| `rrf-60` | `rrf` | `60` | — | — | — | `transcript-hybrid-search-rrf-60` |
| `norm-minmax-arith` | `normalization` | — | `min_max` | `arithmetic_mean` | — | `…-norm-min_max-arithmetic_mean` |
| `norm-minmax-geom` | `normalization` | — | `min_max` | `geometric_mean` | — | `…-norm-min_max-geometric_mean` |
| `norm-minmax-harm` | `normalization` | — | `min_max` | `harmonic_mean` | — | `…-norm-min_max-harmonic_mean` |
| `norm-l2-arith` | `normalization` | — | `l2` | `arithmetic_mean` | — | `…-norm-l2-arithmetic_mean` |
| `norm-zscore-arith` | `normalization` | — | `z_score` | `arithmetic_mean` | — | `…-norm-z_score-arithmetic_mean` |
| `norm-minmax-arith-w70-30` | `normalization` | — | `min_max` | `arithmetic_mean` | `0.7,0.3` | `…-norm-min_max-arithmetic_mean-w70_30` |
| `norm-minmax-arith-w30-70` | `normalization` | — | `min_max` | `arithmetic_mean` | `0.3,0.7` | `…-norm-min_max-arithmetic_mean-w30_70` |

Weights are **BM25 leg first, vector leg second** — the order the two subqueries appear in the
`hybrid` body (`chunk_retrieval._build_body`). `rrf-30-explicit` exists only as a control on the
flag itself: it must resolve to the same pipeline as the default and produce identical numbers,
which it does to every decimal place recorded.

#### The corpus state these numbers are true of

A retrieval number is a statement about a corpus **and** the model that vectorised it. All
twenty-one runs below were taken against one index, with no reindex and no write of any kind
between them:

| | value |
|---|---|
| index | `transcript_chunks`, `_meta.version` **6** |
| `indexing.total` | **825,795** — unchanged before, during and after the whole sweep |
| `docs.count` / `docs.deleted` | **210,908** / **0** |
| corpus | 432 files: 232 QMSum (Tier A, MIT) + 200 synthetic (`otsynth-core-v1`, seed 20260812) |
| chunks | 122,371 QMSum + 88,537 synthetic |
| queries | 1,651 scored, 0 dropped unjudgeable, 0 unanswered; mean 47.66 judged chunks/query |
| `embedding_dimension` | 384 |
| `embedding_verdict` | **`unattributed`** — all 210,908 documents carry the legacy `"neural"` bucket |
| `configured_embedding_model` | `huggingface/sentence-transformers/all-MiniLM-L6-v2` |
| metric engine | `pytrec_eval_terrier` 0.5.10, `ndcg_cut`, linear gain, `trec_eval -c` semantics |
| relevance policy | graded by gold word-share; high 0.5, low 0.0, not binary |

The embedding verdict is `unattributed` and must stay stated that way: the circumstantial
evidence is a 384-dimension index and a configured MiniLM, and **that is not the same claim as
"measured on all-MiniLM-L6-v2"**.

#### What was held constant

The value of an A/B is entirely in what did *not* vary, so it is enumerated rather than assumed:

- **The index.** No reindex, no mapping change, no document write. `indexing.total` was 825,795
  at the start and 825,795 at the end; a search pipeline is query-time metadata and touches no
  document.
- **The query set.** The same 1,651 queries, sorted by query id, from the same two injection
  manifests. Zero dropped as unjudgeable in every arm, so no arm scored a different denominator.
- **The qrels.** Same relevance policy (0.5 / 0.0, graded), same turn→chunk overlap rule, same
  78,694 judged documents.
- **The retrieval shape.** `--stage retrieve`, `--scope corpus`, `--search-mode hybrid`,
  `--size 48`, `--workers 4` — identical across every fusion arm. Only the search pipeline moved.
- **Tie-breaking.** The harness re-sorts every run by `(-score, doc_type, file_uuid,
  chunk_index)` before scoring, so `trec_eval`'s id-descending tie-break cannot reach a result.
  This was checked *for the new arms specifically*: normalization scores are dense floats in
  [0,1] and RRF scores are sums of `1/(k+rank)`, so the two families have very different tie
  structures — but the normalisation is applied to the run, not to the strategy, and the
  identical-arms control (`rrf-30-explicit` vs `rrf-30-default`, byte-identical rows) confirms
  the scoring path did not change underneath the sweep.
- **The metric engine and its version.**

### The fusion bake-off (#363), in full

`--stage retrieve --scope corpus`, corpus-wide — what chat actually does. Control is
`rrf-30-default`, the shipped `score-ranker-processor` at `rank_constant` 30.

| arm | qmsum `all` nDCG@10 | Δ | Δ% | synthetic `all` nDCG@10 | Δ | Δ% | verdict |
|---|---|---|---|---|---|---|---|
| `rrf-30-default` | 0.0983 | — | — | 0.2952 | — | — | **control** |
| `rrf-30-explicit` | 0.0983 | +0.0000 | +0.0% | 0.2952 | +0.0000 | +0.0% | identical to control |
| `rrf-60` | 0.0979 | −0.0003 | −0.4% | **0.3016** | **+0.0063** | **+2.1%** | split |
| `norm-minmax-arith` | **0.0990** | **+0.0008** | +0.8% | 0.2397 | −0.0555 | −18.8% | split |
| `norm-l2-arith` | **0.0993** | **+0.0010** | +1.0% | 0.2319 | −0.0633 | −21.4% | split |
| `norm-zscore-arith` | **0.0997** | **+0.0015** | +1.5% | 0.2265 | −0.0687 | −23.3% | split |
| `norm-minmax-arith-w70-30` | **0.0996** | **+0.0014** | +1.4% | 0.1503 | −0.1450 | −49.1% | split |
| `norm-minmax-arith-w30-70` | 0.0890 | −0.0093 | −9.5% | 0.2870 | −0.0082 | −2.8% | loss on both |
| `norm-minmax-geom` | 0.0831 | −0.0152 | −15.5% | 0.0968 | −0.1984 | −67.2% | loss on both |
| `norm-minmax-harm` | 0.0821 | −0.0161 | −16.4% | 0.0894 | −0.2059 | −69.7% | loss on both |

Per class, because D5 requires it and because the class breakdown is where the mechanism shows:

| arm | qmsum lookup | Δ | synthetic lookup | Δ | qmsum summarize | Δ | synthetic multi_file | Δ |
|---|---|---|---|---|---|---|---|---|
| `rrf-30-default` | 0.1038 | — | 0.3363 | — | 0.0821 | — | 0.2132 | — |
| `rrf-30-explicit` | 0.1038 | +0.0000 | 0.3363 | +0.0000 | 0.0821 | +0.0000 | 0.2132 | +0.0000 |
| `rrf-60` | 0.1037 | −0.0002 | 0.3439 | +0.0076 | 0.0812 | −0.0009 | 0.2169 | +0.0037 |
| `norm-minmax-arith` | 0.1041 | +0.0003 | 0.2889 | −0.0474 | 0.0843 | +0.0022 | 0.1414 | −0.0718 |
| `norm-l2-arith` | 0.1045 | +0.0006 | 0.2580 | −0.0782 | 0.0842 | +0.0022 | 0.1797 | −0.0335 |
| `norm-zscore-arith` | 0.1040 | +0.0002 | 0.2807 | −0.0555 | 0.0872 | +0.0051 | 0.1181 | −0.0951 |
| `norm-minmax-arith-w70-30` | 0.1052 | +0.0013 | 0.1692 | −0.1671 | 0.0836 | +0.0016 | 0.1125 | −0.1007 |
| `norm-minmax-arith-w30-70` | 0.0926 | −0.0112 | 0.3509 | +0.0146 | 0.0784 | −0.0037 | 0.1593 | −0.0540 |
| `norm-minmax-geom` | 0.0873 | −0.0165 | 0.1180 | −0.2182 | 0.0707 | −0.0114 | 0.0545 | −0.1587 |
| `norm-minmax-harm` | 0.0864 | −0.0174 | 0.1070 | −0.2292 | 0.0697 | −0.0124 | 0.0540 | −0.1592 |

**Zero arms of ten win on both corpora.** Three lose on both. The remaining five are splits, and
their splits are not close: the largest QMSum gain any arm achieves is **+0.0015 nDCG@10
(+1.5%)** while the same arm gives up **−0.0687 (−23.3%)** on synthetic — a loss **46× the size
of the win**. No significance test is needed to read that.

#### The OpenSearch BEIR result does not transfer to transcript retrieval

This is the finding #363 was opened to obtain. OpenSearch's own benchmark measured the
`normalization-processor` **3.86% higher nDCG@10 than RRF** across six BEIR datasets, and #363's
whole premise was that a public BEIR average is not evidence about *our* corpus. Measured:

- On QMSum the best normalization arm is **+1.5% relative**, under half the published figure.
- On the synthetic tier every normalization arm is **negative**, from −2.8% to −69.7%.
- The pooled effect is decisively negative.

The BEIR result is not wrong; it is about a different corpus shape. Recorded so nobody re-derives
the same expectation from the same blog post in a year.

#### Why geometric and harmonic mean collapse

`norm-minmax-geom` and `norm-minmax-harm` lose 15–70%, and the mechanism is structural rather
than a tuning miss. Both means are **zero if either input is zero**, and after per-leg
normalisation a document found by only *one* leg scores 0 on the other — so single-leg hits are
annihilated instead of ranked. RRF has the opposite property by construction: a single-leg hit
still scores `1/(k+rank)`. Hybrid retrieval over speaker-turn chunks is full of single-leg hits
(a 17-word turn matches BM25 or the vector, rarely both), which is why the collapse is this
large here and would be milder on long, keyword-rich documents. **Do not re-test these two on
this index expecting a different answer**; test them only if the chunking granularity changes.

#### The two corpora want opposite leg weights {/* #the-two-corpora-want-opposite-leg-weights */}

The weighted arms are the clearest demonstration of why the both-corpus rule exists:

- **BM25-heavy** (`w70_30`) is the *best* arm on QMSum lookup (0.1052, +0.0013) and the
  second-worst on synthetic lookup (0.1692, **−0.1671**).
- **Vector-heavy** (`w30_70`) is the *best* arm on synthetic lookup (0.3509, **+0.0146**) and
  clearly negative on QMSum lookup (0.0926, −0.0112).

Each would have been adopted by a single-corpus gate, and each would have been a significant
regression for the other half of the evaluation. The likely reason the corpora disagree — stated
as a hypothesis, not a measurement — is Stage 3's `embedding_text` result: synthetic queries
discriminate on the title and roster carried in the embedded header, so their answer lives in the
vector leg, while QMSum's conversational queries are literal-word matches that BM25 finds.

#### Latency: no arm is measurably cheaper or more expensive — and one run said otherwise

Phase 7 gates each A/B on **p95 added latency**, so `runinfo.json` now carries the per-query
retrieval cost (`retrieval_latency_ms`: samples, concurrency, p50/p95/p99/max/mean). It lives in
`runinfo.json` and not `metrics.json` on purpose: a duration cannot be byte-identical across
runs, and the results document's determinism is what makes an arm-to-arm difference attributable.

| run | arm | p50 (ms) | p95 (ms) | mean (ms) |
|---|---|---|---|---|
| 1 | `rrf-30-default` | 179.0 | 262.5 | 182.5 |
| 1 | `rrf-60` | 212.8 | **393.4** | 227.8 |
| 1 | `norm-minmax-arith` | 177.1 | 273.5 | 184.0 |
| 2 | `rrf-30-default` | 177.6 | 264.2 | 182.6 |
| 2 | `rrf-60` | 178.2 | **260.1** | 181.8 |
| 3 | `rrf-30-default` | 177.9 | 260.2 | 181.6 |
| 3 | `rrf-60` | 179.6 | **267.9** | 184.1 |

1,651 samples per run at concurrency 4, on a shared machine.

:::warning A single latency run manufactured a 50% regression that does not exist
Run 1 measured `rrf-60` at **+130.9 ms p95, +50%** against the control. A rank constant changes
no work — it is a divisor in a scoring formula — so the number was mechanistically implausible
and was re-measured *interleaved* with the control rather than believed. It did not reproduce:
across three passes `rrf-60` reads 393.4 / 260.1 / 267.9 ms p95 while the control reads 262.5 /
264.2 / 260.2. The honest conclusion is **no measurable latency difference between fusion
strategies at this corpus size**, with a run-to-run band of ±3% and at least one outlier run far
outside it.

These are a **comparable cost signal between arms measured under the harness's own worker pool,
not a user-facing latency figure** — which is why `concurrency` is recorded beside them.
:::

### Reproducibility of the sweep itself

Three separate checks, because an arm-to-arm difference is only attributable if the instrument
holds still:

1. **The control arm reproduces the committed baseline bit-for-bit.** `rrf-30-default`, run
   fresh in this window, matches `backend/tests/eval/baselines/stage4-control/metrics.json` with
   **max |Δ| = 0.0 across every row and every measure**. The only differences in the whole
   document are `control_name` and the new `retrieval.fusion` block.
2. **Three arms re-run under changed harness code reproduce their own first run exactly.**
   `rrf-30-default`, `rrf-60` and `norm-minmax-arith` were re-run after the latency
   instrumentation landed; all three came back with identical rows. Timing instrumentation
   cannot move a ranking, and now that is measured rather than assumed.
3. **The flag itself is inert.** `rrf-30-explicit` resolves to the same pipeline as the default
   and produces identical numbers, so every difference in the table is the pipeline and not the
   plumbing.

:::note Adding `retrieval.fusion` changes the bytes of any re-derived baseline
`metrics.json`'s `retrieval` block now always carries a `fusion` sub-block naming the resolved
strategy and pipeline id — including on runs that named no arm, where it records
`selected_explicitly: false`. Re-deriving one of the four re-derivable baselines will therefore
produce that one extra block and no other change; check (1) above is exactly that comparison,
performed deliberately. The alternative — omitting the block when the default is used — was
rejected for the reason `redaction/export_policy.py` argues in general: an absent value and a
default value must not look the same.
:::

### Reranker candidates — the licence gate came first

Every candidate was licence- and shippability-checked **before** any measurement, because a
candidate we cannot ship is not a candidate. Metadata was not treated as the licence: this page
already records four cases where dataset-hub metadata misrepresented the real terms.

| candidate | params | licence (metadata) | evidence checked | drop-in for `CrossEncoder`? | verdict |
|---|---|---|---|---|---|
| `cross-encoder/ms-marco-MiniLM-L6-v2` **(incumbent)** | 22.7 M | apache-2.0 | card front matter; no `LICENSE` file in repo | yes | **shippable — in use** |
| `BAAI/bge-reranker-base` | 278 M | mit | card front matter | yes — `XLMRobertaForSequenceClassification`, no `auto_map` | shippable |
| `BAAI/bge-reranker-v2-m3` | 568 M | apache-2.0 | card front matter | yes — same architecture | shippable |
| `mixedbread-ai/mxbai-rerank-base-v2` | 494 M | apache-2.0 | **`LICENSE` file present, full Apache-2.0 text** | **no** — `Qwen2ForCausalLM`; a generative/listwise reranker needing its own `mxbai-rerank` package | licence clean, not a cross-encoder |
| `jinaai/jina-reranker-v1-turbo-en` | 37.8 M | apache-2.0 | card front matter | **no** — `config.json` carries `auto_map`, i.e. `trust_remote_code=True` | **REJECTED — remote code execution** |
| `jinaai/jina-reranker-v2-base-multilingual` | — | **cc-by-nc-4.0** | card front matter | — | **REJECTED — non-commercial** |
| `Alibaba-NLP/gte-multilingual-reranker-base` | 306 M | apache-2.0 | card front matter | **no** — `auto_map`, `model_type: "new"`, i.e. `trust_remote_code=True` | **REJECTED — remote code execution** |
| Cohere Rerank / Voyage rerank | — | commercial API terms | — | — | **REJECTED — D6**: retrieval quality must not depend on an external service |

Three findings worth keeping:

- **The plan's own warning was too narrow.** #383 says *"Avoid `jina-reranker-v3` — CC BY-NC-4.0"*.
  `jina-reranker-v2-base-multilingual` is **also** CC BY-NC-4.0, and it is the one with a million
  monthly downloads. The rejection is the family, not one version.
- **`trust_remote_code` is a rejection on its own, independent of licence.** Two Apache-2.0
  candidates require executing arbitrary Python fetched from the Hub inside the backend
  container. `jina-reranker-v1-turbo-en` is otherwise the only candidate anywhere near the
  incumbent's cost (37.8 M vs 22.7 M parameters), which is precisely why the reason has to be
  written down — it will look attractive again.
- **The incumbent's *weights* are Apache-2.0; its *training data* is MS MARCO**
  (`datasets: ['sentence-transformers/msmarco']`), whose underlying terms this page already
  records as *non-commercial research only*. The restriction binds the dataset, not a model
  trained on it, and no change follows — but it is noted because this repo has been caught by MS
  MARCO's terms once already.

**No reranker was swapped, and none can be adopted on retrieval metrics alone.** The plan
requires a reranker A/B *per model tier* because reranking matters more for small models, and
that is an answer-quality question this harness cannot answer. The cheapest shippable
alternative, `bge-reranker-base`, is **12× the incumbent's parameter count** on a CPU-only,
in-request code path — so the burden of proof is on the candidate, and the measurement that
could discharge it does not exist yet. Recorded as deferred, with the reason, rather than
attempted and reported inconclusively.

### A fourth way this harness measured the wrong thing — found by the budget sweep

The `rerank` stage claims to measure *"what actually reaches the prompt"*. It did not, and the
48/12/4 sweep is what exposed it. Recorded at length beside the [three earlier
cases](#three-ways-this-harness-measured-the-wrong-index) because, like all of them, it produced
no error — only a plausible number.

**The symptom.** `nDCG@5` moved when `--final-chunks` changed. That is impossible if the metric
sees the prompt's order: `diversity_sample` builds its list by round-robin and returns early on
`cap`, so its output is **prefix-invariant in `cap`** — the first five documents are the same for
8, 12 and 20. (Asserted directly over 200 random inputs, because this reasoning had already been
wrong once in this investigation.)

**The three candidate explanations, and how each was eliminated.**

1. *Nondeterminism in the cross-encoder* (plausible: eight worker threads, CPU float reduction
   order). **Eliminated by measurement** — three consecutive runs of `budget-48-12-4` produced
   **bit-identical** rows.
2. *`diversity_sample` is not prefix-invariant.* **Eliminated** by the 200-input property check
   above.
3. *The harness re-sorts the list before scoring.* **Confirmed.**

**The cause.** `normalise_run` re-sorts every run by `-score`. That is correct for `retrieve` —
OpenSearch already returns score order, so the re-sort only makes the tie-break blind to document
*names* (#32) — and **wrong** after `diversity_sample`, whose entire purpose is to interleave
files so one long recording cannot crowd out the rest. Re-sorting by score undoes the
interleaving, so the metric ranked a list the model is never given. Measured over 60 synthetic
queries:

| | count |
|---|---|
| queries examined | 60 |
| queries whose **prompt order was changed** by the re-sort | **40** |
| queries whose **scored top-5 depended on `final_chunks`** | **23** |

**A second defect travelled with it.** `rerank` writes the cross-encoder score back onto its
first `rerank_max_pairs` hits and leaves the tail carrying RRF scores. Cross-encoder scores are
routinely **negative** (measured on this corpus: −4.35 to −11.31 for a typical query) while RRF
scores are small positives bounded by `2/(k+1)` = 0.0645. So with
`candidate_pool > rerank_max_pairs` a score sort floats the **un-reranked tail above every
reranked document**. That is precisely the `96/12/4` arm.

:::note Production never had this bug
`diversity_sample` walks **list order**, and `retrieval.py` passes it `rerank`'s output directly,
so what reaches a real prompt was always correct. This was the instrument reading a ranking out
of two incomparable score scales — which is why it could sit there producing numbers.
:::

**The fix.** `_to_run_docs(hits, preserve_order=True)` derives the score from the hit's
**position**, so for the rerank stage the metric ranks exactly what the prompt receives and no
tie can occur. The `retrieve` stage is untouched and keeps the score-based tie-break, so **every
fusion number above is unaffected** — that is not an assumption, it is a different branch, and
the control arm was re-run after the change to confirm it. `test_eval_runner.py` carries the
guard, red without the fix.

**Everything measured on the pre-fix instrument was discarded**, not adjusted. The budget numbers
below are from a complete re-run.

### The speaker-turn chunking decision, recorded

#363's second half is not a measurement at all — it is a decision that exists to stop a future
reader "fixing" something deliberate. It is recorded here because an issue gets closed and a
methodology page does not.

**SeCom (Pan et al., ICLR 2025, [arXiv:2502.05589](https://arxiv.org/abs/2502.05589)) measured
that turn-level memory units are suboptimal for retrieval**, with topic-coherent segment-level
units winning. `chunk_transcript_by_speaker_turns`
(`backend/app/services/search/chunking_service.py`) is turn-level, so on a generic retrieval
benchmark it is the weaker choice — and this page's own numbers are consistent with that: QMSum
chunks average **17 words**, which is a large part of why corpus-wide nDCG@10 sits at 0.098.

**It stays, deliberately.** Speaker-scoped retrieval — *"what did Dana say about pricing"* — is
structurally dependent on the invariant **one chunk = one speaker turn**, because that is what
makes the `speaker` keyword filter in `_build_filters` an exact `terms` match rather than an
approximation. Topic-coherent segments spanning multiple speakers would make that filter fuzzy,
trading a capability no general-purpose RAG tool has for a small generic-benchmark gain.

**Accepted trade-off: slightly lower generic retrieval scores in exchange for exact speaker
attribution.** The measured consequence is visible above and is not hidden. If this is ever
revisited, the alternative worth evaluating is a **second** chunking granularity indexed
alongside the existing one — topic segments for broad questions, speaker turns for attribution —
never a replacement. Note that `doc_type` (D1) already makes a second plane in one index a
solved shape, and the digest plane is a working precedent for it.

### The 48/12/4 budget sweep

`candidate_pool` / `final_chunks` / `max_chunks_per_file` are **48 / 12 / 4**
(`core/constants.DEFAULT_CHAT_RAG_*`), chosen by judgement and never measured. `--stage rerank`
now measures them at the shipped values — it used to default to **20/3**, so every rerank number
ever taken described a deployment nobody runs.

```bash
# the shipped centre point, full corpus
./opentr.sh bench rag --fresh rag403 --corpus qmsum --corpus synthetic \
    --stage rerank --workers 8 --control-name full-48-12-4 --out /tmp/sweep403/full/full-48-12-4
# an arm: only the flag changes
./opentr.sh bench rag --fresh rag403 --corpus qmsum --corpus synthetic \
    --stage rerank --workers 8 --size 12 --control-name full-12-12-4 --out /tmp/sweep403/full/full-12-12-4
```

`--stage rerank` needs the cross-encoder weights (`cross-encoder/ms-marco-MiniLM-L-6-v2`) and
**raises rather than silently skipping** if they are missing. Running it from the host venv needs
`SENTENCE_TRANSFORMERS_HOME` pointed at `models/sentence-transformers` and `HF_HUB_OFFLINE=1`;
that venv's `torchcodec` is built against a different torch ABI, so `sentence_transformers` will
not import without a stub package on `PYTHONPATH` (the container has a working ffmpeg and needs
none). Verified equivalent before use: the host and the `otfresh-rag403-backend` container return
**identical** cross-encoder scores (6.845277 / −11.305734) for the same pair.

#### Two of the three knobs cannot be chosen by a ranking metric, and the numbers show it

| arm | qmsum nDCG@5 | Δ | qmsum nDCG@10 | Δ | synth nDCG@5 | Δ | synth nDCG@10 | Δ |
|---|---|---|---|---|---|---|---|---|
| `48/12/4` **(shipped)** | 0.0509 | — | 0.0366 | — | 0.1416 | — | 0.1605 | — |
| `48/12/4` repeat | 0.0509 | +0.0000 | 0.0366 | +0.0000 | 0.1416 | +0.0000 | 0.1605 | +0.0000 |
| `48/**20**/4` | 0.0509 | +0.0000 | 0.0366 | +0.0000 | 0.1416 | +0.0000 | 0.1605 | +0.0000 |
| `48/**8**/4` | 0.0509 | +0.0000 | 0.0353 | −0.0013 | 0.1416 | +0.0000 | 0.1577 | −0.0028 |
| `48/12/**2**` | 0.0509 | +0.0000 | 0.0366 | +0.0000 | 0.1416 | +0.0000 | 0.1605 | +0.0000 |
| `48/12/**8**` | 0.0509 | +0.0000 | 0.0366 | +0.0000 | 0.1416 | +0.0000 | 0.1605 | +0.0000 |

(475-query subset — 400 QMSum + all 75 synthetic — since these arms differ only after retrieval.)

**`final_chunks` and `max_chunks_per_file` are inert on the metric, and that is correct rather
than suspicious.** `diversity_sample` is prefix-invariant in `cap`, so raising `final_chunks` can
only *append*; `48/8/4`'s −0.0013 / −0.0028 at nDCG@10 is purely mechanical — a list of eight
cannot fill ranks nine and ten. `max_chunks_per_file` 2 vs 4 vs 8 moves nothing because the
per-file ceiling almost never binds within twelve chunks drawn from a 432-file corpus.

So **a ranking metric cannot choose these two knobs.** What they actually trade — prompt budget,
per-file coverage, and how much irrelevant material a model is asked to read — is the
answer-quality axis this harness does not have. Recording that is more useful than a table of
zeros: it says which question to stop asking of nDCG. (This page's [replaced
metric](#a-metric-we-replaced) makes the same point from
the other direction — coverage needed a claim-verification measure, not a rank.)

#### `candidate_pool` IS measurable, and smaller is better all the way down

Full corpus, 1,651 queries, `final_chunks` 12 / `max_chunks_per_file` 4 / `rerank_max_pairs` 50
held constant. nDCG@10, Δ against the shipped pool of 48:

| pool | qmsum lookup | qmsum summarize | qmsum all | synth lookup | synth multi_file | synth all | retrieval p50 | wall clock |
|---|---|---|---|---|---|---|---|---|
| **12** | 0.0934 (+0.0125) | 0.0712 (+0.0137) | **0.0877 (+0.0128)** | 0.2436 (+0.0180) | 0.0969 (+0.0667) | **0.1947 (+0.0342)** | 204.4 ms | 215 s |
| 24 | 0.0844 (+0.0036) | 0.0613 (+0.0038) | 0.0785 (+0.0036) | 0.2212 (**−0.0044**) | 0.0673 (+0.0371) | 0.1699 (+0.0095) | 233.9 ms | 464 s |
| 32 | 0.0820 (+0.0012) | 0.0593 (+0.0018) | 0.0762 (+0.0013) | 0.2347 (+0.0090) | 0.0632 (+0.0330) | 0.1775 (+0.0170) | 250.1 ms | 621 s |
| **48 (shipped)** | 0.0808 | 0.0575 | 0.0748 | 0.2256 | 0.0302 | 0.1605 | 261.6 ms | 707 s |
| 96 | 0.0803 (**−0.0006**) | 0.0556 (**−0.0019**) | 0.0740 (**−0.0009**) | 0.2366 (+0.0110) | 0.0354 (+0.0052) | 0.1695 (+0.0091) | 295.5 ms | 719 s |

Two further arms, both losers, recorded rather than dropped: `96/12/4` with
`--rerank-max-pairs 96` — i.e. reranking the *whole* enlarged pool — scores **below** `96/12/4`
at max_pairs 50 (synthetic nDCG@5 0.1476 vs 0.1542) while costing 31% more wall clock. Reranking
more candidates makes it worse.

:::danger A quarter of the query set flipped the sign
On the 475-query subset, pool 96 read **+0.0004** nDCG@10 on QMSum. On all 1,651 queries it reads
**−0.0009**. Same instrument, same index, same arm — 400 of 1,576 QMSum queries were enough to
invert the conclusion, and the subset's version would have been reported as a both-corpus win.
Every conclusion above is from the full query set for exactly this reason.
:::

:::warning `--size` is not a truncation knob — it changes the ranking
Retrieval at `--size 12` and `--size 48` do **not** share a top ten: nDCG@10 is 0.0942 vs 0.0983
on QMSum. `dynamic_rrf_window(size) = max(100, min(size*4, 500))`, so the request size sets the
depth the two legs are *fused* over — 100 at size 12, 192 at size 48 — and a different fusion
window is a different ranking. This was assumed to be a pure truncation and checked; the check is
why the sentence above is right. Never compare two `--size` values as though one were a prefix of
the other.
:::

#### The finding under the pool sweep: the cross-encoder is net-harmful on this corpus

The monotone trend has an obvious candidate explanation — a larger pool is precisely more
material the cross-encoder is allowed to *promote* into the final twelve — so it was measured
against a same-length control: `--stage retrieve --size 12`, retrieval's own top twelve with no
reranking and no diversity sampling.

| pipeline | qmsum nDCG@10 | Δ vs no-rerank | synth nDCG@10 | Δ vs no-rerank |
|---|---|---|---|---|
| **no rerank** (retrieval top-12) | **0.0942** | — | **0.2385** | — |
| rerank, pool 12 | 0.0877 | −0.0065 (−6.9%) | 0.1947 | −0.0438 (−18.4%) |
| rerank, pool 24 | 0.0785 | −0.0157 (−16.7%) | 0.1699 | −0.0685 (−28.7%) |
| **rerank, pool 48 (SHIPPED)** | 0.0748 | **−0.0194 (−20.6%)** | 0.1605 | **−0.0780 (−32.7%)** |
| rerank, pool 96 | 0.0740 | −0.0203 (−21.5%) | 0.1695 | −0.0689 (−28.9%) |

#383 predicted the shape and understated the size: *"off-the-shelf cross-encoders have been
observed degrading nDCG 0.3–3.1% ... on corpora unlike their training distribution."* Measured
here it is **20.6% and 32.7%**, an order of magnitude larger.

**What this does and does not establish**, because the difference decides what to do next:

- **The cross-encoder's *selection* is harmful, and that part is isolated.** Across the pool
  arms `final_chunks`, `max_chunks_per_file` and `diversity_sample` are all constant; the only
  thing that varies is how much material the cross-encoder may promote from. More promotion
  power, monotonically worse.
- **The −20.6% / −32.7% against the no-rerank control is NOT purely the reranker.** That control
  has no `diversity_sample` either, and diversity sampling deliberately trades rank quality for
  per-file coverage — nDCG scores it as a loss by construction. Separating the two needs a
  "diversity, no rerank" arm the harness does not have.
- **Therefore: do not read this as "turn off diversity sampling."** Coverage is the thing nDCG
  provably cannot see; that is the lesson of [the metric we
  replaced](#a-metric-we-replaced).

This is the largest single number Stage 5 produced and it deserves its own issue with a proper
decomposition, not a constant edited at the end of a sweep.

### Nothing was adopted, and here is the rule that rejected each candidate

Twenty-four arms. **Zero pass the gate**: a win on both corpora *and* no regression in the lookup
class, on the reported measures.

| candidate | why it looked adoptable | why it was rejected |
|---|---|---|
| `rrf-60` | +2.1% nDCG@10 on synthetic | QMSum `all` −0.4% and QMSum lookup −0.0002: a split, not a win |
| `norm-z_score-arithmetic_mean` | best QMSum arm, +1.5% | synthetic −23.3%, a loss **16× the win** |
| `norm-min_max-arithmetic_mean` w70/30 | best QMSum lookup of any arm | synthetic lookup −0.1671 |
| `norm-min_max-arithmetic_mean` w30/70 | best synthetic lookup of any arm | QMSum lookup −0.0112 |
| `candidate_pool` 96 | won on both corpora on the 475-query subset | full corpus flipped QMSum to −0.0009, and QMSum lookup to −0.0006 |
| `candidate_pool` 24 | +0.0036 QMSum / +0.0095 synthetic nDCG@10 | **synthetic lookup −0.0044** — the class D5 says must never regress |
| **`candidate_pool` 12** | **the strongest arm measured**: +0.0128 QMSum / +0.0342 synthetic nDCG@10, every class up, and 22% lower p50 latency | **synthetic lookup nDCG@5 −0.0155 and MRR −0.0178** — it wins at depth 10–20 and loses at the very top of the ranking, on the class that must not regress |
| `final_chunks`, `max_chunks_per_file` | — | provably inert on a ranking metric; the axis they trade is unmeasured |
| every reranker candidate | two are licence- and architecture-clean | adoption needs the per-model-tier answer-quality axis this harness does not have |

**`candidate_pool = 12` is the one to look at next**, and it is a single constant
(`DEFAULT_CHAT_RAG_CANDIDATE_POOL`). It was deliberately **not** changed here for three reasons:
its nDCG@5/MRR regression on synthetic lookup fails the stated gate; its mechanism indicts the
*reranker* rather than the pool, so shrinking the pool treats the symptom; and a smaller
candidate pool reduces the material available to map-reduce and multi-file answers, which is
coverage — the thing this harness cannot score.

### #363's checkboxes, with the numbers that close them

| checkbox | status | evidence |
|---|---|---|
| Build a retrieval-evaluation set from real OpenTranscribe content, with graded judgements including speaker-scoped queries | **closed** | 1,651 scored queries over 432 files — 1,576 QMSum human queries (Tier A, MIT) + 75 synthetic; 78,694 graded judgements, mean 47.66 per query, 0 dropped unjudgeable |
| Measure RRF (`rank_constant` 30 **and 60**) against `normalization-processor` variants | **closed** | ten arms, both rank constants, all three normalization techniques × all three combination techniques on `min_max`, plus two weightings. Full table above |
| Only then decide whether to change the default, and record the numbers | **closed — the default does NOT change** | zero of ten arms win on both corpora. Best case +1.5% on QMSum against −23.3% on synthetic |
| Reuse the same harness to validate #362's Phase 11 options | **open — belongs to #362** | the reuse seam is `--fusion` plus the `retrieval.fusion` provenance block; Phase 11's granite tiers and the neural-sparse leg are additive options that plug into the same arms |
| Record the speaker-turn chunking decision | **closed** | [above](#the-speaker-turn-chunking-decision-recorded) |

### Synonyms — not measurable without a reindex, and that is a fact about the feature

A `synonym_graph` filter is an **analyzer** change. Analyzers live in index settings, so adopting
one means a mapping change and a full reindex — which this sweep is forbidden from doing and,
more importantly, which takes it out of the class of change Stage 5 exists for. #383 says so
itself: *"A synonym filter change is an analyzer change, so it needs the reindex path; fold it
into Phase 3's single bump if it wins early, otherwise it is its own bump."* Phase 3's bump has
already shipped (index v6).

So the honest status is: **unmeasured, with a named blocker**, not "tried and rejected". The
prerequisite is a domain vocabulary to put in the filter, and this corpus does not have one —
QMSum is a remote-control design scenario and the synthetic tier's jargon is generated. Testing
synonym expansion against a corpus with no real domain vocabulary would measure the generator.

## Do the two corpora agree? {/* #corpus-agreement */}

Every run on this page scores QMSum and the synthetic tier in the same pass, so both numbers
have always been present. What was never produced is the **statement about whether they
agree** — and the whole both-corpora gate rests on the answer. If the two corpora rank changes
the same way, the gate is a redundancy check and a single-corpus result is nearly as good. If
they do not, then **a tuning decision taken on one corpus is not evidence**, and several
decisions on this page would rest on nothing.

They do not agree. On the class the gate protects, they agree **less than a coin flip would**.

### The two rules that make the comparison legitimate

**Absolute nDCG is never compared.** QMSum's control is 0.0983 and the synthetic tier's is
0.2952. That 3× gap is a property of the corpora — 17-word QMSum speaker turns against
generated meetings carrying a title-and-roster header in the embedded text — and it says
nothing about any arm. Every arm is therefore reduced to a **delta against its own family's
control**, and only that delta's **sign** and **rank** are used.

**An arm is only compared to arms sharing its control, stage and query set.** The fusion arms
are `--stage retrieve` over 1,651 queries; the budget arms are `--stage rerank` over the
475-query subset; the pool arms are `--stage rerank` over the full set. Per-family coefficients
are the result; the pooled one is a summary and is reported with that caveat rather than
instead of them.

### Method — how to reproduce it, or redo it with a 25th arm

```bash
# the 24 arms already measured, no re-run — reads /tmp/sweep403/*/metrics.json
scripts/rag_corpus_agreement.py
scripts/rag_corpus_agreement.py --class lookup              # the class D5 protects
scripts/rag_corpus_agreement.py --drop norm-minmax-geom --drop norm-minmax-harm
scripts/rag_corpus_agreement.py --exclude-inert             # tie-artefact sensitivity
scripts/rag_corpus_agreement.py --json                      # machine-readable
```

**No benchmark was re-run for this analysis and no index was touched.** It reads the
`metrics.json` files the Stage 5 sweep already wrote; `indexing.total` was **825,795** before
and after, `docs.count` 210,908, `docs.deleted` 0, `_meta.version` 6 — the same numbers the
sweep reports, because reading a results file cannot move them.

Adding a 25th arm needs **no code edit**: `--emit-manifest` prints the built-in arm table as
JSON, add the new arm's `{name, run, axis}` under the family whose control it was measured
against, and pass it back with `--manifest`. The only requirement on the new run is the one
`benchmark_rag.py` already meets — a `metrics.json` with `rows[].corpus` /
`rows[].query_class` / `rows[].metrics`.

**Kendall's tau-b is the headline coefficient**, and the choice is forced rather than
stylistic:

- The arm set contains **exact ties**. Four budget arms move nothing on either corpus, because
  `diversity_sample` is prefix-invariant in `cap`. tau-b has a defined tie correction; Spearman's
  midrank merely does not crash. Spearman is printed beside it and agrees throughout.
- **Pearson is printed to be distrusted, not used.** Pooled it reads **+0.697 (p = 0.0004)**,
  which looks like strong agreement. Drop the two arms that collapse for a structural reason
  (`geometric_mean` / `harmonic_mean` annihilate single-leg hits) and it falls to **+0.115
  (p = 0.64)**, while tau-b moves from +0.301 to +0.131. A correlation carried by two outliers
  is not a finding about the other nineteen arms.
- Rank measures are invariant to any monotone per-corpus transform, so Δ and Δ% give identical
  coefficients. That is asserted in `backend/tests/eval/test_corpus_agreement.py`, not assumed.

Arithmetic and edge cases are pinned by that test module (20 tests; tau-b reads ±1 on the
extremes, a constant corpus returns *undefined* rather than 0.0, inert arms are counted
separately from agreement). `scipy` — **BSD-3-Clause**, verified from the installed
distribution's own metadata — is the only dependency, and it is not a new one: it is a hard
requirement of `sentence-transformers`, so it is already present in `requirements.txt`,
`requirements-ci.txt` and the eval venv. It is **not** the licence-restricted case
`requirements-eval.txt` documents for `pytrec_eval_terrier`.

### The per-arm result — 21 arms, `all` class, nDCG@10

Δ is against each arm's own family control. `inert` means the arm moved **nothing** on either
corpus and is counted separately: two zeros are a metric that cannot see the knob, not two
corpora concurring.

| family | arm | axis | Δ qmsum | Δ synthetic | |
|---|---|---|---|---|---|
| fusion | `rrf-30-explicit` | flag-inertness control | +0.0000 | +0.0000 | inert |
| fusion | `rrf-60` | rank_constant | −0.0003 | +0.0063 | **disagree** |
| fusion | `norm-minmax-arith` | normalization | +0.0008 | −0.0555 | **disagree** |
| fusion | `norm-l2-arith` | normalization | +0.0010 | −0.0633 | **disagree** |
| fusion | `norm-zscore-arith` | normalization | +0.0015 | −0.0687 | **disagree** |
| fusion | `norm-minmax-geom` | combination | −0.0152 | −0.1984 | agree |
| fusion | `norm-minmax-harm` | combination | −0.0161 | −0.2059 | agree |
| fusion | `norm-minmax-arith-w70-30` | weighting | +0.0014 | −0.1450 | **disagree** |
| fusion | `norm-minmax-arith-w30-70` | weighting | −0.0093 | −0.0082 | agree |
| budget | `budget-48-12-4-repeat` | repeatability control | +0.0000 | +0.0000 | inert |
| budget | `budget-48-20-4` | final_chunks | +0.0000 | +0.0000 | inert |
| budget | `budget-48-08-4` | final_chunks | −0.0013 | −0.0028 | agree |
| budget | `budget-48-12-2` | max_chunks_per_file | +0.0000 | +0.0000 | inert |
| budget | `budget-48-12-8` | max_chunks_per_file | +0.0000 | +0.0000 | inert |
| budget | `budget-24-12-4` | candidate_pool | +0.0009 | +0.0095 | agree |
| budget | `budget-96-12-4` | candidate_pool | +0.0004 | +0.0091 | agree |
| budget | `budget-96-12-4-pairs96` | rerank_max_pairs | +0.0007 | +0.0057 | agree |
| pool | `pool-12` | candidate_pool | +0.0128 | +0.0342 | agree |
| pool | `pool-24` | candidate_pool | +0.0036 | +0.0095 | agree |
| pool | `pool-32` | candidate_pool | +0.0013 | +0.0170 | agree |
| pool | `pool-96` | candidate_pool | −0.0009 | +0.0091 | **disagree** |

(21 rows, not 24: the three family controls are Δ = 0 against themselves by construction.)

**Sign agreement, `all` class: 10 agree, 6 disagree, 5 inert — 10 of the 16 arms that moved
on both corpora, 62.5%.** A fair coin gives 50%.

**Sign agreement, `lookup` class: 6 agree, 9 disagree, 5 inert, 1 one-sided — 6 of 15, 40.0%.**
On the class D5 says must never regress, the corpora are **more often opposed than aligned**.

### The coefficients

`all` class, nDCG@10, Δ vs family control:

| set | n | Kendall tau-b | p | Spearman rho | p |
|---|---|---|---|---|---|
| fusion | 9 | **+0.000** | 1.000 | +0.133 | 0.732 |
| budget | 8 | +0.909 | 0.004 | +0.973 | 0.00005 |
| pool | 4 | +0.667 | 0.333 | +0.800 | 0.200 |
| **pooled** | **21** | **+0.301** | **0.066** | +0.378 | 0.091 |

`lookup` class — the same arms, the class the gate protects:

| set | n | Kendall tau-b | p | Spearman rho | p |
|---|---|---|---|---|---|
| fusion | 9 | −0.111 | 0.761 | +0.083 | 0.831 |
| budget | 8 | +0.201 | 0.540 | +0.116 | 0.784 |
| pool | 4 | +0.000 | 1.000 | +0.200 | 0.800 |
| **pooled** | **21** | **−0.005** | **0.975** | +0.056 | 0.809 |

**tau-b = −0.005 on `lookup` is not weak agreement; it is the absence of any relationship.**
Knowing what an arm did to QMSum lookup tells you nothing whatever about what it did to
synthetic lookup.

Two artefacts have to be read off before either table is quoted:

- **The budget family's +0.909 is substantially a tie artefact.** Half its arms are exact zeros
  on both corpora, and a zero pairs concordantly with almost everything. `--exclude-inert`
  drops it to **+0.667 at p = 0.333** (n = 4) — the difference between "significant agreement"
  and "four arms and no evidence". On `lookup`, excluding inert arms takes *every* family to
  tau-b = 0.000.
- **Excluding `geometric_mean` / `harmonic_mean` flips the fusion result from null to
  significantly negative** (next section). Those two are not a tuning choice that lost; they
  collapse structurally, and both corpora notice, which is why they are the only reason the
  fusion coefficient reaches zero rather than going negative.

### Where they disagree: the fusion axis, and it is ANTI-correlated

`geometric_mean` and `harmonic_mean` are zero if either leg is zero, so a single-leg hit is
annihilated rather than ranked — a structural collapse both corpora agree about, and the only
concordant pair in the family. Removing those two leaves the seven arms that are genuine
tuning candidates:

| set (fusion, geom/harm removed) | n | Kendall tau-b | p | Spearman rho | p |
|---|---|---|---|---|---|
| `all` class | 7 | **−0.714** | **0.030** | −0.857 | 0.014 |
| `lookup` class | 7 | **−0.905** | **0.003** | −0.964 | 0.001 |

**Among the fusion arms anyone would actually consider adopting, improving QMSum predicts
harming the synthetic tier, almost monotonically, and the relationship is statistically
significant at n = 7.** This is much stronger than "the corpora sometimes disagree": on this
axis one corpus's ranking is close to the *reverse* of the other's.

Per axis, `all` / `lookup`:

| axis | arms | agree | disagree | reading |
|---|---|---|---|---|
| `normalization` (min_max / l2 / z_score) | 3 | 0 / 0 | **3 / 3** | **total disagreement, both classes.** Every normalization arm gains on QMSum and loses on synthetic |
| `weighting` (BM25/vector split) | 2 | 1 / 0 | 1 / **2** | **the worst axis on `lookup`**: `w70_30` is QMSum lookup's best arm and synthetic lookup's second-worst; `w30_70` is the exact mirror |
| `rank_constant` (RRF 30 vs 60) | 1 | 0 / 0 | 1 / 1 | the one arm disagrees on both classes |
| `candidate_pool` | 6 | 5 / 3 | 1 / **3** | agrees on `all`, **splits evenly on `lookup`** |
| `combination` (geom / harm) | 2 | 2 / 2 | 0 / 0 | agrees — and both arms are structural collapses, not tuning |
| `final_chunks`, `max_chunks_per_file`, `rerank_max_pairs` | 5 | 2 / 1 | 0 / 0 | three of the five are inert; a ranking metric cannot see these knobs at all |

The Stage 5 write-up already named the weighting axis as the worst offender. **Confirmed on
`lookup`, and extended: `normalization` is just as bad and has three arms rather than two.**
The `all` class understates weighting because `w30_70` loses on both corpora at corpus level
while *winning* synthetic lookup.

### Would the two corpora pick the same winner?

| family | QMSum picks | synthetic picks | |
|---|---|---|---|
| fusion, `all` | `norm-zscore-arith` (+0.0015) | `rrf-60` (+0.0063) | **different** |
| fusion, `lookup` | `norm-minmax-arith-w70-30` (+0.0013) | `norm-minmax-arith-w30-70` (+0.0146) | **different — and opposite** |
| budget, `all` | `budget-24-12-4` (+0.0009) | `budget-24-12-4` (+0.0095) | same |
| budget, `lookup` | `budget-24-12-4` (+0.0012) | `budget-96-12-4` (+0.0110) | **different** |
| pool, `all` | `pool-12` (+0.0128) | `pool-12` (+0.0342) | same |
| pool, `lookup` | `pool-12` (+0.0125) | `pool-12` (+0.0180) | same |

**The fusion axis picks a different winner on both classes, and on `lookup` the two winners
are the two halves of the same knob turned opposite ways.** The pool axis picks the same winner
every time — which is the one place a single-corpus result would have been safe, and it is
also the axis where Stage 5's strongest candidate (`candidate_pool` 12) sits.

### One more asymmetry: QMSum barely moves on the fusion axis

| family | QMSum Δ range | synthetic Δ range | synthetic / QMSum |
|---|---|---|---|
| fusion | 0.0176 | 0.2122 | **12.1×** |
| budget | 0.0022 | 0.0123 | 5.6× |
| pool | 0.0137 | 0.0251 | 1.8× |

The entire QMSum fusion spread is 0.0176 nDCG@10, and the "wins" inside it are 0.0008–0.0015 —
around 1% relative on a control of 0.0983. So the anti-correlation above is a near-reversal of
a ranking whose QMSum side has almost **no dynamic range**. That cuts in an uncomfortable
direction rather than a convenient one: it does not rescue the synthetic tier, it says the
QMSum side of a fusion A/B is a weak signal being read as a strong one. Both readings end at
the same rule.

### The consequence: the both-corpora gate is TIGHTENED, not relaxed

The Stage 5 gate — *a win must hold on **both corpora**, with the lookup class never regressing
on either* — is now measured rather than asserted, and this analysis **supports and tightens
it**:

1. **A single-corpus tuning result is not evidence, and that is now a number.** Kendall tau-b
   = +0.301 (p = 0.066, n = 21) pooled, and **−0.005 (p = 0.975) on the lookup class**. Where
   the corpora were most likely to be used as a shortcut — the fusion axis — the coefficient is
   **−0.714 / −0.905** among genuine candidates. "It won on QMSum" and "it won on synthetic" are
   not weak evidence of each other; on that axis they are mild evidence *against* each other.
2. **Report the lookup class separately, always.** Corpus-level agreement (62.5% signs, tau-b
   +0.301) is meaningfully better than lookup-class agreement (40.0%, tau-b −0.005). Quoting
   only the `all` row would overstate agreement on the exact class the gate protects.
3. **Do not treat "both corpora agreed" as strong on its own when the arms are inert.** Four
   budget arms agree at exactly zero. `--exclude-inert` before quoting a family coefficient.
4. **Neither corpus is the arbiter.** Nothing here says synthetic is right and QMSum is wrong,
   or the reverse. The likely mechanism is Stage 3's `embedding_text` result — synthetic queries
   discriminate on the embedded title/roster header, so their answer lives in the vector leg,
   while QMSum's conversational queries are literal-word matches BM25 finds. Both are real
   retrieval regimes; a deployment has both kinds of user. **An arm that helps one and hurts the
   other is a trade-off to be decided deliberately, not a win.**
5. **A third corpus would be worth more than a 25th arm.** Two corpora that disagree can only
   veto; they cannot adjudicate. This is the concrete argument for the additional Tier A
   English meeting-retrieval judgements [this page lists as
   missing](#what-we-cannot-currently-claim).

### What this analysis cannot claim

- **The Stage 5 21-arm analysis above still has no per-query significance test — but that is now
  a baseline-freshness gap, not a structural one.** `report.build_retrieval_per_query` landed at
  `8117e6f3`, and `metrics.json` has carried a `retrieval_per_query` array (one row per
  retrieval-scored query, per measure) ever since — see [Paired
  significance](#paired-significance) below for the method it enables. The Stage 5 arms
  themselves were measured **before** that commit, and `baselines/README.md` marks their results
  historical or needing the live `rag403` stack to re-derive, so back-filling per-query rows for
  those specific 21 arms is a regeneration, not a maths problem. **It is wrong to read "zero
  `retrieval_per_query` rows" as evidence of a pre-schema-v2 baseline** either: six of the eight
  non-MIRACL baselines under `tests/eval/baselines/` report `schema_version: 2` and still carry
  no per-query rows, because schema v2 predates `8117e6f3` by itself — the field is keyed to that
  commit, not to the results-schema version. `tests/eval/test_eval_report.py`'s baseline-integrity
  sweep enforces this distinction with an explicit, reasoned allowlist per baseline (never a
  blanket schema-version skip), so a baseline that regresses silently to missing per-query rows
  fails a test instead of being read as historical by assumption.
- **n is small and the p-values are fragile.** 21 arms pooled, 4–9 per family. The pooled
  `all`-class coefficient (p = 0.066) is not significant at α = 0.05, and the pool family's
  +0.667 at n = 4 is not evidence of anything on its own. Only the fusion anti-correlation
  (p = 0.030 / 0.003) and the tie-inflated budget figure clear α = 0.05.
- **The pooled coefficient mixes incomparable families.** Different stages, different query
  sets, and `budget-24-12-4` / `budget-96-12-4` share their synthetic measurement with
  `pool-24` / `pool-96` (all 75 synthetic queries are in both sets, so those two synthetic
  deltas are literally the same number twice). Dropping the two duplicates moves pooled tau-b
  from +0.301 to +0.242 — the caveat is real but it is not what produces the result.
- **This is agreement about *retrieval ranking*, nothing else.** No LLM was involved (D6), so
  it says nothing about whether the two corpora would agree about answer quality — the axis on
  which the reranker and `final_chunks` decisions actually turn.

### Paired significance

`tests/eval/harness/significance.py` (#461 phase A1) turns two runs' `retrieval_per_query`
arrays into a per-`(corpus, query_class, measure)` verdict on whether their difference is
distinguishable from noise. `scripts/benchmark_rag.py --compare-only <baseline-A> <baseline-B>`
is the CLI entry point — it reads two **committed** baseline directories and needs no running
stack, no OpenSearch, and no Postgres, so it can be run against any two baselines that both
carry per-query rows (today: `miracl-es-english` and `miracl-es-multilingual`).

**What it tests.** For each paired query, `score_b - score_a` is a delta. The **primary** method
is a seeded paired bootstrap: `numpy.random.default_rng(0)`, 10,000 resamples with replacement
over the *paired* deltas (never over the raw A/B scores separately — resampling deltas is what
keeps each resampled unit tied to one query), reporting the mean delta and the empirical 95%
percentile interval around it. Fixing the seed makes the interval **bit-for-bit reproducible**:
the same two baselines always produce the same numbers, so a quoted CI can be checked rather
than merely trusted. The **secondary** method is a paired t-test on the same deltas (equivalent
to a one-sample t-test against 0), reported alongside the CI rather than instead of it — the CI
answers "how big might the effect be," the p-value answers "how surprising is this under the
null," and those are different questions that can point different directions on a small, noisy
axis (the fusion-arm anti-correlation earlier on this page is exactly a case where "significant"
and "practically meaningful" come apart).

**What a CI containing 0 means.** It means the sample cannot distinguish this delta from no
change **at this query count** — not that there is no effect. A genuine small effect and a
genuine null effect can produce visually similar intervals on ~150 queries; only a narrower CI
(more queries, or a larger effect) resolves that. Read "CI excludes 0" as a positive claim and
"CI contains 0" as "not yet distinguishable," never as "proved zero."

**Why the `lookup` class is always broken out**, never folded only into an aggregate: this page's
own Stage 5 analysis found corpus-level sign agreement (62.5%) meaningfully better than
lookup-class agreement (40.0%, tau-b −0.005 vs +0.301 pooled) — an aggregate delta can look
stable while hiding a lookup regression inside gains elsewhere, and `lookup` is the query class
the Stage 5 gate exists to protect. `significance.summarize` reports `lookup` as its own row
whenever a corpus has any lookup queries, even when it is that corpus's only class.

**Why not Wilcoxon signed-rank**, despite being the common textbook default for paired retrieval
comparisons: it discards delta *magnitude* and keeps only sign and rank, a needless power loss
against continuous bounded measures (nDCG, recall, MRR) that have no reason to prefer a rank
transform. Its validity also assumes a symmetric difference distribution, which per-query
retrieval deltas routinely strain — many queries tie at delta 0, and nDCG's `[0, 1]` bound clips
the distribution's tails. This module does **not** claim to have independently measured that
combination inflating Type-I error here — that would need its own citation-backed study; see
Urbano, Lima & Hanjalic (SIGIR 2019) for the closest published treatment of significance testing
in this exact IR setting, whose findings are narrower than a blanket claim. The decision rests on
the power/symmetry argument alone, which stands on its own: the bootstrap makes no distributional
assumption at all, and the t-test's normality assumption is checkable from the query count
already on the row; Wilcoxon buys no advantage over either that would justify the rank
transform's cost.

**Refusing a silent partial join.** `significance.paired_join` requires both runs to score
*exactly* the same query id set — a run missing queries the other has raises `PartialJoinError`
naming the counts on each side, rather than silently intersecting and reporting a comparison over
whichever queries happened to survive. A comparison across two baselines that scored different
query counts is not paired data; it is two point estimates wearing a paired analysis's clothing.
A **duplicated** query id on either side is refused too (`DuplicateQueryIdError`, checked before
the id-set comparison): a set comparison alone is multiplicity-blind, so a repeated id would pass
`ids_a == ids_b` and then silently lose all but its last occurrence to a last-wins dict build —
the identical failure through a different door.

## Wave 2 instruments (#461 W2.E1) — no Wave-2 flag flips without these

Four new query classes and three per-turn instrumentation hooks, gating every feature flag this
plan turns on. All four classes are deterministic and license-free: they are carved or planted
from data this harness already loads (QMSum's own human-authored queries, already licensed) or
synthesized outright, with no new corpus and no LLM required to SCORE against them — though most
have no product path yet to submit an answer worth scoring (see the table below).

| Class | Carved/planted from | Measure(s) | Engine |
|---|---|---|---|
| `SPEAKER_ATTR` | QMSum queries matching an attribution regex (`"according to X"`, `"who said"`, ...) whose gold turn span resolves to exactly one speaker | `answer_names_gold_speaker`, `citation_speaker_match` — **never merged into one number** | `harness/attribution.py` |
| `SPEAKER_SUMMARY` | Same carving applied to QMSum's SUMMARIZE-class queries | `speaker_coverage` | `harness/attribution.py` |
| `ATTRIBUTION_PROBE` | One planted negative per SPEAKER_ATTR case: a real OTHER speaker from the same meeting who did NOT say the quoted material (deterministic decoy selection — lexicographically first other speaker, no randomness) | `false_attribution_rate` (**lower is better** — see `significance.MEASURE_DIRECTION`) | `harness/attribution.py` |
| `RECURRENCE` | Synthetic planted action-item groups across files, in the DEFAULT SUMMARY PROMPT's shape | `group_precision`, `group_recall` (pairwise/co-membership) | `harness/recurrence.py` |

### The RECURRENCE shape: verified, not assumed

The task briefing named a specific risk: planting the WRONG action-item shape produces a harness
that stays green while production finds zero groups, which is worse than no harness. Both
candidate shapes were read directly, not assumed:

- `backend/app/core/default_prompts.py` lines 63-71 (`UNIVERSAL_CONTENT_ANALYZER_PROMPT`'s
  `action_items` block) — what the default summary prompt actually instructs the model to
  produce: `{item, owner, due_date, priority, context, mentioned_timestamp}`.
- `backend/app/schemas/summary.py` lines 44-52 (`ActionItem`) — `{text, assigned_to, due_date,
  priority, context, status}`. A DIFFERENT shape, and — checked by grep across `app/services` and
  `app/tasks` — **dead code**: `ActionItem` is exported from `app/schemas/__init__.py` but no
  caller validates or renders a summary through it. `SummaryData.action_items` is typed
  `list[Any]` and accepts whatever the prompt produces verbatim.

`harness/recurrence.py` plants `PLANTED_FIELDS = (item, owner, due_date, priority, context,
mentioned_timestamp)` — the prompt shape — and `test_eval_recurrence.py` pins both the exact
field set and that it is NOT the schema shape, so a future edit reintroducing the wrong shape
fails a test rather than shipping quietly.

### Measurable vs pending

| Instrument | Status | Why |
|---|---|---|
| SPEAKER_ATTR / SPEAKER_SUMMARY carving | **Measurable now** | Pure derivation over data the harness already loads; no product change needed to compute the gold set |
| ATTRIBUTION_PROBE planting | **Measurable now** | Same — deterministic, reuses SPEAKER_ATTR's resolved gold |
| Scoring an actual submitted answer for any of the three above | **Pending a product path** | Nothing in the chat pipeline currently returns a structured `(speaker, citations)` answer this harness can consume; until then the only honest submission is the `none` answerer's floor (0 on every measure) — the same pattern Stage 4 established for aggregation's null-answerer floor |
| RECURRENCE planting + pairwise scoring | **Measurable now** | Pure, deterministic, no product dependency |
| RECURRENCE detection itself | **Not built in the product at all** | `chat/prompting.py`'s `<recurrence>` block and `schemas/chat.ChatWarningCode.RECURRENCE_UNAVAILABLE` are both explicitly commented "(Wave 2; no emitter yet)" in the current codebase |
| `llm_calls` per turn | **Partially measurable now** | `chat/mapreduce.Overview.as_metadata()` emits `"llm_calls"` — but only merged into `meta["overview"]` on turns whose route adds an `<overview>` block. A lookup-routed turn carries no `overview` key at all. `harness/chat_instrumentation.extract_llm_calls` returns `None` (not `0`) for those turns |
| `router_language_unmatched` | **Pending** | `schemas/chat.ChatWarningCode.ROUTER_LANGUAGE_UNMATCHED` exists as a reserved warning code; grepped, no emitter sets it anywhere. The extractor reads a specific, documented key (`meta["route"]["language_unmatched"]`) chosen to match the existing per-stage-block convention, ready the moment an emitter lands |
| planner fire-rate | **Pending, and the key name itself is unconfirmed** | No "planner" concept exists anywhere in `schemas/chat.py` or `services/chat/` — grepped, zero hits, unlike the other two which at least have a reserved name. `extract_planner_fired` reads a PROPOSED key (`meta["planner"]["fired"]`); treat it as a suggestion for the owning lane, not a contract |

Every extractor in `harness/chat_instrumentation.py` returns `None` — never a default `0`/`False`
— when its source key is absent, and `summarize_instrumentation` reports `coverage` (how many
turns actually carried the field) alongside any rate/mean, so a corpus-level rollup can never
read "not measured" as "measured and zero."

## AMI distractor haystack (#461 A5)

**This adds distractor realism, not new judgements.** A reader who assumes 34 new meetings
came with relevance judgements will misread every table below it: QMSum's gold set is
completely unchanged — same 1,576 queries, same gold spans, same qrels. What changes is the
HAYSTACK those queries are retrieved against. `adapters/ami.py`'s whole job is to inject 34
real AMI meetings, same domain as QMSum's `Product` split, that carry zero relevance
judgements of their own and exist purely to make retrieval discriminate against more
same-domain content — the way a real deployment's index is never just the files a benchmark
happens to care about.

### The number: measured, not assumed

QMSum's `Product` domain redistributes 137 of AMI v1.6.2's 171 meetings (see "Corpus
composition is a result, not a detail" above). The other 34 were **measured directly against
the real corpus**, not taken from the plan that specified this lane:

```
AMI meetings.xml observations:        171
QMSum data/Product/all/*.json stems:  137
Distractor set (AMI − QMSum):          34   (EN 16, IN 10, IB 7, TS 1)
```

This matches the number the task brief was written against, but it was re-derived rather than
trusted — `TestAgainstTheRealCorpus::test_distractor_count_and_prefix_distribution` in
`backend/tests/unit/test_corpus_injection_ami_adapter.py` pins it against the live NAS copy,
gated the same way `test_corpus_injection_adapters.py` gates its own real-corpus assertions.

### Why no diff-based alignment, unlike QMSum's Product/Academic timing recovery

`qmsum.py` recovers real timings for its AMI/ICSI-sourced meetings by **diffing** QMSum's
redistributed (and lightly re-edited) text against the reference corpus's timed words
(`nxt.align_turns_to_channels`) — necessary because QMSum's turns are not identical to AMI's
own segments (ES2004a: 320 QMSum turns vs 283 AMI segments; see `nxt.py`'s module docstring).
The 34 distractors need no such reconciliation: `adapters/ami.py` builds turns directly from
AMI's own `segments.xml` (curator-set `transcriber_start`/`transcriber_end`) and `words.xml`
(per-word times) — there is no second, independently-redistributed transcript to reconcile
against, so every turn is real-timed by construction (100%, not a measured alignment rate).
Two things worth knowing if you read the adapter:

- **`meetings.xml` under-lists a real meeting's channels.** `IN1001` lists 3 `<speaker>`
  entries (A/B/C) but ships 4 channels' worth of `segments.xml`/`words.xml` (A-D) — measured,
  not assumed. Channel *presence* comes from the `segments`/`words` directory listing, never
  from `meetings.xml`.
- **Non-verbal-only segments are dropped, not emitted empty.** A `<segment>` whose only
  child is `<vocalsound>` (a laugh, a cough) contributes no turn. Measured across all 34
  meetings: 23,049 of 25,269 raw `<segment>` elements (91.2%) carry real text.

### Injecting it

```bash
./scripts/inject-eval-corpus.sh --fresh <name> --corpus qmsum   # the scored corpus, as always
./scripts/inject-eval-corpus.sh --fresh <name> --corpus ami     # the distractor haystack, same stack
```

`ami` is a normal `CorpusAdapter` (`adapters/__init__.py`'s registry, key `"ami"`), injected
through the same production search-indexing path as every other corpus. It is deliberately
**not** wireable through `scripts/benchmark_rag.py --corpus ami`: `harness/corpora.py` ships no
query loader for it (`_load_corpus`'s `else` branch raises `SystemExit` naming exactly that), so
a distractor corpus can never accidentally be scored as though it had judgements. Its only
effect on a `--corpus qmsum` run is a bigger index to search against — which is the point.

### ⚠️ The control-baseline trap this makes MANDATORY, and how it is closed

Changing index composition invalidates every prior number, and **nothing about `metrics.json`
shows that on its own** — a distractor corpus contributes no `corpora` entry (it is never
`--corpus`-scored), so a QMSum-only baseline and a QMSum+AMI-distractors baseline can look
identical in the file that gets compared. `scripts/benchmark_rag.py` now closes this with an
**injection identity**, recorded in `runinfo.json` (never `metrics.json` — like every other
run-circumstance field, it is not part of the deterministic scoring claim):

- `_scan_injection_identity` (in `benchmark_rag.py`) reads every manifest directory actually
  present in the **measured OpenSearch index** — scored or not — and records
  `{key, version, meetings_in_manifest, files_present_in_index, scored}` per corpus, sorted by
  key, plus a `fingerprint` (sha256 of the canonical list, truncated to 16 hex chars). A
  manifest on disk whose files never landed in *this* index (a stale manifest from an unrelated
  `--fresh` deployment) is excluded rather than recorded as zero-present — indistinguishable
  from "present but retrieved nothing" otherwise.
- `--compare-only` reads both baselines' `runinfo.json` and **refuses (exit 3), not warns**,
  when both sides recorded an identity and the fingerprints differ — comparing a QMSum-only run
  against a QMSum+AMI-distractors run would report a delta that is actually a haystack-composition
  change, not a retrieval change. A baseline committed before this landed (no `runinfo.json`, or
  one without `injection_identity`) still compares, with a loud warning instead — refusing every
  legacy baseline would be a regression, not a safety improvement.

Tests: `backend/tests/eval/test_eval_benchmark_injection_identity.py` — including an
end-to-end CLI-seam test that actually invokes `_run_compare_only` and asserts exit 3. That
test was itself caught failing to test what it claimed during red-checking: the first version
gave one baseline no `retrieval_per_query` rows at all, so it hit `_run_compare_only`'s
*earlier* empty-rows guard and returned 3 for an unrelated reason — mutating the identity
comparison to always allow the run still passed. Fixed by giving both baselines identical,
valid rows so exit 3 can only come from the identity refusal.

### ⚠️ Interaction with the Product-domain false-negative trap — report Recall separately

"Corpus composition is a result, not a detail" (above) already measured that QMSum's `Product`
domain suffers severe qrels false-negativity: R@1 of **0.124** (vs 0.664 for Committee), median
gold rank 22, and — the number that matters here — **49.2 of the other 136 Product meetings
score ≥90% of the gold meeting's own score** for a typical query. Product queries are already
heavily penalised for retrieving a genuinely on-topic AMI meeting that nobody marked relevant,
because AMI's four-role, one-scenario design makes every Product meeting look like every other
Product meeting to a retriever.

Injecting 34 MORE same-domain, same-role-structure AMI meetings makes this mechanically worse,
**not because retrieval got worse** — because there is now more near-duplicate competition for
Product-domain queries to be marked "wrong" against. A Recall drop on Product-class queries after
this injection is the **expected shape of the existing false-negative problem**, not evidence of
a regression, and must never be read as one.

Consequently: **Recall must be reported separately from every other measure after this
injection, never folded into one blended headline number.** A single "Recall dropped 4 points"
line invites exactly the misreading this section exists to prevent. Report it broken out at
least by domain (Product vs Academic vs Committee) or by corpus scope (QMSum-only index vs
QMSum+AMI-distractors index), the same axis "Corpus composition is a result, not a detail"
already uses, so a reader can see the Product-domain number moving for the reason this section
documents rather than inferring a retrieval regression from a corpus-wide average.

## Answer-quality harness (#463, W2/A2)

Where retrieval measures "did the right chunks come back," this tier measures "was the
*answer* any good" — a question no nDCG/recall number can address, and the gap #453/#461's
own worked examples kept running into.

### The measures

| Tier | Measure | Engine | Needs |
|---|---|---|---|
| Deterministic floor | `rougeL_f`, `rouge1_f`, `token_f1`, `answered` | `harness/answer_text.py` | Nothing — no LLM, no GPU |
| Deterministic floor (optional) | `bertscore_f1` | `harness/answer_text.py` (`microsoft/deberta-large-mnli`, `rescale_with_baseline=True`, `lang="en"` — pinned at the one call site) | torch + transformers (already app deps) |
| LLM-judged, reference-free | `faithfulness` | `harness/answer_judge.py` → `harness/judge_runner.py` (RAGAS, in `backend/venv-eval/`) | A configured judge provider + `backend/venv-eval/` |
| LLM-judged, reference-based | `answer_correctness` | `harness/answer_judge.py` → `harness/judge_runner.py` (RAGAS, local sentence-transformers embedder, in `backend/venv-eval/`) | A configured judge provider + `backend/venv-eval/` |
| Negative control | `false_answer_rate` | `synthetic/unanswerable.py` | Nothing to PLANT; a real system to submit against |

**The floor and the judged tiers answer different questions, and neither substitutes for the
other.** ROUGE/token-F1 reward lexical overlap with QMSum's own gold answer — a correct
answer phrased differently scores low here on purpose, which is exactly why it is a floor, not
a verdict. `faithfulness` and `answer_correctness` are two SEPARATE axes reported side by side,
never merged: a model can be faithful to bad context (high faithfulness, wrong answer) or
unfaithful to good context (low faithfulness, accidentally right) — see `answer_judge.py`'s
module docstring for the full argument.

### The RAGAS judge tier lives in its own venv, talked to over a subprocess boundary

**Resolved, not a workaround — read this before "simplifying" it back into one venv.**
`ragas==0.4.3` requires `instructor` unconditionally (no extras gate); `instructor==1.15.4` — the
newest version PyPI has, checked 2026-08-19, no newer release exists — requires
`openai<3.0.0,>=2.0.0`. This repo's `requirements.txt` pins `openai==3.3.0`, the app's real LLM
client, exercised by host-venv pytest and mypy alike in a checkout where other agents run against
`backend/venv` concurrently. `pip install --dry-run` with `ragas` added to `backend/venv`
resolved to `Would install ... openai-2.54.0` — a silent **downgrade** of the shared venv's
`openai` package for every other consumer. That is the exact venv/image divergence issue #492
exists to prevent, just relocated to venv-vs-venv instead of venv-vs-image. `rouge-score==0.1.2`,
`bert-score==0.3.13` and `nltk==3.10.3` were verified conflict-free the same way (`pip install
--dry-run`: only `absl-py` new, everything else already satisfied by the app's own
torch/transformers stack) and stayed in `backend/requirements-eval.txt`, installed into
`backend/venv` as before.

The fix is a **separate interpreter**, not a relaxed pin. `backend/requirements-eval-judge.txt`
is installed **only** into `backend/venv-eval/` (gitignored: `python3.12 -m venv backend/venv-eval
&& backend/venv-eval/bin/pip install -r backend/requirements-eval-judge.txt`), and is never
installed into `backend/venv`, a Docker image, or merged into `requirements-eval.txt`.
`backend/tests/eval/harness/judge_runner.py` is the **one file in the whole answer-quality
tier that imports `ragas`** — it runs exclusively under `backend/venv-eval/bin/python`, as a
subprocess of `answer_judge.py` (which runs in `backend/venv` and never imports `ragas` at all).
The boundary is JSONL over stdin/argv/stdout: `answer_judge.py` writes `{question, answer,
contexts, ground_truth}` records to a temp file, invokes
`backend/venv-eval/bin/python judge_runner.py --mode ... --input ... --output ...`
(`subprocess.run`, `timeout=1800s`), and reads `{query_id, score}` back — `score: null` for a
per-record judge failure (counted as a NaN, never dropped, same convention as the rest of this
harness), a non-zero exit for an infrastructure failure (bad input, ragas won't import, the
provider is unreachable), raised as `RuntimeError` so an outage is never misreported as a batch
of low scores. The two venvs' dependency graphs never need to be compatible, because they are
never the same Python process — permanent by construction, not by discipline.

**Two more upstream ragas packaging gaps were found and pinned while building `venv-eval`,
independently of the `openai` conflict above:**

- `ragas==0.4.3` declares `Requires-Dist: langchain-community` with **no version bound at all**,
  so an unconstrained install resolves the newest release (0.4.2, checked 2026-08-19).
  `ragas/llms/base.py` imports `from langchain_community.chat_models.vertexai import
  ChatVertexAI` at **module level** — not lazily, not behind a provider check — and that
  submodule does not exist in `langchain-community==0.4.2` at all (that package's own README
  says it "is being sunset," and Vertex AI support moved to the standalone
  `langchain-google-vertexai` package). The result is `ModuleNotFoundError: No module named
  'langchain_community.chat_models.vertexai'` on a bare `import ragas`, not a provider-specific
  failure. Pinned to `langchain-community==0.3.31` — the last 0.3.x release, which still ships
  `chat_models.vertexai` and satisfies ragas's own `langchain>=0.3.27,<2.0.0` bound.
- `ragas.embeddings.huggingface_provider.HuggingFaceEmbeddings(use_api=False)` —
  `answer_correctness`'s local similarity embedder — lazy-imports `sentence-transformers` and
  raises its own `ImportError` without it; ragas does not declare it as a hard dependency because
  not every metric needs an embedder. `faithfulness` worked before this pin was added;
  `answer_correctness` failed at construction with exactly that `ImportError` until it was.
  Pinned to `sentence-transformers==5.7.0` — the same version `backend/requirements.txt` already
  resolves for the app's own embedder, so `venv-eval`'s `all-MiniLM-L6-v2` behaves identically to
  the app's, not coincidentally close.

Both pins, with the exact evidence above, are documented in
`backend/requirements-eval-judge.txt`'s own header — read it before touching either version.

**Unexecuted judge code is not a judge — this was run for real, against the live vLLM
(`http://localhost:5195/v1`, `gemma-4-e4b`, temperature 0), before being reported as working:**

- `faithfulness` discriminates: a faithful answer scored **1.0**, a fabricated one **0.0**.
- `answer_correctness` discriminates: a correct paraphrase of the gold answer scored **0.946**,
  a wrong answer scored **0.042**.

Verified through both the raw `judge_runner.py` script directly and through the full
`answer_judge.py` API running in `backend/venv` (confirming the subprocess plumbing end to end,
not just the runner script in isolation). `backend/tests/eval/test_eval_answer_judge.py`'s
`TestRealFaithfulness`/`TestRealAnswerCorrectness`/`TestRealBatchEvaluation` classes are gated on
both `backend/venv-eval` being present and the vLLM being TCP-reachable
(`pytest.mark.skipif`, port derived from `LLM_TEST_PORT`, default 5195 — never a bare literal, so
the probe follows a `--fresh ... --port-offset N` stack instead of always asking about whichever
stack owns the base port) and skip cleanly on a machine without either.

**The D6-safe degrade path is tested, not assumed.** `TestIsAvailableForcedAbsent` monkeypatches
`_EVAL_VENV_PYTHON` to a path that provably does not exist (independent of whether this machine
actually has `venv-eval` set up) and asserts `is_available()` returns `False`, `build_judge`
raises an `ImportError` naming the exact install command
(`backend/venv-eval/bin/pip install -r backend/requirements-eval-judge.txt`), and
`_run_judge_subprocess` refuses before ever spawning a process — so a deployment with no judge
venv still runs the deterministic floor and reports "not measured" for the judged tier, never a
crash.

### Negative result: `gemma-4-e4b` has no reasoning off-switch, and it eats the completion budget

Measured directly against the reference vLLM (`http://localhost:5195/v1`) while `RagAnswerer`
was written — a plain, corpus-free connectivity check, not a judged or retrieval measurement:
`max_tokens=10` truncated mid-reasoning and returned **no answer content at all**;
`max_tokens=200` was enough for a trivial arithmetic question's ~47-token reasoning trace. This
matches `app/services/CLAUDE.md`'s own reasoning table for this exact model (no off-switch;
`false` is byte-identical to omitting the parameter). `RagAnswerer.response_tokens` defaults to
2048 for this reason — a starting point, not a calibrated floor, since a real QMSum answer's
reasoning trace is unmeasured.

### `RagAnswerer` (`harness/answerers.py`) — the real chat path, driven in-process

Drives `retrieve_context` → `mask_chunks` → `build_system_prompt`/`build_messages` →  a raw
OpenAI-compatible completion, exactly the stages a real chat turn runs (`chat/service.py`).
Two things worth restating because they were the load-bearing design constraints, not
afterthoughts:

- **Hard-fails at construction without a configured provider.** `base_url`/`model` are
  required constructor arguments; missing either raises `ValueError` immediately, so a
  misconfigured run cannot silently produce a results file full of empty "declined" answers
  that would read as a real measurement.
- **`rerank_enabled` bypasses `chat.settings.apply_user_preferences` entirely.** That function
  narrows one-way — `base.rerank_enabled and rerank_enabled` — so if the resolved admin
  default happens to be off, asking it for `rerank_enabled=True` silently produces `False`
  anyway. An A/B whose "on" arm needs to reliably BE on (the eventual #463-adjacent reranker
  comparison depends on this) cannot be built on that function. `RagAnswerer` constructs its
  own `ChatSettings` with the caller's exact value written in.
  `tests/eval/test_eval_rag_answerer.py`'s `TestNeverRoutesRerankThroughApplyUserPreferences`
  is an AST guard over the module source (not a mocked call path) — it was verified to catch a
  real reintroduction of `apply_user_preferences` by mutating a throwaway copy of the source
  and confirming the test goes red.

### Unanswerable controls (`synthetic/unanswerable.py`)

30 deterministic questions about entities engineered to have near-zero chance of appearing in
real transcript content — invented proper nouns with no dictionary-word roots (`Zorblatt
Industries`, `Kwenzalotl Corporation`, ...), a stronger guarantee than picking obscure-but-real
names a committee/business transcript could plausibly mention by coincidence. `false_answer_rate`
is a conservative text heuristic (checks for an explicit decline phrase; can undercount a decline
phrased unusually, never overcounts an explicit decline as a fabrication) — the point it exists
to make is that **no relevance metric can see this failure mode at all**: a system that retrieves
nothing and correctly declines scores identically, on nDCG/ROUGE alike, to one that fabricates a
confident answer about something that was never in the corpus.

### `load_qmsum_answer_queries` (`harness/corpora.py`)

Two sources from data already loaded elsewhere in this file, no new corpus:
`specific_query_list[].answer` (gold_text, spans kept as faithfulness context) and
`general_query_list` — "Summarize the whole meeting," excluded from retrieval scoring
(`load_qmsum_queries`) for lacking a `relevant_text_span`, but included here as a
**gold-file-scoped** SUMMARIZE query (`spans` names exactly one file, so a `--scope gold-files`
run restricts retrieval to it — asking the full corpus to summarize one meeting is a different,
undefined task). Verified against the real QMSum data while this was written: **0 missing
`answer` fields across all 4,728 specific queries** (696 meeting files, every domain/split), and
**`data/ALL/` has no `all/` split subdirectory at all** (only `jsonl`/`test`/`train`/`val`) — the
existing `data/{domain}/all/{meeting_id}.json` convention `load_qmsum_queries` already uses is
the only one that ever resolves.

### DoD status: no baseline committed yet

The plan calls for one committed `baselines/qmsum-answers-<model-slug>/` at temperature 0. That
needs a real generation run over an injected QMSum corpus, which needs either the live dev
stack (never — it holds real user data, unrelated to this eval) or a `--fresh` deployment with
QMSum injected. This lane's permission set excludes `./opentr.sh` (no stack start/stop), and no
`--fresh` eval deployment was up at the time this was written (checked: no `otfresh-*`/`rag403`
containers running). **Not run.** Everything upstream of that run — the measures, the loaders,
the answerer, the negative controls — is built, tested, and CI-safe; generating the committed
baseline is the one remaining step, and per this lane's own instructions it needs an estimated
runtime and a go-ahead before it starts (retrieval-only QMSum arms measured previously on this
machine: ~215-707 s depending on candidate pool; a generation arm calls an LLM per query and
will be substantially slower — see this lane's final report for the actual estimate once a
target corpus/query count is known).

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
  rank](#scoring-an-answer-not-a-rank)). The reference answerer's 1.000 characterises the corpus and
  the mechanism, not the shipped system; **the shipped system scores 0.800** through
  `--answerer product` (see [Stage 4](#the-products-aggregation-path)),
  and the gap is one rule with a named structural cause.
- **Injecting synthetic data moves the QMSum numbers.** Retrieval runs corpus-wide, so the
  candidate pool roughly doubles and document frequencies shift. Run the QMSum-only control before
  and after and record the delta; **never compare a measurement taken across the injection.**
- **Injecting the synthetic tier will move the QMSum numbers**, because both corpora share one
  index and its document frequencies. Any mixed-corpus baseline is a new control, not a comparison
  against this one.
- **No *generation* quality number exists.** Aggregation exactness is scored; faithfulness,
  citation correctness and answer prose are not, and the synthetic tier is explicitly not a source
  of generation ground truth. Nothing in this harness evaluates what a model wrote. The Stage 4
  coverage check (25 recordings / 44h 30m / 102 speakers, each verified against Postgres) is a
  **single hand-run measurement over one scope**, not a harness stage — it is evidence that the
  overview block works, not a number that can be regressed against.
- **Scale is split**: the largest real corpus (MeetingBank, 31.7 M words) is internal-only.
- **Multilingual coverage is 20 languages scored, not 100.** The unscored remainder is enumerated
  with a specific reason each — no public benchmark with relevance judgements, transcripts but no
  queries, or non-commercial licensing — rather than being implied by the product's language list.
