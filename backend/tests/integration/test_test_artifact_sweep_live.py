"""The store planes of ``scripts/cleanup-test-data.py`` against REAL OpenSearch, MinIO and
Postgres (issue #1195).

Every artifact here is created by this module under a registered test shape with a fresh
uuid, and removed in a ``finally`` whether or not the sweep removed it first. The sweep is
pointed at a cutoff in the future, so "old enough" is not what is being tested — the
refusals are: a populated index needs Tier B, a forged finding naming a production index is
re-checked and refused at the point of deletion, a database with an open connection is kept.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy import text

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FUTURE = 4_000_000_000


def _load() -> ModuleType:
    path = _REPO_ROOT / "scripts" / "lib" / "test_artifact_sweep.py"
    spec = importlib.util.spec_from_file_location("test_artifact_sweep_live", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sweep = _load()


def _opensearch_url() -> str:
    return (
        f"http://{os.environ.get('OPENSEARCH_HOST', 'localhost')}:{os.environ['OPENSEARCH_PORT']}"
    )


@pytest.fixture
def os_plane():
    if os.environ.get("SKIP_OPENSEARCH", "").lower() == "true":
        pytest.skip("needs a reachable OpenSearch (SKIP_OPENSEARCH is set)")
    session = requests.Session()
    if os.environ.get("OPENSEARCH_USER"):
        session.auth = (os.environ["OPENSEARCH_USER"], os.environ.get("OPENSEARCH_PASSWORD", ""))
    return sweep.OpenSearchPlane(session, _opensearch_url())


def _exists(plane, name: str) -> bool:
    status: int = plane.session.head(f"{plane.base_url}/{name}", timeout=30).status_code
    return status == 200


@pytest.mark.integration
def test_an_empty_test_index_is_swept_and_a_populated_one_needs_tier_b(os_plane) -> None:
    empty = f"test_summary_api_{uuid.uuid4().hex[:12]}"
    full = f"test_summary_api_{uuid.uuid4().hex[:12]}"
    base = os_plane.base_url
    os_plane.session.put(f"{base}/{empty}", timeout=60).raise_for_status()
    try:
        os_plane.session.put(f"{base}/{full}", timeout=60).raise_for_status()
        os_plane.session.post(
            f"{base}/{full}/_doc?refresh=true", json={"x": 1}, timeout=60
        ).raise_for_status()

        findings = {f.name: f for f in os_plane.list_findings(_FUTURE)}
        assert findings[empty].tier == sweep.TIER_A
        assert findings[full].tier == sweep.TIER_B

        assert os_plane.delete_one(findings[empty], _FUTURE, include_tier_b=False)
        assert not _exists(os_plane, empty)
        assert not os_plane.delete_one(findings[full], _FUTURE, include_tier_b=False)
        assert _exists(os_plane, full), "a populated index is never Tier A"
        assert os_plane.delete_one(findings[full], _FUTURE, include_tier_b=True)
        assert not _exists(os_plane, full)
    finally:
        for name in (empty, full):
            os_plane.session.delete(f"{base}/{name}", timeout=60)


@pytest.mark.integration
def test_a_forged_finding_for_a_production_index_is_refused_at_delete(os_plane) -> None:
    """The re-check at the point of deletion, not the listing, is the last line of defence."""
    production = "transcript_chunks"
    if not _exists(os_plane, production):
        pytest.skip("this cluster has no transcript_chunks index to protect")
    forged = sweep.Finding("index", production, sweep.TIER_A, "forged")
    assert not os_plane.delete_one(forged, _FUTURE, include_tier_b=True)
    assert _exists(os_plane, production)


@pytest.fixture
def minio_client():
    if os.environ.get("SKIP_S3", "").lower() == "true":
        pytest.skip("needs a reachable MinIO (SKIP_S3 is set)")
    from app.services.minio_service import minio_client as client

    return client


@pytest.mark.integration
def test_a_test_bucket_and_a_test_object_in_the_media_bucket_are_swept(minio_client) -> None:
    from app.core.config import settings

    bucket = f"test-erasure-journal-{uuid.uuid4().hex[:10]}"
    key = f"tests/presign-revocation-907/{uuid.uuid4().hex}.txt"
    media = settings.MEDIA_BUCKET_NAME
    configured = {"MEDIA_BUCKET_NAME": media, "CACHE_BUCKET_NAME": settings.CACHE_BUCKET_NAME}
    plane = sweep.MinioPlane(minio_client, set(configured.values()), configured)
    minio_client.make_bucket(bucket)
    try:
        minio_client.put_object(bucket, "a.txt", io.BytesIO(b"x"), length=1)
        minio_client.put_object(media, key, io.BytesIO(b"x"), length=1)

        findings = {f.name: f for f in plane.list_findings(_FUTURE)}
        assert findings[bucket].tier == sweep.TIER_A
        assert findings[f"{media}/{key}"].tier == sweep.TIER_A
        assert findings[media].tier is None, "the media bucket itself is protected"

        assert plane.delete_one(findings[bucket], _FUTURE, include_tier_b=False)
        assert not minio_client.bucket_exists(bucket)
        assert plane.delete_one(findings[f"{media}/{key}"], _FUTURE, include_tier_b=False)
        assert not any(o.object_name == key for o in minio_client.list_objects(media, prefix=key))
        assert minio_client.bucket_exists(media)
    finally:
        if minio_client.bucket_exists(bucket):
            for obj in minio_client.list_objects(bucket, recursive=True):
                minio_client.remove_object(bucket, obj.object_name)
            minio_client.remove_bucket(bucket)
        minio_client.remove_object(media, key)


def _admin_url() -> str:
    from app.core.config import settings

    return (
        f"postgresql://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
    )


@pytest.mark.integration
def test_a_scratch_database_is_tier_b_and_kept_while_connected() -> None:
    from app.core.config import settings

    name = f"ot_sweep{uuid.uuid4().hex[:8]}_test"
    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    plane = sweep.PostgresDatabasePlane(admin, settings.POSTGRES_DB)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        holder = create_engine(_admin_url().rsplit("/", 1)[0] + f"/{name}")
        with holder.connect():
            busy = {f.name: f for f in plane.list_findings(_FUTURE)}[name]
            assert busy.tier is None, "a database with a connection is never a candidate"
        holder.dispose()

        idle = {f.name: f for f in plane.list_findings(_FUTURE)}[name]
        assert idle.tier == sweep.TIER_B
        assert not plane.delete_one(idle, _FUTURE, include_tier_b=False), "scratch needs --execute"
        assert plane.delete_one(idle, _FUTURE, include_tier_b=True)
        assert name not in {f.name for f in plane.list_findings(_FUTURE)}
        assert settings.POSTGRES_DB in {f.name for f in plane.list_findings(_FUTURE)}
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
