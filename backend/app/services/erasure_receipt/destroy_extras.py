"""The deletes an erasure used to leave behind (issue #1353), run after the canonical destroy.

* the login-lockout record, keyed by the address, which named the subject for its whole TTL;
* ``temp/preprocess/<file uuid>/`` staging objects, every version;
* unfinished multipart uploads under the subject's prefixes (parts bill and never list as objects);
* the subject's keys on the media mirror destination (decision D2, erasure-only).

Each failure is appended to ``errors`` as a fixed sentence naming the class only, so the erasure
is PARTIAL rather than silently incomplete. Nothing here raises.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.erasure_receipt.types import Captured
from app.services.erasure_receipt.types import ErasureScope
from app.services.erasure_receipt.types import object_prefixes

logger = logging.getLogger(__name__)


def _fail(errors: list[dict[str, Any]], stage: str, exc: Exception) -> None:
    errors.append({"stage": stage, "error": f"{stage} cleanup failed ({type(exc).__name__})"})


def erase_lockout(captured: Captured, errors: list[dict[str, Any]]) -> None:
    if not captured.email:
        return
    try:
        from app.auth.lockout import erase_identifier

        erase_identifier(captured.email)
    except Exception as e:  # noqa: BLE001 - recorded, never raised
        _fail(errors, "lockout", e)


def erase_object_prefixes(
    scope: ErasureScope, captured: Captured, errors: list[dict[str, Any]]
) -> None:
    """Remove every version of everything still under the subject's object prefixes.

    The canonical per-file destroy removes the objects it knows by name; this removes what it
    does not (``user_<uid>/youtube_<id>/`` thumbnails, a rendition an older version wrote, noncurrent
    versions of either). A file a legal hold keeps is never touched, and a user prefix is only
    removed whole when nothing under it is held.
    """
    from app.core.config import settings
    from app.services.minio_service import minio_client
    from app.services.storage_versions import remove_completely

    prefixes = object_prefixes(scope, captured)
    if scope.kind == "user" and scope.user_id is not None:
        prefixes = [*prefixes, f"avatars/{scope.user_id}/"]
    for prefix in prefixes:
        try:
            remove_completely(minio_client, settings.MEDIA_BUCKET_NAME, prefix=prefix)
        except Exception as e:  # noqa: BLE001
            _fail(errors, "object_prefix", e)


def erase_derived_cache(captured: Captured, errors: list[dict[str, Any]]) -> None:
    """Remove the regenerable derived cache of every erased file, every version.

    The canonical destroy removes the cache keys it can name from the filename; this removes the
    rest by prefix (``derived/<file id>_``), including masked variants an older policy left.
    """
    from app.core.config import settings
    from app.services.minio_service import minio_client
    from app.services.storage_versions import remove_completely
    from app.services.video_processing_service import VideoProcessingService

    held = set(captured.held_file_ids)
    for file_id in captured.file_ids:
        if file_id in held:
            continue
        try:
            remove_completely(
                minio_client,
                settings.CACHE_BUCKET_NAME,
                prefix=f"{VideoProcessingService.DERIVED_CACHE_PREFIX}{file_id}_",
            )
        except Exception as e:  # noqa: BLE001
            _fail(errors, "derived_cache", e)


def erase_temp_objects(captured: Captured, errors: list[dict[str, Any]]) -> None:
    from app.core.config import settings
    from app.services.minio_service import TEMP_PREPROCESS_PREFIX
    from app.services.minio_service import minio_client
    from app.services.storage_versions import remove_completely

    for file_uuid in captured.file_uuids:
        try:
            remove_completely(
                minio_client,
                settings.MEDIA_BUCKET_NAME,
                prefix=f"{TEMP_PREPROCESS_PREFIX}/{file_uuid}/",
            )
        except Exception as e:  # noqa: BLE001
            _fail(errors, "temp_objects", e)


def abort_multipart(scope: ErasureScope, captured: Captured, errors: list[dict[str, Any]]) -> None:
    from app.core.config import settings
    from app.services.minio_service import minio_client
    from app.services.multipart_upload import abort_upload

    for prefix in object_prefixes(scope, captured):
        try:
            listing = minio_client._list_multipart_uploads(  # the SDK exposes no public listing
                settings.MEDIA_BUCKET_NAME, prefix=prefix
            )
            for upload in listing.uploads or []:
                if not abort_upload(str(upload.object_name), str(upload.upload_id)):
                    raise RuntimeError("abort refused")
        except Exception as e:  # noqa: BLE001
            _fail(errors, "multipart", e)


def erase_mirror(scope: ErasureScope, captured: Captured, errors: list[dict[str, Any]]) -> None:
    try:
        from app.services.media_mirror_erasure import erase_mirrored

        erase_mirrored(object_prefixes(scope, captured))
    except Exception as e:  # noqa: BLE001
        _fail(errors, "mirror", e)


def erase_extras(scope: ErasureScope, captured: Captured, errors: list[dict[str, Any]]) -> None:
    """Run every extra delete for ``scope``."""
    if scope.kind == "user":
        erase_lockout(captured, errors)
    erase_object_prefixes(scope, captured, errors)
    erase_derived_cache(captured, errors)
    erase_temp_objects(captured, errors)
    abort_multipart(scope, captured, errors)
    erase_mirror(scope, captured, errors)
