#!/usr/bin/env python3
"""Do the two evaluation corpora AGREE about which retrieval change is an improvement?

Every ``benchmark_rag.py`` run scores QMSum (real, human queries) and the synthetic tier
in the same pass, so a sweep already contains the data — what it has never contained is a
statement about whether the two *rank the arms the same way*. Stage 5 measured 24 arms and
found several where one corpus says "adopt" and the other says "regression", so the
question is not hypothetical: if agreement is poor, a single-corpus tuning result is not
evidence, and the both-corpora gate is load-bearing rather than ceremony.

Two rules make the comparison meaningful:

**Direction and rank only, never absolute nDCG.** QMSum's control sits at 0.0983 and the
synthetic tier's at 0.2952. That gap is a property of the corpora — 17-word QMSum turns
against generated meetings with an embedded header — and comparing the two absolute
numbers measures the corpora, not the arm. So every arm is reduced to a **delta against
its own family's control**, and only the sign and the rank of that delta are used.

**Deltas are compared within a family.** An arm is only comparable to arms sharing its
control, its ``--stage`` and its query set; the fusion arms are ``--stage retrieve`` over
1,651 queries while the budget arms are ``--stage rerank`` over a 475-query subset. Family
coefficients are therefore the primary result and the pooled one is a summary, reported
with its caveat rather than instead of them.

Kendall's tau-b is the headline coefficient. Spearman's rho is printed beside it, but the
arm set contains **exact ties** — several budget arms move nothing on either corpus
because ``diversity_sample`` is prefix-invariant in ``cap`` — and tau-b has a defined tie
correction where Spearman's midrank handling merely does not crash. Pearson is printed
too, as a warning rather than a result: it is dominated by the two arms that collapse for
a structural reason, and dropping them moves it from +0.70 to +0.12 while tau-b barely
notices. That contrast is the argument for using a rank measure here.

Usage::

    scripts/rag_corpus_agreement.py                          # Stage 5's 24 arms
    scripts/rag_corpus_agreement.py --class lookup           # the class the gate protects
    scripts/rag_corpus_agreement.py --drop norm-minmax-geom --drop norm-minmax-harm
    scripts/rag_corpus_agreement.py --manifest my-arms.json  # a 25th arm, no code edit
    scripts/rag_corpus_agreement.py --json                   # machine-readable

Adding an arm needs no edit to this file: write a manifest (``--emit-manifest`` prints the
built-in one as a starting point), add the arm's entry, and re-run. The only requirement on
the new run is the one ``benchmark_rag.py`` already meets — a ``metrics.json`` with a
``rows`` array carrying ``corpus``/``query_class``/``metrics``.

Dependency note: ``scipy`` (BSD-3-Clause) is a hard requirement of ``sentence-transformers``
and so is already installed everywhere the backend runs, including CI. It is NOT the
licence-restricted case ``requirements-eval.txt`` documents for ``pytrec_eval_terrier``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Deltas are rounded to this many places before comparison. ``metrics.json`` carries six
#: decimals, so anything beyond that is float subtraction noise — and noise here is not
#: cosmetic: it turns an exact tie (an arm that provably moved nothing) into a 1e-17
#: "improvement" that a rank correlation would happily order.
_DELTA_PLACES = 6

#: Stage 5's 24 arms, grouped by the control they were measured against. `axis` is the
#: knob the arm moves — the unit the "which axes disagree" summary aggregates over.
DEFAULT_MANIFEST: dict[str, Any] = {
    'runs_root': '/tmp/sweep403',
    'measure': 'nDCG@10',
    'families': [
        {
            'name': 'fusion',
            'control': {'name': 'rrf-30-default', 'run': 'rrf-30-default'},
            'stage': 'retrieve',
            'query_set': 'full corpus, 1,651 queries',
            'arms': [
                {
                    'name': 'rrf-30-explicit',
                    'run': 'rrf-30-explicit',
                    'axis': 'flag-inertness control',
                },
                {'name': 'rrf-60', 'run': 'rrf-60', 'axis': 'rank_constant'},
                {'name': 'norm-minmax-arith', 'run': 'norm-minmax-arith', 'axis': 'normalization'},
                {'name': 'norm-l2-arith', 'run': 'norm-l2-arith', 'axis': 'normalization'},
                {'name': 'norm-zscore-arith', 'run': 'norm-zscore-arith', 'axis': 'normalization'},
                {'name': 'norm-minmax-geom', 'run': 'norm-minmax-geom', 'axis': 'combination'},
                {'name': 'norm-minmax-harm', 'run': 'norm-minmax-harm', 'axis': 'combination'},
                {
                    'name': 'norm-minmax-arith-w70-30',
                    'run': 'norm-minmax-arith-w70-30',
                    'axis': 'weighting',
                },
                {
                    'name': 'norm-minmax-arith-w30-70',
                    'run': 'norm-minmax-arith-w30-70',
                    'axis': 'weighting',
                },
            ],
        },
        {
            'name': 'budget',
            'control': {'name': 'budget-48-12-4', 'run': 'v2/budget-48-12-4'},
            'stage': 'rerank',
            'query_set': '475-query subset (400 QMSum + all 75 synthetic)',
            'arms': [
                {
                    'name': 'budget-48-12-4-repeat',
                    'run': 'v2/budget-48-12-4-repeat',
                    'axis': 'repeatability control',
                },
                {'name': 'budget-48-20-4', 'run': 'v2/budget-48-20-4', 'axis': 'final_chunks'},
                {'name': 'budget-48-08-4', 'run': 'v2/budget-48-08-4', 'axis': 'final_chunks'},
                {
                    'name': 'budget-48-12-2',
                    'run': 'v2/budget-48-12-2',
                    'axis': 'max_chunks_per_file',
                },
                {
                    'name': 'budget-48-12-8',
                    'run': 'v2/budget-48-12-8',
                    'axis': 'max_chunks_per_file',
                },
                {'name': 'budget-24-12-4', 'run': 'v2/budget-24-12-4', 'axis': 'candidate_pool'},
                {'name': 'budget-96-12-4', 'run': 'v2/budget-96-12-4', 'axis': 'candidate_pool'},
                {
                    'name': 'budget-96-12-4-pairs96',
                    'run': 'v2/budget-96-12-4-pairs96',
                    'axis': 'rerank_max_pairs',
                },
            ],
        },
        {
            'name': 'pool',
            'control': {'name': 'pool-48', 'run': 'full/full-48-12-4'},
            'stage': 'rerank',
            'query_set': 'full corpus, 1,651 queries',
            'arms': [
                {'name': 'pool-12', 'run': 'full/full-12-12-4', 'axis': 'candidate_pool'},
                {'name': 'pool-24', 'run': 'full/full-24-12-4', 'axis': 'candidate_pool'},
                {'name': 'pool-32', 'run': 'full/full-32-12-4', 'axis': 'candidate_pool'},
                {'name': 'pool-96', 'run': 'full/full-96-12-4', 'axis': 'candidate_pool'},
            ],
        },
    ],
}


@dataclass(frozen=True)
class ArmDelta:
    """One arm's movement against its family control, on both corpora."""

    family: str
    arm: str
    axis: str
    qmsum: float
    synthetic: float

    @property
    def verdict(self) -> str:
        """``agree`` / ``disagree`` / ``inert``, on sign alone.

        ``inert`` is its own category and not a third of an agreement: an arm that moves
        nothing on either corpus (``final_chunks`` on a prefix-invariant sampler) is
        evidence about the metric, not about whether the corpora concur.
        """
        if self.qmsum == 0.0 and self.synthetic == 0.0:
            return 'inert'
        if self.qmsum == 0.0 or self.synthetic == 0.0:
            return 'partial'
        return 'agree' if (self.qmsum > 0) == (self.synthetic > 0) else 'disagree'


