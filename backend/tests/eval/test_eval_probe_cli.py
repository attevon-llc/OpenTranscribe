"""``scripts/probe_chat_rag.py`` — the pure (non-network) half of the live chat probe.

Loaded via ``importlib`` the same way ``test_eval_fusion_arm.py`` loads the CLI half
of ``scripts/benchmark_rag.py`` — it lives outside the ``tests.eval`` package tree.
Only functions that touch neither the network nor a live stack are exercised here:
argument parsing, question-set loading/validation, scope parsing, and the raw-record
assembly. Everything that calls ``requests`` (``login``, ``create_conversation``,
``send_message_and_collect``, ...) is exactly what the task brief says not to run —
this file never imports ``requests`` itself and never invokes ``main()``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _probe_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "probe_chat_rag.py"
    spec = importlib.util.spec_from_file_location("probe_chat_rag_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _probe_module()


# ---------------------------------------------------------------------------
# parse_scope
# ---------------------------------------------------------------------------


def test_parse_scope_splits_and_strips() -> None:
    assert probe.parse_scope(" a, b ,c") == ["a", "b", "c"]


def test_parse_scope_drops_blanks() -> None:
    assert probe.parse_scope("a,,b,") == ["a", "b"]


def test_parse_scope_empty_string_is_empty_list() -> None:
    assert probe.parse_scope("") == []


# ---------------------------------------------------------------------------
# load_question_set
# ---------------------------------------------------------------------------


def _write_question_set(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_question_set_valid_file(tmp_path: Path) -> None:
    path = _write_question_set(
        tmp_path,
        [
            {
                "label": "q1",
                "category": "single_specific",
                "question": "example question, not from any dataset",
                "file_uuids": ["a"],
                "expect_refusal": False,
            },
            {
                "label": "q2",
                "category": "negative_control",
                "question": "another example question",
                "file_uuids": ["a", "b"],
                "scope_desc": "two files",
                "reference": "an example reference, not from any dataset",
                "expect_refusal": True,
            },
        ],
    )
    questions = probe.load_question_set(path)
    assert [q.label for q in questions] == ["q1", "q2"]
    assert questions[0].file_uuids == ["a"]
    assert questions[0].reference is None
    assert questions[1].expect_refusal is True
    assert questions[1].reference == "an example reference, not from any dataset"


def test_load_question_set_missing_required_key_exits(tmp_path: Path) -> None:
    path = _write_question_set(
        tmp_path, [{"label": "q1", "category": "x"}]
    )  # no question/file_uuids
    with pytest.raises(SystemExit):
        probe.load_question_set(path)


def test_load_question_set_not_a_list_exits(tmp_path: Path) -> None:
    path = _write_question_set(tmp_path, {"label": "q1"})
    with pytest.raises(SystemExit):
        probe.load_question_set(path)


def test_load_question_set_invalid_json_exits(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        probe.load_question_set(path)


def test_load_question_set_missing_file_exits(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        probe.load_question_set(tmp_path / "does-not-exist.json")


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_requires_question_or_question_set() -> None:
    parser = probe.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--host", "localhost"])


def test_build_parser_question_set_and_question_are_mutually_exclusive() -> None:
    parser = probe.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--question-set", "x.json", "--question", "text", "--scope", "a"])


def test_build_parser_defaults() -> None:
    parser = probe.build_parser()
    args = parser.parse_args(["--question-set", "x.json"])
    assert args.host == "localhost"
    assert args.port == 5174
    assert args.email == probe.DEFAULT_EMAIL
    assert args.out == "/tmp/ot-probe"  # noqa: S108 — asserting the CLI default, not writing here
    assert args.metrics_out is None
    assert args.skip_llm_config is False


def test_build_parser_port_is_overridable_for_a_fresh_stack() -> None:
    """The tool must be able to target a --fresh stack's offset port, not only
    the dev default — this is the '--port' half of the brief's requirement."""
    parser = probe.build_parser()
    args = parser.parse_args(["--question-set", "x.json", "--port", "5274"])
    assert args.port == 5274


# ---------------------------------------------------------------------------
# _resolve_questions
# ---------------------------------------------------------------------------


