import datetime
import io
import logging
import os
from typing import BinaryIO
from urllib.parse import quote

import urllib3
from minio import Minio
from minio.error import S3Error

from app.core.config import settings

logger = logging.getLogger(__name__)

# Disable urllib3 warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Initialize the MinIO client
minio_client = Minio(
    f"{settings.MINIO_HOST}:{settings.MINIO_PORT}",
    access_key=settings.MINIO_ROOT_USER,
    secret_key=settings.MINIO_ROOT_PASSWORD,
    secure=settings.MINIO_SECURE,
)


def ensure_bucket_exists():
    """
    Ensure the media bucket exists, creating it if necessary
    """
    try:
        if not minio_client.bucket_exists(settings.MEDIA_BUCKET_NAME):
            minio_client.make_bucket(settings.MEDIA_BUCKET_NAME)
    except S3Error as e:
        raise Exception(f"Error ensuring bucket exists: {e}") from e


def upload_file(file_content: BinaryIO, file_size: int, object_name: str, content_type: str) -> str:
    """
    Upload a file to MinIO

    Args:
        file_content: File content as a BytesIO object
        file_size: Size of the file in bytes
        object_name: Object name in MinIO (path/filename)
        content_type: MIME type of the file

    Returns:
        Object name
    """
    ensure_bucket_exists()

    try:
        minio_client.put_object(
            bucket_name=settings.MEDIA_BUCKET_NAME,
            object_name=object_name,
            data=file_content,
            length=file_size,
            content_type=content_type,
        )
        return object_name
    except S3Error as e:
        raise Exception(f"Error uploading file: {e}") from e


def download_file(object_name: str) -> tuple[io.BytesIO, int, str]:
    """
    Download a file from MinIO

    Args:
        object_name: Object name in MinIO

    Returns:
        Tuple containing:
        - File content as BytesIO object
        - File size in bytes
        - Content type (MIME type)
    """
    logger = logging.getLogger(__name__)
    try:
        # Get the file from MinIO
        response = minio_client.get_object(settings.MEDIA_BUCKET_NAME, object_name)

        # Get content type
        content_type = response.headers.get("content-type", "application/octet-stream")

        # Get content length
        content_length = int(response.headers.get("content-length", 0))

        # Read the entire file into memory
        file_content = response.read()

        # Close the response to free up resources
        response.close()
        response.release_conn()

        # Return tuple with file data as BytesIO, size, and content type
        return io.BytesIO(file_content), content_length, content_type
    except S3Error as e:
        if e.code == "NoSuchKey":
            # Missing object is a distinct, recoverable condition (callers map to 404)
            raise FileNotFoundError(f"Object not found in storage: {object_name}") from e
        logger.error(f"Error downloading file: {e}")
        raise Exception(f"Error downloading file: {e}") from e
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        raise Exception(f"Error downloading file: {e}") from e


def download_file_to_path(object_name: str, file_path: str) -> None:
    """Download a file from MinIO directly to a local path (no memory copy)."""
    minio_client.fget_object(settings.MEDIA_BUCKET_NAME, object_name, file_path)


