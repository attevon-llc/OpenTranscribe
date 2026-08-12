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
Repository and dataset-hub metadata has misrepresented the real terms **three times** in this
project:

- OpenSLR's AMI mirror serves an older release under **CC BY-NC-SA**, while the Edinburgh original
  v1.6.2 is **CC BY 4.0**.
- MeetingBank's Zenodo metadata field says `cc-by-4.0`; the `LICENSE.txt` shipped *inside* the
  archive — and the authors' own site — say **CC BY-NC-ND 4.0**.
- Every `BeIR/*` dataset repo is tagged `cc-by-sa-4.0`, including `BeIR/msmarco`, whose underlying
  MS MARCO terms are **non-commercial research only**.

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

### Tier 3 — synthetic **(planned)**

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

We use [`pytrec_eval`](https://github.com/cvangysel/pytrec_eval), which wraps NIST's `trec_eval` C
implementation. This is not a convenience choice — different libraries produce materially different
numbers for identical input:

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

## Reproducing the numbers

**(planned — commands land with the harness in Stage 1)**

Reproduction requires four things to be pinned, and all four are recorded with every result:

1. **Corpus** — dataset versions and checksums from `scripts/fetch-rag-eval-data.sh --verify`, plus
   the exact composition (which meetings, after which deduplication).
2. **Corpus state** — the injection manifest mapping each indexed file to its source meeting, turn
   and word counts, and whether its timings are real or synthetic.
3. **Seeds** — synthetic corpora regenerate byte-identically from their recorded seed.
4. **Metric implementation and version** — per the divergence above.

:::note Synthetic timestamps
QMSum has no timestamps, but OpenTranscribe chunks and cites by time. Meetings without a timed
counterpart in AMI or ICSI receive **synthetic** timings, flagged as such in the injection manifest
so that no timing-derived metric can be computed from them. 196 of 232 QMSum meetings (84.5%) have
real timings available, since all 137 Product meetings appear in AMI and all 59 Academic meetings
in ICSI.
:::

## What we cannot currently claim

Stated plainly, because a benchmark's limits are part of its result:

- **Publishable retrieval quality rests on QMSum.** The other Tier A corpora supply realism,
  multilingual coverage or long-context, not additional English meeting-retrieval judgements.
- **Multi-file and aggregation have no publishable real-data ground truth** — those numbers will
  come from synthetic corpora with the provenance stated.
- **Scale is split**: the largest real corpus (MeetingBank, 31.7 M words) is internal-only.
- **Multilingual coverage is 20 languages scored, not 100.** The unscored remainder is enumerated
  with a specific reason each — no public benchmark with relevance judgements, transcripts but no
  queries, or non-commercial licensing — rather than being implied by the product's language list.
