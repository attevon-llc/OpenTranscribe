"""Search task outages must be visible, not read as "covered" or "nothing to do"."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from unittest.mock import patch

from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError


def test_speaker_id_backfill_survey_outage_is_not_reported_as_fully_covered(caplog):
    from app.tasks.search_indexing_task import backfill_speaker_id_fields_task

    client = MagicMock()
    client.indices.exists.return_value = True
    client.search.side_effect = OpenSearchConnectionError("N/A", "down", Exception("down"))

    with (
        patch("app.services.opensearch_service.get_opensearch_client", return_value=client),
        caplog.at_level(logging.ERROR, logger="app.tasks.search_indexing_task"),
    ):
        result = backfill_speaker_id_fields_task.run(limit=10)

    assert result == {"status": "skipped", "reason": "opensearch_error"}
    survey = [r for r in caplog.records if "candidate survey failed" in r.getMessage()]
    assert survey and survey[0].exc_info is not None


def test_indexed_uuid_lookup_outage_returns_none_with_traceback(caplog):
    from app.tasks import search_maintenance_task as mod

    client = MagicMock()
    client.indices.exists.return_value = True
    client.search.side_effect = OpenSearchConnectionError("N/A", "down", Exception("down"))

    with (
        patch("app.services.opensearch_service.opensearch_client", client),
        caplog.at_level(logging.WARNING, logger=mod.logger.name),
    ):
        assert mod._get_indexed_uuids() is None

    failed = [r for r in caplog.records if "Could not check indexed files" in r.getMessage()]
    assert failed and failed[0].exc_info is not None


def test_reindex_dispatch_failures_are_counted_and_logged_with_traceback(caplog):
    from kombu.exceptions import OperationalError

    from app.tasks import search_maintenance_task as mod

    with (
        patch("app.tasks.reindex_task.reindex_transcripts_task") as task,
        caplog.at_level(logging.ERROR, logger=mod.logger.name),
    ):
        task.delay.side_effect = OperationalError("broker down")
        failed = mod._dispatch_reindex_tasks({1: ["a"], 2: ["b"]})

    assert failed == 2
    records = [r for r in caplog.records if "Failed to dispatch reindex" in r.getMessage()]
    assert len(records) == 2 and all(r.exc_info is not None for r in records)
