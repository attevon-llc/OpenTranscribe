"""Browser-side presigned multipart upload (issue #327).

Three things are pinned here.

**The sizing policy.** Which transport an object gets, how many parts it splits
into, and that the threshold can never be configured above the backend's
single-PUT ceiling — the case the whole feature exists for.

**The minio-py primitives we depend on.** ``multipart_upload`` drives minio-py's
underscore-prefixed multipart methods rather than adding boto3 as a second SDK
with its own copy of the endpoint/credential policy. That is a deliberate
coupling, so a version bump that removes one must fail here, in CI, and not in a
user's 10 GB upload.

**A genuine round trip**, when the dev stack's MinIO is reachable
(``SKIP_S3=False``, auto-detected by conftest): create → sign → PUT parts →
complete → verify bytes → abort/cleanup. Everything it writes lives under a
``tests/multipart/`` prefix and is deleted again.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.core.config import settings
from app.services import multipart_upload as mp
from app.services import storage_backend as sb

S3_LIVE = os.environ.get("SKIP_S3", "True").lower() != "true"

pytestmark = pytest.mark.xdist_group("storage_backend")

MIB = 1024**2
GIB = 1024**3


@pytest.fixture
def minio_backend(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "minio")
    monkeypatch.setattr(settings, "MULTIPART_THRESHOLD_MB", 512)


@pytest.fixture
def s3_backend(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "MULTIPART_THRESHOLD_MB", 512)


# --------------------------------------------------------------------------
# Transport choice
# --------------------------------------------------------------------------


def test_small_object_keeps_the_single_put_path(minio_backend):
    assert sb.use_multipart_upload(64 * MIB) is False
    assert sb.supports_single_put(64 * MIB) is True


def test_unknown_size_is_not_forced_into_multipart(minio_backend):
    # /files/complete re-checks the size storage actually observed; guessing
    # multipart here would break every upload that omits a size.
    assert sb.use_multipart_upload(None) is False


def test_threshold_switches_to_multipart(minio_backend):
    assert sb.use_multipart_upload(512 * MIB - 1) is False
    assert sb.use_multipart_upload(512 * MIB) is True


def test_threshold_is_configurable(minio_backend, monkeypatch):
    monkeypatch.setattr(settings, "MULTIPART_THRESHOLD_MB", 2048)
    assert sb.use_multipart_upload(1 * GIB) is False
    assert sb.use_multipart_upload(2 * GIB) is True


def test_threshold_cannot_be_raised_past_the_s3_single_put_ceiling(s3_backend, monkeypatch):
    """The 5 GiB case is not optional: a single PUT above it is simply rejected."""
    monkeypatch.setattr(settings, "MULTIPART_THRESHOLD_MB", 1024 * 1024)  # 1 TiB
    assert sb.multipart_threshold_bytes() == sb.S3_SINGLE_PUT_MAX_BYTES
    assert sb.use_multipart_upload(6 * GIB) is True
    assert sb.supports_single_put(6 * GIB) is False


def test_minio_ceiling_is_still_5_tib(minio_backend, monkeypatch):
    monkeypatch.setattr(settings, "MULTIPART_THRESHOLD_MB", 1024 * 1024)
    assert sb.multipart_threshold_bytes() == 1024 * 1024 * MIB
    assert sb.supports_single_put(6 * GIB) is True


# --------------------------------------------------------------------------
# Part sizing
# --------------------------------------------------------------------------


def test_default_part_size_for_realistic_files():
    assert sb.multipart_part_size(10 * GIB) == 64 * MIB
    assert sb.multipart_part_count(10 * GIB, 64 * MIB) == 160


def test_last_part_is_counted():
    assert sb.multipart_part_count(64 * MIB + 1, 64 * MIB) == 2
    assert sb.multipart_part_count(1, 64 * MIB) == 1


def test_part_size_grows_to_stay_under_the_10000_part_cap():
    huge = 900 * GIB  # 14 400 parts at 64 MiB
    part_size = sb.multipart_part_size(huge)
    assert part_size > 64 * MIB
    assert part_size % MIB == 0
    assert sb.multipart_part_count(huge, part_size) <= sb.MULTIPART_MAX_PARTS


def test_part_size_never_drops_below_the_5_mib_minimum():
    assert sb.multipart_part_size(1024) >= sb.MULTIPART_MIN_PART_BYTES


# --------------------------------------------------------------------------
# SDK coupling
# --------------------------------------------------------------------------


def test_minio_py_multipart_primitives_exist():
    """Guard the underscore-prefixed methods this module drives."""
    from minio import Minio

    for name in (
        "_create_multipart_upload",
        "_complete_multipart_upload",
        "_abort_multipart_upload",
        "_list_parts",
        "_list_multipart_uploads",
    ):
        assert callable(getattr(Minio, name, None)), f"minio-py no longer exposes {name}"


def test_abort_lifecycle_rule_is_s3_only(minio_backend):
    """MinIO's ILM rejects an AbortIncompleteMultipartUpload rule and does not
    need one — it expires stale uploads on its own background scan."""
    assert mp.ensure_abort_incomplete_lifecycle("any-bucket") is False


def test_etag_quoting_is_stripped():
    assert (
        mp._clean_etag('"d41d8cd98f00b204e9800998ecf8427e"') == "d41d8cd98f00b204e9800998ecf8427e"
    )
    assert mp._clean_etag(" abc ") == "abc"


# --------------------------------------------------------------------------
# Upload plan
# --------------------------------------------------------------------------


def test_plan_below_threshold_is_a_single_put(minio_backend, monkeypatch):
    monkeypatch.setattr(
        "app.services.minio_service.presigned_put_url", lambda name: f"https://example/{name}"
    )
    plan = mp.build_upload_plan("media/x.wav", "audio/wav", 1024)
    assert plan is not None
    assert plan["upload_method"] == "PUT"
    assert "multipart" not in plan


def test_plan_above_s3_ceiling_is_never_a_single_put(s3_backend, monkeypatch):
    monkeypatch.setattr(mp, "create_upload", lambda name, ct: "upl-1")
    monkeypatch.setattr(
        mp, "presign_parts", lambda name, upl, parts: ({n: "u" for n in parts}, 900)
    )
    plan = mp.build_upload_plan("media/big.mp4", "video/mp4", 6 * GIB)
    assert plan is not None
    assert plan["upload_method"] == "MULTIPART"
    assert plan["multipart"]["part_size"] == 64 * MIB
    assert plan["multipart"]["part_count"] == 96
    # Only the first batch is signed up front — part URLs are clamped to
    # PRESIGNED_URL_MAX_SECONDS and a 6 GiB upload can outlive that.
    assert len(plan["multipart"]["urls"]) == mp.PART_URL_BATCH


def test_plan_falls_back_when_multipart_setup_fails(s3_backend, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(mp, "create_upload", boom)
    # None makes /files/prepare withhold a URL, which sends the browser to the
    # API-mediated POST /files instead of failing the upload outright.
    assert mp.build_upload_plan("media/big.mp4", "video/mp4", 6 * GIB) is None


# --------------------------------------------------------------------------
# Live round trip
# --------------------------------------------------------------------------


def _direct(url: str) -> str:
    """Undo the browser-facing host rewrite so a test process can PUT the part.

    ``rewrite_public_host`` maps MinIO onto the ``/s3`` proxy path, which only
    exists inside the SPA's origin.
    """
    if url.startswith("http"):
        return url
    base = f"http://{settings.MINIO_HOST}:{settings.MINIO_PORT}"
    return base + (url[3:] if url.startswith("/s3/") else url)


@pytest.mark.skipif(not S3_LIVE, reason="requires the dev stack's MinIO")
def test_multipart_round_trip_against_real_storage(minio_backend):
    """create → sign → PUT two parts → complete → verify → delete."""
    import requests

    from app.services.minio_service import delete_file
    from app.services.minio_service import object_exists_and_size

    object_name = f"tests/multipart/{uuid.uuid4()}.bin"
    part_size = sb.MULTIPART_MIN_PART_BYTES
    bodies = [os.urandom(part_size), os.urandom(1024)]

    upload_id = mp.create_upload(object_name, "application/octet-stream")
    try:
        urls, expires_in = mp.presign_parts(object_name, upload_id, [1, 2])
        assert expires_in <= sb.max_presigned_seconds()

        etags = []
        for number, body in enumerate(bodies, start=1):
            url = _direct(urls[number])
            response = requests.put(url, data=body, timeout=60)
            assert response.status_code == 200, response.text
            etags.append(mp._clean_etag(response.headers["ETag"]))

        listed = mp.list_uploaded_parts(object_name, upload_id)
        assert [p["part_number"] for p in listed] == [1, 2]
        assert [p["size"] for p in listed] == [len(bodies[0]), len(bodies[1])]

        mp.complete_upload(
            object_name,
            upload_id,
            [{"part_number": n, "etag": e} for n, e in enumerate(etags, start=1)],
        )
        assert object_exists_and_size(object_name) == sum(len(b) for b in bodies)
    finally:
        mp.abort_uploads_for_object(object_name)
        try:
            delete_file(object_name)
        except Exception:  # noqa: BLE001 — cleanup only
            pass
    assert object_exists_and_size(object_name) is None


@pytest.mark.skipif(not S3_LIVE, reason="requires the dev stack's MinIO")
def test_abort_releases_an_unfinished_upload(minio_backend):
    """An abandoned upload keeps billing for its parts until it is aborted."""
    import requests

    object_name = f"tests/multipart/{uuid.uuid4()}.bin"
    upload_id = mp.create_upload(object_name, "application/octet-stream")
    urls, _ = mp.presign_parts(object_name, upload_id, [1])
    body = os.urandom(sb.MULTIPART_MIN_PART_BYTES)
    assert requests.put(_direct(urls[1]), data=body, timeout=60).ok

    assert mp.list_uploaded_parts(object_name, upload_id)
    assert mp.abort_uploads_for_object(object_name) == 1
    assert mp.abort_uploads_for_object(object_name) == 0
