"""Outage injection for the storage-facing handlers (broad-except lane B7).

The two lifecycle tests pin a real fail-open: an outage while reading the bucket's
existing lifecycle rules used to read as "no rules", and the write that followed
replaced every other rule on the bucket with just the new one.
"""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import MagicMock

import pytest
import urllib3
from minio import Minio
from minio.error import S3Error

from app.services import minio_service
from app.services import multipart_upload
from app.services.minio_bucket_admin import MinIOService


def _s3_error(code: str) -> S3Error:
    return S3Error(MagicMock(spec=urllib3.BaseHTTPResponse), code, code, "res", "req", "host")


def _traceback_records(caplog, name):
    return [r for r in caplog.records if r.name == name and r.exc_info]


def _existing_rule(rule_id: str = "other-rule"):
    from minio.commonconfig import ENABLED
    from minio.commonconfig import Filter
    from minio.lifecycleconfig import Expiration
    from minio.lifecycleconfig import Rule

    return Rule(
        ENABLED, rule_filter=Filter(prefix="x/"), rule_id=rule_id, expiration=Expiration(days=1)
    )


class _FakeLifecycleStore:
    """A bucket whose lifecycle config is read with ``get`` and replaced wholesale with ``set``."""

    def __init__(self, stored, read_error=None):
        self.stored = stored
        self.read_error = read_error

    def get_bucket_lifecycle(self, bucket):
        if self.read_error is not None:
            raise self.read_error
        return self.stored

    def set_bucket_lifecycle(self, bucket, config):
        self.stored = config


def _rule_ids(config) -> list[str]:
    return [r.rule_id for r in config.rules] if config else []


def test_ensure_prefix_expiry_does_not_overwrite_rules_when_the_config_is_unreadable():
    from minio.lifecycleconfig import LifecycleConfig

    outage = urllib3.exceptions.MaxRetryError(MagicMock(), "http://minio", None)
    service = MinIOService.__new__(MinIOService)
    keep = LifecycleConfig([_existing_rule("keep-me")])
    store = _FakeLifecycleStore(keep, read_error=outage)
    service.client = cast(Minio, store)

    service.ensure_prefix_expiry("b", "tmp/", 7, "expire-tmp")

    assert store.stored is keep, "an unreadable config must not be rewritten"


def test_ensure_prefix_expiry_still_creates_the_rule_when_none_exists():
    service = MinIOService.__new__(MinIOService)
    store = _FakeLifecycleStore(None, read_error=_s3_error("NoSuchLifecycleConfiguration"))
    service.client = cast(Minio, store)

    service.ensure_prefix_expiry("b", "tmp/", 7, "expire-tmp")

    assert _rule_ids(store.stored) == ["expire-tmp"]


def test_multipart_lifecycle_does_not_overwrite_rules_when_the_config_is_unreadable(monkeypatch):
    client = MagicMock()
    client.get_bucket_lifecycle.side_effect = _s3_error("InternalError")
    monkeypatch.setattr(multipart_upload, "minio_client", client)
    monkeypatch.setattr(multipart_upload.storage_backend, "is_native_s3", lambda: True)
    assert multipart_upload.ensure_abort_incomplete_lifecycle("b") is False
    client.set_bucket_lifecycle.assert_not_called()


def test_multipart_abort_failure_is_logged_with_traceback_and_returns_false(monkeypatch, caplog):
    client = MagicMock()
    client._abort_multipart_upload.side_effect = urllib3.exceptions.HTTPError("down")
    monkeypatch.setattr(multipart_upload, "minio_client", client)
    monkeypatch.setattr(multipart_upload, "_bucket", lambda: "b")
    with caplog.at_level(logging.WARNING, logger=multipart_upload.logger.name):
        assert multipart_upload.abort_upload("obj", "uid") is False
    assert _traceback_records(caplog, multipart_upload.logger.name)


def test_a_bug_in_multipart_abort_is_not_swallowed(monkeypatch):
    client = MagicMock()
    client._abort_multipart_upload.side_effect = AttributeError("bug, not an outage")
    monkeypatch.setattr(multipart_upload, "minio_client", client)
    monkeypatch.setattr(multipart_upload, "_bucket", lambda: "b")
    with pytest.raises(AttributeError):
        multipart_upload.abort_upload("obj", "uid")


def test_legal_hold_outage_returns_false_with_traceback(monkeypatch, caplog):
    client = MagicMock()
    client.enable_object_legal_hold.side_effect = _s3_error("InvalidRequest")
    monkeypatch.setattr(minio_service, "minio_client", client)
    with caplog.at_level(logging.WARNING, logger=minio_service.logger.name):
        assert minio_service.set_object_legal_hold("obj", True) is False
    assert _traceback_records(caplog, minio_service.logger.name)


def test_notification_suppression_fails_closed_with_traceback_on_a_db_outage(monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError

    from app.db import session_utils
    from app.services import file_availability

    def _down():
        raise OperationalError("SELECT", {}, Exception("db down"))

    monkeypatch.setattr(session_utils, "session_scope", _down)
    with caplog.at_level(logging.WARNING, logger=file_availability.logger.name):
        assert file_availability.is_notification_suppressed(1, 2) is True
    assert _traceback_records(caplog, file_availability.logger.name)


def test_download_event_publish_redis_outage_is_logged_with_traceback(monkeypatch, caplog):
    from redis.exceptions import ConnectionError as RedisConnectionError

    from app.services import download_events

    redis_client = MagicMock()
    redis_client.publish.side_effect = RedisConnectionError("down")
    monkeypatch.setattr(download_events, "get_request_redis", lambda: redis_client)
    with caplog.at_level(logging.ERROR, logger=download_events.logger.name):
        download_events._publish("chan", {"a": 1})
    assert _traceback_records(caplog, download_events.logger.name)


def test_download_event_publish_does_not_swallow_a_bug(monkeypatch):
    from app.services import download_events

    redis_client = MagicMock()
    redis_client.publish.side_effect = AttributeError("bug, not an outage")
    monkeypatch.setattr(download_events, "get_request_redis", lambda: redis_client)
    with pytest.raises(AttributeError):
        download_events._publish("chan", {"a": 1})


def test_s3_backup_listing_outage_is_empty_with_traceback(monkeypatch, caplog):
    from botocore.exceptions import EndpointConnectionError

    from app.services import backup_s3

    monkeypatch.setattr(backup_s3, "get_s3_secret_key", lambda db: "secret")
    monkeypatch.setattr(
        backup_s3,
        "_build_s3_client",
        MagicMock(side_effect=EndpointConnectionError(endpoint_url="x")),
    )
    cfg = {"s3_bucket": "b", "s3_prefix": ""}
    with caplog.at_level(logging.WARNING, logger=backup_s3.logger.name):
        assert backup_s3.list_backups_s3(cfg) == []
    assert _traceback_records(caplog, backup_s3.logger.name)
