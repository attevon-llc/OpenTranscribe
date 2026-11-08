"""How large a request the API accepts: the upload ceiling and the general body ceiling.

Split out of ``app.core.config`` (at its size cap) as one cohesive concern. ``Settings``
inherits these fields; the env source, ``env_ignore_empty`` and ``extra="ignore"`` come from
``Settings``' own ``model_config``, and this class is never instantiated on its own.
"""

from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings


class RequestLimitSettings(BaseSettings):
    # Global server-side upload ceiling, in bytes (issue #284 A0.12). Matches the 15 GB
    # the UI advertises and the yt-dlp `max_filesize`. Before this there was NO
    # server-side ceiling in community: the only limit lived in the browser, so a client
    # could skip the UI and PUT an arbitrarily large object to the presigned URL.
    # Enforced at prepare (declared size) AND complete (the size MinIO observed).
    # Set 0 to disable — only sensible on a trusted single-user install.
    MAX_UPLOAD_BYTES: int | None = 15 * 1024 * 1024 * 1024

    @field_validator("MAX_UPLOAD_BYTES", mode="after")
    @classmethod
    def _normalize_max_upload_bytes(cls, v: int | None) -> int | None:
        """An explicit 0 still means "disabled" (falsy -> None)."""
        return v or None

    # Ceiling on any request body the API reads, in bytes. FastAPI reads and parses a
    # JSON or form body BEFORE dependencies (authentication included) run, so without a
    # ceiling an anonymous caller could make the process buffer an arbitrarily large
    # body. The two file-carrying routes (POST /api/files and the speaker avatar) raise
    # it to their own limits in app/middleware/upload_limit.py; everything else gets
    # this. The bundled nginx configs cap /api/ at 10m, so raising this above 10 MiB
    # also needs their client_max_body_size raised.
    MAX_REQUEST_BODY_BYTES: int = Field(default=1024 * 1024, ge=1024)
