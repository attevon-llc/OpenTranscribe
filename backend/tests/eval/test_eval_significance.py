"""Tests for ``harness.significance`` — paired significance over per-query rows (#461).

No OpenSearch, Postgres, or ``pytrec_eval`` needed: everything here operates on the
plain-dict ``retrieval_per_query`` shape ``report.build_retrieval_per_query`` emits.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.eval.harness.significance import DuplicateQueryIdError
from tests.eval.harness.significance import PairedQuery
from tests.eval.harness.significance import PartialJoinError
from tests.eval.harness.significance import higher_is_better
from tests.eval.harness.significance import paired_bootstrap_ci
from tests.eval.harness.significance import paired_join
from tests.eval.harness.significance import paired_ttest
from tests.eval.harness.significance import summarize


def _row(query_id: str, corpus: str, query_class: str, **scores: float) -> dict:
    return {"query_id": query_id, "corpus": corpus, "query_class": query_class, "scores": scores}


class TestPairedJoin:
    def test_joins_matching_query_ids_sorted(self) -> None:
        rows_a = [
            _row("q2", "qmsum", "lookup", **{"nDCG@10": 0.4}),
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.2}),
        ]
        rows_b = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.3}),
            _row("q2", "qmsum", "lookup", **{"nDCG@10": 0.5}),
        ]
        paired = paired_join(rows_a, rows_b)
        assert [p.query_id for p in paired] == ["q1", "q2"]
        assert paired[0].scores_a == {"nDCG@10": 0.2}
        assert paired[0].scores_b == {"nDCG@10": 0.3}

    def test_refuses_a_silent_partial_join(self) -> None:
        rows_a = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.2})]
        rows_b = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.3}),
            _row("q2", "qmsum", "lookup", **{"nDCG@10": 0.5}),
        ]
        with pytest.raises(PartialJoinError) as excinfo:
            paired_join(rows_a, rows_b)
        assert excinfo.value.only_in_a == frozenset()
        assert excinfo.value.only_in_b == frozenset({"q2"})
        assert "1 only in B" in str(excinfo.value)

    def test_refuses_a_join_where_both_sides_have_unique_ids(self) -> None:
        rows_a = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.2})]
        rows_b = [_row("q2", "qmsum", "lookup", **{"nDCG@10": 0.5})]
        with pytest.raises(PartialJoinError) as excinfo:
            paired_join(rows_a, rows_b)
        assert excinfo.value.only_in_a == frozenset({"q1"})
        assert excinfo.value.only_in_b == frozenset({"q2"})

    def test_rejects_a_query_id_that_changed_corpus_between_runs(self) -> None:
        rows_a = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.2})]
        rows_b = [_row("q1", "synthetic", "lookup", **{"nDCG@10": 0.2})]
        with pytest.raises(ValueError, match="changed corpus/class"):
            paired_join(rows_a, rows_b)

    def test_empty_both_sides_joins_to_empty(self) -> None:
        assert paired_join([], []) == []

    def test_a_duplicate_query_id_in_a_is_rejected_even_though_the_id_sets_match(
        self,
    ) -> None:
        """Reviewer finding: a set-based id comparison is multiplicity-blind, so a
        duplicate on one side alone still passes `ids_a == ids_b` and a naive
        `{row["query_id"]: row for row in rows}` build would silently keep only the
        LAST occurrence — losing a row exactly as `PartialJoinError` exists to
        prevent, through a different door. Demonstrated with the reviewer's own
        repro: q1 appears twice in A (0.9 then 0.1); without the guard the join
        would "succeed" and q1's delta would depend on row order."""
        rows_a = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.9}),
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.1}),
        ]
        rows_b = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.5})]
        with pytest.raises(DuplicateQueryIdError) as excinfo:
            paired_join(rows_a, rows_b)
        assert excinfo.value.side == "A"
        assert excinfo.value.duplicate_counts == {"q1": 2}

    def test_a_duplicate_query_id_in_b_is_rejected(self) -> None:
        rows_a = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.5})]
        rows_b = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.9}),
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.1}),
        ]
        with pytest.raises(DuplicateQueryIdError) as excinfo:
            paired_join(rows_a, rows_b)
        assert excinfo.value.side == "B"

    def test_duplicate_check_runs_before_the_partial_join_check(self) -> None:
        """A duplicate on one side alongside a genuinely mismatched id set must
        still report as a duplicate, not get relabelled as a partial join."""
        rows_a = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.9}),
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.1}),
        ]
        rows_b = [_row("q2", "qmsum", "lookup", **{"nDCG@10": 0.5})]
        with pytest.raises(DuplicateQueryIdError):
            paired_join(rows_a, rows_b)