@dataclass
class SignCounts:
    """The plain count a correlation coefficient hides."""

    agree: int = 0
    disagree: int = 0
    inert: int = 0
    partial: int = 0

    @property
    def decided(self) -> int:
        """Arms that moved on both corpora, i.e. the ones a sign can be read from."""
        return self.agree + self.disagree

    @property
    def rate(self) -> float | None:
        """Fraction of decided arms that agree. ``None`` when nothing moved."""
        return None if self.decided == 0 else self.agree / self.decided


@dataclass
class RankAgreement:
    """Rank-correlation summary for one set of arms."""

    n: int
    kendall_tau_b: float | None
    kendall_p: float | None
    spearman_rho: float | None
    spearman_p: float | None
    pearson_r: float | None
    pearson_p: float | None
    note: str = ''


@dataclass
class Winner:
    """The arm each corpus would pick, and whether that is the same arm."""

    scope: str
    qmsum_arm: str
    qmsum_delta: float
    synthetic_arm: str
    synthetic_delta: float
    fields: list[str] = field(default_factory=list)

    @property
    def same(self) -> bool:
        return self.qmsum_arm == self.synthetic_arm


def load_rows(run_dir: Path) -> dict[tuple[str, str], dict[str, float]]:
    """Read one run's ``metrics.json`` into ``{(corpus, query_class): {measure: value}}``."""
    path = run_dir / 'metrics.json'
    if not path.is_file():
        raise FileNotFoundError(f'no metrics.json under {run_dir}')
    payload = json.loads(path.read_text())
    return {(row['corpus'], row['query_class']): row['metrics'] for row in payload['rows']}


