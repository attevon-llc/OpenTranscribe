"""``MinIOService.list_objects`` wraps minio-py's lazy generator.

The S3 error surfaces while the caller iterates, so the translation to ``StorageError`` has to
wrap the iteration, not the call that creates the generator; otherwise a listing refusal escapes
as a raw client error and the callers' ``except StorageError`` is dead code.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from minio.error import S3Error
from urllib3.response import BaseHTTPResponse

from app.core.exceptions import StorageError
from app.services.minio_bucket_admin import MinIOService
from app.services.storage_versions import StorageDeleteIncompleteError
from app.services.video_processing_service import VideoProcessingService


def _access_denied() -> S3Error:
    response = MagicMock(spec=BaseHTTPResponse)
    return S3Error(response, "AccessDenied", "denied", "/cache", "req", "host")


def _lazy_failure(exc: BaseException):
    def _generator(*args: Any, **kwargs: Any):
        raise exc
        yield  # makes this a generator: the error happens on the first next()

    return _generator


def _service_over(client: Any) -> MinIOService:
    service = MinIOService.__new__(MinIOService)
    service.client = client
    return service


def test_list_objects_translates_an_error_raised_during_iteration():
    denied = _access_denied()
    client = MagicMock()
    client.list_objects.side_effect = _lazy_failure(denied)

    with pytest.raises(StorageError):
        list(_service_over(client).list_objects("cache", prefix="derived/"))


def test_masked_render_listing_refusal_becomes_incomplete_delete():
    denied = _access_denied()
    client = MagicMock()
    client.list_objects.side_effect = _lazy_failure(denied)
    svc = VideoProcessingService.__new__(VideoProcessingService)
    svc.minio_service = _service_over(client)
    svc.cache_bucket = "cache"

    with pytest.raises(StorageDeleteIncompleteError):
        svc._masked_video_cache_keys(7, "talk.mp4")