class TestPairedBootstrapCi:
    def test_zero_delta_everywhere_gives_a_zero_width_interval_at_zero(self) -> None:
        result = paired_bootstrap_ci([0.0] * 20, seed=0)
        assert result.delta_mean == 0.0
        assert result.ci_low == 0.0
        assert result.ci_high == 0.0

    def test_same_seed_is_bit_for_bit_reproducible(self) -> None:
        deltas = [0.1, -0.05, 0.2, 0.0, 0.15, -0.1, 0.05]
        first = paired_bootstrap_ci(deltas, seed=0, n_resamples=500)
        second = paired_bootstrap_ci(deltas, seed=0, n_resamples=500)
        assert first == second

    def test_different_seed_can_move_the_interval(self) -> None:
        rng = np.random.default_rng(1)
        deltas = list(rng.normal(loc=0.02, scale=0.1, size=40))
        a = paired_bootstrap_ci(deltas, seed=0, n_resamples=500)
        b = paired_bootstrap_ci(deltas, seed=1, n_resamples=500)
        # Same underlying data, different resample draws: the mean is fixed,
        # the interval need not be identical.
        assert a.delta_mean == b.delta_mean
        assert (a.ci_low, a.ci_high) != (b.ci_low, b.ci_high)

    def test_a_clear_positive_shift_produces_a_ci_excluding_zero(self) -> None:
        deltas = [0.2] * 30
        result = paired_bootstrap_ci(deltas, seed=0)
        assert result.ci_low > 0.0

    def test_empty_deltas_raises(self) -> None:
        with pytest.raises(ValueError, match="no paired queries"):
            paired_bootstrap_ci([])

    def test_default_n_resamples_is_pinned_at_10000(self) -> None:
        """Reviewer F8: `n_resamples`'s default is a #461 spec value (10,000), not
        an arbitrary literal — assert the field the function actually returns
        records it, so a changed default (e.g. to 37) is caught directly rather
        than by an indirect comparison that could pass by coincidence."""
        result = paired_bootstrap_ci([0.1, 0.2, 0.3], seed=0)
        assert result.n_resamples == 10_000

    def test_default_seed_is_pinned_at_0(self) -> None:
        """Reviewer F8: same for `seed` — no field records which seed produced a
        result, so this compares the DEFAULT call against an explicit seed=0 call;
        they can only be bit-for-bit identical if the default really is 0."""
        deltas = [0.12, -0.03, 0.27, 0.0, -0.15, 0.08, 0.19, -0.22, 0.05, 0.31]
        default = paired_bootstrap_ci(deltas)
        explicit = paired_bootstrap_ci(deltas, seed=0)
        assert default == explicit

    def test_confidence_level_changes_the_interval_width(self) -> None:
        """Reviewer F2: `confidence` must actually reach `np.quantile` — a mutant
        hardcoding `[0.05, 0.95]` would make every confidence level produce the
        SAME width, which this directly rules out."""
        rng = np.random.default_rng(9)
        deltas = list(rng.normal(loc=0.0, scale=1.0, size=500))
        narrow = paired_bootstrap_ci(deltas, seed=0, n_resamples=2000, confidence=0.50)
        wide = paired_bootstrap_ci(deltas, seed=0, n_resamples=2000, confidence=0.99)
        assert (narrow.ci_high - narrow.ci_low) < (wide.ci_high - wide.ci_low)

    def test_perfectly_correlated_arms_give_a_zero_width_ci(self) -> None:
        """Reviewer F1: the reviewer's own repro — swap the paired bootstrap for an
        UNPAIRED two-sample bootstrap (resampling A's and B's scores
        independently) and this must go red, because an unpaired resample would
        inherit each arm's own spread instead of the (constant) per-query delta.

        Constructed so each arm individually has real variance (`base.std()` well
        above 0), but B is EXACTLY A + 0.05 per query — so the delta is the same
        constant 0.05 for every query, and a correctly PAIRED bootstrap must
        collapse to a point interval at exactly that constant, regardless of how
        much the underlying scores vary."""
        rng = np.random.default_rng(5)
        base = rng.uniform(0.1, 0.9, size=50)
        assert base.std() > 0.15, "fixture sanity: the arms must have real spread"
        deltas = [0.05] * len(base)  # what a PAIRED bootstrap actually resamples
        result = paired_bootstrap_ci(deltas, seed=0, n_resamples=2000)
        assert result.delta_mean == pytest.approx(0.05)
        assert result.ci_low == pytest.approx(0.05, abs=1e-12)
        assert result.ci_high == pytest.approx(0.05, abs=1e-12)


