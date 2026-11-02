"""``--compare-only``: paired significance between two committed baselines."""

from __future__ import annotations

import argparse
from typing import Any

from rag_bench.baselines import load_baseline_metrics
from rag_bench.config import logger
from rag_bench.corpus import refuse_on_differing_injection_identity


def render_significance_table(rows: list[dict[str, Any]]) -> str:
    """The ``--compare-only`` table: one line per (corpus, class, measure).

    ``dir`` reads ``↑`` for a higher-is-better measure (nDCG, recall, MRR, ...) and
    ``↓`` for a lower-is-better one (``false_attribution_rate`` — #461 W2.E1). It is
    display-only: `higher_is_better` never changes the sign of `delta_mean` itself,
    only how a reader should interpret a positive one.

    The CI column header is DERIVED from ``row["confidence"]`` (``significance.
    summarize`` carries it on every row precisely so this never hardcodes "95%" —
    a mutant that silently changed the bootstrap's actual confidence level would
    otherwise mislabel its own output). A ``degenerate`` row (n < 2, or every
    delta identical — see ``significance.summarize``'s docstring) is marked with
    ``*``: its CI/p-value do not mean what they would over real spread, and a
    reader scanning only ``ci_contains_zero`` must not mistake it for a real
    result on a class this small.
    """
    confidence = rows[0]['confidence'] if rows else 0.95
    ci_header = f'{confidence * 100:.0f}% CI'
    header = ['corpus', 'class', 'measure', 'dir', 'n', 'delta_mean', ci_header, 't', 'p']
    lines = [
        '| ' + ' | '.join(header) + ' |',
        '|' + '|'.join(['---'] * len(header)) + '|',
    ]
    any_degenerate = False
    for row in rows:
        ci = f'[{row["ci_low"]:+.4f}, {row["ci_high"]:+.4f}]'
        if row['ci_contains_zero']:
            ci += ' (contains 0)'
        t_stat = 'n/a' if row['t_statistic'] is None else f'{row["t_statistic"]:.3f}'
        p_val = 'n/a' if row['p_value'] is None else f'{row["p_value"]:.4f}'
        direction = '↑' if row.get('higher_is_better', True) else '↓'
        degenerate = row.get('degenerate', False)
        any_degenerate = any_degenerate or degenerate
        n_cell = f'{row["n"]}*' if degenerate else str(row['n'])
        lines.append(
            '| '
            + ' | '.join(
                [
                    row['corpus'],
                    row['query_class'],
                    row['measure'],
                    direction,
                    n_cell,
                    f'{row["delta_mean"]:+.4f}',
                    ci,
                    t_stat,
                    p_val,
                ]
            )
            + ' |'
        )
    table = '\n'.join(lines) + '\n'
    if any_degenerate:
        table += (
            '\n* degenerate: n < 2 or every delta identical — CI/p-value are not '
            'meaningful at this sample size, do not gate a decision on them.\n'
        )
    return table


def run_compare_only(args: argparse.Namespace) -> int:
    """``--compare-only A B``: paired significance between two COMMITTED baselines.

    Reads two ``metrics.json`` files and computes it (#461) — no OpenSearch, no
    Postgres, no corpus injection, so this runs with no stack up at all.
    """
    from tests.eval.harness.significance import PartialJoinError, paired_join, summarize

    name_a, name_b = args.compare_only
    metrics_a = load_baseline_metrics(name_a)
    metrics_b = load_baseline_metrics(name_b)

    refusal = refuse_on_differing_injection_identity(name_a, name_b)
    if refusal is not None:
        logger.error('%s', refusal)
        return 3

    rows_a = metrics_a.get('retrieval_per_query') or []
    rows_b = metrics_b.get('retrieval_per_query') or []
    if not rows_a or not rows_b:
        empty = name_a if not rows_a else name_b
        logger.error(
            '--compare-only: %r has no retrieval_per_query rows — it predates '
            'report.build_retrieval_per_query (8117e6f3) or was regenerated before it, '
            'see rag-evaluation.md#paired-significance',
            empty,
        )
        return 3

    try:
        paired = paired_join(rows_a, rows_b)
    except PartialJoinError as exc:
        logger.error('--compare-only: %s', exc)
        return 3

    # The baseline ARGUMENT identifies which file was read; metrics.json's own
    # `control_name` is whatever --control-name the run was invoked with (often
    # left at its default), so printing that instead would show 'stage1-baseline'
    # for two baselines the caller explicitly told apart by name.
    summary_rows = summarize(paired)
    print(f'Paired significance: {name_a!r} (A) vs {name_b!r} (B), {len(paired)} queries')
    print(render_significance_table(summary_rows))
    return 0