def get_file_url(object_name: str, expires: int = 86400) -> str:
    """
    Get a presigned URL for a file in MinIO

    Args:
        object_name: Object name in MinIO
        expires: URL expiration time in seconds (default: 24 hours)

    Returns:
        Presigned URL that directly accesses the media file
    """
    try:
        # Ensure expires is a positive integer
        if not isinstance(expires, int) or expires <= 0:
            expires = 86400  # Default to 24 hours

        # Debug logging
        logger = logging.getLogger(__name__)
        logger.info(f"Getting presigned URL for {object_name} with expires={expires} seconds")

        # Create a direct URL using the get_presigned_url method
        try:
            # Try using the presigned_get_object method with a timedelta
            delta = datetime.timedelta(seconds=expires)
            url = minio_client.presigned_get_object(
                bucket_name=settings.MEDIA_BUCKET_NAME,
                object_name=object_name,
                expires=delta,
            )
        except Exception as inner_e:
            logger.info(f"First attempt failed: {inner_e}, trying alternative method")
            # If that fails, try using the raw method with timedelta
            url = minio_client.get_presigned_url(
                "GET",
                settings.MEDIA_BUCKET_NAME,
                object_name,
                expires=datetime.timedelta(seconds=expires),
            )

        # Verify the URL is valid
        if not url or not url.startswith("http"):
            raise ValueError(f"Invalid URL generated: {url}")

        # Replace the internal minio URL with the externally accessible URL
        # This is required because MinIO generates URLs with its internal hostname
        minio_internal = f"http://{settings.MINIO_HOST}:{settings.MINIO_PORT}"

        if minio_internal in url:
            # Determine the public URL for MinIO
            if settings.MINIO_PUBLIC_URL:
                # Explicit public URL configured (k8s, custom setups)
                public_base = settings.MINIO_PUBLIC_URL.rstrip("/")
            else:
                # Default: Use /s3 path which nginx proxies to MinIO API (port 9000)
                # Note: /minio/ goes to console (9001), /s3/ goes to API (9000)
                # The browser resolves /s3 relative to current origin
                public_base = "/s3"

            url = url.replace(minio_internal, public_base)
            logger.info(f"Replaced {minio_internal} with {public_base} in presigned URL")

        # Log the generated URL (truncated for security)
        logger.info(f"Generated presigned URL: {url[:50]}...")

        return url  # type: ignore[no-any-return]
    except Exception as e:
        # Catch all exceptions, not just S3Error
        logger.error(f"Error in get_file_url: {e}, type(expires)={type(expires)}")
        raise Exception(f"Error getting file URL: {e}") from e


def delete_file(object_name: str):
    """
    Delete a file from MinIO

    Args:
        object_name: Object name in MinIO
    """
    try:
        minio_client.remove_object(bucket_name=settings.MEDIA_BUCKET_NAME, object_name=object_name)
    except S3Error as e:
        raise Exception(f"Error deleting file: {e}") from e


TEMP_PREPROCESS_PREFIX = "temp/preprocess"


def _temp_audio_object_name(file_uuid: str) -> str:
    """MinIO object key for the preprocessed WAV."""
    return f"{TEMP_PREPROCESS_PREFIX}/{file_uuid}/audio.wav"


def upload_temp_audio(file_uuid: str, local_path: str) -> str:
    """Stage the preprocessed audio.wav for consumption by downstream workers.

    Phase 2 PR #4: when the shared scratch volume is available we move the
    WAV into ``/scratch/opentranscribe/{file_uuid}/audio.wav`` (one atomic
    rename, zero network I/O) and skip the MinIO round-trip entirely.
    Multi-host deployments without the scratch mount fall through to the
    original MinIO upload path, preserving backward compatibility.
    """
    _logger = logging.getLogger(__name__)
    from app.utils import scratch_volume

    # Scratch-first fast path
    if scratch_volume.is_scratch_available():
        file_size = os.path.getsize(local_path)
        dest = scratch_volume.write_audio(file_uuid, local_path)
        if dest is not None:
            _logger.info(f"Staged temp audio ({file_size / (1024 * 1024):.1f}MB) to scratch {dest}")
            return str(dest)
        _logger.warning("scratch write failed; falling back to MinIO temp upload")

    # Fallback: MinIO temp bucket (cross-host compatible). Use tuned part
    # size so multi-hundred-MB WAVs transfer in ~64 MiB chunks rather than
    # the minio-py 5 MB default.
    object_name = _temp_audio_object_name(file_uuid)
    file_size = os.path.getsize(local_path)
    with open(local_path, "rb") as f:
        minio_client.put_object(
            settings.MEDIA_BUCKET_NAME,
            object_name,
            f,
            file_size,
            content_type="audio/wav",
            part_size=PRESIGNED_PUT_PART_SIZE if file_size >= PRESIGNED_PUT_PART_SIZE else 0,
        )
    _logger.info(f"Uploaded temp audio ({file_size / (1024 * 1024):.1f}MB) to {object_name}")
    return object_name


def download_temp_audio(file_uuid: str, local_path: str) -> None:
    """Fetch the preprocessed audio.wav for a downstream worker.

    Tries the shared scratch volume first (same-host hard-link path), then
    falls back to MinIO temp when the scratch copy isn't present. Raises
    the underlying MinIO exception when neither source has the file.
    """
    from app.utils import scratch_volume

    if scratch_volume.read_audio(file_uuid, local_path):
        return

    object_name = _temp_audio_object_name(file_uuid)
    minio_client.fget_object(settings.MEDIA_BUCKET_NAME, object_name, local_path)


