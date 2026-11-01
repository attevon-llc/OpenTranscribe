"""The quarantine/release routes report a failed media revocation instead of hiding it.

Staging repro (``STORAGE_BACKEND=s3``, bucket-policy Deny attached, app identity missing
``s3:PutObjectTagging``): ``POST /api/admin/files/{uuid}/quarantine`` answered a clean 200
while an already-issued presigned media URL kept working for ``MEDIA_URL_EXPIRE_SECONDS``.

Decision under test: the takedown still lands in the database (every read surface gates on
it), and the response, the audit event and the log all say the URL was NOT revoked and
which storage permission is missing. Storage is ``fixtures.fake_tagging_store`` (real
``S3Error`` ``AccessDenied``), the database is the real savepoint session.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import MagicMock

import pytest
from fastapi import status
from fixtures.fake_tagging_store import FakeTaggingStore
from fixtures.fake_tagging_store import install
from fixtures.fake_tagging_store import quarantine_deny_policy

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.core.config import settings
from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services import storage_backend

PUT_TAGS = "s3:PutObjectTagging"
QUARANTINE_BODY = {"reason": "DMCA-1", "operator_action_reason": "operator action (test)"}


@pytest.fixture
def owned_file(db_session) -> MediaFile:
    from app.core.security import get_password_hash

    uid = uuid_pkg.uuid4().hex[:8]
    owner = User(
        email=f"revocation_owner_{uid}@example.com",
        full_name="Revocation Test Owner",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(owner)
    db_session.commit()
    fuuid = uuid_pkg.uuid4()
    media = MediaFile(
        uuid=fuuid,
        filename=f"revocation_{fuuid.hex[:8]}.mp4",
        storage_path=f"user_{owner.id}/file_{fuuid.hex[:8]}/original.mp4",
        thumbnail_path=f"user_{owner.id}/file_{fuuid.hex[:8]}/thumb.webp",
        content_type="video/mp4",
        file_size=1000,
        user_id=owner.id,
        status=FileStatus.COMPLETED,
    )
    db_session.add(media)
    db_session.commit()
    db_session.refresh(media)
    return media


@pytest.fixture
def audit(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr("app.services.takedown_notices.audit_logger", fake)
    return fake


def _s3(monkeypatch, file: MediaFile, *, deny=(), policy: bool = True) -> FakeTaggingStore:
    store = FakeTaggingStore(
        objects=[file.storage_path, file.thumbnail_path],
        deny=deny,
        bucket_policy=quarantine_deny_policy(settings.MEDIA_BUCKET_NAME) if policy else None,
    )
    return install(monkeypatch, store, native_s3=True)


def _events(audit, event_type):
    return [c.kwargs for c in audit.log.call_args_list if c.kwargs["event_type"] == event_type]


def _quarantine(client, headers, file):
    return client.post(
        f"/api/admin/files/{file.uuid}/quarantine", headers=headers, json=QUARANTINE_BODY
    )


def test_missing_put_object_tagging_quarantines_but_reports_the_url_not_revoked(
    client, admin_token_headers, owned_file, db_session, monkeypatch, audit
):
    store = _s3(monkeypatch, owned_file, deny=[PUT_TAGS])

    response = _quarantine(client, admin_token_headers, owned_file)

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["is_quarantined"] is True
    assert body["presign_revoked"] is False
    assert body["presign_tag_cleared"] is None
    assert body["media_revocation"] == {
        "action": "revoke",
        "complete": False,
        "reason": "tag_write_failed",
        "enforcement": "bucket_policy",
        "missing_permissions": [PUT_TAGS],
        "exposure_seconds": storage_backend.clamp_presigned_expiry(
            settings.MEDIA_URL_EXPIRE_SECONDS
        ),
        "objects": [
            {
                "role": "original",
                "outcome": "failed",
                "error_code": "AccessDenied",
                "failed_action": PUT_TAGS,
            },
            {
                "role": "thumbnail",
                "outcome": "failed",
                "error_code": "AccessDenied",
                "failed_action": PUT_TAGS,
            },
        ],
    }
    # The record is quarantined regardless: storage permissions never block a takedown.
    db_session.expire_all()
    reloaded = db_session.query(MediaFile).filter(MediaFile.uuid == owned_file.uuid).one()
    assert reloaded.is_quarantined is True
    assert store.quarantined(owned_file.storage_path) is False

    (event,) = _events(audit, AuditEventType.ADMIN_FILE_QUARANTINE)
    assert event["outcome"] == AuditOutcome.PARTIAL
    assert event["error_code"] == "tag_write_failed"
    assert event["details"]["presign_revoked"] is False
    assert event["details"]["media_revocation"]["missing_permissions"] == [PUT_TAGS]


def test_a_fully_permitted_identity_reports_a_clean_revocation(
    client, admin_token_headers, owned_file, monkeypatch, audit
):
    store = _s3(monkeypatch, owned_file)

    response = _quarantine(client, admin_token_headers, owned_file)

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["presign_revoked"] is True
    revocation = body["media_revocation"]
    assert (revocation["complete"], revocation["reason"]) == (True, None)
    assert revocation["enforcement"] == "bucket_policy"
    assert revocation["missing_permissions"] == []
    assert [o["outcome"] for o in revocation["objects"]] == ["applied", "applied"]
    assert store.quarantined(owned_file.storage_path) is True
    (event,) = _events(audit, AuditEventType.ADMIN_FILE_QUARANTINE)
    assert event["outcome"] == AuditOutcome.SUCCESS


def test_a_thumbnail_tag_failure_is_not_reported_as_revoked(
    client, admin_token_headers, owned_file, monkeypatch, audit
):
    """A thumbnail URL outliving the takedown is a revocation failure too."""
    store = _s3(monkeypatch, owned_file)
    store.deny_on[owned_file.thumbnail_path] = {PUT_TAGS}

    response = _quarantine(client, admin_token_headers, owned_file)

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert store.quarantined(owned_file.storage_path) is True
    assert body["presign_revoked"] is False
    assert [o["outcome"] for o in body["media_revocation"]["objects"]] == ["applied", "failed"]


def test_tags_with_no_bucket_policy_are_not_reported_as_revoked(
    client, admin_token_headers, owned_file, monkeypatch, audit
):
    """On native S3 a tag nothing enforces leaves every issued URL working."""
    store = _s3(monkeypatch, owned_file, policy=False)

    response = _quarantine(client, admin_token_headers, owned_file)

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert store.quarantined(owned_file.storage_path) is True
    assert body["presign_revoked"] is False
    assert body["media_revocation"]["reason"] == "enforcement_missing"
    assert body["media_revocation"]["enforcement"] == "none"


def test_a_release_whose_untag_is_refused_reports_the_file_still_blocked(
    client, admin_token_headers, owned_file, monkeypatch, audit
):
    monkeypatch.setattr("app.services.media_revocation.UNTAG_BACKOFF_SECONDS", 0)
    store = _s3(monkeypatch, owned_file)
    assert _quarantine(client, admin_token_headers, owned_file).status_code == 200
    store.deny.add(PUT_TAGS)

    response = client.post(
        f"/api/admin/files/{owned_file.uuid}/release", headers=admin_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["is_quarantined"] is False
    assert body["presign_tag_cleared"] is False
    assert body["presign_revoked"] is None
    revocation = body["media_revocation"]
    assert revocation["action"] == "restore"
    assert revocation["reason"] == "tag_clear_failed"
    assert revocation["missing_permissions"] == [PUT_TAGS]
    assert revocation["exposure_seconds"] is None
    assert store.quarantined(owned_file.storage_path) is True
    (event,) = _events(audit, AuditEventType.ADMIN_FILE_RELEASE)
    assert event["outcome"] == AuditOutcome.PARTIAL
    assert event["error_code"] == "tag_clear_failed"
