---
sidebar_position: 9
title: RAG — Design Decisions and How We Validated Them
description: Why corpus-scale retrieval is built the way it is, what we rejected, and the measurement traps that nearly produced fake wins
---

# RAG — Design Decisions and How We Validated Them

Three pages cover retrieval, and they answer different questions:

| Page | Question |
|---|---|
| [RAG Chat (Internals)](./rag-chat.md) | How does the pipeline **work**? |
| [RAG Evaluation Methodology](./rag-evaluation.md) | How is quality **measured**, and how do you reproduce a number? |
| **This page** | **Why** is it shaped this way, what did we reject, and what nearly fooled us? |

This one is a narrative. It is the page to read before proposing a change to retrieval, because
most of the obvious changes have already been tried, measured, or explicitly ruled out — and
because several of the numbers that would have justified them turned out to be artefacts.

:::info Status — most of this is designed and measured, not shipped
Corpus-scale RAG ([issue&nbsp;#403](https://github.com/attevon-llc/OpenTranscribe/issues/403)) is
an eight-stage programme. What is **live in the product today** is the hybrid retrieval and chat
pipeline described in [RAG Chat](./rag-chat.md) — everything else on this page is marked.

| Stage | What it adds | Status |
|---|---|---|
| 0 | Prerequisite fixes (bench container names, delete-before-reindex, ingest field map, speaker/title rename propagation) | Merged |
| 1 | Evaluation harness + committed baseline | **Built** — `./opentr.sh bench rag` |
| 2 | Deterministic ingest artifacts: per-file facts, extractive digests, keyphrases | In progress |
| 3 | Index v6 — one reindex, digests in the index | Not started |
| 4 | Query router, map-reduce, aggregation | Not started |
| 5 | Retrieval tuning bake-off (fusion, reranker, synonyms) | Not started |
| 6 | [Documents](../features/documents.md) | Not started |
| 7 | Opt-in enrichment | Not started |
| 8 | Whitepaper | Not started |

Anything described below as *planned* is a design with a gate attached, not a feature you can
use. The index is still at version 5 and the assistant's base prompt still has nine rules — both
are the cheap ways to check.
:::

## The architecture, in plain language

### Retrieval is hybrid, and hybrid means rank fusion

A question is answered from **speaker-turn chunks** of your transcripts, found two ways at once:

- **BM25** — classic keyword scoring. Excellent at rare literal strings: a ticket number, a
  product code, a surname.
- **kNN vector search** — an embedding model (default `all-MiniLM-L6-v2`, 384 dimensions, running
  *inside* OpenSearch) puts semantically similar text near each other. Excellent at paraphrase,
  useless at a string it has never seen.

The two result lists are combined with **Reciprocal Rank Fusion**: each document scores
`1 / (k + rank)` in each list, and those are summed, with `k = 30`
(`SEARCH_RRF_RANK_CONSTANT`).

RRF fuses **ranks, not scores**, which is the entire reason to use it. BM25 scores and cosine
similarities are not on a comparable scale, and normalising them requires choosing a
normalisation — min-max over the page, over the corpus, z-scores — each of which is a tuning
parameter that silently changes results as the corpus grows.

:::note The alternative is being measured, not assumed
Score-based fusion may well beat RRF here. The decision recorded in
[issue&nbsp;#363](https://github.com/attevon-llc/OpenTranscribe/issues/363) is **to measure it
before switching**, which is why Stage 5 exists and why it needs a second search-pipeline id
plumbed through before any A/B is possible. "RRF is the standard choice" is not evidence.
:::

### Chunking follows speaker turns, not a word window

`chunk_transcript_by_speaker_turns` groups consecutive segments from the same speaker into one
turn, then splits over-long turns at sentence boundaries with a sliding window
(`SEARCH_CHUNK_TARGET_WORDS` 200, `SEARCH_CHUNK_OVERLAP_WORDS` 40).

**Why turns and not fixed windows:** a fixed 200-word window over a conversation straddles
speakers. The moment it does, speaker attribution in an answer becomes a guess, and the
Speakers scope filter — retrieve only what *this person* said — stops being exact. The whole
value of transcript RAG over generic document RAG is that the corpus has structure; chunking
across that structure throws it away.

**The cost is real and measured:** chunk documents average **17 words**, because in conversation
one turn is usually one short chunk. That is a large part of what the baseline numbers below
measure, and it is production behaviour rather than a harness artefact.

Turn-bounded chunking is a **recorded decision not to relitigate** (#363). What is open is
whether documents — which have no turns — should chunk differently; the plan says yes, using a
sentence-boundary splitter for plain text and the parser's own chunker for PDFs.

### The summary / digest tier (planned, Stage 2–3)

The single most useful thing the Stage 1 baseline established is **where the loss is**:

| Scope | nDCG@10 (all 1,576 QMSum queries) |
|---|---|
| Corpus-wide — what chat actually does | **0.1052** |
| Oracle: restricted to the query's own gold files | **0.3296** |

Handing the retriever the right meeting **triples** the score. Roughly two thirds of the loss is
therefore *file selection*, not passage ranking within a file — so tuning the reranker attacks
the smaller term, and a tier that helps pick the right file attacks the larger one.

That is the argument for digests: one extra document per file, summarising it, so a query can
match a file as a whole before competing at the passage level.

Design constraints that fell out of it:

- **Extractive, never generative.** TextRank over a tf-idf sentence-similarity matrix. No LLM, no
  model load — because of D6, the rule that an `LLM_PROVIDER`-empty deployment stays first-class
  (see [Working Without an AI Model](../user-guide/without-an-ai-model.md)). A digest tier that
  needed a model would make the whole no-LLM deployment unmeasurable as well as unfeatured.
- **Every digest sentence is verbatim and carries provenance** — segment ids and real timestamps —
  so a digest citation deep-links to when it was said, and so the redaction layer can re-mask it
  from cached spans rather than re-detecting.
- **Sectioned, sized to the embedding window, from a measurement.** The embedding model truncates
  silently past 128 word-pieces, so digest sections are sized from a measured ratio of word-pieces
  per word on real transcript text (~1.37) rather than a guessed word count. A digest that
  overflows the window is worse than no digest: the tail is silently not embedded.

### Query routing, map-reduce and aggregation (planned, Stage 4)

Four question shapes fail differently, so they get different machinery:

| Shape | Example | How it is planned to be answered |
|---|---|---|
| lookup | "what did Dana say about pricing?" | Retrieval as today |
| multi-file | "what did we decide about pricing across all my calls?" | Retrieval + digests |
| summarize | "summarise the Q3 planning meetings" | Two-level map-reduce over per-file material |
| aggregation | "how many meetings mentioned the vendor contract?" | **Search aggregations or SQL — never an LLM counting** |

**The router is rules, not a model.** A lexicon over the original and rewritten query plus
structural signals (how many files are in scope, is a speaker filter set, is there a quoted
phrase). Sub-millisecond and deterministic. Where an LLM signal helps, it is piggybacked onto the
follow-up rewrite call that is already being paid for — the rule is **never make a routing-only
LLM call**, because that would put a provider round-trip in front of features that must work
without a provider.

**Aggregation never asks a model to count.** Counts and coverage come from an OpenSearch
cardinality aggregation; "which speakers discussed X" from a terms aggregation; enumerations from
Postgres over stored summary JSON; and "who talked most" from the Stage 2 facts table. An LLM, at
most, phrases the result.

One hard constraint shapes all of it: **aggregations must not be issued against a hybrid query
body.** OpenSearch 3.4 throws `ArrayIndexOutOfBoundsException` inside `score-ranker-processor`
when cardinality aggregations meet hybrid + collapse + RRF. That is why the aggregation path is a
separate `size: 0` search rather than an extra clause on the existing one — and why the same rule
binds the evaluation harness.

## What we rejected, and why

Recording rejections is cheaper than rediscovering them.

| Rejected | In favour of | Reason |
|---|---|---|
| `ranx` as metric engine | `pytrec_eval_terrier` | Tie-break by dictionary insertion order — not reproducible. Also +445&nbsp;MB and 13 top-level packages including pandas, which this repo deliberately removed |
| `sklearn.ndcg_score` | same | Its dense input shape cannot express "retrieved but unjudged", which is most of a real run |
| BEIR's `EvaluateRetrieval` | same | It is a wrapper over `pytrec_eval_terrier`; the dependency buys nothing |
| `semantic-router` | ~120 lines of rules | Its base dependencies drag `litellm` / `openai` into a product whose no-LLM deployment is a first-class mode |
| LangChain `map_reduce` chains | Our own reducer | The reducer must not import the app's prompt-manager layers; a framework chain cannot honour that constraint |
| GraphRAG / a knowledge graph | Hybrid + metadata | Measured, not aesthetic: on a 39,190-artifact enterprise benchmark, structured-metadata retrieval scored 32.96 and plain hybrid 20.61, while a GraphRAG variant scored 10.31 — at 100–2000× the indexing cost, and one published measurement of 331,375 tokens per global-search query against 879 for vanilla RAG |
| Chunk-level metadata facets | Document-level only | Document-level metadata measured as a clear win; chunk-level measured as a small negative |
| An off-the-shelf RAG server (Open WebUI, Dify, Onyx, Morphik) | Building on our own index | Each duplicates four subsystems we already own to supply the one we lack — and every candidate that could talk to our OpenSearch is licence-blocked for an AGPL project (branding riders, multi-tenant prohibitions, enterprise code inside the official images, or a revenue-capped BSL grant) |

## The traps

This is the section that should earn or lose your trust in the numbers, because every one of
these was found *after* it had already produced a plausible result.

### 1. A corpus can decide your retrieval score before your retriever does

Measuring BM25 over all 1,576 QMSum queries with one identical qrels convention, and varying only
which domain the meetings come from:

| Domain | R@1 | Median rank of gold | Other meetings scoring ≥90% of gold |
|---|---|---|---|
| Product (AMI — one fictional scenario, 137 meetings) | **0.124** | 22 | **49.2** of 136 |
| Academic (ICSI) | 0.234 | 4 | 22.1 |
| Committee (parliamentary) | **0.664** | 1 | 2.2 |

Same retriever, same metric, **5.4× difference**. The AMI "Product" meetings are all the same
fictional remote-control design scenario with the same four role names, and are really only 35
distinct projects recorded across multiple sessions. *"What did the Project Manager say about the
buttons?"* legitimately matches dozens of them; exactly one is marked relevant, so the retriever
is penalised for being right. This is **qrels false-negativity**, a property of the corpus, not a
retrieval defect.

The composition effect is bigger than the scale effect:

- Deduplicating to one session per AMI series (130 documents) lifts R@1 from **0.289 to 0.422**.
- Adding 569 cross-domain distractors — a **5.4× larger index** — costs 6 points (0.422 → 0.361).
- Adding back 107 near-duplicate AMI sessions costs **13 points** (0.422 → 0.289) *while shrinking
  the index*.

**Near-duplicate structure costs more than scale.** Any benchmark that pools all 232 meetings is
substantially measuring its own corpus construction. The practical rule adopted: always report
per-domain alongside pooled, and never present the naive pool as a headline without labelling it.

### 2. A document-naming convention can manufacture a gate pass

Transcript chunks are identified `{file_uuid}_{chunk_index}`; the planned digests are
`{file_uuid}_digest`. `trec_eval` breaks tied scores by **document id, descending**, and in ASCII
`d` sorts above every digit.

Measured, at *identical* relevance scores:

```
gold = '3f2a9c10_digest'  →  rank  1 of 13
gold = '3f2a9c10_11'      →  rank 10 of 13
gold = '3f2a9c10_0'       →  rank 13 of 13
```

Ties are not incidental here. RRF scores are sums of `1/(k + rank)` over integer ranks, so
identical ranks in different legs produce bit-identical scores **structurally**.

Now line that up with the plan: Stage 3's gate is *"nDCG@10 improves on the multi-file class"*,
and Stage 3 is the stage that introduces digest documents. **It could have passed its own gate on
document naming alone, with no retrieval improvement whatsoever** — and the result would have
looked exactly like a win.

The harness re-sorts every run by `(-score, doc_type, file_uuid, chunk_index)` and re-emits
strictly decreasing synthetic scores, so the evaluator's id tie-break can never reach a result.
Two tests pin it: one swaps the id conventions and asserts the metric is unchanged, and a second
asserts that raw `trec_eval` *would* have scored them differently — the guard test that proves the
hazard is real rather than theoretical.

### 3. Two metric libraries, identical input, 0.369 apart

| Case | pytrec_eval | ranx | sklearn |
|---|---|---|---|
| graded 3/2/1, nDCG@10 | 0.922495 | 0.922495 | 0.922495 |
| tie, gold sorts first | **0.630930** | **1.000000** | 0.5 |
| tie, gold sorts last | **1.000000** | **0.630930** | 0.5 |

**0.369 absolute nDCG@10 on the same input**, caused entirely by tie-breaking policy:
`trec_eval` by docid descending, `ranx` by dictionary insertion order, `sklearn` by averaging the
tied gains. On the non-tied case all three agree to six decimal places, which is exactly why this
is easy to miss — you validate on a clean case and never see it.

The conclusion is stated as a principle on the methodology page: **a metric with no named
implementation is not a result.** The engine, its version, and the relevance policy are written
into every results file.

### 4. The library omits the queries you most need to see

`pytrec_eval` silently drops any query present in the qrels that the run did not answer. So
`mean(results.values())` averages over the queries you *answered* — and **flatters exactly the
regressions worth catching**. A change that breaks retrieval for 200 queries and returns nothing
for them scores *nothing* for them, not zero, and the mean can go **up**.

The harness iterates the qrels query set rather than the results dict, substitutes 0.0, records
the unanswered set, and then asserts the denominator equals the number of qrels queries. Two
tests pin it, including one that computes the naive mean (1.0) beside the corrected one (0.5) on
the same input.

Three lines of code, and the highest-consequence three lines in the harness.

### 5. Platform licence metadata has been wrong four times

Not once, not as an anomaly. Four times in one project, in both directions:

| Dataset | Metadata said | Actual terms |
|---|---|---|
| AMI (OpenSLR mirror) | CC BY-NC-SA | The Edinburgh original v1.6.2 is **CC BY 4.0** |
| MeetingBank | Zenodo field `cc-by-4.0` | `LICENSE.txt` *inside the archive* says **CC BY-NC-ND 4.0** |
| `BeIR/*` | every repo tagged `cc-by-sa-4.0` | MS MARCO's own terms are **non-commercial research only** |
| OmniDocBench | HuggingFace `license` field **empty** — every automated check passes | prose "Copyright Statement": **research purposes only, not for commercial use** |

Each would have put an unpublishable number in a paper. The generalisable rule: **an absent or
permissive metadata field is not evidence of a permissive licence.** Read the card body, the repo
`LICENSE`, and any licence file shipped *inside* the archive — the last one binds.

The same trap has a tooling face: `pytrec_eval_terrier` declares MIT, but that covers the Python
wrapper; several embedded `trec_eval` C sources carry "permission is granted for use and
modification of this file for research, non-commercial purposes". Hence it is an **evaluation-only
dependency**, isolated in `requirements-eval.txt` and never built into a published image.

### 6. `ORDER BY start_time` is not a total order

The measurement was deterministic. The **index** was not.

Re-indexing one *unchanged* corpus three times:

| Run | Chunks | nDCG@10 (all) |
|---|---|---|
| initial | 119,950 | 0.1052 |
| after a stack rebuild | 119,949 | 0.1023 |
| after a forced re-index | 120,540 | 0.1029 |

Identical inputs each time — 232 files, 129,062 segments, 2,145 speakers — and the index was
internally coherent on every run. The chunking genuinely differed.

**Cause:** overlapping speech and interpolated backchannels routinely share an onset —
**3,072 tie groups covering 6,152 segments** in this corpus. Postgres returns tied rows in
physical storage order, which a delete-then-bulk-insert reshuffles:

```
471.983  471.993  "Uh - huh ."
471.983  473.233  "I mean , if you did it at th..."
```

Whether that 10&nbsp;ms backchannel sorts before or after the 1.25&nbsp;s utterance it overlaps
decides whether the turn is split, which moves chunk boundaries, which moves the score.

The drift is about **2.8%** — the same size as a plausible real improvement. Stage 3 mandates a
full reindex, so its control and its treatment necessarily sit on different indexes; without this
fix that stage could have passed its gate on document reordering alone.

Every chronological segment read now orders by `(start_time, end_time, id)` — `id` last because
`start_time` *and* `end_time` can both tie. It is enforced by an AST detector over `backend/app`
rather than by 23 hand-edits and a hope, with an allowlist that requires a written reason.

This one was not only an evaluation problem: the same instability produced SRT/VTT cues in
different orders between exports, transcripts reordering between page loads, and the summariser
seeing a different transcript on each run.

:::danger The rule this leaves behind
**A control with an unmeasured reproducibility band is not a control.** Establish the band before
trusting any delta against it. Both of the traps that would have manufactured a Stage 3 pass —
document-id tie-breaking and index instability — were found by asking "what would make this gate
pass without the feature working?" rather than by a test failing.
:::

## What we still cannot claim

- **The committed Stage 1 baseline predates the total-order fix.** It is a valid control for
  everything measured on the same index, but it cannot be compared against anything produced after
  that fix; it owes a regeneration before Stage 2 or 3 reports a delta.
- **No answer-quality number exists anywhere.** Everything above measures retrieval. Nothing yet
  scores whether an answer is faithful to what it cites.
- **No reranker latency has been measured**, despite the reranker being on by default.
- **Publishable retrieval quality rests on QMSum.** The other permissively-licensed corpora
  contribute realism, multilingual coverage or long-context — not additional English
  meeting-retrieval judgements.
- **Mixed transcript + document retrieval quality cannot honestly be claimed as a benchmark
  result** when documents ship: no public dataset provides ground truth for it. The defensible
  framing is per-type quality on public benchmarks plus a constructed, fully-specified gate.

## Related

- [RAG Evaluation Methodology](./rag-evaluation.md) — corpora, metric definitions, and how to
  reproduce every number here
- [RAG Chat (Internals)](./rag-chat.md) — the pipeline as it exists today
- [Documents (Planned)](../features/documents.md) — Stage 6
- [Working Without an AI Model](../user-guide/without-an-ai-model.md) — the D6 deployment these
  decisions protect
