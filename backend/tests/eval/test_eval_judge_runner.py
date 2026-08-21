"""Tests for the pure/CLI parts of ``harness.judge_runner`` — importable from ``backend/venv``
without ragas, because every ``ragas``/``openai`` import in that module is lazy (inside
``_build_metric``, only reached once a real judge call happens). This file does NOT invoke
``main()`` end-to-end against a real judge — that is ``test_eval_faithfulness_judge.py``'s
real-execution tier, run via the ``backend/venv-eval`` subprocess. This file covers the
argument parsing, JSONL I/O, and error-exit-code contract ``faithfulness_judge.py`` depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.eval.harness import judge_runner


class TestReadRecords:
    def test_reads_one_record_per_line(self, tmp_path: Path) -> None:
        path = tmp_path / "records.jsonl"
        path.write_text(
            '{"query_id": "q1", "question": "a"}\n{"query_id": "q2", "question": "b"}\n'
        )
        records = judge_runner._read_records(path)
        assert [r["query_id"] for r in records] == ["q1", "q2"]

    def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "records.jsonl"
        path.write_text('{"query_id": "q1"}\n\n   \n{"query_id": "q2"}\n')
        records = judge_runner._read_records(path)
        assert len(records) == 2

    def test_empty_file_reads_as_empty_list(self, tmp_path: Path) -> None:
        path = tmp_path / "records.jsonl"
        path.write_text("")
        assert judge_runner._read_records(path) == []


class TestWriteResults:
    def test_writes_one_json_object_per_line(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        judge_runner._write_results(
            path, [{"query_id": "q1", "score": 0.5}, {"query_id": "q2", "score": None}]
        )
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"query_id": "q1", "score": 0.5}
        assert json.loads(lines[1]) == {"query_id": "q2", "score": None}

    def test_round_trips_through_read_records(self, tmp_path: Path) -> None:
        path = tmp_path / "roundtrip.jsonl"
        rows = [{"query_id": "q1", "score": 0.75}]
        judge_runner._write_results(path, rows)
        assert judge_runner._read_records(path) == rows


class TestMainArgumentValidation:
    def test_missing_input_file_exits_2(self, tmp_path: Path, capsys) -> None:
        exit_code = judge_runner.main(
            [
                "--mode",
                "faithfulness",
                "--input",
                str(tmp_path / "does-not-exist.jsonl"),
                "--output",
                str(tmp_path / "out.jsonl"),
                "--model",
                "m",
                "--base-url",
                "http://x",
            ]
        )
        assert exit_code == 2
        assert "failed to read" in capsys.readouterr().err

    def test_empty_input_file_exits_2(self, tmp_path: Path, capsys) -> None:
        input_path = tmp_path / "empty.jsonl"
        input_path.write_text("")
        exit_code = judge_runner.main(
            [
                "--mode",
                "faithfulness",
                "--input",
                str(input_path),
                "--output",
                str(tmp_path / "out.jsonl"),
                "--model",
                "m",
                "--base-url",
                "http://x",
            ]
        )
        assert exit_code == 2
        assert "no records" in capsys.readouterr().err

    def test_invalid_mode_is_rejected_by_argparse(self, tmp_path: Path) -> None:
        try:
            judge_runner.main(
                [
                    "--mode",
                    "not-a-real-mode",
                    "--input",
                    str(tmp_path / "x.jsonl"),
                    "--output",
                    str(tmp_path / "out.jsonl"),
                    "--model",
                    "m",
                    "--base-url",
                    "http://x",
                ]
            )
            raised = False
        except SystemExit as exc:
            raised = True
            assert exc.code == 2
        assert raised, "argparse should reject an unknown --mode with SystemExit(2)"

    def test_answer_correctness_mode_is_gone(self, tmp_path: Path) -> None:
        """The reference-based axis moved to the in-venv label judge
        (``harness/answer_judge.py``); this runner scores faithfulness only. A
        resurrected ``answer_correctness`` mode would mean two paths for one axis
        again — the exact pattern the removal commit deleted."""
        try:
            judge_runner.main(
                [
                    "--mode",
                    "answer_correctness",
                    "--input",
                    str(tmp_path / "x.jsonl"),
                    "--output",
                    str(tmp_path / "out.jsonl"),
                    "--model",
                    "m",
                    "--base-url",
                    "http://x",
                ]
            )
            raised = False
        except SystemExit as exc:
            raised = True
            assert exc.code == 2
        assert raised, "argparse should reject --mode answer_correctness with SystemExit(2)"

    def test_default_temperature_matches_the_module_constant(self) -> None:
        assert judge_runner.DEFAULT_TEMPERATURE == 0.0
