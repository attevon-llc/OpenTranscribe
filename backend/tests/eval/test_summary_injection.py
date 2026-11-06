"""Arm injection and failure injection, end to end through the app's own summary pipeline.

No network and no model: the transport under `EvalLLMService` is a fake that returns canned,
schema-valid JSON, so the real chunker, slice prompt, `ThreadPoolExecutor` fan-out, `_error`
placeholders, merge payload and merge call all run.
"""

from __future__ import annotations

import contextlib
import inspect
import json
from types import SimpleNamespace

import pytest

from tests.eval.summary_bench_support import MERGE as SUPPORT_MERGE
from tests.eval.summary_bench_support import SCOPE as SUPPORT_SCOPE
from tests.eval.summary_bench_support import FakeTransport
from tests.eval.summary_bench_support import load_bench
from tests.eval.summary_bench_support import make_context as _context

SCOPE = SUPPORT_SCOPE
MERGE = SUPPORT_MERGE


@pytest.fixture(scope="module")
def bench() -> SimpleNamespace:
    return load_bench()


class TestArmInjection:
    def test_every_slice_call_carries_the_scope_rules_and_the_merge_call_the_merge_rules(
        self, bench: SimpleNamespace
    ) -> None:
        transport = FakeTransport()
        ctx, entry = _context(bench, transport, mode=None)
        record = bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))
        slices = [s for phase, s in transport.systems if phase == "slice"]
        merges = [s for phase, s in transport.systems if phase == "merge"]
        assert len(slices) == len(entry["chunks"])
        assert all(SCOPE in s and MERGE not in s for s in slices)
        assert len(merges) == 1
        assert MERGE in merges[0] and SCOPE not in merges[0]
        assert record["status"] == "ok"
        assert record["applied"] == {"checked": len(entry["chunks"]) + 1, "failed": 0}
        assert record["merge_attempts"] == 1

    def test_without_the_patch_the_applied_check_fails_and_the_cell_is_void(
        self, bench: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = FakeTransport()
        ctx, _ = _context(bench, transport, mode=None)
        # Skip the injection only: the app then uses its own module values, not the arm's.
        monkeypatch.setattr(bench.runner, "inject_arm", lambda arm: contextlib.nullcontext())
        record = bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))
        assert record["applied"]["failed"] == record["applied"]["checked"] > 0
        assert (record["status"], record["void_reason"]) == ("void", "arm_not_applied")
        assert not any(SCOPE in s for _, s in transport.systems)

    def test_the_record_holds_hashes_of_the_rules_never_their_text(
        self, bench: SimpleNamespace
    ) -> None:
        ctx, _ = _context(bench, FakeTransport(), mode=None)
        record = bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))
        assert "SENTINEL" not in json.dumps(record)
        assert set(record["arm_hashes"]) == {
            "name",
            "scope_sha256",
            "scope_len",
            "merge_sha256",
            "merge_len",
        }

    def test_the_seed_pairs_run_k_across_arms_and_is_sent_to_vllm_only(
        self, bench: SimpleNamespace
    ) -> None:
        transport = FakeTransport()
        ctx, _ = _context(bench, transport, mode=None)
        bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 3))
        bench.runner.run_cell(ctx, bench.runner.CellId("IX", "y", 3))
        seeds = {p["seed"] for p in transport.payloads}
        assert seeds == {bench.config.SEED_BASE + 3}
        from app.core.enums import LLMProvider
        from app.services.llm_types import LLMConfig

        ollama = bench.service.build_service_class()(
            LLMConfig(provider=LLMProvider.OLLAMA, model="m", base_url="http://127.0.0.1:9"),
            arm=bench.arms.Arm("x", SCOPE, MERGE),
            seed=7,
        )
        assert "seed" not in ollama._prepare_payload([{"role": "user", "content": "hi"}])

    def test_token_usage_and_latency_are_recorded_per_call(self, bench: SimpleNamespace) -> None:
        ctx, entry = _context(bench, FakeTransport(), mode=None)
        record = bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))
        assert len(record["calls"]) == len(entry["chunks"]) + 1
        assert {(c["prompt_tokens"], c["completion_tokens"]) for c in record["calls"]} == {
            (100, 20)
        }
        assert all(
            c["latency_s"] is not None and len(c["system_sha256"]) == 64 for c in record["calls"]
        )


