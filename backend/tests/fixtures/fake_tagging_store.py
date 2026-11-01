"""A minio-py-shaped object store that refuses chosen storage actions like S3 IAM does.

The quarantine revocation path (``minio_service.write_object_quarantine_tag`` and
``media_revocation``) talks to ``minio_service.minio_client``. Installing this in its place
lets a test run that REAL code against the failure the cloud team hit on staging: an app
identity without ``s3:PutObjectTagging`` gets ``AccessDenied`` from ``set_object_tags``, which
used to produce a WARNING and a clean 200. Errors are real ``minio.error.S3Error`` instances,
so the production ``except S3Error`` branches are the ones exercised.

Shared by the unit suite (``unit/test_media_revocation.py``) and the route suites, so the two
cannot model S3 differently. A live-MinIO counterpart with a genuinely under-privileged
identity is ``integration/test_quarantine_tag_permissions_live.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from minio.commonconfig import Tags
from minio.error import S3Error

from app.core.constants import STORAGE_QUARANTINE_TAG_KEY


def s3_error(code: str, resource: str = "/media/key") -> S3Error:
    """A real minio-py ``S3Error`` carrying ``code``."""
    return S3Error(MagicMock(), code, f"{code} (fake)", resource, "fake-request", "fake-host")


def quarantine_deny_policy(bucket: str) -> str:
    """The bucket policy ``docs/abuse-and-takedown.md`` tells an S3 operator to attach."""
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{bucket}/*",
                    "Condition": {
                        "StringEquals": {
                            f"s3:ExistingObjectTag/{STORAGE_QUARANTINE_TAG_KEY}": "true"
                        }
                    },
                }
            ],
        }
    )


class FakeTaggingStore:
    """In-memory objects + tags; any action listed in ``deny`` raises ``AccessDenied``.

    Args:
        objects: Keys that exist.
        deny: IAM action names to refuse (``s3:GetObjectTagging``, ``s3:PutObjectTagging``,
            ``s3:GetBucketPolicy``, ``s3:PutObject``, ``s3:DeleteObject``).
        bucket_policy: The policy ``get_bucket_policy`` returns; None answers
            ``NoSuchBucketPolicy`` like S3 does for a bucket with no policy.
        deny_on: Per-key refusals, ``{key: (action, ...)}`` (a resource-scoped IAM policy).
    """

    def __init__(
        self,
        *,
        objects: tuple[str, ...] | list[str] = (),
        deny: tuple[str, ...] | list[str] = (),
        bucket_policy: str | None = None,
        deny_on: dict[str, tuple[str, ...]] | None = None,
    ):
        self.objects: dict[str, bytes] = {key: b"media" for key in objects}
        self.tags: dict[str, dict[str, str]] = {}
        self.deny = set(deny)
        self.deny_on = {key: set(actions) for key, actions in (deny_on or {}).items()}
        self.bucket_policy = bucket_policy
        self.calls: list[tuple[str, str]] = []

    def _gate(self, action: str, key: str) -> None:
        self.calls.append((action, key))
        if action in self.deny or action in self.deny_on.get(key, set()):
            raise s3_error("AccessDenied", key)

    def _require(self, key: str) -> None:
        if key not in self.objects:
            raise s3_error("NoSuchKey", key)

    def quarantined(self, key: str) -> bool:
        """Whether ``key`` currently carries the quarantine tag."""
        return self.tags.get(key, {}).get(STORAGE_QUARANTINE_TAG_KEY) == "true"

    def get_object_tags(self, bucket_name: str, object_name: str, version_id=None):
        self._gate("s3:GetObjectTagging", object_name)
        self._require(object_name)
        stored = self.tags.get(object_name)
        if not stored:
            return None
        tags = Tags.new_object_tags()
        for k, v in stored.items():
            tags[k] = v
        return tags

    def set_object_tags(self, bucket_name: str, object_name: str, tags, version_id=None):
        self._gate("s3:PutObjectTagging", object_name)
        self._require(object_name)
        self.tags[object_name] = dict(tags)

    def put_object(self, bucket_name: str, object_name: str, data, length: int, **_kw):
        self._gate("s3:PutObject", object_name)
        self.objects[object_name] = data.read(length)

    def remove_object(self, bucket_name: str, object_name: str, **_kw):
        self._gate("s3:DeleteObject", object_name)
        self.objects.pop(object_name, None)
        self.tags.pop(object_name, None)

    def list_objects(self, bucket_name: str, prefix: str = "", recursive: bool = False, **_kw):
        """Keys under ``prefix`` (buckets are not modelled: test keys never collide)."""
        from types import SimpleNamespace

        self._gate("s3:ListBucket", prefix)
        return [
            SimpleNamespace(object_name=k) for k in sorted(self.objects) if k.startswith(prefix)
        ]

    def get_bucket_policy(self, bucket_name: str) -> str:
        self._gate("s3:GetBucketPolicy", bucket_name)
        if self.bucket_policy is None:
            raise s3_error("NoSuchBucketPolicy", bucket_name)
        return self.bucket_policy

    # The takedown also places a best-effort object legal hold; accepted and ignored here.
    def enable_object_legal_hold(self, bucket_name: str, object_name: str, **_kw) -> None:
        return None

    def disable_object_legal_hold(self, bucket_name: str, object_name: str, **_kw) -> None:
        return None


def install(
    monkeypatch,
    store: FakeTaggingStore,
    *,
    native_s3: bool,
    presign_identity: bool = True,
) -> FakeTaggingStore:
    """Put ``store`` behind ``minio_service.minio_client`` and pick the storage backend.

    ``native_s3=True`` makes enforcement come from ``store.bucket_policy``; otherwise from
    whether the MinIO presign identity is (pretended to be) provisioned.
    """
    from app.core.config import settings
    from app.services import minio_service
    from app.services import storage_presign_identity

    monkeypatch.setattr(minio_service, "minio_client", store)
    monkeypatch.setattr(minio_service, "get_takedown_client", lambda: store)
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3" if native_s3 else "minio")
    monkeypatch.setattr(
        storage_presign_identity, "presign_identity_available", lambda: presign_identity
    )
    return store
