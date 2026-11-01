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
    from minio import Minio

    from app.services.media_revocation import ObjectRole
    from app.services.media_revocation import ObjectTagResult

logger = logging.getLogger(__name__)

DERIVED_PREFIX = "derived/"
BULK_PREFIX = "bulk/"
BULK_MARKER_PREFIX = "bulk/by-file/"


#: Extracted-audio keys written before the file id led the key (cache key hardening):
#: ``derived/{base}{suffix}``. Nothing reads them any more; delete paths still remove them,
#: and ``cache_management_service.reclaim_legacy_audio_cache`` sweeps the rest once.
LEGACY_AUDIO_SUFFIXES = ("_audio_mp3.mp3", "_audio_wav.wav", "_audio_original")


def legacy_audio_keys(filename: str | None) -> list[str]:
    """The pre-hardening extracted-audio keys a file named ``filename`` could own."""
    if not filename:
        return []
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    return [f"{DERIVED_PREFIX}{base}{suffix}" for suffix in LEGACY_AUDIO_SUFFIXES]


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


def _list(prefix: str, client: Minio | None = None) -> list[str]:
    from app.services.minio_service import minio_client

    storage = client or minio_client
    return [
        str(obj.object_name)
        for obj in storage.list_objects(settings.CACHE_BUCKET_NAME, prefix=prefix, recursive=True)
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


def _legacy_audio_present(filename: str | None, client: Minio | None) -> list[str]:
    keys = legacy_audio_keys(filename)
    if not keys:
        return []
    # One listing of the shared stem, then exact names only: another file's legacy key that
    # merely starts with this stem is not this file's.
    stem = keys[0].removesuffix(LEGACY_AUDIO_SUFFIXES[0])
    wanted = set(keys)
    return [k for k in _list(f"{stem}_audio_", client) if k in wanted]


def remove_cache_copies(
    file_id: int, *, filename: str | None = None, client: Minio | None = None
) -> list[ObjectTagResult]:
    """Delete every cache-bucket copy of ``file_id`` and report each one. Never raises.

    Args:
        file_id: The file.
        filename: Its original filename, to also remove pre-hardening extracted-audio keys
            (:func:`legacy_audio_keys`) that do not carry the id.
        client: Storage client (the bounded takedown client in production).
    """
    from app.services.media_revocation import ObjectTagResult
    from app.services.minio_service import remove_object_reported

    bucket = settings.CACHE_BUCKET_NAME
    results: list[ObjectTagResult] = []
    try:
        derived = _list(derived_prefix(file_id), client)
        derived += [k for k in _legacy_audio_present(filename, client) if k not in derived]
    except Exception as e:  # noqa: BLE001 — reported as a failed object, never raised
        results.append(_listing_failure("derived", e))
        derived = []
    results.extend(
        ObjectTagResult("derived", remove_object_reported(bucket, key, client=client))
        for key in derived
    )

    try:
        markers = _list(bulk_marker_prefix(file_id), client)
    except Exception as e:  # noqa: BLE001 — reported as a failed object, never raised
        results.append(_listing_failure("bulk_export", e))
        markers = []
    for marker in markers:
        export_key = f"{BULK_PREFIX}{marker.removeprefix(bulk_marker_prefix(file_id))}"
        results.append(
            ObjectTagResult(
                "bulk_export", remove_object_reported(bucket, export_key, client=client)
            )
        )
        drop_bulk_result(export_key)
        # The marker itself serves nothing; a leftover one only costs a later no-op delete.
        remove_object_reported(bucket, marker, client=client)
    return results


#: Redis key of a finished bulk export's cached result (URL included), per job.
BULK_RESULT_KEY = "bulk_export_result:{job}"


def bulk_job_of(export_key: str) -> str:
    """``bulk/<job>.<ext>`` -> ``<job>``."""
    name = export_key.removeprefix(BULK_PREFIX)
    return name.rsplit(".", 1)[0] if "." in name else name


def drop_bulk_result(export_key: str) -> None:
    """Forget the cached result (and its presigned URL) of the export at ``export_key``.

    A takedown deletes the object; without this the stream's reconnect path would still
    replay the stored URL for the rest of its TTL. Best-effort: the replay re-checks
    quarantine itself (:func:`hidden_from_requester`), so a Redis failure is logged only.
    """
    try:
        from app.core.redis import get_redis

        get_redis().delete(BULK_RESULT_KEY.format(job=bulk_job_of(export_key)))
    except Exception as e:  # noqa: BLE001 — the replay re-check is the guarantee
        logger.warning(f"Could not drop the cached result of {export_key}: {type(e).__name__}")


def hidden_from_requester(file_ids: Iterable[int], requester_id: int) -> bool:
    """Whether any of ``file_ids`` is quarantined and ``requester_id`` is not a review admin."""
    ids = list(file_ids)
    if not any_quarantined(ids):
        return False
    from app.db.session_utils import session_scope
    from app.services.takedown_predicates import is_review_admin

    with session_scope() as db:
        return not is_review_admin(db, requester_id)


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
