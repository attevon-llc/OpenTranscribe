"""Backfill and media task outages must fail or be recorded, never read as a zero count."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from kombu.exceptions import OperationalError as BrokerUnavailableError
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError


def _os_down() -> OpenSearchConnectionError:
    return OpenSearchConnectionError("N/A", "down", Exception("down"))


def test_speaker_backfill_outage_raises_instead_of_reporting_zero_updated(caplog):
    from app.tasks import tenant_backfill_task as mod

    with (
        patch(
            "app.services.opensearch_service.update_by_query.update_by_query",
            side_effect=_os_down(),
        ),
        caplog.at_level(logging.ERROR, logger=mod.logger.name),
        pytest.raises(OpenSearchConnectionError),
    ):
        mod._update_by_query(MagicMock(), "speakers", {}, {}, "profile")

    rec = [r for r in caplog.records if "Speaker backfill" in r.getMessage()]
    assert rec and rec[0].exc_info is not None


def test_file_uuid_backfill_with_a_failed_org_fails_the_run():
    from app.tasks import tenant_backfill_task as mod

    client = MagicMock()
    client.indices.exists.return_value = True
    client.update_by_query.side_effect = _os_down()

    with pytest.raises(mod.TenantBackfillError, match=r"\[7\]"):
        mod._backfill_file_uuid_docs(client, "chunks", {7: ["f-1"]})


def test_speaker_index_existence_outage_is_not_skipped_silently():
    from app.tasks import tenant_backfill_task as mod

    client = MagicMock()
    client.indices.exists.side_effect = _os_down()

    with (
        patch(
            "app.services.opensearch_service.speaker_maintenance.speaker_embedding_indices",
            return_value=["speakers_v3"],
        ),
        pytest.raises(OpenSearchConnectionError),
    ):
        mod._speaker_indices(client)


def test_failed_summary_reset_returns_false_with_traceback(caplog):
    from app.tasks import summary_retry as mod

    media_file = MagicMock(status="completed", id=3)
    db = MagicMock()
    db.commit.side_effect = BrokerUnavailableError("broker down")

    with (
        patch("app.services.uuid_helpers.get_by_uuid_optional", return_value=media_file),
        caplog.at_level(logging.ERROR, logger=mod.logger.name),
    ):
        assert mod.reset_summary_for_retry(db, "u-1") is False

    rec = [r for r in caplog.records if "Error resetting summary" in r.getMessage()]
    assert rec and rec[0].exc_info is not None
    db.rollback.assert_called_once()


def test_waveform_original_download_outage_returns_none_with_traceback(caplog):
    from app.core.exceptions import StorageUnavailableError
    from app.tasks import waveform as mod

    err = StorageUnavailableError("Storage unavailable while downloading a file: ConnectionError")
    with (
        patch.object(mod, "download_file", side_effect=err),
        caplog.at_level(logging.ERROR, logger=mod.logger.name),
    ):
        assert mod._download_to_temp_file("a/b.mp4") is None

    rec = [r for r in caplog.records if "Error downloading file" in r.getMessage()]
    assert rec and rec[0].exc_info is not None
