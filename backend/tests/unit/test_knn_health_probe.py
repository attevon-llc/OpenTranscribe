"""The vector plane must be probed directly, and only real corruption may repair it.

Issue #540: after several back-to-back stack recreations every kNN/hybrid query on
``transcript_chunks`` answered ``503 search_phase_execution_exception`` while BM25 on
the same shard worked, the ML model predicted fine, and
``GET /search/models/neural/status`` reported everything healthy. Chat degraded to
``retrieval_failed`` on every turn. Lucene's text segments are crash-safe; the HNSW
vector files are the fragile part — so the ``match_all`` probe
``check_and_repair_indices`` relied on is structurally incapable of seeing this.

Two directions have to be pinned, and the second is the one that bites:

* **Under-reporting** — a corrupt vector plane must be *detected*. The BM25 probe never
  was, and ``transcript_chunks`` was not even in the checked index list.
* **Over-reporting** — repairing the chunk plane DELETES it and re-embeds every owner's
  corpus, so a false ``corrupt`` is far more damaging than a missed one. Two live shapes
  in this deployment produce a rejection carrying the literal string
  ``search_phase_execution_exception`` that ``_is_index_corruption_error`` matches:

  1. the legacy ``transcripts`` index declares ``knn_vector`` with **no ANN method**, so
     an ANN query is rejected ``400 … not built for ANN search``;
  2. an index with no documents answers a kNN query exactly like a populated one.

  Both were misreported as corrupt by the first version of this probe, which would have
  put an intact index into a rebuild loop on every 10-minute health tick.

A closed index is the counter-case that keeps the 4xx handling honest: it is *also* a
400, and it is a genuine fault.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from opensearchpy.exceptions import RequestError
from opensearchpy.exceptions import TransportError

MAIN_PY = Path(__file__).resolve().parents[3] / "backend" / "app" / "main.py"

ANN_METHOD = {
    "engine": "lucene",
    "space_type": "cosinesimil",
    "name": "hnsw",
    "parameters": {"ef_construction": 256, "m": 16},
}


class _FakeIndices:
    def __init__(self, parent: _FakeOpenSearch) -> None:
        self._parent = parent

    def exists(self, index: str) -> bool:
        return index in self._parent.mappings or index in self._parent.aliases

    def exists_alias(self, name: str) -> bool:
        return name in self._parent.aliases

    def get_alias(self, name: str) -> dict[str, Any]:
        return {self._parent.aliases[name]: {"aliases": {name: {}}}}

    def get_mapping(self, index: str) -> dict[str, Any]:
        return {index: {"mappings": {"properties": self._parent.mappings[index]}}}


class _FakeOpenSearch:
    """Just enough OpenSearch to drive the probe's decision tree.

    ``search`` either returns a hit envelope or raises whatever the test planted,
    which is how the real failure shapes (a 503 transport error, a 400 request
    error) are reproduced without a cluster.
    """

    def __init__(
        self,
        mappings: dict[str, dict[str, Any]],
        *,
        doc_counts: dict[str, int] | None = None,
        search_error: Exception | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.mappings = mappings
        self.doc_counts = doc_counts or {}
        self.search_error = search_error
        self.aliases = aliases or {}
        self.searched: list[tuple[str, dict[str, Any]]] = []
        self.indices = _FakeIndices(self)

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.searched.append((index, body))
        if self.search_error is not None:
            raise self.search_error
        return {"hits": {"total": {"value": 1, "relation": "eq"}, "hits": []}}

    def count(self, index: str) -> dict[str, int]:
        return {"count": self.doc_counts.get(index, 1)}


def _ann_mapping(dimension: int = 384) -> dict[str, Any]:
    return {"embedding": {"type": "knn_vector", "dimension": dimension, "method": ANN_METHOD}}


def _non_ann_mapping(dimension: int = 384) -> dict[str, Any]:
    """The legacy ``transcripts`` shape: a knn_vector field with no ANN method."""
    return {"embedding": {"type": "knn_vector", "dimension": dimension, "doc_values": True}}


@pytest.fixture
def probe(monkeypatch):
    """Return a callable that probes *index_name* against a planted fake cluster."""
    from app.services.opensearch_service import client as os_client

    def _run(fake: _FakeOpenSearch, index_name: str):
        monkeypatch.setattr(os_client, "opensearch_client", fake)
        os_client.reset_knn_health_cache()
        return os_client.probe_knn_health(index_name)

    return _run


# ---------------------------------------------------------------------------
# The failure this issue is about
# ---------------------------------------------------------------------------
def test_a_503_search_phase_execution_exception_is_corrupt(probe):
    """The exact live symptom: kNN 503s while the index is otherwise present."""
    fake = _FakeOpenSearch(
        {"transcript_chunks": _ann_mapping()},
        search_error=TransportError(503, "search_phase_execution_exception", {"failed_shards": []}),
    )
    result = probe(fake, "transcript_chunks")

    assert result.status == "corrupt"
    assert result.is_corrupt is True
    assert result.is_serviceable is False


def test_the_probe_queries_the_vector_plane_not_match_all(probe):
    """A BM25 probe cannot see this failure, so the query must be a real kNN one.

    Pinned because the whole defect is that ``check_and_repair_indices`` verified a
    vector index with ``match_all``.
    """
    fake = _FakeOpenSearch({"transcript_chunks": _ann_mapping(dimension=384)})
    probe(fake, "transcript_chunks")

    assert len(fake.searched) == 1
    _index, body = fake.searched[0]
    knn = body["query"]["knn"]["embedding"]
    assert len(knn["vector"]) == 384, "probe vector must match the declared dimension"
    assert knn["k"] == 1
    assert body["size"] == 0, "the probe must not pay to materialise hits"


def test_the_probe_uses_a_literal_vector_never_a_neural_query(probe):
    """A neural query round-trips through ML Commons.

    That would make a failure ambiguous between "the embedding model is down" and
    "the vector segments are corrupt" — which have opposite remedies.
    """
    fake = _FakeOpenSearch({"transcript_chunks": _ann_mapping()})
    probe(fake, "transcript_chunks")

    _index, body = fake.searched[0]
    assert "neural" not in str(body), "the probe must not depend on ML Commons"


# ---------------------------------------------------------------------------
# Over-reporting: the expensive direction
# ---------------------------------------------------------------------------
def test_an_empty_index_is_not_corrupt(probe):
    """A kNN query on an empty index succeeds identically to one on a populated index.

    So the hit count cannot distinguish them — only a doc count can. Reporting
    ``empty`` as corrupt would rebuild a freshly rebuilt index, forever.
    """
    fake = _FakeOpenSearch(
        {"transcript_chunks": _ann_mapping()}, doc_counts={"transcript_chunks": 0}
    )
    result = probe(fake, "transcript_chunks")

    assert result.status == "empty"
    assert result.is_corrupt is False
    assert result.is_serviceable is True


def test_a_knn_vector_field_without_an_ann_method_is_unsupported_not_corrupt(probe):
    """The legacy ``transcripts`` index, measured on the dev cluster.

    OpenSearch answers ``400 … Field 'embedding' is not built for ANN search`` — and
    that message carries ``search_phase_execution_exception``, which
    ``_is_index_corruption_error`` matches. Without this discrimination the health
    check rebuilds an intact index on every tick.
    """
    fake = _FakeOpenSearch(
        {"transcripts": _non_ann_mapping()},
        search_error=RequestError(
            400,
            "search_phase_execution_exception",
            "failed to create query: Field 'embedding' is not built for ANN search.",
        ),
    )
    result = probe(fake, "transcripts")

    assert result.status == "unsupported"
    assert result.is_corrupt is False
    assert result.is_serviceable is True


def test_a_non_ann_mapping_is_classified_before_any_query_is_issued(probe):
    """Cheaper and safer than relying on the error message: read the mapping first."""
    fake = _FakeOpenSearch({"transcripts": _non_ann_mapping()})
    result = probe(fake, "transcripts")

    assert result.status == "unsupported"
    assert fake.searched == [], "an index with no ANN graph must not be ANN-queried"


def test_an_index_with_no_embedding_field_is_unsupported(probe):
    fake = _FakeOpenSearch({"transcript_summaries": {"content": {"type": "text"}}})
    result = probe(fake, "transcript_summaries")

    assert result.status == "unsupported"
    assert result.is_corrupt is False


# ---------------------------------------------------------------------------
# ...but a 400 is not automatically benign
# ---------------------------------------------------------------------------
def test_a_closed_index_is_corrupt_even_though_it_answers_400(probe):
    """``index_closed_exception`` is a 400 and a real fault.

    This is why the benign-4xx branch is matched narrowly on the ANN-capability
    message rather than on the status class: a blanket "any 4xx is fine" reports a
    closed index as serviceable and no repair is ever attempted.
    """
    fake = _FakeOpenSearch(
        {"transcript_chunks": _ann_mapping()},
        search_error=RequestError(400, "index_closed_exception", "closed"),
    )
    result = probe(fake, "transcript_chunks")

    assert result.status == "corrupt"
    assert result.is_serviceable is False


def test_an_unrecognised_error_is_unknown_never_corrupt(probe):
    """Repair is destructive; an unrelated bug must not be able to trigger it."""
    fake = _FakeOpenSearch(
        {"transcript_chunks": _ann_mapping()},
        search_error=ValueError("something entirely unrelated"),
    )
    result = probe(fake, "transcript_chunks")

    assert result.status == "unknown"
    assert result.is_corrupt is False
    assert result.is_serviceable is False, "unknown is not evidence the plane works"


def test_a_missing_index_is_absent_and_not_serviceable(probe):
    fake = _FakeOpenSearch({})
    result = probe(fake, "transcript_chunks")

    assert result.status == "absent"
    assert result.is_corrupt is False
    assert result.is_serviceable is False


# ---------------------------------------------------------------------------
# Alias handling
# ---------------------------------------------------------------------------
def test_an_alias_resolves_to_its_concrete_index_for_the_mapping_read(probe):
    """``get_mapping(index=<alias>)`` is keyed by the CONCRETE index.

    ``check_and_repair_indices`` probes ``get_speaker_index()``, which is the
    ``speakers`` alias in this deployment — so a mapping read keyed by the requested
    name silently returns ``{}`` and the whole speaker plane is skipped.
    """
    fake = _FakeOpenSearch(
        {"speakers_v4": _ann_mapping(dimension=256)},
        aliases={"speakers": "speakers_v4"},
    )
    result = probe(fake, "speakers")

    assert result.status == "healthy"
    _index, body = fake.searched[0]
    assert len(body["query"]["knn"]["embedding"]["vector"]) == 256


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
def test_the_cached_probe_reuses_a_fresh_verdict(monkeypatch):
    """The status endpoint is polled, so the probe must not run per request."""
    from app.services.opensearch_service import client as os_client

    fake = _FakeOpenSearch({"transcript_chunks": _ann_mapping()})
    monkeypatch.setattr(os_client, "opensearch_client", fake)
    os_client.reset_knn_health_cache()

    first = os_client.probe_knn_health_cached("transcript_chunks")
    second = os_client.probe_knn_health_cached("transcript_chunks")

    assert first.status == "healthy"
    assert second.status == "healthy"
    assert len(fake.searched) == 1, "second read must come from the cache"


def test_the_cache_expires_rather_than_latching(monkeypatch):
    """A wall-clock TTL, not a verify-once-and-trust-forever flag.

    ``is_neural_pipeline_available`` uses the sticky-flag shape, and that shape IS
    the bug this issue describes: state assumed to be runtime truth.
    """
    from app.services.opensearch_service import client as os_client

    fake = _FakeOpenSearch({"transcript_chunks": _ann_mapping()})
    monkeypatch.setattr(os_client, "opensearch_client", fake)
    os_client.reset_knn_health_cache()

    os_client.probe_knn_health_cached("transcript_chunks", ttl=0.0)
    os_client.probe_knn_health_cached("transcript_chunks", ttl=0.0)

    assert len(fake.searched) == 2, "an expired verdict must be re-measured"


def test_resetting_the_cache_forces_a_fresh_measurement(monkeypatch):
    """Called after a repair, so the next read cannot replay the pre-repair verdict."""
    from app.services.opensearch_service import client as os_client

    fake = _FakeOpenSearch({"transcript_chunks": _ann_mapping()})
    monkeypatch.setattr(os_client, "opensearch_client", fake)
    os_client.reset_knn_health_cache()

    os_client.probe_knn_health_cached("transcript_chunks")
    os_client.reset_knn_health_cache()
    os_client.probe_knn_health_cached("transcript_chunks")

    assert len(fake.searched) == 2
