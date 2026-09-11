"""Live-stack integration test for presigned-URL revocation (issue #907).

Falsifiable end-to-end proof: mint a presigned URL, confirm it works, quarantine-tag
the object, confirm the SAME URL now 403s, remove the tag, confirm access is
restored on that SAME URL — all against the real dev-stack MinIO (the repo's exact
pinned image), fetched with a plain HTTP client (``requests``) rather than a Minio
SDK instance, because the whole point is that the URL's signature alone carries
authorization.

Gated the same way as the other MinIO-backed tests: conftest TCP-probes
localhost:5178 and sets SKIP_S3 accordingly; also skips outright on
``STORAGE_BACKEND=s3`` (native S3 has no MinIO admin API, so there is nothing for
this mechanism to provision).

Watch it fail first, per root CLAUDE.md's git-archive recipe (run from the repo
root, not this worktree, so HEAD is the pre-#907 tree this file didn't exist in —
copy this file onto that tree instead):

    git archive HEAD | (mkdir -p /tmp/redcheck907 && tar -x -C /tmp/redcheck907)
    cp backend/tests/integration/test_presign_revocation.py \\
        /tmp/redcheck907/backend/tests/integration/
    cd /tmp/redcheck907/backend && venv/bin/python -m pytest \\
        tests/integration/test_presign_revocation.py -v

Expect ``test_a_presigned_url_minted_before_takedown_stops_working`` to fail with
200 where it asserts 403 — proof this test isn't vacuous.

Run: cd backend && PYTHONPATH=. pytest -m integration tests/integration/test_presign_revocation.py -v
"""

from __future__ import annotations

import io
import os
import uuid

import pytest
import requests

from app.core.config import settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SKIP_S3", "True").lower() == "true",
        reason="MinIO (localhost:5178) not reachable — presign revocation test needs the dev stack",
    ),
    pytest.mark.skipif(
        settings.STORAGE_BACKEND.strip().lower() == "s3",
        reason="STORAGE_BACKEND=s3 has no MinIO admin API — nothing to provision/revoke here",
    ),
]

TEST_PREFIX = "tests/presign-revocation-907"
_HTTP_TIMEOUT_S = 10


def _upload_test_object(key: str, body: bytes = b"issue-907-presign-revocation") -> None:
    from app.services.minio_service import minio_client

    minio_client.put_object(
        settings.MEDIA_BUCKET_NAME,
        key,
        io.BytesIO(body),
        length=len(body),
        content_type="text/plain",
    )


def _delete_test_object(key: str) -> None:
    from app.services.minio_service import minio_client

    try:
        minio_client.remove_object(settings.MEDIA_BUCKET_NAME, key)
    except Exception:  # noqa: BLE001 — best-effort test cleanup
        pass


@pytest.fixture()
def _presign_identity_reset(monkeypatch):
    """Force a real (re-)provisioning attempt against the live MinIO admin API.

    Without this, a module-level memoized result from an earlier (mocked) unit
    test run in the same interpreter could short-circuit this test before it ever
    talks to the real admin API.
    """
    from app.services import storage_presign_identity as pid

    monkeypatch.setattr(pid, "_ensure_attempted", False)
    monkeypatch.setattr(pid, "_ensure_result", False)
    monkeypatch.setattr(pid, "_restricted_client", None)
    monkeypatch.setattr(pid, "_fallback_error_logged", False)


@pytest.fixture()
def _public_url_matches_internal(monkeypatch):
    """Trap #1 (see module docstring): ``get_file_url`` rewrites the internal MinIO
    host onto ``STORAGE_PUBLIC_URL`` or the ``/s3`` proxy path — neither is directly
    fetchable from this pytest process. Point the rewrite at the SAME host:port the
    test's own client already talks to, so the returned URL is fetchable as-is. This
    changes nothing about what is under test (the signature/policy enforcement),
    only where the returned string happens to point.
    """
    scheme = "https" if settings.MINIO_SECURE else "http"
    internal = f"{scheme}://{settings.MINIO_HOST}:{settings.MINIO_PORT}"
    monkeypatch.setattr(settings, "STORAGE_PUBLIC_URL", internal)


