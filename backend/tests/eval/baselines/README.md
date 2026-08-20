# Committed baselines — and which of them name their own embedding model

A retrieval number is a statement about a corpus **and** the model that vectorised it. Swap the
model and the same code over the same corpus produces a different number, so a baseline that does
not name its model cannot be compared to anything.

`metrics.json`'s `index` block carries that provenance, sourced from the **documents** rather than
from the settings table (issue #437: `search.embedding_model` and `search.opensearch_model_id` are
written by different endpoints with nothing reconciling them, so a settings-derived label can name
a model that never touched a vector):

| field | meaning |
|---|---|
| `embedding_models` | the models the indexed documents themselves report, sorted |
| `embedding_verdict` | `empty` / `unattributed` / `uniform` / `partially_unattributed` / `mixed` |
| `embedding_unattributed` | documents in the `"neural"` UNKNOWN bucket |
| `embedding_dimension` | the index's `knn_vector` dimension |
| `configured_embedding_model` | what the settings *say* — kept separately so drift is visible rather than collapsed |

The harness **refuses (exit 3)** to write a baseline over a proven-mixed vector space — two *named*
models — because cosine between two models is not a similarity, so the ranking being scored fused
two incomparable populations.

## Status of each baseline

Measured against the `otfresh-rag403` stack, index `_meta.version` 6, 210,908 documents.

| baseline | index when measured | provenance | status |
|---|---|---|---|
| `stage3-index-v6` | 210,908 | **recorded** | **re-derived** — reproduces bit-for-bit |
| `stage4-control` | 210,908 | **recorded** | **re-derived** — reproduces bit-for-bit |
| `stage4-routed` | 210,908 | **recorded** | **re-derived** — reproduces bit-for-bit |
| `stage4-aggregation` | 210,908 | **recorded** | **re-derived** — reproduces bit-for-bit |
| `stage1-baseline` | 119,950 | none, unrecoverable | **historical** — do not regenerate |
| `stage1-baseline-goldscope` | 119,950 | none, unrecoverable | **historical** — do not regenerate |
| `stage1-synthetic-answers` | 208,333 | none, unrecoverable | **historical** — do not regenerate |
| `stage3-control-pre-v6` | 208,332 | none, unrecoverable | **historical** — do not regenerate |
| `stage4-router` | — | **not applicable** | classifier only; touches no index |

### Why the historical four are kept and not regenerated

Each measured an index that **no longer exists**, so re-running the same command today would not
re-derive them — it would silently replace a measurement of one index with a measurement of a
different one, under the old name. That is precisely the failure this directory exists to prevent.

- **`stage1-baseline` / `stage1-baseline-goldscope`** — 232 QMSum meetings, 119,950 chunks, before
  the v6 reindex and before three determinism fixes. They are the evidence for the epic's most-cited
  result: corpus-wide nDCG@10 **0.1052** against **0.3296** with an oracle gold-file scope, i.e.
  *roughly two thirds of the loss is picking the wrong recording*. `stage1-baseline` is also
  `--control-name`'s default and the `--compare` target the docs name.
- **`stage1-synthetic-answers`** — 208,333 chunks, pre-v6 and pre-determinism-fix. Its synthetic
  rows differ from `stage3-control-pre-v6`'s over what is otherwise the same corpus (MRR −0.0106 on
  the `all` row), which is the reproducibility band those fixes closed. It also holds
  `answers-null-control.md`, the **0.0000 EM / 20 unanswered** floor that exists nowhere else and is
  what makes the 1.000 beside it a measurement rather than a tautology. That floor is structurally
  index-independent — the `none` answerer declines every query — so it needs no provenance.
- **`stage3-control-pre-v6`** — the *before* arm of the v6 A/B, and historical by definition: index
  v6 is what replaced it. Deleting it deletes the control for every Stage 3 delta.

**None is obsolete.** Nothing here was deleted.

### Their embedding model is unknowable, not merely unrecorded

The cluster cannot answer for them retroactively. All 210,908 documents carry
`embedding_model: "neural"` — #437's single UNKNOWN bucket, kept deliberately as *one* unknown
rather than backfilled with the current model, which would assert something nobody can know. The
only evidence for the historical four is circumstantial: a 384-dimension index and a configured
`huggingface/sentence-transformers/all-MiniLM-L6-v2`.

So the current verdict on **every** baseline, re-derived ones included, is `unattributed`. What the
re-derived four now record is not "which model", but the auditable fact that **nobody can tell** —
which is a claim a later comparison can check, where silence was not.

## `probe-chat-live-2026-08-20` — a different instrument, a different schema

Every baseline above is scored by `scripts/benchmark_rag.py` against `retrieve_chunks`/
`retrieve_digests` in-process, and their `metrics.json` share one schema (`rows`, `qrels`, the
`index`/`retrieval` provenance blocks). `probe-chat-live-2026-08-20/metrics.json` is NOT that
schema — it comes from `scripts/probe_chat_rag.py` (issue #72), which drives the **real chat HTTP
path** end to end against a **real LLM**, and its `metrics.json` carries `rows`/`summary`/`target`
as `tests/eval/harness/probe_metrics.py` defines them: per-turn query id, category, scope size,
files consulted, chunks used, retrieved count, warning codes, and the derived coverage ratio
(`files_consulted / scope_size`). **Do not diff it against a retrieval baseline** — different
instrument, different measures, not comparable row for row.

It records a 14-question run against `otfresh-ragmeas` and a Gemma vLLM instance
(`gemma-4-e4b`), converted offline from the raw probe output through
`probe_metrics.build_probe_results` — see `docs-site/docs/developer-guide/rag-evaluation.md`'s
"Live chat-RAG HTTP probe" section for the full write-up, including why no question text,
reference answer, or answer prose is present (QMSum licence ambiguity; `assert_no_prose` enforces
it). Headline finding: the four `multi_file` questions consulted **3/4, 3/4, 2/4, 2/4** of their
scoped files. Regenerate only by re-running the probe and re-converting through the same
function — never by hand-editing the numbers.

## Re-deriving one

```bash
./opentr.sh bench rag --fresh rag403 --corpus qmsum --corpus synthetic --control-name stage4-control
```

`runinfo.json` is gitignored on purpose: it holds elapsed seconds, which cannot be byte-identical
across runs and is therefore outside the claim. `metrics.json`, `metrics.md` and `answers.md` are
byte-identical across runs over an unchanged index — verified, not assumed.