def temp_audio_exists(file_uuid: str) -> bool:
    """Return True when the preprocessed WAV is reachable from either source."""
    from app.utils import scratch_volume

    if scratch_volume.scratch_audio_path(file_uuid).exists():
        return True
    try:
        minio_client.stat_object(settings.MEDIA_BUCKET_NAME, _temp_audio_object_name(file_uuid))
        return True
    except Exception:
        return False


def cleanup_temp_audio(file_uuid: str) -> None:
    """Remove preprocessed audio from both scratch and MinIO temp (best-effort)."""
    _logger = logging.getLogger(__name__)
    from app.utils import scratch_volume

    # Scratch dir — best effort, shared volume may not be mounted
    scratch_volume.cleanup(file_uuid)

    # MinIO temp — always try; no-op if nothing there
    object_name = _temp_audio_object_name(file_uuid)
    try:
        minio_client.remove_object(settings.MEDIA_BUCKET_NAME, object_name)
        _logger.info(f"Cleaned up temp audio: {object_name}")
    except Exception as e:
        _logger.debug(f"Temp audio cleanup failed (non-fatal): {e}")


def get_presigned_download_url(
    object_name: str,
    *,
    bucket_name: str | None = None,
    download_filename: str | None = None,
    content_type: str | None = None,
    expires: int = settings.MEDIA_URL_EXPIRE_SECONDS,
) -> str:
    """Generate a browser-reachable presigned GET URL that forces a download.

    Adds S3 ``response-content-disposition`` / ``response-content-type`` overrides
    so the browser saves the object under ``download_filename`` with the right MIME,
    regardless of the underlying object key. The internal MinIO host is rewritten to
    the browser-facing endpoint (``MINIO_PUBLIC_URL`` or the ``/s3`` proxy path) so
    the same URL works in dev (Vite proxy) and prod (nginx).

    Args:
        object_name: Object key.
        bucket_name: Bucket holding the object (defaults to the media bucket).
        download_filename: Filename the browser should save as (Content-Disposition).
        content_type: MIME type to report.
        expires: URL lifetime in seconds.

    Returns:
        A presigned, browser-reachable download URL.
    """
    bucket = bucket_name or settings.MEDIA_BUCKET_NAME
    response_headers: dict[str, str | list[str] | tuple[str]] = {}
    if download_filename:
        # RFC 6266 / 5987: ASCII fallback + UTF-8 form for non-ASCII names.
        ascii_name = download_filename.encode("ascii", "ignore").decode("ascii") or "download"
        quoted = quote(download_filename, safe="")
        response_headers["response-content-disposition"] = (
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"
        )
    if content_type:
        response_headers["response-content-type"] = content_type

    url = minio_client.presigned_get_object(
        bucket_name=bucket,
        object_name=object_name,
        expires=datetime.timedelta(seconds=max(60, int(expires))),
        response_headers=response_headers or None,
    )
    return _rewrite_minio_host(str(url))


def get_internal_presigned_url(object_name: str, expires: int = 3600) -> str:
    """Get a presigned URL for server-to-server access (no hostname rewriting)."""
    delta = datetime.timedelta(seconds=expires)
    return str(
        minio_client.presigned_get_object(
            bucket_name=settings.MEDIA_BUCKET_NAME,
            object_name=object_name,
            expires=delta,
        )
    )


# ---------------------------------------------------------------------------
# Presigned PUT helpers (Phase 2 of the timing audit plan)
#
# The legacy upload path buffers the entire file in the API container's heap
# (up to 50 GB) before calling put_object. Presigned PUTs let the browser
# stream bytes directly to MinIO, removing the API's heap pressure and
# cutting HTTP-response latency from 19-40 s to ~100 ms for a 200 MB video.
# See docs/PIPELINE_TIMING.md for the full flow description.
# ---------------------------------------------------------------------------


# 64 MiB is the sweet spot for a 10 GB file: ~160 parts, high throughput,
# predictable memory on the MinIO server. 5 MB (minio-py default) would
# require 2000 parts for the same file.
PRESIGNED_PUT_PART_SIZE = 64 * 1024 * 1024