class TestFailureInjection:
    @pytest.mark.parametrize("mode", ["exception", "parse"])
    def test_the_merge_receives_the_failed_slice_as_an_error_placeholder(
        self, bench: SimpleNamespace, mode: str
    ) -> None:
        ctx, entry = _context(bench, FakeTransport(), mode=mode, failing=2)
        record = bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))
        assert record["status"] == "ok", record
        sections = record["slices"]
        assert len(sections) == len(entry["chunks"])
        assert sections[1].get("_error") is True
        assert not any(s.get("_error") for i, s in enumerate(sections) if i != 1)
        assert record["failing_slice"] == 2 and record["mode"] == mode

    def test_a_cell_where_the_failure_never_arrived_is_void_not_ok(
        self, bench: SimpleNamespace
    ) -> None:
        ctx, _ = _context(bench, FakeTransport(), mode="noop", failing=2)
        record = bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))
        assert (record["status"], record["void_reason"]) == ("void", "failure_not_injected")

    def test_the_chosen_unparseable_content_really_ends_in_an_error_not_a_repaired_dict(
        self, bench: SimpleNamespace
    ) -> None:
        json_repair = pytest.importorskip("json_repair")
        repaired = json_repair.loads(bench.service.UNPARSEABLE_CONTENT)
        assert not (isinstance(repaired, dict) and repaired)
        with pytest.raises(json.JSONDecodeError):
            json.loads(bench.service.UNPARSEABLE_CONTENT)


class TestFailedCells:
    def test_a_merge_the_app_swallowed_into_partial_results_is_an_error_cell(
        self, bench: SimpleNamespace
    ) -> None:
        ctx, _ = _context(bench, FakeTransport(fail_merge=True), mode=None)
        record = bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))
        assert (record["status"], record["error"]) == ("error", "MergeFailed")
        assert record["merged"] is None

    def test_a_slice_that_failed_for_real_makes_the_cell_an_error_not_a_quiet_ok(
        self, bench: SimpleNamespace
    ) -> None:
        ctx, _ = _context(bench, FakeTransport(fail_slices=True), mode=None)
        record = bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))
        assert (record["status"], record["error"]) == ("error", "SliceFailed")

    def test_the_deliberately_failed_slice_alone_is_not_an_error(
        self, bench: SimpleNamespace
    ) -> None:
        ctx, _ = _context(bench, FakeTransport(), mode="exception", failing=2)
        record = bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))
        assert (record["status"], record["error"]) == ("ok", None)

    def test_a_bug_in_the_harness_is_not_swallowed_as_a_model_failure(
        self, bench: SimpleNamespace
    ) -> None:
        ctx, _ = _context(bench, FakeTransport(), mode=None)
        ctx.prompt = "{no_such_placeholder}"  # str.format raises KeyError outside any LLM call
        with pytest.raises(KeyError):
            bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))

    def test_an_error_cell_scores_zero_and_is_counted_in_the_verdict_inputs(
        self, bench: SimpleNamespace
    ) -> None:
        from tests.eval.harness.summary_scoring import GoldRef

        ctx, _ = _context(bench, FakeTransport(fail_merge=True), mode=None)
        record = bench.runner.run_cell(ctx, bench.runner.CellId("IX", "x", 0))
        gold = [GoldRef("Bxx", "decisions", "g", "The remote will be yellow", 1)]
        score = bench.scoring.score_record(record, gold, {"tail_index": None})
        assert score.status == "error"
        assert score.carry.recall == 0.0 and score.empty_result


class TestHooksStillExist:
    EXPECTED = {
        "_summarize_section": ["self", "chunk", "section_num", "total_sections"],
        "_combine_sections": [
            "self",
            "sections",
            "speaker_data",
            "prompt_template",
            "total_sections",
        ],
        "chat_completion": ["self", "messages"],
        "_prepare_payload": ["self", "messages"],
        "_send_llm_request": ["self", "url", "payload", "headers", "timeout"],
    }

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_the_app_method_the_harness_overrides_keeps_its_signature(self, name: str) -> None:
        from app.services.llm_service import LLMService

        params = list(inspect.signature(getattr(LLMService, name)).parameters)
        expected = self.EXPECTED[name]
        assert params[: len(expected)] == expected

    def test_the_module_constants_are_still_looked_up_at_call_time(
        self, bench: SimpleNamespace
    ) -> None:
        import app.services.llm_summarization as module

        assert hasattr(module, "SECTION_SCOPE_RULES") and hasattr(module, "SECTION_MERGE_RULES")
        arm = bench.arms.Arm("x", SCOPE, MERGE)
        with bench.service.inject_arm(arm):
            assert module.SECTION_SCOPE_RULES == SCOPE and module.SECTION_MERGE_RULES == MERGE
        assert module.SECTION_SCOPE_RULES != SCOPE
