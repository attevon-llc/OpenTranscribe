---
sidebar_position: 10
title: RAG — Prior Art and the Package Ledger
description: How other systems summarize large corpora, which industry patterns we implement, and an auditable record of every package we adopted or rejected and why
---

# RAG — Prior Art and the Package Ledger

:::info This is a living document
It is written to be **re-read and revised as the work lands**, not archived. Every claim here is
either measured, cited, or explicitly marked as unverified. When a stage ships and changes one of
these answers, change it here in the same PR.

Last substantive review: **2026-08-13**, during issue&nbsp;#403 Stage 4 design.
:::

Three pages cover retrieval design; this is the fourth, and it answers the question the others
assume: **what already exists, and why did we build anything at all?**

| Page | Question |
|---|---|
| [RAG Chat (Internals)](./rag-chat.md) | How does the pipeline **work**? |
| [RAG Evaluation Methodology](./rag-evaluation.md) | How is quality **measured**? |
| [Design Decisions](./rag-design-and-validation.md) | **Why** this shape, and what nearly fooled us? |
| **This page** | What is the **prior art**, and what do we **import versus write**? |

## Part 1 — How other systems summarize a large corpus

### The canonical taxonomy

There are five patterns in general use. All of them are well-named, and using the wrong name for
one of them is how teams end up building a sixth by accident.

| Pattern | What it does | Where it breaks |
|---|---|---|
| **Stuff** | Put every document in one prompt | The context window. Hard stop. |
| **Map-reduce** | Summarize each chunk (map), then summarize the summaries (reduce) | Loses cross-chunk narrative; reduce prompt can itself overflow |
| **Refine** | Carry a running summary forward, chunk by chunk | Strictly sequential — no parallelism, latency scales with corpus |
| **DocumentSummaryIndex** | Precompute a per-document summary; use it as the *retrieval handle* | Summaries go stale when the document changes |
| **Hierarchical / RAPTOR** | Recursively cluster and summarize into a tree; retrieve at any level | Build cost; clustering quality is corpus-dependent |

**Map-reduce and `tree_summarize` are the same pattern.** The LlamaIndex documentation states the
equivalence outright — *"In LlamaIndex this is referred to as `tree_summarize`, in LangChain this is
referred to as map-reduce"* — and they differ only in recursion depth: LangChain's classic
map-reduce fans in once, `tree_summarize` repeats the fan-in until one root remains. If you find
yourself designing a "novel" summarization strategy, check first that it is not one of these
five wearing different vocabulary.

### What Open WebUI actually does

Open WebUI is the most common open-source point of comparison, so it is worth being precise: **it
does not solve corpus-scale summarization, and its maintainers say so.**

It offers two modes, neither of which is summarization:

1. **Default RAG.** Chunk the document, embed, retrieve top-k, send those chunks. Users
   consistently report thin, partial summaries, because top-k retrieval answers "which passages
   match this query" — a fundamentally different question from "what does this document say."
2. **Full Context Mode.** Send the entire document. Works until the context window, then
   truncates. Practically it requires an 80K–256K model plus the VRAM to serve it.

