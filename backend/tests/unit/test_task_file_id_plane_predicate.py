"""Every ``update_by_query`` against the chunks index under ``app/tasks/`` that
filters on the literal ``file_id`` term must also carry a plane predicate (lane C0
task 6, #T10).

``file_id`` is the ambiguous field: ``MediaFile.id`` and ``Document.id`` are
independent integer sequences (both start at 1 and grow forever), so a bare
``{"term": {"file_id": file_id}}`` — with nothing distinguishing which entity
``file_id`` names — can match a document's chunks when sharing or tagging a media
file whose id happens to collide with that document's, and vice versa. This is
exactly what ``update_file_access_index`` / ``update_file_tags_index`` did before
this lane's fix (``app/tasks/search_indexing_task.py``); the reachable, real
consequence is a permission leak, not a relevance bug.

AST-based, not a grep — a grep for the string ``file_id`` would also match every
comment and docstring in these files, and a grep for the ABSENCE of ``doc_type``
would false-positive on anything that merely mentions it in prose without using it as
a real predicate. Self-tested against two synthetic samples (below) so the detector
is proven to fire on a deliberately-broken shape before it is trusted against the
real tree — the repo's own rule for every AST-based gate here
(``tests/unit/test_chunk_plane_compat_arm.py``, ``scripts/audit-tests.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

TASKS_DIR = Path(__file__).resolve().parents[2] / "app" / "tasks"

#: Function/attribute names that, if referenced anywhere inside the enclosing
#: function, count as "this reader decided about the plane". Mirrors
#: ``test_chunk_plane_compat_arm.py``'s ``_DECIDED`` plus the two markers this lane
#: added in ``search_indexing_task.py``.
_PLANE_MARKERS = (
    "chunk_plane_clause",
    "chunk_plane_query",
    "digest_plane_query",
    "file_plane_query",
    "document_chunk_plane_clause",
    "document_chunk_plane_query",
    "_document_plane_clause",
    "_document_plane_exclusion_clause",
)


def _is_file_id_term(node: ast.AST) -> bool:
    """``{"term": {"file_id": ...}}`` (or the ``file_id`` key of any dict), matched
    structurally — a dict literal whose keys include the string constant
    ``"file_id"`` — not by searching the source text for the substring, which would
    also match ``"media_file_id"`` or a docstring mentioning the field.
    """
    return isinstance(node, ast.Dict) and any(
        isinstance(key, ast.Constant) and key.value == "file_id" for key in node.keys
    )


def _update_by_query_calls_on_file_id(
    tree: ast.AST,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.Call]]:
    """Every ``(enclosing function, call)`` pair for an ``update_by_query`` call
    whose own argument tree contains a ``file_id`` term.

    The enclosing function is deliberately ``FunctionDef | AsyncFunctionDef``, not
    just ``FunctionDef`` — the ``isinstance`` check below already walks async
    defs too (an ``update_by_query`` call could equally sit inside one), and the
    return type must say so honestly rather than silently narrowing what callers
    believe they can receive.
    """
    results: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.Call]] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update_by_query"
            ):
                continue
            if any(_is_file_id_term(n) for n in ast.walk(node)):
                results.append((func, node))
    return results


def _function_is_plane_decided(func: ast.FunctionDef | ast.AsyncFunctionDef, source: str) -> bool:
    segment = ast.get_source_segment(source, func) or ""
    if any(marker in segment for marker in _PLANE_MARKERS):
        return True
    # A raw doc_type predicate (e.g. `{"term": {"doc_type": ...}}` or a `must_not`
    # exclusion built by hand) also counts — the marker list above covers the named
    # helpers, this covers a reader that builds the clause inline.
    return '"doc_type"' in segment or "'doc_type'" in segment


def _undecided_file_id_readers(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "update_by_query" not in source or "file_id" not in source:
            continue
        tree = ast.parse(source)
        for func, _call in _update_by_query_calls_on_file_id(tree):
            if not _function_is_plane_decided(func, source):
                findings.append(f"{path.name}::{func.name}")
    return sorted(set(findings))


# ---------------------------------------------------------------------------
# Self-test: prove the detector actually fires, on synthetic source (not the real
# tree) — a detector that matches nothing reports zero findings, which is
# indistinguishable from a clean codebase.
# ---------------------------------------------------------------------------

_BROKEN_SAMPLE = """
def update_something(client, index_name, file_id, ids):
    response = client.update_by_query(
        index=index_name,
        body={
            "query": {"term": {"file_id": file_id}},
            "script": {"source": "ctx._source.x = params.y", "params": {"y": ids}},
        },
    )
    return response
"""

_CLEAN_SAMPLE_NAMED_HELPER = """
def update_something(client, index_name, file_id, ids):
    plane_filter = _document_plane_exclusion_clause()
    response = client.update_by_query(
        index=index_name,
        body={
            "query": {"bool": {"filter": [{"term": {"file_id": file_id}}, plane_filter]}},
            "script": {"source": "ctx._source.x = params.y", "params": {"y": ids}},
        },
    )
    return response
"""

_CLEAN_SAMPLE_INLINE_DOC_TYPE = """
def update_something(client, index_name, file_id, ids):
    response = client.update_by_query(
        index=index_name,
        body={
            "query": {
                "bool": {
                    "filter": [{"term": {"file_id": file_id}}],
                    "must_not": [{"term": {"doc_type": "document_chunk"}}],
                }
            },
            "script": {"source": "ctx._source.x = params.y", "params": {"y": ids}},
        },
    )
    return response
"""

_CLEAN_SAMPLE_NOT_FILE_ID = """
def update_something(client, index_name, media_file_id, ids):
    response = client.update_by_query(
        index=index_name,
        body={
            "query": {"term": {"media_file_id": media_file_id}},
            "script": {"source": "ctx._source.x = params.y", "params": {"y": ids}},
        },
    )
    return response
"""


def _findings_in_source(source: str) -> list[str]:
    tree = ast.parse(source)
    findings = []
    for func, _call in _update_by_query_calls_on_file_id(tree):
        if not _function_is_plane_decided(func, source):
            findings.append(func.name)
    return findings


def test_the_detector_fires_on_a_bare_file_id_term_with_no_plane_predicate():
    assert _findings_in_source(_BROKEN_SAMPLE) == ["update_something"], (
        "the detector did not flag the deliberately-broken sample — it cannot be "
        "trusted against the real tree if it never fires here"
    )


def test_the_detector_stays_clean_for_a_named_plane_helper():
    assert _findings_in_source(_CLEAN_SAMPLE_NAMED_HELPER) == []


def test_the_detector_stays_clean_for_an_inline_doc_type_exclusion():
    assert _findings_in_source(_CLEAN_SAMPLE_INLINE_DOC_TYPE) == []


def test_the_detector_does_not_flag_a_query_that_never_mentions_file_id():
    """``media_file_id`` (the speaker-plane field) must not be mistaken for
    ``file_id`` by a loose substring check — this is why the match is structural."""
    assert _findings_in_source(_CLEAN_SAMPLE_NOT_FILE_ID) == []


def test_every_update_by_query_on_file_id_under_app_tasks_carries_a_plane_predicate():
    findings = _undecided_file_id_readers(TASKS_DIR)
    assert not findings, (
        "these update_by_query calls filter on the ambiguous `file_id` term with no "
        "plane predicate distinguishing MediaFile from Document chunks (#T10) — add "
        "one of search_indexing_task.py's _document_plane_clause / "
        "_document_plane_exclusion_clause, or an inline doc_type exclusion: " + ", ".join(findings)
    )
