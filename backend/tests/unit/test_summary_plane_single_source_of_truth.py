"""Guard the #963 core claim: single source of truth, no duplicate writes.

Modelled *exactly* on ``test_transcript_summaries_index_retired.py``'s
detector/self-test/sweep triad. #67's defect was ``_persist_summary`` writing
the SAME dict to ``media_file.summary_data`` and to OpenSearch in one task.
#963's write path (``TranscriptIndexingService._index_summary_plane``) makes
that shape structurally impossible rather than merely discouraged: the method
has no parameter a summary payload could travel through, so a caller cannot
reproduce the #67 defect even by accident. This file asserts both the
structural guarantee (T-G2) and that no function anywhere near the summary
write path still exhibits the old SHAPE (T-G1), with a guard-the-guard test
(T-G3) proving the detector actually discriminates.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

_BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

#: The #67 shape, expressed structurally: a function that both assigns
#: ``<something>.summary_data`` AND calls one of the names that writes to
#: OpenSearch, in the SAME function body.
_OPENSEARCH_WRITE_CALLS = {
    "index",
    "bulk",
    "_bulk_index_documents",
    "_bulk_index_chunks",
    "_index_summary_plane",
    "build_summary_documents",
}


def _writes_a_second_summary_copy(source: str) -> set[str]:
    """Names of functions whose body sets ``.summary_data`` AND writes to
    OpenSearch in the same frame — #67's exact shape, expressed structurally.
    """
    tree = ast.parse(source)
    offenders: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        sets_summary_data = False
        calls_opensearch_write = False
        for child in ast.walk(node):
            if isinstance(child, ast.Assign | ast.AnnAssign):
                target = child.targets[0] if isinstance(child, ast.Assign) else child.target
                if isinstance(target, ast.Attribute) and target.attr == "summary_data":
                    sets_summary_data = True
            if isinstance(child, ast.Call):
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name in _OPENSEARCH_WRITE_CALLS:
                    calls_opensearch_write = True
        if sets_summary_data and calls_opensearch_write:
            offenders.add(node.name)
    return offenders


def test_the_dual_write_detector_discriminates() -> None:
    """T-G3, guard-the-guard: must-fire and must-stay-clean cases."""
    assert _writes_a_second_summary_copy(
        "def f(mf, svc):\n    mf.summary_data = d\n    svc._bulk_index_documents(docs, True)\n"
    ) == {"f"}
    assert _writes_a_second_summary_copy("def f(mf):\n    mf.summary_data = d\n") == set()
    assert (
        _writes_a_second_summary_copy(
            '"""We must never write summary_data and bulk index in one function."""\n'
        )
        == set()
    )
    assert (
        _writes_a_second_summary_copy(
            "x = 1  # mf.summary_data = d then _bulk_index_documents(...)\n"
        )
        == set()
    )


def test_no_function_writes_summary_data_and_indexes_it_in_one_frame() -> None:
    """T-G1: sweep the three modules #67's defect actually lived in."""
    files = [
        _BACKEND_APP / "tasks" / "summarization.py",
        _BACKEND_APP / "tasks" / "summary_retry.py",
        _BACKEND_APP / "api" / "endpoints" / "summarization.py",
    ]
    offenders: dict[str, set[str]] = {}
    for path in files:
        found = _writes_a_second_summary_copy(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path)] = found
    assert not offenders, (
        "these functions both write media_file.summary_data AND index to "
        f"OpenSearch in one frame — the #67 shape: {offenders}"
    )


def test_index_summary_plane_has_no_summary_data_parameter() -> None:
    """T-G2: the mechanical half of the guarantee.

    There is no argument shape through which a caller could hand
    ``_index_summary_plane`` a summary payload — it reads
    ``media_file.summary_data`` itself, on its own session. Assert the
    signature carries no such parameter, under any of the spellings a future
    edit might reach for.
    """
    from app.services.search.indexing_service import TranscriptIndexingService

    sig = inspect.signature(TranscriptIndexingService._index_summary_plane)
    forbidden = {"summary_data", "summary", "summary_payload", "summary_json"}
    assert not (set(sig.parameters) & forbidden), (
        f"_index_summary_plane must not accept a summary payload; found "
        f"parameters: {set(sig.parameters) & forbidden}"
    )


def test_index_file_summary_task_never_receives_a_summary_payload() -> None:
    """The Celery dispatch mirrors T-G2: it carries a file id, nothing more."""
    from app.tasks.search_indexing_task import index_file_summary

    sig = inspect.signature(index_file_summary.run)
    forbidden = {"summary_data", "summary", "summary_payload", "summary_json"}
    assert not (set(sig.parameters) & forbidden)


def test_every_summary_data_clear_dispatches_the_reindex_task() -> None:
    """Every write path that mutates ``summary_data`` must reach the plane.

    Not exhaustive proof of correctness — a structural smoke check that the
    Celery task import (``index_file_summary``) appears in the same module as
    every ``summary_data =`` write site named in the issue's dispatch table,
    so a future edit that drops one is at least visible in a diff of this
    test's expectations.
    """
    sites = {
        _BACKEND_APP / "tasks" / "summarization.py": True,
        _BACKEND_APP / "tasks" / "summary_retry.py": True,
        _BACKEND_APP / "utils" / "task_utils.py": True,
        _BACKEND_APP / "api" / "endpoints" / "files" / "reprocess.py": True,
        _BACKEND_APP / "api" / "endpoints" / "summarization.py": True,
    }
    for path in sites:
        source = path.read_text(encoding="utf-8")
        assert ".summary_data" in source, (
            f"{path} no longer touches summary_data — update this test"
        )
        assert "index_file_summary" in source, (
            f"{path} mutates summary_data but never dispatches index_file_summary — "
            "the OpenSearch summary plane will drift from Postgres"
        )
