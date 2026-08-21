# RAG answer-quality — measurement tracking

**Internal.** This directory is in the repo and versioned, but it is **NOT published** — Docusaurus
builds from `docs-site/docs/` only. Same status as `docs/benchmark-results/` and
`docs/diarization-boundary-results/`.

Keep it that way. These pages carry candid product-quality numbers mid-investigation, and the repo
is public. Anything that has settled and is safe to state publicly belongs in
`docs-site/docs/developer-guide/rag-evaluation.md` instead — deliberately, not by drift.

⚠️ **No dataset prose here.** QMSum/AMI question and reference text stays in `.rag-403/`
(gitignored). Metrics, config names and conclusions only.

## The goal these numbers serve

Accurate, grounded answers about transcripts — topics, summaries, people, details — **proven with
measurements**, not asserted. A null result is an acceptable answer.

---

## Status at 2026-08-21

**The chat is grounded and does not fabricate.** Negative controls (absent topics and absent
speakers) were refused **6/6 on every arm measured**, on two different models. That is the property
that matters most and it holds.

**Multi-meeting summary answers are incomplete, not wrong.** They carry a fraction of what a human
annotator listed. That fraction is what the tuning below moves.

## Measured arms

81 questions each, **81/81 clean on every arm**, same corpus (2,250 files / ~144,700 chunks), same
build, same model (`gemma-4-e4b`, local vLLM), one variable at a time.

Question set: 25 single-file specific + 25 single-file general + 25 multi-file + 6 negative
controls. 75 carry human references; all 25 multi-file are grounded in AMI's
`<decisions>`/`<actions>`/`<problems>`/`<abstract>` layers.

| ctx / pool / final_chunks / per_file / rerank | chunks | AMI recall | content cov | neg-ctrl | latency |
|---|---|---|---|---|---|
| **SHIPPED DEFAULT** 8k / 48 / 12 / 4 / on | 17.3 | **5.6%** | 20.3% | 6/6 | 47.5 s |
| 60k / 48 / 40 / 12 / on | 39.3 | 9.3% | 26.7% | 6/6 | 55.0 s |
| 60k / **128** / 40 / 12 / on | 42.5 | 6.5% | 23.3% | 6/6 | 54.1 s |
| **BEST MEASURED** 60k / 48 / 40 / 12 / **off** | 39.3 | **11.5%** | **29.3%** | 6/6 | 53.9 s |

**Shipped → best is 5.6% → 11.5% (2.05×), safety 6/6 throughout, +13% latency.**

> **AMI content recall is a FLOOR.** `tests/eval/harness/ami_recall.py` scores lexical overlap, so
> "priced at twenty-five euros" against "25 Euro dollars" counts as a **miss**. It can under-report
> a good answer but cannot over-report a bad one — the direction an automated metric must fail in
> when used to argue a change helped. Rank arms with it. Absolute figures come from the calibrated
> judge, and only once its Kappa is known.

## Three findings that are not obvious

1. **The context window is NOT the constraint.** 8k → 60k gave 20× the prompt room (`budget_chars`
   8,788 → 172,020) and `chunks_used` stayed **byte-identical at 17.3** in every category.
2. **Widening `candidate_pool` HURTS** (48 → 128: 9.3% → 6.5%). A wider pool admits weaker
   candidates that dilute the evidence. `rerank_max_pairs` was raised with it, so this is not the
   known un-reranked-tail artifact.
3. **The reranker hurts ANSWERS too.** Disabling it is the single best change measured
   (9.3% → 11.5%, latency unchanged), corroborating the earlier retrieval-only finding.

**The order the ceilings bind:** `max_chunks_per_file` → `final_chunks` → context window. With ~4
files in scope and a per-file cap of 4, the chunk plane yields at most **4×4=16** chunks however
large `final_chunks` is.

## A bigger model does not fix it

`qwen3.8:27b` scored **5.2%** vs `gemma-4-e4b`'s **5.6%** on the same questions and window — the gap
is **ours**, not a model ceiling. ⚠️ Caveat: qwen completed only 6 of 25 multi-file questions
cleanly (watchdog timeouts), so this is suggestive, not conclusive.

## What is NOT yet true

- **Not shipped as defaults.** One corpus, one model, floor metric. Gated on judge calibration and
  a second corpus.
- **`single_general` moved 0/25 → 1/25 refusals** in one arm — re-check before shipping.
- **The OFFERED-vs-USED gap is unexplained**: retrieval offers 99% of scope, citations use 75%.

## Traps — do not repeat these

- **`LLMConfig.max_tokens` IS the context window**, default 8192. Four complete runs measured at
  ~1/20th the available budget before it was noticed.
- **A setting you sent is not a setting that applied.** The probe reused a config matched by
  `base_url` and silently discarded the requested window — a fake null result that argued *against*
  the change. It now reconciles, reads the value back, and refuses to run on mismatch.
- **`files_consulted` is citation-derived**, mixing deterministic retrieval with stochastic model
  behaviour. Use `offered_citations` for scope coverage.
- **A narrow refusal regex cried hallucination twice** — *"there is no speaker named X"* and *"do
  not **include** a speaker named X"* are correct refusals, both written up as safety regressions
  before the sentences were read.
- **Raw agreement is not calibration** — on skewed labels it overstates by 30+ points.
- **The measurement stack is BAKED.** Every `backend/app/**` change needs rebuild +
  `--force-recreate` there, and `GIT_SHA` must be verified on the backend and every celery worker.

## Reproducing

```bash
python3 scripts/probe_chat_rag.py --port 5274 \
  --question-set <question-set.json> --concurrency 4 \
  --llm-provider vllm --llm-base-url http://llm-test-vllm:8000/v1 \
  --llm-model gemma-4-e4b --llm-max-tokens 60000 \
  --out .rag-403/probe-runs/<arm> --metrics-out .rag-403/probe-runs/<arm>-metrics
```

`--out` carries prose and is gitignored. `--metrics-out` is the metrics-only artifact.

⚠️ **Stack state:** the measurement stack is currently left in the **BEST** arm
(`final_chunks=40`, `max_chunks_per_file=12`, `rerank_enabled=false`, window 60000), **not** the
shipped default. Restore before any unrelated baseline or the run is silently non-comparable.
