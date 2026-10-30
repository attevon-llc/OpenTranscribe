"""Characterization of the object-storage guards in ``minio_service`` / ``MinIOService``.

Pins the quarantine tag read-modify-write (issue #907) and the presign guards
(expiry clamp, restricted presign identity for browser-facing URLs, root client for
server-side fetches) against a fake client, so they are covered without a live MinIO.
"""

from __future__ import annotations

import datetime

import pytest

from app.core.constants import STORAGE_QUARANTINE_TAG_KEY
from app.services import minio_bucket_admin
from app.services import minio_service
from app.services import storage_backend
from app.services import storage_presign_identity

_CEILING = storage_backend.max_presigned_seconds()
_OVER_CEILING = _CEILING + 100_000
_URL = "http://minio:9000/bucket/key?X-Amz-Signature=abc"


class _FakeClient:
    """Records tag writes and presign requests; never touches the network."""

    def __init__(self, label: str, tags: dict[str, str] | None = None):
        self.label = label
        self.tags = tags or {}
        self.written: dict[str, str] | None = None
        self.presigned: list[datetime.timedelta] = []

    def get_object_tags(self, bucket, name):
        return dict(self.tags)

    def set_object_tags(self, bucket, name, tags):
        self.written = dict(tags.items())

    def presigned_get_object(self, bucket_name, object_name, expires, **_kw):
        self.presigned.append(expires)
        return _URL


@pytest.fixture
def root(monkeypatch):
    client = _FakeClient("root")
    monkeypatch.setattr(minio_service, "minio_client", client)
    return client


@pytest.fixture
def presign(monkeypatch):
    client = _FakeClient("presign")
    monkeypatch.setattr(storage_presign_identity, "presign_client", lambda: client)
    return client


def test_quarantine_tag_is_set_and_other_tags_are_kept(root):
    root.tags = {"keep": "me"}
    assert minio_service.set_object_quarantine_tag("obj", True) is True
    assert root.written == {
        "keep": "me",
        STORAGE_QUARANTINE_TAG_KEY: storage_presign_identity.QUARANTINE_TAG_VALUE,
    }


def test_quarantine_tag_is_cleared_and_other_tags_are_kept(root):
    root.tags = {"keep": "me", STORAGE_QUARANTINE_TAG_KEY: "true"}
    assert minio_service.set_object_quarantine_tag("obj", False) is True
    assert root.written == {"keep": "me"}


def test_quarantine_tag_failure_is_best_effort_false(root, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("storage down")

    monkeypatch.setattr(root, "get_object_tags", boom)
    assert minio_service.set_object_quarantine_tag("obj", True) is False


def test_get_file_url_clamps_expiry_and_signs_with_presign_identity(root, presign):
    assert minio_service.get_file_url("obj", expires=_OVER_CEILING).startswith("http")
    assert presign.presigned == [datetime.timedelta(seconds=_CEILING)]
    assert root.presigned == []


def test_download_url_clamps_expiry_and_signs_with_presign_identity(root, presign):
    minio_service.get_presigned_download_url("obj", expires=_OVER_CEILING)
    assert presign.presigned == [datetime.timedelta(seconds=_CEILING)]
    assert root.presigned == []


def test_class_presigned_url_clamps_expiry_and_signs_with_presign_identity(root, presign):
    svc = minio_bucket_admin.MinIOService()
    assert id(svc.client) == id(root)
    svc.get_presigned_url("bucket", "obj", expires=_OVER_CEILING)
    assert presign.presigned == [datetime.timedelta(seconds=_CEILING)]
    assert root.presigned == []


def test_internal_presign_stays_on_root_client(root, presign):
    minio_service.get_internal_presigned_url("obj", expires=_OVER_CEILING)
    assert root.presigned == [datetime.timedelta(seconds=_CEILING)]
    assert presign.presigned == []
