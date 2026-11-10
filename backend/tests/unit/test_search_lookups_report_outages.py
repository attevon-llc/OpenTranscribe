"""Search lookups: "could not look" must not read as "not found" / "unset" / zero."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from opensearchpy.exceptions import NotFoundError
from sqlalchemy.exc import OperationalError

from app.core.exceptions import ServiceUnavailableError
from app.services.search import model_downloader
from app.services.search import neural_bootstrap
from app.services.search import pipelines
from app.services.search import settings_service
from app.services.search.ml_model_service import OpenSearchMLModelService

# --- ingest / search pipeline lookups ---------------------------------------------


def test_ingest_pipeline_lookup_outage_is_not_reported_as_not_found():
    client = MagicMock()
    client.ingest.get_pipeline.side_effect = OSConnectionError("N/A", "down", Exception("down"))
    with (
        patch.object(pipelines._client, "opensearch_client", client),
        pytest.raises(OSConnectionError),
    ):
        pipelines._check_existing_pipeline_config("p", {"model_id": "m"})


def test_ingest_pipeline_lookup_404_is_still_not_found():
    client = MagicMock()
    client.ingest.get_pipeline.side_effect = NotFoundError(404, "missing", {})
    with patch.object(pipelines._client, "opensearch_client", client):
        assert pipelines._check_existing_pipeline_config("p", {"model_id": "m"}) is None


def test_search_pipeline_lookup_outage_does_not_attempt_creation():
    client = MagicMock()
    client.transport.perform_request.side_effect = OSConnectionError("N/A", "down", Exception("x"))
    with patch.object(pipelines._client, "opensearch_client", client):
        assert pipelines.ensure_search_pipeline_exists() is False
    methods = [c.args[0] for c in client.transport.perform_request.call_args_list]
    assert methods == ["GET"]  # no PUT on the strength of "I could not read it"


# --- settings reads ----------------------------------------------------------------


@contextmanager
def _broken_session():
    raise OperationalError("SELECT", {}, Exception("connection refused"))
    yield


def test_unreadable_setting_raises_instead_of_reading_as_unset():
    with (
        patch("app.db.session_utils.session_scope", _broken_session),
        pytest.raises(OperationalError),
    ):
        settings_service._get_setting("search.opensearch_model_id")


def test_unreadable_embedding_settings_raise_instead_of_returning_the_default_model():
    with (
        patch("app.db.session_utils.session_scope", _broken_session),
        pytest.raises(OperationalError),
    ):
        settings_service.get_search_embedding_settings()


# --- model registry ----------------------------------------------------------------


def test_find_model_by_name_raises_when_the_registry_cannot_be_read():
    service = OpenSearchMLModelService()
    client = MagicMock()
    client.transport.perform_request.side_effect = OSConnectionError("N/A", "down", Exception("x"))
    service._client = client
    with pytest.raises(ServiceUnavailableError):
        service.find_model_by_name("some/model")


def test_find_model_by_name_returns_none_when_registry_has_no_such_model():
    service = OpenSearchMLModelService()
    client = MagicMock()
    client.transport.perform_request.return_value = {"hits": {"hits": []}}
    service._client = client
    assert service.find_model_by_name("some/model") is None


def test_list_models_keeps_its_soft_empty_answer_for_polling_callers():
    service = OpenSearchMLModelService()
    client = MagicMock()
    client.transport.perform_request.side_effect = OSConnectionError("N/A", "down", Exception("x"))
    service._client = client
    assert service.list_models() == []


# --- model manifest ----------------------------------------------------------------


def test_unreadable_manifest_is_left_untouched_not_rewritten_without_the_other_models(tmp_path):
    manifest = tmp_path / "model_manifest.json"
    manifest.write_text(json.dumps({"models": [{"name": "other"}]}))
    before = manifest.read_text()
    opened: list[tuple] = []

    def failing_open(*args, **kwargs):
        opened.append(args)
        raise PermissionError("denied")

    with patch("builtins.open", failing_open):
        model_downloader._update_manifest(
            tmp_path, "new/model", {"short_name": "n", "version": "1", "dimension": 384}
        )

    assert len(opened) == 1  # the read; no rewrite was attempted from an empty manifest
    assert manifest.read_text() == before


def test_valid_manifest_keeps_other_entries_when_a_model_is_added(tmp_path):
    manifest = tmp_path / "model_manifest.json"
    manifest.write_text(json.dumps({"models": [{"name": "other"}]}))

    model_downloader._update_manifest(
        tmp_path, "new/model", {"short_name": "n", "version": "1", "dimension": 384}
    )

    names = [m["name"] for m in json.loads(manifest.read_text())["models"]]
    assert names == ["other", "new/model"]


# --- bootstrap report --------------------------------------------------------------


def test_text_only_count_is_none_when_the_cluster_cannot_be_asked():
    client = MagicMock()
    client.indices.exists.side_effect = OSConnectionError("N/A", "down", Exception("x"))
    with patch("app.services.opensearch_service.opensearch_client", client):
        assert neural_bootstrap._text_only_chunk_files_count() is None


def test_text_only_count_is_zero_when_the_index_is_absent():
    client = MagicMock()
    client.indices.exists.return_value = False
    with patch("app.services.opensearch_service.opensearch_client", client):
        assert neural_bootstrap._text_only_chunk_files_count() == 0