class TestPairedTtest:
    def test_zero_variance_positive_mean_is_maximally_significant(self) -> None:
        result = paired_ttest([0.1] * 10)
        assert result.p_value == 0.0
        assert result.statistic > 0
        assert result.engine == "degenerate"

    def test_all_zero_deltas_is_not_significant(self) -> None:
        result = paired_ttest([0.0] * 10)
        assert result.p_value == 1.0
        assert result.statistic == 0.0

    def test_noisy_deltas_around_zero_do_not_reject_the_null(self) -> None:
        rng = np.random.default_rng(2)
        deltas = list(rng.normal(loc=0.0, scale=1.0, size=200))
        result = paired_ttest(deltas)
        assert result.p_value > 0.05

    def test_large_consistent_shift_rejects_the_null(self) -> None:
        rng = np.random.default_rng(3)
        deltas = list(rng.normal(loc=1.0, scale=0.1, size=200))
        result = paired_ttest(deltas)
        assert result.p_value < 0.001
        assert result.statistic > 0

    def test_needs_at_least_two_paired_queries(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            paired_ttest([0.1])

    def test_scipy_and_fallback_engines_agree_to_three_decimal_places(self) -> None:
        """The normal-approximation fallback exists so this module needs no new
        dependency when scipy is absent; it must actually approximate scipy,
        not merely run."""
        scipy_stats = pytest.importorskip("scipy.stats")
        rng = np.random.default_rng(4)
        deltas = list(rng.normal(loc=0.05, scale=0.2, size=150))
        scipy_result = paired_ttest(deltas)
        assert scipy_result.engine == "scipy"

        mean = float(np.mean(deltas))
        std = float(np.std(deltas, ddof=1))
        se = std / (len(deltas) ** 0.5)
        statistic = mean / se
        expected_p = float(scipy_stats.t.sf(abs(statistic), len(deltas) - 1) * 2)
        assert scipy_result.p_value == pytest.approx(expected_p, rel=1e-9)

        import math

        normal_approx_p = math.erfc(abs(statistic) / math.sqrt(2))
        assert scipy_result.p_value == pytest.approx(normal_approx_p, abs=5e-3)

    def test_the_normal_approx_fallback_actually_runs_when_scipy_is_unimportable(
        self,
    ) -> None:
        """Reviewer F7: the fallback path was previously untested — the test above
        only ever exercises the ``scipy`` branch, because scipy IS installed here.
        Force ``from scipy import stats`` to raise ``ImportError`` by blanking both
        module names in ``sys.modules`` for the duration of one call (Python's
        import machinery treats a ``None`` entry as "already tried, not found"),
        and assert the math.erfc fallback formula this module's own docstring
        specifies — not merely that SOME p-value came back."""
        import math
        import sys
        from unittest import mock

        deltas = [0.1, -0.05, 0.2, 0.0, 0.15, -0.1, 0.05, 0.12, -0.03, 0.07]
        with mock.patch.dict(sys.modules, {"scipy": None, "scipy.stats": None}):
            result = paired_ttest(deltas)
        assert result.engine == "normal-approx"

        mean = float(np.mean(deltas))
        std = float(np.std(deltas, ddof=1))
        se = std / (len(deltas) ** 0.5)
        statistic = mean / se
        expected_p = math.erfc(abs(statistic) / math.sqrt(2))
        assert result.statistic == pytest.approx(statistic)
        assert result.p_value == pytest.approx(expected_p)

        # Measured divergence from Student's t at this n (module docstring, F7):
        # roughly 2x apart at n=10, not a rounding difference — the fallback's
        # accuracy genuinely depends on the per-class n `summarize` calls it with.
        scipy_stats = pytest.importorskip("scipy.stats")
        scipy_p = float(scipy_stats.t.sf(abs(statistic), len(deltas) - 1) * 2)
        assert abs(result.p_value - scipy_p) > 0.01


class TestSummarize:
    def _paired_two_class_corpus(self) -> list[PairedQuery]:
        rows_a = [
            _row("l1", "qmsum", "lookup", **{"nDCG@10": 0.5, "MRR": 0.6}),
            _row("l2", "qmsum", "lookup", **{"nDCG@10": 0.4, "MRR": 0.5}),
            _row("m1", "qmsum", "multi_file", **{"nDCG@10": 0.3, "MRR": 0.3}),
        ]
        rows_b = [
            _row("l1", "qmsum", "lookup", **{"nDCG@10": 0.6, "MRR": 0.7}),
            _row("l2", "qmsum", "lookup", **{"nDCG@10": 0.5, "MRR": 0.6}),
            _row("m1", "qmsum", "multi_file", **{"nDCG@10": 0.3, "MRR": 0.3}),
        ]
        return paired_join(rows_a, rows_b)

    def test_lookup_class_is_always_broken_out_as_its_own_row(self) -> None:
        paired = self._paired_two_class_corpus()
        rows = summarize(paired, n_resamples=200)
        classes = {(row["corpus"], row["query_class"]) for row in rows}
        assert ("qmsum", "lookup") in classes
        assert ("qmsum", "multi_file") in classes
        assert ("qmsum", "all") in classes

    def test_lookup_only_corpus_still_reports_both_lookup_and_all(self) -> None:
        """Even when lookup is the ONLY class in a corpus, both rows must
        exist rather than 'all' silently standing in for it — a reader
        filtering for the lookup row specifically must always find one."""
        rows_a = [_row("l1", "synthetic", "lookup", **{"nDCG@10": 0.5})]
        rows_b = [_row("l1", "synthetic", "lookup", **{"nDCG@10": 0.6})]
        paired = paired_join(rows_a, rows_b)
        rows = summarize(paired, n_resamples=200)
        pairs = {(row["corpus"], row["query_class"]) for row in rows}
        assert ("synthetic", "lookup") in pairs
        assert ("synthetic", "all") in pairs

    def test_delta_mean_matches_manual_computation(self) -> None:
        paired = self._paired_two_class_corpus()
        rows = summarize(paired, n_resamples=200)
        lookup_ndcg = next(
            r for r in rows if r["query_class"] == "lookup" and r["measure"] == "nDCG@10"
        )
        assert lookup_ndcg["delta_mean"] == pytest.approx((0.1 + 0.1) / 2)
        assert lookup_ndcg["n"] == 2

    def test_ci_contains_zero_flag_matches_the_interval(self) -> None:
        paired = self._paired_two_class_corpus()
        rows = summarize(paired, n_resamples=200)
        assert rows, "summarize produced no rows — the loop below would vacuously pass"
        for row in rows:
            assert row["ci_contains_zero"] == (row["ci_low"] <= 0.0 <= row["ci_high"])

    def test_reports_every_measure_present_when_none_specified(self) -> None:
        paired = self._paired_two_class_corpus()
        rows = summarize(paired, n_resamples=200)
        measures = {row["measure"] for row in rows}
        assert measures == {"nDCG@10", "MRR"}

    def test_restricting_to_named_measures(self) -> None:
        paired = self._paired_two_class_corpus()
        rows = summarize(paired, measures=["MRR"], n_resamples=200)
        assert {row["measure"] for row in rows} == {"MRR"}

    def test_empty_input_returns_empty_list(self) -> None:
        assert summarize([]) == []

    def test_perfectly_correlated_arms_give_a_zero_width_ci_end_to_end(self) -> None:
        """Reviewer F1, at the level that actually matters: `summarize` swapped for
        an UNPAIRED two-sample bootstrap (resample A's and B's scores
        INDEPENDENTLY, then difference the resampled means) passed all 23 tests
        that existed before this one, because the point estimate
        (`delta_mean`) is identical either way — mean-of-differences ≡
        difference-of-means. Only the interval WIDTH tells them apart.

        Each arm has real per-query spread (`base.std() > 0.15`, asserted below),
        but B is exactly A + 0.05 for every query, so the delta is the SAME
        constant for every query. A correctly PAIRED bootstrap resamples that
        already-constant delta list and must collapse to a point interval at
        0.05 — an unpaired bootstrap would instead inherit each arm's own
        variance and report a wide one.
        """
        rng = np.random.default_rng(11)
        base = rng.uniform(0.1, 0.9, size=60)
        assert base.std() > 0.15, "fixture sanity: the arms must have real spread"
        rows_a = [
            _row(f"q{i}", "qmsum", "lookup", **{"nDCG@10": float(v)}) for i, v in enumerate(base)
        ]
        rows_b = [
            _row(f"q{i}", "qmsum", "lookup", **{"nDCG@10": float(v) + 0.05})
            for i, v in enumerate(base)
        ]
        paired = paired_join(rows_a, rows_b)
        rows = summarize(paired, n_resamples=2000, seed=0)
        row = next(r for r in rows if r["measure"] == "nDCG@10" and r["query_class"] == "all")
        assert row["delta_mean"] == pytest.approx(0.05)
        assert row["ci_low"] == pytest.approx(0.05, abs=1e-9)
        assert row["ci_high"] == pytest.approx(0.05, abs=1e-9)

    def test_a_measure_present_only_in_a_is_a_loud_error_not_a_keyerror(self) -> None:
        """Reviewer F4, direction 1: previously an uncaught KeyError deep in the
        delta loop — real, but not an intentional, named error."""
        rows_a = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.5, "MRR": 0.6})]
        rows_b = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.6})]
        paired = paired_join(rows_a, rows_b)
        with pytest.raises(ValueError, match="only in A.*MRR"):
            summarize(paired, n_resamples=50)

    def test_a_measure_present_only_in_b_is_also_a_loud_error_not_silently_dropped(
        self,
    ) -> None:
        """Reviewer F4, direction 2 — the dangerous one: before this fix, a
        measure only B had was silently absent from the table with exit 0,
        reading as "measured and unremarkable" rather than "never compared".
        This is exactly what happens comparing baselines from before/after a
        measure is added to a metric engine's MEASURES."""
        rows_a = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.5})]
        rows_b = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.6, "MRR": 0.7})]
        paired = paired_join(rows_a, rows_b)
        with pytest.raises(ValueError, match="only in B.*MRR"):
            summarize(paired, n_resamples=50)

    def test_explicit_measures_missing_from_one_side_of_one_query_also_raises(self) -> None:
        """Even when `measures=` is passed explicitly (bypassing the derivation
        this reviewer finding targets), a measure absent from one query's scores
        must still raise loudly rather than KeyError."""
        rows_a = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.5, "MRR": 0.6}),
            _row("q2", "qmsum", "lookup", **{"nDCG@10": 0.4}),  # no MRR here
        ]
        rows_b = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.6, "MRR": 0.7}),
            _row("q2", "qmsum", "lookup", **{"nDCG@10": 0.5, "MRR": 0.5}),
        ]
        paired = paired_join(rows_a, rows_b)
        with pytest.raises(ValueError, match="'MRR'.*'q2'|'q2'.*'MRR'"):
            summarize(paired, measures=["nDCG@10", "MRR"], n_resamples=50)

    def test_a_single_query_row_is_flagged_degenerate(self) -> None:
        """Reviewer F5: n=1 has no ttest and a bootstrap CI that necessarily
        collapses to a point (every resample draws the one value) —
        `ci_contains_zero=False` there would read as a confident result from a
        sample of one. Must be marked, not silently indistinguishable from a
        real result."""
        rows_a = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.5})]
        rows_b = [_row("q1", "qmsum", "lookup", **{"nDCG@10": 0.6})]
        paired = paired_join(rows_a, rows_b)
        rows = summarize(paired, n_resamples=50)
        row = next(r for r in rows if r["query_class"] == "all")
        assert row["n"] == 1
        assert row["degenerate"] is True

    def test_zero_variance_deltas_at_n_3_are_flagged_degenerate(self) -> None:
        """Reviewer F5: n=3 with identical deltas gives t=inf, p=0.0000 from
        `paired_ttest`'s own "degenerate" engine — reads as maximally significant
        while being a zero-variance artifact of too few identical observations."""
        rows_a = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.5}),
            _row("q2", "qmsum", "lookup", **{"nDCG@10": 0.5}),
            _row("q3", "qmsum", "lookup", **{"nDCG@10": 0.5}),
        ]
        rows_b = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.6}),
            _row("q2", "qmsum", "lookup", **{"nDCG@10": 0.6}),
            _row("q3", "qmsum", "lookup", **{"nDCG@10": 0.6}),
        ]
        paired = paired_join(rows_a, rows_b)
        rows = summarize(paired, n_resamples=50)
        row = next(r for r in rows if r["query_class"] == "all")
        assert row["p_value"] == 0.0
        assert row["degenerate"] is True

    def test_a_real_multi_query_result_with_spread_is_not_flagged_degenerate(self) -> None:
        """Control: a genuine result over several queries with real variance in the
        DELTAS (not just in the raw scores — `_paired_two_class_corpus`'s lookup
        class deltas are a constant +0.1, which is itself degenerate) must NOT be
        marked degenerate, or the flag would be worthless noise."""
        rows_a = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.5}),
            _row("q2", "qmsum", "lookup", **{"nDCG@10": 0.3}),
            _row("q3", "qmsum", "lookup", **{"nDCG@10": 0.6}),
        ]
        rows_b = [
            _row("q1", "qmsum", "lookup", **{"nDCG@10": 0.6}),  # delta +0.1
            _row("q2", "qmsum", "lookup", **{"nDCG@10": 0.5}),  # delta +0.2
            _row("q3", "qmsum", "lookup", **{"nDCG@10": 0.55}),  # delta -0.05
        ]
        paired = paired_join(rows_a, rows_b)
        rows = summarize(paired, n_resamples=200)
        lookup_row = next(
            r for r in rows if r["query_class"] == "lookup" and r["measure"] == "nDCG@10"
        )
        assert lookup_row["n"] == 3
        assert lookup_row["degenerate"] is False

    def test_confidence_is_carried_on_every_row(self) -> None:
        """Reviewer F2: the row must carry its own confidence level so a renderer
        never has to hardcode "95%"."""
        paired = self._paired_two_class_corpus()
        default_rows = summarize(paired, n_resamples=50)
        assert all(row["confidence"] == 0.95 for row in default_rows)
        custom_rows = summarize(paired, n_resamples=50, confidence=0.80)
        assert all(row["confidence"] == 0.80 for row in custom_rows)