def delta(
    control_rows: dict[tuple[str, str], dict[str, float]],
    arm_rows: dict[tuple[str, str], dict[str, float]],
    corpus: str,
    query_class: str,
    measure: str,
) -> float:
    """Arm minus control for one (corpus, class, measure), rounded to the file's precision."""
    key = (corpus, query_class)
    for name, rows in (('control', control_rows), ('arm', arm_rows)):
        if key not in rows:
            raise KeyError(f'{name} run has no row for {key}')
        if measure not in rows[key]:
            raise KeyError(f'{name} run row {key} has no measure {measure!r}')
    return round(arm_rows[key][measure] - control_rows[key][measure], _DELTA_PLACES)


def collect_deltas(
    manifest: dict[str, Any],
    query_class: str,
    measure: str,
    runs_root: Path,
    drop: frozenset[str] = frozenset(),
) -> list[ArmDelta]:
    """Load every arm in the manifest and reduce it to a (qmsum, synthetic) delta pair."""
    out: list[ArmDelta] = []
    for family in manifest['families']:
        control_rows = load_rows(runs_root / family['control']['run'])
        for arm in family['arms']:
            if arm['name'] in drop:
                continue
            arm_rows = load_rows(runs_root / arm['run'])
            out.append(
                ArmDelta(
                    family=family['name'],
                    arm=arm['name'],
                    axis=arm['axis'],
                    qmsum=delta(control_rows, arm_rows, 'qmsum', query_class, measure),
                    synthetic=delta(control_rows, arm_rows, 'synthetic', query_class, measure),
                )
            )
    return out


def sign_agreement(deltas: list[ArmDelta]) -> SignCounts:
    """Count how many arms move the same way on both corpora."""
    counts = SignCounts()
    for item in deltas:
        setattr(counts, item.verdict, getattr(counts, item.verdict) + 1)
    return counts


