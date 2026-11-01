"""Startup check (native S3): can this identity do what a takedown needs?

A takedown tags the media objects (``s3:GetObjectTagging`` + ``s3:PutObjectTagging`` on the
media bucket) and deletes the cache copies (``s3:ListBucket`` + ``s3:DeleteObject`` on the
cache bucket). Without them every takedown reports an incomplete revocation; this finds out at
boot. Non-fatal like every storage posture check: ERROR naming the missing actions, WARNING
when inconclusive, INFO when verified. It never blocks or fails startup.

What it costs the operator, stated plainly (also in ``docs/abuse-and-takedown.md``):

* One throwaway object per bucket per boot, under ``_opentranscribe/probes/``, with a unique
  key so replicas starting together never collide; it is deleted again at once.
* On a **versioned** bucket each boot leaves a noncurrent version and a delete marker behind
  (a lifecycle rule on ``_opentranscribe/`` cleans them up); under **Object Lock** default
  retention the probe object cannot be deleted until it expires.
* It writes to ``_opentranscribe/probes/``, so a **prefix-scoped** IAM policy (granting
  tagging only under ``user_*/``, say) can make it report a permission missing that media
  keys do have, or the reverse. The message says so.

``STORAGE_TAG_PROBE_ENABLED=false`` turns it off. It uses the bounded, no-retry probe client
(``minio_service.get_probe_client``), never the shared client.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import uuid
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.constants import STORAGE_QUARANTINE_TAG_KEY
from app.services import storage_backend
from app.services import storage_presign_identity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from minio import Minio

logger = logging.getLogger(__name__)

PROBE_PREFIX = "_opentranscribe/probes/"

_PREFIX_CAVEAT = (
    f"The probe writes under '{PROBE_PREFIX}'; if your IAM policy is scoped to key prefixes "
    "this result may not match what media keys are allowed."
)


@dataclasses.dataclass(frozen=True)
class TaggingProbe:
    """Result of the startup probe.

    Attributes:
        verified: Every exercised call succeeded.
        missing_permissions: ``"<action> on <bucket>"`` for each ``AccessDenied``.
        error: A non-permission failure that left the result inconclusive, else None.
    """

    verified: bool
    missing_permissions: tuple[str, ...] = ()
    error: str | None = None


def probe_key() -> str:
    """A key unique to this boot, so concurrent replicas never touch each other's probe."""
    return f"{PROBE_PREFIX}takedown-probe-{uuid.uuid4().hex}"


def _code(error: Exception) -> str:
    return str(getattr(error, "code", None) or type(error).__name__)


def _run(client: Minio, bucket: str, key: str, steps) -> tuple[list[str], str | None]:
    """Write ``key``, run ``steps`` ((action, call) pairs), delete ``key``."""
    from minio.error import S3Error

    body = b"opentranscribe takedown probe"
    try:
        client.put_object(bucket, key, io.BytesIO(body), len(body))
    except Exception as e:  # noqa: BLE001 — a probe failure is reported, never raised at boot
        return [], f"probe object write to '{bucket}' failed ({_code(e)})"
    missing: list[str] = []
    error: str | None = None
    for action, call in steps:
        try:
            call()
        except S3Error as e:
            label = f"{action} on {bucket}"
            if e.code == "AccessDenied":
                if label not in missing:
                    missing.append(label)
            elif error is None:
                error = f"{action} on {bucket} failed ({e.code})"
        except Exception as e:  # noqa: BLE001 — reported, never raised at boot
            if error is None:
                error = f"{action} on {bucket} failed ({_code(e)})"
    try:
        client.remove_object(bucket, key)
    except Exception as e:  # noqa: BLE001 — a leftover probe object is harmless; say so
        if _code(e) == "AccessDenied":
            label = f"s3:DeleteObject on {bucket}"
            if label not in missing:
                missing.append(label)
        logger.warning(
            f"Could not delete the takedown probe object '{key}' in '{bucket}' ({_code(e)}); "
            f"it is a {len(body)}-byte object, safe to delete by hand."
        )
    return missing, error


def probe_takedown_permissions(
    client: Minio, media_bucket: str, cache_bucket: str | None = None
) -> TaggingProbe:
    """Exercise a takedown's storage calls on throwaway objects. Never raises."""
    from minio.commonconfig import Tags

    tagged = Tags.new_object_tags()
    tagged[STORAGE_QUARANTINE_TAG_KEY] = storage_presign_identity.QUARANTINE_TAG_VALUE
    key = probe_key()
    missing, error = _run(
        client,
        media_bucket,
        key,
        (
            ("s3:GetObjectTagging", lambda: client.get_object_tags(media_bucket, key)),
            ("s3:PutObjectTagging", lambda: client.set_object_tags(media_bucket, key, tagged)),
            (
                "s3:PutObjectTagging",
                lambda: client.set_object_tags(media_bucket, key, Tags.new_object_tags()),
            ),
        ),
    )
    if cache_bucket and cache_bucket != media_bucket:
        cache_key = probe_key()
        cache_missing, cache_error = _run(
            client,
            cache_bucket,
            cache_key,
            (("s3:ListBucket", lambda: list(client.list_objects(cache_bucket, prefix=cache_key))),),
        )
        missing += [m for m in cache_missing if m not in missing]
        error = error or cache_error
    return TaggingProbe(not missing and error is None, tuple(missing), error)


def report_native_s3_tagging_posture(client: Minio | None = None) -> TaggingProbe | None:
    """Log, once at startup on native S3, whether a takedown's storage calls will succeed.

    Args:
        client: Override for tests; defaults to the bounded probe client.

    Returns:
        The probe result, or None when ``STORAGE_TAG_PROBE_ENABLED`` is off.
    """
    if not settings.STORAGE_TAG_PROBE_ENABLED:
        logger.info(
            "STORAGE_TAG_PROBE_ENABLED=false: not checking takedown storage permissions at "
            "startup; each takedown still reports its own outcome."
        )
        return None
    if client is None:
        from app.services.minio_service import get_probe_client

        client = get_probe_client()
    probe = probe_takedown_permissions(
        client, settings.MEDIA_BUCKET_NAME, settings.CACHE_BUCKET_NAME
    )
    window = storage_backend.clamp_presigned_expiry(settings.MEDIA_URL_EXPIRE_SECONDS)
    if probe.missing_permissions:
        logger.error(
            f"STORAGE_BACKEND=s3: the storage identity is missing "
            f"{', '.join(probe.missing_permissions)} — quarantine CANNOT revoke already-issued "
            f"presigned media URLs (they keep working for up to {window}s), and every takedown "
            f"will report media_revocation.complete=false. Grant them; see "
            f"docs/abuse-and-takedown.md. {_PREFIX_CAVEAT}"
        )
    elif probe.error:
        logger.warning(
            f"STORAGE_BACKEND=s3: could not verify the takedown storage permissions "
            f"({probe.error}). Takedowns report the real outcome in media_revocation. "
            f"{_PREFIX_CAVEAT}"
        )
    else:
        logger.info(
            "STORAGE_BACKEND=s3: the storage identity can tag media objects and remove cache "
            "copies — takedowns will revoke issued URLs where the bucket policy enforces it."
        )
    return probe