def test_resolve_questions_from_question_set(tmp_path: Path) -> None:
    path = _write_question_set(
        tmp_path,
        [{"label": "q1", "category": "c", "question": "text", "file_uuids": ["a"]}],
    )
    parser = probe.build_parser()
    args = parser.parse_args(["--question-set", str(path)])
    questions = probe._resolve_questions(args)
    assert len(questions) == 1
    assert questions[0].label == "q1"


def test_resolve_questions_ad_hoc_requires_scope() -> None:
    parser = probe.build_parser()
    args = parser.parse_args(["--question", "text"])
    with pytest.raises(SystemExit):
        probe._resolve_questions(args)


def test_resolve_questions_ad_hoc_builds_one_question() -> None:
    parser = probe.build_parser()
    args = parser.parse_args(
        ["--question", "text", "--scope", "a,b", "--label", "my-q", "--category", "smoke"]
    )
    questions = probe._resolve_questions(args)
    assert len(questions) == 1
    assert questions[0] == probe.Question(
        label="my-q", category="smoke", question="text", file_uuids=["a", "b"]
    )


# ---------------------------------------------------------------------------
# result_to_record — the raw (full-fidelity) shape metrics extraction consumes
# ---------------------------------------------------------------------------


def test_result_to_record_shape_matches_probe_metrics_expectations() -> None:
    question = probe.Question(
        label="q1",
        category="single_specific",
        question="text",
        file_uuids=["a", "b"],
        scope_desc="two files",
        reference="ref",
        expect_refusal=False,
    )
    result = probe.Result(
        q=question,
        conversation_uuid="uuid-1",
        answer_text="answer",
        latency_s=1.234,
        files_consulted_uuids=["a"],
        chunks_used=4,
        retrieved=10,
        offered_citations=[{"id": 1, "file_uuid": "a"}],
    )
    record = probe.result_to_record(result)
    # Every field tests.eval.harness.probe_metrics.extract_turn_metrics reads.
    for required in (
        "label",
        "category",
        "scope_file_uuids",
        "expect_refusal",
        "error",
        "warnings",
        "chunks_used",
        "retrieved",
        "files_consulted_uuids",
    ):
        assert required in record
    assert record["label"] == "q1"
    assert record["scope_file_uuids"] == ["a", "b"]
    assert record["latency_s"] == 1.23  # rounded
    # tests.eval.harness.traceability's extra field, additive to the shape above.
    assert record["offered_citations"] == [{"id": 1, "file_uuid": "a"}]


def test_result_offered_citations_defaults_to_empty_list() -> None:
    """A Result built without the kwarg (e.g. an errored turn) must not carry a
    mutable-default landmine shared across instances."""
    question = probe.Question(label="q", category="c", question="t", file_uuids=["a"])
    first = probe.Result(q=question)
    second = probe.Result(q=question)
    first.offered_citations.append({"id": 1, "file_uuid": "a"})
    assert second.offered_citations == []


# ---------------------------------------------------------------------------
# _offered_citation_refs — strips a 'sources' frame's citations to id/file_uuid
# ---------------------------------------------------------------------------


def test_offered_citation_refs_keeps_only_id_and_file_uuid() -> None:
    refs = probe._offered_citation_refs(
        [
            {
                "id": 3,
                "file_uuid": "abc",
                "snippet": "a transcript excerpt that must not survive",
                "title": "Recording title",
            }
        ]
    )
    assert refs == [{"id": 3, "file_uuid": "abc"}]
    assert "snippet" not in refs[0]
    assert "title" not in refs[0]


def test_offered_citation_refs_skips_malformed_entries() -> None:
    """A missing id/file_uuid is dropped, not raised — one odd frame from a live
    server must not abort the whole probe run."""
    refs = probe._offered_citation_refs(
        [
            {"id": 1, "file_uuid": "a"},
            {"id": 2},  # no file_uuid
            {"file_uuid": "b"},  # no id
            "not-a-dict",  # type: ignore[list-item]
        ]
    )
    assert refs == [{"id": 1, "file_uuid": "a"}]


def test_offered_citation_refs_empty_input_is_empty_output() -> None:
    assert probe._offered_citation_refs([]) == []
