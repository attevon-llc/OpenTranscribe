# backend/tests/eval — the RAG evaluation harness

Moved intact from `backend/tests/CLAUDE.md`. Design and the traps of measuring RAG quality: `docs-site/docs/developer-guide/rag-evaluation.md`.

## `tests/eval/` — the RAG evaluation harness (issue #403 Stage 1)

Not a test suite that gates anything: it is the **instrument** every retrieval-affecting change
reports against (D5), plus the tests that keep the instrument honest. `synthetic/` generates a
corpus with ground truth known by construction; `harness/` measures.

| Module | Owns |
|---|---|
| `harness/metrics.py` | `trec_eval` via `pytrec_eval_terrier`. Tie normalisation, `-c` semantics, linear gain — all three are NOT the library default |
| `harness/qrels.py` | gold turn ranges -> chunk-level graded judgements. **One adapter for QMSum and the synthetic tier** — they share the inclusive turn-range convention deliberately |
| `harness/corpora.py` | queries + gold, remapped onto the uuids the app indexed, with the licence tier attached |
| `harness/index_reader.py` | **settle** (complete + stable + nothing predating the run) -> refresh -> force-merge -> refresh, then read chunks back |
| `harness/runner.py` | drives `retrieve_chunks` (the chat path), never `/api/search`; owns the per-request fusion arm and the 48/12/4 budget |
| `harness/report.py` | the deterministic results document and metric table |

```bash
./opentr.sh bench rag --fresh rag403              # the one command, <5 min
pytest tests/eval -q                              # logic tests, nothing running
OPENSEARCH_PORT=5280 pytest tests/integration/test_rag_eval_harness.py -m integration
```

Three things to know before touching it:

- **The metric engine is an eval-only dependency** (`backend/requirements-eval.txt`) for a
  **licence** reason, not a size one: trec_eval's C sources carry a "research, non-commercial
  purposes" header and we publish images. Never move it into `requirements.txt`. Every module that
  imports it does so lazily and every test `importorskip`s it with that reason.
- **`normalise_run` is load-bearing, not tidiness.** trec_eval breaks ties by docid *descending*,
  our ids are `{uuid}_{chunk_index}` and `{uuid}_digest_{n}`, and RRF produces ties structurally —
  so an untied-broken run lets a stage pass its own gate on document naming. `test_eval_metrics.py`
  swaps the id convention and asserts the metric is unchanged, with a guard test proving the
  hazard is real; it reads the digest id from `index_mapping.digest_document_id` rather than
  spelling it, because it was already guarding a scheme the app had moved past once.
- **A measurement is refused unless the corpus has settled.** `await_settled` requires every
  expected file to carry chunks, the (files, chunks) pair to repeat across two polls, **and** —
  when a dispatch timestamp is passed — nothing in the corpus to predate the run. The first two
  alone are satisfied by a reindex that has been dispatched and has not started, which certifies
  the old index as the new one. Polling the chunk total alone produced phantom deltas of
  223 / 357 / 591 chunks over an unchanged corpus.
- **A sweep arm is one flag combination, and the results file names it.** `--fusion` /
  `--rank-constant` / `--normalization-technique` / `--combination-technique` /
  `--combination-weights` select the hybrid fusion strategy per run (#363); `--size` /
  `--final-chunks` / `--max-per-file` / `--rerank-max-pairs` are the 48/12/4 budget, whose
  defaults are pinned to the shipped `chat.rag.*` constants by `test_eval_fusion_arm.py` (they
  used to be 20/3 — every `--stage rerank` number described a deployment nobody runs).
  `metrics.json`'s `retrieval.fusion` records the *resolved* strategy and pipeline id, so no arm
  can be unattributable; per-query latency lands in `runinfo.json`, outside the deterministic
  claim. **Never quote a single latency run** — one put `rrf-60` at +50% p95 and it did not
  reproduce.
- ⚠️ **`--stage rerank` is scored in the order the PROMPT receives, not by score.**
  `_to_run_docs(..., preserve_order=True)`. `normalise_run` re-sorts by `-score`, which is right
  for `retrieve` and wrong after `diversity_sample`, whose job is to interleave files — the
  re-sort undid that for **40 of 60** measured queries and made the scored top-5 depend on
  `final_chunks`, which provably cannot move the prompt's top-5. It also let the *un-reranked*
  tail outrank reranked hits whenever `candidate_pool > rerank_max_pairs`, because `rerank`
  leaves the tail on RRF scores (small positives) while cross-encoder scores are routinely
  negative. Production was never affected; it walks list order.
- **`scripts/reindex_eval_corpus.py`** dispatches the real `reindex_transcripts` task and waits
  for the settle. Two consecutive runs must report the same chunk count; that equality is the
  precondition for any phase-over-phase delta, and it is measured, not assumed.
- **Baselines under `tests/eval/baselines/` are committed controls.** `metrics.json` and
  `metrics.md` are byte-identical across runs by construction; anything non-deterministic
  (elapsed time, target) lives in `runinfo.json`, outside the claim. Regenerate one only when the
  corpus composition genuinely changes, and say so in the PR.
- **`scripts/probe_chat_rag.py` (issue #72) is the one tool in this family that drives the real
  chat HTTP path** (login -> scoped conversation -> POST message -> SSE -> re-fetch
  `msg_metadata`) against a real LLM, rather than calling `retrieve_chunks` in-process. Its
  question sets are supplied at runtime via `--question-set` (a JSON file, never hardcoded) so the
  tool itself carries no licence-encumbered content; `--metrics-out` writes a metrics-only artifact
  (`harness/probe_metrics.py`, `assert_no_prose` enforced) that IS safe to commit, while `--out`'s
  full-fidelity report (question/reference/answer prose) is NOT. Two environment gotchas
  (vLLM's `docker network connect --alias`, and the CSRF double-submit header) and the committed
  `probe-chat-live-2026-08-20` baseline are documented in
  `docs-site/docs/developer-guide/rag-evaluation.md`'s "Live chat-RAG HTTP probe" section — read it
  before pointing this at a fresh stack, or the first mutating request 403s with no obvious cause.

## Summary-merge quality gate

The instrument for "is the independently written scope/merge rule set at least as good as the current one".
Pure scorers in `harness/summary_{detectors,scoring,verdict,judge}.py`, run by
`scripts/benchmark_summary.py` (package `scripts/summary_bench/`) through
`scripts/run-summary-eval.sh`; the GPU window and the lead's arm files are the only things it
needs that this repo does not hold.

The real-corpus gold counts `test_summary_gold.py` reproduces (decisions, with linked in parentheses): ES2008 33 decisions (32) and 17 actions (16); Bed003 8 (7); Bmr023 7 (7).

- **Arms are runtime inputs, never repo content.** Rule text lives in JSON files outside the repo
  (`~/ot-cleanup-work/summary-eval/arms/`); a results file holds a SHA-256 and a length.
  `test_summary_artifacts.py` runs a sentinel arm end to end and searches every file written.
- **VOID is not a failure to read around.** A missing cell, an arm that never reached a system
  message, a negative control (`sabotage`) that was not caught, or an A/A control that tripped a
  rule voids the run; `summary_verdict.decide` decides that before comparing `new` to `old`.
- **Gold comes from the NAS corpora** (`nxt_summaries.py`); `test_summary_gold.py` reproduces
  the plan's counts and skips with a reason when `/mnt/nas/opentranscribe-benchmarks` is absent.
- Tests that need a harness module use `tests/eval/summary_bench_support.py`.

Methodology, the overlap->relevance rule, and the committed numbers:
`docs-site/docs/developer-guide/rag-evaluation.md`.
