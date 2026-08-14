"""The ``transcript_summaries`` OpenSearch index is retired (#67).

Grounding moved to the digest plane inside the v6 ``transcript_chunks`` index
(``doc_type: "digest"``), and the *displayed* summary has always been written to
``media_file.summary_data`` in the same transaction that wrote the OpenSearch
document. That left a second index holding a byte-for-byte duplicate of a
PostgreSQL JSONB column, plus a read path that preferred the copy over the
original.

What these tests pin, and why each would have been a silent regression:

* **No writer.** A write-only index is worse than either extreme — it pays
  indexing cost, doubles the GDPR erasure surface, and its contents can drift
  from the column the UI actually renders.
* **No reader.** ``GET /files/{uuid}/summary`` must answer from PostgreSQL. When
  it preferred OpenSearch, a cluster outage silently downgraded to the "fallback"
  and nothing distinguished that from having no summary.
* **No route.** ``POST /api/files/search`` searched only this index. Left mounted
  over an index nothing writes, it would answer ``200 {"hits": [], "total": 0}``
  forever — an absent signal indistinguishable from a genuine empty result.

**The legacy-purge paths deliberately survive** and are asserted here as such:
an upgraded deployment still has the index on disk with real user summaries in
it, so file deletion and GDPR erasure must keep sweeping it until an operator
deletes it. Those call sites are the allow-list below; anything else naming the
index is a resurrection.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
from fastapi import status

from app.main import app

_BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

#: Modules allowed to name the retired index. Each purges or reports on data
#: that already exists in deployed clusters; none creates, writes or reads it
#: for display.
_LEGACY_PURGE_ALLOWLIST = {
    "core/config.py",  # the setting the purge paths resolve the name through
    "services/file_cleanup_service.py",  # per-file delete + GDPR erasure sweep
    "tasks/opensearch_integrity_task.py",  # orphan sweep + operator index census
}


def test_the_summary_index_service_module_is_gone() -> None:
    """``OpenSearchSummaryService`` was the only creator/reader/writer."""
    assert importlib.util.find_spec("app.services.opensearch_summary_service") is None, (
        "app/services/opensearch_summary_service.py still exists; it creates the "
        "retired index on construction (_ensure_summary_index_exists)"
    )


def _names_the_retired_index(source: str) -> bool:
    """Whether a module refers to the index *in code* — never merely in prose.

    A substring scan is wrong here and it fired on the first run: five modules
    carry a comment or docstring explaining the retirement, and a check that
    cannot tell an explanation from a resurrection pressures the next author to
    delete the explanation. So: a ``Name``/``Attribute`` spelled
    ``OPENSEARCH_SUMMARY_INDEX``, or a string constant that *is* the index name.
    A docstring mentioning it is a longer string and does not match.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "OPENSEARCH_SUMMARY_INDEX":
            return True
        if isinstance(node, ast.Name) and node.id == "OPENSEARCH_SUMMARY_INDEX":
            return True
        if isinstance(node, ast.Constant) and node.value == "transcript_summaries":
            return True
    return False


def test_the_prose_and_code_detectors_disagree_as_intended() -> None:
    """Guard the guard: a scanner that matched nothing would pass everything."""
    assert _names_the_retired_index("client.count(index=settings.OPENSEARCH_SUMMARY_INDEX)")
    assert _names_the_retired_index('INDEX = "transcript_summaries"')
    assert not _names_the_retired_index('"""The transcript_summaries index is retired."""')
    assert not _names_the_retired_index("x = 1  # transcript_summaries went away")


def test_only_the_legacy_purge_paths_name_the_retired_index() -> None:
    """Anything else naming it is a new writer or a new read path."""
    offenders = []
    for path in sorted(_BACKEND_APP.rglob("*.py")):
        rel = path.relative_to(_BACKEND_APP).as_posix()
        if rel in _LEGACY_PURGE_ALLOWLIST:
            continue
        if _names_the_retired_index(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, (
        f"these modules reference the retired transcript_summaries index: {offenders}. "
        "Only the legacy-purge paths may, and they must no-op when it is absent."
    )


def test_the_summarization_task_does_not_index_to_opensearch() -> None:
    """The task writes ``media_file.summary_data`` and nothing else."""
    source = (_BACKEND_APP / "tasks" / "summarization.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    resurrected = names & {
        "_index_summary",
        "_store_summary_to_opensearch",
        "_clear_stale_opensearch_summary",
    }
    assert not resurrected, f"summarization.py regrew an OpenSearch summary writer: {resurrected}"
    assert "OpenSearchSummaryService" not in source


def test_the_summary_search_route_is_unmounted() -> None:
    """It searched only the retired index, so it can no longer answer anything."""
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/files/search" not in paths, (
        "POST /api/files/search is still mounted; over an index nothing writes it "
        "answers an empty 200, which reads as 'no matches' rather than 'retired'"
    )


def test_the_unmounted_search_path_answers_405_not_404(client, user_token_headers) -> None:
    """The wire-level consequence of the unmount, for an API-only caller.

    **405, not 404**, and the difference is routing rather than intent: with the
    literal ``/search`` route gone, ``/api/files/search`` is absorbed by the files
    router's ``/api/files/{file_uuid}``, which serves GET and DELETE — so FastAPI
    reports Method Not Allowed. Same family as the ``GET /api/files/analytics``
    422 pinned in ``test_summarization_endpoints.py``: a literal segment under a
    parameterized prefix never simply disappears. Pinned so the path cannot
    silently start routing somewhere that answers.
    """
    response = client.post(
        "/api/files/search", headers=user_token_headers, json={"query": "budget"}
    )
    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


def test_the_summary_search_schemas_are_gone() -> None:
    """They described the retired index's hit shape only."""
    import app.schemas.summary as summary_schemas

    for name in ("SummarySearchRequest", "SummarySearchHit", "SummarySearchResponse"):
        assert not hasattr(summary_schemas, name), f"{name} outlived the route it shaped"


@pytest.mark.parametrize("field", ["source", "document_id", "created_at", "updated_at"])
def test_summary_response_drops_the_opensearch_provenance_fields(field: str) -> None:
    """They described *which OpenSearch document* answered; there is no longer one."""
    from app.schemas.summary import SummaryResponse

    assert field not in SummaryResponse.model_fields
