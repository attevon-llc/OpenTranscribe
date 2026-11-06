"""Arms, the CLI surface, and the arm-isolation rule: no arm text in any artifact.

The harness reads the rule strings at run time and records only a SHA-256 and a length. These
tests run the real pipeline on a sentinel arm and then search everything it wrote.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.eval.harness.probe_metrics import ProseLeakError
from tests.eval.harness.probe_metrics import assert_no_prose
from tests.eval.summary_bench_support import FakeTransport
from tests.eval.summary_bench_support import load_bench
from tests.eval.summary_bench_support import make_context as _context

SENTINEL = "ARM-TEXT-SENTINEL-4f1c"


@pytest.fixture(scope="module")
def bench() -> SimpleNamespace:
    return load_bench()


def _arm_file(tmp_path: Path, name: str = "new", **over: str) -> Path:
    payload = {
        "name": name,
        "scope_rules": f" scope {SENTINEL}.",
        "merge_rules": f" merge {SENTINEL}.",
    }
    payload.update(over)
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload))
    return path


class TestArmLoader:
    def test_loads_a_file_and_exposes_only_hashes_and_lengths(
        self, bench: SimpleNamespace, tmp_path: Path
    ) -> None:
        arm = bench.arms.load_arm(f"new={_arm_file(tmp_path)}")
        record = arm.as_record()
        assert record["name"] == "new"
        assert record["scope_len"] == len(f" scope {SENTINEL}.")
        assert len(record["scope_sha256"]) == len(record["merge_sha256"]) == 64
        assert SENTINEL not in json.dumps(record)
        assert SENTINEL not in repr(arm) and SENTINEL not in str(arm)

    def test_refuses_to_record_arm_text_even_when_asked_for_the_dict_form(
        self, bench: SimpleNamespace, tmp_path: Path
    ) -> None:
        arm = bench.arms.load_arm(f"new={_arm_file(tmp_path)}")
        assert not {"scope_rules", "merge_rules", "text"} & set(arm.as_record())

    @pytest.mark.parametrize("field", ["scope_rules", "merge_rules"])
    def test_a_rule_that_does_not_start_with_a_space_is_refused(
        self, bench: SimpleNamespace, tmp_path: Path, field: str
    ) -> None:
        path = _arm_file(tmp_path, **{field: "no leading space"})
        with pytest.raises(bench.config.HarnessError, match="start with a space"):
            bench.arms.load_arm(f"new={path}")

    @pytest.mark.parametrize("spec", ["new", "=x.json", "new=", "new=/no/such/file.json"])
    def test_a_malformed_or_unreadable_spec_is_a_misuse_error(
        self, bench: SimpleNamespace, spec: str
    ) -> None:
        with pytest.raises(bench.config.HarnessError) as refusal:
            bench.arms.load_arm(spec)
        assert refusal.value.code == bench.config.ExitCode.MISUSE

    def test_a_file_missing_a_rule_is_refused(self, bench: SimpleNamespace, tmp_path: Path) -> None:
        path = tmp_path / "half.json"
        path.write_text(json.dumps({"scope_rules": " only"}))
        with pytest.raises(bench.config.HarnessError, match="merge_rules"):
            bench.arms.load_arm(f"half={path}")

    def test_the_module_shorthand_reads_the_constants_without_exposing_them(
        self, bench: SimpleNamespace
    ) -> None:
        import app.services.llm_summarization as module

        arm = bench.arms.load_arm("old=@module")
        assert arm.scope_rules == module.SECTION_SCOPE_RULES
        record = arm.as_record()
        assert record["scope_len"] == len(module.SECTION_SCOPE_RULES)
        assert module.SECTION_MERGE_RULES not in json.dumps(record)
        assert module.SECTION_MERGE_RULES not in repr(arm)

    def test_the_aa_control_is_the_same_rules_under_another_label(
        self, bench: SimpleNamespace, tmp_path: Path
    ) -> None:
        arm = bench.arms.load_arm(f"old={_arm_file(tmp_path, 'old')}")
        twin = arm.renamed("old-aa")
        assert twin.name == "old-aa"
        assert {k: v for k, v in twin.as_record().items() if k != "name"} == {
            k: v for k, v in arm.as_record().items() if k != "name"
        }

    def test_the_shipped_sabotage_arm_is_valid_and_is_the_plans_text(
        self, bench: SimpleNamespace
    ) -> None:
        arm = bench.arms.sabotage_arm()
        assert arm.name == "sabotage"
        assert arm.scope_rules.startswith(" Treat this section as the entire recording.")
        assert (
            arm.merge_rules == " Use only the last section summary and ignore all earlier sections."
        )


class TestNoArmTextInArtifacts:
    def test_test_no_arm_text_in_artifacts(self, bench: SimpleNamespace, tmp_path: Path) -> None:
        """Run the pipeline on a sentinel arm, write everything, and search every file."""
        from tests.eval.harness.summary_scoring import GoldRef

        transport = FakeTransport()
        ctx, entry = _context(bench, transport, mode=None)
        ctx.arms["x"] = bench.arms.Arm("x", f" scope {SENTINEL}.", f" merge {SENTINEL}.")
        records_path = tmp_path / "records.jsonl"
        records = list(
            bench.runner.run_cells(
                ctx, [bench.runner.CellId("IX", "x", 0)], records_path, lambda r: None
            )
        )
        # The model was really sent the sentinel (so the absence below is not vacuous)...
        assert any(SENTINEL in system for _, system in transport.systems)
        gold = [GoldRef("Bxx", "decisions", "g1", "The remote will be yellow", 1)]
        scored = bench.scoring.score_record(records[0], gold, entry)
        rows = [scored.scores_row()]
        verdict = {
            "verdict": "PASS",
            "params": {"margin": 0.05, "arms": {"x": records[0]["arm_hashes"]}},
            "reasons": [],
        }
        metrics = bench.report.build_metrics(
            label="t",
            input_set="smoke",
            verdict=verdict,
            rows=rows,
            provenance={"model": "m", "arms": {"x": records[0]["arm_hashes"]}},
            judge=None,
            cuts=[],
        )
        bench.report.write_metrics(metrics, tmp_path)
        bench.planner.write_lock({"version": 1, "inputs": {"IX": entry}}, tmp_path)
        (tmp_path / "scores.jsonl").write_text(json.dumps(rows[0]))
        # ... and yet it appears in no file the harness wrote.
        written = sorted(p for p in tmp_path.iterdir() if p.is_file())
        assert {p.name for p in written} >= {
            "records.jsonl",
            "metrics.json",
            "metrics.md",
            "inputs.lock.json",
        }
        for path in written:
            assert SENTINEL not in path.read_text(), f"arm text leaked into {path.name}"

    def test_metrics_pass_the_prose_guard_and_a_leaked_summary_would_not(
        self, bench: SimpleNamespace
    ) -> None:
        clean = bench.report.build_metrics(
            label="t",
            input_set="gate",
            verdict={"verdict": "PASS", "params": {"margin": 0.05}, "reasons": []},
            rows=[],
            provenance={"model": "m"},
            judge=None,
            cuts=[],
        )
        assert_no_prose(clean)
        with pytest.raises(ProseLeakError):
            bench.report.build_metrics(
                label="t",
                input_set="gate",
                verdict={"verdict": "PASS", "params": {"margin": 0.05}, "reasons": []},
                rows=[],
                provenance={"answer": "a summary sentence"},
                judge=None,
                cuts=[],
            )


class TestCli:
    def _arms(self, tmp_path: Path) -> list[str]:
        return ["--arm", "old=@module", "--arm", f"new={_arm_file(tmp_path)}"]

    def test_the_gate_dry_run_lists_the_72_summaries_of_the_plan_in_blocks_by_run(
        self, bench: SimpleNamespace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = bench.cli.main(["run", "--inputs", "gate", "--dry-run", *self._arms(tmp_path)])
        lines = [ln.strip() for ln in capsys.readouterr().out.splitlines()]
        assert code == 0
        assert lines[0].startswith("72 cells")
        cells = [ln.split(". ", 1)[1] for ln in lines[1:]]
        assert len(cells) == len(set(cells)) == 72
        runs = [int(c.rsplit("|", 1)[1]) for c in cells]
        assert runs == sorted(runs)  # strictly blocks by run index k
        first_block = [c.split("|")[1] for c in cells if c.endswith("|0")]
        second_block = [c.split("|")[1] for c in cells if c.endswith("|1")]
        assert {"old", "new", "old-aa", "sabotage"} == set(first_block)
        assert (
            first_block[0] != second_block[0] or first_block != second_block
        )  # order is seeded per k

    def test_the_smoke_dry_run_without_controls_is_two_cells(
        self, bench: SimpleNamespace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bench.cli.main(
            [
                "run",
                "--inputs",
                "smoke",
                "--runs",
                "1",
                "--no-controls",
                "--dry-run",
                *self._arms(tmp_path),
            ]
        )
        assert capsys.readouterr().out.splitlines()[0].startswith("2 cells")

    def test_both_gate_arms_are_required(
        self, bench: SimpleNamespace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = bench.cli.main(["run", "--dry-run", "--arm", f"new={_arm_file(tmp_path)}"])
        assert code == bench.config.ExitCode.MISUSE
        assert "refused" in capsys.readouterr().err

    def test_an_existing_records_file_is_refused_without_resume(
        self, bench: SimpleNamespace
    ) -> None:
        done = {"I1|old|0": {"status": "ok"}}
        cells = [bench.runner.CellId("I1", "old", 0), bench.runner.CellId("I1", "old", 1)]
        with pytest.raises(bench.config.HarnessError):
            bench.runner.pending_cells(cells, done, resume=False, retry_errors=False)
        assert bench.runner.pending_cells(cells, done, resume=True, retry_errors=False) == cells[1:]

    def test_retry_errors_reruns_only_the_failed_cells(self, bench: SimpleNamespace) -> None:
        done = {"I1|old|0": {"status": "error"}, "I1|old|1": {"status": "ok"}}
        cells = [bench.runner.CellId("I1", "old", k) for k in range(3)]
        redo = bench.runner.pending_cells(cells, done, resume=False, retry_errors=True)
        assert [c.run for c in redo] == [0, 2]

    def test_the_projection_refuses_a_matrix_that_will_not_fit_the_budget(
        self, bench: SimpleNamespace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "runinfo.json").write_text(
            json.dumps({"sessions": [{"seconds_per_summary": 300.0}]})
        )
        over = bench.cli.main(["project", "--run-dir", str(tmp_path), "--budget-hours", "4"])
        assert over == bench.config.ExitCode.NOT_MEASURED
        (tmp_path / "runinfo.json").write_text(
            json.dumps({"sessions": [{"seconds_per_summary": 60.0}]})
        )
        assert bench.cli.main(["project", "--run-dir", str(tmp_path), "--budget-hours", "4"]) == 0
        assert "projected" in capsys.readouterr().out
