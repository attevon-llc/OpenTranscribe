"""The summary plane excludes quarantined files as a PRE-filter on every leg (#818, #1253).

``summary_search.search_summaries`` hands the caller's review exemption to
``HybridSearchService.search_summary_plane``; ``tests/unit/test_summary_search.py`` pins that
hand-off. This file pins the other half: the ``availability`` exclusion sits inside the filter
list of the request the plane actually sends, in the BM25 body and in BOTH legs of the hybrid
body, so ``total`` and the page are computed over what the caller may see. The real cluster
behaviour is ``tests/api/test_search_summaries_quarantine.py``, which needs OpenSearch; this
one runs without it, against the request body, through the service's ``get_opensearch_client``
lookup.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.search import hybrid_search_service as hss
from tests.fixtures.opensearch_filter_eval import matches_all

pytestmark = pytest.mark.unit

QUARANTINED_DOC = {"accessible_user_ids": [7], "availability": "quarantined"}
TRASHED_DOC = {"accessible_user_ids": [7], "availability": "trashed"}


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


def _admits(filters: list[dict[str, Any]], doc: dict[str, Any]) -> bool:
    """Whether a cluster obeying ``filters`` would return ``doc`` (a personal-scope chunk)."""
    return matches_all({**doc, "doc_type": "summary"}, filters)


def test_the_bm25_body_excludes_the_quarantined_files(recorder, monkeypatch):
    _neural(monkeypatch, enabled=False)

    hss.HybridSearchService().search_summary_plane("roadmap", 7)

    assert len(recorder.bodies) == 1
    (filters,) = _leg_filters(recorder.bodies[0])
    assert not _admits(filters, QUARANTINED_DOC)
    assert _admits(filters, {"accessible_user_ids": [7]}), "control: a live summary is admitted"


def test_both_hybrid_legs_exclude_the_quarantined_files(recorder, monkeypatch):
    """The kNN leg is a query of its own: an exclusion on the BM25 leg alone leaks via it."""
    _neural(monkeypatch, enabled=True)

    hss.HybridSearchService().search_summary_plane("roadmap", 7)

    assert len(recorder.bodies) == 1
    legs = _leg_filters(recorder.bodies[0])
    assert len(legs) == 2, "the neural arm was not taken, so this proves nothing about kNN"
    for filters in legs:
        assert not _admits(filters, QUARANTINED_DOC)


def test_the_review_exemption_keeps_quarantined_and_never_trashed(recorder, monkeypatch):
    """The control: the quarantine exclusion comes from the argument, the trash's does not."""
    _neural(monkeypatch, enabled=False)

    hss.HybridSearchService().search_summary_plane("roadmap", 7, include_quarantined=True)

    (filters,) = _leg_filters(recorder.bodies[0])
    assert _admits(filters, QUARANTINED_DOC)
    assert not _admits(filters, TRASHED_DOC)
    # The personal-scope tenant gate is a must_not of its own; it must still be there.
    assert {"bool": {"must_not": {"exists": {"field": "organization_id"}}}} in filters
