"""An OpenSearch outage must not read as zero documents / nothing migrated."""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from sqlalchemy.exc import OperationalError

from app.tasks import embedding_migration_v4_scope as scope
from app.tasks import opensearch_integrity_task as integrity


def _pg_down() -> OperationalError:
    return OperationalError("select", {}, Exception("no db"))


def _down() -> OSConnectionError:
    return OSConnectionError("N/A", "down", Exception("down"))


def test_already_migrated_ids_raise_instead_of_returning_an_empty_set():
    client = MagicMock()
    client.indices.exists.return_value = True
    client.search.side_effect = _down()
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        pytest.raises(OSConnectionError),
    ):
        scope._get_already_migrated_file_ids()


def test_migration_status_reports_error_when_a_document_count_fails():
    client = MagicMock()
    client.indices.exists.return_value = True
    client.count.side_effect = _down()
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch.object(scope.EmbeddingModeService, "detect_mode", return_value="v3"),
    ):
        status = scope.get_migration_status()
    assert status["status"] == "error"
    assert "v3_document_count" not in status


def test_migration_status_reports_counts_when_opensearch_answers():
    client = MagicMock()
    client.indices.exists.return_value = True
    client.count.return_value = {"count": 12}
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch.object(scope.EmbeddingModeService, "detect_mode", return_value="v3"),
    ):
        status = scope.get_migration_status()
    assert status["v3_document_count"] == 12
    assert status["v4_document_count"] == 12


def test_index_overview_marks_failed_counts_instead_of_reporting_zero():
    client = MagicMock()
    client.indices.exists.return_value = True
    client.count.side_effect = _down()
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch("app.db.session_utils.session_scope", side_effect=_pg_down()),
    ):
        overview = integrity.get_index_overview()
    assert len(overview["indices"]) == 4
    assert all(entry.get("error") for entry in overview["indices"])
    assert all(entry.get("total") is None for entry in overview["indices"])


def test_index_overview_still_reports_real_zero_counts():
    client = MagicMock()
    client.indices.exists.return_value = True
    client.count.return_value = {"count": 0}
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch("app.db.session_utils.session_scope", side_effect=_pg_down()),
    ):
        overview = integrity.get_index_overview()
    assert len(overview["indices"]) == 4
    assert all("error" not in e for e in overview["indices"])
    assert all(e["total"] == 0 for e in overview["indices"])
