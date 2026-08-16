"""The requested fusion strategy reaches the wire at every attach site (#363).

A search pipeline is attached **per request**, via the ``search_pipeline`` query
parameter, at four sites:

===============================================  ==================================
``hybrid_search_service._search_with_two_phase``  phase 1 of a non-relevance sort
``hybrid_search_service._search_with_collapse``   the main hybrid search body
``chunk_retrieval.retrieve_chunks``               RAG chat, chunk plane
``chunk_retrieval.retrieve_digests``              RAG chat, digest plane (Stage 4)
===============================================  ==================================

#363's review comment named the first three; the fourth arrived with Stage 4. A
site that kept reading ``settings.OPENSEARCH_SEARCH_PIPELINE`` would run the
default strategy while the caller believed it had selected another one — and
would report a number, not an error. Every site is therefore asserted on the
recorded request parameters rather than on a call having happened.

Module-global pipeline-verification state is mutated here, so the module takes an
``xdist_group`` and resets that state around every test.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.services.search import chunk_retrieval
from app.services.search import hybrid_search_service as hss
from app.services.search.fusion import NORMALIZATION
from app.services.search.fusion import RRF
from app.services.search.fusion import FusionConfig
from app.services.search.fusion import search_pipeline_id

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("search_fusion_state")]

_NORM = FusionConfig(strategy=NORMALIZATION, normalization_technique="l2")
_RRF60 = FusionConfig(strategy=RRF, rank_constant=60)


class _Recorder:
    """An OpenSearch client stand-in that keeps every request it was handed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        index: str | None = None,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"index": index, "body": body, "params": params or {}})
        return {"hits": {"hits": [], "total": {"value": 0}}}

    @property
    def attached_pipelines(self) -> list[str | None]:
        return [call["params"].get("search_pipeline") for call in self.calls]


@pytest.fixture(autouse=True)
def _clean_pipeline_state():
    """No verified-pipeline id survives into or out of a test in this module."""
    hss.reset_infrastructure_state()
    hss.clear_search_cache()
    yield
    hss.reset_infrastructure_state()
    hss.clear_search_cache()


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    """A recorder wired in at both modules' client seams, with neural forced on."""
    rec = _Recorder()
    monkeypatch.setattr(hss, "get_opensearch_client", lambda: rec)
    monkeypatch.setattr(chunk_retrieval, "get_opensearch_client", lambda: rec)
    monkeypatch.setattr(
        hss.HybridSearchService, "_get_neural_model_id", lambda self: "model-under-test"
    )
    monkeypatch.setattr(
        hss.HybridSearchService,
        "_generate_query_embedding",
        lambda self, query, mode: (None, True, True),
    )
    # ensure_search_pipeline_exists talks to the cluster; the id derivation under
    # test does not, so the cluster call is the seam and the id is the assertion.
    monkeypatch.setattr(hss, "ensure_search_pipeline_exists", lambda fusion=None: True)
    return rec


def _collapse_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "query": "budget",
        "search_query": "budget",
        "filters": [{"terms": {"accessible_user_ids": [7]}}],
        "page": 1,
        "page_size": 10,
        "sort_by": "relevance",
        "sort_order": "desc",
        "search_mode": "hybrid",
        "filters_applied": {},
        "start_time": time.time(),
        "has_speaker_filter": False,
        "use_neural": True,
    }
    kwargs.update(overrides)
    return kwargs


class TestSearchUiAttachSites:
    def test_the_main_hybrid_body_attaches_the_requested_pipeline(self, recorder):
        hss.HybridSearchService()._search_with_collapse(
            **_collapse_kwargs(search_pipeline="pipeline-under-test")
        )

        assert recorder.calls, "no request reached the client"
        assert recorder.attached_pipelines[0] == "pipeline-under-test"

    def test_the_two_phase_first_phase_attaches_the_requested_pipeline(self, recorder):
        kwargs = _collapse_kwargs(search_pipeline="two-phase-pipeline")
        del kwargs["use_neural"]
        hss.HybridSearchService()._search_with_two_phase(**kwargs)

        assert recorder.calls, "no request reached the client"
        assert recorder.attached_pipelines[0] == "two-phase-pipeline"

    def test_search_resolves_the_request_strategy_and_hands_it_down(self, recorder, monkeypatch):
        seen: list[str] = []

        def _capture(self, **kwargs):
            seen.append(kwargs["search_pipeline"])
            return self._empty_response(kwargs["query"], kwargs["page"], kwargs["page_size"])

        monkeypatch.setattr(hss.HybridSearchService, "_search_with_collapse", _capture)

        hss.HybridSearchService().search("budget", user_id=7, fusion=_NORM)

        assert seen == [search_pipeline_id(_NORM)]
        assert seen[0] != "transcript-hybrid-search"

    def test_search_without_a_strategy_still_uses_the_historical_pipeline(
        self, recorder, monkeypatch
    ):
        """The control: unchanged behaviour for every existing caller."""
        seen: list[str] = []

        def _capture(self, **kwargs):
            seen.append(kwargs["search_pipeline"])
            return self._empty_response(kwargs["query"], kwargs["page"], kwargs["page_size"])

        monkeypatch.setattr(hss.HybridSearchService, "_search_with_collapse", _capture)

        hss.HybridSearchService().search("budget", user_id=7)

        assert seen == ["transcript-hybrid-search"]


