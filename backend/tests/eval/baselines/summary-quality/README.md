# Summary-merge quality baselines

Metrics-only results of `scripts/run-summary-eval.sh` (design:
`docs/design/summary_quality_eval_plan.md`). One subdirectory per run, named
`<date>-<git short sha>`, holding exactly:

| file | contents |
|---|---|
| `inputs.lock.json` | per input: the locked `max_tokens`, each slice's length and SHA-256, the transcript SHA-256, and the gold item ids per slice. Hashes, lengths and counts only |
| `metrics.json` | the verdict, every threshold, per-input per-arm aggregates, arm hashes, model and judge provenance. `assert_no_prose` runs before it is written |
| `metrics.md` | the same numbers as a table, generated from `metrics.json` |

Never committed here: `records.jsonl`, `scores.jsonl`, `judge.jsonl`, `calibration/`. They hold
corpus-derived prose and live under `~/ot-cleanup-work/summary-eval/runs/`.

A run directory is published with `benchmark_summary.py report --publish <dir>`, which copies
only the three files above.

## Sources and attribution

All inputs are Tier A and the metrics derived from them may be published with attribution:

- AMI Meeting Corpus: Carletta et al. 2005, CC BY 4.0. Human decision/action annotations and
  dialogue-act links from the AMI manual annotations release 1.6.2.
- ICSI Meeting Corpus: Janin et al. 2003, CC BY 4.0. Same layers, from ICSI plus NXT.
- QMSum: Zhong et al., NAACL 2021 (arXiv:2104.05938), MIT. The speaker-attributed turns of the
  AMI and ICSI meetings used here.

MeetingBank and ELITR are Tier B: no metric derived from them may ever be written to this
directory. Provenance rules for the rest of the baselines: `../README.md`.
