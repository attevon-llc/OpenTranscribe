"""The summary plane applies the caller's quarantine set as a PRE-filter on every leg (#818).

``summary_search.search_summaries`` resolves the exclusion set from Postgres and hands it to
``HybridSearchService.search_summary_plane``; ``tests/unit/test_summary_search.py`` pins that
hand-off. This file pins the other half: the set becomes a ``must_not`` inside the filter list
of the request the plane actually sends, in the BM25 body and in BOTH legs of the hybrid body,
so ``total`` and the page are computed over what the caller may see. The real cluster
behaviour is ``tests/api/test_search_summaries_quarantine.py``, which needs OpenSearch; this
one runs without it, against the request body, through the service's ``get_opensearch_client``
lookup.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.search import hybrid_search_service as hss

pytestmark = pytest.mark.unit

TAKEN_DOWN = ["quarantined-file-uuid"]
EXCLUSION = {"bool": {"must_not": {"terms": {"file_uuid": TAKEN_DOWN}}}}


class _Recorder:
    """An OpenSearch client stand-in that keeps every search body it was handed."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def search(
        self,
        index: str | None = None,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert body is not None
        self.bodies.append(body)
        return {"hits": {"hits": []}}


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(hss, "get_opensearch_client", lambda: rec)
    monkeypatch.setattr(hss, "_ensure_infrastructure", lambda fusion=None: "pipeline-under-test")
    return rec


def _neural(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(
        hss.HybridSearchService, "_check_neural_search_available", lambda self: enabled
    )
    monkeypatch.setattr(hss.HybridSearchService, "_get_neural_model_id", lambda self: "model-x")


def _leg_filters(body: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """The filter list of every leg the body sends (one for BM25, two for hybrid)."""
    query = body["query"]
    if "hybrid" in query:
        return [leg["bool"]["filter"] for leg in query["hybrid"]["queries"]]
    return [query["bool"]["filter"]]


def test_the_bm25_body_excludes_the_quarantined_files(recorder, monkeypatch):
    _neural(monkeypatch, enabled=False)

    hss.HybridSearchService().search_summary_plane(
        "roadmap", 7, quarantined_file_uuids=list(TAKEN_DOWN)
    )

    assert len(recorder.bodies) == 1
    legs = _leg_filters(recorder.bodies[0])
    assert len(legs) == 1
    assert EXCLUSION in legs[0]


def test_both_hybrid_legs_exclude_the_quarantined_files(recorder, monkeypatch):
    """The kNN leg is a query of its own: an exclusion on the BM25 leg alone leaks via it."""
    _neural(monkeypatch, enabled=True)

    hss.HybridSearchService().search_summary_plane(
        "roadmap", 7, quarantined_file_uuids=list(TAKEN_DOWN)
    )

    assert len(recorder.bodies) == 1
    legs = _leg_filters(recorder.bodies[0])
    assert len(legs) == 2, "the neural arm was not taken, so this proves nothing about kNN"
    for filters in legs:
        assert EXCLUSION in filters


def test_no_exclusion_set_adds_no_exclusion_clause(recorder, monkeypatch):
    """The control: the clause comes from the argument, not from the body's fixed shape."""
    _neural(monkeypatch, enabled=False)

    hss.HybridSearchService().search_summary_plane("roadmap", 7, quarantined_file_uuids=[])

    (filters,) = _leg_filters(recorder.bodies[0])
    file_exclusions = [
        clause
        for clause in filters
        if "file_uuid" in clause.get("bool", {}).get("must_not", {}).get("terms", {})
    ]
    assert file_exclusions == []
    # The personal-scope tenant gate is a must_not of its own; it must still be there.
    assert {"bool": {"must_not": {"exists": {"field": "organization_id"}}}} in filters
