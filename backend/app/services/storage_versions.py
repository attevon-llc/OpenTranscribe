"""Delete objects completely: every version and delete marker on a versioned bucket.

On a versioned bucket (which S3 object lock, the storage half of a legal hold, requires) a
plain ``DELETE`` only writes a delete marker; the bytes stay retrievable by version id. So a
file delete, a GDPR erasure or a cache purge that used the plain call reported success and
removed nothing. :func:`remove_completely` is the ONE implementation every destroy path uses.

Rules it keeps:

* **Every failure is raised, never swallowed.** A version under an object-lock legal hold or a
  retention period refuses the delete; :class:`StorageDeleteIncompleteError` names every key
  and version that remains. No governance-bypass header is ever sent: a lock is honoured.
* **Success means verified gone.** On a versioned (or unknown) bucket the keys are listed again
  after the delete, and anything left is reported.
* **A missing READ permission never fails a completed delete.** The bucket's versioning state
  is cached ONLY when it is ``on`` (:data:`VERSIONING_CACHE_SECONDS`, dropped on any error).
  An ``off`` answer is never cached: an operator enabling versioning must not leave versions
  behind for the length of a cache window. If ``GetBucketVersioning`` is refused the state is
  ``unknown`` and the version-aware path is attempted; if ``ListBucketVersions`` is refused
  too, the plain delete runs but the result is flagged :attr:`RemovalResult.unverified` (an
  ERROR also names the missing permissions): whether versions remain cannot be known, and an
  erasure caller must record that as partial, never complete. On a bucket known to be
  versioned, a refused version listing IS a failure: versions certainly remain.
* **A store without versioning is not a denial.** ``NotImplemented`` from the versioning read
  means there are no versions to find: the state is ``off``, with no ERROR per delete.
* **A missing bucket holds nothing.** ``NoSuchBucket`` is a result of 0, not a failure.
* **Deleting every version is irreversible**, including for the orphan sweep.

IAM actions this needs on native S3: ``s3:GetBucketVersioning``, ``s3:ListBucketVersions``,
``s3:DeleteObjectVersion`` (plus the ``s3:ListBucket`` / ``s3:DeleteObject`` a plain delete
already needs). See ``docs/abuse-and-takedown.md``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from minio.deleteobjects import DeleteObject
from minio.error import S3Error

logger = logging.getLogger(__name__)

#: How long one process trusts a bucket's versioning state. Short, because an operator may
#: enable versioning (object lock) on a running deployment.
VERSIONING_CACHE_SECONDS = 300

ON, OFF, UNKNOWN = "on", "off", "unknown"
_DENIED = {"AccessDenied", "AllAccessDisabled", "NotImplemented"}
_NO_VERSIONING = "NotImplemented"

UNVERIFIED_VERSIONS_ERROR = (
    "deleted, but old object versions could not be checked for "
    "(s3:GetBucketVersioning / s3:ListBucketVersions not permitted): erasure is unverified"
)

_versioning: dict[str, tuple[str, float]] = {}


class RemovalResult(int):
    """The count removed (an ``int``, so existing callers are unchanged) plus ``unverified``.

    ``unverified`` is True when the delete ran but the bucket's versions could not be read, so
    whether old versions remain is unknown. An erasure must record that as partial.
    """

    unverified: bool

    def __new__(cls, count: int, unverified: bool = False) -> RemovalResult:
        obj = super().__new__(cls, count)
        obj.unverified = unverified
        return obj


class StorageDeleteIncompleteError(Exception):
    """At least one object version survived a delete. ``remaining`` names each one."""

    def __init__(self, bucket: str, remaining: list[str], reason: str) -> None:
        self.bucket = bucket
        self.remaining = remaining
        shown = ", ".join(remaining[:20]) + (" ..." if len(remaining) > 20 else "")
        super().__init__(f"{reason} in bucket {bucket}: {len(remaining)} left ({shown})")


def forget_versioning_state(bucket: str | None = None) -> None:
    """Drop the cached versioning state (all buckets when ``bucket`` is None)."""
    if bucket is None:
        _versioning.clear()
    else:
        _versioning.pop(bucket, None)


def versioning_state(client: Any, bucket: str) -> str:
    """``on`` (enabled or suspended: versions may exist), ``off``, or ``unknown``."""
    cached = _versioning.get(bucket)
    if cached is not None and time.monotonic() - cached[1] < VERSIONING_CACHE_SECONDS:
        return cached[0]
    try:
        status = getattr(client.get_bucket_versioning(bucket), "status", None)
        state = ON if status in ("Enabled", "Suspended") else OFF
    except S3Error as e:
        if e.code not in _DENIED:
            raise
        state = OFF if e.code == _NO_VERSIONING else UNKNOWN
    if state == ON:
        _versioning[bucket] = (state, time.monotonic())
    else:
        _versioning.pop(bucket, None)
    return state


def _matches(name: str, names: set[str] | None, prefix: str | None) -> bool:
    if names is not None:
        return name in names
    return prefix is None or name.startswith(prefix)


def _versions(client: Any, bucket: str, names: set[str] | None, prefix: str | None) -> list[Any]:
    """Every version and delete marker of the selected keys."""
    found: list[Any] = []
    listings = [prefix] if names is None else sorted(names)
    for listing in listings:
        for obj in client.list_objects(
            bucket, prefix=listing, recursive=True, include_version=True
        ):
            if _matches(obj.object_name or "", names, prefix):
                found.append(obj)
    return found


def _current(client: Any, bucket: str, prefix: str) -> list[str]:
    return [o.object_name for o in client.list_objects(bucket, prefix=prefix, recursive=True)]


def _remove(client: Any, bucket: str, targets: list[DeleteObject]) -> list[str]:
    """Remove ``targets`` in one streaming call; return ``name@version: code`` per failure."""
    errors = []
    for err in client.remove_objects(bucket, iter(targets)):
        version = getattr(err, "version_id", None)
        errors.append(f"{err.name}{'@' + version if version else ''}: {err.code}")
    return errors


def remove_completely(
    client: Any,
    bucket: str,
    *,
    names: Iterable[str] | None = None,
    prefix: str | None = None,
) -> RemovalResult:
    """Delete the named keys (or every key under ``prefix``), every version of each.

    Exactly one of ``names`` / ``prefix`` is required. Absent keys are not an error.

    Returns:
        The number of objects (or object versions) removed, as a :class:`RemovalResult`
        whose ``unverified`` is True when old versions could not be checked for.

    Raises:
        StorageDeleteIncompleteError: Anything selected still exists afterwards, or could not
            be removed; every survivor is named.
    """
    if (names is None) == (prefix is None):
        raise ValueError("pass exactly one of names= or prefix=")
    if prefix is not None and not prefix:
        raise ValueError("prefix must not be empty: it would select the whole bucket")
    wanted = None if names is None else {n for n in names if n}
    if wanted is not None and not wanted:
        return RemovalResult(0)
    try:
        state = versioning_state(client, bucket)
        if state == OFF:
            keys = sorted(wanted) if wanted is not None else _current(client, bucket, prefix or "")
            errors = _remove(client, bucket, [DeleteObject(k) for k in keys])
            if errors:
                raise StorageDeleteIncompleteError(bucket, errors, "objects could not be removed")
            return RemovalResult(len(keys))
        try:
            versions = _versions(client, bucket, wanted, prefix)
        except S3Error as e:
            if e.code not in _DENIED:
                raise
            if state == ON:
                raise StorageDeleteIncompleteError(
                    bucket,
                    sorted(wanted or {f"{prefix}*"}),
                    "versions remain: listing them was refused (s3:ListBucketVersions)",
                ) from e
            keys = sorted(wanted) if wanted is not None else _current(client, bucket, prefix or "")
            errors = _remove(client, bucket, [DeleteObject(k) for k in keys])
            if errors:
                raise StorageDeleteIncompleteError(
                    bucket, errors, "objects could not be removed"
                ) from e
            logger.error(
                "Deleted %d object(s) in %s but cannot verify no old versions remain: grant "
                "s3:GetBucketVersioning and s3:ListBucketVersions",
                len(keys),
                bucket,
            )
            return RemovalResult(len(keys), unverified=True)
        errors = _remove(
            client,
            bucket,
            [DeleteObject(v.object_name, getattr(v, "version_id", None)) for v in versions],
        )
        left = [
            f"{v.object_name}@{getattr(v, 'version_id', None)}"
            for v in _versions(client, bucket, wanted, prefix)
        ]
        if errors or left:
            raise StorageDeleteIncompleteError(
                bucket, sorted(set(errors) | set(left)), "object versions remain"
            )
        return RemovalResult(len(versions))
    except (S3Error, StorageDeleteIncompleteError) as e:
        forget_versioning_state(bucket)
        if isinstance(e, S3Error) and e.code == "NoSuchBucket":
            return RemovalResult(0)
        raise
