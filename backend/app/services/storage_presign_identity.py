"""Dedicated, least-privilege MinIO identity for browser-facing presigned GETs (issue #907).

A presigned MinIO URL is valid for ``MEDIA_URL_EXPIRE_SECONDS`` (6 h default). If a file is
quarantined via takedown while that window is still open, the already-minted URL keeps
working — a takedown revokes read *access* going forward, not a signature already handed to a
browser. Four cheaper mitigations were considered and rejected (measured, not re-litigated
here — see ``docs/abuse-and-takedown.md``): shortening the TTL breaks in-flight playback,
a bucket-policy Deny is bypassed by root-signed URLs, rotating the signing credential revokes
every URL including bystanders', and renaming the object key is blocked by legal-hold and
isn't cleanly reversible.

The fix: presign browser-facing GETs with a dedicated, non-root MinIO **service account**
whose inline policy Denies ``s3:GetObject`` on any object carrying the quarantine tag
(``app.core.constants.STORAGE_QUARANTINE_TAG_KEY``). A URL minted *before* the object was
tagged 403s the instant the tag is applied — same URL, same signature, no new mint. Removing
the tag restores the identical pre-existing URL to 200. All measured against the repo's
pinned MinIO image; see the module functions below for the specific constraints that were
also measured (case sensitivity, the write/read action split, least privilege).

**MinIO-only.** There is no admin API on native AWS S3 (``STORAGE_BACKEND=s3``): the tag is
still written (harmless) but nothing enforces it there — see
:func:`ensure_presign_identity`'s docstring and ``docs/abuse-and-takedown.md`` for the
equivalent bucket-policy JSON an S3 operator would attach by hand.

**Fails open, visibly.** If the service account cannot be created or refreshed (admin API
unreachable, a non-MinIO S3-compatible backend, ``STORAGE_PRESIGN_IDENTITY_ENABLED=false``),
presigning falls back to the root client — today's exact pre-#907 behavior, no regression, but
silently inert unless something logs it. :func:`presign_client` logs ERROR once per process on
that fallback.

**Admin bypass, on purpose (decision, not a bug):** admins presign through this same
restricted identity, so an admin also gets 403 on a quarantined file's media URL — the same
403 a normal user (already 404'd off every list surface) would encounter if they had somehow
kept a URL. We do not carve out a root-signed bypass for admin review, because that would
reopen a signed URL that outlives the review window. Admins review the transcript text
instead, which this change does not touch.

**Reads only.** This revokes future reads of a presigned URL; it cannot un-download bytes a
client already fetched before the tag was applied.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from minio import Minio

from app.core.config import settings
from app.core.constants import STORAGE_QUARANTINE_TAG_KEY
from app.services import storage_backend

logger = logging.getLogger(__name__)

#: The value the presign identity's policy Deny checks for with StringEquals.
#: ⚠️ MEASURED: StringEquals is CASE-SENSITIVE — "TRUE" does NOT trigger the Deny.
#: Every tagger (``minio_service.set_object_quarantine_tag``) must write exactly
#: this lowercase string; do not "normalize" it to a bool-ish value elsewhere.
QUARANTINE_TAG_VALUE = "true"

# The buckets covered by the presign identity's policy. Both are GET-presigned by
# browser-facing helpers today (media originals + the processed-videos derived cache).
_PROCESSED_VIDEOS_BUCKET = "processed-videos"

_SERVICE_ACCOUNT_NAME = "ot-presign"
_SERVICE_ACCOUNT_DESCRIPTION = "OpenTranscribe presigned-GET identity (issue #907)"

# ``XMinioIAMServiceAccountNotAllowed`` is what MinIO's admin API returns for
# "a service account with this access key already exists" — measured against the
# repo's pinned image. There is no dedicated exception subtype for it in minio-py;
# it surfaces as a MinioAdminException whose body/message contains this string.
_ALREADY_EXISTS_MARKER = "XMinioIAMServiceAccountNotAllowed"

# Module-global memoization: one admin round trip (or one no-op decision) per
# process, not one per presigned URL. Celery workers pick this up the same way
# the API process does — via the first call to presign_client()/ensure_presign_identity()
# in that process — rather than through celery.py's worker-init hook, which forbids
# network calls.
_ensure_attempted = False
_ensure_result = False
_restricted_client: Minio | None = None
_fallback_error_logged = False


def derive_presign_credentials() -> tuple[str, str]:
    """Deterministically derive the presign identity's access/secret key pair.

    No new table and no cross-process coordination: every process (API, each Celery
    worker) derives the SAME pair from ``JWT_SECRET_KEY``, so they always agree on the
    identity to create/use. Rotating ``JWT_SECRET_KEY`` produces a new derived identity
    that :func:`ensure_presign_identity` provisions on next start — the old service
    account is simply abandoned (MinIO does not need it deleted for the new one to work).

    Returns:
        ``(access_key, secret_key)`` — 20 and 40 base32 characters respectively.
    """
    secret = settings.JWT_SECRET_KEY.encode()
    access = (
        base64.b32encode(
            hmac.new(secret, b"opentranscribe/minio/presign/access-key/v1", hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")[:20]
    )
    secret_key = (
        base64.b32encode(
            hmac.new(secret, b"opentranscribe/minio/presign/secret-key/v1", hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")[:40]
    )
    return access, secret_key


def build_presign_policy(buckets: list[str], tag_key: str) -> dict:
    """Build the presign identity's IAM policy JSON.

    Three statements, in order:

    1. ``Allow s3:GetBucketLocation`` on the BUCKET-level ARNs. Presigning itself needs
       this — minio-py's internal ``_get_region()`` call raises ``AccessDenied`` before a
       URL is even produced without it (measured; this bit the research probe on the
       first attempt).
    2. ``Allow s3:GetObject`` on the OBJECT-level ARNs (``bucket/*``).
    3. ``Deny s3:GetObject`` on the same object ARNs, conditioned on
       ``StringEquals {s3:ExistingObjectTag/<tag_key>: "true"}`` — the quarantine kill
       switch. A URL minted before the tag was applied 403s the instant the tag lands.

    Deliberately excluded (both measured, both load-bearing for least privilege):

    - No ``s3:PutObject`` in the Deny's Action list — MinIO rejects an
      ``ExistingObjectTag`` condition on a write action and the whole policy-creation
      call 400s.
    - No ``s3:PutObjectTagging`` / ``s3:DeleteObjectTagging`` / ``s3:ListBucket`` anywhere
      — the identity that can be Denied by a tag must not be able to clear that tag
      itself, and does not need to enumerate the bucket to presign a known key.

    Args:
        buckets: Bucket names the identity may presign GETs against.
        tag_key: The object-tag key the Deny condition checks (``STORAGE_QUARANTINE_TAG_KEY``).

    Returns:
        The policy as a plain dict, ready for ``MinioAdmin.add_service_account``/
        ``update_service_account``'s ``policy=`` kwarg.
    """
    bucket_arns = [f"arn:aws:s3:::{b}" for b in buckets]
    object_arns = [f"arn:aws:s3:::{b}/*" for b in buckets]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation"],
                "Resource": bucket_arns,
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": object_arns,
            },
            {
                "Effect": "Deny",
                "Action": ["s3:GetObject"],
                "Resource": object_arns,
                "Condition": {
                    "StringEquals": {f"s3:ExistingObjectTag/{tag_key}": QUARANTINE_TAG_VALUE}
                },
            },
        ],
    }


def _presign_buckets() -> list[str]:
    """Buckets the presign identity's policy must cover."""
    return [settings.MEDIA_BUCKET_NAME, _PROCESSED_VIDEOS_BUCKET]


def presign_identity_available() -> bool:
    """Whether the restricted presign identity is usable in this process.

    Triggers :func:`ensure_presign_identity` on first call (per process) and returns
    its cached result thereafter. Never raises.
    """
    return ensure_presign_identity()


def ensure_presign_identity() -> bool:
    """Idempotently create (or refresh the policy of) the restricted presign identity.

    No-op — returns ``False`` immediately, with no admin call attempted — when
    ``STORAGE_PRESIGN_IDENTITY_ENABLED`` is off or the configured backend is native S3
    (:func:`storage_backend.is_native_s3`): there is no MinIO admin API on AWS, so the
    quarantine tag is still written by ``minio_service.set_object_quarantine_tag`` (harmless)
    but nothing here can enforce it. An operator on native S3 attaches the equivalent
    bucket-policy JSON by hand — see ``docs/abuse-and-takedown.md``.

    Otherwise builds a ``MinioAdmin`` client from the root credentials
    (``MINIO_ROOT_USER``/``MINIO_ROOT_PASSWORD``) and the configured storage endpoint, and
    tries ``add_service_account``. If the account already exists (MinIO's admin API answers
    ``XMinioIAMServiceAccountNotAllowed``), refreshes its policy with
    ``update_service_account`` instead — this is how a ``STORAGE_QUARANTINE_TAG_KEY`` or
    bucket-list change reaches an already-provisioned identity.

    Never raises: any failure (admin API unreachable, unexpected response, a
    S3-compatible-but-not-MinIO backend with no admin API) is logged and reported as
    ``False`` so the caller falls back to the root client.

    Returns:
        True if the restricted identity is ready to presign with, False otherwise.
    """
    global _ensure_attempted, _ensure_result

    if _ensure_attempted:
        return _ensure_result
    _ensure_attempted = True

    if not settings.STORAGE_PRESIGN_IDENTITY_ENABLED:
        logger.info(
            "Presign identity disabled (STORAGE_PRESIGN_IDENTITY_ENABLED=false); "
            "presigned media URLs will be signed with the root credential."
        )
        _ensure_result = False
        return False

    if storage_backend.is_native_s3():
        logger.info(
            "Presign identity skipped: STORAGE_BACKEND=s3 has no MinIO admin API. "
            "Quarantine tags are still written to objects; see "
            "docs/abuse-and-takedown.md for the equivalent bucket-policy JSON."
        )
        _ensure_result = False
        return False

    try:
        from minio.credentials import StaticProvider
        from minio.minioadmin import MinioAdmin
        from minio.minioadmin import MinioAdminException

        endpoint, secure = storage_backend.storage_endpoint()
        admin = MinioAdmin(
            endpoint=endpoint,
            credentials=StaticProvider(settings.MINIO_ROOT_USER, settings.MINIO_ROOT_PASSWORD),
            secure=secure,
        )
        access_key, secret_key = derive_presign_credentials()
        policy = build_presign_policy(_presign_buckets(), STORAGE_QUARANTINE_TAG_KEY)

        try:
            admin.add_service_account(
                access_key=access_key,
                secret_key=secret_key,
                name=_SERVICE_ACCOUNT_NAME,
                description=_SERVICE_ACCOUNT_DESCRIPTION,
                policy=policy,
            )
            logger.info(f"Created MinIO presign service account '{_SERVICE_ACCOUNT_NAME}'")
        except MinioAdminException as e:
            if _ALREADY_EXISTS_MARKER in str(e):
                admin.update_service_account(access_key=access_key, policy=policy)
                logger.info(
                    f"Refreshed policy for existing MinIO presign service account "
                    f"'{_SERVICE_ACCOUNT_NAME}'"
                )
            else:
                raise

        _ensure_result = True
        return True
    except Exception as e:  # noqa: BLE001 — must never break presigning
        logger.error(
            f"Could not provision the restricted presign identity — falling back to the "
            f"root client for presigned media URLs (quarantine will not revoke "
            f"already-minted URLs while this persists): {e}"
        )
        _ensure_result = False
        return False


def presign_client() -> Minio:
    """The client browser-facing GET-presign helpers should sign with.

    Lazily ensures the restricted identity exists (memoized per process — see
    :func:`ensure_presign_identity`). On success, returns a memoized restricted
    ``Minio`` client built from the derived credentials. On failure, logs ERROR
    **once** per process (not once per call) and returns the existing root
    ``minio_service.minio_client`` — presigning must never be blocked by this feature.

    Returns:
        A ``minio.Minio`` client: the restricted presign identity's, or the root
        client as a fallback.
    """
    global _restricted_client, _fallback_error_logged

    if not ensure_presign_identity():
        if not _fallback_error_logged:
            logger.error(
                "Presigning media URLs with the ROOT MinIO credential (presign identity "
                "unavailable) — quarantine will NOT revoke already-minted URLs until this "
                "is resolved. See the preceding log line for why provisioning failed."
            )
            _fallback_error_logged = True
        from app.services.minio_service import minio_client

        return minio_client

    if _restricted_client is None:
        from minio import Minio

        endpoint, secure = storage_backend.storage_endpoint()
        access_key, secret_key = derive_presign_credentials()
        _restricted_client = Minio(
            endpoint, access_key=access_key, secret_key=secret_key, secure=secure
        )

    return _restricted_client
