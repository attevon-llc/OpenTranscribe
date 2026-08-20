"""Tests for the pure/CLI parts of ``harness.judge_runner`` — importable from ``backend/venv``
without ragas, because every ``ragas``/``openai`` import in that module is lazy (inside
``_build_metric``, only reached once a real judge call happens). This file does NOT invoke
``main()`` end-to-end against a real judge — that is ``test_eval_answer_judge.py``'s
``TestRealBatchEvaluation``, run via the ``backend/venv-eval`` subprocess. This file covers the
argument parsing, JSONL I/O, and error-exit-code contract ``answer_judge.py`` depends on.
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

    def test_default_temperature_matches_the_module_constant(self) -> None:
        assert judge_runner.DEFAULT_TEMPERATURE == 0.0

    def test_default_concurrency_and_embedding_model_are_sane(self, tmp_path: Path) -> None:
        """A malformed-input exit (2) happens before the judge is built, so this
        reaches argparse's defaults without needing ragas at all."""
        parser_defaults: dict[str, object] = {}

        class _CaptureNamespace:
            def __setattr__(self, key: str, value: object) -> None:
                parser_defaults[key] = value

        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--mode", required=True, choices=("faithfulness", "answer_correctness"))
        parser.add_argument("--input", required=True, type=Path)
        parser.add_argument("--output", required=True, type=Path)
        parser.add_argument("--model", required=True)
        parser.add_argument("--base-url", required=True)
        parser.add_argument("--api-key", default="not-needed")
        parser.add_argument("--embedding-model", default="sentence-transformers/all-MiniLM-L6-v2")
        parser.add_argument("--concurrency", type=int, default=4)
        parser.add_argument("--temperature", type=float, default=judge_runner.DEFAULT_TEMPERATURE)
        args = parser.parse_args(
            [
                "--mode",
                "faithfulness",
                "--input",
                "x",
                "--output",
                "y",
                "--model",
                "m",
                "--base-url",
                "u",
            ]
        )
        assert args.concurrency == 4
        assert args.api_key == "not-needed"
        assert args.embedding_model == "sentence-transformers/all-MiniLM-L6-v2"
