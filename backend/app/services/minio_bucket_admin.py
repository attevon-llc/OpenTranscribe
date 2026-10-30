"""Bucket-oriented MinIO/S3 operations: the ``MinIOService`` class.

The functional upload/download/presign API, legal hold and quarantine tagging stay in
``minio_service``; this module holds the class wrapper and its lifecycle/prefix helpers.
"""

import datetime
import io
import logging
import os

from app.core.config import settings
from app.services import minio_service
from app.services import storage_presign_identity
from app.services.storage_backend import clamp_presigned_expiry
from app.services.storage_backend import rewrite_public_host

logger = logging.getLogger(__name__)


class MinIOService:
    """
    Class-based wrapper for MinIO operations.
    Provides an object-oriented interface to the existing functional API.
    """

    def __init__(self):
        self.client = minio_service.minio_client

    def upload_file(
        self,
        file_path: str,
        bucket_name: str,
        object_name: str,
        content_type: str | None = None,
    ):
        """Upload a file to MinIO bucket."""
        try:
            with open(file_path, "rb") as file_data:
                file_size = os.path.getsize(file_path)
                # Use default content type if not provided
                effective_content_type = (
                    content_type if content_type is not None else "application/octet-stream"
                )
                self.client.put_object(
                    bucket_name=bucket_name,
                    object_name=object_name,
                    data=file_data,
                    length=file_size,
                    content_type=effective_content_type,
                )
        except Exception as e:
            raise Exception(f"Error uploading file: {e}") from e

    def upload_bytes(
        self, bucket_name: str, object_name: str, data: bytes, content_type: str
    ) -> None:
        """Upload an in-memory bytes payload to a MinIO bucket."""
        try:
            self.client.put_object(
                bucket_name=bucket_name,
                object_name=object_name,
                data=io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
        except Exception as e:
            raise Exception(f"Error uploading bytes: {e}") from e

    def download_file(self, object_name: str, file_path: str, bucket_name: str | None = None):
        """Download a file from MinIO to local path."""
        bucket = bucket_name or settings.MEDIA_BUCKET_NAME
        try:
            self.client.fget_object(bucket, object_name, file_path)
        except Exception as e:
            raise Exception(f"Error downloading file: {e}") from e

    def get_presigned_url(self, bucket_name: str, object_name: str, expires: int = 3600):
        """Get a browser-reachable presigned URL for object access.

        Uses the same host-rewrite and expiry clamp as the module-level helpers.
        It previously hardcoded ``http://minio:9000`` → ``localhost:5178`` (or an
        undocumented ``EXTERNAL_MINIO_URL``), which only worked for one deployment
        shape and pinned the URL to MinIO (issue #284 A1.12).

        Browser-facing presign (issue #907): signed by the restricted presign identity
        (not ``self.client``, which is the root client) so a takedown's quarantine tag
        can revoke this URL after it's already been handed out.
        """
        try:
            url = storage_presign_identity.presign_client().presigned_get_object(
                bucket_name=bucket_name,
                object_name=object_name,
                expires=datetime.timedelta(seconds=clamp_presigned_expiry(expires)),
            )
            return rewrite_public_host(str(url))
        except Exception as e:
            raise Exception(f"Error getting presigned URL: {e}") from e

    def delete_object(self, bucket_name: str, object_name: str):
        """Delete an object from MinIO."""
        try:
            self.client.remove_object(bucket_name, object_name)
        except Exception as e:
            raise Exception(f"Error deleting object: {e}") from e

    def list_objects(self, bucket_name: str, prefix: str | None = None, recursive: bool = False):
        """List objects in a bucket."""
        try:
            return self.client.list_objects(bucket_name, prefix=prefix, recursive=recursive)
        except Exception as e:
            raise Exception(f"Error listing objects: {e}") from e

    def stat_object(self, bucket_name: str, object_name: str):
        """Get object statistics."""
        try:
            return self.client.stat_object(bucket_name, object_name)
        except Exception as e:
            raise Exception(f"Error getting object stats: {e}") from e

    def get_object(self, bucket_name: str, object_name: str):
        """Get object from MinIO.

        Stays on the ROOT ``self.client`` (issue #907): a server-side read, not a
        browser-facing presign, so the quarantine Deny must not apply here — admin
        review and the thumbnail proxy route depend on this staying unaffected by
        takedown.
        """
        try:
            return self.client.get_object(bucket_name, object_name)
        except Exception as e:
            raise Exception(f"Error getting object: {e}") from e

    def bucket_exists(self, bucket_name: str) -> bool:
        """Check if bucket exists."""
        try:
            return self.client.bucket_exists(bucket_name)  # type: ignore[no-any-return]
        except Exception as e:
            raise Exception(f"Error checking bucket existence: {e}") from e

    def make_bucket(self, bucket_name: str):
        """Create a new bucket."""
        try:
            self.client.make_bucket(bucket_name)
        except Exception as e:
            raise Exception(f"Error creating bucket: {e}") from e

    def ensure_prefix_expiry(
        self,
        bucket_name: str,
        prefix: str,
        days: int,
        rule_id: str,
        *,
        update_if_exists: bool = True,
    ) -> None:
        """Ensure an object-expiration lifecycle rule exists for a bucket prefix.

        Idempotent and best-effort: used to auto-expire ephemeral artifacts (e.g.
        bulk-export ZIPs) so they don't accumulate in object storage. Never raises —
        a missing lifecycle rule must not block uploads. Only objects under ``prefix``
        are affected; other objects in the bucket are untouched.

        ``update_if_exists=False`` only bootstraps a missing rule and leaves an existing
        one untouched — used so per-instance construction never clobbers the authoritative
        (admin/startup-set) retention value.
        """
        from minio.commonconfig import ENABLED
        from minio.commonconfig import Filter
        from minio.lifecycleconfig import Expiration
        from minio.lifecycleconfig import LifecycleConfig
        from minio.lifecycleconfig import Rule

        try:
            existing_rules = []
            try:
                current = self.client.get_bucket_lifecycle(bucket_name)
                if current and current.rules:
                    for r in current.rules:
                        if r.rule_id == rule_id:
                            # Already present: skip if unchanged, or if caller only wants to
                            # bootstrap a missing rule (not override an existing/authoritative one).
                            if not update_if_exists:
                                return
                            if r.expiration and r.expiration.days == days:
                                return
                        else:
                            existing_rules.append(r)
            except Exception:
                # No lifecycle config yet (S3 returns NoSuchLifecycleConfiguration).
                existing_rules = []

            rule = Rule(
                ENABLED,
                rule_filter=Filter(prefix=prefix),
                rule_id=rule_id,
                expiration=Expiration(days=days),
            )
            self.client.set_bucket_lifecycle(bucket_name, LifecycleConfig([*existing_rules, rule]))
            logger.info(
                f"Set lifecycle rule '{rule_id}' on {bucket_name}/{prefix} (expire {days}d)"
            )
        except Exception as e:  # pragma: no cover - best-effort housekeeping
            logger.warning(f"Could not set lifecycle rule on {bucket_name}/{prefix}: {e}")

    def remove_lifecycle_rule(self, bucket_name: str, rule_id: str) -> None:
        """Remove a single lifecycle rule by id, preserving any others. Best-effort."""
        from minio.lifecycleconfig import LifecycleConfig

        try:
            current = self.client.get_bucket_lifecycle(bucket_name)
            if not current or not current.rules:
                return
            remaining = [r for r in current.rules if r.rule_id != rule_id]
            if len(remaining) == len(current.rules):
                return  # rule wasn't present
            self.client.set_bucket_lifecycle(bucket_name, LifecycleConfig(remaining))
            logger.info(f"Removed lifecycle rule '{rule_id}' from {bucket_name}")
        except Exception as e:  # pragma: no cover - best-effort housekeeping
            logger.warning(f"Could not remove lifecycle rule '{rule_id}' on {bucket_name}: {e}")

    def prefix_stats(self, bucket_name: str, prefix: str) -> tuple[int, int]:
        """Return ``(object_count, total_bytes)`` for all objects under a prefix."""
        count = 0
        total = 0
        try:
            for obj in self.client.list_objects(bucket_name, prefix=prefix, recursive=True):
                count += 1
                total += int(obj.size or 0)
        except Exception as e:
            logger.warning(f"Could not compute prefix stats for {bucket_name}/{prefix}: {e}")
        return count, total

    def delete_prefix(self, bucket_name: str, prefix: str) -> int:
        """Delete every object under a prefix. Returns the number deleted. Best-effort."""
        from minio.deleteobjects import DeleteObject

        deleted = 0
        try:
            names = [
                obj.object_name
                for obj in self.client.list_objects(bucket_name, prefix=prefix, recursive=True)
            ]
            if not names:
                return 0
            errors = self.client.remove_objects(bucket_name, (DeleteObject(n) for n in names))
            failed = 0
            for err in errors:
                failed += 1
                logger.warning(f"Failed to delete {err.name}: {err.code} {err.message}")
            deleted = len(names) - failed
        except Exception as e:
            logger.warning(f"Could not delete prefix {bucket_name}/{prefix}: {e}")
        return deleted
