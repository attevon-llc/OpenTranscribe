"""The kNN health probe, against a real cluster (issue #540).

The unit suite drives the probe's decision tree with a fake client, which pins the
*logic* and cannot pin the *shapes*: whether OpenSearch really rejects an ANN query
on a method-less ``knn_vector`` field with a message containing
``search_phase_execution_exception``, what a closed index actually answers, and what
an empty ANN index returns. Every one of those was established by measurement here,
and each is what a version bump could silently change.

This module owns its index. It creates ``knn_probe_live_test``, drives it through
healthy → empty → closed → reopened, and deletes it in teardown. It never touches
``transcript_chunks`` or any speaker index: the whole point of the probe is that a
false ``corrupt`` triggers a destructive rebuild, so the test for it must not be
capable of provoking one against real data.

    pytest backend/tests/integration/test_knn_probe_live.py -m integration
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.services.opensearch_service import get_opensearch_client
from app.services.opensearch_service import probe_knn_health

pytestmark = pytest.mark.integration

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

PROBE_INDEX = "knn_probe_live_test"
DIMENSION = 8


def _ann_index_body() -> dict[str, Any]:
    return {
        "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "embedding": {
                    "type": "knn_vector",
                    "dimension": DIMENSION,
                    "method": {
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "name": "hnsw",
                        "parameters": {"ef_construction": 128, "m": 16},
                    },
                }
            }
        },
    }


def _non_ann_index_body() -> dict[str, Any]:
    """A ``knn_vector`` field with NO method — the legacy ``transcripts`` shape."""
    return {
        "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {"properties": {"embedding": {"type": "knn_vector", "dimension": DIMENSION}}},
    }


@pytest.fixture
def client():
    if _OPENSEARCH_ABSENT:
        pytest.skip("OpenSearch not reachable (SKIP_OPENSEARCH)")
    os_client = get_opensearch_client()
    if os_client is None:
        pytest.skip("OpenSearch client unavailable")
    return os_client


@pytest.fixture
def ann_index(client):
    """A real ANN index of our own, removed however the test ends."""
    client.indices.delete(index=PROBE_INDEX, ignore=[404])
    client.indices.create(index=PROBE_INDEX, body=_ann_index_body())
    try:
        yield PROBE_INDEX
    finally:
        client.indices.open(index=PROBE_INDEX, ignore=[400, 404])
        client.indices.delete(index=PROBE_INDEX, ignore=[404])


def test_an_empty_ann_index_reports_empty_not_corrupt(ann_index):
    """Measured, not assumed: an empty ANN index answers a kNN query successfully.

    So the hit count cannot separate empty from populated — only a doc count can.
    Getting this wrong rebuilds a freshly rebuilt index on every health tick.
    """
    result = probe_knn_health(ann_index)

    assert result.status == "empty"
    assert result.is_corrupt is False
    assert result.is_serviceable is True


def test_a_populated_ann_index_reports_healthy(client, ann_index):
    client.index(
        index=ann_index,
        id="1",
        body={"embedding": [1.0] + [0.0] * (DIMENSION - 1)},
        refresh=True,
    )

    result = probe_knn_health(ann_index)

    assert result.status == "healthy"
    assert result.is_serviceable is True
    assert result.latency_ms is not None and result.latency_ms > 0


def test_a_closed_index_reports_corrupt(client, ann_index):
    """``index_closed_exception`` is a **400**, and it is a genuine fault.

    This is the case that forbids a blanket "any 4xx is benign" rule — which is
    exactly what the first version of this probe did, reporting a closed index as
    serviceable so no repair was ever attempted.
    """
    client.index(
        index=ann_index,
        id="1",
        body={"embedding": [1.0] + [0.0] * (DIMENSION - 1)},
        refresh=True,
    )
    client.indices.close(index=ann_index)

    result = probe_knn_health(ann_index)

    assert result.status == "corrupt"
    assert result.is_serviceable is False


def test_reopening_the_index_restores_a_healthy_verdict(client, ann_index):
    """The control: the corrupt verdict above is caused by the close, nothing else."""
    client.index(
        index=ann_index,
        id="1",
        body={"embedding": [1.0] + [0.0] * (DIMENSION - 1)},
        refresh=True,
    )
    client.indices.close(index=ann_index)
    assert probe_knn_health(ann_index).status == "corrupt"

    client.indices.open(index=ann_index)
    # opensearch-py wants a NUMBER here; the "30s" duration string that the REST
    # API accepts is rejected client-side by urllib3.
    client.cluster.health(index=ann_index, wait_for_status="yellow", timeout=30)

    assert probe_knn_health(ann_index).status == "healthy"


def test_a_method_less_knn_vector_field_reports_unsupported(client):
    """The legacy ``transcripts`` shape, reproduced from scratch.

    OpenSearch answers ``400 … not built for ANN search``, and that message carries
    ``search_phase_execution_exception`` — which ``_is_index_corruption_error``
    matches. If this ever stopped being classified ``unsupported``, the health check
    would delete and re-embed an intact index on every tick.
    """
    index = f"{PROBE_INDEX}_non_ann"
    client.indices.delete(index=index, ignore=[404])
    client.indices.create(index=index, body=_non_ann_index_body())
    try:
        client.index(
            index=index,
            id="1",
            body={"embedding": [1.0] + [0.0] * (DIMENSION - 1)},
            refresh=True,
        )
        result = probe_knn_health(index)

        assert result.status == "unsupported"
        assert result.is_corrupt is False
        assert result.is_serviceable is True
    finally:
        client.indices.delete(index=index, ignore=[404])


def test_the_probe_is_cheap_enough_to_run_on_a_health_tick(client, ann_index):
    """It runs on every 10-minute health check and behind a polled status endpoint."""
    for i in range(20):
        # Never a zero vector: `cosinesimil` rejects one outright
        # ("zero vector is not supported when space type is [cosinesimil]"),
        # which is also why the probe itself sends a unit vector.
        client.index(
            index=ann_index,
            id=str(i),
            body={"embedding": [float(i + 1)] + [0.0] * (DIMENSION - 1)},
        )
    client.indices.refresh(index=ann_index)

    result = probe_knn_health(ann_index)

    assert result.status == "healthy"
    assert result.latency_ms is not None
    assert result.latency_ms < 1000, f"probe took {result.latency_ms:.1f} ms"
