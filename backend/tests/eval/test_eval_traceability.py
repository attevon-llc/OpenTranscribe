"""``tests.eval.harness.traceability`` — the deterministic traceability half of task #11 /
GH #463 (representation / traceability / correctness). No LLM judge, no network, no live
stack: everything here is a pure function over a canned record shaped like the probe's own
per-question JSON (``scripts/probe_chat_rag.py::result_to_record``).

None of the canned text below is QMSum's or any other licensed dataset's — it is written for
this test file, exactly like ``test_eval_probe_metrics.py``.
"""

from __future__ import annotations

import pytest

from tests.eval.harness import probe_metrics
from tests.eval.harness.probe_metrics import ProseLeakError
from tests.eval.harness.traceability import TurnTraceability
from tests.eval.harness.traceability import build_traceability_results
from tests.eval.harness.traceability import build_traceability_rows
from tests.eval.harness.traceability import extract_turn_traceability
from tests.eval.harness.traceability import render_traceability_table
from tests.eval.harness.traceability import summarize_traceability_rows

pytestmark = pytest.mark.unit


def _record(**overrides: object) -> dict:
    """A well-formed raw probe record, scoring perfectly on every measure this module
    computes: no dangling markers, no scope leak, offered count matches chunks_used, and
    the one quoted claim is supported by its cited chunk's snippet.
    """
    base = {
        "label": "single-1-example",
        "category": "single_specific",
        "question": "irrelevant to traceability extraction",
        "reference_answer": "irrelevant to traceability extraction",
        "scope_file_uuids": ["file-a", "file-b"],
        "expect_refusal": False,
        "app_answer": 'The team agreed to "ship on friday"[1] and reviewed feedback [2].',
        "reasoning_text": "",
        "latency_s": 1.0,
        "error": None,
        "warnings": [],
        "msg_metadata": {"chunks_used": 2},
        "citations": [
            {"id": 1, "file_uuid": "file-a", "snippet": "we agreed to ship on friday morning"},
            {"id": 2, "file_uuid": "file-b", "snippet": "reviewed the feedback together"},
        ],
        "offered_citations": [
            {"id": 1, "file_uuid": "file-a"},
            {"id": 2, "file_uuid": "file-b"},
        ],
        "files_consulted_uuids": ["file-a", "file-b"],
        "chunks_used": 2,
        "retrieved": 12,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# citation_resolution — dangling [n] markers
# ---------------------------------------------------------------------------


def test_citation_resolution_must_fire_on_dangling_marker() -> None:
    """A [7] marker with only citations 1-5 rendered is dangling."""
    record = _record(
        app_answer="The team shipped on time [7].",
        citations=[{"id": i, "file_uuid": "file-a", "snippet": "x"} for i in range(1, 6)],
        offered_citations=[{"id": i, "file_uuid": "file-a"} for i in range(1, 6)],
        chunks_used=5,
    )
    metrics = extract_turn_traceability(record)
    assert metrics.markers_total == 1
    assert metrics.markers_dangling == 1
    assert metrics.citation_resolution_rate == 0.0


def test_citation_resolution_must_stay_clean_when_every_marker_resolves() -> None:
    metrics = extract_turn_traceability(_record())
    assert metrics.markers_total == 2
    assert metrics.markers_dangling == 0
    assert metrics.citation_resolution_rate == 1.0


def test_citation_resolution_rate_is_none_when_answer_cites_nothing() -> None:
    """No markers at all is 'nothing to resolve', not a measured perfect score."""
    record = _record(app_answer="No citation markers appear in this sentence.")
    metrics = extract_turn_traceability(record)
    assert metrics.markers_total == 0
    assert metrics.citation_resolution_rate is None


def test_citation_resolution_counts_repeated_markers() -> None:
    record = _record(app_answer="Ship date [1]. Confirmed again [1]. And feedback [2].")
    metrics = extract_turn_traceability(record)
    assert metrics.markers_total == 3
    assert metrics.markers_dangling == 0


# ---------------------------------------------------------------------------
# citation_validity — a citation naming a file outside the resolved scope
# ---------------------------------------------------------------------------


def test_citation_validity_must_fire_on_a_scope_leak() -> None:
    record = _record(
        scope_file_uuids=["file-a"],
        offered_citations=[
            {"id": 1, "file_uuid": "file-a"},
            {"id": 2, "file_uuid": "file-outside-scope"},
        ],
    )
    metrics = extract_turn_traceability(record)
    assert metrics.citations_total == 2
    assert metrics.citations_leaked == 1
    assert metrics.citation_validity_rate == 0.5


def test_citation_validity_must_stay_clean_when_every_citation_is_in_scope() -> None:
    metrics = extract_turn_traceability(_record())
    assert metrics.citations_total == 2
    assert metrics.citations_leaked == 0
    assert metrics.citation_validity_rate == 1.0


def test_citation_validity_leaked_is_none_when_scope_is_unresolved() -> None:
    """An empty scope cannot distinguish 'leaked' from 'all accessible' — must not
    read as a measured zero."""
    record = _record(scope_file_uuids=[])
    metrics = extract_turn_traceability(record)
    assert metrics.citations_leaked is None
    assert metrics.citation_validity_rate is None


def test_citation_validity_falls_back_to_rendered_citations_when_offered_absent() -> None:
    """A record captured before the probe carried offered_citations still gets a
    (narrower, documented) validity check over the persisted/used citations."""
    record = _record()
    del record["offered_citations"]
    record["citations"] = [
        {"id": 1, "file_uuid": "file-a", "snippet": "x"},
        {"id": 2, "file_uuid": "file-outside-scope", "snippet": "y"},
    ]
    record["app_answer"] = "Mentioned in [1] and also [2]."
    metrics = extract_turn_traceability(record)
    assert metrics.citations_total == 2
    assert metrics.citations_leaked == 1


# ---------------------------------------------------------------------------
# prompt_membership — chunks_used vs offered citation count (issue #384)
# ---------------------------------------------------------------------------


def test_prompt_membership_must_fire_on_mismatch() -> None:
    """A budget or SSE-capture regression: 3 excerpts reached the prompt but only
    2 were offered as citations."""
    record = _record(chunks_used=3, offered_citations=[{"id": 1, "file_uuid": "file-a"}])
    metrics = extract_turn_traceability(record)
    assert metrics.offered_citations_count == 1
    assert metrics.chunks_used == 3
    assert metrics.prompt_membership_matches is False


def test_prompt_membership_must_stay_clean_when_counts_match() -> None:
    metrics = extract_turn_traceability(_record())
    assert metrics.offered_citations_count == metrics.chunks_used == 2
    assert metrics.prompt_membership_matches is True


def test_prompt_membership_is_none_when_offered_citations_never_captured() -> None:
    record = _record()
    del record["offered_citations"]
    metrics = extract_turn_traceability(record)
    assert metrics.offered_citations_count is None
    assert metrics.prompt_membership_matches is None


def test_prompt_membership_is_none_when_chunks_used_missing() -> None:
    record = _record(msg_metadata={})
    del record["chunks_used"]
    metrics = extract_turn_traceability(record)
    assert metrics.chunks_used is None
    assert metrics.prompt_membership_matches is None


# ---------------------------------------------------------------------------
# quote_fidelity — a quoted span absent from its cited chunk
# ---------------------------------------------------------------------------


def test_quote_fidelity_must_fire_on_an_unsupported_quote() -> None:
    record = _record(
        app_answer='The lead engineer said "we should cancel the launch"[1].',
        citations=[{"id": 1, "file_uuid": "file-a", "snippet": "totally unrelated content"}],
        offered_citations=[{"id": 1, "file_uuid": "file-a"}],
        chunks_used=1,
    )
    metrics = extract_turn_traceability(record)
    assert metrics.quotes_total == 1
    assert metrics.quotes_unsupported == 1
    assert metrics.quote_fidelity == 0.0


def test_quote_fidelity_must_stay_clean_when_the_quote_is_supported() -> None:
    metrics = extract_turn_traceability(_record())
    assert metrics.quotes_total == 1
    assert metrics.quotes_unsupported == 0
    assert metrics.quote_fidelity == 1.0


def test_quote_fidelity_is_none_when_the_answer_makes_no_quoted_claims() -> None:
    record = _record(app_answer="The team discussed feedback [2] without quoting anyone.")
    metrics = extract_turn_traceability(record)
    assert metrics.quotes_total == 0
    assert metrics.quote_fidelity is None


def test_quote_fidelity_normalises_whitespace_and_case() -> None:
    record = _record(
        app_answer='They said "Ship   ON Friday"[1].',
        citations=[{"id": 1, "file_uuid": "file-a", "snippet": "we plan to ship on friday"}],
        offered_citations=[{"id": 1, "file_uuid": "file-a"}],
        chunks_used=1,
    )
    metrics = extract_turn_traceability(record)
    assert metrics.quotes_unsupported == 0


def test_quote_fidelity_ignores_a_quote_pointed_at_a_dangling_marker() -> None:
    """A quote cited against a marker with no rendered citation has no snippet to
    check against, so it counts as unsupported rather than being silently skipped."""
    record = _record(
        app_answer='They said "this never happened"[9].',
        citations=[{"id": 1, "file_uuid": "file-a", "snippet": "unrelated"}],
        offered_citations=[{"id": 1, "file_uuid": "file-a"}],
        chunks_used=1,
    )
    metrics = extract_turn_traceability(record)
    assert metrics.quotes_total == 1
    assert metrics.quotes_unsupported == 1


# ---------------------------------------------------------------------------
# extract_turn_traceability — required fields
# ---------------------------------------------------------------------------


def test_extract_turn_traceability_missing_label_raises() -> None:
    record = _record()
    del record["label"]
    with pytest.raises(KeyError):
        extract_turn_traceability(record)


# ---------------------------------------------------------------------------
# build_traceability_rows / summarize_traceability_rows
# ---------------------------------------------------------------------------


def test_build_traceability_rows_sorted_by_query_id() -> None:
    records = [_record(label="zzz-last"), _record(label="aaa-first")]
    rows = build_traceability_rows(records)
    assert [row["query_id"] for row in rows] == ["aaa-first", "zzz-last"]


def test_summarize_traceability_rows_reports_mean_and_min_over_measured_subset() -> None:
    """A corpus mean must not hide the one turn that leaked scope — min is reported
    alongside mean for every ratio field."""
    rows = build_traceability_rows(
        [
            _record(label="perfect"),  # default scope covers both offered citations
            _record(
                label="leaked",
                scope_file_uuids=["file-a"],
                offered_citations=[
                    {"id": 1, "file_uuid": "file-a"},
                    {"id": 2, "file_uuid": "file-outside-scope"},
                ],
            ),
        ]
    )
    summary = summarize_traceability_rows(rows)
    entry = summary["single_specific"]["citation_validity_rate"]
    assert entry["coverage"] == 2
    assert entry["mean"] == pytest.approx(0.75)
    assert entry["min"] == 0.5


def test_summarize_traceability_rows_omits_stats_when_nothing_measured() -> None:
    """A category whose rows all have empty scope has nothing to average for
    citation_validity_rate — must report coverage 0, never a fabricated value."""
    rows = build_traceability_rows([_record(label="x", scope_file_uuids=[])])
    summary = summarize_traceability_rows(rows)
    entry = summary["single_specific"]["citation_validity_rate"]
    assert entry["coverage"] == 0
    assert "mean" not in entry
    assert "min" not in entry


def test_summarize_traceability_rows_reports_prompt_membership_rate() -> None:
    rows = build_traceability_rows(
        [
            _record(label="match"),
            _record(label="mismatch", offered_citations=[{"id": 1, "file_uuid": "file-a"}]),
        ]
    )
    summary = summarize_traceability_rows(rows)
    entry = summary["single_specific"]["prompt_membership_matches"]
    assert entry["coverage"] == 2
    assert entry["rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# assert_no_prose — reused guard, must-fire + must-stay-clean over THIS module's
# artifact shape
# ---------------------------------------------------------------------------


def test_build_traceability_results_clean_on_real_output() -> None:
    results = build_traceability_results(
        run_name="test-run",
        target={"host": "localhost", "port": 5274},
        records=[_record(label="a"), _record(label="b")],
    )
    assert results["schema_version"] == 1
    assert "single_specific" in results["summary"]
    assert "Traceability, not correctness" in results["scope_note"]


def test_build_traceability_results_raises_on_a_note_that_smuggles_prose() -> None:
    with pytest.raises(ProseLeakError):
        build_traceability_results(
            run_name="test-run",
            target={"host": "localhost", "port": 5274},
            records=[_record()],
            notes=[{"app_answer": "smuggled via notes"}],  # type: ignore[list-item]
        )


def test_build_traceability_results_never_contains_forbidden_keys_at_any_depth() -> None:
    """A walk that visits nothing would pass having checked nothing — the exact
    failure mode this guard exists to catch, just relocated into the guard itself.
    So the walk counts every key it actually checks and that count is asserted
    non-zero BEFORE the per-key assertions are trusted, not left implicit in the
    loop running at all."""
    results = build_traceability_results(
        run_name="test-run",
        target={"host": "localhost", "port": 5274},
        records=[_record(label="a"), _record(label="b", error="TimeoutError: slow")],
    )

    keys_checked = 0

    def _walk(node: object) -> None:
        nonlocal keys_checked
        if isinstance(node, dict):
            for key, value in node.items():
                keys_checked += 1
                assert key not in probe_metrics.FORBIDDEN_KEYS
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(results)
    # The real artifact nests rows + a per-category summary inside the top-level
    # dict, so a genuine walk checks dozens of keys, not merely a nonzero one.
    assert keys_checked > 20, "the walk found almost nothing to check — verifies nothing"


# ---------------------------------------------------------------------------
# render_traceability_table
# ---------------------------------------------------------------------------


def test_render_traceability_table_contains_every_row() -> None:
    rows = build_traceability_rows([_record(label="x")])
    table = render_traceability_table(rows)
    assert "x" in table
    assert "single_specific" in table
    header_line = table.splitlines()[0]
    for forbidden in ("snippet", "answer", "question", "reference_answer"):
        assert forbidden not in header_line


def test_render_traceability_table_renders_none_as_not_applicable() -> None:
    rows = build_traceability_rows([_record(label="x", app_answer="no markers here at all")])
    table = render_traceability_table(rows)
    assert "n/a" in table


# ---------------------------------------------------------------------------
# TurnTraceability.as_json — field shape
# ---------------------------------------------------------------------------


def test_turn_traceability_as_json_field_shape() -> None:
    metrics = TurnTraceability(
        query_id="q1",
        category="single_specific",
        markers_total=2,
        markers_dangling=0,
        citation_resolution_rate=1.0,
        citations_total=2,
        citations_leaked=0,
        citation_validity_rate=1.0,
        chunks_used=2,
        offered_citations_count=2,
        prompt_membership_matches=True,
        quotes_total=1,
        quotes_unsupported=0,
        quote_fidelity=1.0,
    )
    payload = metrics.as_json()
    assert set(payload) == {
        "query_id",
        "category",
        "markers_total",
        "markers_dangling",
        "citation_resolution_rate",
        "citations_total",
        "citations_leaked",
        "citation_validity_rate",
        "chunks_used",
        "offered_citations_count",
        "prompt_membership_matches",
        "quotes_total",
        "quotes_unsupported",
        "quote_fidelity",
    }
