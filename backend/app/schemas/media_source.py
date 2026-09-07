"""Pydantic schemas for user media source settings."""

import logging
import re
from datetime import datetime

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

from app.utils.url_validation import is_safe_url

logger = logging.getLogger(__name__)

ALLOWED_PROVIDER_TYPES = {"mediacms"}

#: Syntax only — an LDH hostname, optionally dotted. Says nothing about whether the host
#: is safe to fetch; :func:`app.utils.url_validation.is_safe_url` answers that.
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*$")


def _validated_hostname(v: str) -> str:
    """Normalise ``host`` / ``host:port`` and refuse anything not publicly reachable.

    This value is **not** inert configuration: it becomes an allowed host for MediaCMS
    ingestion (``protected_media_plugins/mediacms.py::_get_allowed_hosts``) which the
    server then fetches with the user's credentials, echoing response and error
    differences back — a non-blind SSRF oracle available to any authenticated
    non-admin user.

    Safety is therefore judged by :mod:`app.utils.url_validation`, the one
    implementation in this codebase that resolves the name and inspects **every**
    address it answers with. What used to be here — a regex, a "must contain a dot"
    rule and a hardcoded first-label blocklist — never resolved anything, and passed
    ``169.254.169.254``, ``127.0.0.1``, ``10.0.0.5``, ``0.0.0.0`` and
    ``metadata.google.internal`` alike (audit finding A1).

    Args:
        v: The raw ``hostname`` field value, as ``host`` or ``host:port``.

    Returns:
        The normalised (stripped, lower-cased) value.

    Raises:
        ValueError: if the value is malformed, or names a host that must not be
            fetched server-side.
    """
    v = v.strip().lower()

    host_part = v
    if ":" in v:
        host_part, _, port_str = v.rpartition(":")
        if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
            raise ValueError("Invalid port number (must be 1-65535)")

    if not _HOSTNAME_RE.match(host_part):
        raise ValueError("Invalid hostname format")

    safe, reason = is_safe_url(f"https://{v}")
    if not safe:
        # The reason distinguishes "private address" from "cannot resolve", which would
        # turn this endpoint into a network scanner. Logged, never returned.
        logger.warning("Blocked media source hostname %r: %s", v, reason)
        raise ValueError(
            "Hostname must be a publicly reachable fully qualified domain name "
            "(e.g., media.example.com)"
        )
    return v


def _validate_provider_type_value(v: str) -> str:
    """Shared provider_type validation logic."""
    if v not in ALLOWED_PROVIDER_TYPES:
        raise ValueError(
            f"Unsupported provider type. Must be one of: {', '.join(sorted(ALLOWED_PROVIDER_TYPES))}"
        )
    return v


class UserMediaSourceCreate(BaseModel):
    """Schema for creating a new user media source."""

    hostname: str = Field(..., min_length=1, max_length=255)
    provider_type: str = Field(default="mediacms", max_length=50)
    username: str = Field(default="")
    password: str = Field(default="")
    verify_ssl: bool = True
    label: str = Field(default="", max_length=200)

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, v: str) -> str:
        return _validated_hostname(v)

    @field_validator("provider_type")
    @classmethod
    def validate_provider_type(cls, v: str) -> str:
        return _validate_provider_type_value(v)


class UserMediaSourceUpdate(BaseModel):
    """Schema for updating a media source."""

    hostname: str | None = None
    provider_type: str | None = None
    username: str | None = None
    password: str | None = None
    verify_ssl: bool | None = None
    label: str | None = None
    is_shared: bool | None = None

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, v: str | None) -> str | None:
        if v is not None:
            return _validated_hostname(v)
        return v

    @field_validator("provider_type")
    @classmethod
    def validate_provider_type(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_provider_type_value(v)
        return v


class UserMediaSourceResponse(BaseModel):
    """Response schema for a user media source."""

    uuid: str
    hostname: str
    provider_type: str
    username: str = ""
    has_credentials: bool = False
    verify_ssl: bool = True
    label: str = ""
    is_active: bool = True
    is_shared: bool = False
    shared_at: datetime | None = None
    owner_name: str | None = None
    owner_role: str | None = None
    is_own: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class UserMediaSourcesList(BaseModel):
    """Response schema for the list of user media sources."""

    sources: list[UserMediaSourceResponse] = []
    shared_sources: list[UserMediaSourceResponse] = []
