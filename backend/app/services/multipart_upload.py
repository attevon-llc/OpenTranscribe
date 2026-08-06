"""Browser-side presigned multipart upload (issue #327).

The single presigned PUT in ``minio_service.presigned_put_url`` is rejected by
AWS S3 above 5 GiB, and a PUT that fails at 90% of 10 GB restarts at zero on any
backend. This module splits the object into parts the browser uploads
independently: the API creates the upload, signs a *batch* of part URLs at a
time, and completes or aborts it. Bytes never enter the API container.

Only the control plane lives here. The browser holds no credentials and never
parses S3 XML — it PUTs opaque URLs and reports the ETags it got back.

**Part URLs are minted per batch, not per upload.** Every presigned URL is
clamped to ``PRESIGNED_URL_MAX_SECONDS`` (6 h) because it cannot outlive the STS
credentials that signed it, and a 15 GB upload on a slow line can easily run
longer than that. Signing ~8 parts at a time means a URL only has to survive its
own batch, and the client re-asks for the next one.

minio-py's multipart primitives (``_create_multipart_upload`` and friends) are
underscore-prefixed but are the whole of its multipart implementation and are
stable across 7.x, which ``requirements.txt`` pins. Using them keeps the single
``minio.Minio`` client that ``storage_backend`` exists to guarantee; the
alternative — boto3 — would mean a second SDK with its own duplicate copy of the
endpoint/credential/region policy. ``tests/unit/test_multipart_upload.py``
asserts the primitives still exist so an SDK bump fails in CI, not in a user's
10 GB upload.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from app.core.config import settings
from app.services import storage_backend
from app.services.minio_service import ensure_bucket_exists
from app.services.minio_service import minio_client
from app.services.storage_backend import clamp_presigned_expiry
from app.services.storage_backend import rewrite_public_host

logger = logging.getLogger(__name__)

#: How many part URLs to hand the browser per signing round trip. Small enough
#: that a batch finishes long before its URLs expire, large enough to keep the
#: part-upload pipeline full at the client's concurrency.
PART_URL_BATCH = 8

#: Lifetime requested for part URLs. Clamped like every other presigned URL; the
#: batch design means the effective ceiling is never the binding constraint.
PART_URL_EXPIRE_SECONDS = 3600

#: Storage-side backstop for uploads the browser never finished (tab closed, laptop
#: shut). S3 and MinIO both bill for the parts of an incomplete upload until it is
#: aborted, and neither expires one on its own by default.
ABORT_INCOMPLETE_AFTER_DAYS = 7

_LIFECYCLE_RULE_ID = "abort-incomplete-multipart"


def _bucket() -> str:
    return settings.MEDIA_BUCKET_NAME


def _clean_etag(etag: str) -> str:
    """Strip the transport quoting S3 puts around an ETag header value."""
    return str(etag).strip().strip('"')


def create_upload(object_name: str, content_type: str | None) -> str:
    """Start a multipart upload and return its ``upload_id``.

    ``content_type`` is fixed here, not at completion: S3 stores the object
    metadata from the create call, so omitting it yields an
    ``application/octet-stream`` object the media player will refuse to stream.
    """
    ensure_bucket_exists()
    headers: dict[str, str] = {"Content-Type": content_type or "application/octet-stream"}
    upload_id = minio_client._create_multipart_upload(_bucket(), object_name, headers)  # noqa: SLF001
    logger.info(f"Created multipart upload {upload_id} for {object_name}")
    return str(upload_id)


def presign_parts(
    object_name: str, upload_id: str, part_numbers: list[int]
) -> tuple[dict[int, str], int]:
    """Sign PUT URLs for *part_numbers* of an existing multipart upload.

    Returns:
        ``(urls_by_part_number, expires_in_seconds)``. The lifetime is the
        clamped value actually signed, so the client can re-ask before it lapses
        rather than discovering the expiry as a 403 half-way through a part.
    """
    expires = clamp_presigned_expiry(PART_URL_EXPIRE_SECONDS)
    delta = datetime.timedelta(seconds=expires)
    urls: dict[int, str] = {}
    for number in part_numbers:
        url = minio_client.get_presigned_url(
            "PUT",
            _bucket(),
            object_name,
            expires=delta,
            extra_query_params={"partNumber": str(number), "uploadId": upload_id},
        )
        urls[number] = rewrite_public_host(str(url))
    return urls, expires


def list_uploaded_parts(object_name: str, upload_id: str) -> list[dict[str, Any]]:
    """List the parts the backend has already stored, oldest part number first.

    This is what makes resume authoritative: the client's idea of what it sent
    is a guess (a PUT can succeed with the response lost), the bucket's is not.
    """
    parts: list[dict[str, Any]] = []
    marker: str | None = None
    while True:
        result = minio_client._list_parts(  # noqa: SLF001
            _bucket(), object_name, upload_id, part_number_marker=marker
        )
        for part in result.parts:
            parts.append(
                {
                    "part_number": int(part.part_number),
                    "etag": _clean_etag(part.etag),
                    "size": int(part.size or 0),
                }
            )
        if not result.is_truncated:
            break
        marker = str(result.next_part_number_marker)
    parts.sort(key=lambda p: p["part_number"])
    return parts


def complete_upload(object_name: str, upload_id: str, parts: list[dict[str, Any]]) -> None:
    """Assemble the uploaded parts into the final object.

    Args:
        object_name: Target object key.
        upload_id: The upload to complete.
        parts: ``{"part_number": int, "etag": str}`` entries. Sorted here because
            S3 rejects an out-of-order part list, and the browser uploads parts
            concurrently so its natural order is completion order.
    """
    from minio.datatypes import Part

    ordered = sorted(parts, key=lambda p: int(p["part_number"]))
    payload = [Part(int(p["part_number"]), _clean_etag(p["etag"])) for p in ordered]
    minio_client._complete_multipart_upload(_bucket(), object_name, upload_id, payload)  # noqa: SLF001
    logger.info(f"Completed multipart upload {upload_id} for {object_name} ({len(payload)} parts)")


def abort_upload(object_name: str, upload_id: str) -> bool:
    """Abort one multipart upload, discarding its parts. Best-effort."""
    try:
        minio_client._abort_multipart_upload(_bucket(), object_name, upload_id)  # noqa: SLF001
        logger.info(f"Aborted multipart upload {upload_id} for {object_name}")
        return True
    except Exception as e:  # noqa: BLE001 — cleanup must never fail the caller
        logger.warning(f"Could not abort multipart upload {upload_id} for {object_name}: {e}")
        return False


def abort_uploads_for_object(object_name: str) -> int:
    """Abort every in-progress multipart upload for *object_name*.

    The cancel/delete path knows the object key but not the ``upload_id`` (it is
    client state), so the uploads are discovered by listing. Without this a
    cancelled 10 GB upload keeps billing for its parts indefinitely.

    Returns:
        Number of uploads aborted. Never raises.
    """
    aborted = 0
    try:
        result = minio_client._list_multipart_uploads(  # noqa: SLF001
            _bucket(), prefix=object_name
        )
        for upload in result.uploads or []:
            if upload.object_name != object_name:
                continue
            if abort_upload(object_name, str(upload.upload_id)):
                aborted += 1
    except Exception as e:  # noqa: BLE001 — cleanup must never fail the caller
        logger.warning(f"Could not list multipart uploads for {object_name}: {e}")
    return aborted


def ensure_abort_incomplete_lifecycle(
    bucket_name: str, days: int = ABORT_INCOMPLETE_AFTER_DAYS
) -> bool:
    """Install the bucket rule that expires abandoned multipart uploads.

    The explicit abort in :func:`abort_uploads_for_object` covers cancel and
    delete; nothing covers a browser that simply goes away mid-upload. Existing
    lifecycle rules (bulk-export and derived-cache expiry) are preserved by id.

    **Native S3 only.** MinIO's ILM engine rejects an
    ``AbortIncompleteMultipartUpload`` rule outright (``InvalidArgument``, and it
    silently drops the action from a mixed rule) — verified against
    RELEASE.2025-09-07. It does not need one: MinIO purges stale multipart
    uploads itself on a background scan, ``api.stale_uploads_expiry``, 24 h by
    default. Attempting it anyway would log a warning on every MinIO startup.

    Returns:
        True if the rule is present after the call, False if it was skipped or
        could not be set. Best-effort — never raises, so it cannot block startup.
    """
    from minio.commonconfig import ENABLED
    from minio.commonconfig import Filter
    from minio.lifecycleconfig import AbortIncompleteMultipartUpload
    from minio.lifecycleconfig import LifecycleConfig
    from minio.lifecycleconfig import Rule

    if not storage_backend.is_native_s3():
        return False

    try:
        others = []
        try:
            current = minio_client.get_bucket_lifecycle(bucket_name)
            for rule in current.rules if current and current.rules else []:
                if rule.rule_id == _LIFECYCLE_RULE_ID:
                    existing = rule.abort_incomplete_multipart_upload
                    if existing and existing.days_after_initiation == days:
                        return True
                else:
                    others.append(rule)
        except Exception:
            others = []

        rule = Rule(
            ENABLED,
            rule_filter=Filter(prefix=""),
            rule_id=_LIFECYCLE_RULE_ID,
            abort_incomplete_multipart_upload=AbortIncompleteMultipartUpload(
                days_after_initiation=days
            ),
        )
        minio_client.set_bucket_lifecycle(bucket_name, LifecycleConfig([*others, rule]))
        logger.info(f"Abandoned multipart uploads on {bucket_name} expire after {days}d")
        return True
    except Exception as e:  # noqa: BLE001 — best-effort housekeeping
        logger.warning(f"Could not set multipart-abort lifecycle on {bucket_name}: {e}")
        return False


def build_upload_plan(
    object_name: str, content_type: str | None, file_size: int | None
) -> dict[str, Any] | None:
    """Decide how the browser should deliver *object_name* and set it up.

    The backend owns this decision so the client only executes it: multipart
    above :func:`storage_backend.multipart_threshold_bytes`, one presigned PUT
    below it, and ``None`` when neither is possible so the caller falls back to
    the API-mediated ``POST /files``.

    Returns:
        A dict merged into the ``/files/prepare`` response, or ``None``.
    """
    from app.services.minio_service import presigned_put_url

    if storage_backend.use_multipart_upload(file_size):
        size = int(file_size or 0)
        part_size = storage_backend.multipart_part_size(size)
        part_count = storage_backend.multipart_part_count(size, part_size)
        try:
            upload_id = create_upload(object_name, content_type)
            first_batch = list(range(1, min(PART_URL_BATCH, part_count) + 1))
            urls, expires_in = presign_parts(object_name, upload_id, first_batch)
        except Exception as e:  # noqa: BLE001 — degrade to the API-mediated path
            logger.warning(f"Multipart setup failed for {object_name}, falling back: {e}")
            return None
        return {
            "upload_method": "MULTIPART",
            "http_flow": "presigned-multipart",
            "multipart": {
                "upload_id": upload_id,
                "part_size": part_size,
                "part_count": part_count,
                "batch_size": PART_URL_BATCH,
                "expires_in": expires_in,
                "urls": {str(number): url for number, url in urls.items()},
            },
        }

    if not storage_backend.supports_single_put(file_size):
        return None

    return {
        "upload_method": "PUT",
        "http_flow": "presigned",
        "upload_url": presigned_put_url(object_name),
    }