def rank_agreement(deltas: list[ArmDelta]) -> RankAgreement:
    """Kendall tau-b (primary), Spearman rho and Pearson r over the arm deltas.

    Returns ``None`` coefficients rather than raising when they are undefined: fewer than
    three arms, or one corpus constant across every arm (which happens to the QMSum side
    of an all-inert family and makes every correlation ``nan``, not zero).
    """
    from scipy import stats  # imported here so --help works without scipy installed

    n = len(deltas)
    if n < 3:
        return RankAgreement(n, None, None, None, None, None, None, note='n < 3')
    qmsum = [d.qmsum for d in deltas]
    synthetic = [d.synthetic for d in deltas]
    if len(set(qmsum)) == 1 or len(set(synthetic)) == 1:
        return RankAgreement(n, None, None, None, None, None, None, note='a corpus is constant')

    tau = stats.kendalltau(qmsum, synthetic, variant='b')
    rho = stats.spearmanr(qmsum, synthetic)
    pearson = stats.pearsonr(qmsum, synthetic)

    def _clean(value: float) -> float | None:
        return None if math.isnan(float(value)) else round(float(value), 4)

    return RankAgreement(
        n=n,
        kendall_tau_b=_clean(tau.statistic),
        kendall_p=_clean(tau.pvalue),
        spearman_rho=_clean(rho.statistic),
        spearman_p=_clean(rho.pvalue),
        pearson_r=_clean(pearson.statistic),
        pearson_p=_clean(pearson.pvalue),
    )


def winner(deltas: list[ArmDelta], scope: str) -> Winner | None:
    """The best arm by each corpus. ``None`` when the set is empty.

    Ties are broken by arm name so the answer is deterministic; a tie is visible anyway
    because the two deltas are printed.
    """
    if not deltas:
        return None
    best_q = max(deltas, key=lambda d: (d.qmsum, d.arm))
    best_s = max(deltas, key=lambda d: (d.synthetic, d.arm))
    return Winner(
        scope=scope,
        qmsum_arm=best_q.arm,
        qmsum_delta=best_q.qmsum,
        synthetic_arm=best_s.arm,
        synthetic_delta=best_s.synthetic,
    )


def by_axis(deltas: list[ArmDelta]) -> dict[str, SignCounts]:
    """Sign agreement grouped by the knob each arm moves."""
    groups: dict[str, list[ArmDelta]] = {}
    for item in deltas:
        groups.setdefault(item.axis, []).append(item)
    return {axis: sign_agreement(items) for axis, items in groups.items()}


def _fmt(value: float | None, places: int = 4) -> str:
    return '—' if value is None else f'{value:+.{places}f}'


def report(deltas: list[ArmDelta], query_class: str, measure: str) -> None:
    """Print the human-readable analysis."""
    print(f'AGREEMENT — {measure}, query class {query_class!r}, {len(deltas)} arms')
    print("Δ is against the arm's OWN family control; absolute nDCG is never compared.")
    print()
    print(f'{"family":<8} {"arm":<26} {"axis":<24} {"Δ qmsum":>10} {"Δ synth":>10}  verdict')
    for item in deltas:
        print(
            f'{item.family:<8} {item.arm:<26} {item.axis:<24} '
            f'{item.qmsum:>+10.4f} {item.synthetic:>+10.4f}  {item.verdict}'
        )

    counts = sign_agreement(deltas)
    rate = counts.rate
    print()
    print('SIGN AGREEMENT')
    print(
        f'  agree {counts.agree} | disagree {counts.disagree} | inert (both 0.0) {counts.inert} | one-sided {counts.partial}'
    )
    print(
        f'  of the {counts.decided} arms that moved on both corpora, '
        + (f'{counts.agree} agree ({rate:.1%})' if rate is not None else 'none')
    )

    print()
    print('RANK AGREEMENT (Kendall tau-b primary; Pearson shown only to be distrusted)')
    families = sorted({d.family for d in deltas})
    for name in families:
        subset = [d for d in deltas if d.family == name]
        agreement = rank_agreement(subset)
        print(f'  {name:<10} {_render_rank(agreement)}')
    pooled = rank_agreement(deltas)
    print(f'  {"POOLED":<10} {_render_rank(pooled)}')
    print(
        '  pooled mixes families with different stages and query sets — a summary, not the result'
    )

    print()
    print('WOULD THE TWO CORPORA PICK THE SAME WINNER?')
    for name in families:
        best = winner([d for d in deltas if d.family == name], name)
        if best is not None:
            print(f'  {name:<10} {_render_winner(best)}')
    best = winner(deltas, 'pooled')
    if best is not None:
        print(f'  {"pooled":<10} {_render_winner(best)}')

    print()
    print('BY AXIS (which knob the corpora disagree about)')
    for axis, counts in sorted(by_axis(deltas).items(), key=lambda kv: (-kv[1].disagree, kv[0])):
        print(
            f'  {axis:<24} agree {counts.agree}  disagree {counts.disagree}  '
            f'inert {counts.inert}  one-sided {counts.partial}'
        )


