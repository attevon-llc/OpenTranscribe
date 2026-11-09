"""Outage-injection tests for the B8 broad-except lane (services/*.py after opensearch_erasure).

Each test injects a failure into a dependency and asserts the failure is visible (raised, or
logged with a traceback) rather than reading as a legitimate empty result.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from opensearchpy.exceptions import NotFoundError

from app.core.exceptions import StorageUnavailableError
from app.services import opensearch_snapshot
from app.services.storage_versions import StorageDeleteIncompleteError
from app.services.video_processing_service import VideoProcessingService


def _has_traceback(caplog: pytest.LogCaptureFixture) -> bool:
    return any(r.exc_info for r in caplog.records)


def test_masked_render_listing_outage_is_not_an_empty_listing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    minio = MagicMock()
    minio.list_objects.side_effect = StorageUnavailableError("storage down")
    svc = VideoProcessingService.__new__(VideoProcessingService)
    svc.minio_service = minio
    svc.cache_bucket = "media"
    with caplog.at_level(logging.ERROR), pytest.raises(StorageDeleteIncompleteError):
        svc._masked_video_cache_keys(7, "talk.mp4")
    assert _has_traceback(caplog)


def test_snapshot_repository_lookup_outage_propagates() -> None:
    client = MagicMock()
    client.snapshot.get_repository.side_effect = OSConnectionError("N/A", "refused", Exception())
    with pytest.raises(OSConnectionError):
        opensearch_snapshot.ensure_repository(client)


def test_snapshot_repository_missing_is_created() -> None:
    client = MagicMock()
    client.snapshot.get_repository.side_effect = NotFoundError(404, "missing", {})
    opensearch_snapshot.ensure_repository(client)
    kwargs = client.snapshot.create_repository.call_args.kwargs
    assert kwargs["repository"] == opensearch_snapshot.REPO_NAME


def test_snapshot_listing_outage_logs_traceback(caplog: pytest.LogCaptureFixture) -> None:
    client = MagicMock()
    client.snapshot.get.side_effect = OSConnectionError("N/A", "refused", Exception())
    with caplog.at_level(logging.WARNING):
        assert opensearch_snapshot.list_snapshot_names(client) == []
    assert _has_traceback(caplog)
