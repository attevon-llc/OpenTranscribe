"""Speaker task outages must be recorded where an operator sees them, with a traceback."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from unittest.mock import patch

from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from sqlalchemy.exc import OperationalError


def _os_down() -> OpenSearchConnectionError:
    return OpenSearchConnectionError("N/A", "down", Exception("down"))


def test_index_existence_outage_is_recorded_in_the_normalization_summary(caplog):
    from app.tasks import speaker_embedding_migration as mod

    client = MagicMock()
    client.indices.exists.side_effect = _os_down()
    summary: dict = {}

    with caplog.at_level(logging.ERROR, logger=mod.logger.name):
        mod._normalize_index(client, "speakers_v4", 100, summary)

    assert summary["errors"] and "speakers_v4" in summary["errors"][0]
    rec = [r for r in caplog.records if "Error checking index" in r.getMessage()]
    assert rec and rec[0].exc_info is not None


def test_active_speaker_index_lookup_outage_is_recorded_in_the_summary(caplog):
    from app.tasks import speaker_embedding_migration as mod

    with (
        patch.object(mod, "get_opensearch_client", return_value=MagicMock()),
        patch.object(mod, "_normalize_index"),
        patch(
            "app.services.opensearch_service.get_active_speaker_index",
            side_effect=_os_down(),
        ),
        caplog.at_level(logging.WARNING, logger=mod.logger.name),
    ):
        summary = mod._run_normalize_embeddings(100)

    assert any("active speaker index" in e for e in summary["errors"])
    rec = [r for r in caplog.records if "active speaker index" in r.getMessage()]
    assert rec and rec[0].exc_info is not None


def test_speaker_uuid_query_outage_returns_none_with_traceback(caplog):
    from app.tasks import speaker_embedding_consistency as mod

    client = MagicMock()
    client.indices.exists.return_value = True
    client.search.side_effect = _os_down()

    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        caplog.at_level(logging.WARNING, logger=mod.logger.name),
    ):
        assert mod._get_opensearch_speaker_uuids("speakers") is None

    rec = [r for r in caplog.records if "Failed to query speaker UUIDs" in r.getMessage()]
    assert rec and rec[0].exc_info is not None


def test_predicted_check_db_outage_runs_detection_and_logs_traceback(caplog):
    from app.tasks import speaker_attribute_task as mod

    with (
        patch.object(mod, "session_scope", side_effect=OperationalError("s", {}, Exception("x"))),
        caplog.at_level(logging.WARNING, logger=mod.logger.name),
    ):
        assert mod._attributes_already_predicted("u-1") is False

    rec = [r for r in caplog.records if "already-predicted check failed" in r.getMessage()]
    assert rec and rec[0].exc_info is not None


def test_non_database_error_in_task_tracking_is_not_swallowed():
    from app.tasks import speaker_attribute_task as mod

    boom = RuntimeError("programming error, not an outage")
    with patch.object(mod, "session_scope", side_effect=boom):
        try:
            mod._update_attr_task("t-1", "completed")
        except RuntimeError as exc:
            assert exc is boom
        else:
            raise AssertionError("a non-database error must propagate")
