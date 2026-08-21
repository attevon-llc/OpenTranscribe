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

⚠️ **Stack state:** superseded by the 2026-08-21 second campaign below — check the settings
rows on the stack (`chat.rag.%` in `system_settings`) rather than trusting any note here.

---

# Second campaign — 2026-08-21 (post-ELITR-injection, current build)

Judge calibrated first: **Cohen's Kappa 0.857 ("almost perfect")**, qwen3.8 judging gemma
answers vs an owner-authorized provisional grading of all 81 (#518, closed — labels are an AI
grader's; spot-check sheet + reopen rule recorded on the issue). Zero degraded parses across
every judge pass so far.

## ⚠️ The injection MOVED the floor — old and new numbers are not comparable

ELITR-Bench injection (18 files / 5,450 chunks) shifted index-wide document-frequency/length
statistics; the identical former-BEST config measured **11.5% before, 6.8% after** on scoped
retrieval. Every comparison below is within the post-injection corpus (2,268 files), one build
(`35213f11`), GIT_SHA verified per container by the scripted #528 check.

## AMI-81, three arms

| arm | config | AMI recall (ALL) | multi_file | judge content-bearing | neg | med lat |
|---|---|---|---|---|---|---|
| SHIPPED | 8k / 48 / 12 / 4 / rerank ON | **3.9%** | 4.1% | 54/81 (mf 14/25) | 6/6 | 49.3 s |
| budget, rerank ON | 60k / 48 / 40 / 12 / ON | **6.9%** | 7.4% | 58/81 (mf 16/25) | 6/6 | 55.9 s |
| budget, rerank OFF | 60k / 48 / 40 / 12 / OFF | **6.5%** | 6.8% | 60/81 (mf 18/25) | 6/6 | 56.0 s |

- **The budget finding REPRODUCES: ~1.8×** (3.9% → 6.5–6.9%), safety intact, +13% latency.
- **The judge agrees with the floor's ranking on all three arms**: shipped last (54/81),
  both budget arms ahead (58–60/81; multi_file 14 → 16–18 of 25).
- **The reranker-off gain does NOT reproduce.** Floor leans ON (+2 items), judge leans OFF
  (+2) — both within noise. **#531 revised: ship `final_chunks` 12→40 +
  `max_chunks_per_file` 4→12 only; leave `rerank_enabled` and `candidate_pool` untouched.**
- `single_general` refusals 0/25 on every arm — the earlier 0→1 blip did not recur.

## SHIPPED (2026-08-21): `final_chunks` 40 / `max_chunks_per_file` 12

Owner-approved on the evidence above plus the ELITR no-harm check below; rerank stays ON
(wash), `candidate_pool` stays 48 (widening measured harmful). The acceptance suite's basis
run moved to `ami81-postelitr-rerank` — the arm whose config IS the new shipped default —
and every floor was re-derived there (summaries 22/25, multi 16/25, due-outs 3/6 — a rise
from 2/6 — speaker 4/11). Deferred by owner decision, tagged `deferred` on the tracker:
#523/#526 (context expansion), #532 runs (arms coded, flags default-OFF), #506.

## ELITR-277 (second corpus, #521) — the direction check

Both arms **277/277 clean, negative controls 6/6 each** (one shipped-arm control was a regex
false-alarm on *"do not mention a 'Legal Counsel'"* — the same trap as before, correct refusal
when read). Token-F1 floor (medians), identical scorer both arms:

| arm | who | what | when | howmany | ALL med | ALL mean |
|---|---|---|---|---|---|---|
| shipped 8k / 48 / 12 / 4 | 0.057 | 0.115 | 0.118 | 0.078 | 0.087 | 0.124 |
| budget 60k / 48 / 40 / 12 | 0.053 | 0.113 | 0.105 | 0.106 | 0.087 | 0.121 |

**A WASH — not a confirmation, not a refutation.** Paired per-item: budget better on 93,
worse on 119, tie 59 (sign test p≈0.07). ELITR is single-meeting factoid QA — it does not
exercise the multi-file summary/aggregation shapes the budget change targets (per-file cap
4→12 did engage: `chunks_used` 4 → 12 on these turns — more context neither helped nor hurt
short factoid answers). What this run establishes is the **no-harm half**: the known trap
(a tuning that helps corpus A measurably degrading corpus B) did not occur, and grounding
held at the wider budget. The honest claim shipping under #531: the gain is demonstrated on
meeting-summary/aggregation shapes (AMI, two instruments); on single-meeting factoid QA the
change is neutral.

## Acceptance suite (#519) — floors now pinned as ratchets

`tests/eval/test_acceptance_query_shapes.py`, graded on the budget-OFF arm + judge:
summaries **92%** content-bearing · cross-meeting aggregation **68%** · **due-outs 2/6 — the
product's headline ask is its measured weakest slice** (→ #532-d) · speaker-scoped **5/11**
(→ #523) · map coverage 1.0 wherever a map ran · multi-file offered coverage ≥0.75 min,
22/25 full. Floors sit at measured values: regressions fail, improvements raise them.

## Also closed/landed this campaign

#517 (was already built as `mapreduce/coverage.py` — verified on live artifacts), #524, #528
(scripted GIT_SHA verify — caught the stack mixed-SHA live), #533 (context-window discovery —
measured qwen3.8 at **262,144** vs the 8,192 default it was being driven at), #536 filed
(`<recurrence>` vocabulary leaked into an answer). #532 arms (a)(b)(c) coded behind
default-OFF experiment flags (`e59a8e8f`), runs pending the post-#531 stack rebuild.
