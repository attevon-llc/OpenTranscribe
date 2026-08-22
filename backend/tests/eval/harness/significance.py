"""Paired significance testing over per-query retrieval scores (#461 phase A1).

``report.build_retrieval_per_query`` (landed at ``8117e6f3``) turned every retrieval
number this harness publishes from a point estimate into something an interval can be
computed over. This module is that computation: it joins two runs' per-query rows on
``query_id`` and reports, per ``(corpus, query_class, measure)``, whether the delta
between them is distinguishable from noise.

**Primary method: a seeded paired bootstrap CI on the mean delta.** Each resampled unit
is one query's ``(A, B)`` pair, so the pairing survives the resample — this is what
makes it *paired* rather than a two-sample bootstrap that would ignore that both scores
came from the same query. ``numpy.random.default_rng(0)`` and 10,000 resamples make the
interval reproducible bit-for-bit: the same two baselines always produce the same CI,
so a reported interval can be checked rather than merely quoted.

**A CI that contains 0 means "not distinguishable from no change at this sample size,"
not "no effect."** With QMSum's ~150 queries and a typical nDCG@10 spread, a genuine
0.01 effect and a genuine 0.00 effect can produce the same interval; only a wider CI
(or more queries) resolves that. Read it as "cannot reject no-change," never as "proved
no-change."

**Secondary method: a paired t-test**, reported alongside the CI rather than instead of
it — a p-value answers "is this surprising under the null," the CI answers "how big
might it be," and #461's own worked example (Stage 5's fusion axis, tau-b −0.714 /
−0.905 between corpora) is exactly the situation where the first question is answered
differently from the second: QMSum's whole fusion spread was 0.0176 nDCG@10, small
enough that a technically-significant p-value there says little about a decision.

**Why not Wilcoxon signed-rank.** It is the common IR-textbook default for paired
retrieval comparisons, and it is not used here on purpose: it discards the *magnitude*
of each per-query delta and keeps only its sign and rank, which is needless power loss
when the underlying scores are already continuous bounded measures (nDCG, recall, MRR)
with no reason to prefer a rank transform. Its validity also assumes a symmetric
difference distribution, which retrieval deltas routinely strain — many queries move by
exactly 0 (both systems tie) or are floor/ceiling-clipped (nDCG in ``[0, 1]``) — a
condition this module has NOT independently measured to inflate Type-I error here (that
would need its own citation-backed study; see Urbano, Lima & Hanjalic, SIGIR 2019, for
the closest published treatment of significance testing in this exact IR setting, whose
findings are narrower than a blanket "Wilcoxon inflates Type-I error" claim). The
decision to skip it rests on the power/symmetry argument above, which stands on its own:
the bootstrap makes no distributional assumption at all, and the t-test's assumption
(approximately normal delta distribution, or enough queries for the CLT to cover for it)
is checkable from the query count already in the row — Wilcoxon buys no advantage over
either that would justify the rank transform's cost.

**The ``lookup`` query class is always broken out as its own row**, never folded only
into an ``all`` aggregate — see rag-evaluation.md's Stage 5 analysis, where a
corpus-level aggregate agreed far more often (62.5% signs) than the ``lookup`` class
alone (40.0%), and ``lookup`` is the class the #461 gate protects. An aggregate that
looks stable can still hide a lookup regression inside gains elsewhere.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

#: Whether a positive delta (B minus A) is an improvement, per measure name (#461
#: W2.E1: the new rate/coverage measures mix higher-is-better and lower-is-better
#: in one table, unlike the nDCG-family measures this module started with, all of
#: which are higher-is-better). Reporting a ``false_attribution_rate`` delta as
#: though it read like an ``nDCG@10`` delta would print "+0.05" for a WORSE probe
#: result and call it an improvement. Purely a display annotation — every
#: computation above (delta, CI, t-test) is direction-agnostic; this dict is
#: consulted only when rendering. Defaults to ``True`` (higher is better) for any
#: measure not listed, which is every measure this module shipped with originally.
MEASURE_DIRECTION: dict[str, bool] = {
    "false_attribution_rate": False,
}


def higher_is_better(measure: str) -> bool:
    """Whether a positive delta on ``measure`` is an improvement. See
    :data:`MEASURE_DIRECTION`; unlisted measures default to ``True``."""
    return MEASURE_DIRECTION.get(measure, True)


class PartialJoinError(ValueError):
    """Two per-query score sets do not share exactly the same ``query_id`` set.

    Raised rather than silently intersecting: a partial join that drops queries no
    reader asked to drop is the exact failure this module exists to prevent — the
    two sides would look paired while actually comparing different query subsets.
    """

    def __init__(self, only_in_a: frozenset[str], only_in_b: frozenset[str]) -> None:
        self.only_in_a = only_in_a
        self.only_in_b = only_in_b
        sample_a = sorted(only_in_a)[:3]
        sample_b = sorted(only_in_b)[:3]
        super().__init__(
            f"partial paired join: {len(only_in_a)} query id(s) only in A "
            f"(e.g. {sample_a!r}), {len(only_in_b)} only in B (e.g. {sample_b!r}) — "
            "refusing to silently drop queries; pass two runs over the same query set"
        )


class DuplicateQueryIdError(ValueError):
    """One side of a paired join repeats a ``query_id``.

    A set-based comparison of ids (what :func:`paired_join` checks first) is
    multiplicity-blind: ``{"q1", "q2"}`` equals itself whether ``q1`` appeared once
    or three times, so a naive ``{row["query_id"]: row for row in rows}`` dict build
    silently keeps only the LAST occurrence and drops the rest — the exact "queries
    quietly go missing" failure :class:`PartialJoinError` exists to prevent, reached
    through row order instead of a missing id. Raised before either side is turned
    into a dict, so no join is ever built from ambiguous input.
    """

    def __init__(self, side: str, duplicate_counts: Mapping[str, int]) -> None:
        self.side = side
        self.duplicate_counts = dict(duplicate_counts)
        sample = sorted(duplicate_counts)[:3]
        super().__init__(
            f"paired_join: side {side} has {len(duplicate_counts)} duplicate query_id(s) "
            f"(e.g. {sample!r}) — refusing a last-wins join that would silently drop rows"
        )


def _reject_duplicate_ids(rows: Sequence[Mapping[str, Any]], *, side: str) -> None:
    counts = Counter(row["query_id"] for row in rows)
    duplicates = {query_id: count for query_id, count in counts.items() if count > 1}
    if duplicates:
        raise DuplicateQueryIdError(side, duplicates)


@dataclass(frozen=True)
class PairedQuery:
    """One query's scores from both runs, ready to be differenced per measure."""

    query_id: str
    corpus: str
    query_class: str
    scores_a: Mapping[str, float]
    scores_b: Mapping[str, float]


