"""``tests.eval.harness.probe_metrics`` — the licence guard is enforced by code (#72).

The live chat-RAG probe (``scripts/probe_chat_rag.py``) is the one instrument in this
repo that can observe an app-generated answer and, when its question set is built from
QMSum, a verbatim human reference answer. Both are prose this **public** repo must
never commit alongside a question set whose upstream README asks research-only use.

These tests exercise the metrics-only extraction with a canned record shaped like the
probe's own per-question JSON (see ``scripts/probe_chat_rag.py::result_to_record``) —
nothing here makes a network call, and none of the canned text below is QMSum's; it is
written for this test file.
"""

from __future__ import annotations

import pytest

from tests.eval.harness import probe_metrics
from tests.eval.harness.probe_metrics import ProbeTurnMetrics
from tests.eval.harness.probe_metrics import ProseLeakError
from tests.eval.harness.probe_metrics import assert_no_prose
from tests.eval.harness.probe_metrics import build_probe_results
from tests.eval.harness.probe_metrics import build_probe_rows
from tests.eval.harness.probe_metrics import coverage_ratio
from tests.eval.harness.probe_metrics import extract_turn_metrics
from tests.eval.harness.probe_metrics import render_probe_table
from tests.eval.harness.probe_metrics import summarize_probe_rows

pytestmark = pytest.mark.unit


def _record(**overrides: object) -> dict:
    """A minimal, well-formed raw probe record — the shape ``result_to_record``
    produces, trimmed to what ``extract_turn_metrics`` actually reads."""
    base = {
        "label": "multi-1-example",
        "category": "multi_file",
        "question": "irrelevant to metrics extraction",
        "reference_answer": "irrelevant to metrics extraction",
        "scope_requested": "four files",
        "scope_file_uuids": ["a", "b", "c", "d"],
        "expect_refusal": False,
        "conversation_uuid": "11111111-1111-1111-1111-111111111111",
        "app_answer": "irrelevant to metrics extraction",
        "reasoning_text": "",
        "latency_s": 12.3,
        "error": None,
        "warnings": [],
        "msg_metadata": {},
        "citations": [
            {"file_uuid": "a", "snippet": "irrelevant"},
            {"file_uuid": "b", "snippet": "irrelevant"},
            {"file_uuid": "a", "snippet": "irrelevant, duplicate file"},
        ],
        "files_consulted_uuids": ["a", "b"],
        "chunks_used": 12,
        "retrieved": 48,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# coverage_ratio
# ---------------------------------------------------------------------------


def test_coverage_ratio_basic_fraction() -> None:
    assert coverage_ratio(3, 4) == 0.75


def test_coverage_ratio_full_coverage() -> None:
    assert coverage_ratio(4, 4) == 1.0


def test_coverage_ratio_zero_scope_is_none_not_zero() -> None:
    """An unscoped/malformed request must not read as 'measured zero coverage'."""
    assert coverage_ratio(0, 0) is None


def test_coverage_ratio_capped_at_one() -> None:
    """More consulted files than the scope named cannot read as >100% coverage."""
    assert coverage_ratio(5, 4) == 1.0


# ---------------------------------------------------------------------------
# extract_turn_metrics — the archived 2026-08-20 run's four multi-file
# coverage ratios, reproduced from counts only (never from the archived
# question/reference text).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope_size", "files_consulted", "expected_ratio"),
    [
        (4, 3, 0.75),
        (4, 3, 0.75),
        (4, 2, 0.5),
        (4, 2, 0.5),
    ],
)
def test_multi_file_coverage_matches_2026_08_20_baseline(
    scope_size: int, files_consulted: int, expected_ratio: float
) -> None:
    record = _record(
        scope_file_uuids=[f"f{i}" for i in range(scope_size)],
        files_consulted_uuids=[f"f{i}" for i in range(files_consulted)],
    )
    metrics = extract_turn_metrics(record)
    assert metrics.coverage_ratio == expected_ratio