class TestHigherIsBetter:
    """#461 W2.E1: the new rate/coverage measures mix higher- and lower-is-better in
    one table, unlike the nDCG-family measures this module started with."""

    def test_unlisted_measure_defaults_to_higher_is_better(self) -> None:
        assert higher_is_better("nDCG@10") is True
        assert higher_is_better("some_new_measure_nobody_registered") is True

    def test_false_attribution_rate_is_lower_is_better(self) -> None:
        assert higher_is_better("false_attribution_rate") is False


class TestSummarizeHandlesRateAndCoverageMeasures:
    """The rate/coverage measures ATTRIBUTION_PROBE and SPEAKER_ATTR/SPEAKER_SUMMARY
    produce are plain 0/1 or fractional floats in a `scores` dict — the exact shape
    `summarize` already consumes for nDCG. This proves it generalises rather than
    silently assuming an nDCG-shaped input, and that `higher_is_better` is threaded
    onto every row including a lower-is-better one."""

    def test_mixed_higher_and_lower_is_better_measures_in_one_summarize_call(self) -> None:
        rows_a = [
            _row(
                "p1",
                "qmsum",
                "attribution_probe",
                false_attribution_rate=1.0,
                answer_names_gold_speaker=0.0,
            ),
            _row(
                "p2",
                "qmsum",
                "attribution_probe",
                false_attribution_rate=1.0,
                answer_names_gold_speaker=1.0,
            ),
        ]
        rows_b = [
            _row(
                "p1",
                "qmsum",
                "attribution_probe",
                false_attribution_rate=0.0,
                answer_names_gold_speaker=1.0,
            ),
            _row(
                "p2",
                "qmsum",
                "attribution_probe",
                false_attribution_rate=0.0,
                answer_names_gold_speaker=1.0,
            ),
        ]
        paired = paired_join(rows_a, rows_b)
        rows = summarize(paired, n_resamples=200)
        assert rows, "summarize produced no rows"

        far = next(r for r in rows if r["measure"] == "false_attribution_rate")
        # A REDUCTION in false_attribution_rate (1.0 -> 0.0) is an IMPROVEMENT, and
        # delta_mean is still computed as B - A = negative, unchanged by direction.
        assert far["delta_mean"] == pytest.approx(-1.0)
        assert far["higher_is_better"] is False

        names = next(r for r in rows if r["measure"] == "answer_names_gold_speaker")
        assert names["higher_is_better"] is True