There is an [open proposal](https://github.com/open-webui/open-webui/discussions/19177) to pick
between the two automatically by token threshold, and an
[older issue](https://github.com/open-webui/open-webui/issues/3129) observing that a real
summarization pipeline "requires significantly more implementation effort." **It has not been
built.** A widely-reported symptom is that the same model which summarizes a PDF well in ChatGPT
returns a stub in Open WebUI — the model is not the variable, the retrieval strategy is.

This matters for our roadmap in one specific way: on the "summarize everything" query class there
is **no open-source prior art to import**. We are not behind it.

### Closed-source systems

Less can be stated with confidence here, so less is stated. What is publicly documented: the
frontier assistants lean on very long contexts plus agentic chunked reading rather than a fixed
map-reduce chain, and Anthropic has published *contextual retrieval* — prepending
chunk-specific context to each chunk **before** embedding, which is cheap and improves retrieval
independently of the summarization strategy. That last one is adjacent to our Stage 5 work and is
**not yet evaluated here**; treat it as an open idea, not a decision.

## Part 2 — The package ledger

The standing rule in this repository is **do not hand-roll what a package already does well**. The
rule that qualifies it is **do not import a framework to obtain a control-flow pattern**. Most
disagreements about "should we use library X" dissolve once you ask which of those two applies.

The test we apply, in order:

1. **Does the package do genuinely hard work?** Format parsing, tokenization, ranking metrics,
   embeddings, OCR — yes. Fan out N calls and combine the results — no.
2. **Does it duplicate a layer we already run?** Our embeddings execute *inside OpenSearch* via ML
   Commons, and fusion is OpenSearch-native. A framework that brings its own vector store and
   retriever adds a second implementation of a layer we already have.
3. **What does it cost the published image?** We publish containers. Dependency weight and
   licence are release concerns, not preferences.
4. **Is the licence compatible with publishing?** Not just with using. See `trec_eval` below.

### Adopted — packages doing the hard work

| Job | Package / service | Note |
|---|---|---|
| Lexical ranking (BM25) | **OpenSearch** | Native; not reimplemented |
| Dense vectors | **OpenSearch ML Commons** + `all-MiniLM-L6-v2` (384-dim) | Embeddings run **in the cluster**, not in Python |
| Hybrid fusion | **OpenSearch `score-ranker-processor`** (RRF, `rank_constant` 30) | Native Reciprocal Rank Fusion |
| Sentence splitting | **nltk punkt** | One splitter shared by the transcript chunker and digests — see the note below |
| Retrieval metrics | **`pytrec_eval_terrier`** (NIST trec_eval C code) | nDCG@10 / recall@k / MRR |
| LLM serving | **vLLM** | Gemma 4 E4B |
| Reranking seam | `reranker.get_reranker()` | Deliberately a seam; the model is Stage 5's bake-off |

:::warning trec_eval is an eval-only dependency for a licence reason, not a size one
Its C sources carry a "research, non-commercial purposes" header, and **we publish images**. It
lives in `backend/requirements-eval.txt`; every module that uses it imports lazily and every test
`importorskip`s it with that reason recorded. Never move it into `requirements.txt`.
:::

### Rejected — and the specific reason

These are recorded so they can be **overruled with evidence** rather than re-argued from scratch.

| Package | Rejected because |
|---|---|
| **LangChain** summarization chains | Three reasons, in increasing order of how much they cost us. (a) It brings a framework to obtain a control-flow pattern — fan out N calls, combine the results — which is test 1. (b) The classic chains (`load_summarize_chain` with `stuff` / `map_reduce` / `refine`) are **deprecated in favour of LangGraph**, so adopting them means adopting a migration we did not need. (c) The decisive one: **every chain assumes the map step is an LLM call.** Ours is not — the per-file map output is the extractive digest, computed deterministically at ingest, which is exactly what makes a summary over 1,000 recordings cost zero map-time work. A chain cannot express "the map already happened", so adopting one would have meant paying for the thing the design exists to avoid. We reuse the *shape* (bounded thread pool, pre-filled error slots, index-keyed results) and none of the code. |
| **LlamaIndex** response synthesizers | Duplicates a layer we already run (test 2): its value is ingestion, vector stores, and retrievers, and ours live in OpenSearch. We adopt its **vocabulary** — `tree_summarize`, `DocumentSummaryIndex` — and name our components accordingly. |
| **`semantic-router`** | Routes by embedding similarity, i.e. a client-side encoder call per turn **on the critical path**; and its base dependencies pull `litellm` + `openai` + `aiohttp` + `tiktoken`. Our routing requirement is explicitly rules-first with zero LLM calls before retrieval on turn 1. |
| **RAPTOR** (for now) | Genuinely additive and not rejected on principle — see below. Deferred until it can be measured against a Stage 4 baseline that does not yet exist. |

### Written ourselves — and why that is the right call

Three components are ours, and in each case the reason is the same: **they are the transcript-aware
part.** A generic package cannot know about speaker turns, and speaker turns are the product.

- **Speaker-turn chunking.** Every general-purpose chunker splits on characters, tokens, or
  sentences. Ours splits on *who is talking*, which is why a retrieved passage is a coherent
  exchange rather than a window that begins mid-sentence in one speaker and ends mid-sentence in
  another. This is the differentiator, not an implementation detail.
- **Timestamp-anchored citations.** A citation resolves to a real position in the media, so a
  claim in an answer is clickable back to the audio.
- **Rename propagation.** Renaming a speaker updates the indexed text, because the indexed text
  contains the name.

:::note Sharing beats reimplementing, even internally
When the document chunker needed to split over-long blocks, it imported
`chunking_service.split_into_sentences` rather than writing a third splitter — the transcript
chunker and the digest builder already shared it. Three splitters over the same words means three
sets of boundaries and three sets of off-by-one bugs. The same test applies inside the codebase as
outside it.
:::

### Checking the novelty claim: pre-LLM prior art exists, modern RAG prior art does not

"A generic package cannot know about speaker turns, and speaker turns are the product," above, is
a claim about *retrieval*, so it deserves the same scrutiny as anything else on this page: has
anyone already built this?

**No citable modern RAG system treats speaker identity as a first-class retrieval signal** —
something that filters, boosts, or routes a query, the way `speaker_focus_names` does in
`chat/speaker_resolver.py`. The adjacent systems stop short of it in a specific, checkable way:

- **SA-ASR** (speaker-attributed ASR) stops at producing a *labelled transcript*. Attribution ends
  at the transcription step; nothing downstream retrieves by it.
- **MeetingQA** (ACL 2023, [aclanthology.org/2023.acl-long.837](https://aclanthology.org/2023.acl-long.837))
  cares about speakers in the **answer** — "what did X say" is a question type it evaluates — but
  does not retrieve *by* speaker; the retrieval stage is speaker-blind.
- **Backtracing** ([arXiv:2403.03956](https://arxiv.org/abs/2403.03956)) renders speaker identity
  as text fed to a likelihood retriever — a feature engineered into the input, not a structural
  filter/boost/route the way a numeric `speaker_id` axis is.

**There is genuine prior art, just not from the RAG era.** The late-1990s/2000s spoken-document-retrieval
(SDR) literature has patents combining content and speaker indexes: **US6345252** ("retrieving
audio information using content and speaker information") and **US6434520** (voiceprint-based
segment retrieval). The idea of indexing audio by who-said-it alongside what-was-said is roughly
20 years old.

**Write this honestly, because an absence claim needs its evidence stated:** what appears
unexplored is applying that idea *inside a modern hybrid BM25/kNN RAG pipeline over ASR +
diarization output* — not the underlying idea of speaker-indexed audio retrieval, which predates
this project by two decades. The search behind this finding was English-language web search only,
roughly 15 queries, with no ACL Anthology index crawl and no patent-database search beyond
locating the two numbers above. Treat "unexplored in modern RAG" as the current best read of an
incomplete search, not as an exhaustively verified negative.

## Part 3 — What we implement, in industry terms

So that the mapping is unambiguous:

| Our component | Industry name |
|---|---|
| Hybrid chunk retrieval | BM25 + dense kNN fused by **Reciprocal Rank Fusion** |
| The digest plane (Stage 3) | **DocumentSummaryIndex** / parent-document (small-to-big) retrieval |
| The query router (Stage 4) | **Query routing** — "route, don't fuse" |
| Two-level summarization (Stage 4) | **Map-reduce** = **`tree_summarize`** — see [the map step is a read](#the-map-step-is-a-read-not-a-call) |
| Rerank stage (Stage 5) | **Two-stage retrieve-then-rerank** |

**We built the standard patterns. For a while we simply were not calling them by their names** —
the digest plane was `DocumentSummaryIndex` before anyone wrote that down. Naming them is not
cosmetic: it is how a reader knows which known failure modes apply.

### The map step is a read, not a call

Our `tree_summarize` differs from every published implementation in one respect, and it is the
respect that makes corpus scale tractable:

```
transcript chunks ──(TextRank, at ingest, NO LLM)──▶ file digest      ← the MAP
file digests      ──(code, or N small bounded calls)─▶ collection view ← the REDUCE
```

Level 1 already ran when the file was ingested. A summary over 1,000 recordings therefore costs
**zero** map-time work — the map is a database read. That is the whole reason the digest is
deterministic and extractive rather than LLM-written: a map step that needed a model would leave
the corpus-scale case exactly as impossible as it was before.

Two levels only, deliberately. Not recursive — see RAPTOR below.

### Ranking picks the best passages; mapping covers every document

**This distinction is load-bearing, it is not obvious, and it was learned by shipping the wrong
one.** Anyone reading `mapreduce.scope_digest_hits` will notice that a ranked digest retrieval
already exists (`retrieve_digests`) and wonder why the map does not simply use it. It did, once.

Asked for 50 digest sections over a **25-file** scope, the ranked leg returned 50 sections drawn
from **8 files** — sections cluster by relevance, which is precisely what a ranker is for. The
composed overview was therefore headed `recordings: 8`, and the model faithfully answered *"The
recordings cover 8 vendor review board sessions"* over a scope of twenty-five. Nothing looked
broken. The number was simply wrong, and confidently so.

The two operations are not interchangeable:

| | question it answers | correct behaviour |
|---|---|---|
| **Ranking** (`retrieve_digests`) | "which passages best match this query?" | return the top K by relevance, wherever they cluster |
| **Mapping** (`scope_digest_hits`) | "what is in each document?" | return one summary **per document**, ignoring relevance entirely |

So for a **bounded** scope the map reads `file_facts` for every file in it and ignores ranking. The
ranked leg survives only for the unbounded "all accessible" case, where mapping over everything is
not possible — and there the header says how much it covered rather than reporting the covered
count as the total.

:::danger Do not "simplify" the map back to the ranked leg
It will look like a redundant second retrieval path and it is not. Increasing `size` does not fix
it: ranking gives you no coverage guarantee at **any** K. `tests/unit/test_chat_mapreduce.py::test_the_scope_map_covers_every_file_not_the_best_ranked_ones`
fails if the map is replaced by a ranked retrieval.
:::

### No eval framework catches this bug — surveyed for that reason

The `recordings: 8` failure above is not only something none of our own tests caught until one was
written for it — it is something an adopted evaluation framework could not have caught either, on
purpose, because of what those frameworks measure. Six were surveyed, following the same
adopted/rejected discipline as the package ledger in Part 2:

| Framework | Rejected because |
|---|---|
| **RAGAS** `Context Recall` | Scores groundedness against whatever context was **retrieved**, never against the scope a query claimed to cover. Would grade the `recordings: 8` run **perfect** — every claim genuinely was grounded in the 8 files the map step saw. The failure is invisible to a per-claim metric by construction. |
| **DeepEval** `Coverage` | Completeness of **one** generated summary against **one** source document — not scope across N files. The name invites exactly the wrong conclusion for this use case: it answers "did the summary miss anything the document said," never "did the system look at every document in scope." |
| **ARES** ([arXiv:2311.09476](https://arxiv.org/abs/2311.09476), Stanford, NAACL 2024) | Worth naming for what distinguishes it: prediction-powered inference (PPI) propagates the judge's own uncertainty from a small human-labelled calibration set, rather than trusting a single LLM-judge score outright. Still scores per-claim groundedness against retrieved context — the calibration is orthogonal to the scope-coverage gap. |
| **TruLens**, **FActScore**, **continuous-eval** | Same shape as RAGAS/DeepEval: per-claim or per-passage groundedness against retrieved context, with no notion of the scope a map step was supposed to cover. |
| **SummHay** ("Summary of a Haystack", Salesforce, [arXiv:2407.01370](https://arxiv.org/abs/2407.01370), EMNLP 2024) | The one surveyed artifact whose design is structurally right for this: a known ground-truth insight→document map, scored against what the system actually cited. Not adoptable as-is — a synthetic, **research-purposes-only** benchmark, not a runtime metric. It can inform how a coverage check is *designed*; it cannot replace one run in CI. |

**Conclusion:** this is why `mapreduce.scope_digest_hits`'s coverage guarantee
(`test_the_scope_map_covers_every_file_not_the_best_ranked_ones`, above) is our own assertion
rather than an adopted framework — and it needs no LLM at all, because `files_touched ==
files_in_scope` is a count, not a judgement.

### RAPTOR — the one open idea worth measuring

RAPTOR builds a recursive summary tree by **clustering nodes semantically** before summarizing,
and indexes every level so a query can retrieve at whatever abstraction it needs. It is the natural
generalization of our digest plane.

The reason it is interesting *here* specifically: RAPTOR clusters because it has no better grouping
signal. **We have one** — real speaker turns, meeting boundaries, and timestamps. Applying the
product's actual moat to the summarization tier, rather than to retrieval alone, is a genuinely
novel direction and a candidate for the whitepaper.

The reason it is not scheduled: the RAPTOR paper reports **no significant gain** from its
clustering variant over the simpler sequential approach. It has to beat a Stage 4 baseline that
does not exist yet. Measure first.

## Sources

- [Open WebUI — RAG documentation](https://docs.openwebui.com/features/chat-conversations/rag/)
- [Open WebUI — full context mode discussion](https://github.com/open-webui/open-webui/discussions/17656)
- [Open WebUI — automatic RAG/full-context strategy proposal](https://github.com/open-webui/open-webui/discussions/19177)
- [Open WebUI — whole/full document mode issue](https://github.com/open-webui/open-webui/issues/3129)
- [The Open WebUI RAG conundrum: chunks vs. full documents](https://demodomain.dev/2025/02/20/the-open-webui-rag-conundrum-chunks-vs-full-documents/)
- [LlamaIndex — building response synthesis from scratch](https://developers.llamaindex.ai/python/examples/low_level/response_synthesis/)
- [LangChain summarization: stuff, map_reduce, refine](https://medium.com/@abonia/summarization-with-langchain-b3d83c030889)
- [RAG summarization patterns and long-context tradeoffs (2026)](https://futureagi.com/blog/rag-summarization/)
- [Hierarchical indexes using LlamaIndex for RAG content enrichment](https://sujitpal.blogspot.com/2024/03/hierarchical-and-other-indexes-using.html)
