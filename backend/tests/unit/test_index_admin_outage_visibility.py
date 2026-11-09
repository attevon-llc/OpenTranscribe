"""Outage visibility for the chunks-index lifecycle handlers (broad-except lane B9).

An OpenSearch outage must be logged with its traceback and reported through the
function's return value; an error that is NOT an OpenSearch error (a bug) must
propagate instead of being flattened into the same "failed" answer.
"""

import logging
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError

import app.services.search.index_admin as svc

LOGGER = "app.services.search.index_admin"


def _failing_client(method_path: str, exc: Exception) -> MagicMock:
    client = MagicMock()
    client.indices.exists.return_value = False
    client.indices.exists_alias.return_value = False
    target = client
    for part in method_path.split("."):
        target = getattr(target, part)
    target.side_effect = exc
    return client


def _tracebacks(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and r.exc_info]


def test_ensure_chunks_index_reports_outage_with_traceback(caplog):
    client = _failing_client(
        "indices.create", OpenSearchConnectionError("N/A", "down", Exception())
    )
    with (
        patch.object(svc._client, "opensearch_client", client),
        patch(
            "app.services.search.settings_service.get_search_embedding_dimension",
            return_value=384,
        ),
        caplog.at_level(logging.DEBUG, logger=LOGGER),
    ):
        assert svc.ensure_chunks_index_exists() is False
    assert _tracebacks(caplog)


def test_ensure_chunks_index_does_not_swallow_a_programming_error():
    client = _failing_client("indices.create", KeyError("bug"))
    with (
        patch.object(svc._client, "opensearch_client", client),
        patch(
            "app.services.search.settings_service.get_search_embedding_dimension",
            return_value=384,
        ),
        pytest.raises(KeyError),
    ):
        svc.ensure_chunks_index_exists()


def test_recreate_index_reports_outage_with_traceback(caplog):
    client = _failing_client(
        "indices.create", OpenSearchConnectionError("N/A", "down", Exception())
    )
    with (
        patch.object(svc._client, "opensearch_client", client),
        caplog.at_level(logging.DEBUG, logger=LOGGER),
    ):
        assert svc.recreate_index_for_dimension(384) is False
    assert _tracebacks(caplog)


def test_version_check_logs_outage_with_traceback(caplog):
    client = _failing_client(
        "indices.get_mapping", OpenSearchConnectionError("N/A", "down", Exception())
    )
    with (
        patch.object(svc._client, "opensearch_client", client),
        caplog.at_level(logging.DEBUG, logger=LOGGER),
    ):
        svc._check_index_version("idx")
    assert _tracebacks(caplog)


def test_additive_steps_log_outage_with_traceback(caplog):
    client = _failing_client(
        "indices.get_mapping", OpenSearchConnectionError("N/A", "down", Exception())
    )
    with (
        patch.object(svc._client, "opensearch_client", client),
        caplog.at_level(logging.DEBUG, logger=LOGGER),
    ):
        svc._apply_pending_additive_steps("idx")
    assert _tracebacks(caplog)


def test_survey_logs_outage_with_traceback_and_stays_unavailable(caplog):
    client = _failing_client("search", OpenSearchConnectionError("N/A", "down", Exception()))
    client.indices.exists.return_value = True
    with (
        patch.object(svc._client, "opensearch_client", client),
        caplog.at_level(logging.DEBUG, logger=LOGGER),
    ):
        result = svc.survey_speaker_id_coverage()
    assert result["verdict"] == "unavailable"
    assert _tracebacks(caplog)