def test_extract_turn_metrics_deduplicates_files_consulted() -> None:
    """Two citations from the same file are one file consulted, not two."""
    metrics = extract_turn_metrics(_record())
    assert metrics.files_consulted == 2  # 'a' and 'b', 'a' cited twice


def test_extract_turn_metrics_errored_flag() -> None:
    ok = extract_turn_metrics(_record(error=None))
    failed = extract_turn_metrics(_record(error="RuntimeError: boom"))
    assert ok.errored is False
    assert failed.errored is True


def test_extract_turn_metrics_warning_codes_sorted_and_deduplicated() -> None:
    record = _record(
        warnings=[
            {"code": "context_dropped"},
            {"code": "budget_exceeded"},
            {"code": "context_dropped"},
        ]
    )
    metrics = extract_turn_metrics(record)
    assert metrics.warning_codes == ("budget_exceeded", "context_dropped")


def test_extract_turn_metrics_ignores_missing_warning_code() -> None:
    """A malformed warning frame with no 'code' key must not crash extraction."""
    record = _record(warnings=[{"message": "no code field"}])
    metrics = extract_turn_metrics(record)
    assert metrics.warning_codes == ()


def test_extract_turn_metrics_missing_label_raises() -> None:
    record = _record()
    del record["label"]
    with pytest.raises(KeyError):
        extract_turn_metrics(record)


def test_extract_turn_metrics_expect_refusal_and_null_metadata() -> None:
    """A negative control that never consulted a file: scope_size > 0, ratio is a
    real measured 0.0, not None — it IS answerable (scope was non-empty)."""
    record = _record(
        category="negative_control",
        expect_refusal=True,
        files_consulted_uuids=[],
        citations=[],
        chunks_used=12,
        retrieved=48,
    )
    metrics = extract_turn_metrics(record)
    assert metrics.expect_refusal is True
    assert metrics.files_consulted == 0
    assert metrics.coverage_ratio == 0.0


# ---------------------------------------------------------------------------
# build_probe_rows / summarize_probe_rows
# ---------------------------------------------------------------------------


def test_build_probe_rows_sorted_by_query_id() -> None:
    records = [
        _record(label="zzz-last", category="single_specific"),
        _record(label="aaa-first", category="single_specific"),
    ]
    rows = build_probe_rows(records)
    assert [row["query_id"] for row in rows] == ["aaa-first", "zzz-last"]


def test_summarize_probe_rows_groups_by_category_and_reports_min_mean() -> None:
    rows = build_probe_rows(
        [
            _record(
                label="m1",
                category="multi_file",
                scope_file_uuids=["a", "b", "c", "d"],
                files_consulted_uuids=["a", "b", "c"],
            ),
            _record(
                label="m2",
                category="multi_file",
                scope_file_uuids=["a", "b", "c", "d"],
                files_consulted_uuids=["a", "b"],
            ),
        ]
    )
    summary = summarize_probe_rows(rows)
    assert summary["multi_file"]["queries"] == 2
    assert summary["multi_file"]["errored"] == 0
    assert summary["multi_file"]["mean_coverage_ratio"] == pytest.approx(0.625)
    assert summary["multi_file"]["min_coverage_ratio"] == 0.5


def test_summarize_probe_rows_omits_coverage_stats_when_unmeasured() -> None:
    """A category whose rows all have scope_size 0 has nothing to average — the
    summary must not fabricate a 0.0 rather than omitting the key entirely."""
    rows = build_probe_rows([_record(label="x", scope_file_uuids=[], files_consulted_uuids=[])])
    summary = summarize_probe_rows(rows)
    assert "mean_coverage_ratio" not in summary["multi_file"]
    assert summary["multi_file"]["queries"] == 1


