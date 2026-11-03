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


def _put_versions(bucket: str, key: str, versions: int = 2) -> None:
    from app.services.minio_service import minio_client

    for n in range(versions):
        body = f"take {n}".encode()
        minio_client.put_object(bucket, key, io.BytesIO(body), len(body), "video/mp4")


def _all_versions(bucket: str, prefix: str = "") -> list[str]:
    from app.services.minio_service import minio_client

    return [
        f"{o.object_name}@{o.version_id}"
        for o in minio_client.list_objects(
            bucket, prefix=prefix, recursive=True, include_version=True
        )
    ]


def test_a_prefix_purge_removes_every_version(versioned_media_bucket):
    """``delete_prefix`` (the admin derived-cache clear) on a versioned bucket."""
    from app.services.minio_service import MinIOService

    for key in ("derived/1_a_with_speakers.mp4", "derived/2_b_audio.mp3"):
        _put_versions(versioned_media_bucket, key)
    _put_versions(versioned_media_bucket, "keep/other.mp4", versions=1)

    removed = MinIOService().delete_prefix(versioned_media_bucket, "derived/")

    assert removed == 4
    assert _all_versions(versioned_media_bucket, "derived/") == []
    assert len(_all_versions(versioned_media_bucket, "keep/")) == 1, "outside the prefix kept"


def test_a_files_derived_cache_is_removed_every_version(versioned_media_bucket):
    """``clear_derived_cache`` (part of every file delete and erasure) on a versioned bucket."""
    from app.services.minio_service import MinIOService
    from app.services.video_processing_service import VideoProcessingService

    service = VideoProcessingService(MinIOService())
    service.cache_bucket = versioned_media_bucket
    keys = service.derived_cache_keys(4242, "talk.wav")
    assert keys, "no derived keys to seed"
    for key in keys:
        _put_versions(versioned_media_bucket, key)

    service.clear_derived_cache(4242, "talk.wav")

    assert _all_versions(versioned_media_bucket) == []


def test_the_orphan_sweep_removes_every_version(versioned_media_bucket):
    from app.services.minio_service import minio_client
    from app.tasks.storage_orphan_sweep_task import _remove_group

    names = ["user_1/file_999999901/a.wav", "user_1/file_999999901/thumbnail.webp"]
    for key in names:
        _put_versions(versioned_media_bucket, key)

    assert _remove_group(minio_client, versioned_media_bucket, names) == 0
    assert _all_versions(versioned_media_bucket) == []


@pytest.fixture
def locked_media_bucket(monkeypatch) -> Iterator[str]:
    """A bucket with object lock (so versioned), as a legal-hold deployment configures it."""
    from app.core.config import settings
    from app.services.minio_service import minio_client

    bucket = f"e2e-versioned-erasure-{uuid_pkg.uuid4().hex[:12]}"
    minio_client.make_bucket(bucket, object_lock=True)
    try:
        monkeypatch.setattr(settings, "MEDIA_BUCKET_NAME", bucket)
        yield bucket
    finally:
        for obj in minio_client.list_objects(bucket, recursive=True, include_version=True):
            if not obj.is_delete_marker:
                minio_client.disable_object_legal_hold(bucket, obj.object_name, obj.version_id)
        _empty_and_remove(minio_client, bucket)


def test_a_version_under_legal_hold_fails_the_delete_and_is_named(locked_media_bucket):
    """Partial failure reports failure: the held version survives and the error names it."""
    from app.services.file_cleanup_service import delete_file_storage_artifacts
    from app.services.minio_service import delete_file
    from app.services.minio_service import minio_client

    key = "user_1/file_999999902/held.wav"
    _put_versions(locked_media_bucket, key, versions=1)
    held = minio_client.stat_object(locked_media_bucket, key).version_id
    assert held, "object lock implies versioning; the seed has no version id"
    minio_client.enable_object_legal_hold(locked_media_bucket, key, held)
    _put_versions(locked_media_bucket, key, versions=1)  # a second, unheld version

    with pytest.raises(Exception, match=held) as raised:
        delete_file(key)
    assert key in str(raised.value)
    assert delete_file_storage_artifacts(999999902, {"storage_path": key}) is False
    assert _all_versions(locked_media_bucket) == [f"{key}@{held}"], "only the held one survives"