def _render_rank(agreement: RankAgreement) -> str:
    if agreement.note:
        return f'n={agreement.n}  undefined ({agreement.note})'
    return (
        f'n={agreement.n:<3} tau_b={_fmt(agreement.kendall_tau_b, 3)} (p={agreement.kendall_p:.3f})  '
        f'rho={_fmt(agreement.spearman_rho, 3)} (p={agreement.spearman_p:.3f})  '
        f'r={_fmt(agreement.pearson_r, 3)} (p={agreement.pearson_p:.3f})'
    )


def _render_winner(best: Winner) -> str:
    mark = 'SAME ARM' if best.same else 'DIFFERENT ARMS'
    return (
        f'qmsum→{best.qmsum_arm} ({best.qmsum_delta:+.4f})  '
        f'synthetic→{best.synthetic_arm} ({best.synthetic_delta:+.4f})  [{mark}]'
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Measure whether QMSum and the synthetic tier agree about retrieval arms.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--manifest', type=Path, help="JSON arm manifest (default: Stage 5's 24 arms)"
    )
    parser.add_argument('--runs-root', type=Path, help="override the manifest's runs_root")
    parser.add_argument(
        '--class', dest='query_class', default='all', help='query class row to read (default: all)'
    )
    parser.add_argument('--measure', help="measure to compare (default: the manifest's, nDCG@10)")
    parser.add_argument(
        '--drop',
        action='append',
        default=[],
        metavar='ARM',
        help='exclude an arm (repeatable) — for sensitivity checks',
    )
    parser.add_argument(
        '--exclude-inert',
        action='store_true',
        help='drop arms that moved NOTHING on either corpus. Two zeros are a concordant pair '
        'to tau-b, so a family full of inert arms can report high agreement about nothing.',
    )
    parser.add_argument(
        '--emit-manifest', action='store_true', help='print the built-in manifest as JSON and exit'
    )
    parser.add_argument('--json', action='store_true', help='emit machine-readable results')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.emit_manifest:
        print(json.dumps(DEFAULT_MANIFEST, indent=2))
        return 0

    manifest = json.loads(args.manifest.read_text()) if args.manifest else DEFAULT_MANIFEST
    runs_root = args.runs_root or Path(manifest.get('runs_root', '.'))
    measure = args.measure or manifest.get('measure', 'nDCG@10')

    try:
        deltas = collect_deltas(
            manifest, args.query_class, measure, Path(runs_root), frozenset(args.drop)
        )
    except (FileNotFoundError, KeyError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2

    if args.exclude_inert:
        deltas = [d for d in deltas if d.verdict != 'inert']

    if args.json:
        payload = {
            'measure': measure,
            'query_class': args.query_class,
            'runs_root': str(runs_root),
            'dropped': sorted(args.drop),
            'arms': [asdict(d) | {'verdict': d.verdict} for d in deltas],
            'sign_agreement': asdict(sign_agreement(deltas)),
            'rank_agreement': {
                **{
                    name: asdict(rank_agreement([d for d in deltas if d.family == name]))
                    for name in sorted({d.family for d in deltas})
                },
                'pooled': asdict(rank_agreement(deltas)),
            },
            'by_axis': {axis: asdict(counts) for axis, counts in by_axis(deltas).items()},
        }
        best = winner(deltas, 'pooled')
        if best is not None:
            payload['winner'] = asdict(best) | {'same': best.same}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    report(deltas, args.query_class, measure)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