def paired_join(
    rows_a: Sequence[Mapping[str, Any]], rows_b: Sequence[Mapping[str, Any]]
) -> list[PairedQuery]:
    """Join two ``retrieval_per_query`` lists on ``query_id``.

    Args:
        rows_a: ``retrieval_per_query`` rows from the first (baseline) run.
        rows_b: ``retrieval_per_query`` rows from the second (candidate) run.

    Returns:
        One :class:`PairedQuery` per shared query id, sorted by id.

    Raises:
        DuplicateQueryIdError: either side repeats a ``query_id``. Checked BEFORE
            the partial-join check: a duplicate would otherwise still pass the set
            comparison (sets are multiplicity-blind) and then silently lose rows to
            a last-wins dict build.
        PartialJoinError: either side has query ids the other lacks.
        ValueError: a shared query id names a different corpus or query class in
            the two runs — the two files describe different corpora, not two
            measurements of the same one.
    """
    _reject_duplicate_ids(rows_a, side="A")
    _reject_duplicate_ids(rows_b, side="B")

    ids_a = {row["query_id"] for row in rows_a}
    ids_b = {row["query_id"] for row in rows_b}
    if ids_a != ids_b:
        raise PartialJoinError(frozenset(ids_a - ids_b), frozenset(ids_b - ids_a))

    by_a = {row["query_id"]: row for row in rows_a}
    by_b = {row["query_id"]: row for row in rows_b}
    paired: list[PairedQuery] = []
    for query_id in sorted(ids_a):
        a = by_a[query_id]
        b = by_b[query_id]
        if a["corpus"] != b["corpus"] or a["query_class"] != b["query_class"]:
            raise ValueError(
                f"query {query_id!r} changed corpus/class between runs: "
                f"({a['corpus']!r}, {a['query_class']!r}) vs "
                f"({b['corpus']!r}, {b['query_class']!r})"
            )
        paired.append(
            PairedQuery(
                query_id=query_id,
                corpus=a["corpus"],
                query_class=a["query_class"],
                scores_a=a["scores"],
                scores_b=b["scores"],
            )
        )
    return paired


@dataclass(frozen=True)
class BootstrapResult:
    """A seeded paired-bootstrap confidence interval on a mean delta."""

    delta_mean: float
    ci_low: float
    ci_high: float
    n: int
    n_resamples: int


