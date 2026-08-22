"""Tests for the per-query retrieval report and the committed-baseline integrity sweep.

Two things live here (#461 phase A1):

1. **Unit tests for ``report.build_retrieval_per_query``** — the function
   ``8117e6f3`` added so a retrieval number stops being a bare point estimate
   (see ``harness/significance.py`` and ``rag-evaluation.md``'s "Paired
   significance" section for what those per-query rows make computable).
2. **A baseline-integrity sweep** over every committed baseline under
   ``tests/eval/baselines/``. It found — while this file was being written —
   that "pre-schema-v2 baselines are exempt" is not what the checked-in tree
   actually looks like: six of the eight non-MIRACL baselines report
   ``schema_version: 2`` and STILL carry no ``retrieval_per_query``, because
   they were generated before ``8117e6f3`` landed the field, not before the v2
   results schema. Only ``stage1-baseline`` and ``stage1-baseline-goldscope``
   are genuinely schema v1. So the exemption below is keyed by **baseline
   name with a written reason**, matching this repo's
   ``audit-allowlist.txt`` convention, rather than by schema version alone —
   a schema-version-only rule would have had to either fail on six committed,
   intentionally-historical baselines (``baselines/README.md`` marks each one
   "historical — do not regenerate" or "re-derivable but needs the live
   rag403 stack") or silently pass them, which is the blanket-skip this sweep
   is required not to do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.eval.harness.corpora import EvalQuery
from tests.eval.harness.metrics import EvalResult
from tests.eval.harness.qrels import GoldSpan
from tests.eval.harness.report import build_retrieval_per_query

FILE_A = "3f2a9c10-0000-0000-0000-000000000000"
FILE_B = "3f2a9c10-0000-0000-0000-000000000001"

BASELINES_DIR = Path(__file__).resolve().parent / "baselines"

#: Baselines exempt from the "schema_version 2 implies non-empty
#: retrieval_per_query" rule, keyed by baseline directory name with a
#: mandatory written reason — never a blanket schema-version skip. A
#: baseline dropped from this dict starts being enforced on the very next
#: run; an entry naming a baseline that no longer needs the exemption is a
#: STALE entry and fails the sweep (see
#: ``test_stale_allowlist_entry_is_rejected``), same as
#: ``backend/tests/audit-allowlist.txt``.
LEGACY_BASELINE_ALLOWLIST: dict[str, str] = {
    "stage1-baseline": (
        "schema_version 1 — predates the v2 results schema entirely, not just "
        "retrieval_per_query. baselines/README.md: 'historical, do not regenerate' "
        "(measured an index that no longer exists)."
    ),
    "stage1-baseline-goldscope": (
        "schema_version 1, same as stage1-baseline — the oracle gold-file-scope arm "
        "of the same historical measurement."
    ),
    "stage1-synthetic-answers": (
        "schema_version 2 but generated before 8117e6f3 landed build_retrieval_per_query. "
        "baselines/README.md: 'historical, do not regenerate' — its embedding model and "
        "index (208,333 chunks, pre-v6) no longer exist, so it cannot be regenerated to "
        "backfill the field without becoming a measurement of a different index under the "
        "old name, which is exactly what this baselines directory exists to prevent."
    ),
    "stage3-control-pre-v6": (
        "schema_version 2, pre-8117e6f3. baselines/README.md: 'historical, do not "
        "regenerate' — the *before* arm of the v6 A/B; the index it measured is gone."
    ),
    "stage3-index-v6": (
        "schema_version 2, pre-8117e6f3. baselines/README.md marks it 're-derived — "
        "reproduces bit-for-bit' against the live rag403 stack, but regenerating it needs "
        "that stack and is out of scope for #461 phase A1 (CI-safe, no-stack lane); tracked "
        "as a follow-up regeneration, not a currently-broken measurement."
    ),
    "stage4-control": (
        "schema_version 2, pre-8117e6f3, re-derivable but needs the live rag403 stack — "
        "same situation as stage3-index-v6."
    ),
    "stage4-routed": (
        "schema_version 2, pre-8117e6f3, re-derivable but needs the live rag403 stack — "
        "same situation as stage3-index-v6."
    ),
    "stage4-aggregation": (
        "schema_version 2, pre-8117e6f3, re-derivable but needs the live rag403 stack — "
        "same situation as stage3-index-v6. Also carries answer-scored aggregation rows, "
        "which are outside build_retrieval_per_query's scope even once regenerated."
    ),
}


def _make_query(
    query_id: str,
    *,
    corpus: str = "synthetic",
    query_class: str = "lookup",
    scored_on: str = "retrieval",
    spans: tuple[GoldSpan, ...] = (),
) -> EvalQuery:
    return EvalQuery(
        query_id=query_id,
        text=f"text for {query_id}",
        query_class=query_class,
        corpus=corpus,
        license_tier="A",
        spans=spans,
        scored_on=scored_on,
    )


class TestBuildRetrievalPerQuery:
    """Unit tests for ``report.build_retrieval_per_query``."""

    def test_emits_one_row_per_scored_query_sorted_by_id(self) -> None:
        queries = [
            _make_query("q2", spans=(GoldSpan(FILE_A, 0, 1),)),
            _make_query("q1", spans=(GoldSpan(FILE_A, 0, 0),)),
        ]
        result = EvalResult(
            per_query={
                "q1": {"nDCG@10": 0.5},
                "q2": {"nDCG@10": 0.75},
            },
            query_count=2,
        )
        rows = build_retrieval_per_query(queries, result)
        assert [row["query_id"] for row in rows] == ["q1", "q2"]

    def test_carries_corpus_class_tier_and_gold_count(self) -> None:
        span = GoldSpan(FILE_A, 2, 5)  # 4 inclusive turn indices
        queries = [
            _make_query(
                "q1", corpus="qmsum", query_class="multi_file", spans=(span, GoldSpan(FILE_B, 0, 0))
            )
        ]
        result = EvalResult(per_query={"q1": {"nDCG@10": 0.5}}, query_count=1)
        rows = build_retrieval_per_query(queries, result)
        assert rows == [
            {
                "query_id": "q1",
                "corpus": "qmsum",
                "query_class": "multi_file",
                "license_tier": "A",
                "gold_count": 2,
                "scores": {"nDCG@10": 0.5},
            }
        ]

    def test_rounds_scores_to_six_decimal_places(self) -> None:
        queries = [_make_query("q1", spans=(GoldSpan(FILE_A, 0, 0),))]
        result = EvalResult(per_query={"q1": {"nDCG@10": 1.0 / 3.0}}, query_count=1)
        rows = build_retrieval_per_query(queries, result)
        assert rows[0]["scores"]["nDCG@10"] == round(1.0 / 3.0, 6)
        # Proves rounding actually happened rather than the raw float
        # surviving by coincidence.
        assert rows[0]["scores"]["nDCG@10"] != 1.0 / 3.0

    def test_excludes_answer_scored_queries_even_if_result_has_a_row_for_them(self) -> None:
        """An answer-scored query has its own table (build_answer_details); if
        its id also showed up here, aggregation's EM score would look like a
        retrieval nDCG in the same file, the exact confusion build_rows'
        docstring warns about for the aggregate table."""
        queries = [
            _make_query("q-retrieval", spans=(GoldSpan(FILE_A, 0, 0),)),
            _make_query("q-answer", scored_on="answer"),
        ]
        # The EvalResult (retrieval engine output) happens to carry a row keyed
        # by the answer query's id too -- e.g. because a caller reused ids
        # across tables by mistake. build_retrieval_per_query must not surface it.
        result = EvalResult(
            per_query={
                "q-retrieval": {"nDCG@10": 0.5},
                "q-answer": {"nDCG@10": 0.9},
            },
            query_count=2,
        )
        rows = build_retrieval_per_query(queries, result)
        assert [row["query_id"] for row in rows] == ["q-retrieval"]

    def test_empty_result_produces_empty_list(self) -> None:
        assert build_retrieval_per_query([_make_query("q1")], EvalResult()) == []


def _iter_baseline_metrics(baselines_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """(baseline name, parsed metrics.json) for every RETRIEVAL-scored baseline
    directory that has one.

    Two different reasons a directory is excluded, kept distinguishable on
    purpose:

    * No ``metrics.json`` at all (``stage4-router`` — a classifier report that
      touches no index, per ``baselines/README.md``) is not a retrieval-scored
      baseline and never reaches this sweep.
    * A ``metrics.json`` that is not ``report.build_results``'s own schema —
      ``probe-chat-live-2026-08-20`` (issue #72), from
      ``tests.eval.harness.probe_metrics.build_probe_results`` — is a different
      instrument (the live chat-RAG HTTP probe) with its own ``rows``/
      ``summary``/``target`` shape, not a `schema_version`-2-without-
      `retrieval_per_query` case of THIS shape. Forcing it through
      ``LEGACY_BASELINE_ALLOWLIST`` would assert something false: that it is a
      pre-``8117e6f3`` retrieval baseline waiting to be regenerated, which it
      is not and never will be. Detected structurally by the absence of
      ``control_name`` — a required, no-default keyword of
      ``report.build_results`` that is therefore present in every genuine
      retrieval baseline and in none of this family.
    """
    found: list[tuple[str, dict[str, Any]]] = []
    for entry in sorted(baselines_dir.iterdir()):
        if not entry.is_dir():
            continue
        metrics_path = entry / "metrics.json"
        if not metrics_path.is_file():
            continue
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        if "control_name" not in metrics:
            continue
        found.append((entry.name, metrics))
    return found


def check_baseline_integrity(
    baselines: list[tuple[str, dict[str, Any]]], allowlist: dict[str, str]
) -> list[str]:
    """Return one violation string per baseline that fails the integrity rule.

    Rule: every baseline with ``schema_version == 2`` must carry a non-empty
    ``retrieval_per_query`` UNLESS its name is in ``allowlist`` (with a
    reason). An allowlist entry for a baseline that now satisfies the rule
    anyway is itself a violation (stale exemption) — same convention as
    ``scripts/audit-tests.py``'s allowlist.
    """
    violations: list[str] = []
    for name, metrics in baselines:
        has_rows = bool(metrics.get("retrieval_per_query"))
        exempt = name in allowlist
        if exempt and has_rows:
            violations.append(
                f"{name}: allowlist entry is STALE — it now carries "
                f"{len(metrics['retrieval_per_query'])} retrieval_per_query row(s); "
                "remove it from LEGACY_BASELINE_ALLOWLIST"
            )
            continue
        if exempt:
            continue
        if metrics.get("schema_version") != 2:
            violations.append(
                f"{name}: schema_version={metrics.get('schema_version')!r} is not 2 and "
                "the baseline is not in LEGACY_BASELINE_ALLOWLIST — a new pre-v2 baseline "
                "must be explicitly allowlisted with a reason, not silently accepted"
            )
        elif not has_rows:
            violations.append(
                f"{name}: schema_version 2 with empty/missing retrieval_per_query and no "
                "allowlist entry — regenerate it or add a named, reasoned exemption"
            )

    # An allowlist entry naming a baseline that is not on disk AT ALL is never
    # reached by the loop above — it iterates `baselines`, not `allowlist` — so
    # this second pass is required, not redundant. `audit-allowlist.txt`'s own
    # convention (this sweep is modelled on it) rejects both shapes of stale
    # entry: one whose finding is gone, and one whose SUBJECT is gone. Only the
    # first was checked before this pass existed.
    seen_names = {name for name, _ in baselines}
    for name in sorted(set(allowlist) - seen_names):
        violations.append(
            f"{name}: allowlist entry names a baseline that does not exist under "
            "the baselines directory at all — remove it from LEGACY_BASELINE_ALLOWLIST"
        )
    return violations


class TestIterBaselineMetrics:
    """``_iter_baseline_metrics``'s non-retrieval-baseline exclusion (issue #72)."""

    def test_excludes_a_directory_whose_metrics_json_has_no_control_name(
        self, tmp_path: Path
    ) -> None:
        retrieval_dir = tmp_path / "a-real-retrieval-baseline"
        retrieval_dir.mkdir()
        (retrieval_dir / "metrics.json").write_text(
            json.dumps({"control_name": "stage1-baseline", "schema_version": 2}),
            encoding="utf-8",
        )
        probe_dir = tmp_path / "probe-chat-live-2026-08-20"
        probe_dir.mkdir()
        (probe_dir / "metrics.json").write_text(
            json.dumps({"schema_version": 1, "run_name": "probe-chat-live-2026-08-20"}),
            encoding="utf-8",
        )

        found = _iter_baseline_metrics(tmp_path)

        assert [name for name, _ in found] == ["a-real-retrieval-baseline"]

    def test_the_committed_probe_baseline_is_excluded_from_the_real_sweep(self) -> None:
        """The must-stay-clean case against the real, committed artifact rather
        than a synthetic stand-in — proves this isn't just a passing fixture."""
        found = _iter_baseline_metrics(BASELINES_DIR)
        assert "probe-chat-live-2026-08-20" not in {name for name, _ in found}


class TestBaselineIntegritySweep:
    """Every committed schema-v2 baseline must carry per-query retrieval rows,
    or be named in ``LEGACY_BASELINE_ALLOWLIST`` with a reason."""

    def test_committed_baselines_pass(self) -> None:
        baselines = _iter_baseline_metrics(BASELINES_DIR)
        # A collection this sweep silently iterates over zero of proves
        # nothing; pin the count so a baselines/ layout change is visible.
        assert len(baselines) >= 10, (
            f"expected at least 10 baseline dirs with metrics.json under {BASELINES_DIR}, "
            f"found {len(baselines)} — is the baselines directory being read at all?"
        )
        violations = check_baseline_integrity(baselines, LEGACY_BASELINE_ALLOWLIST)
        assert not violations, "baseline integrity violations:\n" + "\n".join(violations)

    def test_miracl_baselines_are_not_in_the_allowlist(self) -> None:
        """The two baselines this sweep exists to protect (retrieval_per_query
        landed WITH them) must be enforced, not accidentally exempted."""
        assert "miracl-es-english" not in LEGACY_BASELINE_ALLOWLIST
        assert "miracl-es-multilingual" not in LEGACY_BASELINE_ALLOWLIST

    def test_schema_v2_baseline_missing_retrieval_per_query_is_flagged(self) -> None:
        """A NEW schema-v2 baseline that forgot retrieval_per_query, and was
        never allowlisted, must fail — this is the primary case the sweep
        guards against a regression in."""
        baselines = [("brand-new", {"schema_version": 2, "retrieval_per_query": []})]
        violations = check_baseline_integrity(baselines, {})
        assert len(violations) == 1
        assert "brand-new" in violations[0]

    def test_new_pre_v2_baseline_without_an_allowlist_entry_is_flagged(self) -> None:
        """A future baseline that regresses to schema_version 1 without an
        explicit, reasoned exemption must fail — the blanket-skip this sweep
        was required NOT to implement would let this through silently."""
        baselines = [("some-old-shaped-run", {"schema_version": 1})]
        violations = check_baseline_integrity(baselines, {})
        assert len(violations) == 1
        assert "some-old-shaped-run" in violations[0]

    def test_stale_allowlist_entry_is_rejected(self) -> None:
        """An allowlist entry for a baseline that HAS since gained
        retrieval_per_query is stale and must fail, exactly like a stale
        ``audit-allowlist.txt`` entry — otherwise the allowlist could only
        grow and a fixed baseline would stay silently unchecked forever."""
        baselines = [
            ("regenerated", {"schema_version": 2, "retrieval_per_query": [{"query_id": "q1"}]})
        ]
        violations = check_baseline_integrity(baselines, {"regenerated": "no longer accurate"})
        assert len(violations) == 1
        assert "STALE" in violations[0]

    def test_allowlisted_legacy_baseline_with_no_rows_passes(self) -> None:
        baselines = [("stage1-baseline", {"schema_version": 1})]
        violations = check_baseline_integrity(
            baselines, {"stage1-baseline": "historical, see README"}
        )
        assert violations == []

    def test_an_allowlist_entry_naming_a_baseline_absent_from_disk_is_flagged(self) -> None:
        """Reviewer F6: the main loop iterates `baselines` (what's on disk), so an
        allowlist entry for a name that never appears there — the baseline
        directory was deleted or renamed — was previously NEVER reached and
        never flagged. `audit-allowlist.txt`'s convention (this sweep is modelled
        on it) rejects a stale entry whether its finding is gone OR its subject
        is gone; only the first was checked before this test existed."""
        baselines = [("still-here", {"schema_version": 2, "retrieval_per_query": [{"q": 1}]})]
        violations = check_baseline_integrity(
            baselines, {"deleted-long-ago": "historical, see README"}
        )
        assert len(violations) == 1
        assert "deleted-long-ago" in violations[0]

    def test_an_allowlist_entry_for_an_existing_but_still_missing_baseline_is_not_double_flagged(
        self,
    ) -> None:
        """The orphan check must not ALSO fire for an entry that IS on disk and
        genuinely still needs its exemption — only entries absent from `baselines`
        entirely are orphaned."""
        baselines = [("stage1-baseline", {"schema_version": 1})]
        violations = check_baseline_integrity(
            baselines, {"stage1-baseline": "historical, see README"}
        )
        assert violations == []

    @pytest.mark.parametrize("name", sorted(LEGACY_BASELINE_ALLOWLIST))
    def test_every_allowlist_entry_has_a_real_reason(self, name: str) -> None:
        reason = LEGACY_BASELINE_ALLOWLIST[name]
        assert isinstance(reason, str) and len(reason.strip()) >= 20, (
            f"LEGACY_BASELINE_ALLOWLIST[{name!r}] needs a substantive written reason"
        )
