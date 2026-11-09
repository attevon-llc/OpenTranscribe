"""An OpenSearch or Postgres outage during the integrity sweep must be reported, not read as clean."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from unittest.mock import patch

from opensearchpy.exceptions import ConnectionError as OSConnectionError
from sqlalchemy.exc import OperationalError

from app.tasks import opensearch_integrity_task as sweeper

_UUID = "11111111-1111-1111-1111-111111111111"
_OTHER = "22222222-2222-2222-2222-222222222222"


def _down() -> OSConnectionError:
    return OSConnectionError("N/A", "down", Exception("down"))


def _client() -> MagicMock:
    client = MagicMock()
    client.indices.exists.return_value = True
    client.count.return_value = {"count": 10}
    client.search.return_value = {
        "aggregations": {"file_ids": {"buckets": [{"key": _OTHER, "doc_count": 1}]}}
    }
    client.delete_by_query.return_value = {"deleted": 1}
    return client


def test_failed_aggregation_is_reported_with_traceback(caplog):
    client = _client()
    client.search.side_effect = _down()
    with caplog.at_level(logging.ERROR):
        result = sweeper._cleanup_index_by_field(client, "idx", "file_uuid", {_UUID}, str)
    assert result["error"] is not None
    assert result["orphaned_docs"] == 0
    assert any(r.exc_info for r in caplog.records)


def test_failed_delete_batch_is_counted_not_silently_dropped(caplog):
    client = _client()
    client.delete_by_query.side_effect = _down()
    with caplog.at_level(logging.ERROR):
        result = sweeper._cleanup_index_by_field(
            client, "idx", "file_uuid", {_UUID}, str, force=True
        )
    assert result["failed_delete_batches"] == 1
    assert result["deleted_docs"] == 0
    assert result["error"]
    assert any(r.exc_info for r in caplog.records)


def test_a_clean_sweep_reports_no_error():
    result = sweeper._cleanup_index_by_field(
        _client(), "idx", "file_uuid", {_UUID}, str, force=True
    )
    assert result["error"] is None
    assert result["failed_delete_batches"] == 0


def test_run_summary_names_the_indices_that_could_not_be_swept():
    client = _client()
    client.search.side_effect = _down()
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch.object(sweeper, "_get_all_file_ids_from_db", return_value={1}),
        patch.object(sweeper, "_get_all_file_uuids_from_db", return_value={_UUID}),
        patch.object(sweeper, "_get_all_speaker_uuids_from_db", return_value={_UUID}),
        patch.object(sweeper, "_notify"),
    ):
        results = sweeper.run_orphan_cleanup(dry_run=True)
    failed = results["summary"]["failed_indices"]
    assert len(failed) == 5
    assert all("aggregation failed" in why for why in failed.values())


def test_integrity_counts_flag_unavailable_postgres_counts(caplog):
    client = _client()
    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        patch(
            "app.db.session_utils.session_scope",
            side_effect=OperationalError("select", {}, Exception("pg down")),
        ),
        caplog.at_level(logging.ERROR),
    ):
        status = sweeper.get_index_overview()
    assert status["pg_stats"] == {}
    assert status["pg_stats_error"]
    assert any(r.exc_info for r in caplog.records)
