"""Abstract client interface + factory for watch sources.

Each source type (local folder, S3-compatible bucket, SMB/CIFS share) implements
``BaseWatchSourceClient``. The Celery scan/import tasks and the processing
pipeline talk to this interface only, so adding a source type is purely additive.
``create_client()`` decrypts stored credentials and returns the right client.
"""

from __future__ import annotations

import logging
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.watch_source import WatchSource

logger = logging.getLogger(__name__)


@dataclass
class RemoteFileInfo:
    """A file discovered in a watch source.

    ``path`` is the source-relative path used as the dedup key and passed back
    to ``download_file``. For local sources it is the absolute on-disk path.
    """

    path: str
    name: str
    size: int
    modified_time: datetime | None = None


class BaseWatchSourceClient(ABC):
    """Common interface every watch-source backend implements."""

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """Return ``(ok, message)`` for the configured connection."""

    @abstractmethod
    def list_files(
        self,
        extensions: list[str] | None = None,
        recursive: bool = True,
        min_modified: datetime | None = None,
    ) -> list[RemoteFileInfo]:
        """List candidate media files, filtered by extension / age."""

    @abstractmethod
    def download_file(self, remote_path: str, local_path: str) -> int:
        """Copy ``remote_path`` to ``local_path``; return bytes written.

        Local sources may return the in-place size without copying.
        """

    @abstractmethod
    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """Write a local file back to the source (stitched output)."""

    def close(self) -> None:  # noqa: B027 - optional override
        """Release any held connection/session. Default: no-op."""

    def __enter__(self) -> BaseWatchSourceClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _assert_safe_watch_target(host_or_url: str | None, *, source_type: str) -> None:
    """Refuse a watch source pointing at internal infrastructure (issue #284 A0.10).

    Any user can create a watch source, and its ``s3_endpoint_url`` / SMB ``server`` is
    fetched server-side — the same SSRF class as the LLM/ASR test-connection endpoints,
    and semi-blind because the connection error surfaces in the scan status. Validated
    here, at the single construction point, so every caller is covered.

    ``WATCH_ALLOW_PRIVATE_ENDPOINTS`` re-enables private targets, which is the norm for a
    self-hosted NAS on the LAN; it must stay off on a multi-tenant deployment.

    Raises:
        ValueError: if the target must not be contacted.
    """
    if not host_or_url:
        return

    from app.core.config import settings
    from app.utils.url_validation import is_safe_url

    if settings.WATCH_ALLOW_PRIVATE_ENDPOINTS:
        return

    # SMB gives a bare host; wrap it so the same resolver/range checks apply.
    candidate = host_or_url if "://" in host_or_url else f"http://{host_or_url}"
    safe, reason = is_safe_url(candidate)
    if not safe:
        logger.warning("Blocked %s watch source target %r: %s", source_type, host_or_url, reason)
        raise ValueError(
            "The configured server address could not be used. It must be a publicly "
            "reachable address, or set WATCH_ALLOW_PRIVATE_ENDPOINTS=true for a "
            "private/LAN source."
        )


def create_client(source: WatchSource) -> BaseWatchSourceClient:
    """Build the right client for ``source.source_type`` with decrypted creds.

    Raises:
        ValueError: unknown source type.
        RuntimeError: optional dependency for the source type is not installed.
    """
    from app.utils.encryption import decrypt_api_key

    source_type = source.source_type
    if source_type == "local":
        from app.services.watch_sources.local_client import LocalWatchClient

        return LocalWatchClient(source)

    if source_type == "s3":
        from app.services.watch_sources.s3_client import S3WatchClient

        secret = (
            decrypt_api_key(source.encrypted_s3_secret_key)
            if source.encrypted_s3_secret_key
            else None
        )
        _assert_safe_watch_target(source.s3_endpoint_url, source_type="s3")
        return S3WatchClient(
            endpoint_url=source.s3_endpoint_url,
            bucket=source.s3_bucket_name,
            prefix=source.s3_prefix or "",
            region=source.s3_region,
            access_key_id=source.s3_access_key_id,
            secret_key=secret,
            use_ssl=source.s3_use_ssl,
        )

    if source_type == "smb":
        from app.services.watch_sources.smb_client import SMBWatchClient

        password = (
            decrypt_api_key(source.encrypted_smb_password)
            if source.encrypted_smb_password
            else None
        )
        _assert_safe_watch_target(source.smb_server, source_type="smb")
        return SMBWatchClient(
            server=source.smb_server,
            share=source.smb_share,
            path=source.smb_path or "/",
            username=source.smb_username,
            password=password,
            domain=source.smb_domain,
            port=source.smb_port or 445,
        )

    raise ValueError(f"Unknown watch source type: {source_type!r}")


def parse_extensions(csv: str | None) -> list[str] | None:
    """Parse a CSV of file extensions into a normalized lowercase list.

    ``".MP4, mp3"`` → ``[".mp4", ".mp3"]``. Returns None when empty (= all).
    """
    if not csv or not csv.strip():
        return None
    out: list[str] = []
    for raw in csv.split(","):
        ext = raw.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        out.append(ext)
    return out or None