class TestResponseCacheKeysOnTheStrategy:
    def test_two_strategies_do_not_serve_each_others_cached_page(self, recorder, monkeypatch):
        """Without the strategy in the key, arm B would replay arm A's page.

        The middle call is the control: it proves the cache is really caching, so
        the third call missing means the key changed and not that caching is off.
        """
        calls: list[str] = []

        def _capture(self, **kwargs):
            calls.append(kwargs["search_pipeline"])
            return self._empty_response(kwargs["query"], kwargs["page"], kwargs["page_size"])

        monkeypatch.setattr(hss.HybridSearchService, "_search_with_collapse", _capture)
        service = hss.HybridSearchService()

        service.search("budget", user_id=7, fusion=_RRF60)
        assert len(calls) == 1

        service.search("budget", user_id=7, fusion=_RRF60)
        assert len(calls) == 1, "the cache did not serve the repeat; the control is broken"

        service.search("budget", user_id=7, fusion=_NORM)
        assert len(calls) == 2
        assert calls == [search_pipeline_id(_RRF60), search_pipeline_id(_NORM)]


class TestChatAttachSites:
    def test_chunk_retrieval_attaches_the_requested_pipeline(self, recorder):
        chunk_retrieval.retrieve_chunks("budget", user_id=7, fusion=_NORM)

        assert recorder.calls, "no request reached the client"
        assert recorder.attached_pipelines == [search_pipeline_id(_NORM)]

    def test_digest_retrieval_attaches_the_requested_pipeline(self, recorder):
        chunk_retrieval.retrieve_digests("budget", user_id=7, fusion=_RRF60)

        assert recorder.calls, "no request reached the client"
        assert recorder.attached_pipelines == [search_pipeline_id(_RRF60)]

    def test_chat_without_a_strategy_still_uses_the_historical_pipeline(self, recorder):
        chunk_retrieval.retrieve_chunks("budget", user_id=7)
        chunk_retrieval.retrieve_digests("budget", user_id=7)

        assert recorder.attached_pipelines == [
            "transcript-hybrid-search",
            "transcript-hybrid-search",
        ]

    def test_a_semantic_only_body_still_attaches_nothing(self, recorder):
        """One leg is not a fusion. Selecting a strategy must not change that."""
        chunk_retrieval.retrieve_chunks("budget", user_id=7, search_mode="semantic", fusion=_NORM)

        assert recorder.calls, "no request reached the client"
        assert recorder.attached_pipelines == [None]


def _record_ensure_calls(monkeypatch) -> list[str]:
    """Replace the cluster call with a recorder, and hand back what it saw."""
    seen: list[str] = []

    def _ensure(fusion: FusionConfig | None = None) -> bool:
        assert fusion is not None, "the cluster call must be told which strategy to write"
        seen.append(fusion.slug())
        return True

    monkeypatch.setattr(hss, "ensure_search_pipeline_exists", _ensure)
    return seen


class TestVerifiedPipelineCache:
    def test_verifying_one_strategy_does_not_certify_another(self, monkeypatch):
        """A single boolean flag let the first strategy certify every later one.

        The second arm would then attach an id nobody created, OpenSearch would
        run the query unfused, and the run would report a number.
        """
        seen = _record_ensure_calls(monkeypatch)

        hss.ensure_fusion_pipeline(FusionConfig(strategy=RRF))
        hss.ensure_fusion_pipeline(_NORM)

        assert seen == ["rrf-30", _NORM.slug()]

    def test_a_verified_pipeline_is_not_re_verified(self, monkeypatch):
        seen = _record_ensure_calls(monkeypatch)

        for _ in range(3):
            hss.ensure_fusion_pipeline(_NORM)

        assert seen == [_NORM.slug()]

    def test_reset_infrastructure_state_clears_every_cached_pipeline_id(self, monkeypatch):
        """A stale id survives a config change and the next request trusts it."""
        seen = _record_ensure_calls(monkeypatch)

        hss.ensure_fusion_pipeline(FusionConfig(strategy=RRF))
        hss.ensure_fusion_pipeline(_NORM)
        assert len(seen) == 2
        assert hss._verified_pipelines

        hss.reset_infrastructure_state()

        assert hss._verified_pipelines == set()
        hss.ensure_fusion_pipeline(FusionConfig(strategy=RRF))
        hss.ensure_fusion_pipeline(_NORM)
        assert seen == ["rrf-30", _NORM.slug(), "rrf-30", _NORM.slug()]

    def test_reset_infrastructure_state_also_clears_the_index_flag(self, monkeypatch):
        """The index check shares the reset; #363 must not have dropped it."""
        monkeypatch.setattr(hss, "ensure_chunks_index_exists", lambda: True)
        monkeypatch.setattr(hss, "ensure_search_pipeline_exists", lambda fusion=None: True)

        hss._ensure_infrastructure()
        assert hss._index_verified is True

        hss.reset_infrastructure_state()

        assert hss._index_verified is False
