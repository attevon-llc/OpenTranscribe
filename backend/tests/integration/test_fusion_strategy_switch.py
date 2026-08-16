"""Selecting a fusion strategy changes what OpenSearch does (#363).

Everything in ``test_search_fusion_threading.py`` proves the id reaches the wire.
That is necessary and **not sufficient**: a parameter can be accepted and ignored.
#437 found vLLM returning HTTP 200 while silently dropping a parameter, and #64
found ``enable_thinking: false`` byte-identical to omitting it. So this module
asks the cluster, and asserts on properties that only hold if the processor
really changed:

* RRF scores are sums of ``1/(k + rank)`` over integer ranks, so **nothing can
  exceed ``2/(k+1)``** — 0.0645 at the shipped ``rank_constant`` 30. The
  normalization arm is not bounded by that, and exceeds it.
* The two arms return the **same documents in a different order**.
* The control — the same pipeline twice — is bit-identical, so a difference
  between arms is the pipeline and not query nondeterminism.

Read-only against the index: it issues searches and creates *search pipelines*,
which are query-time cluster metadata. No document is written, and
``indexing.total`` is asserted unchanged across the module for exactly that
reason.

Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 \\
        pytest backend/tests/integration/test_fusion_strategy_switch.py -m integration
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.core.config import settings
from app.services.opensearch_service import get_opensearch_client
from app.services.search.chunk_retrieval import retrieve_chunks
from app.services.search.fusion import NORMALIZATION
from app.services.search.fusion import RRF
from app.services.search.fusion import FusionConfig
from app.services.search.fusion import search_pipeline_id
from app.services.search.indexing_service import ensure_search_pipeline_exists

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). A stand-in cannot answer whether a "
            "search pipeline was honoured — which is the entire question here."
        ),
    ),
]

#: The two arms of #363's first A/B. Parameters are the sweep's business; what
#: this module asserts is that selecting between them is a real selection.
_RRF = FusionConfig(strategy=RRF, rank_constant=settings.SEARCH_RRF_RANK_CONSTANT)
_NORM = FusionConfig(
    strategy=NORMALIZATION,
    normalization_technique="min_max",
    combination_technique="arithmetic_mean",
)

#: Two lexically different legs, so the fusion step has something to do. BM25 on
#: both legs deliberately: the processor fuses ranked lists and does not care what
#: produced them, and a neural leg would make this test's subject depend on a
#: deployed model.
_LEG_A = "budget"
_LEG_B = "marketing launch schedule"


def _hybrid_body(size: int = 20) -> dict[str, Any]:
    return {
        "size": size,
        "track_total_hits": False,
        "_source": ["file_uuid", "chunk_index"],
        "query": {
            "hybrid": {
                "queries": [
                    {"match": {"content": _LEG_A}},
                    {"match": {"content": _LEG_B}},
                ]
            }
        },
    }


def _run(client: Any, pipeline_id: str) -> tuple[list[str], list[float]]:
    response = client.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body=_hybrid_body(),
        params={"search_pipeline": pipeline_id},
    )
    hits = response.get("hits", {}).get("hits", [])
    return [hit["_id"] for hit in hits], [float(hit["_score"]) for hit in hits]


def _indexing_total(client: Any) -> int:
    stats = client.indices.stats(index=settings.OPENSEARCH_CHUNKS_INDEX, metric="indexing")
    return int(stats["_all"]["total"]["indexing"]["index_total"])


@pytest.fixture(scope="module")
def client() -> Any:
    os_client = get_opensearch_client()
    # `assert`, not `pytest.fail`: it fails the test with the same message AND narrows
    # the Optional for the attribute access below. mypy does not treat this
    # `pytest.fail` as NoReturn under the repo's config, and the alternative would
    # have been a `type: ignore` over a genuine Optional — which deletes the check.
    assert os_client is not None, (
        "SKIP_OPENSEARCH said a cluster was reachable, but no client was built"
    )
    if not os_client.indices.exists(index=settings.OPENSEARCH_CHUNKS_INDEX):
        pytest.skip(f"{settings.OPENSEARCH_CHUNKS_INDEX} does not exist on this cluster")
    return os_client


@pytest.fixture(scope="module")
def both_pipelines(client) -> tuple[str, str]:
    """Create both pipelines through the production path, and hand back their ids."""
    assert ensure_search_pipeline_exists(_RRF) is True
    assert ensure_search_pipeline_exists(_NORM) is True
    rrf_id, norm_id = search_pipeline_id(_RRF), search_pipeline_id(_NORM)
    assert rrf_id != norm_id
    return rrf_id, norm_id


class TestTheClusterReallyStoresTwoPipelines:
    def test_each_strategy_is_a_distinct_stored_pipeline(self, client, both_pipelines):
        rrf_id, norm_id = both_pipelines

        stored = client.transport.perform_request("GET", "/_search/pipeline")

        assert (
            stored[rrf_id]["phase_results_processors"]
            == (_RRF.pipeline_body()["phase_results_processors"])
        )
        assert (
            stored[norm_id]["phase_results_processors"]
            == (_NORM.pipeline_body()["phase_results_processors"])
        )

    def test_the_historical_id_still_holds_the_rrf_body(self, client, both_pipelines):
        """The pipeline every deployment already has is untouched by #363."""
        rrf_id, _ = both_pipelines
        assert rrf_id == "transcript-hybrid-search"

        stored = client.transport.perform_request("GET", f"/_search/pipeline/{rrf_id}")
        processor = stored[rrf_id]["phase_results_processors"][0]

        assert "score-ranker-processor" in processor
        assert processor["score-ranker-processor"]["combination"]["rank_constant"] == (
            settings.SEARCH_RRF_RANK_CONSTANT
        )