def paired_bootstrap_ci(
    deltas: Sequence[float],
    *,
    n_resamples: int = 10_000,
    seed: int = 0,
    confidence: float = 0.95,
) -> BootstrapResult:
    """Seeded paired bootstrap CI on the mean of ``deltas``. The primary method.

    Args:
        deltas: per-query ``score_b - score_a`` values. Each element is already a
            paired difference, so resampling *elements* (not raw A/B scores)
            preserves the pairing.
        n_resamples: bootstrap resample count. #461 specifies 10,000.
        seed: seed for ``numpy.random.default_rng``. Fixed at 0 by convention so
            two callers computing the same interval get the same bits.
        confidence: two-sided confidence level for the percentile interval.

    Returns:
        The observed mean delta and the empirical percentile CI around it.

    Raises:
        ValueError: ``deltas`` is empty.
    """
    arr = np.asarray(list(deltas), dtype=np.float64)
    n = arr.size
    if n == 0:
        raise ValueError("paired_bootstrap_ci: no paired queries to resample")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_resamples, n))
    resample_means = arr[idx].mean(axis=1)
    alpha = (1.0 - confidence) / 2.0
    ci_low, ci_high = np.quantile(resample_means, [alpha, 1.0 - alpha])
    return BootstrapResult(
        delta_mean=float(arr.mean()),
        ci_low=float(ci_low),
        ci_high=float(ci_high),
        n=n,
        n_resamples=n_resamples,
    )


@dataclass(frozen=True)
class TTestResult:
    """A paired t-test on the per-query deltas (equivalently, a one-sample test of
    the deltas against 0)."""

    statistic: float
    p_value: float
    df: int
    engine: str


def paired_ttest(deltas: Sequence[float]) -> TTestResult:
    """Paired t-test over ``deltas``. The secondary, confirmatory method.

    Uses ``scipy.stats`` when importable — it already is, transitively, via
    ``sentence-transformers`` (see ``backend/requirements-eval.txt``'s header) — and
    falls back to a normal approximation (``math.erfc``) with **no new dependency**
    when it is not.

    ⚠️ **The fallback's accuracy depends on the sample size actually being
    compared, not on the corpus it was drawn from.** :func:`summarize` calls this
    per ``(corpus, query_class, measure)`` — a query CLASS subset, which can be far
    smaller than the corpus total. Measured directly: at ``n=10``, Student's t
    (via scipy) gives ``p=0.0500`` where the normal approximation gives
    ``p=0.0237`` for the same deltas — roughly 2× apart, not a rounding
    difference. It IS asymptotically valid (the two converge as ``n`` grows), but
    "every paired set this harness produces" is not evidence about a specific
    small class; treat a ``"normal-approx"`` engine on a low ``n`` row as
    approximate, and prefer the bootstrap CI's ``n`` field over the p-value alone
    at small sample sizes.

    Args:
        deltas: per-query ``score_b - score_a`` values.

    Returns:
        The t statistic, two-sided p-value, degrees of freedom, and which engine
        computed the p-value (``"scipy"``, ``"normal-approx"``, or ``"degenerate"``
        when every delta is identical).

    Raises:
        ValueError: fewer than 2 paired queries.
    """
    arr = np.asarray(list(deltas), dtype=np.float64)
    n = arr.size
    if n < 2:
        raise ValueError("paired_ttest: need at least 2 paired queries")
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    df = n - 1
    if std == 0.0:
        statistic = 0.0 if mean == 0.0 else math.copysign(math.inf, mean)
        p_value = 1.0 if mean == 0.0 else 0.0
        return TTestResult(statistic=statistic, p_value=p_value, df=df, engine="degenerate")

    se = std / math.sqrt(n)
    statistic = mean / se
    try:
        from scipy import stats

        p_value = float(stats.t.sf(abs(statistic), df) * 2)
        engine = "scipy"
    except ImportError:  # pragma: no cover - exercised only when scipy is absent
        p_value = math.erfc(abs(statistic) / math.sqrt(2))
        engine = "normal-approx"
    return TTestResult(statistic=statistic, p_value=p_value, df=df, engine=engine)


def _resolve_measure_names(
    paired: Sequence[PairedQuery], measures: Sequence[str] | None
) -> list[str]:
    """The measure names :func:`summarize` reports, validated across BOTH sides of
    EVERY paired row — not just ``paired[0].scores_a``.

    A run pair can legitimately be generated months apart, over a metric engine
    whose ``MEASURES`` grew in between (this is exactly what happens across this
    directory's own baselines over time), so a single-row, single-side peek at the
    measure set fails asymmetrically: a measure present only in A raises a bare
    ``KeyError`` deep in the delta loop, while one present only in B is silently
    absent from the table with exit 0 — the dangerous direction, because it reads
    as "measured and unremarkable" rather than "not compared at all". Both
    directions are raised here, loudly, before any bootstrap runs.

    Args:
        paired: output of :func:`paired_join`.
        measures: explicit measure names, or ``None`` to derive them.

    Raises:
        ValueError: ``measures`` is ``None`` and the A-side and B-side measure sets
            (unioned across every paired row) are not identical.
    """
    if measures is not None:
        return list(measures)
    names_a = {name for row in paired for name in row.scores_a}
    names_b = {name for row in paired for name in row.scores_b}
    if names_a != names_b:
        raise ValueError(
            "summarize: A-side and B-side measure sets differ — "
            f"only in A: {sorted(names_a - names_b)}, only in B: {sorted(names_b - names_a)}. "
            "Pass `measures=` explicitly to compare a specific subset."
        )
    return sorted(names_a)


