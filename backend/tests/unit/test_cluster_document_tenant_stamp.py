"""A rewritten cluster centroid keeps the organization it belongs to.

``update_cluster_embedding`` replaces the whole OpenSearch document. Every tenant-scoped read
(`find_matching_clusters`, the speaker org filter) selects on ``organization_id``, and a
document without the field reads as personal, so a rewrite that dropped the stamp moved an
organization's cluster out of its tenant's results. A recording client stands in for the
cluster, and both the stored document and the filter a reader uses are asserted.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.constants import PYANNOTE_EMBEDDING_DIMENSION_V4
from app.services.opensearch_service import clusters

V4 = [0.01] * PYANNOTE_EMBEDDING_DIMENSION_V4


@pytest.fixture
def os_client(monkeypatch, privacy_allows_all):
    from app.services.opensearch_service import client as _client

    fake = MagicMock(name="opensearch")
    monkeypatch.setattr(_client, "opensearch_client", fake)
    monkeypatch.setattr(clusters, "ensure_indices_exist", lambda *a, **k: None)
    monkeypatch.setattr(clusters, "get_active_speaker_index", lambda: "speakers")
    monkeypatch.setattr(clusters, "query_embedding_dimension_mismatch", lambda *a, **k: False)
    monkeypatch.setattr(clusters, "assert_voiceprint_write_allowed", lambda *a, **k: True)
    return fake


def _stored(os_client, source: dict) -> dict[str, Any]:
    os_client.get.return_value = {"_source": source}
    assert clusters.update_cluster_embedding("c-1", V4) is True
    body: dict[str, Any] = os_client.index.call_args.kwargs["body"]
    return body


def test_an_organization_cluster_keeps_its_organization_when_rewritten(os_client):
    body = _stored(os_client, {"user_id": 7, "label": "Weekly sync", "organization_id": 42})

    assert body["organization_id"] == 42
    assert body["user_id"] == 7
    assert body["label"] == "Weekly sync"


def test_a_personal_cluster_stays_unstamped_when_rewritten(os_client):
    body = _stored(os_client, {"user_id": 7, "label": "Mine"})

    assert "organization_id" not in body


def _matches(doc: dict, clauses: list[dict]) -> bool:
    """The two clause shapes ``org_filter_clauses`` emits, evaluated against a document."""
    for clause in clauses:
        if "term" in clause:
            ((field, wanted),) = clause["term"].items()
            if doc.get(field) != wanted:
                return False
        else:
            field = clause["bool"]["must_not"]["exists"]["field"]
            if field in doc:
                return False
    return True


def test_a_rewritten_organization_cluster_is_found_by_its_tenant_and_not_as_personal(os_client):
    from app.services.opensearch_service.client import _speaker_org_filter_clauses

    body = _stored(os_client, {"user_id": 7, "organization_id": 42})

    assert _matches(body, _speaker_org_filter_clauses(42))
    assert not _matches(body, _speaker_org_filter_clauses(None))