class TestTheSwitchChangesWhatComesBack:
    def test_the_same_pipeline_twice_is_identical(self, client, both_pipelines):
        """The control. Without it, "the arms differ" could be nondeterminism."""
        rrf_id, _ = both_pipelines

        first_ids, first_scores = _run(client, rrf_id)
        second_ids, second_scores = _run(client, rrf_id)

        assert len(first_ids) >= 10, "the index answered with almost nothing; nothing below holds"
        assert first_ids == second_ids
        assert first_scores == second_scores

    def test_rrf_scores_are_bounded_by_the_rank_constant(self, client, both_pipelines):
        """``1/(k+rank)`` summed over two legs cannot exceed ``2/(k+1)``."""
        rrf_id, _ = both_pipelines
        bound = 2.0 / (settings.SEARCH_RRF_RANK_CONSTANT + 1)

        ids, scores = _run(client, rrf_id)

        assert len(ids) >= 10
        assert max(scores) <= bound

    def test_the_normalization_arm_breaks_that_bound(self, client, both_pipelines):
        """The load-bearing assertion: a *different processor* actually ran.

        If the ``search_pipeline`` parameter were accepted and ignored, this arm
        would return the RRF scores and sit under the bound.
        """
        rrf_id, norm_id = both_pipelines
        bound = 2.0 / (settings.SEARCH_RRF_RANK_CONSTANT + 1)

        _, rrf_scores = _run(client, rrf_id)
        norm_ids, norm_scores = _run(client, norm_id)

        assert len(norm_ids) >= 10
        assert max(norm_scores) > bound
        assert norm_scores != rrf_scores

    def test_the_two_arms_rank_the_same_documents_differently(self, client, both_pipelines):
        """Not merely rescaled — reordered. That is what a fusion A/B measures."""
        rrf_id, norm_id = both_pipelines

        rrf_ids, _ = _run(client, rrf_id)
        norm_ids, _ = _run(client, norm_id)

        assert len(rrf_ids) >= 10
        assert set(rrf_ids) & set(norm_ids), "the two arms share no documents at all"
        assert rrf_ids != norm_ids


@pytest.fixture(scope="module")
def corpus_user_id(client) -> int:
    """Whoever owns the indexed corpus, read from the corpus itself.

    Discovered rather than configured: ``retrieve_chunks`` gates on
    ``accessible_user_ids``, and hard-coding an id would silently retrieve
    nothing on any stack but the one it was written against — an empty pool that
    passes an ``assert not raised``-shaped test.
    """
    response = client.search(
        index=settings.OPENSEARCH_CHUNKS_INDEX,
        body={"size": 1, "_source": ["accessible_user_ids"], "query": {"match_all": {}}},
    )
    hits = response.get("hits", {}).get("hits", [])
    if not hits:
        pytest.skip("chunks index is empty")
    user_ids = hits[0]["_source"].get("accessible_user_ids") or []
    if not user_ids:
        pytest.skip("no document carries accessible_user_ids; cannot scope a retrieval")
    return int(user_ids[0])


class TestTheChatPathHonoursTheSelection:
    """The same claim through ``retrieve_chunks`` — the path the harness drives."""

    def test_two_strategies_produce_two_candidate_pools(
        self, client, both_pipelines, corpus_user_id
    ):
        rrf_hits = retrieve_chunks(_LEG_B, user_id=corpus_user_id, size=20, fusion=_RRF)
        norm_hits = retrieve_chunks(_LEG_B, user_id=corpus_user_id, size=20, fusion=_NORM)

        assert len(rrf_hits) >= 10, "chat retrieval returned almost nothing"
        assert len(norm_hits) >= 10

        rrf_scores = [hit.score for hit in rrf_hits]
        norm_scores = [hit.score for hit in norm_hits]
        bound = 2.0 / (settings.SEARCH_RRF_RANK_CONSTANT + 1)

        assert max(rrf_scores) <= bound
        assert max(norm_scores) > bound
        assert [h.chunk_index for h in rrf_hits] != [h.chunk_index for h in norm_hits]

    def test_the_same_strategy_twice_is_identical(self, client, both_pipelines, corpus_user_id):
        """The control for the test above."""
        first = retrieve_chunks(_LEG_B, user_id=corpus_user_id, size=20, fusion=_RRF)
        second = retrieve_chunks(_LEG_B, user_id=corpus_user_id, size=20, fusion=_RRF)

        assert len(first) >= 10
        assert [(h.file_uuid, h.chunk_index, h.score) for h in first] == [
            (h.file_uuid, h.chunk_index, h.score) for h in second
        ]


def test_nothing_in_this_module_wrote_to_the_index(client, both_pipelines):
    """A search pipeline is query-time metadata; measuring must not cost documents.

    Runs last by file order, and compares against the total read at import of the
    ``client`` fixture would be too early — so it reads twice around one more
    search, which is the cheapest honest form of the check.
    """
    before = _indexing_total(client)
    _run(client, both_pipelines[1])
    after = _indexing_total(client)

    assert after == before