# ---------------------------------------------------------------------------
# assert_no_prose — the licence enforcement mechanism, must-fire + must-stay-clean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "poisoned",
    [
        {"reference_answer": "a verbatim QMSum human reference answer"},
        {"question": "what did the group decide"},
        {"app_answer": "the model's generated prose"},
        {"reasoning_text": "a thinking trace"},
        {"citations": [{"snippet": "transcript excerpt"}]},
        {"nested": {"deeply": {"reference_answer": "still forbidden"}}},
        {"rows": [{"ok": 1}, {"answer": "forbidden inside a list"}]},
    ],
)
def test_assert_no_prose_must_fire_on_forbidden_field(poisoned: dict) -> None:
    with pytest.raises(ProseLeakError):
        assert_no_prose(poisoned)


def test_assert_no_prose_clean_payload_does_not_raise() -> None:
    clean = {
        "query_id": "single-1",
        "category": "single_specific",
        "coverage_ratio": 1.0,
        "warning_codes": ["budget_exceeded"],
        "nested": {"chunks_used": 4, "retrieved": 48},
    }
    assert_no_prose(clean)  # must not raise


def test_assert_no_prose_clean_on_real_builder_output() -> None:
    """The actual artifact build_probe_results assembles must pass its own guard —
    this is the must-stay-clean case for the function this repo will actually call."""
    results = build_probe_results(
        run_name="test-run",
        target={"host": "localhost", "port": 5274},
        records=[_record()],
    )
    assert_no_prose(results)  # must not raise a second time


# ---------------------------------------------------------------------------
# build_probe_results / render_probe_table — the assembled artifact
# ---------------------------------------------------------------------------


def test_build_probe_results_never_contains_forbidden_keys_at_any_depth() -> None:
    results = build_probe_results(
        run_name="test-run",
        target={"host": "localhost", "port": 5274},
        records=[
            _record(label="a", category="single_specific"),
            _record(label="b", category="multi_file", error="TimeoutError: slow"),
        ],
        notes=["a note about the measurement, not the content"],
    )
    assert results["schema_version"] == 1
    assert results["run_name"] == "test-run"
    assert [row["query_id"] for row in results["rows"]] == ["a", "b"]
    assert "single_specific" in results["summary"]
    assert "multi_file" in results["summary"]

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in probe_metrics.FORBIDDEN_KEYS
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(results)


def test_build_probe_results_raises_on_note_that_smuggles_a_forbidden_field() -> None:
    """Notes are free text ABOUT the measurement — a dict passed where a string is
    expected must still be caught, since notes flow through the same guard."""
    with pytest.raises(ProseLeakError):
        build_probe_results(
            run_name="test-run",
            target={"host": "localhost", "port": 5274},
            records=[_record()],
            notes=[{"reference_answer": "smuggled via notes"}],  # type: ignore[list-item]
        )


def test_render_probe_table_contains_every_row_and_no_header_collision() -> None:
    rows = build_probe_rows([_record(label="x", category="single_specific")])
    table = render_probe_table(rows)
    assert "x" in table
    assert "single_specific" in table
    # The header names ARE the licence-safe fields; assert none of the forbidden
    # names sneak into the rendered header either.
    header_line = table.splitlines()[0]
    for forbidden in probe_metrics.FORBIDDEN_KEYS:
        assert forbidden not in header_line


def test_probe_turn_metrics_as_json_field_shape() -> None:
    metrics = ProbeTurnMetrics(
        query_id="q1",
        category="single_specific",
        scope_size=1,
        expect_refusal=False,
        errored=False,
        files_consulted=1,
        chunks_used=4,
        retrieved=48,
        coverage_ratio=1.0,
        warning_codes=("budget_exceeded",),
    )
    payload = metrics.as_json()
    assert payload["warning_codes"] == ["budget_exceeded"]  # tuple -> list, JSON-safe
    assert set(payload) == {
        "query_id",
        "category",
        "scope_size",
        "expect_refusal",
        "errored",
        "files_consulted",
        "chunks_used",
        "retrieved",
        "coverage_ratio",
        "warning_codes",
    }