def summarize(
    paired: Sequence[PairedQuery],
    *,
    measures: Sequence[str] | None = None,
    n_resamples: int = 10_000,
    seed: int = 0,
    confidence: float = 0.95,
) -> list[dict[str, Any]]:
    """One row per ``(corpus, query_class, measure)``, plus an ``all`` class per corpus.

    The ``lookup`` class, when present in a corpus, is always its own row alongside
    ``all`` — never folded in silently — for the reason in the module docstring.

    Args:
        paired: output of :func:`paired_join`.
        measures: measure names to report. Defaults to every measure key present on
            BOTH sides of every paired query — see :func:`_resolve_measure_names`.
        n_resamples: forwarded to :func:`paired_bootstrap_ci`.
        seed: forwarded to :func:`paired_bootstrap_ci`.
        confidence: forwarded to :func:`paired_bootstrap_ci`, and also carried on
            every output row (``"confidence"``) so a renderer can label the
            interval correctly instead of hardcoding "95%".

    Returns:
        Rows sorted by ``(corpus, query_class, measure)``, each carrying the
        bootstrap CI, the t-test result, and a ``"degenerate"`` flag.

    Raises:
        ValueError: a requested/derived measure is missing from either side of a
            selected query's scores (see :func:`_resolve_measure_names` for the
            ``measures=None`` case; an explicit ``measures=`` is checked per-query
            here so a typo'd or engine-mismatched name fails loudly too).
    """
    if not paired:
        return []
    names = _resolve_measure_names(paired, measures)

    by_corpus: dict[str, list[PairedQuery]] = {}
    for row in paired:
        by_corpus.setdefault(row.corpus, []).append(row)

    results: list[dict[str, Any]] = []
    for corpus in sorted(by_corpus):
        members = by_corpus[corpus]
        classes_present = sorted({row.query_class for row in members})
        groups: list[tuple[str, list[PairedQuery]]] = [
            (cls, [row for row in members if row.query_class == cls]) for cls in classes_present
        ]
        groups.append(("all", members))
        for query_class, selected in groups:
            if not selected:
                continue
            for measure in names:
                deltas = []
                for row in selected:
                    if measure not in row.scores_a or measure not in row.scores_b:
                        raise ValueError(
                            f"summarize: measure {measure!r} missing from query "
                            f"{row.query_id!r} (in A: {measure in row.scores_a}, "
                            f"in B: {measure in row.scores_b})"
                        )
                    deltas.append(row.scores_b[measure] - row.scores_a[measure])
                boot = paired_bootstrap_ci(
                    deltas, n_resamples=n_resamples, seed=seed, confidence=confidence
                )
                ttest = paired_ttest(deltas) if len(deltas) >= 2 else None
                # A single query (n=1) has no ttest AND a bootstrap CI that
                # necessarily collapses to a point (every resample draws the same
                # one value) — `ci_contains_zero=False` there would read as a
                # confident result from a sample of one. Zero-variance deltas at
                # n>=2 collapse the SAME way for a different reason (paired_ttest's
                # own "degenerate" engine). Both are flagged identically: neither
                # the CI width nor the p-value means what it would over real spread.
                degenerate = ttest is None or ttest.engine == "degenerate"
                results.append(
                    {
                        "corpus": corpus,
                        "query_class": query_class,
                        "measure": measure,
                        "n": len(selected),
                        "delta_mean": boot.delta_mean,
                        "ci_low": boot.ci_low,
                        "ci_high": boot.ci_high,
                        "ci_contains_zero": boot.ci_low <= 0.0 <= boot.ci_high,
                        "confidence": confidence,
                        "t_statistic": None if ttest is None else ttest.statistic,
                        "p_value": None if ttest is None else ttest.p_value,
                        "degenerate": degenerate,
                        "higher_is_better": higher_is_better(measure),
                    }
                )
    return sorted(results, key=lambda row: (row["corpus"], row["query_class"], row["measure"]))
