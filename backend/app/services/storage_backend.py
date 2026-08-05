"""Object-storage backend selection: bundled MinIO or native AWS S3.

Issue #284 A1.11/A1.12. Before this module the storage client was a single
module-level ``Minio(MINIO_HOST:MINIO_PORT, root_user, root_password)`` and every
presigned URL was rewritten from that internal host onto a hardcoded ``/s3``
proxy path. Both are correct for the bundled container and wrong for AWS: S3
needs a regional endpoint, a SigV4 signing region, virtual-host addressing, and
credentials that rotate, and its presigned URLs already name a public host that
must not be rewritten.

``STORAGE_BACKEND`` picks between the two. **``minio`` is the default and its
behaviour is unchanged** — same endpoint string, same static credentials, same
``/s3`` rewrite, same expiry values. Nothing here asks a self-hosted install for
new configuration.

The client type stays ``minio.Minio`` for both backends on purpose: minio-py is a
generic S3 SDK (it switches to virtual-host addressing for AWS hostnames by
itself), so ~60 call sites keep working against one client object instead of
growing a second boto3-shaped code path. boto3 is used for exactly one thing —
bucket CORS — because minio-py has no CORS API.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:  # pragma: no cover - typing only
    from minio import Minio

from app.core.config import settings

logger = logging.getLogger(__name__)

#: AWS rejects a single PUT (including a presigned one) above 5 GiB with
#: ``EntityTooLarge``; anything bigger must go through multipart upload.
S3_SINGLE_PUT_MAX_BYTES = 5 * 1024**3

#: MinIO accepts a single PUT up to 5 TiB, so the browser-direct path stays
#: available for every object size the app allows (MAX_UPLOAD_BYTES caps at 15 GB).
MINIO_SINGLE_PUT_MAX_BYTES = 5 * 1024**4

#: Presigned URLs shorter than this are pointless and usually a caller bug.
MIN_PRESIGNED_SECONDS = 60

# Clamp warnings are logged once per requested value — a hot read path would
# otherwise emit one line per presigned URL.
_clamp_warned: set[int] = set()


def is_native_s3() -> bool:
    """Whether the configured backend is native AWS S3 rather than bundled MinIO."""
    return settings.STORAGE_BACKEND.strip().lower() == "s3"


def storage_region() -> str:
    """SigV4 signing region for the native-S3 backend."""
    return settings.S3_REGION.strip() or "us-east-1"


def _s3_endpoint() -> tuple[str, bool]:
    """Resolve ``(host[:port], secure)`` for the native-S3 backend.

    An empty ``S3_ENDPOINT_URL`` derives the regional AWS endpoint, which is what
    makes minio-py pick virtual-host addressing and the right signing region.
    """
    raw = settings.S3_ENDPOINT_URL.strip()
    if not raw:
        return f"s3.{storage_region()}.amazonaws.com", True
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc or parsed.path
    return host.strip("/"), parsed.scheme != "http"


def storage_endpoint() -> tuple[str, bool]:
    """Resolve ``(host[:port], secure)`` for the configured backend."""
    if is_native_s3():
        return _s3_endpoint()
    return f"{settings.MINIO_HOST}:{settings.MINIO_PORT}", settings.MINIO_SECURE


def _iam_role_credentials():
    """AWS credential chain: env vars → IRSA/ECS/EC2 instance metadata.

    ``IamAwsProvider`` covers the web-identity token file (EKS/IRSA), the ECS task
    role URI, and EC2 IMDS, and it refreshes on expiry — which is the whole point
    of running without static keys.
    """
    from minio.credentials import ChainedProvider
    from minio.credentials import EnvAWSProvider
    from minio.credentials import IamAwsProvider

    return ChainedProvider([EnvAWSProvider(), IamAwsProvider()])


def build_storage_client() -> Minio:
    """Construct the storage client for the configured backend.

    Returns:
        A ``minio.Minio`` client. For ``STORAGE_BACKEND=minio`` this is byte-for-byte
        the client the app has always built.
    """
    from minio import Minio

    endpoint, secure = storage_endpoint()

    if not is_native_s3():
        return Minio(
            endpoint,
            access_key=settings.MINIO_ROOT_USER,
            secret_key=settings.MINIO_ROOT_PASSWORD,
            secure=secure,
        )

    region = storage_region()
    if settings.S3_USE_IAM_ROLE:
        logger.info(f"Storage backend: native S3 at {endpoint} (region {region}, IAM role chain)")
        return Minio(endpoint, secure=secure, region=region, credentials=_iam_role_credentials())

    if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY:
        logger.error(
            "STORAGE_BACKEND=s3 with S3_USE_IAM_ROLE=false but AWS_ACCESS_KEY_ID / "
            "AWS_SECRET_ACCESS_KEY are not set — every S3 request will be unsigned"
        )
    logger.info(f"Storage backend: native S3 at {endpoint} (region {region}, static keys)")
    return Minio(
        endpoint,
        access_key=settings.AWS_ACCESS_KEY_ID,
        secret_key=settings.AWS_SECRET_ACCESS_KEY,
        secure=secure,
        region=region,
    )


def internal_endpoint_prefixes() -> tuple[str, ...]:
    """URL prefixes that identify a signed URL as pointing at the internal endpoint.

    Both schemes are returned deliberately. The original rewrite hardcoded
    ``http://``, so a ``MINIO_SECURE=true`` deployment silently shipped the internal
    hostname to the browser; matching either scheme fixes that without changing the
    plain-HTTP result.
    """
    host, _ = storage_endpoint()
    return (f"http://{host}", f"https://{host}")


def public_base_url() -> str | None:
    """Browser-facing base URL that replaces the internal endpoint in signed URLs.

    Returns:
        The configured public base, ``/s3`` for the MinIO default (the nginx/Vite
        proxy path the frontend has always used), or ``None`` when the signed URL
        must be handed to the browser untouched — the native-S3 case, where the
        signed host is already public and rewriting it would break the signature's
        host binding.
    """
    explicit = (settings.STORAGE_PUBLIC_URL or settings.MINIO_PUBLIC_URL).strip()
    if explicit:
        return explicit.rstrip("/")
    return None if is_native_s3() else "/s3"


def rewrite_public_host(url: str) -> str:
    """Replace the internal storage endpoint in *url* with the browser-facing base."""
    base = public_base_url()
    if base is None:
        return url
    for prefix in internal_endpoint_prefixes():
        if prefix in url:
            return url.replace(prefix, base)
    return url


def max_presigned_seconds() -> int:
    """Effective ceiling for presigned-URL lifetime, in seconds."""
    return max(MIN_PRESIGNED_SECONDS, settings.PRESIGNED_URL_MAX_SECONDS)


def clamp_presigned_expiry(expires: int | None) -> int:
    """Clamp a requested presigned-URL lifetime into the supported range.

    A presigned URL cannot outlive the credentials that signed it. Under an IAM
    role those are STS session credentials (IMDS/IRSA/ECS), so a 24 h URL starts
    returning 403 as soon as the session rotates — the URL looks valid and simply
    stops working. Clamping keeps the failure mode "shorter than asked for" instead
    of "silently dead half-way through".

    Args:
        expires: Requested lifetime in seconds. Non-positive or non-integer values
            fall back to the ceiling, matching the previous default-argument behaviour.

    Returns:
        A lifetime between :data:`MIN_PRESIGNED_SECONDS` and :func:`max_presigned_seconds`.
    """
    ceiling = max_presigned_seconds()
    try:
        requested = int(expires)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ceiling
    if requested <= 0:
        return ceiling
    if requested > ceiling:
        if requested not in _clamp_warned:
            _clamp_warned.add(requested)
            logger.warning(
                f"Presigned URL lifetime {requested}s exceeds PRESIGNED_URL_MAX_SECONDS "
                f"({ceiling}s) and was clamped. Signed URLs cannot outlive the credentials "
                "that signed them; IAM-role (STS) sessions expire well before 24h."
            )
        return ceiling
    return max(MIN_PRESIGNED_SECONDS, requested)


def single_put_max_bytes() -> int:
    """Largest object the backend accepts in one PUT (presigned or otherwise)."""
    return S3_SINGLE_PUT_MAX_BYTES if is_native_s3() else MINIO_SINGLE_PUT_MAX_BYTES


def supports_single_put(size_bytes: int | None) -> bool:
    """Whether an object of *size_bytes* can be uploaded with one presigned PUT.

    ``False`` means the caller must not hand the browser a presigned PUT URL: S3
    would reject the request with ``EntityTooLarge`` after the client had already
    streamed gigabytes. The upload then falls back to the API-mediated path, which
    spools to disk and writes through minio-py's multipart uploader (64 MiB parts,
    so ~640 GB of headroom — far past the 15 GB application ceiling).

    An unknown size is treated as acceptable: the completion step re-checks the
    size the backend actually observed.
    """
    if size_bytes is None:
        return True
    try:
        return int(size_bytes) <= single_put_max_bytes()
    except (TypeError, ValueError):
        return True


def cors_allowed_origins() -> list[str]:
    """Origins permitted to upload directly to the bucket from a browser."""
    configured = [o.strip() for o in settings.S3_CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
    return configured or [o for o in settings.CORS_ORIGINS if o]


def ensure_bucket_cors(bucket_name: str) -> bool:
    """Apply the browser-upload CORS policy to *bucket_name*. Best-effort.

    Only runs for the native-S3 backend and only when ``S3_CONFIGURE_BUCKET_CORS``
    is on: MinIO already answers every origin, and silently rewriting a shared
    bucket's CORS configuration is destructive. boto3 is used because minio-py
    exposes no CORS API.

    Returns:
        True if a CORS configuration was written, False if skipped or on failure.
    """
    if not is_native_s3() or not settings.S3_CONFIGURE_BUCKET_CORS:
        return False

    origins = cors_allowed_origins()
    if not origins:
        logger.warning("S3_CONFIGURE_BUCKET_CORS=true but no allowed origins are configured")
        return False

    try:
        import boto3

        endpoint, secure = storage_endpoint()
        client = boto3.client(
            "s3",
            region_name=storage_region(),
            endpoint_url=f"{'https' if secure else 'http'}://{endpoint}",
        )
        client.put_bucket_cors(
            Bucket=bucket_name,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedMethods": ["GET", "HEAD", "PUT"],
                        "AllowedOrigins": origins,
                        "AllowedHeaders": ["*"],
                        # ETag is required for multipart completion; the Range/Length
                        # headers keep <video> byte-range playback working cross-origin.
                        "ExposeHeaders": [
                            "ETag",
                            "Content-Length",
                            "Content-Range",
                            "Accept-Ranges",
                        ],
                        "MaxAgeSeconds": 3000,
                    }
                ]
            },
        )
        logger.info(f"Configured CORS on bucket {bucket_name} for origins: {origins}")
        return True
    except Exception as e:  # noqa: BLE001 — CORS setup must never block startup
        logger.warning(f"Could not configure CORS on bucket {bucket_name}: {e}")
        return False
