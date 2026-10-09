# #532 arm comparison

**Verdict: FAIL**

- category: `multi_file`; baseline: `mean(control, repeat-control)`
- bootstrap: 20000 resamples, seed 0, 95% CI
- rule: USED delta >= +0.05 with CI lower bound > 0; content CI lower bound > -0.03
- composition counters checked: True

## Arms

| arm | turns | in scope | cited | offered | mean USED | pooled USED | mean OFFERED | mean content | content turns |
|---|---|---|---|---|---|---|---|---|---|
| arm-d | 140 | 548 | 304 | 544 | 0.5560 | 0.5547 | 0.9929 | 0.2929 | 140 |
| control | 140 | 548 | 398 | 544 | 0.7274 | 0.7263 | 0.9929 | 0.3048 | 140 |
| hybrid | 140 | 548 | 161 | 544 | 0.2935 | 0.2938 | 0.9929 | 0.2863 | 140 |
| repeat-control | 140 | 548 | 414 | 544 | 0.7565 | 0.7555 | 0.9929 | 0.3042 | 140 |

## Paired differences (second minus first)

| comparison | n | delta | CI low | CI high |
|---|---|---|---|---|
| hybrid_vs_baseline_used | 140 | -0.4485 | -0.5167 | -0.3804 |
| hybrid_vs_baseline_content | 140 | -0.0182 | -0.0598 | +0.0244 |
| hybrid_vs_baseline_offered | 140 | +0.0000 | +0.0000 | +0.0000 |
| aa_used | 140 | +0.0292 | -0.0083 | +0.0696 |
| aa_content | 140 | -0.0006 | -0.0244 | +0.0232 |
| hybrid_vs_arm_d_used | 140 | -0.2625 | -0.3411 | -0.1827 |
| arm_d_vs_baseline_used | 140 | -0.1860 | -0.2545 | -0.1173 |
| hybrid_vs_arm_d_content | 140 | -0.0065 | -0.0512 | +0.0369 |
| arm_d_vs_baseline_content | 140 | -0.0116 | -0.0554 | +0.0324 |

## Decision

- used_delta_at_least_5_points_ci_low_above_zero: False
- content_ci_low_above_minus_3_points: False
- sanity_hybrid_beats_arm_d: False
- reason: used_coverage_below_threshold_or_ci_includes_zero
- reason: content_coverage_lost
- reason: hybrid_does_not_beat_arm_d
