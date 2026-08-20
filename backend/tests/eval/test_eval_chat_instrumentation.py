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


class TestExtractTurnInstrumentation:
    def test_fully_absent_turn(self) -> None:
        result = extract_turn_instrumentation({})
        assert result == TurnInstrumentation(
            router_language_unmatched=None, planner_fired=None, llm_calls=None
        )

    def test_partially_measured_turn(self) -> None:
        meta = {"overview": {"llm_calls": 2}}
        result = extract_turn_instrumentation(meta)
        assert result.llm_calls == 2
        assert result.router_language_unmatched is None
        assert result.planner_fired is None

    def test_as_json_keeps_none_as_null_not_a_default(self) -> None:
        result = TurnInstrumentation(
            router_language_unmatched=None, planner_fired=None, llm_calls=None
        )
        payload = result.as_json()
        assert payload == {
            "router_language_unmatched": None,
            "planner_fired": None,
            "llm_calls": None,
        }


class TestSummarizeInstrumentation:
    def test_zero_coverage_field_has_no_rate_or_mean_key(self) -> None:
        rows = [
            TurnInstrumentation(router_language_unmatched=None, planner_fired=None, llm_calls=None)
        ]
        summary = summarize_instrumentation(rows)
        assert summary["router_language_unmatched"]["coverage"] == 0
        assert "rate" not in summary["router_language_unmatched"]
        assert summary["llm_calls"]["coverage"] == 0
        assert "mean" not in summary["llm_calls"]

    def test_partial_coverage_reports_rate_over_measured_turns_only(self) -> None:
        rows = [
            TurnInstrumentation(router_language_unmatched=True, planner_fired=None, llm_calls=None),
            TurnInstrumentation(
                router_language_unmatched=False, planner_fired=None, llm_calls=None
            ),
            TurnInstrumentation(router_language_unmatched=None, planner_fired=None, llm_calls=None),
        ]
        summary = summarize_instrumentation(rows)
        entry = summary["router_language_unmatched"]
        assert entry["coverage"] == 2
        assert entry["total"] == 3
        assert entry["rate"] == 0.5

    def test_llm_calls_mean_is_over_measured_turns_only(self) -> None:
        rows = [
            TurnInstrumentation(router_language_unmatched=None, planner_fired=None, llm_calls=2),
            TurnInstrumentation(router_language_unmatched=None, planner_fired=None, llm_calls=4),
            TurnInstrumentation(router_language_unmatched=None, planner_fired=None, llm_calls=None),
        ]
        summary = summarize_instrumentation(rows)
        assert summary["llm_calls"] == {"coverage": 2, "total": 3, "mean": 3.0}

    def test_empty_rows_reports_zero_coverage_for_every_field(self) -> None:
        summary = summarize_instrumentation([])
        for field_name in ("router_language_unmatched", "planner_fired", "llm_calls"):
            assert summary[field_name]["coverage"] == 0
            assert summary[field_name]["total"] == 0