def _rewrite_minio_host(url: str) -> str:
    """Replace MinIO's internal hostname with the browser-accessible host.

    Mirrors the rewriting logic used by ``get_file_url`` for GET URLs so
    browsers can follow the presigned URL.
    """
    minio_internal = f"http://{settings.MINIO_HOST}:{settings.MINIO_PORT}"
    if minio_internal not in url:
        return url
    public_base = settings.MINIO_PUBLIC_URL.rstrip("/") if settings.MINIO_PUBLIC_URL else "/s3"
    return url.replace(minio_internal, public_base)


def presigned_put_url(
    object_name: str,
    expires: int = 4 * 3600,
    *,
    rewrite_host: bool = True,
) -> str:
    """Generate a presigned PUT URL for direct browser → MinIO upload.

    Args:
        object_name: Target object path in ``MEDIA_BUCKET_NAME``.
        expires: URL expiration in seconds. Defaults to 4 hours — enough for
            very large uploads on slow connections.
        rewrite_host: If True (the default) the internal MinIO hostname is
            rewritten to the browser-facing URL via ``_rewrite_minio_host``.
            Set False for server-to-server callers that can reach MinIO
            directly.

    Returns:
        A signed URL the browser can PUT bytes to with no further auth.
    """
    ensure_bucket_exists()
    delta = datetime.timedelta(seconds=max(60, int(expires)))
    url = str(
        minio_client.presigned_put_object(
            bucket_name=settings.MEDIA_BUCKET_NAME,
            object_name=object_name,
            expires=delta,
        )
    )
    if rewrite_host:
        url = _rewrite_minio_host(url)
    return url


def object_exists_and_size(object_name: str) -> int | None:
    """Return the object's size in bytes, or None if it doesn't exist.

    Used by ``complete_upload`` to verify the browser actually delivered the
    bytes to MinIO before we dispatch the transcription pipeline.
    """
    try:
        stats = minio_client.stat_object(settings.MEDIA_BUCKET_NAME, object_name)
        return int(stats.size) if stats.size is not None else None
    except Exception:
        return None


def range_read(object_name: str, offset: int, length: int) -> bytes:
    """Read ``length`` bytes starting at ``offset`` from a MinIO object.

    Used by the imohash fingerprinter to sample first/middle/last chunks
    without downloading the full file.
    """
    _logger = logging.getLogger(__name__)
    response = None
    try:
        response = minio_client.get_object(
            bucket_name=settings.MEDIA_BUCKET_NAME,
            object_name=object_name,
            offset=int(offset),
            length=int(length),
        )
        return bytes(response.read())
    finally:
        if response is not None:
            try:
                response.close()
                response.release_conn()
            except Exception as e:
                _logger.debug(f"range_read close failed for {object_name}: {e}")


def upload_file_tuned(
    file_content: BinaryIO,
    file_size: int,
    object_name: str,
    content_type: str,
) -> str:
    """Upload a file to MinIO with tuned multipart part size.

    Identical behavior to ``upload_file`` but passes ``part_size`` for
    the minio-py multipart path so large uploads use ~64 MiB parts
    instead of the 5 MB default (12.8× fewer parts → ~2× throughput).
    """
    ensure_bucket_exists()
    minio_client.put_object(
        bucket_name=settings.MEDIA_BUCKET_NAME,
        object_name=object_name,
        data=file_content,
        length=file_size,
        content_type=content_type,
        part_size=PRESIGNED_PUT_PART_SIZE if file_size >= PRESIGNED_PUT_PART_SIZE else 0,
    )
    return object_name


class MinIOService:
    """
    Class-based wrapper for MinIO operations.
    Provides an object-oriented interface to the existing functional API.
    """

    def __init__(self):
        self.client = minio_client

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
        """Get a presigned URL for object access."""
        try:
            url = self.client.presigned_get_object(
                bucket_name=bucket_name,
                object_name=object_name,
                expires=datetime.timedelta(seconds=expires),
            )
            # Fix hostname for external access
            if url.startswith("http://minio:9000"):
                from app.core.config import settings

                if settings.ENVIRONMENT == "development":
                    url = url.replace("http://minio:9000", "http://localhost:5178")
                else:
                    # For production, use environment variable for external MinIO URL
                    external_url = os.getenv("EXTERNAL_MINIO_URL", "http://localhost:5178")
                    url = url.replace("http://minio:9000", external_url)
            return url
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
        """Get object from MinIO."""
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
