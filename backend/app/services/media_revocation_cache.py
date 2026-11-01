"""The cache-bucket copies of a file a takedown must remove: derived renders and bulk exports.

A file's media objects (original, thumbnail, playback rendition) are TAGGED on quarantine so a
release can restore the very same URLs (``media_revocation``). The cache bucket
(``CACHE_BUCKET_NAME``) holds copies whose presigned URLs survive a takedown just the same,
but they are regenerable or ephemeral, so a takedown DELETES them instead and a release has
nothing to restore (they are rebuilt on the next request):

* **Derived renders** — extracted audio and burned-in-subtitle video, every key under
  ``derived/{file_id}_`` (``VideoProcessingService.generate_cache_key`` / ``audio_cache_key``;
  the id leads both, which is what makes the prefix exact: ``derived/1_`` never matches file
  ``12``).
* **Bulk exports** — a subtitle ZIP or a DOCX/PDF transcript at ``bulk/{job_id}.{ext}``. The
  key names no file, so the export writes one empty MARKER per file it contains,
  ``bulk/by-file/{file_id}/{job_id}.{ext}``, BEFORE uploading the export (the ``bulk/``
  lifecycle rule expires both). A takedown lists the file's markers and deletes the exports
  they name; an export that a marker names but that is not uploaded yet is caught by the
  export task's own re-check after its upload (:func:`remove_if_quarantined`).

A listing or delete that storage refuses is reported as a failed object, so the revocation is
never called complete while a copy may still be served.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.media_revocation import ObjectRole
    from app.services.media_revocation import ObjectTagResult

logger = logging.getLogger(__name__)

DERIVED_PREFIX = "derived/"
BULK_PREFIX = "bulk/"
BULK_MARKER_PREFIX = "bulk/by-file/"


def derived_prefix(file_id: int) -> str:
    """Every derived-cache key of ``file_id`` starts with this, and no other file's does."""
    return f"{DERIVED_PREFIX}{int(file_id)}_"


def bulk_marker_prefix(file_id: int) -> str:
    """Where the markers naming ``file_id``'s bulk exports live."""
    return f"{BULK_MARKER_PREFIX}{int(file_id)}/"


def record_bulk_export(storage, bucket: str, file_ids: Iterable[int], export_key: str) -> None:
    """Write one marker per file BEFORE ``export_key`` is uploaded, so a takedown can find it.

    Args:
        storage: The ``MinIOService`` the export uploads through (its ``upload_bytes``).
        bucket: The cache bucket the export goes to.
        file_ids: Every file whose content the export holds.
        export_key: ``bulk/<job>.<ext>``.

    Raises:
        Exception: Storage refused a marker. The caller must not publish the export then:
            an export no takedown can find would outlive one.
    """
    name = export_key.removeprefix(BULK_PREFIX)
    for file_id in sorted({int(f) for f in file_ids}):
        storage.upload_bytes(
            bucket, f"{bulk_marker_prefix(file_id)}{name}", b"", "application/octet-stream"
        )


def _list(prefix: str) -> list[str]:
    from app.services.minio_service import minio_client

    return [
        str(obj.object_name)
        for obj in minio_client.list_objects(
            settings.CACHE_BUCKET_NAME, prefix=prefix, recursive=True
        )
        if obj.object_name
    ]


def _listing_failure(role: ObjectRole, error: Exception) -> ObjectTagResult:
    from minio.error import S3Error

    from app.services.media_revocation import ObjectTagResult
    from app.services.minio_service import LIST_ACTION
    from app.services.minio_service import QuarantineTagWrite

    code = error.code if isinstance(error, S3Error) else type(error).__name__
    logger.warning(f"Could not list {role} copies in {settings.CACHE_BUCKET_NAME}: {error}")
    return ObjectTagResult(
        role, QuarantineTagWrite("failed", error_code=code, failed_action=LIST_ACTION)
    )


def remove_cache_copies(file_id: int) -> list[ObjectTagResult]:
    """Delete every cache-bucket copy of ``file_id`` and report each one. Never raises."""
    from app.services.media_revocation import ObjectTagResult
    from app.services.minio_service import remove_object_reported

    bucket = settings.CACHE_BUCKET_NAME
    results: list[ObjectTagResult] = []
    try:
        derived = _list(derived_prefix(file_id))
    except Exception as e:  # noqa: BLE001 — reported as a failed object, never raised
        results.append(_listing_failure("derived", e))
        derived = []
    results.extend(
        ObjectTagResult("derived", remove_object_reported(bucket, key)) for key in derived
    )

    try:
        markers = _list(bulk_marker_prefix(file_id))
    except Exception as e:  # noqa: BLE001 — reported as a failed object, never raised
        results.append(_listing_failure("bulk_export", e))
        markers = []
    for marker in markers:
        export_key = f"{BULK_PREFIX}{marker.removeprefix(bulk_marker_prefix(file_id))}"
        results.append(ObjectTagResult("bulk_export", remove_object_reported(bucket, export_key)))
        # The marker itself serves nothing; a leftover one only costs a later no-op delete.
        remove_object_reported(bucket, marker)
    return results


def any_quarantined(file_ids: Iterable[int]) -> bool:
    """Whether any of ``file_ids`` is quarantined right now (fresh, short read)."""
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile

    ids = [int(f) for f in file_ids]
    if not ids:
        return False
    with session_scope() as db:
        return (
            db.query(MediaFile.id)
            .filter(MediaFile.id.in_(ids), MediaFile.is_quarantined.is_(True))
            .first()
            is not None
        )


def remove_if_quarantined(
    file_ids: Iterable[int], cache_key: str, *, requester_id: int | None = None
) -> bool:
    """After uploading a cache copy, delete it again if a takedown landed meanwhile.

    Closes the window in which a takedown lists a file's copies before this one existed.
    A review admin (who may export quarantined files) is exempt, matching the export's own
    read. Returns True when the copy was removed (the caller must not hand out its URL).
    """
    ids = list(file_ids)
    if not any_quarantined(ids):
        return False
    if requester_id is not None:
        from app.db.session_utils import session_scope
        from app.services.takedown_predicates import is_review_admin

        with session_scope() as db:
            if is_review_admin(db, requester_id):
                return False
    from app.services.minio_service import remove_object_reported

    result = remove_object_reported(settings.CACHE_BUCKET_NAME, cache_key)
    if result.outcome == "failed":
        logger.error(
            f"A takedown landed while {cache_key} was being built and the copy could NOT be "
            f"deleted ({result.error_code}); its URL is not handed out, but the object remains."
        )
    return True
