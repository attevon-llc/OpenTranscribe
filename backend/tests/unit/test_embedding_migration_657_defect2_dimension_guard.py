"""Read-side dimension guard for kNN speaker/cluster searches (issue #657, defect 2).

Before this fix there was NO dimension guard on any read path — only writes
(``speaker_write.py``) refused a mismatched vector. Mid-migration, a query
embedding of the wrong dimension for the active index was simply handed to
OpenSearch, which raised internally; every call site swallowed the exception
and returned an empty result (``[]``/``None``/``False``) indistinguishable
from "no match found". These tests pin the new
``query_embedding_dimension_mismatch`` guard and its use at each read call
site enumerated in the issue.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.opensearch_service import client as client_mod
from app.services.opensearch_service import clusters as clusters_mod
from app.services.opensearch_service import matching as matching_mod


@pytest.mark.unit
class TestQueryEmbeddingDimensionMismatchDetector:
    def test_mismatch_detected_and_logged(self, monkeypatch, caplog):
        monkeypatch.setattr(client_mod, "opensearch_client", MagicMock())
        with patch.object(client_mod, "_resolve_concrete_index", return_value="speakers_v4"):
            with patch.object(client_mod, "_get_index_embedding_dimension", return_value=256):
                with caplog.at_level("ERROR"):
                    result = client_mod.query_embedding_dimension_mismatch("speakers", [0.1] * 512)
        assert result is True
        assert any("EMBEDDING_DIMENSION_MISMATCH" in r.message for r in caplog.records)

    def test_matching_dimension_is_not_a_mismatch(self):
        with patch.object(client_mod, "_resolve_concrete_index", return_value="speakers_v4"):
            with patch.object(client_mod, "_get_index_embedding_dimension", return_value=256):
                assert (
                    client_mod.query_embedding_dimension_mismatch("speakers", [0.1] * 256) is False
                )

    def test_unknown_dimension_fails_open_on_detection(self):
        """An unanswerable mapping check must not itself refuse a legitimate query."""
        with patch.object(client_mod, "_resolve_concrete_index", return_value="speakers_v4"):
            with patch.object(client_mod, "_get_index_embedding_dimension", return_value=None):
                assert (
                    client_mod.query_embedding_dimension_mismatch("speakers", [0.1] * 256) is False
                )

    def test_empty_embedding_is_not_a_mismatch(self):
        assert client_mod.query_embedding_dimension_mismatch("speakers", []) is False


@pytest.mark.unit
class TestFindMatchingSpeakerRefusesMismatchedQuery:
    def test_returns_none_instead_of_hitting_opensearch(self, monkeypatch):
        mock_client = MagicMock()
        monkeypatch.setattr(matching_mod._client, "opensearch_client", mock_client)
        monkeypatch.setattr(matching_mod, "get_active_speaker_index", lambda: "speakers_v4")
        monkeypatch.setattr(matching_mod, "ensure_indices_exist", lambda: None)
        with patch.object(matching_mod, "query_embedding_dimension_mismatch", return_value=True):
            result = matching_mod.find_matching_speaker([0.1] * 512, user_id=1)
        assert result is None
        # The mismatched query must never reach OpenSearch at all.
        mock_client.search.assert_not_called()


@pytest.mark.unit
class TestFindMatchingClustersRefusesMismatchedQuery:
    def test_returns_empty_list_without_querying(self, monkeypatch):
        mock_client = MagicMock()
        monkeypatch.setattr(clusters_mod._client, "opensearch_client", mock_client)
        monkeypatch.setattr(clusters_mod, "get_active_speaker_index", lambda: "speakers_v4")
        with patch.object(clusters_mod, "query_embedding_dimension_mismatch", return_value=True):
            result = clusters_mod.find_matching_clusters([0.1] * 512, user_id=1)
        assert result == []
        mock_client.search.assert_not_called()


@pytest.mark.unit
class TestStoreClusterEmbeddingRefusesMismatchedWrite:
    def test_returns_false_and_does_not_index(self, monkeypatch):
        mock_client = MagicMock()
        monkeypatch.setattr(clusters_mod._client, "opensearch_client", mock_client)
        monkeypatch.setattr(clusters_mod, "get_active_speaker_index", lambda: "speakers_v4")
        monkeypatch.setattr(clusters_mod, "_indices_verified", True)
        with patch.object(clusters_mod, "query_embedding_dimension_mismatch", return_value=True):
            ok = clusters_mod.store_cluster_embedding("uuid-1", user_id=1, embedding=[0.1] * 512)
        assert ok is False
        mock_client.index.assert_not_called()
