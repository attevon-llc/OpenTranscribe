"""Quarantine revocation must be LOUD when storage refuses it (no live storage needed).

Reported from staging (``STORAGE_BACKEND=s3``): with the bucket-policy Deny attached but the
app identity missing ``s3:PutObjectTagging``, a takedown logged one WARNING and answered a
clean 200, and an already-issued presigned URL kept working for ``MEDIA_URL_EXPIRE_SECONDS``.
These tests drive the real ``minio_service.write_object_quarantine_tag`` and
``media_revocation`` against ``fixtures.fake_tagging_store``, which raises real ``S3Error``
``AccessDenied`` for the actions it is told to refuse.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fixtures.fake_tagging_store import FakeTaggingStore
from fixtures.fake_tagging_store import install
from fixtures.fake_tagging_store import quarantine_deny_policy

from app.core.config import settings
from app.core.constants import STORAGE_QUARANTINE_TAG_KEY
from app.models.media import MediaFile
from app.services import media_revocation
from app.services import minio_service
from app.services import storage_backend
from app.services import storage_presign_identity

BUCKET = "media-under-test"
GET_TAGS = "s3:GetObjectTagging"
PUT_TAGS = "s3:PutObjectTagging"
GET_POLICY = "s3:GetBucketPolicy"


@pytest.fixture(autouse=True)
def _bucket(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_BUCKET_NAME", BUCKET)
    monkeypatch.setattr(settings, "MEDIA_URL_EXPIRE_SECONDS", 1800)
    monkeypatch.setattr(media_revocation, "UNTAG_BACKOFF_SECONDS", 0)


def _file(*, thumbnail: bool = True, playback: bool = False) -> MediaFile:
    """A transient (never added to a session) row: revocation only reads its object keys."""
    fid = uuid_pkg.uuid4().hex[:8]
    return MediaFile(
        id=1,
        uuid=uuid_pkg.uuid4(),
        storage_path=f"user_1/file_{fid}/original.mp4",
        thumbnail_path=f"user_1/file_{fid}/thumb.webp" if thumbnail else None,
        playback_path=f"user_1/file_{fid}/x.playback.m4a" if playback else None,
    )


def _keys(file) -> list[str]:
    return [k for k in (file.storage_path, file.thumbnail_path, file.playback_path) if k]


def _s3(monkeypatch, file, *, deny=(), policy: bool = True) -> FakeTaggingStore:
    store = FakeTaggingStore(
        objects=_keys(file),
        deny=deny,
        bucket_policy=quarantine_deny_policy(BUCKET) if policy else None,
    )
    return install(monkeypatch, store, native_s3=True)


class TestWriteObjectQuarantineTag:
    def test_a_refused_tag_write_names_the_missing_action(self, monkeypatch):
        store = install(
            monkeypatch, FakeTaggingStore(objects=["k"], deny=[PUT_TAGS]), native_s3=True
        )

        write = minio_service.write_object_quarantine_tag("k", True)

        assert write.outcome == "failed"
        assert write.error_code == "AccessDenied"
        assert write.failed_action == PUT_TAGS
        assert write.denied_action == PUT_TAGS
        assert store.quarantined("k") is False

    def test_a_refused_tag_read_is_reported_against_the_read_action(self, monkeypatch):
        install(monkeypatch, FakeTaggingStore(objects=["k"], deny=[GET_TAGS]), native_s3=True)

        write = minio_service.write_object_quarantine_tag("k", True)

        assert (write.outcome, write.failed_action) == ("failed", GET_TAGS)

    def test_set_then_clear_keeps_unrelated_tags(self, monkeypatch):
        store = install(monkeypatch, FakeTaggingStore(objects=["k"]), native_s3=True)
        store.tags["k"] = {"owner-tag": "keep"}

        assert minio_service.write_object_quarantine_tag("k", True).outcome == "applied"
        assert store.tags["k"] == {"owner-tag": "keep", STORAGE_QUARANTINE_TAG_KEY: "true"}
        assert minio_service.write_object_quarantine_tag("k", False).outcome == "applied"
        assert store.tags["k"] == {"owner-tag": "keep"}

    def test_a_missing_object_is_absent_not_failed(self, monkeypatch):
        install(monkeypatch, FakeTaggingStore(), native_s3=True)

        write = minio_service.write_object_quarantine_tag("gone", True)

        assert (write.outcome, write.error_code) == ("absent", None)


class TestRevokeMedia:
    def test_staging_repro_missing_put_object_tagging_is_reported_and_logged(
        self, monkeypatch, caplog
    ):
        file = _file()
        _s3(monkeypatch, file, deny=[PUT_TAGS])

        with caplog.at_level("WARNING", logger="app.services.media_revocation"):
            report = media_revocation.revoke_media(file)

        assert report.complete is False
        assert report.reason == media_revocation.REASON_TAG_WRITE_FAILED
        assert report.missing_permissions == [PUT_TAGS]
        assert report.failed_roles == ["original", "thumbnail"]
        assert report.enforcement == "bucket_policy"
        assert report.exposure_seconds == storage_backend.clamp_presigned_expiry(1800)
        errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert PUT_TAGS in errors[0]
        assert "did NOT revoke" in errors[0]

    def test_every_object_tagged_and_enforced_is_complete(self, monkeypatch, caplog):
        file = _file(playback=True)
        store = _s3(monkeypatch, file)

        with caplog.at_level("WARNING", logger="app.services.media_revocation"):
            report = media_revocation.revoke_media(file)

        assert report.complete is True
        assert report.reason is None
        assert report.enforcement == "bucket_policy"
        assert report.exposure_seconds is None
        assert [o.role for o in report.objects] == ["original", "thumbnail", "playback"]
        assert all(store.quarantined(k) for k in _keys(file))
        assert [r for r in caplog.records if r.levelno >= 30] == []

    def test_a_thumbnail_only_failure_is_not_a_clean_revocation(self, monkeypatch):
        file = _file()
        store = _s3(monkeypatch, file)
        store.deny_on[file.thumbnail_path] = {PUT_TAGS}

        report = media_revocation.revoke_media(file)

        assert report.complete is False
        assert report.failed_roles == ["thumbnail"]
        assert store.quarantined(file.storage_path) is True

    def test_tags_with_no_bucket_policy_are_not_a_revocation(self, monkeypatch):
        file = _file()
        store = _s3(monkeypatch, file, policy=False)

        report = media_revocation.revoke_media(file)

        assert all(store.quarantined(k) for k in _keys(file))
        assert report.complete is False
        assert report.reason == media_revocation.REASON_ENFORCEMENT_MISSING
        assert report.enforcement == "none"
        assert report.missing_permissions == []

    def test_an_unreadable_bucket_policy_is_unverified_and_named(self, monkeypatch):
        file = _file(thumbnail=False)
        _s3(monkeypatch, file, deny=[GET_POLICY, GET_TAGS, PUT_TAGS])

        report = media_revocation.revoke_media(file)

        assert report.enforcement == "unknown"
        assert report.reason == media_revocation.REASON_TAG_WRITE_FAILED
        assert report.missing_permissions == [GET_POLICY, GET_TAGS]

    def test_unreadable_policy_alone_is_enforcement_unverified(self, monkeypatch):
        file = _file(thumbnail=False)
        _s3(monkeypatch, file, deny=[GET_POLICY])

        report = media_revocation.revoke_media(file)

        assert report.reason == media_revocation.REASON_ENFORCEMENT_UNVERIFIED
        assert report.missing_permissions == [GET_POLICY]

    def test_minio_without_a_presign_identity_is_not_a_revocation(self, monkeypatch):
        file = _file(thumbnail=False)
        store = FakeTaggingStore(objects=_keys(file))
        install(monkeypatch, store, native_s3=False, presign_identity=False)

        report = media_revocation.revoke_media(file)

        assert store.quarantined(file.storage_path) is True
        assert (report.complete, report.enforcement) == (False, "none")

    def test_minio_with_the_presign_identity_is_complete(self, monkeypatch):
        file = _file(thumbnail=False)
        install(monkeypatch, FakeTaggingStore(objects=_keys(file)), native_s3=False)

        report = media_revocation.revoke_media(file)

        assert (report.complete, report.enforcement) == (True, "presign_identity")

    def test_nothing_in_storage_needs_no_enforcement_check(self, monkeypatch):
        file = _file()
        store = install(monkeypatch, FakeTaggingStore(deny=[GET_POLICY]), native_s3=True)

        report = media_revocation.revoke_media(file)

        assert [o.write.outcome for o in report.objects] == ["absent", "absent"]
        assert report.complete is True
        assert report.enforcement is None
        assert not any(action == GET_POLICY for action, _ in store.calls)

    def test_wire_and_audit_shapes(self, monkeypatch):
        file = _file(thumbnail=False)
        _s3(monkeypatch, file, deny=[PUT_TAGS])

        report = media_revocation.revoke_media(file)

        assert report.to_wire() == {
            "action": "revoke",
            "complete": False,
            "reason": "tag_write_failed",
            "enforcement": "bucket_policy",
            "missing_permissions": [PUT_TAGS],
            "exposure_seconds": storage_backend.clamp_presigned_expiry(1800),
            "objects": [
                {
                    "role": "original",
                    "outcome": "failed",
                    "error_code": "AccessDenied",
                    "failed_action": PUT_TAGS,
                }
            ],
        }
        assert report.to_audit(tenant_visible=False) == {
            "complete": False,
            "reason": "tag_write_failed",
            "enforcement": "bucket_policy",
            "missing_permissions": [PUT_TAGS],
            "failed_objects": ["original"],
        }
        assert report.to_audit(tenant_visible=True) == {
            "complete": False,
            "reason": "revocation_incomplete",
        }


class TestRestoreMedia:
    def test_a_refused_untag_is_retried_then_reported(self, monkeypatch, caplog):
        file = _file(thumbnail=False)
        store = _s3(monkeypatch, file, deny=[PUT_TAGS])
        store.tags[file.storage_path] = {STORAGE_QUARANTINE_TAG_KEY: "true"}

        with caplog.at_level("ERROR", logger="app.services.media_revocation"):
            report = media_revocation.restore_media(file)

        puts = [c for c in store.calls if c[0] == PUT_TAGS]
        assert len(puts) == media_revocation.UNTAG_RETRIES == 3
        assert report.complete is False
        assert report.reason == media_revocation.REASON_TAG_CLEAR_FAILED
        assert report.missing_permissions == [PUT_TAGS]
        assert report.enforcement is None
        assert report.exposure_seconds is None
        assert store.quarantined(file.storage_path) is True
        errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1 and PUT_TAGS in errors[0]

    def test_a_missing_object_is_not_retried(self, monkeypatch):
        file = _file(thumbnail=False)
        store = install(monkeypatch, FakeTaggingStore(), native_s3=True)

        report = media_revocation.restore_media(file)

        assert len([c for c in store.calls if c[0] == GET_TAGS]) == 1
        assert report.complete is True

    def test_a_transient_failure_recovers_on_retry(self, monkeypatch):
        file = _file(thumbnail=False)
        store = _s3(monkeypatch, file)
        store.tags[file.storage_path] = {STORAGE_QUARANTINE_TAG_KEY: "true"}
        real_set = store.set_object_tags
        attempts = {"n": 0}

        def _flaky(bucket_name, object_name, tags, version_id=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("blip")
            return real_set(bucket_name, object_name, tags)

        monkeypatch.setattr(store, "set_object_tags", _flaky)

        report = media_revocation.restore_media(file)

        assert attempts["n"] == 2
        assert report.complete is True
        assert store.quarantined(file.storage_path) is False


def test_unreadable_bucket_policy_warning_names_get_bucket_policy(caplog):
    store = FakeTaggingStore(deny=[GET_POLICY])

    with caplog.at_level("WARNING", logger="app.services.storage_presign_identity"):
        enforced = storage_presign_identity.report_native_s3_revocation_posture(store)

    assert enforced is False
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert GET_POLICY in warnings[0]
