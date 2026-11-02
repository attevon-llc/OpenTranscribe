"""The speaker-to-speaker kNN lookup sends the tenant filter of the scope it resolved.

``find_and_store_speaker_matches`` builds its query with
``match_storage.build_speaker_match_query``. These tests capture the body it sends and
require the organization clauses for the resolved scope, so dropping ``organization_id``
from the call site, or the clauses from the builder, fails here. The personal case also
requires the owner's organization-file exclusion, which only appears when the builder
passes ``user_ids``.

The scope resolution itself (file org, privacy switch) is covered against Postgres in
``tests/test_speaker_tenant_gating.py`` and ``test_speaker_matching_policy.py``; here it is
stubbed so the assertion is only about what reaches OpenSearch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

USER_ID = 31
SPEAKER_ID = 77
OWNER_ORG_FILES = [901, 902]


class _RecordingClient:
    def __init__(self) -> None:
        self.searches: list[tuple[str, dict[str, Any]]] = []

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.searches.append((index, body))
        return {"hits": {"hits": []}}


@pytest.fixture
def client(monkeypatch) -> _RecordingClient:
    from app.services.opensearch_service import client as os_client

    recorder = _RecordingClient()
    monkeypatch.setattr(os_client, "opensearch_client", recorder)
    monkeypatch.setattr(os_client, "_org_file_ids_of", lambda _user_ids: list(OWNER_ORG_FILES))
    return recorder


def _service():
    from app.services.speaker_matching_service import SpeakerMatchingService

    return SpeakerMatchingService(MagicMock(name="db"), None)


def _knn(body: dict[str, Any]) -> dict[str, Any]:
    knn: dict[str, Any] = body["query"]["knn"]["embedding"]
    return knn


def _expected_tenant_clauses(organization_id: int | None) -> list[dict[str, Any]]:
    from app.services.opensearch_service.client import _speaker_org_filter_clauses

    return _speaker_org_filter_clauses(organization_id, user_ids=[USER_ID])


def _assert_tenant_filter(body: dict[str, Any], organization_id: int | None) -> None:
    filters = _knn(body)["filter"]["bool"]["filter"]
    assert {"term": {"user_id": USER_ID}} in filters
    expected = _expected_tenant_clauses(organization_id)
    assert expected, "the tenant helper returned no clauses; this test would prove nothing"
    for clause in expected:
        assert clause in filters, (clause, filters)
    if organization_id is None:
        assert {"bool": {"must_not": {"terms": {"media_file_id": OWNER_ORG_FILES}}}} in filters
        assert not any("organization_id" in f.get("term", {}) for f in filters)
    exclusions = [f["bool"]["must_not"] for f in filters if "must_not" in f.get("bool", {})]
    assert any({"term": {"speaker_id": SPEAKER_ID}} in m for m in exclusions if isinstance(m, list))


@pytest.mark.parametrize("organization_id", [42, None])
def test_stored_match_lookup_sends_the_resolved_tenant_filter(client, monkeypatch, organization_id):
    from app.services.speaker_matching import match_storage

    service = _service()
    monkeypatch.setattr(
        service, "_scope_gate", lambda _user, _org, speaker_id=None: (True, organization_id)
    )
    monkeypatch.setattr(match_storage, "find_matching_speaker", lambda *a, **k: [{"id": 1}])

    service.find_and_store_speaker_matches(
        SPEAKER_ID, np.ones(4, dtype=np.float32), USER_ID, organization_id=organization_id
    )

    assert len(client.searches) == 1
    _index, body = client.searches[0]
    _assert_tenant_filter(body, organization_id)
    assert body["size"] == _knn(body)["k"] == 20
