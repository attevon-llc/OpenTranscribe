"""Tests for ``harness.chat_instrumentation`` — per-turn extractors (#461 W2.E1).

Pure dict-in, value-out functions; no chat pipeline, LLM, or stack involved. The
central behaviour under test is "absent is not zero": every extractor must return
``None`` when its source key is missing, never a default that could be misread as a
measurement — see the module's own docstring for which of the three fields have a
real emitter today and which do not (verified against the current codebase, not
assumed).
"""

from __future__ import annotations

from tests.eval.harness.chat_instrumentation import TurnInstrumentation
from tests.eval.harness.chat_instrumentation import extract_llm_calls
from tests.eval.harness.chat_instrumentation import extract_planner_fired
from tests.eval.harness.chat_instrumentation import extract_router_language_unmatched
from tests.eval.harness.chat_instrumentation import extract_scope_coverage
from tests.eval.harness.chat_instrumentation import extract_turn_instrumentation
from tests.eval.harness.chat_instrumentation import summarize_instrumentation


class TestExtractRouterLanguageUnmatched:
    def test_absent_route_block_is_none(self) -> None:
        assert extract_router_language_unmatched({}) is None

    def test_route_block_without_the_key_is_none(self) -> None:
        assert extract_router_language_unmatched({"route": {"intent": "lookup"}}) is None

    def test_present_and_true(self) -> None:
        meta = {"route": {"language_unmatched": True}}
        assert extract_router_language_unmatched(meta) is True

    def test_present_and_false_is_a_real_measured_false_not_none(self) -> None:
        meta = {"route": {"language_unmatched": False}}
        assert extract_router_language_unmatched(meta) is False

    def test_route_present_but_not_a_dict_is_none(self) -> None:
        assert extract_router_language_unmatched({"route": "not-a-dict"}) is None


class TestExtractPlannerFired:
    def test_absent_planner_block_is_none(self) -> None:
        assert extract_planner_fired({}) is None

    def test_present_and_true(self) -> None:
        assert extract_planner_fired({"planner": {"fired": True}}) is True

    def test_present_and_false(self) -> None:
        assert extract_planner_fired({"planner": {"fired": False}}) is False


class TestExtractLlmCalls:
    def test_no_overview_block_is_none_not_zero(self) -> None:
        """A lookup-routed turn carries no `meta['overview']` at all — this must
        read as 'not measured', never as a measured zero calls."""
        assert extract_llm_calls({}) is None
        assert extract_llm_calls({"route": {"intent": "lookup"}}) is None

    def test_overview_present_with_zero_calls_is_a_real_measured_zero(self) -> None:
        meta = {"overview": {"llm_calls": 0, "reducer": "no-llm"}}
        assert extract_llm_calls(meta) == 0

    def test_overview_present_with_nonzero_calls(self) -> None:
        meta = {"overview": {"llm_calls": 3}}
        assert extract_llm_calls(meta) == 3

    def test_overview_present_but_not_a_dict_is_none(self) -> None:
        assert extract_llm_calls({"overview": "not-a-dict"}) is None


class TestExtractScopeCoverage:
    def test_no_overview_block_is_none_not_zero(self) -> None:
        """A lookup-routed turn, or an unbounded scope with no bounded map to
        measure — either way there is no coverage claim to report, same rule
        ``mapreduce.coverage.check_scope_coverage`` applies for ``file_uuids is
        None`` (``applicable=False`` rather than a fabricated ratio)."""
        assert extract_scope_coverage({}) is None
        assert extract_scope_coverage({"route": {"intent": "lookup"}}) is None

    def test_overview_present_but_not_a_dict_is_none(self) -> None:
        assert extract_scope_coverage({"overview": "not-a-dict"}) is None

    def test_files_in_scope_absent_is_none(self) -> None:
        assert extract_scope_coverage({"overview": {"files_total": 3}}) is None

    def test_files_in_scope_zero_is_none(self) -> None:
        """0 is ambiguous between 'unbounded' and 'a genuinely empty scope' once
        it has passed through `meta` alone — refuse rather than guess."""
        assert extract_scope_coverage({"overview": {"files_total": 0, "files_in_scope": 0}}) is None

    def test_full_coverage_with_no_gap_counters_is_1_0(self) -> None:
        meta = {"overview": {"files_total": 25, "files_in_scope": 25}}
        assert extract_scope_coverage(meta) == 1.0

    def test_partial_coverage_with_no_accounted_gap_is_the_real_bug_shape(self) -> None:
        """THE headline shape: 8 of 25, and nothing on `meta` explains the other 17."""
        meta = {"overview": {"files_total": 8, "files_in_scope": 25}}
        assert extract_scope_coverage(meta) == 8 / 25

    def test_gap_counters_bring_a_partial_map_back_to_1_0_when_reasoned(self) -> None:
        """8 touched + 12 without-artifacts + 5 no-content == the full 25-file scope."""
        meta = {
            "overview": {"files_total": 8, "files_in_scope": 25},
            "map_files_without_artifacts": 12,
            "map_files_no_content": 5,
        }
        assert extract_scope_coverage(meta) == 1.0

    def test_gap_counters_absent_default_to_zero_not_none(self) -> None:
        """Absent here means the emission rule already guaranteed zero (`chat/service.py`
        only sets the key when nonzero) — the opposite convention from `llm_calls`
        above, and documented as such on the function."""
        meta = {"overview": {"files_total": 25, "files_in_scope": 25}}
        assert extract_scope_coverage(meta) == extract_scope_coverage(
            {**meta, "map_files_without_artifacts": 0, "map_files_no_content": 0}
        )

    def test_ratio_is_clamped_at_1_0(self) -> None:
        """An over-reporting gap counter (a caller's own bookkeeping bug) must not
        report MORE than fully covered — 1.0 is the ceiling, not a claim of
        over-coverage."""
        meta = {
            "overview": {"files_total": 25, "files_in_scope": 25},
            "map_files_without_artifacts": 5,
        }
        assert extract_scope_coverage(meta) == 1.0