@pytest.fixture()
def object_key():
    key = f"{TEST_PREFIX}/{uuid.uuid4().hex}.txt"
    yield key
    _delete_test_object(key)


@pytest.fixture()
def bystander_key():
    key = f"{TEST_PREFIX}/{uuid.uuid4().hex}-bystander.txt"
    yield key
    _delete_test_object(key)


@pytest.mark.usefixtures("_presign_identity_reset", "_public_url_matches_internal")
class TestPresignRevocation:
    def test_a_presigned_url_minted_before_takedown_stops_working(self, object_key):
        from app.services.minio_service import get_file_url
        from app.services.minio_service import set_object_quarantine_tag

        _upload_test_object(object_key)

        url = get_file_url(object_key, expires=300)

        control = requests.get(url, timeout=_HTTP_TIMEOUT_S)
        assert control.status_code == 200, (
            f"control request must succeed BEFORE quarantine — got {control.status_code}: "
            f"{control.text[:500]}"
        )

        assert set_object_quarantine_tag(object_key, True) is True

        revoked = requests.get(url, timeout=_HTTP_TIMEOUT_S)
        assert revoked.status_code == 403, (
            "the SAME presigned URL must 403 the instant the object is tagged — got "
            f"{revoked.status_code}: {revoked.text[:500]}"
        )

        assert set_object_quarantine_tag(object_key, False) is True

        restored = requests.get(url, timeout=_HTTP_TIMEOUT_S)
        assert restored.status_code == 200, (
            "release must restore the SAME pre-existing URL to working — got "
            f"{restored.status_code}: {restored.text[:500]}"
        )

    def test_quarantining_one_object_does_not_revoke_another(self, object_key, bystander_key):
        from app.services.minio_service import get_file_url
        from app.services.minio_service import set_object_quarantine_tag

        _upload_test_object(object_key)
        _upload_test_object(bystander_key)

        bystander_url = get_file_url(bystander_key, expires=300)

        assert set_object_quarantine_tag(object_key, True) is True

        bystander_response = requests.get(bystander_url, timeout=_HTTP_TIMEOUT_S)
        assert bystander_response.status_code == 200, (
            "quarantining one object must not collaterally revoke an unrelated one — got "
            f"{bystander_response.status_code}"
        )

    def test_the_presign_identity_cannot_clear_its_own_quarantine_tag(self, object_key):
        from minio.commonconfig import Tags
        from minio.error import S3Error

        from app.services import storage_presign_identity as pid
        from app.services.minio_service import set_object_quarantine_tag

        _upload_test_object(object_key)
        assert set_object_quarantine_tag(object_key, True) is True

        client = pid.presign_client()
        # Least-privilege containment (measured): the restricted identity has no
        # s3:PutObjectTagging, so it cannot remove the very tag it is Denied by.
        # Narrowed to S3Error (AccessDenied) rather than a bare Exception so this
        # can't be satisfied by an unrelated failure in the call.
        with pytest.raises(S3Error) as exc_info:
            client.set_object_tags(settings.MEDIA_BUCKET_NAME, object_key, Tags.new_object_tags())
        assert exc_info.value.code == "AccessDenied"

    def test_a_ranged_get_is_revoked_too(self, object_key):
        """The real <video> player request shape: a 206 Partial Content, not a bare 200."""
        from app.services.minio_service import get_file_url
        from app.services.minio_service import set_object_quarantine_tag

        _upload_test_object(object_key, body=b"0123456789" * 10)
        url = get_file_url(object_key, expires=300)

        ranged_before = requests.get(url, headers={"Range": "bytes=0-9"}, timeout=_HTTP_TIMEOUT_S)
        assert ranged_before.status_code == 206, (
            f"control ranged request must succeed BEFORE quarantine — got "
            f"{ranged_before.status_code}: {ranged_before.text[:500]}"
        )

        assert set_object_quarantine_tag(object_key, True) is True

        ranged_after = requests.get(url, headers={"Range": "bytes=0-9"}, timeout=_HTTP_TIMEOUT_S)
        assert ranged_after.status_code == 403, (
            f"a ranged GET must be revoked exactly like a plain GET — got "
            f"{ranged_after.status_code}: {ranged_after.text[:500]}"
        )
