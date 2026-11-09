"""Every index reader excludes a trashed file's documents through the index itself (#1101).

A trashed file keeps its OpenSearch documents until purge, stamped ``availability: trashed``.
These tests pin the QUERY BODIES the readers send: the transcript-chunk plane through the one
ACL builder (``access_filter_clause``, which ``_build_filters``, suggestions, facets, the
summary plane and chat aggregation all AND in), and the legacy ``transcripts`` readers. The
speaker kNN bodies are guarded separately (``test_knn_bodies_exclude_unavailable.py``).

The exclusion is ``must_not terms availability: [trashed, quarantined]`` (``trashed`` alone under
the quarantine review exemption) and never a positive term: a document
written before the field existed has no value and must stay visible.

The last test is the "10,001 trashed files still searches" property in the form a unit test
can prove: the clause is a constant. Building it never consults the trash, so its size cannot
grow with it, and there is no per-file id list in it to hit ``index.max_terms_count`` or the
quarantine list's 10,000 cap (that list fails closed with a 503, which is why the trash does
not reuse it). The live twin runs the same search against 10,001 real trashed rows
(``tests/integration/test_trash_index_lifecycle_opensearch.py``).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Query

from app.models.media import MediaFile
from tests.fixtures.opensearch_filter_eval import matches

UNAVAILABLE = {"terms": {"availability": ["trashed", "quarantined"]}}


def _excludes_trashed(clause: dict[str, Any]) -> bool:
    """True when ``clause`` (or a ``bool`` it nests) carries a ``must_not`` that a trashed
    document matches."""
    inner = clause.get("bool", {})
    must_not = inner.get("must_not", [])
    if any(
        matches({"availability": "trashed"}, q)
        for q in (must_not if isinstance(must_not, list) else [must_not])
    ):
        return True
    nested = inner.get("filter", [])
    return any(_excludes_trashed(c) for c in (nested if isinstance(nested, list) else [nested]))


def test_the_acl_clause_ands_in_not_trashed_and_keeps_the_acl():
    from app.services.search.access_filter import access_filter_clause

    clause = access_filter_clause(7, None)

    assert clause == {
        "bool": {
            "filter": [{"terms": {"accessible_user_ids": [7]}}],
            "must_not": [UNAVAILABLE],
        }
    }


def test_the_org_acl_clause_excludes_the_trash_on_both_arms(monkeypatch):
    """The organization-collection arm must not reopen a trashed file a member could read."""
    import contextlib

    from app.services import collection_visibility
    from app.services.search.access_filter import access_filter_clause

    @contextlib.contextmanager
    def _no_db():
        yield None

    monkeypatch.setattr("app.db.session_utils.session_scope", _no_db)
    monkeypatch.setattr(collection_visibility, "org_readable_collection_ids", lambda *_a: [3, 4])

    clause = access_filter_clause(7, 11)

    assert clause["bool"]["must_not"] == [UNAVAILABLE]
    (acl,) = clause["bool"]["filter"]
    assert {"terms": {"collection_ids": [3, 4]}} in acl["bool"]["should"]


def test_the_search_filter_list_excludes_the_trash():
    """``_build_filters`` is the filter list of search hits, counts, the summary plane and chat
    chunk/digest retrieval (``chunk_retrieval``)."""
    from app.services.search.filters import _build_filters

    filters = _build_filters(
        user_id=7, speakers=None, tags=None, date_from=None, date_to=None, organization_id=None
    )

    assert any(_excludes_trashed(f) for f in filters), filters


def test_the_chat_aggregation_filters_exclude_the_trash():
    from app.services.chat.aggregation import base_filters

    filters = base_filters(user_id=7, organization_id=None, file_uuids=None)

    assert any(_excludes_trashed(f) for f in filters), filters


class _RecordingClient:
    """Records search bodies; answers with no hits."""

    def __init__(self, get_source: dict[str, Any] | None = None) -> None:
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self._get_source = get_source

    def search(self, index: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.bodies.append((index, body))
        return {"hits": {"total": {"value": 0}, "hits": []}}

    def get(self, **_kw: Any) -> dict[str, Any]:
        return {"_source": self._get_source or {}}


def test_the_gallery_transcript_filter_excludes_the_trash(monkeypatch):
    """The legacy whole-transcript index (gallery "search in transcripts")."""
    from app.services import file_list_filters as filtering

    client = _RecordingClient()
    monkeypatch.setattr("app.services.opensearch_service.get_opensearch_client", lambda: client)

    filtering.apply_transcript_search_filter(
        Query([MediaFile]), "budget", user_id=7, organization_id=None
    )

    ((_index, body),) = client.bodies
    assert any(_excludes_trashed(f) for f in body["query"]["bool"]["filter"]), body


def _filters_with_a_trash_of(monkeypatch, size: int) -> tuple[list[dict[str, Any]], list[str]]:
    """``_build_filters`` with Postgres reporting ``size`` trashed files; returns the filters and
    the trash reads the build made."""
    from app.services import file_availability
    from app.services.search.filters import _build_filters

    reads: list[str] = []
    trash = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(size)]

    def _trashed_ids(*_a: Any, **_k: Any) -> dict[int, str]:
        reads.append("unavailable_file_states")
        return dict.fromkeys(range(size), "trashed")

    def _unavailable(*_a: Any, **_k: Any) -> set[str]:
        reads.append("unavailable_among")
        return set(trash)

    monkeypatch.setattr(file_availability, "unavailable_file_states", _trashed_ids)
    monkeypatch.setattr(file_availability, "unavailable_among", _unavailable)
    filters = _build_filters(
        user_id=7, speakers=None, tags=None, date_from=None, date_to=None, organization_id=None
    )
    return filters, reads


def test_the_read_clause_is_a_constant_whatever_the_trash_holds(monkeypatch):
    """No per-file id list: a body over 10,001 trashed files equals the one over an empty trash.

    The builder never reads the trash (the quarantine-list shape would, and past 10,000 ids it
    fails closed with a 503), and the one trash clause names no file.
    """
    empty, empty_reads = _filters_with_a_trash_of(monkeypatch, 0)
    large, large_reads = _filters_with_a_trash_of(monkeypatch, 10_001)

    assert empty_reads == [] and large_reads == []
    assert json.dumps(large, sort_keys=True) == json.dumps(empty, sort_keys=True)
    trash_clauses = [f for f in large if _excludes_trashed(f)]
    assert len(trash_clauses) == 1
    serialized = json.dumps(trash_clauses[0], sort_keys=True)
    assert "file_uuid" not in serialized and "file_id" not in serialized