class TestExtractTurnInstrumentation:
    def test_fully_absent_turn(self) -> None:
        result = extract_turn_instrumentation({})
        assert result == TurnInstrumentation(
            router_language_unmatched=None,
            planner_fired=None,
            llm_calls=None,
            scope_coverage=None,
        )

    def test_partially_measured_turn(self) -> None:
        meta = {"overview": {"llm_calls": 2, "files_total": 8, "files_in_scope": 25}}
        result = extract_turn_instrumentation(meta)
        assert result.llm_calls == 2
        assert result.router_language_unmatched is None
        assert result.planner_fired is None
        assert result.scope_coverage == 8 / 25

    def test_as_json_keeps_none_as_null_not_a_default(self) -> None:
        result = TurnInstrumentation(
            router_language_unmatched=None,
            planner_fired=None,
            llm_calls=None,
            scope_coverage=None,
        )
        payload = result.as_json()
        assert payload == {
            "router_language_unmatched": None,
            "planner_fired": None,
            "llm_calls": None,
            "scope_coverage": None,
        }


class TestSummarizeInstrumentation:
    def test_zero_coverage_field_has_no_rate_or_mean_key(self) -> None:
        rows = [
            TurnInstrumentation(
                router_language_unmatched=None,
                planner_fired=None,
                llm_calls=None,
                scope_coverage=None,
            )
        ]
        summary = summarize_instrumentation(rows)
        assert summary["router_language_unmatched"]["coverage"] == 0
        assert "rate" not in summary["router_language_unmatched"]
        assert summary["llm_calls"]["coverage"] == 0
        assert "mean" not in summary["llm_calls"]
        assert summary["scope_coverage"]["coverage"] == 0
        assert "mean" not in summary["scope_coverage"]
        assert "min" not in summary["scope_coverage"]

    def test_partial_coverage_reports_rate_over_measured_turns_only(self) -> None:
        rows = [
            TurnInstrumentation(
                router_language_unmatched=True,
                planner_fired=None,
                llm_calls=None,
                scope_coverage=None,
            ),
            TurnInstrumentation(
                router_language_unmatched=False,
                planner_fired=None,
                llm_calls=None,
                scope_coverage=None,
            ),
            TurnInstrumentation(
                router_language_unmatched=None,
                planner_fired=None,
                llm_calls=None,
                scope_coverage=None,
            ),
        ]
        summary = summarize_instrumentation(rows)
        entry = summary["router_language_unmatched"]
        assert entry["coverage"] == 2
        assert entry["total"] == 3
        assert entry["rate"] == 0.5

    def test_llm_calls_mean_is_over_measured_turns_only(self) -> None:
        rows = [
            TurnInstrumentation(
                router_language_unmatched=None, planner_fired=None, llm_calls=2, scope_coverage=None
            ),
            TurnInstrumentation(
                router_language_unmatched=None, planner_fired=None, llm_calls=4, scope_coverage=None
            ),
            TurnInstrumentation(
                router_language_unmatched=None,
                planner_fired=None,
                llm_calls=None,
                scope_coverage=None,
            ),
        ]
        summary = summarize_instrumentation(rows)
        assert summary["llm_calls"] == {"coverage": 2, "total": 3, "mean": 3.0}

    def test_scope_coverage_reports_mean_and_min_over_measured_turns_only(self) -> None:
        """`min`, not just `mean` — a corpus-wide average of 0.9 could still hide
        one turn that dropped an entire file's worth of scope with no accounting,
        and averaging alone would bury exactly that turn."""
        rows = [
            TurnInstrumentation(
                router_language_unmatched=None,
                planner_fired=None,
                llm_calls=None,
                scope_coverage=1.0,
            ),
            TurnInstrumentation(
                router_language_unmatched=None,
                planner_fired=None,
                llm_calls=None,
                scope_coverage=0.32,
            ),
            TurnInstrumentation(
                router_language_unmatched=None,
                planner_fired=None,
                llm_calls=None,
                scope_coverage=None,
            ),
        ]
        summary = summarize_instrumentation(rows)
        assert summary["scope_coverage"]["coverage"] == 2
        assert summary["scope_coverage"]["total"] == 3
        assert summary["scope_coverage"]["mean"] == (1.0 + 0.32) / 2
        assert summary["scope_coverage"]["min"] == 0.32

    def test_empty_rows_reports_zero_coverage_for_every_field(self) -> None:
        summary = summarize_instrumentation([])
        for field_name in (
            "router_language_unmatched",
            "planner_fired",
            "llm_calls",
            "scope_coverage",
        ):
            assert summary[field_name]["coverage"] == 0
            assert summary[field_name]["total"] == 0
