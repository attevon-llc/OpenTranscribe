"""Deleting a file's media must remove every VERSION of it, not just hide the current one.

A versioned bucket is not exotic here: S3 object-lock, which the platform legal hold mirrors onto
the object (``minio_service.set_object_legal_hold``), can only be enabled on one. On such a
bucket a plain ``DELETE`` only writes a delete marker, so the "deleted" recording stays
downloadable by version id, and an erasure that reports success has erased nothing.

The dev MinIO is unversioned, so ``test_deletion_residue_live.py`` cannot see this. These tests
point ``MEDIA_BUCKET_NAME`` at a THROWAWAY versioned bucket (``e2e-versioned-erasure-<hex>``,
emptied of every version and removed in ``finally``) and drive the app's real storage delete,
``file_cleanup_service.delete_file_storage_artifacts``, the one every delete and erasure path
shares. The residue is read by ``deletion_residue.minio_residue``, which lists versions.
"""

from __future__ import annotations

import io
import os
import uuid as uuid_pkg
from collections.abc import Iterator

import pytest

from tests.integration.deletion_residue import minio_residue
from tests.integration.deletion_seed import SeededFile

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SKIP_S3", "True").lower() == "true",
        reason="needs a reachable MinIO (export MINIO_PORT)",
    ),
]


def _empty_and_remove(client, bucket: str) -> None:
    for obj in client.list_objects(bucket, recursive=True, include_version=True):
        client.remove_object(bucket, obj.object_name, version_id=obj.version_id)
    client.remove_bucket(bucket)


@pytest.fixture
def versioned_media_bucket(monkeypatch) -> Iterator[str]:
    from minio.commonconfig import ENABLED
    from minio.versioningconfig import VersioningConfig

    from app.core.config import settings
    from app.services.minio_service import minio_client

    bucket = f"e2e-versioned-erasure-{uuid_pkg.uuid4().hex[:12]}"
    minio_client.make_bucket(bucket)
    try:
        minio_client.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
        monkeypatch.setattr(settings, "MEDIA_BUCKET_NAME", bucket)
        yield bucket
    finally:
        _empty_and_remove(minio_client, bucket)


def _seeded(bucket: str, *, versions: int) -> tuple[SeededFile, str]:
    from app.services.minio_service import minio_client

    file_id = 990_000_000 + int(uuid_pkg.uuid4().int % 1_000_000)
    sf = SeededFile(
        owner_id=-1,
        file_id=file_id,
        file_uuid=str(uuid_pkg.uuid4()),
        filename="erase.wav",
        tag="versioned",
    )
    key = f"user_-1/file_{file_id}/erase.wav"
    for n in range(versions):
        body = f"take {n}".encode()
        minio_client.put_object(bucket, key, io.BytesIO(body), len(body), "audio/wav")
    return sf, key


def test_the_scan_sees_a_noncurrent_version(versioned_media_bucket):
    """Negative control: a plain delete on a versioned bucket leaves bytes, and the scan says so."""
    from app.services.minio_service import minio_client

    sf, key = _seeded(versioned_media_bucket, versions=1)
    minio_client.remove_object(versioned_media_bucket, key)
    assert list(minio_client.list_objects(versioned_media_bucket, recursive=True)) == []
    found = minio_residue(sf, include_owner=False)
    assert len(found) == 2, found  # the original version and the delete marker
    assert all(f.startswith(f"minio: {versioned_media_bucket}/{key} (version ") for f in found)


def test_deleting_a_files_media_removes_every_version(versioned_media_bucket):
    from app.services.file_cleanup_service import delete_file_storage_artifacts

    sf, key = _seeded(versioned_media_bucket, versions=2)
    assert len(minio_residue(sf, include_owner=False)) == 2, "seed did not write two versions"

    assert delete_file_storage_artifacts(sf.file_id, {"storage_path": key}) is True

    assert minio_residue(sf, include_owner=False) == []
