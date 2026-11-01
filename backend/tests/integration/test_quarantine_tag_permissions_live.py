"""A real storage identity without ``s3:PutObjectTagging`` is reported, not shrugged off.

Live counterpart of ``unit/test_media_revocation.py``: instead of a fake raising
``AccessDenied``, this provisions a REAL MinIO service account whose inline policy grants
everything the app needs to store media and read tags but NOT ``s3:PutObjectTagging`` (the
IAM role the cloud team ran on staging), and points the production code at it.

Everything it creates is its own and removed in ``finally``: a throwaway bucket
``e2e-quarantine-iam-<hex12>`` (registered in ``scripts/lib/test_artifact_sweep.py``) and the
service account. Gated like ``test_presign_revocation.py``: needs MinIO reachable (conftest
sets ``SKIP_S3``) and the MinIO admin API, so not ``STORAGE_BACKEND=s3``.

Run: cd backend && pytest -m integration tests/integration/test_quarantine_tag_permissions_live.py
"""

from __future__ import annotations

import io
import os
import uuid

import pytest

from app.core.config import settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SKIP_S3", "True").lower() == "true",
        reason="MinIO not reachable — this test needs a real object store",
    ),
    pytest.mark.skipif(
        settings.STORAGE_BACKEND.strip().lower() == "s3",
        reason="STORAGE_BACKEND=s3 has no MinIO admin API to provision a limited identity",
    ),
]

BUCKET_PREFIX = "e2e-quarantine-iam-"
PUT_TAGS = "s3:PutObjectTagging"


def _root():
    """The ROOT client, read before any test swaps ``minio_service.minio_client``."""
    from app.services.minio_service import minio_client

    return minio_client


def _empty_and_remove(client, bucket: str) -> None:
    for obj in client.list_objects(bucket, recursive=True):
        client.remove_object(bucket, obj.object_name)
    client.remove_bucket(bucket)


@pytest.fixture()
def bucket(monkeypatch):
    root = _root()
    name = f"{BUCKET_PREFIX}{uuid.uuid4().hex[:12]}"
    root.make_bucket(name)
    try:
        monkeypatch.setattr(settings, "MEDIA_BUCKET_NAME", name)
        yield name
    finally:
        # The root client captured above: a test may have swapped the module's client for
        # the limited identity, which is deleted before this runs.
        _empty_and_remove(root, name)


@pytest.fixture()
def limited_client(bucket):
    """A real MinIO identity with the staging role's permissions: no ``s3:PutObjectTagging``."""
    from minio import Minio
    from minio.credentials import StaticProvider
    from minio.minioadmin import MinioAdmin

    from app.services import storage_backend

    endpoint, secure = storage_backend.storage_endpoint()
    admin = MinioAdmin(
        endpoint=endpoint,
        credentials=StaticProvider(settings.MINIO_ROOT_USER, settings.MINIO_ROOT_PASSWORD),
        secure=secure,
    )
    access_key = f"otiam{uuid.uuid4().hex[:15]}"
    secret_key = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:GetObjectTagging",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            },
        ],
    }
    admin.add_service_account(access_key=access_key, secret_key=secret_key, policy=policy)
    try:
        yield Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    finally:
        admin.delete_service_account(access_key)


def _put(client, bucket: str, key: str) -> None:
    body = b"quarantine-iam-live"
    client.put_object(bucket, key, io.BytesIO(body), len(body))


def test_a_real_access_denied_on_put_object_tagging_names_the_action(bucket, limited_client):
    from app.services import minio_service

    root = _root()
    key = "user_1/file_1/original.mp4"
    _put(limited_client, bucket, key)

    write = minio_service.write_object_quarantine_tag(key, True, client=limited_client)

    assert write.outcome == "failed"
    assert write.error_code == "AccessDenied"
    assert write.denied_action == PUT_TAGS
    assert not (root.get_object_tags(bucket, key) or {})


def test_the_startup_probe_finds_the_missing_permission_and_cleans_up(bucket, limited_client):
    from minio.error import S3Error

    from app.services import media_revocation

    probe = media_revocation.report_native_s3_tagging_posture(limited_client)

    assert probe.verified is False
    assert probe.missing_permissions == (PUT_TAGS,)
    assert probe.error is None
    with pytest.raises(S3Error) as gone:
        _root().stat_object(bucket, media_revocation.TAG_PROBE_OBJECT_KEY)
    assert gone.value.code == "NoSuchKey"


def test_the_startup_probe_verifies_a_capable_identity(bucket):
    from app.services import media_revocation

    probe = media_revocation.probe_object_tagging(_root(), bucket)

    assert probe == media_revocation.TaggingProbe(True)
