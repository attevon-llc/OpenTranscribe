"""Server-side constant-time file fingerprinting with the ``imohash`` package.

Frontend SHA-256 (``uploadService.ts``) is a strong duplicate detector but
can't run when the client skips it (API uploads from scripts, future mobile
clients, third-party imports, watch-source auto-import). It also can't be used
as a reusable artifact cache key because untrusted client hashes can't be
trusted.

``imohash`` samples the first/middle/last windows of a file and combines them
with the file size into a small murmur3-based fingerprint — constant-time
regardless of file size (only ~3×``SAMPLE_SIZE`` bytes are ever read). This
module is a thin wrapper over the real `imohash` package (``hashfile`` /
``hashfileobject``); the MinIO path drives the package's sampling through a
seekable shim backed by ranged reads so we never download the full object.

This module is the **single source of truth** for the sampling parameters
(`SAMPLE_SIZE` / `SAMPLE_THRESHOLD`): every ingest path (manual upload, URL
import, watch-source import) and the recompute task call these helpers, so
fingerprints stay consistent across the whole pipeline by construction.

Use cases (not security-critical — imohash is NOT collision-resistant against
adversaries):

1. Server-side duplicate detection (manual upload, URL import, watch sources).
2. Artifact cache key for preprocessed WAV / waveform / thumbnail.
3. Reprocess short-circuit when the same file is re-ingested.
4. Content-similarity pre-filter for "did someone re-upload a trimmed version".
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import BinaryIO

from imohash import hashfile
from imohash import hashfileobject

logger = logging.getLogger(__name__)

# Sampling parameters — the single source of truth for the whole pipeline.
# These match the imohash package defaults (16 KiB sample windows, 128 KiB
# threshold below which the whole file is hashed). They are passed explicitly
# to every call so the value is pinned here and never silently changes with a
# package upgrade. Files smaller than SAMPLE_THRESHOLD (or < 4×SAMPLE_SIZE) are
# read in full by the package; larger files read three SAMPLE_SIZE windows.
SAMPLE_SIZE = 16 * 1024
SAMPLE_THRESHOLD = 128 * 1024


def compute_from_stream(stream: BinaryIO, size: int | None = None) -> str:
    """Fingerprint a file given an open binary stream.

    Args:
        stream: A seekable binary stream positioned anywhere; the package
            seeks to determine the size and the sample windows itself.
        size: Unused — accepted for backward compatibility with existing call
            sites. The package derives the size from the stream.

    Returns:
        The 32-char hex imohash digest.
    """
    return str(
        hashfileobject(
            stream,
            sample_threshhold=SAMPLE_THRESHOLD,
            sample_size=SAMPLE_SIZE,
            hexdigest=True,
        )
    )


def compute_from_path(path: str | Path) -> str | None:
    """Fingerprint a local file by path. Returns None on read error."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return str(
            hashfile(
                str(p),
                sample_threshhold=SAMPLE_THRESHOLD,
                sample_size=SAMPLE_SIZE,
                hexdigest=True,
            )
        )
    except Exception as e:
        logger.debug(f"imohash compute_from_path({path}) failed: {e}")
        return None


def compute_from_bytes(data: bytes) -> str:
    """Fingerprint a raw byte buffer — used on the legacy in-memory path."""
    return compute_from_stream(io.BytesIO(data))


class _MinioRangeReader:
    """Lazy seekable file-like shim over MinIO ranged reads.

    The ``imohash`` package only seeks to (and reads) the start, middle, and
    end sample windows, so this shim translates each ``read()`` into a single
    ``range_read()`` of just the bytes the package asks for — never the whole
    object. Implements just enough of the file protocol that
    ``hashfileobject`` needs: ``seek`` (all three whences), ``tell``, ``read``.
    """

    def __init__(self, object_name: str, size: int) -> None:
        self._object_name = object_name
        self._size = size
        self._pos = 0

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            self._pos = offset
        elif whence == os.SEEK_CUR:
            self._pos += offset
        elif whence == os.SEEK_END:
            self._pos = self._size + offset
        else:  # pragma: no cover - defensive
            raise ValueError(f"Invalid whence: {whence}")
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, n: int = -1) -> bytes:
        from app.services.minio_service import range_read

        remaining = self._size - self._pos
        if n is None or n < 0:
            n = remaining
        n = min(n, max(remaining, 0))
        if n <= 0:
            return b""
        data = range_read(self._object_name, self._pos, n)
        self._pos += len(data)
        return data


def compute_from_minio(object_name: str, size: int | None = None) -> str | None:
    """Fingerprint a MinIO object without downloading it in full.

    Drives the ``imohash`` package's sampling through a seekable shim backed by
    ranged reads, so only ~3×``SAMPLE_SIZE`` bytes transfer for large objects.
    The result is byte-for-byte identical to ``compute_from_path`` for the same
    content (verified in ``test_imohash_package_parity``).

    Args:
        object_name: Path inside the media bucket.
        size: Object size in bytes. If None we stat the object first. Callers
            that already have the size should pass it to avoid a round-trip.

    Returns:
        The 32-char hex imohash digest, or None when the object is confirmed
        missing / empty.

    Raises:
        S3Error: propagated when storage itself is unreachable (B2) — this used
            to be swallowed into the same ``None`` a genuinely absent object
            returns, so a storage outage silently degraded to "dedup found
            nothing" instead of surfacing as the outage it was. Every caller
            already treats a failure here as non-fatal to its own operation
            (see ``complete_upload._fingerprint_object``); the distinction
            matters for the log line and for callers that want to retry.
    """
    from minio.error import S3Error

    try:
        from app.services.minio_service import object_exists_and_size

        if size is None:
            size = object_exists_and_size(object_name)
        if size is None or size <= 0:
            return None

        reader = _MinioRangeReader(object_name, size)
        return compute_from_stream(reader)  # type: ignore[arg-type]  # duck-typed seekable
    except S3Error as e:
        logger.error(f"Storage outage computing imohash for {object_name}: {e}")
        raise
    except Exception as e:
        logger.debug(f"imohash compute_from_minio({object_name}) failed: {e}")
        return None
