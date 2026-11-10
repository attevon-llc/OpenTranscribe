"""An OpenSearch outage that reaches a route is answered 503, with no cluster text in the body.

``OpenSearchUnavailableError`` used to derive from ``RuntimeError`` only, so the application
error handler never saw it and the client got a generic 500 (analyze-outliers, which reads
embeddings with ``strict=True``). The cluster's own error text can quote hosts and users, so the
raise sites carry fixed messages and keep the original on ``__cause__``.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import status
from opensearchpy.exceptions import ConnectionError as OSConnectionError

from app.services import opensearch_service
from app.services.opensearch_service import client as os_client
from app.services.opensearch_service import speaker_read
from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.speaker_clustering_service import SpeakerClusteringService

LEAK = "https://opensearch.internal:9200 returned security_exception for user admin"


def _outage_from_the_real_reader(monkeypatch) -> OpenSearchUnavailableError:
    broken = MagicMock()
    broken.mget.side_effect = OSConnectionError("N/A", LEAK, Exception(LEAK))
    monkeypatch.setattr(os_client, "opensearch_client", broken)
    monkeypatch.setattr(speaker_read, "ensure_indices_exist", lambda: None)
    monkeypatch.setattr(speaker_read, "get_active_speaker_index", lambda: "speakers")
    with pytest.raises(OpenSearchUnavailableError) as raised:
        opensearch_service.get_speaker_embeddings_batch(["a-uuid"], strict=True)
    return raised.value


def test_outage_message_is_fixed_and_the_cause_is_kept(monkeypatch):
    err = _outage_from_the_real_reader(monkeypatch)

    assert LEAK not in err.message
    assert LEAK not in str(err)
    assert isinstance(err.__cause__, OSConnectionError)
    assert isinstance(err, RuntimeError)


def test_analyze_outliers_answers_503_when_the_index_is_down(
    client, user_token_headers, monkeypatch
):
    err = _outage_from_the_real_reader(monkeypatch)

    def _outage(self, *args, **kwargs):
        raise err

    monkeypatch.setattr(SpeakerClusteringService, "analyze_gender_outliers", _outage)

    resp = client.post(
        f"/api/speaker-clusters/{uuid.uuid4()}/analyze-outliers", headers=user_token_headers
    )

    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert resp.json()["detail"] == err.message
    assert LEAK not in resp.text
