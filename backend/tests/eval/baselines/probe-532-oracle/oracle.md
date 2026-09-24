# #532 Phase 0 offline content oracle

Question set: `.rag-403/probe-runs/multi-file-expanded-v060synth.json` (140 multi_file questions)

Preconditions: fresh=137/137 gate_p0_pass=True


| composition | items_recalled/total | pooled_recall | per_file_coverage | mean_chars |
|---|---|---|---|---|
| C | 46/2493 | 0.018 | 0.234 | 926 |
| S0 | 11/2493 | 0.004 | 0.058 | 334 |
| Smid | 12/2493 | 0.005 | 0.073 | 333 |
| Slast | 18/2493 | 0.007 | 0.102 | 336 |
| P | 160/2493 | 0.064 | 0.591 | 736 |
| PI | 271/2493 | 0.109 | 0.745 | 1028 |
| H1 | 198/2493 | 0.079 | 0.664 | 1072 |
| H2 | 312/2493 | 0.125 | 0.788 | 1365 |

## Rules

- R-sec: {'chosen_section': 'Slast', 'overturned': False, 'slast_pooled_recall': 0.007220216606498195, 'slast_items_recalled': 18, 'best_alternative': 'Smid', 'best_alternative_pooled_recall': 0.0048134777376654635, 'best_alternative_items_recalled': 12}
- R-items: {'ship': 'PI', 'pi_items_recalled': 271, 'p_items_recalled': 160, 'delta_items': 111}
- K0: {'chosen_hybrid': 'H2', 'triggered': False, 'hybrid_per_file_coverage': 0.7883211678832117, 'control_per_file_coverage': 0.23357664233576642, 'hybrid_pooled_recall': 0.12515042117930206, 'control_pooled_recall': 0.018451664661050943, 'coverage_ratio': 3.3750000000000004, 'recall_ratio': 6.782608695652175}
